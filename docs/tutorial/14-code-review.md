# Chapter 14 — Code Review: Real Bugs Found and Fixed

## Why This Chapter Exists

Building a system from scratch is one skill.  *Reviewing* and *improving* an
existing system is a different — and equally important — skill.

In this chapter we walk through the real bugs that were found in Tinker's
web UIs during a systematic code review.  We show:

1. What the bug was
2. Why it was a problem
3. How to spot it
4. How to fix it

This is not a theoretical exercise.  These bugs existed in the code.  Some
were critical (the dashboard showed wrong data), some were minor (a missing
CSS class).  All were fixed.

---

## The Review Process

A good code review covers these areas:

| Area | What to look for |
|------|-----------------|
| **Correctness** | Does the code do what it claims? Do field names match? |
| **Data flow** | Does data from component A reach component B in the right shape? |
| **Error handling** | What happens when a network call fails? When a file is missing? |
| **Security** | Input validation, injection, authentication |
| **Platform compatibility** | Does it run on Windows, macOS, and Linux? |
| **Resource management** | Are connections closed? Are files flushed? |

We do not try to find every possible bug.  We focus on the areas most likely
to cause real problems in production.

---

## Bug 1 — Critical: Health Endpoint Shape Mismatch

### What the bug was

The orchestrator writes `tinker_state.json` in this shape:

```json
{
  "totals": {
    "micro": 42,
    "meso": 3,
    "macro": 0
  },
  "current_task_id": "task-abc",
  "micro_history": [
    {"critic_score": 0.82, "subsystem": "api_gateway"}
  ]
}
```

The React dashboard expected the health endpoint to return:

```json
{
  "loops": {
    "micro": 42,
    "meso": 3,
    "macro": 0
  },
  "current_task_id": "task-abc"
}
```

The old code in `webui/core.py` just spread the state file directly:

```python
# OLD (broken)
async def fetch_health() -> dict:
    state = load_state()
    if state:
        return {"online": False, **state}   # ← spreads "totals" not "loops"
    return {"online": False, "status": "unknown"}
```

The browser received `health.totals.micro` instead of the expected
`health.loops.micro`.  Every counter in the dashboard showed zero.

### How to spot it

Read the code that *produces* the data (`OrchestratorState.to_dict()`) and
the code that *consumes* it (the React dashboard's JavaScript).  Compare the
field names.  They were completely different.

### The fix

Add a transformer that reshapes the state file into the API response shape:

```python
# webui/core.py

def _state_to_health(state: dict) -> dict:
    """Reshape state file format → health API response shape."""
    totals     = state.get("totals", {})
    micro_hist = state.get("micro_history", [])
    last_critic = micro_hist[-1].get("critic_score") if micro_hist else None
    return {
        "online":          False,
        "from_state_file": True,
        "status":          state.get("status", "unknown"),
        "uptime_seconds":  state.get("uptime_seconds"),
        "loops": {
            "micro":                totals.get("micro", 0),
            "meso":                 totals.get("meso",  0),
            "macro":                totals.get("macro", 0),
            "consecutive_failures": totals.get("consecutive_failures", 0),
            "current_level":        state.get("current_level", "idle"),
        },
        "current_task_id": state.get("current_task_id"),
        "dlq":             {"pending": 0, "resolved": 0},
        "circuit_breakers": {},
        "memory":           {},
        "rate_limiters":    {},
        "sla":              {},
    }


async def fetch_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{HEALTH_URL}/health")
            if r.status_code == 200:
                return {"online": True, **r.json()}
    except Exception:
        pass

    state = load_state()
    if state:
        return _state_to_health(state)   # ← use transformer, not raw spread

    return {"online": False, "status": "unknown"}
```

### Lesson

**When two components share a data structure, document the schema explicitly
and compare both ends.**  The producer and consumer evolved independently, so
their field names drifted apart.  A transformer that maps one to the other is
a clean fix — it also documents the mapping in one place.

---

## Bug 2 — Critical: SSE Stream Used Wrong Field Names

### What the bug was

The SSE endpoint (Server-Sent Events) streamed live state updates to the
browser.  It read from the state file but used field names that don't exist:

```python
# OLD (broken)
state = load_state()
evt = json.dumps({
    "micro_loops":  state.get("micro_loops"),   # ← doesn't exist
    "current_task": state.get("current_task"),  # ← doesn't exist
    "critic_score": state.get("last_critic_score"),  # ← doesn't exist
})
```

The correct keys are in nested structures:

```python
# CORRECT
totals     = state.get("totals", {})
micro_hist = state.get("micro_history", [])
evt = json.dumps({
    "micro_loops":  totals.get("micro"),          # ← correct
    "current_task": state.get("current_task_id"), # ← correct
    "critic_score": micro_hist[-1].get("critic_score") if micro_hist else None,
})
```

The browser received `null` for every field, so the live log panel showed
nothing updating.

### How to spot it

Print `load_state()` to a Python console and look at the top-level keys.
Or read `OrchestratorState.to_dict()` and list every key it produces.  Then
compare those keys against every `state.get(...)` call in the SSE handler.

### The fix

Use the correct nested paths (shown above) and add a check: only emit an
SSE event when the micro count actually changes.

```python
async def gen() -> AsyncIterator[str]:
    last_micro = -1
    while True:
        if await request.is_disconnected():
            break
        state  = load_state()
        totals = state.get("totals", {})
        micro  = totals.get("micro", -1)

        if micro != last_micro:          # only emit when something changed
            last_micro = micro
            micro_hist = state.get("micro_history", [])
            critic     = micro_hist[-1].get("critic_score") if micro_hist else None
            yield "data: " + json.dumps({
                "micro_loops":  micro,
                "current_task": state.get("current_task_id"),
                "critic_score": critic,
            }) + "\n\n"

        await asyncio.sleep(2)
```

### Lesson

**Test your integration points with real data.**  The SSE handler was never
tested with a real state file.  A five-second manual test (run the orchestrator,
open the SSE endpoint in a browser, check the JavaScript console) would have
revealed this immediately.

---

## Bug 3 — Critical: Backup Trigger Did Nothing

### What the bug was

The "Trigger Backup" button in the web UI sent a POST to `/api/backups/trigger`.
The old handler wrote a flag file to disk:

```python
# OLD (broken)
@app.post("/api/backups/trigger")
async def api_backups_trigger():
    flag_file = BASE_DIR / "tinker_backup_requested.flag"
    flag_file.write_text("1")
    return {"ok": True, "message": "Backup requested."}
```

The `BackupManager` class never reads flag files.  There is no file watcher.
The flag file sat on disk doing nothing.  Clicking "Trigger Backup" appeared
to succeed but no backup was ever created.

### How to spot it

Search the codebase for `tinker_backup_requested.flag` or any code that reads
flag files.  Find nothing.  Then read `BackupManager` to understand how backups
are actually triggered — it turns out there is a `python -m backup --backup`
CLI command.

### The fix

Run the backup CLI as a subprocess:

```python
@app.post("/api/backups/trigger")
async def api_backups_trigger():
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "backup", "--backup",
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=120
        )
        if proc.returncode == 0:
            msg = stdout.decode().strip() or "Backup created successfully."
            return {"ok": True, "message": msg}
        else:
            err = stderr.decode().strip() or "Backup failed."
            return JSONResponse({"ok": False, "error": err}, status_code=500)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "Backup timed out."}, status_code=504)
```

### Lesson

**Before implementing a trigger, understand how the target system actually
works.**  The flag-file approach is a reasonable pattern if the consuming code
has a file watcher.  But it was implemented without checking whether
`BackupManager` had a file watcher.  Read the code before wiring it.

---

## Bug 4 — Medium: Missing Config Validation

### What the bug was

The `/api/config` POST handler validated that values were above a minimum:

```python
# OLD (incomplete)
val = float(raw)
if val > meta["max"]:
    errors.append(f"{meta['label']} must be <= {meta['max']}")
to_save[field_name] = val   # ← saved even if below min!
```

The `if val < meta["min"]` check was present in a comment in the schema but
never implemented in the handler.  You could save `micro_loops_per_meso = -100`
and no error would be raised.

### The fix

```python
val = float(raw)
if val < meta["min"]:
    errors.append(f"{meta['label']} must be >= {meta['min']}")
elif val > meta["max"]:
    errors.append(f"{meta['label']} must be <= {meta['max']}")
else:
    to_save[field_name] = val
```

### Lesson

**Test all validation branches.**  It's easy to implement the `max` check
and forget the `min` check because negative values seem impossible.  But
defensive programming means you check both bounds always.

---

## Bug 5 — Medium: API Calls Had No Error Handling

### What the bug was

The React dashboard's `api` helper fetched JSON from the FastAPI backend:

```javascript
// OLD (no error handling)
const api = {
  get:  (url) => fetch(url).then(r => r.json()),
  post: (url, body) => fetch(url, {method:"POST", ...}).then(r => r.json()),
};
```

If the server returned HTTP 422 (validation error) or 500 (server error),
`r.json()` would still succeed (the error response is valid JSON), but the
response would have `{ "ok": false, "errors": [...] }` instead of the
expected data.  The dashboard would silently fail or crash with
`TypeError: Cannot read properties of undefined`.

Worse: if the server was offline, `fetch(url)` would throw a network error.
This exception would bubble up to the top level of the JavaScript, crashing
the entire dashboard.

### The fix

Check `r.ok` and add `.catch()` for network errors:

```javascript
const api = {
  get: (url) =>
    fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .catch(err => {
        console.warn(`GET ${url}:`, err.message);
        return {};   // return empty object so callers don't crash
      }),

  post: (url, body) =>
    fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .catch(err => {
        console.warn(`POST ${url}:`, err.message);
        return { ok: false, error: err.message };
      }),
};
```

### Lesson

**Every network call can fail.  Always handle the failure case.**  The
pattern is:
1. Check the HTTP status code (`.ok`)
2. Catch network errors (`.catch`)
3. Return a safe default so callers don't crash

---

## Bug 6 — Platform: Windows Signal Handlers

### What the bug was

The orchestrator installed signal handlers for graceful shutdown:

```python
# OLD (crashed on Windows)
loop = asyncio.get_event_loop()
loop.add_signal_handler(signal.SIGINT, self.request_shutdown)
```

On Windows, `asyncio.ProactorEventLoop` (the default on Windows) does not
support `add_signal_handler()`.  It raises `NotImplementedError`.

### The fix

Use `signal.signal()` on Windows instead:

```python
import sys, signal

try:
    loop.add_signal_handler(signal.SIGINT, self.request_shutdown)
    loop.add_signal_handler(signal.SIGTERM, self.request_shutdown)
    logger.debug("Signal handlers installed via event loop")
except (NotImplementedError, RuntimeError):
    # Windows: ProactorEventLoop doesn't support add_signal_handler
    if sys.platform == "win32":
        try:
            signal.signal(
                signal.SIGINT,
                lambda _sig, _frame: self.request_shutdown()
            )
            logger.debug("Windows SIGINT handler installed via signal.signal()")
        except Exception as exc:
            logger.warning("Could not install signal handler: %s", exc)
    else:
        logger.warning("Signal handlers not supported in this environment")
```

### Lesson

**Test on every target platform.**  Signal handling is one of the most
common cross-platform issues in Python.  If you can't test on Windows,
at minimum check the Python docs for which `asyncio` APIs are unsupported
on Windows (`add_signal_handler`, `add_reader`, `add_writer`).

---

## Bug 7 — Platform: `/tmp` Path in `.env.example`

### What the bug was

The `.env.example` file contained:

```bash
TINKER_STATE_PATH=/tmp/tinker_orchestrator_state.json
```

On Windows, `/tmp` does not exist.  The orchestrator would write the state
file to `/tmp/...` on Linux/macOS but fail silently on Windows (the file
would be written to the current working directory instead, at a path like
`C:\tinker\tmp\tinker_orchestrator_state.json`).

### The fix

Use a relative path that works on all platforms:

```bash
TINKER_STATE_PATH=./tinker_state.json
```

### Lesson

**Never use `/tmp` in configuration that ships to users.**  Use relative
paths or `os.path.join(os.path.expanduser("~"), ".tinker", "state.json")`
for user-specific paths.

---

## Bug 8 — Platform: Port Conflict (8080 × 2)

### What the bug was

Both SearXNG and the Tinker health server defaulted to port 8080.  If you
started both (the recommended setup), one would fail to bind and you'd get
a confusing "Address already in use" error.

- `docker-compose.yml`: `"8080:8080"` (SearXNG)
- `.env.example`: `TINKER_HEALTH_PORT=8080` (health server)

### The fix

Move SearXNG to host port 8888:

```yaml
# docker-compose.yml
searxng:
  ports:
    - "8888:8080"   # host 8888 → container 8080
```

```bash
# .env.example
TINKER_HEALTH_PORT=8081
TINKER_SEARXNG_URL=http://localhost:8888
```

### Lesson

**Document every port in one place and check for conflicts before shipping.**
A port reference table in `SETUP.md` makes conflicts obvious:

| Service | Host port |
|---------|-----------|
| Ollama | 11434 |
| Redis | 6379 |
| SearXNG | 8888 |
| Health server | 8081 |
| Web UI | 8082 |
| Gradio UI | 8083 |
| Streamlit UI | 8501 |

---

## How to Approach a Code Review

Here is a repeatable checklist for reviewing a system like Tinker:

### 1. Map the data flows

Draw (on paper or a whiteboard) every place where data moves from component A
to component B.  For each flow, ask:
- What is the expected shape of the data at the source?
- What is the expected shape at the destination?
- Do they match?

### 2. Trace every API endpoint

For each API endpoint:
- What does it return?
- What does the client expect?
- What happens if the server is offline?
- What happens if the response is malformed?

### 3. Check every TODO/fallback

Search the code for `TODO`, `FIXME`, `pass`, `return {}`, `return None`.
Each one is a place where the code may not be doing what you think.

### 4. Read the config and env vars

For every env var and config file:
- What is the default?
- Is the default safe on all platforms?
- Is the default correct for the standard setup?

### 5. Test on the target platforms

If you can, run the code on Windows, macOS, and Linux.  If you can't, check
the Python docs for platform-specific limitations of every API you use.

---

## Summary

| Bug | Severity | Root Cause |
|-----|----------|------------|
| Health endpoint shape mismatch | Critical | Two components evolved separately; field names drifted |
| SSE stream wrong keys | Critical | Not tested against real state file |
| Backup trigger did nothing | Critical | Assumed a file watcher that doesn't exist |
| Missing `min` validation | Medium | Incomplete implementation |
| No API error handling | Medium | Network failures not considered |
| Windows `add_signal_handler` crash | High | Not tested on Windows |
| `/tmp` path in `.env.example` | Low | Linux assumption in cross-platform config |
| Port 8080 conflict | Medium | No port audit during setup |

Every one of these bugs was fixable in under 30 minutes once it was found.
Finding them is the hard part.  That's why systematic code review — checking
correctness, data flows, error handling, and platform compatibility — is a
skill worth practising.

---

## What You Have Built

Congratulations.  If you have followed this tutorial from the beginning,
you have built a complete AI reasoning system:

```
tinker/
  llm/         ✅  Ollama model client + role-based router
  memory/       ✅  Redis + DuckDB + ChromaDB + SQLite unified manager
  tools/        ✅  Web search + scraper + artifact writer
  prompts/      ✅  Architect + Critic + Synthesizer with structured output
  tasks/        ✅  SQLite task registry + priority scoring + generator
  context/      ✅  Token-budgeted context assembly
  orchestrator/ ✅  Three-loop design (micro / meso / macro)
  resilience/   ✅  Circuit breakers + rate limiter + DLQ + distributed lock
  stagnation/   ✅  Five detectors + directive-based intervention
  observability/✅  Append-only audit log + percentile SLA tracking
  webui/        ✅  FastAPI + React dashboard + Gradio + Streamlit
  main.py       ✅  Dependency-injected entry point
```

The system runs 24/7, improves its own designs through critique, detects when
it's stuck, and recovers gracefully from failures.  The web UI lets you watch
it think, inject new tasks, and review what went wrong.

The most important thing you learned is not any specific technology — it's
the **pattern**:

> Build small, independent components.  Wire them together in one place.
> Observe everything.  Expect failure and recover gracefully.

These principles apply to every system you will ever build.

---

*End of Tutorial*

← Back to [Chapter 13 — Integration](./13-integration.md)
← Back to [Tutorial Index](./README.md)
