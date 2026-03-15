"""
tinker/architecture_state/manager.py
──────────────────────────────────────
ArchitectureStateManager — the single public API for the Architecture State
subsystem.  Orchestrators interact only through this class.

Responsibilities
----------------
1. Hold the current ArchitectureState in memory.
2. Accept update payloads and produce new versions via the merger.
3. Persist each version to disk as JSON and auto-commit to Git.
4. Produce compressed text summaries for context assembly.
5. Produce human-readable diffs between any two versions.
6. Answer structured queries (low-confidence items, open questions, etc.).
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .merger import merge_update
from .schema import (
    ArchitectureState,
    Component,
    ConfidenceTier,
    DesignDecision,
    OpenQuestion,
)

logger = logging.getLogger(__name__)


class ArchitectureStateManager:
    """
    Manages the lifecycle of ArchitectureState documents.

    Parameters
    ----------
    workspace : Path | str
        Directory where state files and the Git repository live.
    system_name : str
        Human-readable name for the target system.
    auto_git : bool
        Whether to auto-commit on every update (set False for unit tests).
    """

    STATE_FILENAME = "architecture_state.json"
    HISTORY_DIR    = "history"

    def __init__(
        self,
        workspace: Path | str = "./tinker_workspace",
        system_name: str = "Unknown System",
        auto_git: bool = True,
    ) -> None:
        self.workspace   = Path(workspace)
        self.auto_git    = auto_git
        self._state_path = self.workspace / self.STATE_FILENAME
        self._hist_dir   = self.workspace / self.HISTORY_DIR

        self.workspace.mkdir(parents=True, exist_ok=True)
        self._hist_dir.mkdir(parents=True, exist_ok=True)

        if auto_git:
            self._ensure_git_repo()

        if self._state_path.exists():
            self._state = ArchitectureState.model_validate_json(
                self._state_path.read_text()
            )
            logger.info("Loaded existing state (loop %d)", self._state.macro_loop)
        else:
            self._state = ArchitectureState(system_name=system_name)
            logger.info("Initialised fresh state for '%s'", system_name)

    # ── Public Properties ────────────────────────────────────────────

    @property
    def state(self) -> ArchitectureState:
        return self._state

    @property
    def macro_loop(self) -> int:
        return self._state.macro_loop

    # ── Update / Merge ───────────────────────────────────────────────

    def apply_update(self, update: dict[str, Any]) -> ArchitectureState:
        """
        Merge an update payload into the current state, persist, and
        optionally commit to Git.  Returns the new state.
        """
        old_state   = self._state
        new_state   = merge_update(old_state, update)
        self._state = new_state

        self._persist(new_state)
        self._archive_snapshot(new_state)

        if self.auto_git:
            msg = self._build_commit_message(old_state, new_state, update)
            self._git_commit(msg)

        logger.info(
            "State updated → loop %d | components=%d decisions=%d questions=%d",
            new_state.macro_loop,
            len(new_state.components),
            len(new_state.decisions),
            len(new_state.open_questions),
        )
        return new_state

    # ── Persistence ──────────────────────────────────────────────────

    def _persist(self, state: ArchitectureState) -> None:
        self._state_path.write_text(
            state.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _archive_snapshot(self, state: ArchitectureState) -> None:
        ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fn  = f"loop_{state.macro_loop:04d}_{ts}.json"
        dst = self._hist_dir / fn
        dst.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    # ── Git ──────────────────────────────────────────────────────────

    def _ensure_git_repo(self) -> None:
        git_dir = self.workspace / ".git"
        if not git_dir.exists():
            self._run_git("init")
            self._run_git("config", "user.email", "tinker@local")
            self._run_git("config", "user.name", "Tinker")
            logger.info("Initialised Git repo at %s", self.workspace)

    def _git_commit(self, message: str) -> None:
        try:
            self._run_git("add", "--all")
            self._run_git("commit", "-m", message)
        except subprocess.CalledProcessError as exc:
            if "nothing to commit" not in (exc.output or ""):
                logger.warning("Git commit failed: %s", exc)

    def _run_git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            raise subprocess.CalledProcessError(
                result.returncode, args, result.stdout, result.stderr
            )
        return result.stdout.strip()

    @staticmethod
    def _build_commit_message(
        old: ArchitectureState,
        new: ArchitectureState,
        update: dict,
    ) -> str:
        loop      = new.macro_loop
        new_comps = len(new.components)   - len(old.components)
        new_decs  = len(new.decisions)    - len(old.decisions)
        new_qs    = len(new.open_questions) - len(old.open_questions)
        conf      = new.overall_confidence.value

        parts   = [f"loop {loop:04d}: arch-state update"]
        details = []
        if new_comps > 0:
            details.append(f"+{new_comps} component(s)")
        if new_decs > 0:
            details.append(f"+{new_decs} decision(s)")
        if new_qs > 0:
            details.append(f"+{new_qs} question(s)")
        if details:
            parts.append(", ".join(details))
        parts.append(f"overall_confidence={conf:.2f}")
        if update.get("loop_note"):
            parts.append(update["loop_note"][:120])

        return " | ".join(parts)

    # ── Summariser ───────────────────────────────────────────────────

    def summarise(self, budget_tokens: int = 800) -> str:
        """
        Produce a compressed plain-text summary of the current state,
        sized to fit within *budget_tokens* (approximated as chars/4).
        Suitable for injecting into an LLM context window.
        """
        s    = self._state
        char_budget = budget_tokens * 4
        lines: list[str] = []

        lines.append(f"=== Architecture State: {s.system_name} (loop {s.macro_loop}) ===")
        lines.append(f"Purpose : {s.system_purpose or '(not set)'}")
        lines.append(f"Scope   : {s.system_scope or '(not set)'}")
        tier = s.overall_confidence.tier.value
        lines.append(f"Confidence: {s.overall_confidence.value:.2f} [{tier}]")
        lines.append("")

        # Components
        comps = sorted(s.components.values(), key=lambda c: -c.confidence.value)
        lines.append(f"── Components ({len(comps)}) ──")
        for c in comps:
            resps = "; ".join(c.responsibilities[:3])
            lines.append(
                f"  [{c.confidence.value:.2f}] {c.name}"
                + (f" — {resps}" if resps else "")
            )
        lines.append("")

        # Relationships (capped)
        if s.relationships:
            lines.append(f"── Relationships ({len(s.relationships)}) ──")
            id_to_name = {cid: c.name for cid, c in s.components.items()}
            for r in list(s.relationships.values())[:10]:
                src = id_to_name.get(r.source_id, r.source_id)
                tgt = id_to_name.get(r.target_id, r.target_id)
                lines.append(f"  {src} --[{r.kind}]--> {tgt}"
                             + (f" ({r.description})" if r.description else ""))
            if len(s.relationships) > 10:
                lines.append(f"  … +{len(s.relationships)-10} more")
            lines.append("")

        # Decisions
        if s.decisions:
            lines.append(f"── Design Decisions ({len(s.decisions)}) ──")
            for d in sorted(s.decisions.values(), key=lambda x: -x.confidence.value)[:8]:
                lines.append(f"  [{d.status} {d.confidence.value:.2f}] {d.title}")
            lines.append("")

        # Open questions
        unresolved = s.unresolved_questions()[:5]
        if unresolved:
            lines.append(f"── Open Questions (top {len(unresolved)}) ──")
            for q in unresolved:
                lines.append(f"  [priority={q.priority:.1f}] {q.question}")
            lines.append("")

        # Recent notes
        for n in s.loop_notes[-3:]:
            lines.append(f"NOTE: {n}")

        text = "\n".join(lines)
        if len(text) > char_budget:
            text = text[:char_budget] + "\n… [truncated for context budget]"
        return text

    # ── Diff ─────────────────────────────────────────────────────────

    def diff(
        self,
        loop_a: int | None = None,
        loop_b: int | None = None,
    ) -> str:
        """
        Human-readable diff between two loop versions.
        Defaults: loop_a = current loop - 1, loop_b = current loop.
        """
        state_b = self._state if loop_b is None else self._load_loop(loop_b)
        if loop_a is not None:
            state_a = self._load_loop(loop_a)
        elif state_b.macro_loop > 0:
            state_a = self._load_loop(state_b.macro_loop - 1)
        else:
            state_a = state_b
        return _diff_states(state_a, state_b)

    def _load_loop(self, loop: int) -> ArchitectureState:
        candidates = sorted(self._hist_dir.glob(f"loop_{loop:04d}_*.json"))
        if not candidates:
            raise FileNotFoundError(f"No snapshot found for loop {loop}")
        return ArchitectureState.model_validate_json(candidates[-1].read_text())

    def list_snapshots(self) -> list[dict]:
        result = []
        for p in sorted(self._hist_dir.glob("loop_*_*.json")):
            try:
                s = ArchitectureState.model_validate_json(p.read_text())
                result.append({
                    "file": p.name,
                    "loop": s.macro_loop,
                    "components": len(s.components),
                    "decisions": len(s.decisions),
                    "confidence": round(s.overall_confidence.value, 3),
                    "updated_at": s.updated_at,
                })
            except Exception:
                pass
        return result

    # ── Query Methods ────────────────────────────────────────────────

    def low_confidence_components(self, threshold: float = 0.5) -> list[Component]:
        """Return components below the confidence threshold, sorted asc."""
        return self._state.low_confidence_components(threshold)

    def unresolved_questions(self) -> list[OpenQuestion]:
        """Return unresolved open questions sorted by priority desc."""
        return self._state.unresolved_questions()

    def decisions_for_subsystem(self, subsystem: str) -> list[DesignDecision]:
        return self._state.decisions_for_subsystem(subsystem)

    def speculative_decisions(self) -> list[DesignDecision]:
        return [d for d in self._state.decisions.values()
                if d.confidence.tier == ConfidenceTier.SPECULATIVE]

    def components_by_subsystem(self, subsystem: str) -> list[Component]:
        return [c for c in self._state.components.values()
                if c.subsystem and c.subsystem.lower() == subsystem.lower()]

    def confidence_map(self) -> dict[str, float]:
        """Flat map of {kind:name → confidence} for all tracked items."""
        result: dict[str, float] = {}
        for c in self._state.components.values():
            result[f"component:{c.name}"] = round(c.confidence.value, 4)
        for d in self._state.decisions.values():
            result[f"decision:{d.title}"] = round(d.confidence.value, 4)
        for k, sub in self._state.subsystems.items():
            if isinstance(sub, dict):
                name = sub.get("name", k)
                conf = sub.get("confidence", {}).get("value", 0.5)
            else:
                name, conf = sub.name, sub.confidence.value
            result[f"subsystem:{name}"] = round(conf, 4)
        return result


# ──────────────────────────────────────────────
# Diff helper (module-level)
# ──────────────────────────────────────────────

def _diff_states(a: ArchitectureState, b: ArchitectureState) -> str:
    lines: list[str] = []
    lines.append(
        f"=== Diff: loop {a.macro_loop} → loop {b.macro_loop} ({a.system_name}) ==="
    )

    dc   = b.overall_confidence.value - a.overall_confidence.value
    sign = "+" if dc >= 0 else ""
    lines.append(
        f"\nOverall confidence: {a.overall_confidence.value:.3f} → "
        f"{b.overall_confidence.value:.3f}  ({sign}{dc:.3f})"
    )

    # Components
    a_names = {c.name for c in a.components.values()}
    b_names = {c.name for c in b.components.values()}
    c_added   = b_names - a_names
    c_removed = a_names - b_names
    c_changed = []
    for name in a_names & b_names:
        ca = next(c for c in a.components.values() if c.name == name)
        cb = next(c for c in b.components.values() if c.name == name)
        delta = cb.confidence.value - ca.confidence.value
        if abs(delta) > 0.005 or ca.description != cb.description:
            c_changed.append((name, ca.confidence.value, cb.confidence.value, delta))

    if c_added or c_removed or c_changed:
        lines.append("\n── Components ──")
        for n in sorted(c_added):
            lines.append(f"  [+] ADDED    {n}")
        for n in sorted(c_removed):
            lines.append(f"  [-] REMOVED  {n}")
        for name, oc, nc, delta in sorted(c_changed, key=lambda x: -abs(x[3])):
            arrow = "↑" if delta > 0 else "↓"
            lines.append(f"  [~] UPDATED  {name}  {oc:.3f} → {nc:.3f} {arrow}")

    # Decisions
    a_dt = {d.title for d in a.decisions.values()}
    b_dt = {d.title for d in b.decisions.values()}
    d_added   = b_dt - a_dt
    d_removed = a_dt - b_dt
    d_changed = []
    for title in a_dt & b_dt:
        da = next(d for d in a.decisions.values() if d.title == title)
        db = next(d for d in b.decisions.values() if d.title == title)
        delta = db.confidence.value - da.confidence.value
        if da.status != db.status or abs(delta) > 0.005:
            d_changed.append((title, da.status, db.status,
                               da.confidence.value, db.confidence.value))

    if d_added or d_removed or d_changed:
        lines.append("\n── Design Decisions ──")
        for t in sorted(d_added):
            lines.append(f"  [+] ADDED    {t}")
        for t in sorted(d_removed):
            lines.append(f"  [-] REMOVED  {t}")
        for title, sa, sb, ca, cb in d_changed:
            arrow  = "↑" if cb > ca else "↓"
            status = f"  [{sa}→{sb}]" if sa != sb else ""
            lines.append(f"  [~] UPDATED  {title}  {ca:.3f}→{cb:.3f} {arrow}{status}")

    # Questions
    a_qs = {q.question for q in a.open_questions.values()}
    b_qs = {q.question for q in b.open_questions.values()}
    q_new = b_qs - a_qs
    q_resolved = [
        q.question for q in b.open_questions.values()
        if (aq := a.question_by_text(q.question)) and not aq.resolved and q.resolved
    ]

    if q_new or q_resolved:
        lines.append("\n── Questions ──")
        for q in sorted(q_new):
            lines.append(f"  [+] NEW      {q}")
        for q in sorted(q_resolved):
            lines.append(f"  [✓] RESOLVED {q}")

    # New loop notes
    new_notes = b.loop_notes[len(a.loop_notes):]
    if new_notes:
        lines.append("\n── New Loop Notes ──")
        for n in new_notes:
            lines.append(f"  {n}")

    if len(lines) <= 3:
        lines.append("\n(no significant changes detected)")

    return "\n".join(lines)
