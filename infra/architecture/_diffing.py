"""Diffing mixin and helper for ArchitectureStateManager."""

from __future__ import annotations

import logging

from .schema import ArchitectureState

logger = logging.getLogger(__name__)


def _diff_states(a: ArchitectureState, b: ArchitectureState) -> str:
    """
    Compare two ArchitectureState versions and produce a human-readable
    text diff showing what was added, removed, or updated between them.

    This function is private (underscore prefix) and is called by
    `ArchitectureStateManager.diff()`.  It's at module level (not inside
    the class) because it doesn't need any class state — it's a pure function
    that only needs the two states to compare.

    The output uses these prefixes:
      [+] ADDED    — a new item exists in b that wasn't in a
      [-] REMOVED  — an item in a no longer exists in b (rare, by design)
      [~] UPDATED  — an item exists in both but changed between a and b
      [✓] RESOLVED — a question that was open in a was answered in b

    Parameters
    ----------
    a : The "before" state (older loop).
    b : The "after"  state (newer loop).

    Returns
    -------
    A multi-line string suitable for printing to the console or logging.
    """
    lines: list[str] = []
    lines.append(
        f"=== Diff: loop {a.macro_loop} → loop {b.macro_loop} ({a.system_name}) ==="
    )

    # Overall confidence change — shows whether the AI is becoming more or less certain
    dc = b.overall_confidence.value - a.overall_confidence.value
    sign = "+" if dc >= 0 else ""  # include "+" prefix for positive changes
    lines.append(
        f"\nOverall confidence: {a.overall_confidence.value:.3f} → "
        f"{b.overall_confidence.value:.3f}  ({sign}{dc:.3f})"
    )

    # ── Compare Components ──────────────────────────────────────────
    # Set arithmetic: b_names - a_names = newly added components
    #                 a_names - b_names = removed components
    #                 a_names & b_names = components present in both (may be updated)
    a_names = {c.name for c in a.components.values()}
    b_names = {c.name for c in b.components.values()}
    c_added = b_names - a_names  # in b but not in a → newly added
    c_removed = a_names - b_names  # in a but not in b → removed
    c_changed = []
    for name in a_names & b_names:  # in both → check for changes
        ca = next(c for c in a.components.values() if c.name == name)
        cb = next(c for c in b.components.values() if c.name == name)
        delta = cb.confidence.value - ca.confidence.value
        # Only report as "changed" if the confidence shifted noticeably (> 0.5%)
        # OR if the description text actually changed
        if abs(delta) > 0.005 or ca.description != cb.description:
            c_changed.append((name, ca.confidence.value, cb.confidence.value, delta))

    if c_added or c_removed or c_changed:
        lines.append("\n── Components ──")
        for n in sorted(c_added):
            lines.append(f"  [+] ADDED    {n}")
        for n in sorted(c_removed):
            lines.append(f"  [-] REMOVED  {n}")
        # Sort changed components by the absolute size of the confidence shift
        # so the most significant changes appear first
        for name, oc, nc, delta in sorted(c_changed, key=lambda x: -abs(x[3])):
            arrow = "↑" if delta > 0 else "↓"
            lines.append(f"  [~] UPDATED  {name}  {oc:.3f} → {nc:.3f} {arrow}")

    # ── Compare Design Decisions ────────────────────────────────────
    a_dt = {d.title for d in a.decisions.values()}
    b_dt = {d.title for d in b.decisions.values()}
    d_added = b_dt - a_dt
    d_removed = a_dt - b_dt
    d_changed = []
    for title in a_dt & b_dt:
        da = next(d for d in a.decisions.values() if d.title == title)
        db = next(d for d in b.decisions.values() if d.title == title)
        delta = db.confidence.value - da.confidence.value
        # Report if status changed (e.g. proposed → accepted) OR confidence shifted
        if da.status != db.status or abs(delta) > 0.005:
            d_changed.append(
                (title, da.status, db.status, da.confidence.value, db.confidence.value)
            )

    if d_added or d_removed or d_changed:
        lines.append("\n── Design Decisions ──")
        for t in sorted(d_added):
            lines.append(f"  [+] ADDED    {t}")
        for t in sorted(d_removed):
            lines.append(f"  [-] REMOVED  {t}")
        for title, sa, sb, ca, cb in d_changed:
            arrow = "↑" if cb > ca else "↓"
            # Show status transition only if it changed (e.g. [proposed→accepted])
            status = f"  [{sa}→{sb}]" if sa != sb else ""
            lines.append(f"  [~] UPDATED  {title}  {ca:.3f}→{cb:.3f} {arrow}{status}")

    # ── Compare Open Questions ──────────────────────────────────────
    a_qs = {q.question for q in a.open_questions.values()}
    b_qs = {q.question for q in b.open_questions.values()}
    q_new = b_qs - a_qs  # questions raised in b that didn't exist in a

    # Find questions that were open in a but marked resolved in b.
    # The walrus operator `:=` (Python 3.8+) assigns and tests in one expression:
    #   (aq := a.question_by_text(q.question))   ← assigns aq AND checks it's not None
    q_resolved = [
        q.question
        for q in b.open_questions.values()
        if (aq := a.question_by_text(q.question)) and not aq.resolved and q.resolved
    ]

    if q_new or q_resolved:
        lines.append("\n── Questions ──")
        for q in sorted(q_new):
            lines.append(f"  [+] NEW      {q}")
        for q in sorted(q_resolved):
            lines.append(f"  [✓] RESOLVED {q}")

    # ── New Loop Notes ──────────────────────────────────────────────
    # Notes are append-only, so anything beyond a's length is new in b
    new_notes = b.loop_notes[len(a.loop_notes) :]
    if new_notes:
        lines.append("\n── New Loop Notes ──")
        for n in new_notes:
            lines.append(f"  {n}")

    # If we only produced the header (≤ 3 lines total), nothing meaningful changed
    if len(lines) <= 3:
        lines.append("\n(no significant changes detected)")

    return "\n".join(lines)


class DiffingMixin:
    """Diff, rollback, and snapshot listing capabilities."""

    def diff(
        self,
        loop_a: int | None = None,
        loop_b: int | None = None,
    ) -> str:
        """
        Produce a human-readable diff between two loop versions.

        Defaults:
          - loop_b defaults to the current (most recent) state.
          - loop_a defaults to loop_b - 1 (the previous loop).

        So calling `mgr.diff()` with no arguments shows "what changed in
        the most recent update?" — the most common use case.

        Parameters
        ----------
        loop_a : The "before" loop number.  None → previous loop.
        loop_b : The "after"  loop number.  None → current state.

        Returns
        -------
        A multi-line string showing added, removed, and changed items.
        """
        # Resolve loop_b — use current in-memory state, or load from history
        state_b = self._state if loop_b is None else self._load_loop(loop_b)

        # Resolve loop_a — use the loop before state_b, or load explicitly
        if loop_a is not None:
            state_a = self._load_loop(loop_a)
        elif state_b.macro_loop > 0:
            # Load the snapshot from one loop before state_b
            state_a = self._load_loop(state_b.macro_loop - 1)
        else:
            # If we're on loop 0 there's nothing before — diff against self
            state_a = state_b

        return _diff_states(state_a, state_b)

    def rollback(self, n: int = 1) -> ArchitectureState:
        """
        Restore the architecture state to ``n`` snapshots ago.

        Useful when a macro loop produces a bad update and you want to undo it.
        The rolled-back state is written as the new live file *and* added to
        the history archive (so the rollback itself is part of the audit trail).
        If ``auto_git`` is enabled, the restoration is committed to Git.

        Parameters
        ----------
        n : How many snapshots to step back.  1 (default) restores the previous
            state; 2 restores the one before that, and so on.

        Returns
        -------
        The restored ArchitectureState.

        Raises
        ------
        ValueError : If there are fewer than ``n + 1`` snapshots available
                     (i.e. there is no snapshot to roll back to).
        """
        snapshots = sorted(self._hist_dir.glob("loop_*_*.json"))
        # We need at least n+1 snapshots: the current one plus n predecessors.
        if len(snapshots) <= n:
            raise ValueError(
                f"Cannot roll back {n} step(s): only {len(snapshots)} snapshot(s) exist"
            )
        # The most recent snapshot is snapshots[-1]; n steps back is snapshots[-(n+1)].
        target_path = snapshots[-(n + 1)]
        state = ArchitectureState.model_validate_json(target_path.read_text())
        self._state = state

        # Write the restored state as both the live file and a new archive entry
        # so the rollback appears as a distinct event in the history directory.
        self._persist(state)
        self._archive_snapshot(state)

        if self.auto_git:
            self._git_commit(
                f"rollback(n={n}): restored loop {state.macro_loop} "
                f"from {target_path.name}"
            )

        logger.info(
            "Rolled back %d step(s) to loop %d (source: %s)",
            n,
            state.macro_loop,
            target_path.name,
        )
        return state

    def list_snapshots(self) -> list[dict]:
        """
        Return a summary list of all historical snapshots in the history/ directory.

        Each entry is a dict with keys: file, loop, components, decisions,
        confidence, updated_at.  Useful for building a timeline or for
        debugging which loops produced significant changes.

        Silently skips any files that can't be parsed (e.g. corrupted JSON).
        """
        result = []
        for p in sorted(self._hist_dir.glob("loop_*_*.json")):
            try:
                s = ArchitectureState.model_validate_json(p.read_text())
                result.append(
                    {
                        "file": p.name,
                        "loop": s.macro_loop,
                        "components": len(s.components),
                        "decisions": len(s.decisions),
                        "confidence": round(s.overall_confidence.value, 3),
                        "updated_at": s.updated_at,
                    }
                )
            except Exception:
                # Skip files we can't parse rather than crashing the whole listing
                pass
        return result
