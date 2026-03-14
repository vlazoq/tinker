34/34 tests ran — one failure in the topological order (edge direction was inverted in Kahn's algorithm). Quick fix:**34/34 tests passing.** Now let's copy everything to outputs.**34/34 tests passing.** Here's what was built:

---

## Module structure

```
tinker/task_engine/
├── __init__.py          Clean public API surface
├── schema.py            Task dataclass + TaskType/TaskStatus/Subsystem enums
├── registry.py          SQLite-backed TaskRegistry (CRUD + query helpers)
├── generator.py         TaskGenerator (parses Architect JSON candidate_tasks)
├── scorer.py            PriorityScorer (5-factor weighted scoring + explain())
├── resolver.py          DependencyResolver (block/unblock + Kahn topo-sort)
├── queue.py             TaskQueue (get_next + full lifecycle transitions)
└── tests/
    └── test_task_engine.py  7 test classes, 34 tests
```

---

## Key design decisions

**Task schema** — every scoring signal lives directly on the Task dataclass so the scorer is purely functional (no DB calls during scoring). `to_dict()`/`from_dict()` provide clean SQLite round-trips.

**PriorityScorer** uses five mathematically distinct components:
- `confidence_gap` — linear passthrough of the 0-1 uncertainty signal
- `recency` — exponential inverse-decay (half-life tunable, default 4h)
- `staleness` — sigmoid saturation (prevents indefinite starvation, saturates at 24h)
- `dependency_depth` — exponential penalty (shallow tasks surface first)
- `type_bonus` — static value table (synthesis > design > validation > critique > research > exploration)

**Exploration slot** — implemented probabilistically in `TaskQueue._pick_task()`. A random threshold is drawn in `[exploration_min_pct, exploration_max_pct]` on every `get_next()` call. If the roll lands in the band *and* an exploration task exists, it wins — regardless of score order.

**DependencyResolver** handles three scenarios cleanly: initial block-check on save, reactive unblocking on completion, and full `resolve_all()` batch scan. Kahn's algorithm is also exposed for topological ordering and cycle detection.

**TaskQueue** owns all lifecycle transitions: `get_next()` → `push_to_critique()` → `accept_critique()` / `reject_critique()` / `fail_task()`.