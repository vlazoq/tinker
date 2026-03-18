# =============================================================================
# Tinker — developer automation
#
# Usage:
#   make install        install production deps (exact pinned versions)
#   make install-dev    install dev deps (exact pinned versions + tooling)
#   make deps           regenerate requirements/base.txt    (with SHA-256 hashes)
#   make deps-dev       regenerate requirements/dev.txt     (with SHA-256 hashes)
#   make deps-metrics   regenerate requirements/metrics.txt (with SHA-256 hashes)
#   make deps-all       regenerate all three lock files
#   make test           run the full test suite
#   make lint           run ruff linter
#   make fmt            auto-format with ruff
#   make audit          scan all lock files for CVEs via pip-audit
#   make audit-fix      automatically upgrade vulnerable packages
#   make clean          remove __pycache__ and .pytest_cache trees
#
# Lock-file security
# ------------------
# All `deps*` targets pass --generate-hashes to pip-compile.  The resulting
# requirements/*.txt files include a SHA-256 hash for every downloaded wheel
# and sdist.  pip enforces those hashes automatically — it refuses to install
# a package whose downloaded artifact doesn't match the committed hash.
# This prevents malicious package substitution (supply-chain attacks).
#
# After editing any requirements/*.in source file:
#   1. make deps-all           # regenerate lock files with fresh hashes
#   2. git add requirements/   # commit the updated lock files
#   3. git commit -m "chore: update dependency lock files"
#
# CI (see .github/workflows/ci.yml) enforces that the committed lock files
# match what pip-compile --generate-hashes produces.  A PR that edits *.in
# without updating *.txt will fail the dep-freshness job.
# =============================================================================

.PHONY: install install-dev deps deps-dev deps-all test lint fmt clean audit audit-fix

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
	    --generate-hashes \
	    --annotate \
	    --strip-extras \
	    --upgrade
	@echo "✓ requirements/base.txt regenerated (with SHA-256 hashes)"

deps-dev:
	pip-compile requirements/dev.in \
	    --output-file requirements/dev.txt \
	    --generate-hashes \
	    --annotate \
	    --strip-extras \
	    --upgrade
	@echo "✓ requirements/dev.txt regenerated (with SHA-256 hashes)"

deps-metrics:
	pip-compile requirements/metrics.in \
	    --output-file requirements/metrics.txt \
	    --generate-hashes \
	    --annotate \
	    --strip-extras \
	    --upgrade
	@echo "✓ requirements/metrics.txt regenerated (with SHA-256 hashes)"

deps-all: deps deps-dev deps-metrics
	@echo "✓ all lock files regenerated (with SHA-256 hashes)"

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

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

audit:
	@# Scan all lock files for published CVEs using pip-audit.
	@# pip-audit queries the OSV (Open Source Vulnerabilities) database and
	@# the GitHub Advisory Database.
	@#
	@# NOTE: lock files must contain --generate-hashes lines for --require-hashes
	@# to work.  Run `make deps-all` if you see "Missing hash" errors.
	pip-audit --requirement requirements/base.txt    --require-hashes --progress-spinner=off
	pip-audit --requirement requirements/dev.txt     --require-hashes --progress-spinner=off
	pip-audit --requirement requirements/metrics.txt --require-hashes --progress-spinner=off
	@echo "✓ no CVEs found in any lock file"

audit-fix:
	@# Like `make audit` but automatically upgrades any vulnerable packages.
	@# After running, re-run `make deps-all` to regenerate the lock files with
	@# the upgraded versions, then review the diff carefully before committing.
	pip-audit --requirement requirements/base.txt    --require-hashes --fix
	pip-audit --requirement requirements/dev.txt     --require-hashes --fix
	pip-audit --requirement requirements/metrics.txt --require-hashes --fix
	@echo "✓ vulnerable packages upgraded — re-run: make deps-all"
