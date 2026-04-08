# Adversarial Production Readiness Audit

**Date**: 2026-04-08
**Auditor**: Claude Opus 4.6 (adversarial role)
**Scope**: Full codebase — 336 Python files, 250 source files, 1797 functions, 482 classes
**Branch**: `claude/audit-production-readiness-Y34sO`

> This audit is intentionally adversarial. It focuses exclusively on what is
> broken, stubbed, insecure, inefficient, or not enterprise-grade. Nothing
> positive is mentioned. The goal is honest, constructive criticism that you
> can use to improve the project.

---

## Table of Contents

1. [Executive Summary (The Brutal Truth)](#1-executive-summary)
2. [The Test Suite is Broken](#2-the-test-suite-is-broken)
3. [Security: The Web UI is Wide Open](#3-security-the-web-ui-is-wide-open)
4. [Logic Bugs That Silently Corrupt Behavior](#4-logic-bugs)
5. [Error Handling: The Exception Swallowing Epidemic](#5-error-handling)
6. [Race Conditions and Concurrency Defects](#6-race-conditions)
7. [Stubs, Incomplete Implementations, and Dead Weight](#7-stubs-and-incomplete)
8. [Architectural and Design Flaws](#8-architectural-flaws)
9. [Cryptography and Secrets Management](#9-cryptography)
10. [Observability and Reliability Gaps](#10-observability)
11. [Configuration System Dysfunction](#11-configuration)
12. [Performance and Efficiency Issues](#12-performance)
13. [Dependency and Packaging Issues](#13-dependencies)
14. [Summary Scoreboard](#14-scoreboard)

---

## 1. Executive Summary

**Verdict: NOT production ready. Not even close.**

Here are the hard numbers from running `pytest` against the repo as-is:

| Metric | Count |
|--------|-------|
| Tests collected | 1,229 |
| **Tests FAILED** | **50** |
| **Collection ERRORS** | **8** |
| **Runtime ERRORS** | **31** |
| Tests passed | 1,066 |
| Tests skipped | 5 |
| `except Exception` handlers in non-test code | **76** |
| `print()` calls in non-test production code | **104** |
| Bare `pass` in exception/method bodies | **40+** |
| `NotImplementedError` in production paths | **7** |

The project has 250 source files but cannot even collect its own test suite
without import errors (`fastapi`, `cryptography`, and others fail to import).
Of the tests that *do* run, **50 fail outright** and **31 error during setup**.

The entire anti-stagnation detection subsystem's tests fail. The lineage
tracker's tests fail. The audit log's tests fail. The dead letter queue's
tests fail. The memory manager's integration tests error out. This is not
"a few flaky tests" — entire subsystems are broken or untestable.

Beyond test failures, the audit found:

- **Zero authentication** on the FastAPI web UI — anyone on the network can
  push code, create PRs, inject tasks, and modify configuration
- **Zero CSRF protection** on any POST endpoint
- **CORS set to `allow_origins=["*"]`** — every origin on the internet is trusted
- **A validation logic bug** that makes quality threshold checking literally
  impossible (`config/validation.py:165`)
- **76 broad `except Exception` handlers** that silently swallow errors,
  making production debugging nearly impossible
- **Race conditions** in the orchestrator state, circuit breaker, rate limiter
  persistence, and architecture state manager
- **Weak cryptography** (PBKDF2 at 100k iterations vs. NIST-recommended 600k+)
  with no key rotation and a silent plaintext fallback on decryption failure
- **The configuration system is bypassed** — `bootstrap/components.py` reads
  `os.getenv()` directly instead of using the `TinkerSettings` it claims to use
- **Path traversal vulnerabilities** in multiple web routes
- **No SSRF protection** in the web scraper — the AI can tell it to fetch
  `http://169.254.169.254/latest/meta-data/` and nobody stops it

If you deployed this to production today, it would crash, leak data, accept
unauthenticated commands, and silently corrupt its own state.

---

## 2. The Test Suite is Broken

### 2.1 Eight test files cannot even be imported

These files crash during `pytest --collect-only`:

| File | Error |
|------|-------|
| `agents/fritz/tests/test_agent.py` | Missing dependency at import |
| `agents/fritz/tests/test_gitea_ops.py` | Missing dependency at import |
| `agents/fritz/tests/test_github_ops.py` | Missing dependency at import |
| `agents/fritz/tests/test_retry.py` | Missing dependency at import |
| `agents/grub/tests/test_minion_base.py` | Missing dependency at import |
| `agents/grub/tests/test_registry.py` | Missing dependency at import |
| `infra/security/tests/test_encryption.py` | `pyo3_runtime.PanicException` (Rust panic!) |
| `ui/web/tests/test_api.py` | `ModuleNotFoundError: No module named 'fastapi'` |

The encryption test causes a **Rust panic** — not a Python exception, an
actual memory-unsafe crash in the cryptography library's compiled extension.
This means either the test triggers undefined behavior or the dependency
version is incompatible. Either way, this is a showstopper.

### 2.2 Entire subsystems fail their own tests

**Anti-stagnation (22 failures):** Every single test in
`runtime/stagnation/test_anti_stagnation.py` fails. All 5 detectors
(SemanticLoop, SubsystemFixation, CritiqueCollapse, ResearchSaturation,
TaskStarvation) plus all integration and edge case tests. This means the
stagnation detection system — a core differentiator of Tinker — is either
broken or its tests are testing the wrong interface.

**Lineage tracker (10 failures):** All tests in
`tinker_platform/lineage/tests/test_lineage_tracker.py` fail. Lineage
tracking is a feature you advertise but cannot demonstrate works.

**Audit log (5 failures):** `infra/observability/tests/test_audit_log.py` —
the audit log cannot log, query, count stats, or flush. Your compliance
story is fiction.

**Dead letter queue (4 failures):** `infra/resilience/tests/test_dead_letter_queue.py`
— enqueue, pending items, stats, and purge all fail.

**Memory manager (14 errors):** All integration tests in
`core/memory/test_memory_manager.py` error during setup. The memory
subsystem cannot be tested at all.

### 2.3 Known bug caught by its own test (and not fixed)

`agents/grub/tests/test_grub_tinker_integration.py::TestFailureModes::test_malformed_metadata_does_not_crash_fetch`

This test explicitly checks that `fetch_implementation_tasks()` handles
malformed metadata gracefully. It fails with:
```
AttributeError: 'sqlite3.Row' object has no attribute 'get'
```
at `agents/grub/feedback.py:115`. The test *knows* this is broken — the
assertion message says "must handle gracefully" — but the bug was never fixed.

### 2.4 Research mode tests completely broken

All 5 tests in `tests/test_research_mode.py` error with `ModuleNotFoundError`.
The research mode feature cannot be verified.

### 2.5 `PytestCollectionWarning` on dataclass naming

`agents/grub/contracts/result.py:56` defines a `TestSummary` dataclass.
Pytest tries to collect it as a test class because of the `Test` prefix.
This is sloppy naming that confuses the test runner.

---

## 3. Security: The Web UI is Wide Open

### 3.1 Zero authentication on all endpoints

The FastAPI web UI (`ui/web/app.py`) has **no authentication whatsoever**.
No API keys, no bearer tokens, no session cookies, no OAuth, no basic auth.
Every endpoint is publicly accessible to anyone who can reach the port:

- `POST /api/fritz/ship` — commits and pushes code to git
- `POST /api/fritz/push` — pushes to any branch
- `POST /api/fritz/pr` — creates pull requests on GitHub/Gitea
- `POST /api/config` — overwrites application configuration
- `POST /api/tasks/inject` — injects arbitrary tasks into the orchestrator
- `POST /api/orchestrator/shutdown` — shuts down the engine
- `GET /api/audit` — reads the entire audit trail
- `GET /api/errors/recent` — reads error details with stack traces

An attacker on the same network (or the internet, if the port is exposed)
can push malicious code, shut down the system, inject poisoned tasks, and
read all operational data.

### 3.2 CORS allows every origin

```python
# ui/web/app.py:111-116
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # Every website on the internet
    allow_methods=["*"],     # Every HTTP method
    allow_headers=["*"],     # Every header
)
```

This means any website a user visits can make cross-origin requests to Tinker's
API. A malicious webpage could silently call `/api/fritz/ship` and push code
to your repository while you browse.

### 3.3 Zero CSRF protection

No CSRF tokens are generated or validated on any POST endpoint. Combined with
the wildcard CORS policy, this is a complete bypass of the same-origin policy.

### 3.4 Path traversal in multiple web routes

- **`ui/web/routes/orchestrator_ctrl.py:35`**: `request_id` is used directly
  in file paths: `response_dir / f"{request_id}.json"`. No sanitization.
  A `request_id` of `../../etc/passwd` would escape the directory.
- **`ui/web/routes/reviews.py:100`**: Same pattern with `review_id`.

### 3.5 No SSRF protection in web scraper

`core/tools/web_scraper.py` fetches arbitrary URLs provided by the AI model.
There is zero validation that the URL is not:
- A cloud metadata endpoint (`169.254.169.254`, `metadata.google.internal`)
- A private network address (`10.x.x.x`, `172.16.x.x`, `192.168.x.x`)
- `localhost` or `127.0.0.1` (port scanning the host)
- A file:// URL

No allowlist, no blocklist, no IP validation. The `Grep` for "SSRF", "allowlist",
"is_private", or "_validate_url" across the entire codebase returns **zero results**.

### 3.6 LLM-controlled file paths in Grub minions

`agents/grub/minions/coder.py` extracts file paths from LLM output and writes
files to those paths. If the LLM returns `../../../etc/crontab`, the file
write could escape the repository directory. No path traversal validation
exists.

### 3.7 Subprocess execution from web routes

`ui/web/routes/fritz.py:144-226` spawns `git` subprocesses with arguments
derived from user input. While list-based `create_subprocess_exec` mitigates
shell injection, the `workspace` path (line 138) is read from a JSON config
file that could be manipulated, and diff output (up to 50,000 chars) is
returned directly to the client without sanitization.

### 3.8 Environment variable token exposure

`agents/fritz/github_ops.py:145` passes `GITHUB_TOKEN` via environment
variable using `__import__("os").environ` inline — an unusual pattern that
could leak tokens through process inspection or `/proc/<pid>/environ`.

---

## 4. Logic Bugs That Silently Corrupt Behavior

### 4.1 Quality threshold validation is impossible (config/validation.py:165)

```python
if 0.0 < grub.quality_threshold > 1.0:
    warnings.append(...)
```

This is a **chained comparison** that evaluates as
`(0.0 < threshold) AND (threshold > 1.0)`. This can only be True for values
greater than 1.0 — it **never catches negative values**:

| Input | Buggy result | Correct result |
|-------|-------------|----------------|
| -0.5  | No warning  | Should warn    |
| 0.5   | No warning  | Correct        |
| 1.5   | Warns       | Correct        |

The correct check is `if not (0.0 <= grub.quality_threshold <= 1.0):`.

### 4.2 A/B testing traffic gate is inverted (tinker_platform/experiments/ab_testing.py:336)

```python
if traffic_frac >= exp.traffic_percentage:
    return _assign_control(...)  # Units ABOVE percentage get control
```

The comment says "10% of units see experiment; 90% get control", but the
logic is backwards: 90% of units see the experiment and 10% get control.
Every A/B test you run has inverted traffic allocation.

### 4.3 Greedy regex in score extraction (agents/_shared.py:264-277)

```python
pattern = r"([0-9]\.[0-9]+)\b"
```

This matches ANY decimal number in the text. If the model responds with
"In version 2024.3, the architecture scored well", it extracts `2024.3` as
the score. The fallback `return 0.7` then silently provides a passing score
even when parsing fails.

### 4.4 Greedy regex in task extraction (agents/_shared.py:247)

```python
r"\{.*\}"  # with re.DOTALL
```

This matches from the FIRST `{` to the LAST `}` in the entire text. If the
model outputs 200 lines with multiple JSON objects, this captures everything
between the first opening brace and last closing brace as one giant invalid
JSON blob.

### 4.5 StagnationEventLog counts are permanently wrong (runtime/stagnation/event_log.py:42)

```python
self._events: deque[StagnationEvent] = deque(maxlen=max_size)
self._type_counts: dict[StagnationType, int] = {t: 0 for t in StagnationType}
```

`_type_counts` is monotonically increasing (incremented on every `append()`),
but `_events` is a bounded deque that evicts old entries. After `max_size`
events, the counts permanently diverge from reality. If 500 SEMANTIC_LOOP
events are logged but only 500 fit in the deque, `counts_by_type()` reports
500 even if older ones were evicted and replaced by other types.

### 4.6 Score normalization is inconsistent across the codebase

Three different normalization strategies exist simultaneously:

| Location | Strategy |
|----------|----------|
| `agents/_shared.py:274` | `val / 10.0 if val > 1.0 else val` (assumes >1 means /10 scale) |
| `agents/critic.py:173` | `max(0.0, min(1.0, score))` (hard clamp) |
| `agents/grub/minions/base.py:372` | `max(0.0, min(1.0, val))` (hard clamp) |

If the model returns 1.1 (slightly above 1.0 on a 0-1 scale), `_shared.py`
divides by 10 to get 0.11, while `critic.py` clamps to 1.0. Same input,
different output depending on which code path runs.

### 4.7 Task starvation detector resets on any positive signal

`runtime/stagnation/detectors.py:354`: The consecutive negative counter
resets to 0 on ANY single positive `net_generation` value. Pattern
`[-1, -1, +1, -1, -1]` never triggers because the counter resets at `+1`.
A single noisy positive is enough to mask real stagnation.

### 4.8 Semantic loop detection has O(n^2) false positive amplification

`runtime/stagnation/detectors.py:83`: Creates all pairwise combinations of
embeddings in the window. A single repeated output gets counted in `n-1`
pairs, amplifying the severity score beyond what the actual repetition
warrants. One duplicated response in a window of 10 creates 9 "breaches".

---

## 5. Error Handling: The Exception Swallowing Epidemic

### 5.1 Scale of the problem

The codebase contains **76 `except Exception` handlers** in production code.
Many of these log at `debug` level or swallow the exception entirely with
`pass`. This makes production debugging effectively impossible — errors
happen, get silently eaten, and the system continues in a corrupted state.

### 5.2 Most dangerous exception handlers

**`runtime/orchestrator/micro_loop.py`** — 14 `except Exception` handlers
in a single file. The core execution loop catches and suppresses errors at
nearly every step: task selection, context assembly, architect call, critic
call, artifact storage, task completion, and task generation. A failure at
any step logs a warning and continues, potentially operating on stale or
corrupt data.

**`runtime/orchestrator/state.py:586`** — `except Exception: pass`. The
orchestrator state serialization silently ignores serialization failures.
Your state snapshots may be incomplete or stale with no indication.

**`runtime/orchestrator/checkpoint.py:248`** — `except Exception: pass`.
Checkpoint writes silently fail. You think you have crash recovery but the
checkpoint may not have been written.

**`main.py:265`** — `except Exception: pass` in the dashboard snapshot
callback. State updates to the dashboard silently fail.

**`agents/_shared.py:62`** — System mode file read failures are silently
ignored with `pass`. The system could be in the wrong mode and nobody would
know.

**`agents/_shared.py:101-114`** — Rate limiter and retry helper lazy
initialization failures are logged at `debug` level and return `None`. The
entire resilience layer silently disables itself.

### 5.3 The pattern is systemic

The codebase follows a consistent anti-pattern:

```python
try:
    important_operation()
except Exception as exc:
    logger.warning("Non-fatal: %s", exc)  # or debug, or pass
```

"Non-fatal" is used 15+ times in the codebase. The philosophy seems to be
"never crash" — but the result is "never know anything is wrong." This is
worse than crashing, because a crash at least tells you something broke.

### 5.4 104 print() calls in production code

The codebase has 104 `print()` calls outside test files. These bypass the
logging system entirely, cannot be filtered by log level, cannot be
structured, and go to stdout where they may be lost. Notable offenders:

- `agents/fritz/__main__.py` — 10 print() calls
- `agents/grub/__main__.py` — 4 print() calls
- `tinker_platform/` — 4 print() calls across features, experiments, capacity
- `infra/observability/tracing.py:53` — The *observability* module uses print()
- `infra/backup/__main__.py` — 6 print() calls

The observability module using `print()` is genuinely comedic.

---

## 6. Race Conditions and Concurrency Defects

### 6.1 Orchestrator state has no locking

`runtime/orchestrator/orchestrator.py:174-195`: The `state` dataclass is
read and written by multiple concurrent paths:

- `_main_loop()` modifies `state.shutdown_requested`, `state.paused`,
  `state.current_level`, `state.consecutive_failures`
- `request_shutdown()` sets `state.shutdown_requested` from outside
- `pause()` / `resume()` toggle `state.paused` from web routes
- JSON snapshot is written after each micro loop without atomicity

No `asyncio.Lock()` protects any of these. Concurrent modifications can
produce inconsistent state — e.g., a shutdown request arrives mid-loop
but is overwritten by the loop's own state update.

### 6.2 ModelRouter.hot_reload() has no locking

`core/llm/router.py:163-232`: The `hot_reload()` method:
1. Closes all existing HTTP client sessions
2. Clears `_clients` dict
3. Creates new clients
4. Calls `warmup()`

If a `complete()` call is in-flight when `hot_reload()` runs, it will find
an empty `_clients` dict at step 2 and raise `ModelRouterError`. There is
no lock coordinating hot_reload with in-flight requests.

### 6.3 Architecture state manager mutates without locking

`infra/architecture/manager.py:247`: `apply_update()` reads `self._state`,
performs a potentially slow merge, and writes back. Two concurrent updates
will cause the second to overwrite the first (lost update problem).

### 6.4 Circuit breaker checks state outside its lock

`infra/resilience/circuit_breaker.py:205-223`: The state check happens
inside an `async with self._lock` block, but the `CircuitBreakerOpenError`
is raised after the lock is released. Between releasing the lock and raising
the error, another coroutine could close the breaker, making the error stale.

### 6.5 Rate limiter persistence uses `time.monotonic()` across restarts

`infra/resilience/rate_limiter.py:286-296`: `load_state()` restores
`_last_refill` from Redis, but this value was stored as `time.monotonic()`.
Monotonic clocks reset on process restart. After restart, the restored
`_last_refill` value could be in the future relative to the new monotonic
clock, causing the refill calculation to produce negative elapsed time and
zero token refill indefinitely.

### 6.6 FritzGitOps identity application race

`agents/fritz/git_ops.py:102-105`: `_identity_applied` flag check-and-set
is not protected by a lock. Two concurrent async calls can both see
`_identity_applied = False`, causing duplicate identity applications.

### 6.7 Feature flag reload race condition

`tinker_platform/features/flags.py:246-258`: `_maybe_reload()` checks the
reload interval and updates `_last_reload` without locking. Multiple threads
can trigger simultaneous reloads, causing duplicate file reads and callback
firings.

### 6.8 IP rate limiter dict in web middleware

`ui/web/app.py:44-45`: The `_ip_limiters` dict is accessed from the
middleware without consistent lock usage. While `_limiter_for_ip()` uses a
lock, the middleware itself could race with cleanup operations.

---

## 7. Stubs, Incomplete Implementations, and Dead Weight

### 7.1 Enterprise stack components initialized as None

`bootstrap/enterprise_stack.py:200-201` returns:
```python
"auto_recovery": None,   # "wired later"
"health_server": None,   # "wired later"
```

Every consumer must null-check these. There's no `NullAutoRecovery` or
`NullHealthServer` to provide safe no-op behavior. The "wired later" comment
is a runtime landmine — if wiring fails silently, these stay None forever.

### 7.2 Entire stagnation subsystem is broken

All 5 stagnation detectors fail their tests. Either the implementation
doesn't match the test expectations, or the interface changed without
updating tests. Either way, the stagnation detection — described as a key
feature — is non-functional dead code.

### 7.3 ContextAssembler abstract methods raise NotImplementedError

`core/context/assembler.py:200-222` has 7 methods that `raise NotImplementedError`.
These are described as "intentional" to force subclass implementation, but
the comment on line 189 says "raises NotImplementedError intentionally, so
that subclasses which forget to override will get a clear error." This is a
base class with no concrete implementation — the entire context assembly
pipeline depends on subclasses that may or may not exist.

### 7.4 Multiple web route handlers are just `pass`

| File | Line | Handler |
|------|------|---------|
| `ui/web/routes/fritz.py` | 140 | Config load error |
| `ui/web/routes/reviews.py` | 174, 208, 229 | Review operations |
| `ui/web/routes/streaming.py` | 114 | SSE error |
| `ui/web/routes/orchestrator_ctrl.py` | 114 | Control file error |

These are exception handlers that do literally nothing — errors vanish.

### 7.5 Grub feedback module has a known unfixed bug

`agents/grub/feedback.py:115` calls `.get()` on a `sqlite3.Row` object,
which doesn't support `.get()`. The test for this exists and explicitly
documents the failure in its assertion message: "must handle gracefully."
The bug was found, tested for, documented, and... left unfixed.

### 7.6 Research mode feature is untestable

All 5 tests in `tests/test_research_mode.py` error with `ModuleNotFoundError`.
The feature exists in code but cannot be verified to work.

### 7.7 Services registry provides no failure feedback

`services/registry.py:130-136`: `start_all()` uses `return_exceptions=True`
but doesn't report which services failed. The caller has no way to know if
startup was partial. Same issue with `stop_all()`.

### 7.8 Lineage tracker silently disables itself

`tinker_platform/lineage/tracker.py:178-179`: Methods check `if not self._conn: return`
without logging. If the database connection fails at startup, every lineage
operation silently returns None/empty for the entire session. You think
lineage is tracking but it's doing nothing.

---

## 8. Architectural and Design Flaws

### 8.1 The configuration system is bypassed by its own bootstrap

`config/settings.py` defines a comprehensive `TinkerSettings` with 13 nested
dataclass sections covering ~110 environment variables. It has a singleton
`get_settings()` accessor. It's well-designed.

**It's also almost entirely unused.**

`bootstrap/components.py` — the file that actually builds all components —
calls `os.getenv()` directly **24 times** instead of using `TinkerSettings`.
`main.py` calls `os.getenv()` directly **11 more times**. The entire
configuration system with its validation, type conversion, and defaults
exists in parallel with raw `os.getenv()` calls that duplicate the same
defaults (sometimes differently).

This means:
- Changing a default in `settings.py` doesn't affect what `components.py` uses
- Validation in `validation.py` runs on settings that aren't the ones in use
- There are two sources of truth for every configuration value

### 8.2 No dependency injection — global singleton pattern everywhere

The codebase uses a "build everything in one function and pass via dict"
pattern. `build_real_components()` returns a `dict` with string keys.
Consumers access components via `components["task_engine"]` — no type safety,
no IDE completion, no compile-time verification.

The `enterprise` dict is even worse: it's attached to the orchestrator via
`setattr` (`orchestrator.enterprise = enterprise`) and accessed via
`getattr(orch, "enterprise", {})` — completely invisible to the type system.

### 8.3 Global mutable singletons

- `config/settings.py:406-413`: `_settings` global singleton with no thread safety
- `agents/_shared.py:120-137`: `_rate_limiter_registry` global with a broken
  initialization flag (set BEFORE init, preventing retry on failure)
- `agents/fritz/metrics.py:254`: `_default_metrics` global
- `core/context/prompt_builder_adapter.py:66-79`: Two globals for template registry

### 8.4 Task queue has no dependency cycle detection

`runtime/tasks/resolver.py:300-370`: The resolver uses topological sort for
dependencies but performs no cycle detection. If task A depends on B and B
depends on A, both are marked BLOCKED permanently. The system deadlocks with
no diagnostic.

### 8.5 Micro loop history is unbounded

`runtime/orchestrator/state.py`: `micro_history` is a plain list that grows
forever. Comments mention "caps at 100" but no `maxlen` enforcement exists.
Over days of continuous operation, this leaks memory and makes state
snapshots increasingly large.

### 8.6 Backup verification is optional before restore

`infra/backup/backup_manager.py`: The API allows `restore(backup_id)` without
requiring `verify(backup_id)` first. A corrupted backup can be restored
without warning, propagating data corruption.

### 8.7 No graceful degradation strategy

When services fail (Redis down, Ollama timeout, SearXNG unavailable), the
circuit breaker opens, but there's no automatic fallback mode. The system
either works fully or fails. No "skip research if SearXNG is down" or
"use cached context if Redis is unavailable" degradation paths exist.

### 8.8 Mixed threading models

The codebase mixes `asyncio.Lock`, `threading.RLock`, `threading.Lock`, and
no locking depending on the module:

| Module | Locking |
|--------|---------|
| `rate_limiter.py` | `asyncio.Lock` |
| `event_log.py` | `threading.RLock` |
| `postgres_registry.py` | `threading.Lock` |
| `orchestrator/state.py` | None |
| `architecture/manager.py` | None |

In an async application, threading locks can cause deadlocks when held across
`await` points. The inconsistency makes it impossible to reason about thread
safety.

---

## 9. Cryptography and Secrets Management

### 9.1 PBKDF2 iteration count is below NIST minimum

`infra/security/encryption.py:59`: Uses 100,000 PBKDF2-HMAC-SHA256 iterations.
NIST SP 800-132 and OWASP recommend **600,000+ iterations** as of 2023.
At 100k iterations, a consumer GPU can brute-force weak master keys in hours.

### 9.2 Decryption silently falls back to plaintext

`infra/security/encryption.py:154-160`:
```python
except Exception:
    return payload  # Plaintext passthrough
```

If decryption fails for ANY reason — wrong key, corrupted data, tampered
ciphertext, authentication tag mismatch — the function silently returns the
raw payload as if it were plaintext. This means:

- A tampered artifact passes authentication checks silently
- A key rotation mistake returns garbled base64 as "content"
- You can never distinguish "decryption failed" from "data was never encrypted"

GCM authentication tags exist specifically to prevent this. Ignoring
authentication failure defeats the entire purpose of authenticated encryption.

### 9.3 No key rotation mechanism

`ArtifactEncryptor` takes one master key at initialization. There is no:
- Key versioning in the encrypted payload (the `"v": 1` is a format version,
  not a key version)
- Support for multiple keys during rotation periods
- Re-encryption utility to migrate artifacts to a new key
- Key revocation capability

If the master key is compromised, every artifact ever encrypted is permanently
exposed with no path to recovery.

### 9.4 Encryption disabled by default

`infra/security/encryption.py:77-83`: If `TINKER_ARTIFACT_KEY` is not set,
encryption is silently disabled. All artifacts are stored in plaintext. There
is no warning at startup that encryption is off. The `NullEncryptor` class
exists purely to make "no encryption" the easy default.

### 9.5 Secrets manager has no rotation or audit trail

`infra/security/secrets.py` provides `get_secret()` but has no:
- Automatic secret rotation
- Access audit logging (who accessed what secret, when)
- TTL enforcement
- Revocation capability

---

## 10. Observability and Reliability Gaps

### 10.1 Audit log buffers events in memory and can lose them

`infra/observability/audit_log.py:236`: Events are buffered in memory and
flushed every 5 seconds or when the buffer reaches max size. If the process
crashes between buffer append and flush, all buffered events are lost. For
an *audit log* — whose entire purpose is to provide a reliable record — this
is unacceptable. Critical events like circuit breaker trips, security
violations, or stagnation detections can disappear.

### 10.2 Trace context is lost on exception

`runtime/orchestrator/micro_loop.py:577-592`:
```python
finally:
    if _trace_ctx is not None:
        with contextlib.suppress(Exception):
            _trace_ctx.__exit__(None, None, None)
```

The `__exit__` is called with `(None, None, None)` instead of
`(*sys.exc_info())`. The tracing backend records a "successful" span even
when the loop actually failed. Your traces will show green when the system
is on fire.

### 10.3 Health check returns inconsistent snapshots

`infra/health/http_server.py:220-250`: The `/ready` endpoint iterates over
live circuit breaker stats without taking a snapshot first. The dict can
change during iteration, causing the health check to report an inconsistent
view to the load balancer.

### 10.4 SLA tracker has no window boundary handling

SLA metrics that straddle time window boundaries (e.g., an event that starts
in one reporting period and ends in another) have no explicit handling.
Fence-post errors in window calculations can cause SLA reports to be off
by one event or one time window.

### 10.5 Structured logging has 104 bypasses

The 104 `print()` calls in production code completely bypass the logging
system. They cannot be:
- Filtered by log level
- Structured as JSON
- Correlated with trace IDs
- Captured by log aggregators (Loki, Datadog, etc.)
- Suppressed in production

---

## 11. Configuration System Dysfunction

### 11.1 Two parallel configuration systems

As detailed in section 8.1, `TinkerSettings` exists alongside 35+ raw
`os.getenv()` calls in `bootstrap/components.py` and `main.py`. The settings
system is not the source of truth for the components it claims to configure.

### 11.2 Float conversion has no NaN/Inf protection

`config/settings.py` uses `float(_env(...))` for several values (lines 156,
183, 274-275, 310, 317-319). Python's `float()` happily converts `"inf"`,
`"-inf"`, and `"nan"` to special float values. Setting
`TINKER_WEBUI_RATE_PER_SEC=inf` would create a rate limiter that never
limits. No special-value checks exist.

### 11.3 Validation warnings are non-blocking

`config/validation.py:241-254`: `validate_or_warn()` logs warnings but
**never raises an exception**. Invalid configuration (wrong port ranges,
missing required URLs, conflicting ports) produces log messages but the
system starts anyway with broken config. "Validation" that doesn't prevent
invalid state is not validation — it's a suggestion.

### 11.4 Port conflict detection is incomplete

`config/validation.py:222-236`: Port conflict detection checks 7 ports but
misses the MCP server port, and doesn't consider the health check port
(`8080`) conflicting with the SearXNG default (`8080`). The default
configuration has a port conflict out of the box.

### 11.5 Sensitive config values stored in frozen dataclasses

`config/settings.py:234-249`: `SecuritySettings` stores `artifact_key`,
`vault_token`, and `secrets_file` as plain string fields in a frozen
dataclass. These values are visible in stack traces, debug output, and
any serialization of the settings object. No masking or redaction.

---

## 12. Performance and Efficiency Issues

### 12.1 Embedding similarity is O(n^2)

`runtime/stagnation/detectors.py:83`: The semantic loop detector computes
all pairwise combinations of embeddings in the sliding window:
`itertools.combinations(self._window, 2)`. For a window of size 20, this
is 190 pairs. Each pair requires a dot product over the embedding dimension.
This runs on every micro loop iteration.

### 12.2 OllamaEmbeddingBackend is synchronous

`runtime/stagnation/embeddings.py:79-88`: The `embed()` method uses
synchronous `requests.post()` in an otherwise async codebase. This blocks
the event loop during every embedding call. The docstring even acknowledges
this: "the caller is responsible for threading/async if needed."

### 12.3 System mode file is read on every call

`agents/_shared.py:48-63`: `_read_system_mode()` performs a filesystem read
on every invocation. In a hot loop, this could mean hundreds of file reads
per second. No caching or file-modification-time check.

### 12.4 Idempotency cache has arbitrary 10k bound

`infra/resilience/idempotency.py:196-202`: The in-memory cache evicts
entries when it exceeds 10,000 items, using insertion-order deletion (not
LRU). There is no TTL-based eviction. Evicting a still-relevant key causes
the operation to be re-processed, defeating idempotency.

### 12.5 Task queue fetches all pending tasks on every get_next()

`runtime/tasks/queue.py:152`: `get_next()` fetches ALL pending tasks, scores
them all, and picks the best one. As the task count grows, this becomes
O(n) per micro loop iteration for what should be a priority queue O(log n)
operation.

### 12.6 Unbounded BFS in lineage traversal

`tinker_platform/lineage/tracker.py:273-303`: `get_full_ancestry()` and
`get_descendants()` use BFS with a `max_depth` limit but no `max_nodes`
limit. In a highly connected lineage graph, the visited set can grow to
consume all available memory.

---

## 13. Dependency and Packaging Issues

### 13.1 Optional dependencies fail silently

The codebase is littered with try/except ImportError patterns:
- `aiohttp` — the HTTP client for the LLM router
- `fastapi` — the web UI framework
- `playwright` — the web scraper
- `trafilatura` — the content extractor
- `cryptography` — the encryption module
- `chromadb` — the vector database
- `duckdb` — the analytics database
- `redis` — the caching layer

Each missing dependency silently degrades functionality. There is no startup
check that reports "these features are unavailable because these packages are
missing." You can run the system with half its features silently disabled.

### 13.2 Test dependencies not separated

Tests that require `fastapi` (web UI tests), `cryptography` (encryption
tests), and other optional packages fail at collection time rather than
being properly marked with `pytest.mark.skipif` or requiring extras.

### 13.3 No lockfile or pinned dependencies visible

No `requirements.txt`, `poetry.lock`, `Pipfile.lock`, or equivalent pinned
dependency file was found in the standard locations. Dependencies are
unpinned, making builds non-reproducible.

---

## 14. Summary Scoreboard

### By Category

| Category | Critical | High | Medium | Low | Total |
|----------|----------|------|--------|-----|-------|
| Security | 4 | 3 | 2 | 1 | **10** |
| Logic Bugs | 2 | 4 | 2 | 0 | **8** |
| Error Handling | 1 | 3 | 3 | 2 | **9** |
| Race Conditions | 2 | 3 | 3 | 0 | **8** |
| Stubs/Incomplete | 0 | 3 | 4 | 1 | **8** |
| Architecture | 1 | 2 | 4 | 1 | **8** |
| Cryptography | 1 | 2 | 2 | 0 | **5** |
| Observability | 0 | 2 | 2 | 1 | **5** |
| Configuration | 1 | 1 | 3 | 0 | **5** |
| Performance | 0 | 1 | 3 | 2 | **6** |
| Dependencies | 0 | 1 | 2 | 0 | **3** |
| Test Suite | 2 | 3 | 1 | 1 | **7** |
| **TOTAL** | **14** | **28** | **31** | **9** | **82** |

### Top 10 "Fix These First" Items

1. **Add authentication to the web UI** — anyone on the network owns your system
2. **Fix CORS to restrict origins** — `allow_origins=["*"]` is a security hole
3. **Fix the validation logic bug** — `config/validation.py:165` literally cannot work
4. **Fix the 50 failing tests** — you can't ship what you can't test
5. **Fix the 8 test collection errors** — entire subsystems are unverifiable
6. **Use TinkerSettings instead of os.getenv()** — one source of truth for config
7. **Add SSRF protection to the web scraper** — block private IPs and metadata endpoints
8. **Increase PBKDF2 iterations to 600k+** — current 100k is below NIST minimum
9. **Make decryption failures loud, not silent** — stop returning plaintext on GCM failure
10. **Add asyncio.Lock to orchestrator state** — race conditions corrupt the core loop

### The Uncomfortable Truth

This project has impressive *breadth*. 336 Python files, 482 classes,
enterprise-sounding features like circuit breakers, SLA tracking, A/B testing,
lineage tracking, and distributed locking. The documentation is thorough and
the code is well-commented.

But breadth without depth is a liability. You have:
- An A/B testing system with inverted traffic allocation
- A stagnation detection system where every test fails
- A configuration system that the bootstrap code ignores
- An audit log that loses data on crash
- An encryption system that silently falls back to plaintext
- A validation system with a logic bug that makes it always pass

Every one of these features is *almost* right — which is worse than not
having them at all, because they create false confidence. You think you
have encryption, but you don't. You think you have validation, but it's
broken. You think you have stagnation detection, but it's untested.

Strip it down to what actually works, fix those 50 failing tests, add auth
to the web UI, and *then* expand. Right now you have a beautiful house of
cards.

---

*End of audit. Go fix your tests.*
