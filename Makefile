# Developer shortcuts. Everything here is also what CI runs.
.DEFAULT_GOAL := help
PYTHON ?= python

.PHONY: help install lint format typecheck test regression cover check docs build clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## Install the package and dev tools into the current environment
	$(PYTHON) -m pip install -e ".[dev]"

lint:  ## Ruff lint + format check
	ruff check .
	ruff format --check .

format:  ## Apply ruff fixes and formatting
	ruff check . --fix
	ruff format .

typecheck:  ## mypy (strict)
	mypy

test:  ## Run the test suite
	pytest

regression:  ## Run only the end-to-end regression journeys
	pytest -m regression -v

cover:  ## Run the test suite with coverage
	pytest --cov --cov-report=term-missing

check: lint typecheck cover  ## Everything CI checks

docs:  ## Build the documentation site strictly (broken links fail)
	mkdocs build --strict

build:  ## Build sdist + wheel and validate the metadata
	$(PYTHON) -m build
	$(PYTHON) -m twine check --strict dist/*

clean:  ## Remove build and cache artefacts
	rm -rf dist build site .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage .coverage.*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
