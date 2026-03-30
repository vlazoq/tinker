"""
tinker/architecture/schema.py
==============================

What this file does
--------------------
This file defines all the data shapes ("schemas") used to represent an
evolving software architecture document.  It's the backbone of the entire
architecture package — every other file in this package imports from here.

Think of this file like a set of blank forms:
  - A blank "Component form" (what fields does a component have?)
  - A blank "Design Decision form" (title, rationale, status…)
  - A blank "Open Question form" (what are we still unsure about?)
  …and so on.

Nothing in this file actually *does* anything — it just describes what
the data looks like.  The merger.py fills in those forms; the manager.py
decides when to save and load them.

Why plain Python dataclasses instead of Pydantic?
---------------------------------------------------
Pydantic is a popular library for validated data models, but it's an
external dependency.  This file uses only Python's built-in `dataclasses`
module plus a small amount of hand-written JSON serialisation code.  That
means this package works anywhere Python is installed, with no pip installs
required.

The trade-off is that we write our own `_from_dict` / `_to_dict` helpers
(at the bottom of this file) rather than getting them for free from Pydantic.
That's a small cost for zero external dependencies.

How JSON works here
--------------------
Dataclasses are not JSON-serialisable by default.  So we have:
  - `_to_dict(obj)`          — converts any dataclass (or list/dict of them)
                               into plain Python dicts/lists/strings that
                               json.dumps() can handle.
  - `_from_dict_*` helpers   — one per dataclass type, converts a plain dict
                               back into the typed dataclass.
  - `ArchitectureState.model_dump_json()` / `.model_validate_json()` — the
    top-level API that strings it all together.

Structure of a full ArchitectureState document
-----------------------------------------------
    ArchitectureState
    ├── system_name, system_purpose, system_scope   (strings)
    ├── macro_loop                                  (int — which AI loop we're on)
    ├── overall_confidence                          (ConfidenceScore)
    ├── components      {id → Component}
    ├── relationships   {id → Relationship}
    ├── decisions       {id → DesignDecision}
    ├── rejected_alternatives {id → RejectedAlternative}
    ├── open_questions  {id → OpenQuestion}
    ├── subsystems      {name → SubsystemSummary}
    └── loop_notes      [list of free-text notes]

Python version note
--------------------
This file uses `X | Y` union syntax (e.g. `str | None`) which requires
Python 3.10+.  The `from __future__ import annotations` at the top makes
this work at parse time on Python 3.9 too, but runtime isinstance checks
won't work with those unions on 3.9.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any
from uuid import uuid4

# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────
# An "Enum" (enumeration) is a set of named constants.
# Using an Enum instead of raw strings like "confident" means Python will
# catch typos at runtime — if you type ConfidenceTier.CONFIDETN it's an
# AttributeError, but the string "confidetn" would silently pass through.
# The `(str, Enum)` pattern means the enum values also behave like strings,
# so they serialise naturally to/from JSON.


class ConfidenceTier(StrEnum):
    """
    Human-readable buckets for confidence scores.

    These map a raw 0.0–1.0 float to a named category so code and logs are
    easier to read.  The thresholds are defined in ConfidenceScore.tier.

    SPECULATIVE  — We've just guessed or inferred this; treat with caution.
    TENTATIVE    — We've seen some evidence but it's still early days.
    CONFIDENT    — Solidly supported by multiple observations.
    ESTABLISHED  — Well-proven; extremely unlikely to change.
    """

    SPECULATIVE = "speculative"  # 0.00–0.39
    TENTATIVE = "tentative"  # 0.40–0.64
    CONFIDENT = "confident"  # 0.65–0.84
    ESTABLISHED = "established"  # 0.85–1.00


class RelationshipKind(StrEnum):
    """
    The type of connection between two components.

    These describe *how* one component interacts with another, which is
    important for understanding data flow and dependencies in the system.

    CALLS        — Component A directly invokes Component B (like a function call).
    PUBLISHES_TO — A sends messages/events to B (like a message bus topic).
    READS_FROM   — A reads data that B owns or produces.
    WRITES_TO    — A writes data to a store that B owns.
    DEPENDS_ON   — A needs B to exist/run but the exact interaction is unclear.
    OWNS         — A is responsible for / contains B.
    """

    CALLS = "calls"
    PUBLISHES_TO = "publishes_to"
    READS_FROM = "reads_from"
    WRITES_TO = "writes_to"
    DEPENDS_ON = "depends_on"
    OWNS = "owns"


class DecisionStatus(StrEnum):
    """
    The lifecycle stage of a design decision.

    A decision starts as PROPOSED (someone suggested it), moves to ACCEPTED
    (the team/AI agreed), can be REJECTED (discarded with reasoning), or
    REVISITED (accepted before but now being reconsidered).

    Tracking status lets us distinguish "we definitely chose this" from
    "we're still thinking about it".
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVISITED = "revisited"


# ──────────────────────────────────────────────
# Confidence
# ──────────────────────────────────────────────


@dataclass
class ConfidenceScore:
    """
    Tracks how confident Tinker is about a particular fact, component, or decision.

    Confidence is a float from 0.0 (pure guess) to 1.0 (completely certain).
    It's updated over time using a weighted average — the more evidence we've
    accumulated, the less any single new piece of evidence can swing the score.
    This mimics how humans become more sure of things the more they see
    consistent evidence.

    Fields
    ------
    value          : The current confidence, between 0.0 and 1.0.
    evidence_count : How many times this confidence has been updated.  Used
                     as the weight in the running average.  More updates = more
                     stable score.
    last_updated   : ISO-format timestamp of the most recent update.
    notes          : Human-readable reasons for past updates (kept to last 10).

    Examples
    ---------
    A brand new component gets the default confidence of 0.5 (uncertain).
    After the AI mentions it positively in three consecutive loops, it might
    reach 0.75 (confident).  If the AI then starts expressing doubt, the
    score will slowly drift back down.
    """

    value: float = 0.5  # default to "uncertain" — not yes, not no
    evidence_count: int = 0  # starts at zero; goes up every time absorb() is called
    last_updated: str = field(
        # auto-fill with the current UTC time when the object is created
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    notes: list[str] = field(default_factory=list)  # recent commentary on why confidence changed

    def __post_init__(self):
        # Clamp value to [0, 1] immediately after construction.
        # This guards against bugs like confidence=1.3 or confidence=-0.1
        # which are meaningless but might slip through from raw data.
        self.value = max(0.0, min(1.0, self.value))

    @property
    def tier(self) -> ConfidenceTier:
        """
        Translate the raw float into a human-readable bucket.

        This makes it easy to write code like:
            if score.tier == ConfidenceTier.SPECULATIVE:
                print("Don't trust this yet!")
        instead of remembering that < 0.40 means speculative.
        """
        if self.value < 0.40:
            return ConfidenceTier.SPECULATIVE
        if self.value < 0.65:
            return ConfidenceTier.TENTATIVE
        if self.value < 0.85:
            return ConfidenceTier.CONFIDENT
        return ConfidenceTier.ESTABLISHED

    def absorb(self, new_value: float, note: str | None = None) -> ConfidenceScore:
        """
        Produce a new ConfidenceScore that blends the current value with a fresh
        observation.  This object is NOT modified — a brand-new one is returned.

        Why a weighted average?
        -----------------------
        If we've already seen 9 pieces of evidence, one new data point should
        only shift the score by 1/10th.  This prevents wild swings in a score
        that has a long history.  The weight `w` is the fraction of the old
        evidence vs total evidence after the update.

        Example: current value=0.7, evidence_count=3, new_value=0.4
            w       = 3 / (3+1)  = 0.75  (old evidence gets 75% weight)
            blended = 0.75*0.7 + 0.25*0.4 = 0.525 + 0.10 = 0.625
            The score drops, but not all the way to 0.4 — history counts.

        Parameters
        ----------
        new_value : The new evidence score (0.0–1.0).
        note      : Optional text explaining why this update happened.

        Returns
        -------
        A fresh ConfidenceScore with the blended value and incremented count.
        """
        # w is the weight given to the *existing* history.
        # As evidence_count grows, w → 1.0, so new data has less impact.
        w = self.evidence_count / (self.evidence_count + 1)
        blended = round(max(0.0, min(1.0, w * self.value + (1 - w) * new_value)), 4)
        notes = list(self.notes)  # copy the list so we don't mutate the original
        if note:
            notes.append(note)
        return ConfidenceScore(
            value=blended,
            evidence_count=self.evidence_count + 1,
            last_updated=datetime.now(UTC).isoformat(),
            notes=notes[-10:],  # keep only the 10 most recent notes to save space
        )


# ──────────────────────────────────────────────
# Sub-models
# ──────────────────────────────────────────────
# Each dataclass below represents one "kind of thing" that can appear in an
# architecture document.  They're kept separate so you can work with each
# type independently (e.g. "give me all Components" without caring about
# DesignDecisions).


@dataclass
class Component:
    """
    A single software component in the architecture.

    A "component" is any meaningful building block: a service, a module,
    a database, a queue, an external API, a library — whatever level of
    granularity makes sense for the system being designed.

    Fields
    ------
    name              : The display name (e.g. "API Gateway", "User DB").
                        Used as the human-readable identifier.
    id                : Short random identifier (8 hex chars) used as the
                        dict key in ArchitectureState.components.  This is
                        stable even if the name changes.
    description       : One or two sentences describing what the component is.
    responsibilities  : Bullet-point list of what this component is responsible
                        for.  Grows over time as Tinker learns more.
    subsystem         : Optional grouping (e.g. "auth", "data-pipeline").
    confidence        : How sure Tinker is that this component really exists
                        and is described correctly.
    tags              : Free-form labels for filtering (e.g. ["external", "legacy"]).
    first_seen_loop   : The macro-loop number when this component was first mentioned.
    last_updated_loop : The most recent loop that modified this component.
    metadata          : Catch-all dict for any extra information.
    """

    name: str
    # str(uuid4())[:8] generates a short random ID like "a3f9c1b2"
    # We only use 8 chars because full UUIDs are visually noisy in diffs
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    responsibilities: list[str] = field(
        default_factory=list
    )  # grows as new responsibilities are discovered
    subsystem: str | None = None  # None if we haven't grouped it yet
    confidence: ConfidenceScore = field(
        default_factory=ConfidenceScore
    )  # starts at 0.5 (uncertain)
    tags: list[str] = field(default_factory=list)
    first_seen_loop: int = 0  # 0 means "initialised before any loop ran"
    last_updated_loop: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relationship:
    """
    A directed link between two components.

    Relationships are how we model the "wiring" of the system — who calls
    whom, who reads from whom, who owns whom.  They have a direction
    (source → target) and a type (kind).

    Fields
    ------
    source_id         : The `id` of the component at the "from" end.
    target_id         : The `id` of the component at the "to" end.
    id                : Short random identifier for this relationship.
    kind              : Type of link — one of the RelationshipKind values.
    description       : Free-text explanation of what this relationship means.
    interface_contract: Describes the API/protocol between source and target
                        (e.g. "REST over HTTPS, OpenAPI spec at /docs").
    confidence        : How sure Tinker is this relationship actually exists.
    first_seen_loop   : Loop where this link was first identified.
    last_updated_loop : Loop where this link was most recently updated.
    """

    source_id: str = ""
    target_id: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    kind: str = RelationshipKind.DEPENDS_ON.value  # default to generic "depends on"
    description: str = ""
    interface_contract: str = ""  # empty until Tinker learns what the API looks like
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    first_seen_loop: int = 0
    last_updated_loop: int = 0


@dataclass
class DesignDecision:
    """
    A recorded architectural choice, including its reasoning.

    Design decisions capture *why* the architecture is the way it is.
    This is valuable because:
    1. It prevents relitigating decisions that were already thought through.
    2. It lets future loops see what alternatives were considered.
    3. It provides an audit trail if the system evolves.

    Fields
    ------
    title                  : Short name for the decision (e.g. "Use PostgreSQL
                             for user data storage").
    id                     : Short random identifier.
    description            : Longer explanation of what was decided.
    rationale              : *Why* this was the right choice.
    status                 : One of proposed / accepted / rejected / revisited.
    subsystem              : Which part of the system this decision applies to.
    confidence             : How sure Tinker is this is the right decision.
    alternatives_considered: Other options that were thought about but not chosen.
    tags                   : Labels for searching/filtering.
    first_seen_loop        : When this decision first appeared.
    last_updated_loop      : When it was last changed.
    """

    title: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    rationale: str = ""  # the "why" — often the most important field
    status: str = DecisionStatus.PROPOSED.value  # starts as proposed; must be explicitly accepted
    subsystem: str | None = None
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    alternatives_considered: list[str] = field(default_factory=list)  # what else was on the table
    tags: list[str] = field(default_factory=list)
    first_seen_loop: int = 0
    last_updated_loop: int = 0


@dataclass
class RejectedAlternative:
    """
    An option that was considered but explicitly ruled out.

    Keeping track of rejected alternatives is just as important as keeping
    track of accepted decisions.  If a future AI loop naively re-proposes
    something that was already rejected, the system can point back to this
    record and say "we tried that — here's why it won't work".

    Fields
    ------
    title               : Name of the rejected option.
    id                  : Short random identifier.
    description         : What this option would have entailed.
    rejection_reason    : Concise explanation of why it was ruled out.
    related_decision_id : Optional link to the DesignDecision this was
                          rejected in favour of.
    loop_rejected       : Which AI loop made the rejection call.
    """

    title: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    description: str = ""
    rejection_reason: str = ""  # the key field — never leave this empty
    related_decision_id: str | None = None  # links back to the winning decision
    loop_rejected: int = 0


@dataclass
class OpenQuestion:
    """
    Something the architecture still needs to figure out.

    Open questions are the "known unknowns" — things Tinker has noticed are
    unresolved but doesn't yet have enough information to answer.  Tracking
    them explicitly prevents them from falling through the cracks.

    When a question is answered, `resolved` is set to True and `resolution`
    is filled in.  The record is kept (not deleted) so there's a history.

    Fields
    ------
    question     : The question itself, written as a full sentence.
    id           : Short random identifier.
    context      : Background info explaining why this question matters.
    subsystem    : Which part of the system the question is about.
    priority     : How urgently this needs to be answered (0.0 = low, 1.0 = urgent).
    raised_loop  : When the question was first raised.
    resolved     : True once an answer has been found.
    resolution   : The answer (filled in when resolved=True).
    resolved_loop: Which loop provided the answer.
    """

    question: str = ""
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    context: str = ""
    subsystem: str | None = None
    priority: float = 0.5  # 0.5 = medium priority by default
    raised_loop: int = 0
    resolved: bool = False  # starts unresolved; only becomes True when answered
    resolution: str | None = None  # empty until the question is answered
    resolved_loop: int | None = None  # which loop provided the answer


@dataclass
class SubsystemSummary:
    """
    A high-level grouping of related components.

    Subsystems are optional conceptual buckets.  For a large system you might
    have subsystems like "authentication", "data-pipeline", "api-layer".
    They help organise the architecture mentally without forcing a strict
    hierarchy.

    Fields
    ------
    name             : The subsystem's name (used as the dict key in
                       ArchitectureState.subsystems).
    purpose          : One-sentence description of what this subsystem does.
    components       : List of component names (not IDs) that belong here.
    design_notes     : Any free-text design notes specific to this subsystem.
    confidence       : How sure Tinker is about this subsystem's boundaries.
    last_updated_loop: Most recent loop that touched this subsystem.
    """

    name: str = ""
    purpose: str = ""
    components: list[str] = field(default_factory=list)  # component names, not IDs
    design_notes: str = ""
    confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    last_updated_loop: int = 0


# ──────────────────────────────────────────────
# JSON serialisation helpers
# ──────────────────────────────────────────────
# These functions convert between the Python dataclass objects above and
# plain Python dicts/lists/strings that the `json` module can handle.
#
# The pattern is:
#   dataclass object  ──_to_dict()──►  plain dict  ──json.dumps()──►  JSON string
#   JSON string  ──json.loads()──►  plain dict  ──_from_dict_*()──►  dataclass object
#
# Functions that start with an underscore (_) are "private" — they're
# internal helpers not meant to be called from outside this file.


def _to_dict(obj: Any) -> Any:
    """
    Recursively convert a dataclass (or any nested mix of dataclasses,
    enums, lists, dicts, datetimes) into plain Python types that
    `json.dumps()` can serialise.

    Why recursively?  Because our dataclasses contain other dataclasses
    (e.g. Component contains ConfidenceScore), which in turn may contain
    lists and dicts.  We walk the whole tree.

    The conversion rules are:
      - Primitives (str, int, float, bool, None) → pass through unchanged
      - Enum          → its .value string  (e.g. ConfidenceTier.CONFIDENT → "confident")
      - datetime      → ISO-format string  (e.g. "2024-01-15T12:00:00+00:00")
      - list          → list with each element converted
      - dict          → dict with each value converted
      - dataclass     → dict of {field_name: converted_value}
      - anything else → pass through as-is (shouldn't happen in practice)
    """
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        # These are already JSON-safe — nothing to do
        return obj
    if isinstance(obj, Enum):
        # Enums store their values as strings; use that string
        return obj.value
    if isinstance(obj, datetime):
        # Convert datetime to a standard string so it survives JSON round-trips
        return obj.isoformat()
    if isinstance(obj, list):
        # Recurse into each element of the list
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        # Recurse into each value of the dict (keys are already strings)
        return {k: _to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        # This is a dataclass — convert each field by name
        return {f.name: _to_dict(getattr(obj, f.name)) for f in fields(obj)}
    # Fallback: return unchanged (handles unexpected types gracefully)
    return obj


def _from_dict_confidence(d: dict | None) -> ConfidenceScore:
    """
    Reconstruct a ConfidenceScore from a plain dict (loaded from JSON).
    If `d` is None (the field was missing from the JSON), return a default
    ConfidenceScore with value=0.5.
    """
    if d is None:
        # Missing confidence data → use the default "uncertain" score
        return ConfidenceScore()
    return ConfidenceScore(
        value=d.get("value", 0.5),
        evidence_count=d.get("evidence_count", 0),
        last_updated=d.get("last_updated", datetime.now(UTC).isoformat()),
        notes=d.get("notes", []),
    )


def _from_dict_component(d: dict) -> Component:
    """
    Reconstruct a Component from a plain dict (loaded from JSON).
    Uses .get() with defaults throughout so old JSON files missing newer
    fields don't crash — they just get sensible defaults.
    """
    c = Component(
        name=d.get("name", ""),
        id=d.get("id", str(uuid4())[:8]),  # generate a new ID if missing
        description=d.get("description", ""),
        responsibilities=d.get("responsibilities", []),
        subsystem=d.get("subsystem"),  # None if not present
        confidence=_from_dict_confidence(d.get("confidence")),
        tags=d.get("tags", []),
        first_seen_loop=d.get("first_seen_loop", 0),
        last_updated_loop=d.get("last_updated_loop", 0),
        metadata=d.get("metadata", {}),
    )
    return c


def _from_dict_relationship(d: dict) -> Relationship:
    """Reconstruct a Relationship from a plain dict (loaded from JSON)."""
    return Relationship(
        id=d.get("id", str(uuid4())[:8]),
        source_id=d.get("source_id", ""),
        target_id=d.get("target_id", ""),
        kind=d.get("kind", RelationshipKind.DEPENDS_ON.value),  # default to generic link
        description=d.get("description", ""),
        interface_contract=d.get("interface_contract", ""),
        confidence=_from_dict_confidence(d.get("confidence")),
        first_seen_loop=d.get("first_seen_loop", 0),
        last_updated_loop=d.get("last_updated_loop", 0),
    )


def _from_dict_decision(d: dict) -> DesignDecision:
    """Reconstruct a DesignDecision from a plain dict (loaded from JSON)."""
    return DesignDecision(
        id=d.get("id", str(uuid4())[:8]),
        title=d.get("title", ""),
        description=d.get("description", ""),
        rationale=d.get("rationale", ""),
        status=d.get("status", DecisionStatus.PROPOSED.value),  # default to proposed
        subsystem=d.get("subsystem"),
        confidence=_from_dict_confidence(d.get("confidence")),
        alternatives_considered=d.get("alternatives_considered", []),
        tags=d.get("tags", []),
        first_seen_loop=d.get("first_seen_loop", 0),
        last_updated_loop=d.get("last_updated_loop", 0),
    )


def _from_dict_rejected(d: dict) -> RejectedAlternative:
    """Reconstruct a RejectedAlternative from a plain dict (loaded from JSON)."""
    return RejectedAlternative(
        id=d.get("id", str(uuid4())[:8]),
        title=d.get("title", ""),
        description=d.get("description", ""),
        rejection_reason=d.get("rejection_reason", ""),
        related_decision_id=d.get("related_decision_id"),  # may be None
        loop_rejected=d.get("loop_rejected", 0),
    )


def _from_dict_question(d: dict) -> OpenQuestion:
    """Reconstruct an OpenQuestion from a plain dict (loaded from JSON)."""
    return OpenQuestion(
        id=d.get("id", str(uuid4())[:8]),
        question=d.get("question", ""),
        context=d.get("context", ""),
        subsystem=d.get("subsystem"),
        priority=d.get("priority", 0.5),  # default to medium priority
        raised_loop=d.get("raised_loop", 0),
        resolved=d.get("resolved", False),  # default to unresolved
        resolution=d.get("resolution"),  # None until answered
        resolved_loop=d.get("resolved_loop"),
    )


def _from_dict_subsystem(d: dict) -> SubsystemSummary:
    """Reconstruct a SubsystemSummary from a plain dict (loaded from JSON)."""
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
    """
    The top-level document that holds everything Tinker knows about the system.

    Think of this as the "master architecture document" — a single object that
    contains all components, all relationships, all decisions, all open
    questions, and all subsystem groupings.  Every time a new AI loop runs, a
    new version of this document is produced and saved to disk.

    This is NOT mutated in-place.  Instead, the merger produces a whole new
    ArchitectureState and the manager replaces the old one.  This makes it
    safe to keep old snapshots around for diffing and history.

    Fields
    ------
    schema_version  : Version string for the document format (used if the
                      format ever changes and we need to migrate old files).
    state_id        : Full UUID for this document.  Each version gets a new
                      one so they're distinguishable.
    system_name     : Human name for the system being designed.
    system_purpose  : One-paragraph description of what the system does.
    system_scope    : What's in scope / out of scope for this architecture.
    macro_loop      : Which AI macro-loop produced this version.  Increments
                      each time apply_update() is called.
    created_at      : When the very first version of this document was created.
    updated_at      : When this specific version was saved.
    components      : Dict mapping component ID → Component object.
    relationships   : Dict mapping relationship ID → Relationship object.
    decisions       : Dict mapping decision ID → DesignDecision object.
    rejected_alternatives : Dict mapping ID → RejectedAlternative.
    open_questions  : Dict mapping question ID → OpenQuestion object.
    subsystems      : Dict mapping subsystem name → SubsystemSummary.
    overall_confidence : A single confidence score for the whole document.
    loop_notes      : Free-text notes, one per loop, for human reading.
    """

    schema_version: str = "1.0"
    # Full UUID (not shortened) because this is the document's unique identity
    state_id: str = field(default_factory=lambda: str(uuid4()))
    system_name: str = "Unknown System"
    system_purpose: str = ""
    system_scope: str = ""

    macro_loop: int = 0  # starts at 0 and increments with each update
    created_at: str = field(
        # The creation timestamp never changes after the first save
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = field(
        # This gets refreshed every time we produce a new version
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    # All collections use the item's `id` field as the dict key.
    # This gives O(1) lookup by ID, and the dicts serialise cleanly to JSON.
    components: dict[str, Component] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)
    decisions: dict[str, DesignDecision] = field(default_factory=dict)
    rejected_alternatives: dict[str, RejectedAlternative] = field(default_factory=dict)
    open_questions: dict[str, OpenQuestion] = field(default_factory=dict)
    # Subsystems use the subsystem *name* as the key (not a UUID) because
    # names like "auth" are stable and human-readable as dict keys
    subsystems: dict[str, SubsystemSummary] = field(default_factory=dict)

    overall_confidence: ConfidenceScore = field(default_factory=ConfidenceScore)
    loop_notes: list[str] = field(default_factory=list)

    # ── Serialisation ────────────────────────────────────────────────

    def model_dump_json(self, indent: int = 2) -> str:
        """
        Serialise the entire document to a pretty-printed JSON string.

        This is what gets written to disk.  The `indent=2` makes the file
        human-readable (and good-looking in Git diffs).
        """
        return json.dumps(_to_dict(self), indent=indent)

    @classmethod
    def model_validate_json(cls, text: str) -> ArchitectureState:
        """
        Parse a JSON string (previously produced by model_dump_json) back
        into a live ArchitectureState object.

        This is the "load from disk" path.  It calls _from_dict() internally.
        """
        d = json.loads(text)
        return cls._from_dict(d)

    @classmethod
    def _from_dict(cls, d: dict) -> ArchitectureState:
        """
        Reconstruct an ArchitectureState from a plain Python dict.

        This is the low-level deserialisation step.  Each sub-collection is
        rebuilt by calling the appropriate _from_dict_* helper, which
        reconstructs the correct dataclass type for each item.

        The dict comprehension pattern:
            {k: _from_dict_component(v) for k, v in d.get("components", {}).items()}
        reads as: "for each key-value pair in the components dict, convert
        the value from a plain dict to a Component dataclass".
        """
        # Reconstruct each collection by calling the appropriate helper
        comps = {k: _from_dict_component(v) for k, v in d.get("components", {}).items()}
        rels = {k: _from_dict_relationship(v) for k, v in d.get("relationships", {}).items()}
        decs = {k: _from_dict_decision(v) for k, v in d.get("decisions", {}).items()}
        rejas = {k: _from_dict_rejected(v) for k, v in d.get("rejected_alternatives", {}).items()}
        qs = {k: _from_dict_question(v) for k, v in d.get("open_questions", {}).items()}
        subs = {k: _from_dict_subsystem(v) for k, v in d.get("subsystems", {}).items()}
        return cls(
            schema_version=d.get("schema_version", "1.0"),
            state_id=d.get("state_id", str(uuid4())),
            system_name=d.get("system_name", "Unknown System"),
            system_purpose=d.get("system_purpose", ""),
            system_scope=d.get("system_scope", ""),
            macro_loop=d.get("macro_loop", 0),
            created_at=d.get("created_at", datetime.now(UTC).isoformat()),
            updated_at=d.get("updated_at", datetime.now(UTC).isoformat()),
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
    # These methods make it easy to answer common questions about the state
    # without having to write loops everywhere.

    def component_by_name(self, name: str) -> Component | None:
        """
        Find a component by its human-readable name (case-insensitive).
        Returns None if no component with that name exists.

        This does a linear scan — fine for typical architecture documents
        which have tens to hundreds of components, not millions.
        """
        for c in self.components.values():
            if c.name.lower() == name.lower():
                return c
        return None

    def question_by_text(self, text: str) -> OpenQuestion | None:
        """
        Find an open question by its exact text (case-insensitive).
        Used by the diff engine to detect when a question flips from
        unresolved to resolved between two versions.
        """
        t = text.lower()
        for q in self.open_questions.values():
            if q.question.lower() == t:
                return q
        return None

    def decisions_for_subsystem(self, subsystem: str) -> list[DesignDecision]:
        """
        Return all design decisions that belong to a specific subsystem.
        Useful for understanding all the choices made for, say, the "auth" layer.
        """
        return [
            d
            for d in self.decisions.values()
            if d.subsystem and d.subsystem.lower() == subsystem.lower()
        ]

    def low_confidence_components(self, threshold: float = 0.5) -> list[Component]:
        """
        Return components whose confidence is below `threshold`, sorted
        from least confident to most confident.

        This is the "what do we know least about?" query.  The result
        helps the orchestrator prioritise where to focus the next AI loop.

        The default threshold of 0.5 catches everything at or below "uncertain".
        """
        return sorted(
            [c for c in self.components.values() if c.confidence.value < threshold],
            key=lambda c: c.confidence.value,  # lowest confidence first
        )

    def unresolved_questions(self) -> list[OpenQuestion]:
        """
        Return all questions that haven't been answered yet, sorted by
        priority descending (most urgent first).

        This is the "what do we still need to figure out?" query.
        The orchestrator can use this to direct the next research loop.
        """
        return sorted(
            [q for q in self.open_questions.values() if not q.resolved],
            key=lambda q: -q.priority,  # negate priority so highest comes first
        )
