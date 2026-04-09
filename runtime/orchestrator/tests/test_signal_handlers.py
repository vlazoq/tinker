"""
runtime/orchestrator/tests/test_signal_handlers.py
==========================================
Unit tests for Orchestrator._install_signal_handlers.

Covers three branches:
  1. Unix / happy path  — loop.add_signal_handler() is called for SIGINT + SIGTERM.
  2. Windows fallback   — NotImplementedError triggers signal.signal(SIGINT, …).
  3. No-signal env      — NotImplementedError on non-win32; only a warning is logged.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from unittest.mock import MagicMock, patch

from runtime.orchestrator.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Minimal stub that satisfies Orchestrator.__init__'s required kwargs
# ---------------------------------------------------------------------------

_DUMMY_DEPS = dict(
    task_engine=MagicMock(),
    context_assembler=MagicMock(),
    architect_agent=MagicMock(),
    critic_agent=MagicMock(),
    synthesizer_agent=MagicMock(),
    memory_manager=MagicMock(),
    tool_layer=MagicMock(),
    arch_state_manager=MagicMock(),
)


def _make_orchestrator() -> Orchestrator:
    return Orchestrator(**_DUMMY_DEPS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop_with_add_signal_handler(raises: Exception | None = None) -> MagicMock:
    """Return a mock event loop whose add_signal_handler behaves as requested."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    if raises is not None:
        loop.add_signal_handler.side_effect = raises
    return loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInstallSignalHandlersUnix:
    """Happy path: asyncio loop supports add_signal_handler (Linux / macOS)."""

    def test_adds_sigint_and_sigterm(self) -> None:
        orch = _make_orchestrator()
        mock_loop = _make_loop_with_add_signal_handler()

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            orch._install_signal_handlers()

        calls = mock_loop.add_signal_handler.call_args_list
        registered_signals = {c.args[0] for c in calls}
        assert signal.SIGINT in registered_signals
        assert signal.SIGTERM in registered_signals

    def test_handler_callback_is_handle_signal(self) -> None:
        orch = _make_orchestrator()
        mock_loop = _make_loop_with_add_signal_handler()

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            orch._install_signal_handlers()

        for c in mock_loop.add_signal_handler.call_args_list:
            _sig, callback = c.args[0], c.args[1]
            assert callback == orch._handle_signal, (
                f"Expected _handle_signal as callback for {_sig}, got {callback}"
            )


class TestInstallSignalHandlersWindowsFallback:
    """Windows path: loop.add_signal_handler raises NotImplementedError."""

    def test_falls_back_to_signal_signal_on_win32(self) -> None:
        orch = _make_orchestrator()
        mock_loop = _make_loop_with_add_signal_handler(raises=NotImplementedError())

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch.object(sys, "platform", "win32"),
            patch("signal.signal") as mock_signal,
        ):
            orch._install_signal_handlers()

        # signal.signal must be called exactly once — for SIGINT only.
        assert mock_signal.call_count == 1
        registered_sig = mock_signal.call_args.args[0]
        assert registered_sig == signal.SIGINT

    def test_fallback_handler_calls_request_shutdown(self) -> None:
        """The lambda installed on Windows must invoke request_shutdown()."""
        orch = _make_orchestrator()
        mock_loop = _make_loop_with_add_signal_handler(raises=NotImplementedError())
        installed_handler: list = []

        def capture_handler(sig, handler):
            installed_handler.append(handler)

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch.object(sys, "platform", "win32"),
            patch("signal.signal", side_effect=capture_handler),
            patch.object(orch, "request_shutdown") as mock_shutdown,
        ):
            orch._install_signal_handlers()

            assert installed_handler, "No signal handler was installed"
            # Simulate Ctrl-C: call the lambda with (sig_num, frame) as the OS would.
            installed_handler[0](signal.SIGINT, None)
            mock_shutdown.assert_called_once()

    def test_exception_during_fallback_install_is_swallowed(self) -> None:
        """If signal.signal itself raises, the method must not propagate."""
        orch = _make_orchestrator()
        mock_loop = _make_loop_with_add_signal_handler(raises=NotImplementedError())

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch.object(sys, "platform", "win32"),
            patch("signal.signal", side_effect=OSError("permission denied")),
        ):
            # Should NOT raise — the inner try/except catches it.
            orch._install_signal_handlers()


class TestInstallSignalHandlersNoSignalEnv:
    """Non-Windows environment where add_signal_handler is unavailable."""

    def test_no_signal_signal_call_on_non_win32(self) -> None:
        orch = _make_orchestrator()
        mock_loop = _make_loop_with_add_signal_handler(raises=NotImplementedError())

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch.object(sys, "platform", "linux"),
            patch("signal.signal") as mock_signal,
        ):
            orch._install_signal_handlers()

        # signal.signal must NOT be called on non-Windows.
        mock_signal.assert_not_called()
