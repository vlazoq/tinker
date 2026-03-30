"""
conftest.py — Root-level shared pytest fixtures for the Tinker test suite.

Provides reusable fixtures that eliminate boilerplate across test files:
  - mock_router        : A pre-configured MagicMock ModelRouter
  - mock_response      : A configurable AI response object
  - dummy_deps         : Dict of MagicMock components for Orchestrator.__init__
  - fast_config        : OrchestratorConfig tuned for fast test execution
  - stub_orchestrator  : A fully wired Orchestrator with stub components
  - tmp_state_path     : Temporary file path for state snapshots
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# AI / LLM mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_response():
    """
    Factory fixture: create a mock AI response with configurable fields.

    Usage::

        def test_something(mock_response):
            resp = mock_response(raw_text="hello", total_tokens=50)
    """

    def _factory(
        structured: dict | None = None,
        raw_text: str = "Mock AI response.",
        total_tokens: int = 100,
    ):
        resp = MagicMock()
        resp.structured = structured
        resp.raw_text = raw_text
        resp.total_tokens = total_tokens
        return resp

    return _factory


@pytest.fixture
def mock_router(mock_response):
    """
    A MagicMock ModelRouter whose ``complete()`` returns a configurable response.

    The default response has ``raw_text="Mock AI response."`` and
    ``total_tokens=100``.  Override by setting ``mock_router.complete.return_value``.
    """
    router = MagicMock()
    router.complete = AsyncMock(return_value=mock_response())
    return router


# ---------------------------------------------------------------------------
# Orchestrator fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dummy_deps():
    """
    Dict of MagicMock components satisfying Orchestrator.__init__'s required kwargs.

    Usage::

        def test_something(dummy_deps):
            orch = Orchestrator(**dummy_deps)
    """
    return dict(
        task_engine=MagicMock(),
        context_assembler=MagicMock(),
        architect_agent=MagicMock(),
        critic_agent=MagicMock(),
        synthesizer_agent=MagicMock(),
        memory_manager=MagicMock(),
        tool_layer=MagicMock(),
        arch_state_manager=MagicMock(),
    )


@pytest.fixture
def tmp_state_path(tmp_path):
    """Return a temporary file path for state snapshot writes."""
    return str(tmp_path / "tinker_test_state.json")


@pytest.fixture
def fast_config(tmp_state_path):
    """
    OrchestratorConfig tuned for fast test execution.

    Key overrides:
      - meso_trigger_count=2 (fires quickly)
      - macro_interval_seconds=9999 (effectively disabled)
      - All timeouts = 5s (fail fast)
      - micro_loop_idle_seconds=0 (no artificial delay)
      - quality_gate_threshold=0 (disabled)
      - state_snapshot_path → tmp dir
    """
    from runtime.orchestrator.config import OrchestratorConfig

    return OrchestratorConfig(
        meso_trigger_count=2,
        macro_interval_seconds=9999,
        architect_timeout=5.0,
        critic_timeout=5.0,
        synthesizer_timeout=10.0,
        tool_timeout=5.0,
        micro_loop_idle_seconds=0.0,
        quality_gate_threshold=0.0,
        state_snapshot_path=tmp_state_path,
    )


@pytest.fixture
def stub_orchestrator(fast_config):
    """
    A fully wired Orchestrator with in-process stub components.

    The stubs return synthetic data without making any network calls.
    The orchestrator is returned *not yet running* — call ``orch.run()``
    in your test to start it.
    """
    from runtime.orchestrator.orchestrator import Orchestrator
    from runtime.orchestrator.stubs import build_stub_components

    components = build_stub_components()
    return Orchestrator(
        config=fast_config,
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
