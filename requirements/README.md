# requirements/ — Dependency Pinning

Tinker uses a **two-layer dependency strategy** that is standard practice
at production-grade Python shops.

---

## The two-layer system

| File | Format | Purpose |
|------|--------|---------|
| `pyproject.toml` (project root) | `>=` lower bounds | Developer-facing: describes what Tinker *needs* |
| `requirements/*.in` | `>=` lower bounds | Sub-package source files; compiled into `.txt` lock files |
| `requirements/base.txt` | `==` exact pins | **Production / CI**: exact byte-for-byte reproducible installs |
| `requirements/dev.txt` | `==` exact pins | **Development**: same as base + test runners and tooling |
| `requirements/metrics.txt` | `==` exact pins | Optional Prometheus metrics endpoint |

### Why two layers?

- **Loose ranges** (`>=`) in `pyproject.toml` let `pip` resolve a compatible
  set for each developer's environment.  This is useful during development
  and when Tinker is installed as a library inside another project.

- **Exact pins** in `*.txt` lock files guarantee that CI, staging, and
  production all run the exact same code — including every transitive
  dependency three levels deep.  A `chromadb` upgrade that breaks `grpcio`
  won't silently appear on a production deploy.

---

## Regenerating the lock files

The `.txt` files are **auto-generated** by `pip-compile` (from the
[pip-tools](https://pip-tools.readthedocs.io/) package).  Never edit them
by hand.

```bash
# Regenerate all lock files at once:
make deps-all

# Or individually:
make deps          # → requirements/base.txt
make deps-dev      # → requirements/dev.txt
make deps-metrics  # → requirements/metrics.txt
```

Always **commit the regenerated `.txt` files** so the entire team and CI
use the same resolved set.

---

## Installing from lock files

```bash
# Production / CI (no dev tools):
pip install -r requirements/base.txt
pip install -e . --no-deps

# Development (includes test runners, pip-tools, Textual devtools):
pip install -r requirements/dev.txt
pip install -e . --no-deps
```

---

## File map

```
requirements/
├── base.in        ← EDIT THIS to add/remove production deps
├── base.txt       ← auto-generated (commit this)
├── dev.in         ← EDIT THIS to add/remove dev-only deps
├── dev.txt        ← auto-generated (commit this)
├── metrics.in     ← EDIT THIS to add/remove metrics deps
├── metrics.txt    ← auto-generated (commit this)
└── README.md      ← you are here
```
