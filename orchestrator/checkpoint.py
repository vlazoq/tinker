"""
orchestrator/checkpoint.py
==========================
CheckpointManager — save and restore mid-run orchestrator state.

What problem does this solve?
------------------------------
Tinker's orchestrator is designed to run for hours or days.  During a long run,
several things can interrupt it:

  * Power failure on the home lab server.
  * OOM kill from the OS when another process needs memory.
  * Ctrl-C from the operator who needs to do something else and will come back.
  * A deliberate pause requested via the Dashboard (see orchestrator/confirmation.py).

Before this module, any interruption meant starting from scratch: the task that
was in progress when the process died would be partially completed (Architect
called, but result not stored) and would need to be retried from the top.

The CheckpointManager solves this by periodically serializing the "in progress"
state to a JSON file on disk.  When Tinker starts up and finds a checkpoint
file, it restores the state and resumes where it left off.

What gets checkpointed?
------------------------
Not everything — just what is needed to resume without repeating work:

  current_task       : The task being processed (so we don't re-select a different one).
  subsystem_counts   : Per-subsystem micro loop counters (for meso trigger logic).
  micro_history_tail : The last 10 micro loop records (for context + stagnation detection).
  architect_result   : If the Architect already ran this loop, we skip calling it again.
  critic_iterations  : How many Critic refinement loops already happened.

What does NOT get checkpointed:
  * The task queue itself (it's in SQLite and survives restarts).
  * Memory/artifacts (they're in DuckDB/ChromaDB and survive restarts).
  * The architecture state (it's in git and survives restarts).

These durable stores mean the only "lost" work on an unclean restart is the
current micro loop's Architect/Critic calls — which the checkpoint eliminates.

Pause vs Stop
-------------
Checkpointing also enables a true "pause" operation (distinct from stop):

  pause: save checkpoint → suspend the asyncio event loop → process stays alive
  resume: read the already-in-memory checkpoint → clear pause event → continue

  stop:  save checkpoint → exit process
  restart with resume: load checkpoint from disk → restore state → continue

Both use the same CheckpointManager.  The difference is whether the process
exits or just waits.

File format
-----------
Plain JSON, atomically written (temp file + rename, same pattern as state snapshots).

    {
      "version": 1,
      "created_at": "2025-01-01T12:00:00+00:00",
      "micro_iteration": 42,
      "current_task": {"id": "...", "description": "..."},
      "subsystem_counts": {"api_gateway": 3, "auth_service": 1},
      "micro_history_tail": [...],
      "architect_result": {"content": "...", "tokens_used": 1234},
      "critic_iterations_done": 0
    }

Usage
-----
In main.py::

    from orchestrator.checkpoint import CheckpointManager
    checkpoint_mgr = CheckpointManager(config.checkpoint_path)

    # On startup: check for a prior checkpoint
    prior = checkpoint_mgr.load()
    if prior:
        logger.info("Resuming from checkpoint at iteration %d", prior["micro_iteration"])

    # Pass to Orchestrator so micro_loop can write checkpoints
    orch = Orchestrator(..., checkpoint_manager=checkpoint_mgr)

In orchestrator.py::

    def pause(self):
        self.checkpoint_manager.save(self._build_checkpoint())
        self._pause_event.set()

    async def resume(self):
        self._pause_event.clear()
        self.checkpoint_manager.clear()
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Increment this when the checkpoint format changes in a backwards-incompatible
# way.  On load, we check this and discard incompatible checkpoints rather than
# crashing on a KeyError.
_CHECKPOINT_VERSION = 1


class CheckpointManager:
    """
    Saves and restores orchestrator mid-run state.

    Parameters
    ----------
    path    : File path for the checkpoint JSON (e.g. "./tinker_checkpoint.json").
    enabled : If False, save() and load() are no-ops.  Useful for disabling
              checkpointing in tests without changing the calling code.
    """

    def __init__(self, path: str, enabled: bool = True) -> None:
        self._path = path
        self._enabled = enabled
        # In-memory copy of the most recently saved checkpoint.
        # Used when pausing: the checkpoint is already in memory, so we can
        # resume without reading from disk.
        self._in_memory: Optional[dict] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def save(self, data: dict) -> None:
        """
        Atomically write a checkpoint to disk.

        Parameters
        ----------
        data : Dict containing the checkpoint payload (see module docstring
               for the expected keys).  The "version" and "created_at" keys
               are added automatically if not present.
        """
        if not self._enabled:
            return

        checkpoint = {
            "version": _CHECKPOINT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self._in_memory = checkpoint
        self._atomic_write(checkpoint)
        logger.debug(
            "Checkpoint saved: iteration=%s, task=%s",
            data.get("micro_iteration"),
            data.get("current_task", {}).get("id", "?")[:8] if data.get("current_task") else "none",
        )

    def load(self) -> Optional[dict]:
        """
        Load a checkpoint from disk, if one exists.

        Returns
        -------
        dict if a valid checkpoint was found, None otherwise.

        A checkpoint is considered invalid (and discarded with a warning) if:
          * The file doesn't exist.
          * The file contains invalid JSON.
          * The version number doesn't match _CHECKPOINT_VERSION.
        """
        if not self._enabled:
            return None

        if self._in_memory is not None:
            # Already loaded this session (e.g. after a pause/resume cycle).
            return self._in_memory

        path = Path(self._path)
        if not path.exists():
            return None

        try:
            raw = path.read_text()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Checkpoint at %s is unreadable (%s) — starting fresh",
                self._path,
                exc,
            )
            self._try_delete()
            return None

        if data.get("version") != _CHECKPOINT_VERSION:
            logger.warning(
                "Checkpoint version mismatch (file=%s, expected=%s) — discarding",
                data.get("version"),
                _CHECKPOINT_VERSION,
            )
            self._try_delete()
            return None

        age_s = time.time() - _parse_iso(data.get("created_at", ""))
        logger.info(
            "Checkpoint loaded: iteration=%s, age=%.0fs, task=%s",
            data.get("micro_iteration"),
            age_s,
            data.get("current_task", {}).get("id", "?")[:8] if data.get("current_task") else "none",
        )
        self._in_memory = data
        return data

    def clear(self) -> None:
        """
        Delete the checkpoint file and clear the in-memory copy.

        Called after a successful resume (no need to keep the checkpoint around)
        or after a clean shutdown (the next run should start fresh).
        """
        self._in_memory = None
        self._try_delete()
        logger.debug("Checkpoint cleared")

    def exists(self) -> bool:
        """Return True if a checkpoint file exists on disk."""
        return Path(self._path).exists()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _atomic_write(self, data: dict) -> None:
        """Write data to self._path using the temp-file-then-rename pattern."""
        json_str = json.dumps(data, indent=2, default=str)
        dir_ = os.path.dirname(self._path) or "."
        os.makedirs(dir_, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json_str)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _try_delete(self) -> None:
        """Delete the checkpoint file, ignoring errors if it doesn't exist."""
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete checkpoint file %s: %s", self._path, exc)


def _parse_iso(iso_str: str) -> float:
    """Parse an ISO 8601 timestamp string to a Unix timestamp float."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0
