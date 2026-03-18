# =============================================================================
# Tinker — developer automation
#
# Usage:
#   make install        install production deps (exact pinned versions)
#   make install-dev    install dev deps (exact pinned versions + tooling)
#   make deps           regenerate requirements/base.txt from base.in
#   make deps-dev       regenerate requirements/dev.txt from dev.in
#   make test           run the full test suite
#   make lint           run ruff linter
#   make fmt            auto-format with ruff
#   make clean          remove __pycache__ and .pytest_cache trees
# =============================================================================

.PHONY: install install-dev deps deps-dev deps-all test lint fmt clean

# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

install:
	pip install -r requirements/base.txt
	pip install -e . --no-deps

install-dev:
	pip install -r requirements/dev.txt
	pip install -e ".[dev]" --no-deps

# ---------------------------------------------------------------------------
# Lock-file regeneration
#
# Always run after editing any requirements/*.in file.
# Commit the resulting *.txt files so CI and production use exact same pins.
# ---------------------------------------------------------------------------

deps:
	pip-compile requirements/base.in \
	    --output-file requirements/base.txt \
	    --annotate \
	    --strip-extras \
	    --upgrade
	@echo "✓ requirements/base.txt regenerated"

deps-dev:
	pip-compile requirements/dev.in \
	    --output-file requirements/dev.txt \
	    --annotate \
	    --strip-extras \
	    --upgrade
	@echo "✓ requirements/dev.txt regenerated"

deps-metrics:
	pip-compile requirements/metrics.in \
	    --output-file requirements/metrics.txt \
	    --annotate \
	    --strip-extras \
	    --upgrade
	@echo "✓ requirements/metrics.txt regenerated"

deps-all: deps deps-dev deps-metrics
	@echo "✓ all lock files regenerated"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test:
	pytest -x -q

test-v:
	pytest -x -v

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint:
	ruff check .

fmt:
	ruff format .

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✓ cleaned"
