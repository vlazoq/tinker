"""Query convenience methods mixin for ArchitectureStateManager."""

from __future__ import annotations

from .schema import (
    Component,
    ConfidenceTier,
    DesignDecision,
    OpenQuestion,
)


class QueriesMixin:
    """Structured queries against the current architecture state."""

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
