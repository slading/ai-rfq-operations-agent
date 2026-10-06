# ---------------------------------------------------------------------------
# AI RFQ Operations Agent - developer entry points
# Phase 0 targets: install, lint, format, test, check, clean
# ---------------------------------------------------------------------------

VENV      ?= .venv
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
RUFF      := $(VENV)/bin/ruff
PYTEST    := $(VENV)/bin/pytest
PYTHON_VERSION ?= 3.13

.DEFAULT_GOAL := help
.PHONY: help venv install lint format test check autofix clean migrate seed seed-reset

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	python$(PYTHON_VERSION) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: ## Install runtime + dev dependencies (editable)
	$(PIP) install -e ".[dev]"

lint: ## Exit check: ruff lint + format check (no writes)
	$(RUFF) check .
	$(RUFF) format --check .

format: ## Apply ruff autofixes and formatting
	$(RUFF) check --fix .
	$(RUFF) format .

autofix: format ## Alias for `make format`

test: ## Run the test suite (offline only; live Groq tests are opt-in)
	$(PYTEST) -m "not live"

migrate: ## Create/upgrade the SQLite schema (alembic upgrade head)
	$(VENV)/bin/alembic upgrade head

seed: ## Write the demo business dataset (idempotent; safe to re-run)
	$(PY) -m rfq_agent.seed

seed-reset: ## Delete the demo dataset rows and write them again from scratch
	$(PY) -m rfq_agent.seed --reset

check: lint test ## Full Phase 0 exit check

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -prune -exec rm -rf {} +
