"""
core/memory/auto_memory.py
===========================

Auto-Memory: persistent cross-session learning for Tinker.

Inspired by Claude Code's MEMORY.md / auto-memory feature, this module
automatically captures and persists learned patterns across sessions:

  * Effective prompts and phrasing that produced high-quality outputs
  * Architecture decisions and their rationale
  * Common pitfalls and failure modes
  * Subsystem-specific knowledge (what works, what doesn't)
  * Critic feedback patterns that led to improvements

The auto-memory is stored as a structured JSON file (not Markdown) so it
can be programmatically queried and injected into agent context.  A human-
readable MEMORY.md is also maintained as a summary view.

Integration
-----------
The AutoMemory subscribes to EventBus events and passively observes the
orchestrator lifecycle.  It does NOT require any changes to the orchestrator
code beyond wiring in the EventBus (which is already done).

Usage::

    from core.memory.auto_memory import AutoMemory
    from core.events import EventBus

    bus = EventBus()
    auto_mem = AutoMemory(memory_dir="./tinker_memory")
    auto_mem.attach(bus)  # subscribes to relevant events

    # Later, inject learned context into an agent call:
    lessons = auto_mem.get_lessons(subsystem="caching", limit=5)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("tinker.auto_memory")


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class MemoryEntry:
    """A single learned pattern or observation."""

    category: str  # "effective_prompt", "pitfall", "decision", "insight"
    subsystem: str  # which subsystem this applies to (or "general")
    content: str  # the actual lesson learned
    source_event: str  # which event triggered this memory
    confidence: float = 1.0  # 0.0–1.0, decays if contradicted
    reinforcement_count: int = 1  # how many times this was confirmed
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_reinforced_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class AutoMemoryState:
    """Persistent state for the auto-memory system."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=lambda: {
        "total_micro_loops_observed": 0,
        "high_score_count": 0,
        "low_score_count": 0,
        "stagnation_count": 0,
        "refinement_improvements": 0,
    })
    version: int = 1


# ── Score tracking for pattern detection ────────────────────────────────────


class _ScoreTracker:
    """Sliding window of recent critic scores for pattern detection."""

    def __init__(self, window_size: int = 20) -> None:
        self._scores: list[dict[str, Any]] = []
        self._window = window_size

    def add(self, score: float | None, subsystem: str | None) -> None:
        if score is None:
            return
        self._scores.append({
            "score": score,
            "subsystem": subsystem or "unknown",
            "time": time.monotonic(),
        })
        if len(self._scores) > self._window * 2:
            self._scores = self._scores[-self._window:]

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        return self._scores[-n:]

    def avg_score(self, n: int = 10) -> float | None:
        recent = self.recent(n)
        if not recent:
            return None
        return sum(r["score"] for r in recent) / len(recent)

    def subsystem_avg(self, subsystem: str, n: int = 10) -> float | None:
        sub_scores = [s for s in self._scores if s["subsystem"] == subsystem][-n:]
        if not sub_scores:
            return None
        return sum(s["score"] for s in sub_scores) / len(sub_scores)


# ── AutoMemory ──────────────────────────────────────────────────────────────


class AutoMemory:
    """Persistent cross-session learning engine.

    Parameters
    ----------
    memory_dir : str or Path
        Directory where memory files are stored.
    high_score_threshold : float
        Critic scores above this trigger "effective pattern" memories.
    low_score_threshold : float
        Critic scores below this trigger "pitfall" memories.
    max_entries : int
        Maximum number of memory entries to retain.
    """

    def __init__(
        self,
        memory_dir: str | Path = "./tinker_memory",
        high_score_threshold: float = 0.85,
        low_score_threshold: float = 0.4,
        max_entries: int = 500,
    ) -> None:
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._json_path = self._dir / "auto_memory.json"
        self._md_path = self._dir / "MEMORY.md"
        self._high_threshold = high_score_threshold
        self._low_threshold = low_score_threshold
        self._max_entries = max_entries
        self._tracker = _ScoreTracker()

        self._state = self._load_state()
        logger.info(
            "AutoMemory loaded — %d entries from %s",
            len(self._state.entries),
            self._json_path,
        )

    # ── EventBus integration ────────────────────────────────────────────────

    def attach(self, bus: Any) -> None:
        """Subscribe to EventBus events for passive observation."""
        from core.events import EventType

        bus.subscribe_handler(EventType.MICRO_LOOP_COMPLETED, self._on_micro_completed)
        bus.subscribe_handler(EventType.MICRO_LOOP_FAILED, self._on_micro_failed)
        bus.subscribe_handler(EventType.STAGNATION_DETECTED, self._on_stagnation)
        bus.subscribe_handler(EventType.MESO_LOOP_COMPLETED, self._on_meso_completed)
        bus.subscribe_handler(EventType.MACRO_LOOP_COMPLETED, self._on_macro_completed)
        bus.subscribe_handler(EventType.CRITIC_SCORED, self._on_critic_scored)
        bus.subscribe_handler(EventType.REFINEMENT_ITERATION, self._on_refinement)
        logger.info("AutoMemory attached to EventBus")

    # ── Event handlers ──────────────────────────────────────────────────────

    async def _on_micro_completed(self, event: Any) -> None:
        """Track successful micro loops and detect high/low score patterns."""
        p = event.payload
        self._state.stats["total_micro_loops_observed"] += 1

        score = p.get("critic_score")
        subsystem = p.get("subsystem", "unknown")
        self._tracker.add(score, subsystem)

        if score is not None and score >= self._high_threshold:
            self._state.stats["high_score_count"] += 1
            self._add_entry(MemoryEntry(
                category="effective_pattern",
                subsystem=subsystem,
                content=(
                    f"High critic score ({score:.2f}) achieved on subsystem "
                    f"'{subsystem}' at micro loop iteration "
                    f"{p.get('iteration', '?')}. "
                    f"Tokens used: architect={p.get('architect_tokens', '?')}, "
                    f"critic={p.get('critic_tokens', '?')}."
                ),
                source_event="micro_loop_completed",
            ))

        if score is not None and score <= self._low_threshold:
            self._state.stats["low_score_count"] += 1
            self._add_entry(MemoryEntry(
                category="pitfall",
                subsystem=subsystem,
                content=(
                    f"Low critic score ({score:.2f}) on subsystem '{subsystem}' "
                    f"at iteration {p.get('iteration', '?')}. "
                    "Consider adjusting prompts or approach for this subsystem."
                ),
                source_event="micro_loop_completed",
                confidence=0.7,
            ))

        # Periodic save (every 10 loops)
        if self._state.stats["total_micro_loops_observed"] % 10 == 0:
            self._save_state()

    async def _on_micro_failed(self, event: Any) -> None:
        """Record failure patterns."""
        p = event.payload
        error = p.get("error", "unknown error")
        subsystem = p.get("subsystem", "unknown")
        self._add_entry(MemoryEntry(
            category="pitfall",
            subsystem=subsystem,
            content=f"Micro loop failure on '{subsystem}': {error[:200]}",
            source_event="micro_loop_failed",
            confidence=0.6,
        ))

    async def _on_stagnation(self, event: Any) -> None:
        """Record stagnation patterns and interventions."""
        p = event.payload
        self._state.stats["stagnation_count"] += 1
        self._add_entry(MemoryEntry(
            category="pitfall",
            subsystem=p.get("subsystem", "unknown"),
            content=(
                f"Stagnation detected: {p.get('stagnation_type', '?')} "
                f"(severity={p.get('severity', '?'):.2f}). "
                f"Intervention: {p.get('intervention_type', '?')}."
            ),
            source_event="stagnation_detected",
            confidence=0.9,
        ))
        self._save_state()

    async def _on_meso_completed(self, event: Any) -> None:
        """Record successful subsystem syntheses."""
        p = event.payload
        subsystem = p.get("subsystem", "unknown")
        avg = self._tracker.subsystem_avg(subsystem)
        if avg is not None:
            self._add_entry(MemoryEntry(
                category="insight",
                subsystem=subsystem,
                content=(
                    f"Meso synthesis completed for '{subsystem}'. "
                    f"Average critic score over recent loops: {avg:.2f}."
                ),
                source_event="meso_loop_completed",
            ))
            self._save_state()

    async def _on_macro_completed(self, event: Any) -> None:
        """Record macro snapshot events."""
        p = event.payload
        self._add_entry(MemoryEntry(
            category="decision",
            subsystem="general",
            content=(
                f"Macro architectural snapshot #{p.get('iteration', '?')} committed"
                f" (hash={p.get('commit_hash', '?')})."
            ),
            source_event="macro_loop_completed",
        ))
        self._save_state()
        self._write_markdown_summary()

    async def _on_critic_scored(self, event: Any) -> None:
        """Track critic scores for trend analysis."""
        p = event.payload
        self._tracker.add(p.get("score"), p.get("subsystem"))

    async def _on_refinement(self, event: Any) -> None:
        """Record when refinement loops improve quality."""
        p = event.payload
        iteration = p.get("iteration", 0)
        score = p.get("score")
        if iteration > 1 and score is not None:
            self._state.stats["refinement_improvements"] += 1

    # ── Query API ───────────────────────────────────────────────────────────

    def get_lessons(
        self,
        subsystem: str | None = None,
        category: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Retrieve relevant lessons for injection into agent context.

        Parameters
        ----------
        subsystem : optional filter by subsystem (also includes "general")
        category : optional filter by category
        limit : max entries to return

        Returns
        -------
        list of dicts with keys: category, subsystem, content, confidence
        """
        entries = self._state.entries

        if subsystem:
            entries = [
                e for e in entries
                if e.get("subsystem") == subsystem or e.get("subsystem") == "general"
            ]

        if category:
            entries = [e for e in entries if e.get("category") == category]

        # Sort by confidence * reinforcement_count (most validated first)
        entries.sort(
            key=lambda e: e.get("confidence", 0) * e.get("reinforcement_count", 1),
            reverse=True,
        )

        return entries[:limit]

    def get_context_block(self, subsystem: str | None = None, limit: int = 5) -> str:
        """Return a formatted text block suitable for injection into prompts.

        This is the primary integration point — the context assembler can call
        this to add learned lessons to agent prompts.
        """
        lessons = self.get_lessons(subsystem=subsystem, limit=limit)
        if not lessons:
            return ""

        lines = ["[AUTO-MEMORY — Lessons from prior sessions]"]
        for i, entry in enumerate(lessons, 1):
            cat = entry.get("category", "?")
            content = entry.get("content", "")
            lines.append(f"  {i}. [{cat}] {content}")
        lines.append("[END AUTO-MEMORY]")
        return "\n".join(lines)

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics."""
        return {
            **self._state.stats,
            "total_entries": len(self._state.entries),
            "global_avg_score": self._tracker.avg_score(),
        }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _add_entry(self, entry: MemoryEntry) -> None:
        """Add a memory entry, deduplicating similar entries."""
        entry_dict = asdict(entry)

        # Check for duplicates — reinforce existing entry instead
        for existing in self._state.entries:
            if (
                existing.get("category") == entry.category
                and existing.get("subsystem") == entry.subsystem
                and self._similarity(existing.get("content", ""), entry.content) > 0.8
            ):
                existing["reinforcement_count"] = existing.get("reinforcement_count", 1) + 1
                existing["last_reinforced_at"] = datetime.now(timezone.utc).isoformat()
                existing["confidence"] = min(
                    1.0, existing.get("confidence", 0.5) + 0.05
                )
                return

        self._state.entries.append(entry_dict)

        # Prune if over limit — remove lowest confidence entries
        if len(self._state.entries) > self._max_entries:
            self._state.entries.sort(
                key=lambda e: e.get("confidence", 0) * e.get("reinforcement_count", 1),
            )
            self._state.entries = self._state.entries[-self._max_entries:]

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Quick word-overlap similarity (Jaccard index)."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def _load_state(self) -> AutoMemoryState:
        """Load state from disk, or create fresh if missing/corrupt."""
        if not self._json_path.exists():
            return AutoMemoryState()
        try:
            raw = json.loads(self._json_path.read_text(encoding="utf-8"))
            return AutoMemoryState(
                entries=raw.get("entries", []),
                stats=raw.get("stats", AutoMemoryState().stats),
                version=raw.get("version", 1),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("AutoMemory state corrupt, starting fresh: %s", exc)
            return AutoMemoryState()

    def _save_state(self) -> None:
        """Persist state to disk atomically."""
        try:
            from utils.io import atomic_write
            atomic_write(
                str(self._json_path),
                json.dumps(asdict(self._state), indent=2, default=str),
            )
        except ImportError:
            # Fallback if utils not available
            tmp = self._json_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(asdict(self._state), indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self._json_path))

    def _write_markdown_summary(self) -> None:
        """Write a human-readable MEMORY.md summary of learned patterns."""
        stats = self._state.stats
        lines = [
            "# Tinker Auto-Memory",
            "",
            "This file is auto-generated by the Auto-Memory system.",
            "It summarizes patterns learned across sessions.",
            "",
            "## Statistics",
            "",
            f"- Micro loops observed: {stats.get('total_micro_loops_observed', 0)}",
            f"- High-score patterns: {stats.get('high_score_count', 0)}",
            f"- Low-score pitfalls: {stats.get('low_score_count', 0)}",
            f"- Stagnation events: {stats.get('stagnation_count', 0)}",
            f"- Refinement improvements: {stats.get('refinement_improvements', 0)}",
            f"- Total memory entries: {len(self._state.entries)}",
            "",
        ]

        # Group by category
        by_cat: dict[str, list[dict]] = {}
        for entry in self._state.entries:
            cat = entry.get("category", "unknown")
            by_cat.setdefault(cat, []).append(entry)

        category_titles = {
            "effective_pattern": "Effective Patterns",
            "pitfall": "Pitfalls & Failure Modes",
            "decision": "Architecture Decisions",
            "insight": "Insights",
        }

        for cat, title in category_titles.items():
            entries = by_cat.get(cat, [])
            if not entries:
                continue
            # Show top 10 per category by confidence
            entries.sort(
                key=lambda e: e.get("confidence", 0) * e.get("reinforcement_count", 1),
                reverse=True,
            )
            lines.append(f"## {title}")
            lines.append("")
            for entry in entries[:10]:
                conf = entry.get("confidence", 0)
                rcount = entry.get("reinforcement_count", 1)
                sub = entry.get("subsystem", "?")
                lines.append(
                    f"- **[{sub}]** {entry.get('content', '')} "
                    f"*(confidence: {conf:.1f}, seen {rcount}x)*"
                )
            lines.append("")

        try:
            from utils.io import atomic_write
            atomic_write(str(self._md_path), "\n".join(lines))
        except ImportError:
            self._md_path.write_text("\n".join(lines), encoding="utf-8")
