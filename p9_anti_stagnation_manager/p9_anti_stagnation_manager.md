Let me build this out fully. The Anti-Stagnation System is the most algorithmically dense component in Tinker — I'll structure it so every detector is independently testable and the monitor itself is a clean integration point for the Orchestrator.Now the test suite — one test class per failure mode, plus integration scenarios:26/26 tests pass. Let me copy to outputs and present:**26/26 tests passing.** Here's a map of what was built and the key decisions:

---

## Module breakdown

| File | Responsibility |
|---|---|
| `config.py` | All thresholds in one dataclass tree; `from_dict()` for runtime overrides |
| `models.py` | `MicroLoopContext`, `InterventionDirective`, `StagnationEvent`, enums, canonical `INTERVENTION_MAP` |
| `embeddings.py` | `OllamaEmbeddingBackend` (production) + `FallbackTFIDFBackend` (no deps, deterministic) |
| `detectors.py` | Five independent detectors, each with a `deque`-based sliding window and `reset()` |
| `event_log.py` | Thread-safe ring-buffer with `filter()`, `counts_by_type()`, and `to_dicts()` for dashboards |
| `monitor.py` | `StagnationMonitor` — orchestrates all detectors, builds directives, writes the log |
| `tests/` | 26 tests covering detection, false-positive suppression, severity bounds, edge cases |

---

## Detection algorithm notes

**Semantic Loop** — embeds each output and computes pairwise cosine similarity over a sliding window of N vectors. Fires when ≥ K pairs breach the threshold. Severity = fraction of breaching pairs. Uses the TF-IDF fallback when Ollama is unreachable.

**Subsystem Fixation** — counts tag frequency in the window; fires only when the window is *full* (avoids early false positives in the first few loops). The dominant subsystem is passed as `avoid_subsystem_hint` in the directive so the Orchestrator knows exactly where not to go.

**Critique Collapse** — blends two signals: the excess of the rolling mean above threshold, and the *uniformity* of scores (low stddev = suspiciously flat agreement). Both must be high for max severity.

**Research Saturation** — Jaccard similarity on consecutive URL-set pairs. Repeated URLs are extracted and forwarded as `exclude_urls` in the directive so the Researcher prompt can be instructed to avoid them.

**Task Starvation** — requires *both* conditions simultaneously: queue depth below threshold AND net generation negative for N consecutive samples. A deep queue with negative net rate doesn't trigger; a shallow queue with positive generation doesn't trigger. Both gates must open.

---

## Orchestrator integration

```python
from tinker.anti_stagnation import StagnationMonitor, MicroLoopContext

monitor = StagnationMonitor()   # or pass StagnationMonitorConfig(...)

# Inside the micro-loop:
ctx = MicroLoopContext(
    loop_index=i,
    output_text=architect_output,
    subsystem_tag=current_task.subsystem,
    critic_score=critic_result.confidence,
    research_urls=researcher_result.sources,
    queue_depth=task_engine.depth(),
    tasks_generated=new_tasks_count,
    tasks_consumed=1,
)
directives = monitor.check(ctx)   # [] = healthy, else act on directives[0]

if directives:
    top = directives[0]           # highest severity first
    # top.intervention_type → what to do
    # top.metadata           → hints (avoid_subsystem, exclude_urls, etc.)
```