"""
OrchestratorConfig — all tuneable parameters in one place.
Defaults are sane for a long-running production run; override in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OrchestratorConfig:
    # ── Micro loop ──────────────────────────────────────────────────────────
    # How many micro loops to run before considering a meso synthesis on
    # a given subsystem.
    meso_trigger_count: int = 5

    # Maximum consecutive micro-loop failures before the orchestrator backs
    # off and sleeps briefly.
    max_consecutive_failures: int = 3

    # Seconds to sleep after a burst of failures before resuming.
    failure_backoff_seconds: float = 10.0

    # Seconds to sleep between micro loops (0 = run flat-out).
    micro_loop_idle_seconds: float = 0.0

    # ── Meso loop ───────────────────────────────────────────────────────────
    # Minimum number of artifacts required to justify a meso synthesis.
    meso_min_artifacts: int = 2

    # ── Macro loop ──────────────────────────────────────────────────────────
    # How often (seconds) to trigger a full architectural snapshot.
    macro_interval_seconds: float = 4 * 60 * 60   # 4 hours

    # ── Researcher routing ──────────────────────────────────────────────────
    # Maximum Tool Layer calls per micro loop (guard against runaway loops).
    max_researcher_calls_per_loop: int = 3

    # ── Timeouts (seconds) ──────────────────────────────────────────────────
    architect_timeout: float = 120.0
    critic_timeout: float = 60.0
    synthesizer_timeout: float = 180.0
    tool_timeout: float = 30.0

    # ── Context assembly ────────────────────────────────────────────────────
    # Maximum number of prior artifacts injected into architect context.
    context_max_artifacts: int = 10

    # ── Dashboard state path ────────────────────────────────────────────────
    state_snapshot_path: str = "/tmp/tinker_orchestrator_state.json"
