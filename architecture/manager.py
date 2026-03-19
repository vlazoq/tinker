"""
tinker/architecture/manager.py
================================

What this file does
--------------------
This file is the "front door" for the entire architecture package.  Everything
the rest of Tinker needs to do with architecture state goes through the
`ArchitectureStateManager` class defined here.

Think of it as a combination of:
  - A librarian (loads the document from disk, hands it out, puts it back)
  - A scribe (applies new knowledge via the merger, saves updated versions)
  - A historian (keeps a timestamped archive of every version in a history folder)
  - A summariser (compresses the document to fit inside an LLM context window)
  - A Git wrapper (optionally commits each version to a Git repository)

Typical usage by the orchestrator
-----------------------------------
    from architecture import ArchitectureStateManager

    # Step 1: Create a manager (loads existing state from disk if it exists)
    mgr = ArchitectureStateManager(workspace="./tinker_workspace")

    # Step 2: After an AI loop produces output, apply its update
    mgr.apply_update({
        "loop_note": "Identified the API Gateway component",
        "components": [
            {"name": "API Gateway", "description": "Routes all external traffic",
             "responsibilities": ["Rate limiting", "Auth token validation"],
             "confidence_value": 0.75}
        ]
    })

    # Step 3: Get a compressed summary to inject into the next AI prompt
    context_text = mgr.summarise(budget_tokens=800)

    # Step 4: See what changed since the last loop
    print(mgr.diff())

File layout on disk
--------------------
    workspace/
    ├── architecture_state.json     ← the "live" current version
    └── history/
        ├── loop_0001_20240115T120000Z.json
        ├── loop_0002_20240115T120045Z.json
        └── ...                     ← one snapshot per apply_update() call

The "live" file is always the latest version.  The history folder keeps
every previous version so you can diff, roll back, or analyse progress.

Git integration
----------------
When `auto_git=True` (the default), the manager will:
1. Initialise a Git repository in the workspace folder if one doesn't exist.
2. After each update, run `git add --all` and `git commit` with a
   descriptive message showing what changed.

This gives you a complete version history viewable with `git log`.
Set `auto_git=False` for unit tests or environments without Git.

Responsibilities
-----------------
1. Hold the current ArchitectureState in memory.
2. Accept update payloads and produce new versions via the merger.
3. Persist each version to disk as JSON and auto-commit to Git.
4. Produce compressed text summaries for context assembly.
5. Produce human-readable diffs between any two versions.
6. Answer structured queries (low-confidence items, open questions, etc.).
"""

from __future__ import annotations

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

# Module-level logger.  Log messages from this file will be labelled
# with the module path, e.g. "tinker.architecture.manager".
logger = logging.getLogger(__name__)


class ArchitectureStateManager:
    """
    Manages the full lifecycle of an ArchitectureState document.

    This is the only class outside callers need to interact with.  All the
    schema types, merger logic, and JSON serialisation are internal details.

    Parameters
    ----------
    workspace : Path | str
        Path to the directory where state files and Git history are stored.
        Created automatically if it doesn't exist.
        Default: "./tinker_workspace"
    system_name : str
        Human-readable name for the system being designed.  Only used when
        creating a brand-new state (ignored if one already exists on disk).
        Default: "Unknown System"
    auto_git : bool
        If True, automatically initialise a Git repo in the workspace and
        commit after every `apply_update()` call.
        Set False for unit tests or read-only usage.
        Default: True

    Attributes (public)
    -------------------
    workspace  : The resolved Path to the workspace directory.
    auto_git   : Whether Git commits are enabled.
    state      : Property — returns the current in-memory ArchitectureState.
    macro_loop : Property — shortcut for state.macro_loop.
    """

    # The name of the "live" current-state file (always in workspace root)
    STATE_FILENAME = "architecture_state.json"
    # The subdirectory where historical snapshots are stored
    HISTORY_DIR = "history"

    def __init__(
        self,
        workspace: Path | str = "./tinker_workspace",
        system_name: str = "Unknown System",
        auto_git: bool = True,
    ) -> None:
        self.workspace = Path(workspace)
        self.auto_git = auto_git

        # Build the full paths for convenience
        self._state_path = self.workspace / self.STATE_FILENAME
        self._hist_dir = self.workspace / self.HISTORY_DIR

        # Create the workspace and history directories if they don't exist yet.
        # parents=True means it will create intermediate directories too.
        # exist_ok=True means it won't error if the directory already exists.
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._hist_dir.mkdir(parents=True, exist_ok=True)

        # Initialise a Git repo in the workspace if auto_git is on
        if auto_git:
            self._ensure_git_repo()

        # Load existing state from disk, or create a fresh one
        if self._state_path.exists():
            # There's an existing state file — load it
            self._state = ArchitectureState.model_validate_json(
                self._state_path.read_text()
            )
            logger.info("Loaded existing state (loop %d)", self._state.macro_loop)
        else:
            # First run — create a blank state document
            self._state = ArchitectureState(system_name=system_name)
            logger.info("Initialised fresh state for '%s'", system_name)

    # ── Public Properties ────────────────────────────────────────────

    @property
    def state(self) -> ArchitectureState:
        """The current in-memory ArchitectureState (read-only view)."""
        return self._state

    @property
    def macro_loop(self) -> int:
        """Convenience shortcut for state.macro_loop."""
        return self._state.macro_loop

    # ── Update / Merge ───────────────────────────────────────────────

    def apply_update(self, update: dict[str, Any]) -> ArchitectureState:
        """
        Merge a new update payload into the current state, save it to disk,
        and optionally commit to Git.  Returns the newly produced state.

        This is the main method the orchestrator calls after each AI loop.

        Parameters
        ----------
        update : A plain dict of new information from the AI.  See merger.py
                 for the full list of supported keys.

        Returns
        -------
        The new ArchitectureState after the merge.

        Side effects
        -----------
        - Updates self._state to the new version.
        - Writes the new state to architecture_state.json.
        - Writes a timestamped snapshot to the history/ directory.
        - If auto_git is True, runs git add + git commit.
        """
        old_state = self._state
        # The merger produces a brand-new state object — nothing is mutated
        new_state = merge_update(old_state, update)
        self._state = new_state  # update our in-memory reference

        # Save the new state in both the "live" file and the history archive
        self._persist(new_state)
        self._archive_snapshot(new_state)

        # Optionally commit all changes to Git
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
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # :04d pads the loop number with leading zeros to 4 digits
        fn = f"loop_{state.macro_loop:04d}_{ts}.json"
        dst = self._hist_dir / fn
        dst.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    # ── Git ──────────────────────────────────────────────────────────

    def _ensure_git_repo(self) -> None:
        """
        Initialise a Git repository in the workspace if one doesn't exist.
        Also sets a local git user identity (required for commits to work)
        using non-real values (tinker@local / Tinker) since this is all local.
        """
        git_dir = self.workspace / ".git"
        if not git_dir.exists():
            self._run_git("init")
            # Git requires user.email and user.name to make commits.
            # We use dummy values since this is just a local history tool.
            self._run_git("config", "user.email", "tinker@local")
            self._run_git("config", "user.name", "Tinker")
            logger.info("Initialised Git repo at %s", self.workspace)

    def _git_commit(self, message: str) -> None:
        """
        Stage all changed files and make a Git commit with the given message.
        Silently ignores the "nothing to commit" case (which happens if the
        state didn't actually change between two updates).
        """
        try:
            self._run_git("add", "--all")  # stage everything in the workspace
            self._run_git("commit", "-m", message)
        except subprocess.CalledProcessError as exc:
            # "nothing to commit" is not a real error — it just means no files changed
            if "nothing to commit" not in (exc.output or ""):
                logger.warning("Git commit failed: %s", exc)

    def _run_git(self, *args: str) -> str:
        """
        Run a git command in the workspace directory and return its stdout.
        Raises subprocess.CalledProcessError on failure (unless the output
        contains "nothing to commit", which we treat as success).

        Parameters
        ----------
        *args : The git subcommand and arguments, e.g. ("commit", "-m", "msg").

        Returns
        -------
        The stripped stdout text from the git command.
        """
        result = subprocess.run(
            ["git", *args],  # ["git", "commit", "-m", "msg", ...]
            cwd=self.workspace,  # run the command inside the workspace folder
            capture_output=True,  # capture stdout and stderr instead of printing
            text=True,  # return strings instead of bytes
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
        """
        Build a descriptive Git commit message summarising what changed.

        The message format is:
            loop 0042: arch-state update | +2 component(s), +1 decision(s) |
            overall_confidence=0.72 | [optional loop note snippet]

        Why this format?
        - `git log --oneline` shows you a clear timeline of AI progress.
        - The loop number tells you exactly which iteration produced the change.
        - The counts show at a glance how much new information was added.

        Parameters
        ----------
        old    : The state BEFORE the update (used to calculate "what's new").
        new    : The state AFTER the update.
        update : The raw update dict (used to extract the loop_note).
        """
        loop = new.macro_loop
        # Count how many items were ADDED in each collection
        new_comps = len(new.components) - len(old.components)
        new_decs = len(new.decisions) - len(old.decisions)
        new_qs = len(new.open_questions) - len(old.open_questions)
        conf = new.overall_confidence.value

        # Build the message by assembling pipe-separated parts
        parts = [f"loop {loop:04d}: arch-state update"]
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
        # Include the first 120 characters of the loop_note (if any) for context
        if update.get("loop_note"):
            parts.append(update["loop_note"][:120])

        return " | ".join(parts)

    # ── Summariser ───────────────────────────────────────────────────

    def summarise(self, budget_tokens: int = 800) -> str:
        """
        Produce a compressed plain-text summary of the current state,
        sized to fit within *budget_tokens* (approximated as chars / 4).

        Why do we need this?
        ---------------------
        LLMs have a "context window" — a limit on how many tokens (roughly
        words) they can process in one call.  The full architecture state
        JSON can be very large (many kilobytes), but the AI only needs the
        most important highlights.

        This method produces a concise, scannable text that:
        - Lists all components, sorted by confidence (most certain first).
        - Shows up to 10 relationships.
        - Lists up to 8 design decisions.
        - Lists the top 5 unresolved questions.
        - Appends the 3 most recent loop notes.
        - Truncates at the character budget if the output is still too long.

        Parameters
        ----------
        budget_tokens : Approximate number of LLM tokens to target.
                        Uses the rule-of-thumb that 1 token ≈ 4 characters.
                        Default 800 tokens ≈ 3200 characters.

        Returns
        -------
        A plain-text string ready to paste directly into an LLM prompt.
        """
        s = self._state
        # Convert token budget to character budget using the 1 token ≈ 4 chars rule
        char_budget = budget_tokens * 4
        lines: list[str] = []

        # Header: system name, loop number, confidence
        lines.append(
            f"=== Architecture State: {s.system_name} (loop {s.macro_loop}) ==="
        )
        lines.append(f"Purpose : {s.system_purpose or '(not set)'}")
        lines.append(f"Scope   : {s.system_scope or '(not set)'}")
        tier = s.overall_confidence.tier.value
        lines.append(f"Confidence: {s.overall_confidence.value:.2f} [{tier}]")
        lines.append("")

        # Components — sorted by confidence descending so most-certain appear first
        comps = sorted(s.components.values(), key=lambda c: -c.confidence.value)
        lines.append(f"── Components ({len(comps)}) ──")
        for c in comps:
            # Show only the first 3 responsibilities to save space
            resps = "; ".join(c.responsibilities[:3])
            lines.append(
                f"  [{c.confidence.value:.2f}] {c.name}"
                + (f" — {resps}" if resps else "")
            )
        lines.append("")

        # Relationships (capped at 10 to keep the summary manageable)
        if s.relationships:
            lines.append(f"── Relationships ({len(s.relationships)}) ──")
            # Build a lookup from component ID → component name for readable output
            id_to_name = {cid: c.name for cid, c in s.components.items()}
            for r in list(s.relationships.values())[:10]:
                # Show component names rather than raw IDs wherever possible
                src = id_to_name.get(r.source_id, r.source_id)
                tgt = id_to_name.get(r.target_id, r.target_id)
                lines.append(
                    f"  {src} --[{r.kind}]--> {tgt}"
                    + (f" ({r.description})" if r.description else "")
                )
            if len(s.relationships) > 10:
                lines.append(f"  … +{len(s.relationships) - 10} more")
            lines.append("")

        # Decisions — top 8 by confidence, with status label
        if s.decisions:
            lines.append(f"── Design Decisions ({len(s.decisions)}) ──")
            for d in sorted(s.decisions.values(), key=lambda x: -x.confidence.value)[
                :8
            ]:
                lines.append(f"  [{d.status} {d.confidence.value:.2f}] {d.title}")
            lines.append("")

        # Open questions — top 5 by priority (highest priority = most urgent)
        unresolved = s.unresolved_questions()[:5]
        if unresolved:
            lines.append(f"── Open Questions (top {len(unresolved)}) ──")
            for q in unresolved:
                lines.append(f"  [priority={q.priority:.1f}] {q.question}")
            lines.append("")

        # Most recent loop notes — last 3 for a quick "what just happened" view
        for n in s.loop_notes[-3:]:
            lines.append(f"NOTE: {n}")

        text = "\n".join(lines)
        # Hard-truncate at the character budget with a clear indicator
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

    # ── Query Methods ────────────────────────────────────────────────
    # These convenience methods let callers ask common questions about the
    # current state without needing to work with the raw collections directly.

    def low_confidence_components(self, threshold: float = 0.5) -> list[Component]:
        """
        Return all components whose confidence is below `threshold`,
        sorted ascending by confidence (least certain first).

        Use this to direct research loops: "what do we know least about?"
        Default threshold of 0.5 catches everything at or below "uncertain".
        """
        return self._state.low_confidence_components(threshold)

    def unresolved_questions(self) -> list[OpenQuestion]:
        """
        Return all open questions that haven't been answered yet,
        sorted by priority descending (most urgent first).

        Use this to direct the next research or design loop:
        "what's the most important thing we still need to figure out?"
        """
        return self._state.unresolved_questions()

    def decisions_for_subsystem(self, subsystem: str) -> list[DesignDecision]:
        """
        Return all design decisions tagged with the given subsystem name.
        Case-insensitive.  Useful for subsystem-specific planning.
        """
        return self._state.decisions_for_subsystem(subsystem)

    def speculative_decisions(self) -> list[DesignDecision]:
        """
        Return all decisions with SPECULATIVE confidence (score < 0.40).

        These are decisions the AI has proposed but hasn't yet backed with
        strong evidence.  They may need to be revisited or challenged.
        """
        return [
            d
            for d in self._state.decisions.values()
            if d.confidence.tier == ConfidenceTier.SPECULATIVE
        ]

    def components_by_subsystem(self, subsystem: str) -> list[Component]:
        """
        Return all components tagged with the given subsystem name.
        Case-insensitive.  Useful for getting a complete picture of one
        part of the system.
        """
        return [
            c
            for c in self._state.components.values()
            if c.subsystem and c.subsystem.lower() == subsystem.lower()
        ]

    def confidence_map(self) -> dict[str, float]:
        """
        Build a flat dictionary mapping every tracked item to its current
        confidence score.  Keys are namespaced by type for disambiguation.

        Format: {"component:API Gateway": 0.75, "decision:Use PostgreSQL": 0.80, ...}

        Why is this useful?
        - Quick overview of everything Tinker is confident/uncertain about.
        - Easy to serialise to JSON for logging or visualisation.
        - The orchestrator can use it to prioritise where to focus next.

        Note: Subsystems can be stored either as SubsystemSummary dataclasses
        or as raw dicts (during in-progress merges), so we handle both.
        """
        result: dict[str, float] = {}
        for c in self._state.components.values():
            result[f"component:{c.name}"] = round(c.confidence.value, 4)
        for d in self._state.decisions.values():
            result[f"decision:{d.title}"] = round(d.confidence.value, 4)
        for k, sub in self._state.subsystems.items():
            # Handle both dict and dataclass forms (can occur during transitions)
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
