# Tools run from .venv, not through `uv run`: a bare `uv run` locks the whole
# project, and the two eduTAP requirements are direct references that resolve
# against git rather than an index -- which fails wherever the network or the
# credentials for it are absent.
PYTHON := .venv/bin/python
VENV   := .venv

.DEFAULT_GOAL := help
.PHONY: help venv lint reformat test-local test-integration run

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'

venv: ## Create .venv and install the package with its dev extra
	test -d $(VENV) || uv venv
	uv pip install -U -e ".[dev]"

lint: venv ## Run ruff checks and the type checker
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests
	$(PYTHON) -m ty check src

reformat: venv ## Autoformat and autofix
	$(PYTHON) -m ruff format src tests
	$(PYTHON) -m ruff check --fix src tests

test-local: venv ## Unit tests, no database and no bucket needed
	$(PYTHON) -m pytest -v

test-integration: venv ## Integration tests against a PostgreSQL container
	$(PYTHON) -m pytest -m integration -v

run: venv ## Start the service against the compose environment
	$(PYTHON) -m uvicorn edutap.image_service.api.app:create_app --factory --reload
