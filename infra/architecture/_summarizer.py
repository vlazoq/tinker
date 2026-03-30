"""Summariser mixin for ArchitectureStateManager."""

from __future__ import annotations


class SummarizerMixin:
    """Produces compressed plain-text summaries for LLM context injection."""

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
        lines.append(f"=== Architecture State: {s.system_name} (loop {s.macro_loop}) ===")
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
                f"  [{c.confidence.value:.2f}] {c.name}" + (f" — {resps}" if resps else "")
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
            for d in sorted(s.decisions.values(), key=lambda x: -x.confidence.value)[:8]:
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
