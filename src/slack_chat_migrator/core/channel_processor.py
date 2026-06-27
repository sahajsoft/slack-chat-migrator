"""Channel-level processing logic extracted from the main migrator."""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from slack_chat_migrator.core.context import MigrationContext
    from slack_chat_migrator.core.progress import ProgressTracker
    from slack_chat_migrator.core.state import MigrationState
    from slack_chat_migrator.services.chat_adapter import ChatAdapter
    from slack_chat_migrator.services.files.file import FileHandler
    from slack_chat_migrator.services.messages.message_attachments import (
        MessageAttachmentProcessor,
    )
    from slack_chat_migrator.services.user_resolver import UserResolver

from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

from slack_chat_migrator.constants import (
    API_THROTTLE_MESSAGE_SECONDS,
    CHECKPOINT_INTERVAL_MESSAGES,
)
from slack_chat_migrator.core.config import (
    ImportCompletionStrategy,
    should_process_channel,
)
from slack_chat_migrator.exceptions import SpacePermissionError
from slack_chat_migrator.services.messages.message_builder import (
    build_user_map_with_overrides,
)
from slack_chat_migrator.services.messages.message_sender import (
    send_message,
    track_message_stats,
)
from slack_chat_migrator.services.spaces.discovery import get_last_message_timestamp
from slack_chat_migrator.services.spaces.historical_membership import add_users_to_space
from slack_chat_migrator.services.spaces.regular_membership import add_regular_members
from slack_chat_migrator.services.spaces.space_creator import create_space
from slack_chat_migrator.types import MessageResult
from slack_chat_migrator.utils.logging import (
    is_debug_api_enabled,
    log_with_context,
    setup_channel_logger,
)


class ChannelResult(NamedTuple):
    """Result of processing a single channel."""

    should_abort: bool
    had_errors: bool


class ChannelProcessor:
    """Handles per-channel processing during migration."""

    _CHECKPOINT_INTERVAL = CHECKPOINT_INTERVAL_MESSAGES

    def __init__(
        self,
        ctx: MigrationContext,
        state: MigrationState,
        chat: ChatAdapter,
        user_resolver: UserResolver,
        file_handler: FileHandler | None,
        attachment_processor: MessageAttachmentProcessor,
        progress_tracker: ProgressTracker | None = None,
        on_partial_progress: Callable[[str, float], None] | None = None,
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.chat = chat
        self.user_resolver = user_resolver
        self.file_handler = file_handler
        self.attachment_processor = attachment_processor
        self.progress_tracker = progress_tracker
        self.on_partial_progress = on_partial_progress

    def process_channel(self, ch_dir: Path) -> ChannelResult:
        """Process a single channel directory.

        Creates or reuses a space, imports messages, completes import mode,
        and adds members.

        Args:
            ch_dir: Path to the channel's export directory.

        Returns:
            ChannelResult with should_abort and had_errors fields.
        """
        channel = ch_dir.name

        self.state.context.current_channel = channel

        log_with_context(
            logging.INFO,
            f"{self.ctx.log_prefix}Processing channel: {channel}",
            channel=channel,
        )

        # Check if channel should be processed
        if not should_process_channel(channel, self.ctx.config):
            log_with_context(
                logging.WARNING,
                f"Skipping channel {channel} based on configuration",
                channel=channel,
            )
            return ChannelResult(should_abort=False, had_errors=False)

        self.state.progress.migration_summary["channels_processed"].append(channel)

        # Check for unresolved space conflicts
        if channel in self.state.errors.channel_conflicts:
            log_with_context(
                logging.ERROR,
                f"Skipping channel {channel} due to unresolved duplicate space conflict",
                channel=channel,
            )
            self.state.errors.migration_issues[channel] = (
                "Skipped due to duplicate space conflict - requires disambiguation in config.yaml"
            )
            return ChannelResult(should_abort=False, had_errors=True)

        # Setup channel-specific logging
        self._setup_channel_logging(channel)

        # Emit CHANNEL_START only after we know we'll actually process this channel
        if self.progress_tracker:
            self.progress_tracker.channel_start(channel)

        # Initialize error tracking
        channel_had_errors = False

        # Create or reuse space
        try:
            space, is_newly_created = self._create_or_reuse_space(ch_dir)
        except SpacePermissionError:
            log_with_context(
                logging.WARNING,
                f"Skipping channel {channel} due to space creation permission error",
                channel=channel,
            )
            if self.progress_tracker:
                self.progress_tracker.channel_complete(channel)
            return ChannelResult(should_abort=False, had_errors=True)

        # Set current space
        self.state.context.current_space = space
        self.state.spaces.channel_to_space[channel] = space
        self.state.spaces.created_spaces[channel] = space

        log_with_context(
            logging.DEBUG,
            f"Setting current space to {space} for channel {channel} and storing in channel_to_space mapping",
            channel=channel,
        )

        # Add historical memberships for newly created spaces
        all_memberships_failed = False
        if is_newly_created:
            log_with_context(
                logging.INFO,
                f"{self.ctx.log_prefix}Step 2/6: Adding historical memberships for {channel}",
                channel=channel,
            )
            members_added, members_failed = add_users_to_space(
                self.ctx,
                self.state,
                self.chat,
                self.user_resolver,
                space,
                channel,
                self.progress_tracker,
            )
            if members_failed > 0 and members_added == 0:
                channel_had_errors = True
                all_memberships_failed = True
        else:
            log_with_context(
                logging.INFO,
                "[UPDATE MODE] Skipping historical memberships for existing space (already has history)",
                channel=channel,
            )

        # Skip message processing if all memberships failed — every message
        # would fail with a 400 because the impersonated users aren't in the space.
        if all_memberships_failed:
            log_with_context(
                logging.WARNING,
                f"Skipping message processing for {channel} — all historical "
                "memberships failed, so messages sent via impersonation would fail",
                channel=channel,
            )
            processed_count, failed_count = 0, 0
        else:
            # Process messages
            processed_count, failed_count, channel_had_errors = self._process_messages(
                ch_dir, space, channel_had_errors
            )

        # Complete import mode for newly created spaces
        if is_newly_created:
            channel_had_errors = self._complete_import_mode(
                space, channel, channel_had_errors
            )

        # Add members
        channel_had_errors = self._add_members(
            space, channel, is_newly_created, channel_had_errors
        )

        # Log completion
        log_with_context(
            logging.DEBUG,
            f"Channel log file completed for channel: {channel}",
            channel=channel,
        )

        # Check if we should abort
        if self._should_abort_import(
            channel, processed_count, failed_count, channel_had_errors
        ):
            log_with_context(
                logging.WARNING,
                "Aborting import after first channel due to errors",
                channel=channel,
            )
            if self.progress_tracker:
                self.progress_tracker.channel_complete(channel)
            return ChannelResult(should_abort=True, had_errors=True)

        # Delete space if errors
        if channel_had_errors and not self.ctx.update_mode:
            self._delete_space_if_errors(space, channel)

        if self.progress_tracker:
            self.progress_tracker.channel_complete(channel)
        return ChannelResult(should_abort=False, had_errors=channel_had_errors)

    def _setup_channel_logging(self, channel: str) -> None:
        """Set up channel-specific log handler."""
        if self.state.context.output_dir is None:
            raise RuntimeError("Output directory not set")
        channel_handler = setup_channel_logger(
            self.state.context.output_dir,
            channel,
            self.ctx.verbose,
            is_debug_api_enabled(),
        )
        self.state.spaces.channel_handlers[channel] = channel_handler

    def _create_or_reuse_space(self, ch_dir: Path) -> tuple[str, bool]:
        """Create a new space or reuse an existing one.

        Returns (space_name, is_newly_created).
        """
        channel = ch_dir.name

        if self.ctx.update_mode and channel in self.state.spaces.created_spaces:
            space = self.state.spaces.created_spaces[channel]
            space_id = space.split("/")[-1] if space.startswith("spaces/") else space
            log_with_context(
                logging.INFO,
                f"[UPDATE MODE] Using existing space {space_id} for channel {channel}",
                channel=channel,
            )
            self.state.spaces.space_cache[channel] = space
            return space, False
        else:
            action_desc = (
                "Creating new import mode space"
                if not self.ctx.update_mode
                else "Creating new space (none found in update mode)"
            )
            log_with_context(
                logging.INFO,
                f"{self.ctx.log_prefix}Step 1/6: {action_desc} for {channel}",
                channel=channel,
            )
            space = self.state.spaces.space_cache.get(channel) or create_space(
                self.ctx,
                self.state,
                self.chat,
                self.user_resolver,
                channel,
            )
            self.state.spaces.space_cache[channel] = space
            if self.progress_tracker:
                self.progress_tracker.space_created(channel)
            return space, True

    def _process_messages(
        self, ch_dir: Path, space: str, channel_had_errors: bool
    ) -> tuple[int, int, bool]:
        """Load, deduplicate, and send messages for a channel.

        Returns (processed_count, failed_count, channel_had_errors).
        """
        channel = ch_dir.name

        log_with_context(
            logging.INFO,
            f"{self.ctx.log_prefix}Step 3/6: Processing messages for {channel}",
            channel=channel,
        )

        msgs = self._load_and_sort_messages(channel)
        msgs = self._deduplicate_messages(msgs, channel)

        # Emit message phase start so renderers can create a progress bar
        message_count = sum(1 for m in msgs if m.get("type") == "message")
        if self.progress_tracker and message_count > 0:
            self.progress_tracker.message_phase_start(channel, total=message_count)

        if self.ctx.dry_run:
            log_with_context(
                logging.INFO,
                f"{self.ctx.log_prefix}Found {message_count} messages in channel {channel}",
                channel=channel,
            )

        # Resource discovery queries the Chat API for the last message
        # timestamp (for resumption).  Skipped in dry-run because the stub
        # always returns an empty list — there are no real messages to find.
        if not self.ctx.dry_run or self.ctx.update_mode:
            self._discover_channel_resources(channel)

        # Fast-forward past already-processed messages using the checkpoint
        # timestamp so resume doesn't iterate through thousands of skips.
        last_ts = self.state.progress.last_processed_timestamps.get(channel, 0)
        skipped = 0
        if last_ts > 0:
            before = len(msgs)
            msgs = [m for m in msgs if float(m.get("ts", 0)) > last_ts]
            skipped = before - len(msgs)
            if skipped:
                log_with_context(
                    logging.INFO,
                    f"[RESUME] Fast-forwarded past {skipped} already-processed messages for {channel}",
                    channel=channel,
                )
                # Advance the progress bar to the correct starting position so
                # the UI shows cumulative progress (e.g. 8487/13541) rather
                # than resetting to 0 after fast-forward.
                if self.progress_tracker:
                    self.progress_tracker.message_sent(
                        channel, count=skipped, total=message_count
                    )

        # Build user map with overrides once per channel.
        cached_user_map = build_user_map_with_overrides(self.ctx, self.user_resolver)

        processed_count, failed_count, channel_had_errors = self._send_messages_loop(
            msgs, space, channel, channel_had_errors, cached_user_map,
            progress_offset=skipped,
        )

        # Retry failed messages up to 2 times (handles transient network errors).
        _MAX_SEND_RETRIES = 2
        for retry_num in range(1, _MAX_SEND_RETRIES + 1):
            failed_ts = set(
                self.state.messages.failed_messages_by_channel.get(channel, [])
            )
            if not failed_ts:
                break
            retry_msgs = [m for m in msgs if m.get("ts") in failed_ts]
            if not retry_msgs:
                break

            log_with_context(
                logging.INFO,
                f"[RETRY {retry_num}/{_MAX_SEND_RETRIES}] Re-sending {len(retry_msgs)}"
                f" failed messages for {channel}",
                channel=channel,
            )

            # Remove the previous failure records so they don't double-count
            # if they succeed on retry.
            self.state.messages.failed_messages_by_channel.pop(channel, None)
            self.state.messages.failed_messages = [
                fm
                for fm in self.state.messages.failed_messages
                if not (fm["channel"] == channel and fm["ts"] in failed_ts)
            ]

            retry_processed, retry_failed, channel_had_errors = self._send_messages_loop(
                retry_msgs, space, channel, channel_had_errors, cached_user_map,
                progress_offset=skipped + processed_count,
            )
            processed_count += retry_processed
            # Replace failed_count: net of messages that still fail after retry
            failed_count = failed_count - len(failed_ts) + retry_failed

        log_with_context(
            logging.INFO,
            f"Channel {channel} message import: processed {processed_count}, failed {failed_count}",
            channel=channel,
        )

        return processed_count, failed_count, channel_had_errors

    def _load_and_sort_messages(self, channel: str) -> list[dict[str, Any]]:
        """Load all messages from JSON files and sort by timestamp."""
        msg_dir = self.ctx.export_root / channel
        msgs: list[dict[str, Any]] = []
        for jf in sorted(msg_dir.glob("*.json")):
            try:
                with open(jf, encoding="utf-8") as f:
                    msgs.extend(json.load(f))
            except (OSError, ValueError) as e:
                log_with_context(
                    logging.WARNING,
                    f"Failed to load messages from {jf}: {e}",
                    channel=channel,
                )

        return sorted(msgs, key=lambda m: float(m.get("ts", "0")))

    def _deduplicate_messages(
        self, msgs: list[dict[str, Any]], channel: str
    ) -> list[dict[str, Any]]:
        """Remove duplicate messages based on timestamp."""
        seen_timestamps: set[str] = set()
        deduped: list[dict[str, Any]] = []
        duplicate_count = 0

        for msg in msgs:
            ts = msg.get("ts")
            if ts and ts not in seen_timestamps:
                seen_timestamps.add(ts)
                deduped.append(msg)
            elif ts:
                duplicate_count += 1
                log_with_context(
                    logging.DEBUG,
                    f"Skipping duplicate message with timestamp {ts}",
                    channel=channel,
                    ts=ts,
                )

        if duplicate_count > 0:
            log_with_context(
                logging.INFO,
                f"Deduplicated {duplicate_count} messages in channel {channel} (likely thread reply duplicates)",
                channel=channel,
            )

        return deduped

    def _send_messages_loop(
        self,
        msgs: list[dict[str, Any]],
        space: str,
        channel: str,
        channel_had_errors: bool,
        user_map_with_overrides: dict[str, str] | None = None,
        progress_offset: int = 0,
    ) -> tuple[int, int, bool]:
        """Iterate over messages, sending each and tracking results.

        Returns (processed_count, failed_count, channel_had_errors).
        """
        workers = self.ctx.config.parallel_message_workers
        if workers > 1:
            return self._send_messages_parallel(
                msgs, space, channel, channel_had_errors,
                user_map_with_overrides, progress_offset, workers,
            )

        processed_ts: list[str] = []
        processed_count = 0
        failed_count = 0
        max_failure_percentage = self.ctx.config.max_failure_percentage
        channel_failures: list[str] = []
        total_sendable = sum(1 for m in msgs if m.get("type") == "message")

        for m in msgs:
            if m.get("type") != "message":
                continue

            ts = m["ts"]

            if ts in processed_ts:
                processed_count += 1
                continue

            track_message_stats(
                self.ctx,
                self.state,
                self.user_resolver,
                self.attachment_processor,
                m,
            )

            result = send_message(
                self.ctx,
                self.state,
                self.chat,
                self.user_resolver,
                self.attachment_processor,
                space,
                m,
                user_map_with_overrides=user_map_with_overrides,
            )

            if result.failed:
                failed_count += 1
                channel_failures.append(ts)
                if self.progress_tracker:
                    self.progress_tracker.message_failed(channel, detail=result.error)

                if processed_count > 0:
                    failure_percentage = (
                        failed_count / (processed_count + failed_count)
                    ) * 100
                    if failure_percentage > max_failure_percentage:
                        log_with_context(
                            logging.WARNING,
                            f"Failure rate {failure_percentage:.1f}% exceeds threshold {max_failure_percentage}% for channel {channel}",
                            channel=channel,
                        )
                        channel_had_errors = True
                        self.state.errors.high_failure_rate_channels[channel] = (
                            failure_percentage
                        )
            elif result.skipped != MessageResult.SKIPPED:
                processed_ts.append(ts)
                processed_count += 1
                if self.progress_tracker:
                    self.progress_tracker.message_sent(
                        channel,
                        count=progress_offset + processed_count,
                        total=progress_offset + total_sendable,
                    )
                if (
                    self.on_partial_progress
                    and processed_count % self._CHECKPOINT_INTERVAL == 0
                ):
                    self.on_partial_progress(channel, float(ts))

            time.sleep(
                API_THROTTLE_MESSAGE_SECONDS
            )  # Throttle to avoid Chat API rate limits

        if channel_failures:
            self.state.messages.failed_messages_by_channel[channel] = channel_failures
            channel_had_errors = True

        return processed_count, failed_count, channel_had_errors

    def _send_messages_parallel(
        self,
        msgs: list[dict[str, Any]],
        space: str,
        channel: str,
        channel_had_errors: bool,
        user_map_with_overrides: dict[str, str] | None = None,
        progress_offset: int = 0,
        workers: int = 30,
    ) -> tuple[int, int, bool]:
        """Send messages in parallel batches of `workers` size.

        Each batch is fully completed before checkpointing, so the checkpoint
        ts is always safe to resume from with no missing messages.
        """
        sendable = [m for m in msgs if m.get("type") == "message"]
        total_sendable = len(sendable)
        max_failure_percentage = self.ctx.config.max_failure_percentage
        channel_failures: list[str] = []
        processed_ts_set: set[str] = set()
        processed_count = 0
        failed_count = 0
        lock = threading.Lock()

        log_with_context(
            logging.INFO,
            f"[PARALLEL] Sending {total_sendable} messages with {workers} workers for {channel}",
            channel=channel,
        )

        # One executor for ALL batches: threads survive across batches and reuse
        # their thread-local httplib2.Http connections.  A fresh executor per
        # batch would destroy those threads, forcing every batch to re-acquire
        # _service_build_lock (~2 s each x 30 threads = ~60 s per batch).
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for batch_start in range(0, len(sendable), workers):
                batch = [
                    m for m in sendable[batch_start : batch_start + workers]
                    if m.get("ts") not in processed_ts_set
                ]
                if not batch:
                    continue

                # Stats tracking is fast and writes shared counters — run sequentially.
                for m in batch:
                    track_message_stats(
                        self.ctx, self.state, self.user_resolver,
                        self.attachment_processor, m,
                    )

                # API sends are slow (network I/O) — run concurrently.
                future_to_m = {
                    executor.submit(
                        send_message,
                        self.ctx, self.state, self.chat,
                        self.user_resolver, self.attachment_processor,
                        space, m, user_map_with_overrides,
                    ): m
                    for m in batch
                }

                for future in as_completed(future_to_m):
                    m = future_to_m[future]
                    ts = m["ts"]
                    try:
                        result = future.result()
                    except Exception as exc:
                        log_with_context(
                            logging.ERROR,
                            f"Unexpected error sending message {ts}: {exc}",
                            channel=channel,
                        )
                        with lock:
                            failed_count += 1
                            channel_failures.append(ts)
                        continue

                    with lock:
                        if result.failed:
                            failed_count += 1
                            channel_failures.append(ts)
                            if self.progress_tracker:
                                self.progress_tracker.message_failed(
                                    channel, detail=result.error
                                )
                            if processed_count > 0:
                                failure_pct = (
                                    failed_count / (processed_count + failed_count) * 100
                                )
                                if failure_pct > max_failure_percentage:
                                    log_with_context(
                                        logging.WARNING,
                                        f"Failure rate {failure_pct:.1f}% exceeds threshold "
                                        f"{max_failure_percentage}% for {channel}",
                                        channel=channel,
                                    )
                                    channel_had_errors = True
                                    self.state.errors.high_failure_rate_channels[
                                        channel
                                    ] = failure_pct
                        elif result.skipped != MessageResult.SKIPPED:
                            processed_ts_set.add(ts)
                            processed_count += 1
                            if self.progress_tracker:
                                self.progress_tracker.message_sent(
                                    channel,
                                    count=progress_offset + processed_count,
                                    total=progress_offset + total_sendable,
                                )

                # All messages in batch are done — safe to checkpoint at batch's last ts.
                batch_last_ts = float(batch[-1]["ts"])
                if self.on_partial_progress:
                    self.on_partial_progress(channel, batch_last_ts)

        if channel_failures:
            self.state.messages.failed_messages_by_channel[channel] = channel_failures
            channel_had_errors = True

        return processed_count, failed_count, channel_had_errors

    def _complete_import_mode(
        self, space: str, channel: str, channel_had_errors: bool
    ) -> bool:
        """Complete import mode for a newly created space.

        Returns updated channel_had_errors.
        """
        log_with_context(
            logging.INFO,
            f"{self.ctx.log_prefix}Step 4/6: Completing import mode for {channel}",
            channel=channel,
        )

        # Get the completion strategy from config
        completion_strategy = self.ctx.config.import_completion_strategy

        # Only complete import if there were no errors or we're using force_complete strategy
        if (
            not channel_had_errors
            or completion_strategy == ImportCompletionStrategy.FORCE_COMPLETE
        ):
            try:
                log_with_context(
                    logging.DEBUG,
                    f"Attempting to complete import mode for space {space}",
                    channel=channel,
                )

                complete_import_response = self.chat.complete_import(space)

                log_with_context(
                    logging.INFO,
                    f"Successfully completed import mode for space: {space}",
                    channel=channel,
                )
                log_with_context(
                    logging.DEBUG,
                    f"completeImport response for {space}: {complete_import_response}",
                    channel=channel,
                )

                # Mirror Slack's public/private setting: a public Slack channel
                # (is_private=False) becomes DISCOVERABLE; a private channel stays
                # PRIVATE (the Google Chat default).  The make_spaces_discoverable
                # config flag overrides this, forcing all spaces to be discoverable.
                channel_meta = self.ctx.channels_meta.get(channel, {})
                raw_is_private = channel_meta.get("is_private", "__missing__")
                channel_is_public = not channel_meta.get("is_private", True)

                log_with_context(
                    logging.DEBUG,
                    f"Visibility decision for {channel}: "
                    f"channels_meta has {len(channel_meta)} keys, "
                    f"is_private={raw_is_private!r}, "
                    f"channel_is_public={channel_is_public}, "
                    f"make_spaces_discoverable={self.ctx.config.make_spaces_discoverable}",
                    channel=channel,
                )

                if self.ctx.config.make_spaces_discoverable or channel_is_public:
                    log_with_context(
                        logging.DEBUG,
                        f"Patching {space} to DISCOVERABLE "
                        f"(reason: {'make_spaces_discoverable=true' if self.ctx.config.make_spaces_discoverable else 'public Slack channel'})",
                        channel=channel,
                    )
                    try:
                        patch_response = self.chat.patch_space(
                            name=space,
                            update_mask="accessSettings",
                            body={"accessSettings": {"accessState": "DISCOVERABLE"}},
                            use_admin_access=True,
                        )
                        actual_state = patch_response.get("accessSettings", {}).get(
                            "accessState", "UNKNOWN"
                        )
                        log_with_context(
                            logging.INFO,
                            f"Set space {space} to DISCOVERABLE "
                            f"(API confirmed accessState={actual_state!r})",
                            channel=channel,
                        )
                        if actual_state != "DISCOVERABLE":
                            log_with_context(
                                logging.WARNING,
                                f"patch_space succeeded but accessState is {actual_state!r} "
                                f"instead of 'DISCOVERABLE'. "
                                f"Space Discovery may not be enabled for this Google Workspace "
                                f"organisation — requires Business Standard or above. "
                                f"Manual workaround: open the space in Google Chat → "
                                f"space name → Settings → Who can join → "
                                f"'Anyone in <org>'.",
                                channel=channel,
                            )
                        log_with_context(
                            logging.DEBUG,
                            f"patch_space full response for {space}: {patch_response}",
                            channel=channel,
                        )
                    except HttpError as patch_e:
                        if patch_e.resp.status == 400 and "Invalid update mask" in str(
                            patch_e
                        ):
                            log_with_context(
                                logging.WARNING,
                                f"Cannot set space {space} to DISCOVERABLE — the Google Chat API "
                                f"rejected 'accessSettings' as an update mask field (HTTP 400). "
                                f"This happens when Space Discovery is not enabled for your "
                                f"Google Workspace organisation (requires Business Standard or above). "
                                f"Manual workaround: open the space in Google Chat → "
                                f"space name → Settings → Who can join → 'Anyone in <org>'.",
                                channel=channel,
                            )
                        else:
                            log_with_context(
                                logging.WARNING,
                                f"Failed to set space {space} discoverable: {patch_e}",
                                channel=channel,
                            )
                    except (RefreshError, TransportError) as patch_e:
                        log_with_context(
                            logging.WARNING,
                            f"Failed to set space {space} discoverable: {patch_e}",
                            channel=channel,
                        )
                else:
                    log_with_context(
                        logging.DEBUG,
                        f"Leaving {space} as PRIVATE "
                        f"(channel is_private={raw_is_private!r}, make_spaces_discoverable=false)",
                        channel=channel,
                    )

            except (HttpError, RefreshError, TransportError) as e:
                log_with_context(
                    logging.ERROR,
                    f"Failed to complete import for space {space}: {e}",
                    channel=channel,
                )
                channel_had_errors = True
                self.state.errors.incomplete_import_spaces.append((space, channel))
        elif channel_had_errors:
            log_with_context(
                logging.WARNING,
                f"Skipping import completion for space {space} due to errors (strategy: {completion_strategy})",
                channel=channel,
            )
            self.state.errors.incomplete_import_spaces.append((space, channel))

        return channel_had_errors

    def _add_members(
        self,
        space: str,
        channel: str,
        is_newly_created: bool,
        channel_had_errors: bool,
    ) -> bool:
        """Add or update current members in a space.

        Returns updated channel_had_errors.
        """
        step_desc = (
            "Adding current members to space"
            if is_newly_created
            else "Updating current members in existing space"
        )
        log_with_context(
            logging.INFO,
            f"{self.ctx.log_prefix}Step 5/6: {step_desc} for {channel}",
            channel=channel,
        )

        # Only skip member addition if import mode itself wasn't completed —
        # message/file/historical-membership errors are irrelevant here.
        # A space stuck in import mode cannot receive regular memberships.
        import_stuck = is_newly_created and any(
            s == space for s, _ in self.state.errors.incomplete_import_spaces
        )
        if not import_stuck:
            try:
                add_regular_members(
                    self.ctx,
                    self.state,
                    self.chat,
                    self.user_resolver,
                    self.file_handler,
                    space,
                    channel,
                    self.progress_tracker,
                )
                log_with_context(
                    logging.DEBUG,
                    f"Successfully updated current members for space {space} and channel {channel}",
                    channel=channel,
                )
            except (HttpError, RefreshError, TransportError) as e:
                log_with_context(
                    logging.ERROR,
                    f"Error updating current members for space {space}: {e}",
                    channel=channel,
                )
                log_with_context(
                    logging.DEBUG,
                    f"Exception traceback: {traceback.format_exc()}",
                    channel=channel,
                )
                channel_had_errors = True
            except Exception as e:
                # Catch-all: add_regular_members is a complex function that can raise
                # unexpected errors from file I/O, data lookups, and multiple API calls
                log_with_context(
                    logging.ERROR,
                    f"Unexpected error updating current members for space {space}: {e}",
                    channel=channel,
                )
                log_with_context(
                    logging.DEBUG,
                    f"Exception traceback: {traceback.format_exc()}",
                    channel=channel,
                )
                channel_had_errors = True
        else:
            log_with_context(
                logging.WARNING,
                f"Skipping member addition for space {space} — import mode was not completed, "
                f"regular memberships cannot be added while a space is in import mode",
                channel=channel,
            )

        return channel_had_errors

    def _should_abort_import(
        self,
        channel: str,
        processed_count: int,
        failed_count: int,
        channel_had_errors: bool = False,
    ) -> bool:
        """Determine if the migration should abort after errors in a channel."""
        # Only consider aborting if we had failures (messages or memberships)
        if failed_count > 0 or channel_had_errors:
            if failed_count > 0:
                detail = f"{failed_count} message import errors"
            else:
                detail = "errors during migration (e.g. membership failures)"
            log_with_context(
                logging.WARNING,
                f"Channel '{channel}' had {detail}.",
                channel=channel,
            )

            # Check config for abort_on_error setting
            should_abort = self.ctx.config.abort_on_error

            if should_abort:
                log_with_context(
                    logging.WARNING,
                    "Aborting import due to errors (abort_on_error is enabled in config)",
                    channel=channel,
                )
                return True
            else:
                log_with_context(
                    logging.WARNING,
                    "Continuing with migration despite errors (abort_on_error is disabled in config)",
                    channel=channel,
                )

        return False

    def _delete_space_if_errors(self, space_name: str, channel: str) -> None:
        """Delete a space if it had errors and cleanup is enabled."""
        if not self.ctx.config.cleanup_on_error:
            log_with_context(
                logging.INFO,
                f"Not deleting space {space_name} despite errors (cleanup_on_error is disabled in config)",
                space_name=space_name,
            )
            return

        try:
            log_with_context(
                logging.WARNING,
                f"Deleting space {space_name} due to errors",
                space_name=space_name,
            )
            self.chat.delete_space(space_name)
            log_with_context(
                logging.INFO,
                f"Successfully deleted space {space_name}",
                space_name=space_name,
            )

            # Remove from created_spaces
            if channel in self.state.spaces.created_spaces:
                del self.state.spaces.created_spaces[channel]

            # Decrement space count
            self.state.progress.migration_summary["spaces_created"] -= 1
        except (HttpError, RefreshError, TransportError) as e:
            log_with_context(
                logging.ERROR,
                f"Failed to delete space {space_name}: {e}",
                space_name=space_name,
            )

        log_with_context(logging.INFO, "Cleanup completed")

    def _discover_channel_resources(self, channel: str) -> None:
        """Find the last message timestamp in a space to determine where to resume."""
        # Check if we have a space for this channel
        space_name = self.state.spaces.channel_to_space.get(channel)
        if not space_name:
            log_with_context(
                logging.WARNING,
                f"No space found for channel {channel}, cannot determine last message timestamp",
                channel=channel,
            )
            return

        # Get the timestamp of the last message in the space
        last_timestamp = get_last_message_timestamp(self.chat, channel, space_name)

        if last_timestamp > 0:
            log_with_context(
                logging.INFO,
                f"Found last message timestamp for channel {channel}: {last_timestamp}",
                channel=channel,
            )

            # Store the last timestamp for this channel
            self.state.progress.last_processed_timestamps[channel] = last_timestamp

            # Initialize an empty thread_map so we don't try to load it again
            if self.state.messages.thread_map is None:
                self.state.messages.thread_map = {}
        else:
            # If no messages were found, log it but don't set a last timestamp
            # This will cause all messages to be imported
            log_with_context(
                logging.INFO,
                f"No existing messages found in space for channel {channel}, will import all messages",
                channel=channel,
            )
