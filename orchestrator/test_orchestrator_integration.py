"""
test_orchestrator_integration.py

Integration test: runs 3 complete micro loops end-to-end, verifies all
state transitions, checks meso escalation, and confirms graceful shutdown.

Run with:
    python -m pytest tinker/orchestrator/test_orchestrator_integration.py -v
    # or directly:
    python tinker/orchestrator/test_orchestrator_integration.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

# ── allow running from repo root ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.orchestrator import Orchestrator
from orchestrator.config import OrchestratorConfig
from orchestrator.state import LoopStatus
from orchestrator.stubs import build_stub_components, StubMemoryManager, StubArchStateManager

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test.integration")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_orchestrator(
    target_micro_loops: int = 3,
    meso_trigger: int = 2,
    macro_interval: float = 9999.0,   # disable timer-based macro in most tests
    **config_overrides,
) -> tuple[Orchestrator, dict]:
    """
    Build a fully-wired orchestrator that shuts itself down after
    `target_micro_loops` successful micro iterations.
    """
    components = build_stub_components()

    overrides = {
        "meso_trigger_count": meso_trigger,
        "macro_interval_seconds": macro_interval,
        "micro_loop_idle_seconds": 0.0,
        "failure_backoff_seconds": 0.1,
        "architect_timeout": 5.0,
        "critic_timeout": 5.0,
        "synthesizer_timeout": 5.0,
        "tool_timeout": 5.0,
        "state_snapshot_path": "/tmp/tinker_test_state.json",
    }
    overrides.update(config_overrides)   # caller wins on conflicts
    cfg = OrchestratorConfig(**overrides)

    orch = Orchestrator(config=cfg, **components)

    # Install a hook that shuts the orchestrator down after N micro loops
    original_run_micro = orch._run_micro.__func__  # type: ignore[attr-defined]

    async def _patched_run_micro(self_: Orchestrator) -> bool:
        result = await original_run_micro(self_)
        if self_.state.total_micro_loops >= target_micro_loops:
            logger.info(
                "Test hook: reached %d micro loops — requesting shutdown",
                target_micro_loops,
            )
            self_.request_shutdown()
        return result

    import types
    orch._run_micro = types.MethodType(_patched_run_micro, orch)

    return orch, components


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: 3 micro loops complete successfully
# ─────────────────────────────────────────────────────────────────────────────

async def test_three_micro_loops():
    logger.info("=" * 60)
    logger.info("TEST 1: 3 micro loops complete successfully")
    logger.info("=" * 60)

    orch, components = _make_orchestrator(target_micro_loops=3)
    memory: StubMemoryManager = components["memory_manager"]

    start = time.monotonic()
    await orch.run()
    elapsed = time.monotonic() - start

    # ── Assertions ────────────────────────────────────────────────────────────
    assert orch.state.total_micro_loops == 3, (
        f"Expected 3 micro loops, got {orch.state.total_micro_loops}"
    )
    assert len(orch.state.micro_history) == 3
    assert all(r.status == LoopStatus.SUCCESS for r in orch.state.micro_history), (
        f"Not all micro loops succeeded: {[r.status for r in orch.state.micro_history]}"
    )
    assert memory.artifact_count == 3, (
        f"Expected 3 stored artifacts, got {memory.artifact_count}"
    )

    # Every record should have an artifact_id and task_id
    for record in orch.state.micro_history:
        assert record.artifact_id, f"Record missing artifact_id: {record}"
        assert record.task_id, f"Record missing task_id: {record}"
        assert record.finished_at is not None
        assert record.duration() > 0

    logger.info("✓ test_three_micro_loops PASSED (%.2fs)", elapsed)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Meso escalation triggers after N micro loops on same subsystem
# ─────────────────────────────────────────────────────────────────────────────

async def test_meso_escalation():
    logger.info("=" * 60)
    logger.info("TEST 2: Meso escalation triggers correctly")
    logger.info("=" * 60)

    # Force all tasks to the same subsystem so the counter accumulates
    from orchestrator.stubs import StubTaskEngine
    import uuid, time as _time

    class SingleSubsystemTaskEngine(StubTaskEngine):
        def _make_task(self, parent_id=None):
            return {
                "id": str(uuid.uuid4()),
                "subsystem": "api_gateway",   # always the same
                "description": "API gateway design task",
                "priority": 3,
                "tags": ["api_gateway"],
                "parent_id": parent_id,
                "created_at": _time.time(),
            }

    components = build_stub_components()
    components["task_engine"] = SingleSubsystemTaskEngine(initial_tasks=20)
    memory: StubMemoryManager = components["memory_manager"]

    cfg = OrchestratorConfig(
        meso_trigger_count=2,           # meso fires after every 2 micro loops
        macro_interval_seconds=9999.0,
        micro_loop_idle_seconds=0.0,
        failure_backoff_seconds=0.1,
        architect_timeout=5.0,
        critic_timeout=5.0,
        synthesizer_timeout=5.0,
        tool_timeout=5.0,
        meso_min_artifacts=1,           # ensure meso doesn't skip for low artifact count
        state_snapshot_path="/tmp/tinker_test_meso_state.json",
    )

    orch = Orchestrator(config=cfg, **components)

    # Shutdown after 4 micro loops (should trigger meso at least once at count=2)
    import types

    async def _patched(self_: Orchestrator) -> bool:
        from orchestrator.micro_loop import run_micro_loop, MicroLoopError
        from orchestrator.state import LoopStatus
        try:
            record = await run_micro_loop(self_)
            self_.state.total_micro_loops += 1
            self_.state.current_task_id = record.task_id
            self_.state.current_subsystem = record.subsystem
            self_.state.add_micro_record(record)
            if record.status == LoopStatus.SUCCESS:
                self_.state.increment_subsystem(record.subsystem)
                if self_.state.total_micro_loops >= 4:
                    self_.request_shutdown()
                return True
            return False
        except MicroLoopError as exc:
            logger.error("Micro loop failed: %s", exc)
            return False

    orch._run_micro = types.MethodType(_patched, orch)

    await orch.run()

    assert orch.state.total_micro_loops == 4, (
        f"Expected 4 micro loops, got {orch.state.total_micro_loops}"
    )
    assert orch.state.total_meso_loops >= 1, (
        f"Expected at least 1 meso loop, got {orch.state.total_meso_loops}"
    )
    assert len(orch.state.meso_history) >= 1
    meso_record = orch.state.meso_history[0]
    assert meso_record.subsystem == "api_gateway"
    assert meso_record.status == LoopStatus.SUCCESS
    assert memory.document_count >= 1, "Expected at least 1 subsystem document stored"

    logger.info(
        "✓ test_meso_escalation PASSED — %d meso loop(s) fired", orch.state.total_meso_loops
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Graceful shutdown — finish current loop, write snapshot, exit
# ─────────────────────────────────────────────────────────────────────────────

async def test_graceful_shutdown():
    logger.info("=" * 60)
    logger.info("TEST 3: Graceful shutdown")
    logger.info("=" * 60)

    snapshot_path = "/tmp/tinker_test_shutdown_state.json"
    orch, _ = _make_orchestrator(
        target_micro_loops=3,
        state_snapshot_path=snapshot_path,
    )

    await orch.run()

    # State should be SHUTDOWN and snapshot should be on disk
    assert orch.state.status == LoopStatus.SHUTDOWN, (
        f"Expected SHUTDOWN status, got {orch.state.status}"
    )
    import json, os
    assert os.path.exists(snapshot_path), "State snapshot not written to disk"
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    assert snapshot["status"] == "shutdown"
    assert snapshot["totals"]["micro"] == 3

    logger.info("✓ test_graceful_shutdown PASSED — snapshot at %s", snapshot_path)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Failure recovery — orchestrator survives repeated agent errors
# ─────────────────────────────────────────────────────────────────────────────

async def test_failure_recovery():
    logger.info("=" * 60)
    logger.info("TEST 4: Failure recovery — survives agent errors")
    logger.info("=" * 60)

    components = build_stub_components()

    # Replace architect with one that fails the first 2 calls
    call_count = {"n": 0}

    class FlakyArchitectAgent:
        async def call(self, task, context):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError(f"Simulated architect failure #{call_count['n']}")
            return {
                "content": "Recovery successful",
                "tokens_used": 100,
                "knowledge_gaps": [],
            }

    components["architect_agent"] = FlakyArchitectAgent()

    cfg = OrchestratorConfig(
        meso_trigger_count=999,
        macro_interval_seconds=9999.0,
        micro_loop_idle_seconds=0.0,
        failure_backoff_seconds=0.01,   # fast in tests
        max_consecutive_failures=2,
        architect_timeout=5.0,
        critic_timeout=5.0,
        synthesizer_timeout=5.0,
        tool_timeout=5.0,
        state_snapshot_path="/tmp/tinker_test_recovery_state.json",
    )

    orch = Orchestrator(config=cfg, **components)

    # Shutdown after 1 successful micro loop (failures don't count)
    import types
    from orchestrator.micro_loop import run_micro_loop, MicroLoopError
    from orchestrator.state import LoopStatus

    async def _patched(self_: Orchestrator) -> bool:
        try:
            record = await run_micro_loop(self_)
            self_.state.total_micro_loops += 1
            self_.state.current_task_id = record.task_id
            self_.state.current_subsystem = record.subsystem
            self_.state.add_micro_record(record)
            if record.status == LoopStatus.SUCCESS:
                self_.state.increment_subsystem(record.subsystem)
                self_.request_shutdown()  # 1 success → done
                return True
            return False
        except MicroLoopError:
            return False

    orch._run_micro = types.MethodType(_patched, orch)

    start = time.monotonic()
    await orch.run()
    elapsed = time.monotonic() - start

    assert call_count["n"] >= 3, f"Expected at least 3 architect calls, got {call_count['n']}"
    assert orch.state.total_micro_loops >= 1, "Expected at least 1 successful micro loop"
    success_records = [r for r in orch.state.micro_history if r.status == LoopStatus.SUCCESS]
    assert len(success_records) >= 1

    logger.info(
        "✓ test_failure_recovery PASSED — recovered after %d failures (%.2fs)",
        call_count["n"] - 1, elapsed,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: State snapshot is valid JSON and contains expected fields
# ─────────────────────────────────────────────────────────────────────────────

async def test_state_snapshot():
    logger.info("=" * 60)
    logger.info("TEST 5: State snapshot structure")
    logger.info("=" * 60)

    orch, _ = _make_orchestrator(target_micro_loops=2)
    await orch.run()

    snapshot = orch.get_state_snapshot()

    required_keys = {"uptime_seconds", "status", "current_level", "totals",
                     "subsystem_micro_counts", "micro_history", "meso_history", "macro_history"}
    missing = required_keys - set(snapshot.keys())
    assert not missing, f"Snapshot missing keys: {missing}"

    assert snapshot["totals"]["micro"] == 2
    assert len(snapshot["micro_history"]) == 2
    for entry in snapshot["micro_history"]:
        assert "task_id" in entry
        assert "subsystem" in entry
        assert "status" in entry

    logger.info("✓ test_state_snapshot PASSED")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Macro loop fires when interval elapses
# ─────────────────────────────────────────────────────────────────────────────

async def test_macro_fires():
    logger.info("=" * 60)
    logger.info("TEST 6: Macro loop fires when interval elapses")
    logger.info("=" * 60)

    orch, components = _make_orchestrator(
        target_micro_loops=2,
        macro_interval=0.0,   # fire immediately
    )
    arch_state: StubArchStateManager = components["arch_state_manager"]

    await orch.run()

    assert orch.state.total_macro_loops >= 1, (
        f"Expected at least 1 macro loop, got {orch.state.total_macro_loops}"
    )
    assert arch_state.commit_count >= 1, "Expected at least 1 Git commit"
    macro_record = orch.state.macro_history[-1]
    assert macro_record.commit_hash is not None
    assert macro_record.status == LoopStatus.SUCCESS

    logger.info(
        "✓ test_macro_fires PASSED — %d macro(s), %d commit(s)",
        orch.state.total_macro_loops, arch_state.commit_count,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_all_tests():
    tests = [
        test_three_micro_loops,
        test_meso_escalation,
        test_graceful_shutdown,
        test_failure_recovery,
        test_state_snapshot,
        test_macro_fires,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            await test()
            passed += 1
        except AssertionError as exc:
            logger.error("FAIL %s: %s", test.__name__, exc)
            failed += 1
        except Exception as exc:
            logger.exception("ERROR %s: %s", test.__name__, exc)
            failed += 1
        print()

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_all_tests())
    sys.exit(0 if ok else 1)
