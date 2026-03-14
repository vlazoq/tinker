# Tinker Observability Dashboard

> **Component 10 of 10** — Real-time terminal UI for monitoring the Tinker autonomous architecture engine.

---

## What It Is

The Dashboard is the window into Tinker's mind. While Tinker runs unattended for hours or days, the Dashboard lets you:

- Watch the reasoning loop tick in real time (micro / meso / macro)
- See exactly what task is running, what the Architect proposed, and how the Critic scored it
- Monitor the task queue depth and composition
- Track architecture version history
- Watch the anti-stagnation system fight creative ruts
- Stream live logs from every Tinker component
- Drill into any task or output with a full-content detail view

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Tinker Orchestrator                       │
│  (publishes state patches via asyncio.Queue or Redis)        │
└────────────────────────┬────────────────────────────────────┘
                         │  JSON patch dict  ~1 Hz
                         ▼
              ┌──────────────────────┐
              │   BaseSubscriber     │  QueueSubscriber  (in-process)
              │   (async loop)       │  RedisSubscriber  (cross-process)
              └──────────┬───────────┘
                         │  _deserialise_patch()
                         ▼
              ┌──────────────────────┐
              │     StateStore       │  Thread-safe dataclass store
              │  (TinkerState)       │  snapshot() → deep copy
              └──────────┬───────────┘
                         │  Textual timer (1 Hz) + push callback
                         ▼
              ┌──────────────────────┐
              │   TinkerDashboard    │  Textual App
              │   (Textual App)      │  refresh_all_panels()
              └──────────┬───────────┘
           ┌─────────────┼──────────────┐
           ▼             ▼              ▼
      Left column   Right column   Log stream
      ──────────    ────────────   ──────────
      LoopStatus    ActiveTask     LogStreamPanel
      TaskQueue     Architect        (polls LogBuffer
      Health        Critic            @ 250ms)
                    ArchState
                    Memory
```

---

## File Structure

```
tinker/
├── __init__.py
├── pyproject.toml
└── dashboard/
    ├── __init__.py              Public API surface
    ├── __main__.py              Entry point (python -m tinker.dashboard)
    ├── app.py                   TinkerDashboard Textual App
    ├── state.py                 TinkerState dataclasses + StateStore
    ├── subscriber.py            QueueSubscriber + RedisSubscriber
    ├── log_handler.py           Loguru sink + LogBuffer + stdlib bridge
    ├── detail_view.py           Full-screen modal detail views
    ├── mock_orchestrator.py     Synthetic state generator for dev/demo
    ├── orchestrator_integration.py  Copy-paste integration guide
    ├── css/
    │   └── dashboard.tcss       Textual CSS layout + theming
    └── panels/
        ├── __init__.py
        ├── loop_status.py       Loop level + micro/meso/macro counters
        ├── active_task.py       Currently running task
        ├── architect_critic.py  Last Architect output + Critic score
        ├── task_queue.py        Queue depth + status/type breakdown
        ├── health_arch.py       Stagnation monitor + model metrics + memory
        └── log_stream.py        Live log tail with level filter
```

---

## Installation

```bash
# From the tinker/ directory:
pip install -e ".[dev]"

# With Redis support:
pip install -e ".[dev,redis]"
```

**Python 3.11+ required.**

Core dependencies:
- `textual >= 0.52` — TUI framework
- `rich >= 13.7` — formatting within panels
- `loguru >= 0.7` — structured logging sink

---

## Running the Dashboard

### Demo mode (built-in mock Orchestrator)

```bash
python -m tinker.dashboard
# or, after pip install:
tinker-dashboard
```

This starts the dashboard with a synthetic Orchestrator pumping realistic fake state — useful for development and UI iteration without needing the full Tinker engine.

### Connected to real Orchestrator (in-process)

In your `main.py` or wherever you launch Tinker:

```python
import asyncio
from tinker.dashboard import TinkerDashboard
from tinker.dashboard.subscriber import QueueSubscriber

async def run():
    dashboard = TinkerDashboard(subscriber=QueueSubscriber())
    orchestrator = MyOrchestrator()
    await asyncio.gather(
        dashboard.run_async(),
        orchestrator.run(),
    )

asyncio.run(run())
```

And in your Orchestrator, at the end of each loop tick:

```python
from tinker.dashboard.subscriber import publish_state

publish_state({
    "connected":   True,
    "loop_level":  "micro",
    "micro_count": self.micro_count,
    # ... (see orchestrator_integration.py for full schema)
})
```

### Connected to Orchestrator via Redis (separate processes)

```bash
# Terminal 1: Start your Orchestrator (it publishes to Redis)
python -m tinker.orchestrator

# Terminal 2: Start the Dashboard
python -m tinker.dashboard --redis redis://localhost:6379
```

### CLI options

```
python -m tinker.dashboard [OPTIONS]

  --mock              Use built-in mock Orchestrator (default when no --redis)
  --redis <URL>       Connect to Redis pub/sub  (e.g. redis://localhost:6379)
  --refresh <float>   UI poll interval in seconds  (default: 1.0)
  --log-level <str>   Stdlib log bridge level  (default: DEBUG)
```

---

## Keybindings

| Key | Action |
|-----|--------|
| `q` / `ctrl+c` | Quit |
| `d` | Detail view: active task |
| `a` | Detail view: last Architect output |
| `c` | Detail view: last Critic score |
| `s` | Detail view: current architecture state |
| `l` | Cycle log level filter (DEBUG → INFO → WARNING → ERROR) |
| `x` | Clear log panel |
| `r` | Force UI refresh |
| `f1` | Help overlay |
| `Esc` | Close modal / detail view |

---

## Panel Reference

### Status Bar (top)
Connection indicator (● LIVE / ○ DISCONNECTED), current loop level badge, running counters for all three loop tiers, UTC clock.

### Loop Status Panel
Current loop level with colour coding (cyan=micro, yellow=meso, magenta=macro) and exact iteration counts for each tier.

### Active Task Panel
Live task: ID, type, subsystem, status, elapsed time, description. Updates on each state tick.

### Architect Output Panel
Summary of the last Architect agent response, with timestamp and originating task ID. Press `a` for full content.

### Critic Score Panel
Score (0–10) with colour-coded progress bar, top objection string. Press `c` for full critique.

### Task Queue Panel
Total queue depth, breakdown by status (pending/active/complete/failed) and by task type with mini bar charts. Recent task history list.

### Architecture State Panel
Current version string, last commit timestamp, one-line summary. Press `s` for the full specification.

### Health Panel
Three sections: stagnation score + monitor status + recent events; model call latency (avg + p99) + error rate + total calls; memory stats.

### Memory Stats Panel
Session artifact count, research archive size, working memory token count.

### Live Log Stream (bottom, full width)
Tail of the last 500 log lines. Polls the LogBuffer every 250 ms. Colour-coded by level. Filter with `l`, clear with `x`.

---

## Integrating Loguru

Add the dashboard sink once, anywhere in your application startup:

```python
from loguru import logger
from tinker.dashboard.log_handler import loguru_sink

logger.add(
    loguru_sink,
    format="{time}|{level}|{name}:{function}:{line}|{message}",
    colorize=False,
    level="DEBUG",
)
```

All subsequent `logger.*()` calls across every Tinker component will appear in the live log panel automatically.

For libraries using stdlib `logging`:

```python
from tinker.dashboard.log_handler import install_stdlib_bridge
install_stdlib_bridge()
```

---

## Disconnection Handling

The Dashboard is designed to survive Orchestrator failures:

- **In-process (Queue)**: No crash. If no update arrives for `timeout` seconds (default 5s), the store retains the last known state. The status bar continues to show "● LIVE" until an explicit `{"connected": False}` patch arrives or the subscriber is cancelled.

- **Redis**: Any connection failure triggers `mark_disconnected()` → status bar shows "○ DISCONNECTED" → subscriber enters a retry loop with `REDIS_RECONNECT_DELAY` (default 3s) backoff. Dashboard keeps running and displaying stale state.

- **In both cases**: The Textual app never crashes due to Orchestrator failure. Panels display the last received data. When the Orchestrator recovers, the dashboard resumes live updates automatically.

---

## Detail Views

Press the appropriate key to open a full-screen modal showing the raw content of any major state object:

- `d` — full task spec, result summary, complete agent output
- `a` — full Architect agent response (markdown rendered)
- `c` — full Critic reasoning and all objections
- `s` — complete architecture specification document

Navigate with scroll, close with `Esc` or `q`.

---

## Extending the Dashboard

### Adding a new panel

1. Create `tinker/dashboard/panels/my_panel.py` with a class that extends `Widget`
2. Implement `compose()` with `Static` children
3. Implement `refresh_state(state: TinkerState)` — update the statics
4. Export from `panels/__init__.py`
5. Add to `TinkerDashboard.compose()` in `app.py`
6. Add a `_refresh_all_panels()` call

### Adding a new state field

1. Add the field to the appropriate dataclass in `state.py`
2. Add deserialisation in `subscriber._deserialise_patch()`
3. Add serialisation in the Orchestrator's `publish_state()` call
4. Reference the field in the relevant panel's `refresh_state()`

### Switching subscriber backends

```python
from tinker.dashboard import TinkerDashboard
from tinker.dashboard.subscriber import RedisSubscriber

app = TinkerDashboard(
    subscriber=RedisSubscriber(
        redis_url="redis://my-redis:6379",
        channel="tinker:state",        # custom channel name
    ),
    refresh_interval=0.5,              # faster refresh
)
app.run()
```

---

## Development Tips

Use Textual's built-in devtools for live CSS editing:

```bash
pip install textual-dev
textual run --dev -c "python -m tinker.dashboard"
```

This opens the Textual inspector alongside the dashboard — you can tweak `css/dashboard.tcss` and see changes without restarting.

Run tests:

```bash
pytest tinker/dashboard/tests/
```

---

## Component Interfaces Summary

| Direction | From | To | Mechanism |
|-----------|------|----|-----------|
| State push | Orchestrator | StateStore | `publish_state(patch)` → asyncio.Queue or Redis pub/sub |
| State read | StateStore | Panels | `get_store().snapshot()` on Textual timer |
| Log push | All components | LogBuffer | `loguru_sink` or `StdlibBridgeHandler` |
| Log read | LogBuffer | LogStreamPanel | `buf.since(cursor)` polled every 250ms |
| Detail open | User keypress | DetailScreen | `app.push_screen(detail_for_*(obj))` |
