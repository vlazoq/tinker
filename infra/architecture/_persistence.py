"""Persistence mixin for ArchitectureStateManager."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .schema import ArchitectureState

logger = logging.getLogger(__name__)


class PersistenceMixin:
    """Disk I/O helpers: persist live state, archive snapshots, load history."""

    def _persist(self, state: ArchitectureState) -> None:
        """
        Write the state to the "live" JSON file (architecture_state.json).
        This overwrites the previous version — the live file always reflects
        the most recent state.
        """
        self._state_path.write_text(
            state.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _archive_snapshot(self, state: ArchitectureState) -> None:
        """
        Write a timestamped copy of the state to the history/ directory.
        This is separate from the live file so we can diff/rollback.

        Filename format: loop_0042_20240115T120045Z.json
          - loop_XXXX   : zero-padded loop number (so files sort correctly)
          - YYYYMMDDTHHMMSSZ : UTC timestamp in ISO 8601 "compact" format
        """
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        # :04d pads the loop number with leading zeros to 4 digits
        fn = f"loop_{state.macro_loop:04d}_{ts}.json"
        dst = self._hist_dir / fn
        dst.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _load_loop(self, loop: int) -> ArchitectureState:
        """
        Load a historical snapshot for a specific loop number.

        If multiple snapshots exist for the same loop (e.g. from retries),
        the most recent one (alphabetically last filename) is returned.

        Raises FileNotFoundError if no snapshot exists for the given loop.
        """
        # glob finds all files matching "loop_0042_*.json"
        candidates = sorted(self._hist_dir.glob(f"loop_{loop:04d}_*.json"))
        if not candidates:
            raise FileNotFoundError(f"No snapshot found for loop {loop}")
        # Take the last one (alphabetically latest = most recent timestamp)
        return ArchitectureState.model_validate_json(candidates[-1].read_text())
