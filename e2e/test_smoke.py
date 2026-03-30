"""
e2e/test_smoke.py
==================
Smoke tests: verify the system starts, runs loops, and shuts down cleanly.
All tests use in-process stubs (no real Ollama/Redis/DuckDB required).
Mark: @pytest.mark.e2e
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from e2e.conftest import make_stub_orchestrator

# ---------------------------------------------------------------------------
# Test 1: Orchestrator starts and stops cleanly
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_orchestrator_starts_and_stops():
    """
    Verify that the orchestrator can be started, run briefly, and shut down
    without raising any exceptions.

    The test starts the orchestrator's run() coroutine as a background asyncio
    Task, waits 2 seconds for at least one micro loop to execute, then calls
    request_shutdown() and awaits the task with a 10-second timeout.
    """
    orch = make_stub_orchestrator()

    task = asyncio.create_task(orch.run())

    # Give the orchestrator time to run at least one iteration.
    await asyncio.sleep(2)

    # Request a graceful shutdown.
    orch.request_shutdown()

    # Wait up to 10 s for the orchestrator to stop cleanly.
    try:
        await asyncio.wait_for(task, timeout=10)
    except TimeoutError:
        task.cancel()
        pytest.fail("Orchestrator did not shut down within 10 seconds")

    # The task should have completed without an exception.
    assert not task.cancelled(), "Orchestrator task was unexpectedly cancelled"
    exc = task.exception() if not task.cancelled() else None
    assert exc is None, f"Orchestrator raised an exception: {exc}"


# ---------------------------------------------------------------------------
# Test 2: At least one micro loop runs
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_micro_loop_runs_at_least_once():
    """
    Verify that at least one micro loop executes successfully before shutdown.

    Checks state.total_micro_loops after the orchestrator has been running
    for 2 seconds.  With stub components (no network latency beyond the
    small asyncio.sleep inside each stub call) multiple loops should complete
    well within this window.
    """
    orch = make_stub_orchestrator()

    task = asyncio.create_task(orch.run())

    # Give the orchestrator time to execute some micro loops.
    await asyncio.sleep(2)

    micro_loops_before_shutdown = orch.state.total_micro_loops
    orch.request_shutdown()

    try:
        await asyncio.wait_for(task, timeout=10)
    except TimeoutError:
        task.cancel()
        pytest.fail("Orchestrator did not shut down within 10 seconds")

    assert micro_loops_before_shutdown >= 1, (
        f"Expected at least 1 micro loop to have run, "
        f"but total_micro_loops={micro_loops_before_shutdown}"
    )


# ---------------------------------------------------------------------------
# Test 3: Quality gate fires when critic score is low
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_quality_gate_fires_on_low_score():
    """
    Verify that the quality gate alert fires when the stub critic always
    returns a score below the configured threshold.

    Setup:
    - A stub critic that always returns score=0.1 (well below the 0.4 default).
    - A mock alerter wired into orch.enterprise["alerter"].
    - quality_gate_threshold=0.4 so every micro loop triggers the gate.

    The test runs exactly 3 micro loops by counting invocations of the mock
    alerter, then shuts down.  It asserts that the alerter was called at
    least once with a quality gate breach.
    """
    from runtime.orchestrator.config import OrchestratorConfig
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.stubs import build_stub_components

    # ── Build components with a low-scoring critic ────────────────────────────
    components = build_stub_components()

    # Replace the stub critic with one that always returns score=0.1
    class LowScoreCritic:
        async def call(self, task: dict, architect_result: dict) -> dict:
            return {
                "content": "Score: 0.1\nEverything is terrible.",
                "tokens_used": 50,
                "score": 0.1,
                "flags": ["low_quality"],
            }

    components["critic_agent"] = LowScoreCritic()

    # ── Configure with an active quality gate ─────────────────────────────────
    config = OrchestratorConfig(
        meso_trigger_count=10,  # prevent meso synthesis during test
        macro_interval_seconds=9999,  # disable macro loop
        architect_timeout=5.0,
        critic_timeout=5.0,
        synthesizer_timeout=10.0,
        tool_timeout=5.0,
        micro_loop_idle_seconds=0.0,
        quality_gate_threshold=0.4,  # alert when score < 0.4
        state_snapshot_path=str(Path(tempfile.gettempdir()) / "tinker_test_qg_state.json"),
    )

    # ── Wire a mock alerter ────────────────────────────────────────────────────
    mock_alerter = MagicMock()
    # alert() is a coroutine — use AsyncMock so awaiting it works
    mock_alerter.alert = AsyncMock(return_value=True)

    orch = Orchestrator(
        config=config,
        task_engine=components["task_engine"],
        context_assembler=components["context_assembler"],
        architect_agent=components["architect_agent"],
        critic_agent=components["critic_agent"],
        synthesizer_agent=components["synthesizer_agent"],
        memory_manager=components["memory_manager"],
        tool_layer=components["tool_layer"],
        arch_state_manager=components["arch_state_manager"],
        stagnation_monitor=None,
        metrics=None,
        snapshot_callback=None,
    )

    # Inject the mock alerter into the enterprise dict so micro_loop.py picks it up.
    orch.enterprise["alerter"] = mock_alerter

    # ── Run until we have at least 3 micro loops ──────────────────────────────
    async def _run_and_stop():
        run_task = asyncio.create_task(orch.run())
        # Poll until 3 micro loops have completed (or timeout after 15 s)
        for _ in range(150):
            await asyncio.sleep(0.1)
            if orch.state.total_micro_loops >= 3:
                break
        orch.request_shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=10)
        except TimeoutError:
            run_task.cancel()

    await _run_and_stop()

    # ── Assertions ────────────────────────────────────────────────────────────
    assert orch.state.total_micro_loops >= 3, (
        f"Expected >= 3 micro loops, got {orch.state.total_micro_loops}"
    )

    assert mock_alerter.alert.called, (
        "Expected the alerter to be called with a quality gate breach, but it was never called."
    )

    # Verify at least one call included a quality gate breach title.
    found_quality_gate_call = False
    for call_args in mock_alerter.alert.call_args_list:
        # call_args.kwargs or call_args[1] depending on how it was called
        kwargs = call_args.kwargs if call_args.kwargs else {}
        args = call_args.args if call_args.args else ()
        title = kwargs.get("title", args[1] if len(args) > 1 else "")
        if "quality gate" in title.lower() or "Quality gate" in title:
            found_quality_gate_call = True
            break

    assert found_quality_gate_call, (
        f"Expected an alert with 'quality gate' in the title, "
        f"but got calls: {mock_alerter.alert.call_args_list}"
    )
