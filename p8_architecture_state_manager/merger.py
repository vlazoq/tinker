"""
tinker/architecture_state/merger.py
────────────────────────────────────
Stateless merge helpers.  Takes an existing ArchitectureState and an update
payload dict, returns a new ArchitectureState (nothing mutated in-place).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .schema import (
    ArchitectureState,
    Component,
    ConfidenceScore,
    DesignDecision,
    OpenQuestion,
    RejectedAlternative,
    Relationship,
    SubsystemSummary,
    _to_dict,
    _from_dict_component,
    _from_dict_relationship,
    _from_dict_decision,
    _from_dict_rejected,
    _from_dict_question,
    _from_dict_subsystem,
    _from_dict_confidence,
)


def merge_update(state: ArchitectureState, update: dict[str, Any]) -> ArchitectureState:
    """
    Additively merge *update* into *state* and return a new ArchitectureState.
    Nothing is ever deleted — items can be marked resolved/rejected but persist.
    """
    import copy, json
    # Deep-copy via JSON round-trip (no external deps)
    data: dict = json.loads(state.model_dump_json())
    loop = update.get("loop", state.macro_loop + 1)
    data["macro_loop"] = loop

    for field in ("system_purpose", "system_scope", "system_name"):
        if update.get(field):
            data[field] = update[field]

    _merge_components(data.setdefault("components", {}), update.get("components", []), loop)
    _merge_relationships(data.setdefault("relationships", {}), update.get("relationships", []), loop)
    _merge_decisions(data.setdefault("decisions", {}), update.get("decisions", []), loop)
    _merge_rejected(data.setdefault("rejected_alternatives", {}), update.get("rejected_alternatives", []), loop)
    _merge_questions(data.setdefault("open_questions", {}), update.get("open_questions", []), loop)
    _merge_subsystems(data.setdefault("subsystems", {}), update.get("subsystems", []), loop)

    if "overall_confidence" in update:
        oc = _from_dict_confidence(data.get("overall_confidence"))
        data["overall_confidence"] = _to_dict(
            oc.absorb(update["overall_confidence"], note=f"loop {loop}")
        )

    if update.get("loop_note"):
        data.setdefault("loop_notes", []).append(f"[loop {loop}] {update['loop_note']}")

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return ArchitectureState._from_dict(data)


# ── helpers ─────────────────────────────────────────────────────────

def _find_by_name(collection: dict, name: str) -> tuple[str | None, dict | None]:
    nl = name.lower()
    for k, v in collection.items():
        item_name = v.get("name", "") or v.get("title", "")
        if item_name.lower() == nl:
            return k, v
    return None, None


def _pop_confidence(raw: dict) -> tuple[float | None, str | None]:
    return raw.pop("confidence_value", None), raw.pop("confidence_note", None)


def _apply_confidence(ex: dict, cv: float | None, cn: str | None) -> None:
    if cv is not None:
        old = _from_dict_confidence(ex.get("confidence"))
        ex["confidence"] = _to_dict(old.absorb(cv, cn))


# ── per-collection merges ────────────────────────────────────────────

def _merge_components(col: dict, items: list, loop: int) -> None:
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)
        name = raw.get("name", "")
        ek, ex = _find_by_name(col, name)
        if ex is None:
            c = Component(
                name=name,
                description=raw.get("description", ""),
                responsibilities=raw.get("responsibilities", []),
                subsystem=raw.get("subsystem"),
                tags=raw.get("tags", []),
                metadata=raw.get("metadata", {}),
                first_seen_loop=loop,
                last_updated_loop=loop,
            )
            if cv is not None:
                c.confidence = ConfidenceScore(value=cv, evidence_count=1, notes=[cn] if cn else [])
            col[c.id] = _to_dict(c)
        else:
            ex = dict(ex)
            for f in ("description", "subsystem", "tags", "metadata"):
                if raw.get(f):
                    ex[f] = raw[f]
            for r in raw.get("responsibilities", []):
                if r not in ex.get("responsibilities", []):
                    ex.setdefault("responsibilities", []).append(r)
            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[ek] = ex


def _merge_relationships(col: dict, items: list, loop: int) -> None:
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)
        src  = raw.get("source_id") or raw.get("source_name", "")
        tgt  = raw.get("target_id") or raw.get("target_name", "")
        kind = raw.get("kind", "depends_on")
        sig  = f"{src}::{tgt}::{kind}"
        ek   = next(
            (k for k, v in col.items()
             if f"{v.get('source_id','')}{v.get('source_name','')}::"
                f"{v.get('target_id','')}{v.get('target_name','')}::{v.get('kind','')}" == sig),
            None,
        )
        if ek is None:
            r = Relationship(
                source_id=src, target_id=tgt,
                kind=kind,
                description=raw.get("description", ""),
                interface_contract=raw.get("interface_contract", ""),
                first_seen_loop=loop, last_updated_loop=loop,
            )
            if cv is not None:
                r.confidence = ConfidenceScore(value=cv, evidence_count=1, notes=[cn] if cn else [])
            col[r.id] = _to_dict(r)
        else:
            ex = dict(col[ek])
            for f in ("description", "interface_contract"):
                if raw.get(f):
                    ex[f] = raw[f]
            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[ek] = ex


def _merge_decisions(col: dict, items: list, loop: int) -> None:
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)
        title = raw.get("title", "")
        ek, ex = _find_by_name(col, title)
        if ex is None:
            d = DesignDecision(
                title=title,
                description=raw.get("description", ""),
                rationale=raw.get("rationale", ""),
                status=raw.get("status", "proposed"),
                subsystem=raw.get("subsystem"),
                alternatives_considered=raw.get("alternatives_considered", []),
                tags=raw.get("tags", []),
                first_seen_loop=loop, last_updated_loop=loop,
            )
            if cv is not None:
                d.confidence = ConfidenceScore(value=cv, evidence_count=1, notes=[cn] if cn else [])
            col[d.id] = _to_dict(d)
        else:
            ex = dict(ex)
            for f in ("description", "rationale", "status", "subsystem", "tags"):
                if raw.get(f):
                    ex[f] = raw[f]
            for alt in raw.get("alternatives_considered", []):
                if alt not in ex.get("alternatives_considered", []):
                    ex.setdefault("alternatives_considered", []).append(alt)
            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[ek] = ex


def _merge_rejected(col: dict, items: list, loop: int) -> None:
    for raw in items:
        raw = dict(raw)
        title = raw.get("title", "")
        _, ex = _find_by_name(col, title)
        if ex is None:
            r = RejectedAlternative(
                title=title,
                description=raw.get("description", ""),
                rejection_reason=raw.get("rejection_reason", ""),
                related_decision_id=raw.get("related_decision_id"),
                loop_rejected=loop,
            )
            col[r.id] = _to_dict(r)


def _merge_questions(col: dict, items: list, loop: int) -> None:
    for raw in items:
        raw = dict(raw)
        text = raw.get("question", "")
        ek = next(
            (k for k, v in col.items() if v.get("question", "").lower() == text.lower()),
            None,
        )
        if ek is None:
            q = OpenQuestion(
                question=text,
                context=raw.get("context", ""),
                subsystem=raw.get("subsystem"),
                priority=raw.get("priority", 0.5),
                raised_loop=loop,
                resolved=raw.get("resolved", False),
                resolution=raw.get("resolution"),
                resolved_loop=raw.get("resolved_loop"),
            )
            col[q.id] = _to_dict(q)
        else:
            ex = dict(col[ek])
            for f in ("context", "subsystem", "priority", "resolved", "resolution", "resolved_loop"):
                if raw.get(f) is not None:
                    ex[f] = raw[f]
            col[ek] = ex


def _merge_subsystems(col: dict, items: list, loop: int) -> None:
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)
        name = raw.get("name", "")
        _, ex = _find_by_name(col, name)
        if ex is None:
            s = SubsystemSummary(
                name=name,
                purpose=raw.get("purpose", ""),
                components=raw.get("components", []),
                design_notes=raw.get("design_notes", ""),
                last_updated_loop=loop,
            )
            if cv is not None:
                s.confidence = ConfidenceScore(value=cv, evidence_count=1, notes=[cn] if cn else [])
            col[name] = _to_dict(s)
        else:
            ex = dict(ex)
            for f in ("purpose", "design_notes"):
                if raw.get(f):
                    ex[f] = raw[f]
            for cn_ in raw.get("components", []):
                if cn_ not in ex.get("components", []):
                    ex.setdefault("components", []).append(cn_)
            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[name] = ex
