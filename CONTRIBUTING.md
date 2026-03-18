# Contributing to Tinker

Thanks for wanting to contribute. This guide covers the essentials: getting set up, making changes, and submitting them.

---

## Quick start

```bash
git clone <repo-url>
cd tinker
make install-dev        # installs exact-pinned dev deps + the package in editable mode
```

See [SETUP.md](./SETUP.md) for full OS-specific instructions (Linux / macOS / Windows).

---

## Development workflow

```bash
make lint               # check code style (ruff)
make fmt                # auto-format in place (ruff)
make test               # run the full test suite (pytest -x -q)
make audit              # scan lock files for CVEs
```

Run `make lint` and `make test` before every commit.  CI enforces both.

---

## Code standards

### Docstrings — required on all public APIs

Every public module, class, and function must have a docstring.  Module-level
docstrings should explain *why the module exists* and *how it fits into the
larger system*, not just what it contains.

```python
# Good — explains purpose and context
"""
orchestrator/compat.py
======================
Async/sync compatibility helpers.  coroutine_if_needed() lets the orchestrator
await both async and sync component methods without caring which they are.
"""

# Bad — only describes content
"""Utility functions."""
```

### Inline comments — explain WHY, not WHAT

```python
# Good
# Windows: ProactorEventLoop doesn't support add_signal_handler.
# Fall back to signal.signal() for SIGINT only.
except (NotImplementedError, RuntimeError):

# Bad
# catch exception
except (NotImplementedError, RuntimeError):
```

### Type hints

Use type hints throughout.  The codebase uses `from __future__ import annotations`
so forward references resolve correctly.

### No bare `except`

Always catch specific exceptions.  `except Exception` is acceptable in top-level
loop guards with a `logger.exception()` call; `except:` (bare) is never acceptable.

---

## Project architecture

Tinker is built around three nested async loops (micro → meso → macro) driven
by the `Orchestrator`.  Before changing any core component, read:

- [README.md](./README.md) — system overview and component map
- [Overview.md](./Overview.md) — design rationale
- [orchestrator/p7_orchestrator.md](./orchestrator/p7_orchestrator.md) — loop
  design and key decisions
- [docs/tutorial/](./docs/tutorial/) — 18-chapter step-by-step rebuild (start
  with chapter 00 if you're new to the codebase)

---

## Adding a new component

Tinker uses **dependency injection** throughout.  Components are passed into
the Orchestrator at construction time — they are never imported directly by the
orchestrator or loop modules.

1. Create your module in the appropriate package (e.g. `tools/my_tool.py`).
2. Write a module-level docstring explaining its role in the system.
3. Add the component to `orchestrator/stubs.py` with a matching stub so tests
   can instantiate the orchestrator without your real backend.
4. Wire the real component in `main.py` alongside the other real components.
5. Add tests in the component's `tests/` subdirectory (or a `test_<module>.py`
   file next to the module).

---

## Adding a new Grub minion

Grub is Tinker's code-implementation sub-agent.  Each minion handles one
specialised task (coding, testing, reviewing, debugging, refactoring).

1. Subclass `BaseMinion` from `grub/minions/base.py`.
2. Implement `async def run(self, task, context) -> MinionResult`.
3. Register the minion in `grub/registry.py`.
4. Add a skills Markdown file to `grub/skills/` if the minion needs
   domain-specific knowledge injected into its prompt.
5. Write integration tests in `grub/tests/`.

See [docs/tutorial/16-grub-minions.md](./docs/tutorial/16-grub-minions.md) for
a full walkthrough.

---

## Dependency management

Tinker uses a two-layer pinning strategy:

| File | Purpose |
|------|---------|
| `pyproject.toml` | Loose `>=` ranges — expresses intent |
| `requirements/*.in` | Source files for the lock files |
| `requirements/*.txt` | Exact `==` pins with SHA-256 hashes — used by CI and production |

**After changing any dependency:**

```bash
make deps-all           # regenerate all lock files with fresh hashes
git add requirements/   # commit the updated lock files with your change
```

Never edit `requirements/*.txt` by hand.  CI will reject a PR where the lock
files don't match what `pip-compile --generate-hashes` produces.

See [requirements/README.md](./requirements/README.md) for full details.

---

## Pull request checklist

- [ ] `make lint` passes (no ruff errors)
- [ ] `make test` passes (all tests green)
- [ ] New public APIs have docstrings
- [ ] New complex logic has inline comments explaining *why*
- [ ] If you changed dependencies: `make deps-all` was run and lock files committed
- [ ] If you added a new component: a stub was added to `orchestrator/stubs.py`

---

## Getting help

- **Architecture questions**: read [docs/tutorial/](./docs/tutorial/) — the 18-chapter
  series rebuilds the entire system from scratch and explains every decision.
- **Setup problems**: see [SETUP.md](./SETUP.md) (Linux / macOS / Windows).
- **Bug reports**: open an issue with a minimal reproduction case and the output
  of `python --version` and your OS.
