"""
runtime/orchestrator/confirmation.py
============================
ConfirmationGate — pause before irreversible actions and ask for human approval.

What this does
--------------
Some actions Tinker takes are hard or impossible to undo:
  - Pushing code to a remote git branch
  - Overwriting an existing artifact file
  - Dropping a database schema

Without a confirmation gate, Tinker takes these actions silently.  The gate
adds a pause: it creates a pending request, notifies the operator (via stdout
in CLI mode or via the Dashboard API), and waits for a yes/no response before
proceeding.

Two modes
---------
**CLI mode** (default when no dashboard is running):
  The gate prints a question to stdout and reads from stdin.  Simple, blocking.
  If a timeout is set, stdin reading is done in a thread so the event loop
  isn't blocked.  If the operator doesn't respond within the timeout, the
  action is auto-approved (configurable).

**API mode** (when a Dashboard is running):
  The gate creates a "pending confirmation" dict in OrchestratorState and sets
  an asyncio.Event that it waits on.  The Dashboard sees the pending request,
  displays it to the operator, and calls ``POST /api/confirm/{id}`` to
  approve or deny.  The event fires, the gate returns, and the action proceeds
  or is cancelled.

Integration
-----------
Components that need gating call ``gate.request(action_name, details)`` and
check the returned boolean::

    gate = ConfirmationGate(config, state)

    # In fritz/git_ops.py, before a push:
    allowed = await gate.request("git_push", {"branch": "main", "remote": "origin"})
    if not allowed:
        logger.info("git push cancelled by operator")
        return FritzGitResult(ok=False, operation="push", stderr="Cancelled by operator")

    # Proceed with the push...

Configuration
-------------
Add to OrchestratorConfig:
    confirm_before          : list[str]   — action names that require confirmation
    confirm_timeout_seconds : float       — seconds to wait before auto-approving (0 = wait forever)

Set via environment:
    TINKER_CONFIRM_BEFORE="git_push,artifact_delete"
    TINKER_CONFIRM_TIMEOUT=300
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .config import OrchestratorConfig
    from .state import OrchestratorState


class ConfirmationGate:
    """
    Pauses before irreversible actions and requests human approval.

    Parameters
    ----------
    config : OrchestratorConfig — reads confirm_before and confirm_timeout_seconds.
    state  : OrchestratorState  — pending confirmations are written here so the
             Dashboard can display and respond to them.
    """

    def __init__(
        self,
        config: "OrchestratorConfig",
        state: "OrchestratorState",
    ) -> None:
        self._config = config
        self._state = state
        # Pending asyncio Events — keyed by request_id.
        # When the Dashboard (or CLI) responds, it calls resolve(id, approved)
        # which sets the event.  The waiting coroutine then reads the decision
        # from state.pending_confirmations[id]["approved"].
        self._events: dict[str, asyncio.Event] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def request(
        self,
        action: str,
        details: dict | None = None,
        message: str = "",
    ) -> bool:
        """
        Ask the operator to approve or deny ``action``.

        If ``action`` is not in ``config.confirm_before``, returns True
        immediately (no pause, no interaction).

        Parameters
        ----------
        action  : Short name identifying the action (e.g. "git_push").
        details : Optional dict of action metadata to show the operator.
        message : Optional human-readable description of what will happen.

        Returns
        -------
        bool : True = approved (proceed), False = denied (cancel the action).
        """
        if action not in self._config.confirm_before:
            return True  # Not in the gate list — pass through immediately.

        request_id = str(uuid.uuid4())[:8]
        details = details or {}

        logger.info(
            "ConfirmationGate: action '%s' requires approval (request_id=%s)",
            action,
            request_id,
        )

        # Write the pending request into OrchestratorState so the Dashboard
        # can see it.
        pending = {
            "id": request_id,
            "action": action,
            "message": message or f"Tinker wants to perform: {action}",
            "details": details,
            "approved": None,  # None = pending, True/False = decided
        }
        self._state.pending_confirmations[request_id] = pending

        # Create the asyncio Event that resolve() will set.
        event = asyncio.Event()
        self._events[request_id] = event

        try:
            # Determine mode: if stdin is a TTY (real terminal), use CLI mode.
            # Otherwise use API-only mode (wait for Dashboard response).
            if sys.stdin.isatty():
                approved = await self._cli_prompt(request_id, action, details, message, event)
            else:
                approved = await self._api_wait(request_id, action, event)
        finally:
            # Always clean up, whether approved, denied, or timed out.
            self._events.pop(request_id, None)
            self._state.pending_confirmations.pop(request_id, None)

        verdict = "APPROVED" if approved else "DENIED"
        logger.info(
            "ConfirmationGate: action '%s' %s (request_id=%s)",
            action,
            verdict,
            request_id,
        )
        return approved

    def resolve(self, request_id: str, approved: bool) -> bool:
        """
        Called by the Dashboard API (or tests) to respond to a pending request.

        Parameters
        ----------
        request_id : The UUID returned with the pending confirmation.
        approved   : True = go ahead, False = cancel.

        Returns
        -------
        bool : True if the request_id was found and resolved, False if unknown.
        """
        if request_id not in self._state.pending_confirmations:
            logger.warning(
                "ConfirmationGate.resolve: unknown request_id '%s'", request_id
            )
            return False

        # Write the decision so the waiting coroutine can read it.
        self._state.pending_confirmations[request_id]["approved"] = approved

        # Fire the event to wake up the waiting coroutine.
        event = self._events.get(request_id)
        if event:
            event.set()
        return True

    def list_pending(self) -> list[dict]:
        """Return all currently pending confirmation requests."""
        return list(self._state.pending_confirmations.values())

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _cli_prompt(
        self,
        request_id: str,
        action: str,
        details: dict,
        message: str,
        event: asyncio.Event,
    ) -> bool:
        """
        Print a confirmation prompt to stdout and read y/n from stdin.

        Uses asyncio.to_thread so stdin reading doesn't block the event loop.
        Respects the timeout from config.confirm_timeout_seconds.
        """
        # Build the prompt message.
        prompt_lines = [
            "",
            "=" * 60,
            f"  TINKER CONFIRMATION REQUIRED  [id: {request_id}]",
            "=" * 60,
            f"  Action : {action}",
        ]
        if message:
            prompt_lines.append(f"  Details: {message}")
        for k, v in details.items():
            prompt_lines.append(f"  {k}: {v}")

        timeout = self._config.confirm_timeout_seconds
        if timeout > 0:
            prompt_lines.append(
                f"\n  Auto-approves in {timeout:.0f}s if no response."
            )
        prompt_lines.append("\n  Approve? [y/N] ")

        sys.stdout.write("\n".join(prompt_lines))
        sys.stdout.flush()

        def _read_stdin() -> str:
            try:
                return sys.stdin.readline().strip().lower()
            except Exception:
                return ""

        try:
            if timeout > 0:
                answer = await asyncio.wait_for(
                    asyncio.to_thread(_read_stdin),
                    timeout=timeout,
                )
            else:
                answer = await asyncio.to_thread(_read_stdin)
        except asyncio.TimeoutError:
            sys.stdout.write("\n  [Timed out — auto-approving]\n\n")
            sys.stdout.flush()
            return True  # auto-approve on timeout

        approved = answer in ("y", "yes")
        sys.stdout.write(f"\n  {'Approved.' if approved else 'Denied.'}\n\n")
        sys.stdout.flush()
        return approved

    async def _api_wait(
        self,
        request_id: str,
        action: str,
        event: asyncio.Event,
    ) -> bool:
        """
        Wait for the Dashboard to call resolve() via the API.

        Respects the timeout from config.confirm_timeout_seconds.
        Auto-approves on timeout (with a WARNING log).
        """
        timeout = self._config.confirm_timeout_seconds or None  # None = wait forever

        logger.info(
            "ConfirmationGate: waiting for API response for '%s' "
            "(timeout=%.0fs, request_id=%s)",
            action,
            self._config.confirm_timeout_seconds,
            request_id,
        )

        try:
            if timeout:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()
        except asyncio.TimeoutError:
            logger.warning(
                "ConfirmationGate: timeout waiting for '%s' (request_id=%s) — "
                "auto-approving",
                action,
                request_id,
            )
            return True  # auto-approve on timeout

        # Read the decision that resolve() wrote.
        entry = self._state.pending_confirmations.get(request_id, {})
        return bool(entry.get("approved", True))
