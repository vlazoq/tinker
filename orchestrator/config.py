"""
orchestrator/config.py
======================

All of the orchestrator's tuneable numbers live here — in one place.

Why centralise configuration?
------------------------------
Imagine the alternative: magic numbers scattered throughout the codebase.
You'd need to grep through dozens of files to find out why the system sleeps
for 10 seconds or why it synthesises after 5 loops.  A central config file
means:

* One place to look when you want to understand behaviour.
* One place to change when you want to tune performance.
* Easy to override in tests — just pass a different config object.

What is a dataclass?
---------------------
``@dataclass`` is a Python decorator (think of it as a label you stick on a
class) that automatically writes boilerplate code for you.  In particular it
generates:

* ``__init__`` — so you can do ``OrchestratorConfig(meso_trigger_count=3)``
* ``__repr__`` — so it prints nicely in logs and the debugger

Every field gets a *default value*, so you can create a config with just
``OrchestratorConfig()`` and get sensible production defaults, or you can
override only the fields you care about in tests.

How the three loops use this config
-------------------------------------
  Micro loop — ``meso_trigger_count``, ``max_consecutive_failures``,
               ``failure_backoff_seconds``, ``micro_loop_idle_seconds``,
               ``architect_timeout``, ``critic_timeout``,
               ``max_researcher_calls_per_loop``, ``context_max_artifacts``

  Meso loop  — ``meso_min_artifacts``, ``synthesizer_timeout``

  Macro loop — ``macro_interval_seconds``, ``synthesizer_timeout``

  All loops  — ``state_snapshot_path`` (where the Dashboard reads live state)
"""
from __future__ import annotations

import os
# ``dataclass`` is the decorator; ``field`` lets us define fields whose
# default values are computed at runtime (e.g. reading an env variable).
from dataclasses import dataclass, field


@dataclass
class OrchestratorConfig:
    """
    A single object that holds every knob the Orchestrator exposes.

    Defaults are designed for a long-running production run where the system
    should work as fast as possible (``micro_loop_idle_seconds=0``) while
    still being resilient to transient AI-call failures.

    For unit tests, you typically want:
        OrchestratorConfig(
            meso_trigger_count=2,       # fire meso after just 2 micro loops
            macro_interval_seconds=1,   # fire macro after 1 second
            architect_timeout=5,        # fail fast rather than wait 2 minutes
        )
    """

    # ── Micro loop ──────────────────────────────────────────────────────────
    # The micro loop is the smallest, fastest unit of work Tinker does.
    # It picks one task, runs it through Architect → Critic, stores the
    # result, and spawns new tasks.  These settings govern its pacing and
    # error tolerance.

    # After this many successful micro loops on the *same subsystem*, the
    # orchestrator pauses the micro loop and runs a meso synthesis instead.
    # Think of it like: "after 5 thoughts about the auth_service, stop and
    # write a summary of what we know so far."
    meso_trigger_count: int = 5

    # If a micro loop fails, that's okay — AI calls can be flaky.  But if
    # it fails this many times *in a row*, something is probably seriously
    # wrong (bad network, quota exhausted, etc.) and we should sleep before
    # hammering the API again.
    max_consecutive_failures: int = 3

    # How long to sleep (in seconds) after hitting ``max_consecutive_failures``
    # before trying again.  10 seconds is gentle — enough for a transient
    # network blip to resolve without losing much time.
    failure_backoff_seconds: float = 10.0

    # How long to wait between micro loops when everything is going fine.
    # 0.0 means "run flat-out" — no artificial delay.  You'd increase this
    # if you wanted to reduce API costs or CPU usage during development.
    micro_loop_idle_seconds: float = 0.0

    # ── Meso loop ───────────────────────────────────────────────────────────
    # The meso loop synthesises a subsystem-level design document from the
    # individual artifacts produced by recent micro loops.

    # Don't bother synthesising if there are fewer than this many artifacts
    # available for the subsystem.  A synthesis from a single artifact would
    # be pointless — we need at least a couple of data points to find patterns.
    meso_min_artifacts: int = 2

    # ── Macro loop ──────────────────────────────────────────────────────────
    # The macro loop produces a full architectural snapshot — an AI-authored
    # document describing the entire system — and commits it to version control.

    # How many seconds between macro loop runs.  4 * 60 * 60 = 14400 seconds
    # = 4 hours.  This is intentionally a long interval: the macro snapshot is
    # expensive (it reads everything in memory) and is meant to capture slow,
    # high-level drift in the architecture, not minute-to-minute changes.
    macro_interval_seconds: float = 4 * 60 * 60   # 4 hours

    # ── Researcher routing ──────────────────────────────────────────────────
    # When the Architect AI says "I don't know enough about X", the orchestrator
    # can call a Tool Layer to look X up.  This setting caps how many such
    # lookups can happen in a single micro loop, preventing runaway tool calls
    # from a very confused Architect.
    max_researcher_calls_per_loop: int = 3

    # ── Timeouts (seconds) ──────────────────────────────────────────────────
    # Every AI call is wrapped in ``asyncio.wait_for`` with these timeouts.
    # If the call doesn't respond in time, it's treated as a failure and the
    # micro loop error-handling kicks in.
    #
    # Architect gets the most time (120 s) because it does the most complex
    # reasoning.  Critic gets less (60 s) because it's reviewing existing work.
    # Synthesizer gets the most of all (180 s) because it may be digesting
    # dozens of artifacts at once.  Tool calls are capped tightly (30 s)
    # because a slow web-search shouldn't block the whole micro loop.
    architect_timeout: float = 120.0
    critic_timeout: float = 60.0
    synthesizer_timeout: float = 180.0
    tool_timeout: float = 30.0

    # ── Context assembly ────────────────────────────────────────────────────
    # Before calling the Architect, we retrieve prior artifacts from memory
    # to give it background context.  This caps how many we pull — enough
    # for meaningful context without overwhelming the AI's context window.
    context_max_artifacts: int = 10

    # ── Dashboard state path ────────────────────────────────────────────────
    # After every micro loop, the orchestrator serialises its live state to
    # this JSON file so the Dashboard (a separate process) can read it without
    # locking.  The ``field(default_factory=...)`` pattern means the default
    # value is computed *at the time an OrchestratorConfig is created*, not
    # when Python first loads this module — which means the env-var is read
    # at the right moment.
    #
    # Override the file path with the TINKER_STATE_PATH environment variable,
    # e.g. ``export TINKER_STATE_PATH=/var/run/tinker/state.json``
    state_snapshot_path: str = field(
        default_factory=lambda: os.getenv(
            "TINKER_STATE_PATH", "/tmp/tinker_orchestrator_state.json"
        )
    )
