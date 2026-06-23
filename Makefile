define PENDING_COMPLETE_PY
import yaml, glob, os
print("Channels with pending import-mode completion:")
seen = set(); rows = []
for r in sorted(glob.glob("migration_logs/run_*/migration_report.yaml")):
    log = os.path.join(os.path.dirname(r), "migration.log")
    if not os.path.exists(log): continue
    content = open(log).read()
    real = content.split("DRY RUN VALIDATION COMPLETED")[-1]
    if "Incomplete imports" not in real: continue
    report = yaml.safe_load(open(r))
    run = os.path.basename(os.path.dirname(r))
    for ch in report.get("spaces", {}):
        if ch not in seen: rows.append("  " + run + " -> " + ch); seen.add(ch)
print("\n".join(rows) if rows else "  (none -- all spaces already completed)")
endef
export PENDING_COMPLETE_PY

.PHONY: install lint format format-check typecheck test test-cov check fix clean incomplete-imports pending-complete

install:
	pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check src/slack_chat_migrator/ tests/

fix:
	ruff check --fix src/slack_chat_migrator/ tests/
	ruff format src/slack_chat_migrator/ tests/

format:
	ruff format src/slack_chat_migrator/ tests/

format-check:
	ruff format --check src/slack_chat_migrator/ tests/

typecheck:
	mypy src/slack_chat_migrator/

test:
	pytest tests/ -v

test-cov:
	pytest tests/ --cov=slack_chat_migrator --cov-report=term-missing

check: lint format-check typecheck test

incomplete-imports:
	@for dir in migration_logs/*/; do \
		log="$$dir/migration.log"; \
		report="$$dir/migration_report.yaml"; \
		if grep -q "Incomplete imports" "$$log" 2>/dev/null; then \
			channels=$$(grep -A1 "^spaces:" "$$report" | grep ":" | grep -v "^spaces:" | tr -d ' :'); \
			echo "$$(basename $$dir) → $$channels"; \
		fi \
	done

pending-complete:
	@printf '%s\n' "$$PENDING_COMPLETE_PY" | .venv/bin/python3

clean:
	rm -rf build/ dist/ *.egg-info .mypy_cache .pytest_cache .coverage coverage.xml htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
