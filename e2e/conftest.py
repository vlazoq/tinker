"""
e2e/conftest.py
================
Pytest fixtures for end-to-end tests.

All fixtures use in-process stubs — no real Ollama, Redis, or DuckDB required.
The session-scoped fixtures are created once per test session for efficiency.

pytest.ini_options addition (do NOT add to pyproject.toml here — shown for reference):

    [tool.pytest.ini_options]
    asyncio_mode = "auto"
    markers = [
        "e2e: end-to-end tests (require stub orchestrator, no external services)",
        "integration: integration tests (require Docker compose services)",
        "slow: tests that take > 5 seconds",
    ]
    testpaths = ["e2e", ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the tinker root is on the import path so all packages resolve.
ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def tinker_config():
    """
    Return an OrchestratorConfig tuned for fast test execution.

    Key overrides from the production defaults:
      - meso_trigger_count=2   : meso synthesis fires after just 2 micro loops
                                  on the same subsystem (prod default: 5)
      - macro_interval_seconds=9999 : effectively disables the macro loop
                                       during tests (prod default: 4 hours)
      - architect_timeout / critic_timeout : set to 5 s to fail fast
    """
    from orchestrator.config import OrchestratorConfig

    return OrchestratorConfig(
        meso_trigger_count=2,
        macro_interval_seconds=9999,
        architect_timeout=5.0,
        critic_timeout=5.0,
        synthesizer_timeout=10.0,
        tool_timeout=5.0,
        # Disable idle sleep so the loop runs at maximum speed in tests.
        micro_loop_idle_seconds=0.0,
        # Disable quality gate alerting by default (individual tests can
        # enable it by wiring their own alerter into orch.enterprise).
        quality_gate_threshold=0.0,
        # Write snapshots to /tmp so tests don't pollute the working dir.
        state_snapshot_path="/tmp/tinker_test_state.json",
    )


# ---------------------------------------------------------------------------
# Stub orchestrator fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def stub_orchestrator(tinker_config):
    """
    Build a minimal Orchestrator wired entirely with in-process stubs.

    The stubs (orchestrator/stubs.py) implement the same interfaces as the
    real components but return synthetic data without making any network calls.

    The orchestrator is returned *not yet running* — tests call
    ``orchestrator.run()`` or ``orchestrator.start()`` themselves so they can
    control the lifecycle.

    Note: session scope means the same Orchestrator instance is shared across
    all tests in the session.  Tests that need isolated state should create
    their own orchestrator via ``_make_stub_orchestrator(config)``.
    """
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.stubs import build_stub_components

    components = build_stub_components()
    orch = Orchestrator(
        config=tinker_config,
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
    return orch


# ---------------------------------------------------------------------------
# Helper: create a fresh stub orchestrator with a given config
# ---------------------------------------------------------------------------


def make_stub_orchestrator(config=None):
    """
    Factory function: create a fresh Orchestrator with stub components.

    Accepts an optional OrchestratorConfig; falls back to a fast test config
    if none is provided.  Tests that need full isolation use this rather than
    the session-scoped ``stub_orchestrator`` fixture.
    """
    from orchestrator.config import OrchestratorConfig
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.stubs import build_stub_components

    if config is None:
        config = OrchestratorConfig(
            meso_trigger_count=2,
            macro_interval_seconds=9999,
            architect_timeout=5.0,
            critic_timeout=5.0,
            synthesizer_timeout=10.0,
            tool_timeout=5.0,
            micro_loop_idle_seconds=0.0,
            quality_gate_threshold=0.0,
            state_snapshot_path="/tmp/tinker_test_state.json",
        )

    components = build_stub_components()
    return Orchestrator(
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
