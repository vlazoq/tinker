**21/21. All tests pass.** Now packaging everything up cleanly.**21/21 tests pass.** Here's what changed and why.

---

### Fixes applied in this continuation

**Bug 1 — `list_snapshots` glob** (`loop_*.json` → `loop_*_*.json`): The timestamped filenames (`loop_0001_20260313T...Z.json`) contain two underscores before the extension, so the original single-wildcard pattern silently matched nothing in some glob implementations.

**Bug 2 — `list_snapshots` AttributeError** (`s.updated_at.isoformat()`): The stdlib rewrite stores `updated_at` as an ISO string, not a `datetime` object, so calling `.isoformat()` on it threw `AttributeError` which was swallowed by the `except Exception: pass`. Fixed by using `s.updated_at` directly. This is also a good lesson for that pattern — bare `except Exception: pass` during development should at minimum log.

---

### Final file map

```
tinker/architecture_state/
├── __init__.py     clean re-exports of the full public API
├── schema.py       ArchitectureState + all sub-models (stdlib dataclasses,
│                   no pydantic), JSON round-trip, ConfidenceScore.absorb()
├── merger.py       stateless merge_update() — additive, immutable history
└── manager.py      ArchitectureStateManager:
                      apply_update()         → merge + persist + git commit
                      summarise(budget)      → token-budgeted context string
                      diff(loop_a, loop_b)   → human-readable change summary
                      list_snapshots()       → metadata for all loop archives
                      low_confidence_components(threshold)
                      unresolved_questions()
                      decisions_for_subsystem(name)
                      speculative_decisions()
                      components_by_subsystem(name)
                      confidence_map()       → flat {kind:name → float} dict

tinker/tests/
└── run_test.py     standalone 3-loop integration test (no pytest needed)
```