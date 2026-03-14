"""
tinker/architecture_state/schema.py
────────────────────────────────────
Pure-stdlib schema using dataclasses + JSON serialization.
Python 3.10+ (uses match/case is NOT used; uses | union syntax for 3.10+).
Compatible with 3.9+ if you swap X | Y hints for Optional[X].
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────

class ConfidenceTier(str, Enum):
    SPECULATIVE = "speculative"   # 0.00–0.39
    TENTATIVE   = "tentative"     # 0.40–0.64
    CONFIDENT   = "confident"     # 0.65–0.84
    ESTABLISHED = "established"   # 0.85–1.00


class RelationshipKind(str, Enum):
    CALLS        = "calls"
    PUBLISHES_TO = "publishes_to"
    READS_FROM   = "reads_from"
    WRITES_TO    = "writes_to"
    DEPENDS_ON   = "depends_on"
    OWNS         = "owns"


class DecisionStatus(str, Enum):
    PROPOSED  = "proposed"
    ACCEPTED  = "accepted"
    REJECTED  = "rejected"
    REVISITED = "revisited"


# ──────────────────────────────────────────────
# Confidence
# ──────────────────────────────────────────────

@dataclass
class ConfidenceScore:
    value: float = 0.5
    evidence_count: int = 0
    last_updated: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.value = max(0.0, min(1.0, self.value))

    @property
    def tier(self) -> ConfidenceTier:
        if self.value < 0.40:
            return ConfidenceTier.SPECULATIVE
        if self.value < 0.65:
            return ConfidenceTier.TENTATIVE
        if self.value < 0.85:
            return ConfidenceTier.CONFIDENT
        return ConfidenceTier.ESTABLISHED

    def absorb(self, new_value: float, note: str | None = None) -> "ConfidenceScore":
        """Weighted-average update. Returns a new instance."""
        w = self.evidence_count / (self.evidence_count + 1)
        blended = round(max(0.0, min(1.0, w * self.value + (1 - w) * new_value)), 4)
        notes = list(self.notes)
        if note:
            notes.append(note)
        return ConfidenceScore(
            value=blended,
            evidence_count=self.evidence_count + 1,
            last_updated=datetime.now(timezone.utc).isoformat(),
            notes=notes[-10:],
        )


# ──────────────────────────────────────────────
# Sub-models
# ──────────────────────────────────────────────

@dataclass
class Component:
    name: str
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    responsibilities: list[str] = field(default_factory=list)
    subsystem: str | None = None
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    tags: list[str] = field(default_factory=list)
    first_seen_loop: int = 0
    last_updated_loop: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relationship:
    source_id: str = ""
    target_id: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    kind: str = RelationshipKind.DEPENDS_ON.value
    description: str = ""
    interface_contract: str = ""
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    first_seen_loop: int = 0
    last_updated_loop: int = 0


@dataclass
class DesignDecision:
    title: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    rationale: str = ""
    status: str = DecisionStatus.PROPOSED.value
    subsystem: str | None = None
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    alternatives_considered: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    first_seen_loop: int = 0
    last_updated_loop: int = 0


@dataclass
class RejectedAlternative:
    title: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    rejection_reason: str = ""
    related_decision_id: str | None = None
    loop_rejected: int = 0


@dataclass
class OpenQuestion:
    question: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    context: str = ""
    subsystem: str | None = None
    priority: float = 0.5
    raised_loop: int = 0
    resolved: bool = False
    resolution: str | None = None
    resolved_loop: int | None = None


@dataclass
class SubsystemSummary:
    name: str = ""
    purpose: str = ""
    components: list[str] = field(default_factory=list)
    design_notes: str = ""
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    last_updated_loop: int = 0


# ──────────────────────────────────────────────
# JSON serialisation helpers
# ──────────────────────────────────────────────

def _to_dict(obj: Any) -> Any:
    """Recursively convert dataclass / enum / datetime to JSON-safe types."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {f.name: _to_dict(getattr(obj, f.name))
                for f in fields(obj)}
    return obj


def _from_dict_confidence(d: dict | None) -> ConfidenceScore:
    if d is None:
        return ConfidenceScore()
    return ConfidenceScore(
        value=d.get("value", 0.5),
        evidence_count=d.get("evidence_count", 0),
        last_updated=d.get("last_updated", datetime.now(timezone.utc).isoformat()),
        notes=d.get("notes", []),
    )


def _from_dict_component(d: dict) -> Component:
    c = Component(
        name=d.get("name", ""),
        id=d.get("id", str(uuid4())[:8]),
        description=d.get("description", ""),
        responsibilities=d.get("responsibilities", []),
        subsystem=d.get("subsystem"),
        confidence=_from_dict_confidence(d.get("confidence")),
        tags=d.get("tags", []),
        first_seen_loop=d.get("first_seen_loop", 0),
        last_updated_loop=d.get("last_updated_loop", 0),
        metadata=d.get("metadata", {}),
    )
    return c


def _from_dict_relationship(d: dict) -> Relationship:
    return Relationship(
        id=d.get("id", str(uuid4())[:8]),
        source_id=d.get("source_id", ""),
        target_id=d.get("target_id", ""),
        kind=d.get("kind", RelationshipKind.DEPENDS_ON.value),
        description=d.get("description", ""),
        interface_contract=d.get("interface_contract", ""),
        confidence=_from_dict_confidence(d.get("confidence")),
        first_seen_loop=d.get("first_seen_loop", 0),
        last_updated_loop=d.get("last_updated_loop", 0),
    )


def _from_dict_decision(d: dict) -> DesignDecision:
    return DesignDecision(
        id=d.get("id", str(uuid4())[:8]),
        title=d.get("title", ""),
        description=d.get("description", ""),
        rationale=d.get("rationale", ""),
        status=d.get("status", DecisionStatus.PROPOSED.value),
        subsystem=d.get("subsystem"),
        confidence=_from_dict_confidence(d.get("confidence")),
        alternatives_considered=d.get("alternatives_considered", []),
        tags=d.get("tags", []),
        first_seen_loop=d.get("first_seen_loop", 0),
        last_updated_loop=d.get("last_updated_loop", 0),
    )


def _from_dict_rejected(d: dict) -> RejectedAlternative:
    return RejectedAlternative(
        id=d.get("id", str(uuid4())[:8]),
        title=d.get("title", ""),
        description=d.get("description", ""),
        rejection_reason=d.get("rejection_reason", ""),
        related_decision_id=d.get("related_decision_id"),
        loop_rejected=d.get("loop_rejected", 0),
    )


def _from_dict_question(d: dict) -> OpenQuestion:
    return OpenQuestion(
        id=d.get("id", str(uuid4())[:8]),
        question=d.get("question", ""),
        context=d.get("context", ""),
        subsystem=d.get("subsystem"),
        priority=d.get("priority", 0.5),
        raised_loop=d.get("raised_loop", 0),
        resolved=d.get("resolved", False),
        resolution=d.get("resolution"),
        resolved_loop=d.get("resolved_loop"),
    )


def _from_dict_subsystem(d: dict) -> SubsystemSummary:
    return SubsystemSummary(
        name=d.get("name", ""),
        purpose=d.get("purpose", ""),
        components=d.get("components", []),
        design_notes=d.get("design_notes", ""),
        confidence=_from_dict_confidence(d.get("confidence")),
        last_updated_loop=d.get("last_updated_loop", 0),
    )


# ──────────────────────────────────────────────
# Root document
# ──────────────────────────────────────────────

@dataclass
class ArchitectureState:
    schema_version: str = "1.0"
    state_id: str = field(default_factory=lambda: str(uuid4()))
    system_name: str = "Unknown System"
    system_purpose: str = ""
    system_scope: str = ""

    macro_loop: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # id → item
    components: dict[str, Component] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)
    decisions: dict[str, DesignDecision] = field(default_factory=dict)
    rejected_alternatives: dict[str, RejectedAlternative] = field(default_factory=dict)
    open_questions: dict[str, OpenQuestion] = field(default_factory=dict)
    subsystems: dict[str, SubsystemSummary] = field(default_factory=dict)

    overall_confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    loop_notes: list[str] = field(default_factory=list)

    # ── Serialisation ────────────────────────────────────────────────

    def model_dump_json(self, indent: int = 2) -> str:
        return json.dumps(_to_dict(self), indent=indent)

    @classmethod
    def model_validate_json(cls, text: str) -> "ArchitectureState":
        d = json.loads(text)
        return cls._from_dict(d)

    @classmethod
    def _from_dict(cls, d: dict) -> "ArchitectureState":
        comps = {k: _from_dict_component(v) for k, v in d.get("components", {}).items()}
        rels  = {k: _from_dict_relationship(v) for k, v in d.get("relationships", {}).items()}
        decs  = {k: _from_dict_decision(v) for k, v in d.get("decisions", {}).items()}
        rejas = {k: _from_dict_rejected(v) for k, v in d.get("rejected_alternatives", {}).items()}
        qs    = {k: _from_dict_question(v) for k, v in d.get("open_questions", {}).items()}
        subs  = {k: _from_dict_subsystem(v) for k, v in d.get("subsystems", {}).items()}
        return cls(
            schema_version=d.get("schema_version", "1.0"),
            state_id=d.get("state_id", str(uuid4())),
            system_name=d.get("system_name", "Unknown System"),
            system_purpose=d.get("system_purpose", ""),
            system_scope=d.get("system_scope", ""),
            macro_loop=d.get("macro_loop", 0),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=d.get("updated_at", datetime.now(timezone.utc).isoformat()),
            components=comps,
            relationships=rels,
            decisions=decs,
            rejected_alternatives=rejas,
            open_questions=qs,
            subsystems=subs,
            overall_confidence=_from_dict_confidence(d.get("overall_confidence")),
            loop_notes=d.get("loop_notes", []),
        )

    # ── Convenience queries ──────────────────────────────────────────

    def component_by_name(self, name: str) -> Component | None:
        for c in self.components.values():
            if c.name.lower() == name.lower():
                return c
        return None

    def question_by_text(self, text: str) -> OpenQuestion | None:
        t = text.lower()
        for q in self.open_questions.values():
            if q.question.lower() == t:
                return q
        return None

    def decisions_for_subsystem(self, subsystem: str) -> list[DesignDecision]:
        return [d for d in self.decisions.values()
                if d.subsystem and d.subsystem.lower() == subsystem.lower()]

    def low_confidence_components(self, threshold: float = 0.5) -> list[Component]:
        return sorted(
            [c for c in self.components.values() if c.confidence.value < threshold],
            key=lambda c: c.confidence.value,
        )

    def unresolved_questions(self) -> list[OpenQuestion]:
        return sorted(
            [q for q in self.open_questions.values() if not q.resolved],
            key=lambda q: -q.priority,
        )
