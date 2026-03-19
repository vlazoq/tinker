"""
tinker/architecture/merger.py
==============================

What this file does
--------------------
This file contains the logic for "merging" new AI-generated information into
an existing architecture document.  Every time Tinker's AI loops produce
output, that output is turned into an "update" dictionary, and this file
decides how to blend it into the growing architecture knowledge base.

The most important principle here: NOTHING IS EVER DELETED.
-------------------------------------------------------------
Traditional document editing replaces old content with new content.
Here we only ever ADD.  If the AI mentions a component we already know
about, we UPDATE its description and GROW its list of responsibilities.
If the AI discovers a completely new component, we ADD it.
If the AI changes its mind about a decision's status, we UPDATE the status.
But we never silently remove anything.

Why?  Because losing information is dangerous in a long-running AI loop.
If Tinker's loop 50 forgets what loop 20 discovered, it might spend loop 55
re-discovering the same thing (wasting compute) or contradicting it (causing
confusion).  The merge-only approach means knowledge can only accumulate.

How it works (step by step)
-----------------------------
1. `merge_update()` is the only function the manager calls.  It takes:
   - `state`  — the current ArchitectureState (e.g. from loop 49)
   - `update` — a plain dict of new information (e.g. from loop 50)
   It returns a brand-new ArchitectureState (loop 50's version).

2. Internally, it converts the current state to a plain dict (via JSON
   round-trip), applies all the per-collection merge functions, then
   converts back to a typed ArchitectureState.

3. The JSON round-trip (state → JSON string → dict) is used as a cheap
   deep-copy.  It avoids importing `copy` and works reliably for all the
   simple data types we use.

4. Each collection (components, relationships, decisions, etc.) has its own
   `_merge_*` function.  These follow the same pattern:
   - For each item in the update list:
     - Try to find a matching existing item (by name or ID).
     - If NOT found: CREATE a new item.
     - If found: UPDATE the existing item's fields with new values.

5. Confidence scores are updated using the `absorb()` weighted-average method
   (defined in schema.py) so they change gradually, not abruptly.

This file is "stateless" — `merge_update()` and all the helpers are pure
functions.  They don't modify any object they receive; they only return new
objects.  This makes the code easier to test and reason about.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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
    _from_dict_confidence,
)


def merge_update(state: ArchitectureState, update: dict[str, Any]) -> ArchitectureState:
    """
    Additively merge an update payload into an existing state and return
    a brand-new ArchitectureState.  Nothing is ever deleted.

    Parameters
    ----------
    state  : The current architecture document (from the previous loop).
    update : A plain dict from the AI containing new information.  Expected
             keys include: "loop", "system_purpose", "components",
             "relationships", "decisions", "rejected_alternatives",
             "open_questions", "subsystems", "overall_confidence",
             "loop_note".  Any key can be absent — absent keys are skipped.

    Returns
    -------
    A new ArchitectureState with all the new information blended in.

    How the JSON round-trip deep-copy works
    ----------------------------------------
    We need a full independent copy of `state` before modifying it so the
    original stays unchanged.  The standard library `copy.deepcopy()` works
    for this, but we use a JSON round-trip instead because:
    1. It requires no extra imports beyond `json` (already needed).
    2. It's simple and predictable — only serialisable types survive.
    3. Any non-serialisable accident (e.g. a live file handle) would fail
       loudly rather than silently copying something unsafe.
    """
    import json  # json is re-imported here for clarity; already imported at module level

    # Serialise the whole current state to a JSON string, then parse it back
    # to a plain dict.  This gives us a completely independent deep copy.
    data: dict = json.loads(state.model_dump_json())

    # Determine the loop number for this update.
    # If the caller provides "loop" explicitly, use that.
    # Otherwise, just increment the current loop counter.
    loop = update.get("loop", state.macro_loop + 1)
    data["macro_loop"] = loop

    # Update top-level text fields if the update provides them.
    # We only overwrite if the new value is non-empty — we never blank things out.
    for field in ("system_purpose", "system_scope", "system_name"):
        if update.get(field):
            data[field] = update[field]

    # Merge each collection.
    # `setdefault("components", {})` ensures the key exists even if it was
    # missing from older JSON files — then we pass the dict to _merge_components
    # which modifies it in-place (within the plain dict, not the dataclass).
    _merge_components(
        data.setdefault("components", {}), update.get("components", []), loop
    )
    _merge_relationships(
        data.setdefault("relationships", {}), update.get("relationships", []), loop
    )
    _merge_decisions(
        data.setdefault("decisions", {}), update.get("decisions", []), loop
    )
    _merge_rejected(
        data.setdefault("rejected_alternatives", {}),
        update.get("rejected_alternatives", []),
        loop,
    )
    _merge_questions(
        data.setdefault("open_questions", {}), update.get("open_questions", []), loop
    )
    _merge_subsystems(
        data.setdefault("subsystems", {}), update.get("subsystems", []), loop
    )

    # Update overall document confidence if the AI provides a new score.
    # We use absorb() so the confidence shifts gradually, not abruptly.
    if "overall_confidence" in update:
        oc = _from_dict_confidence(data.get("overall_confidence"))
        data["overall_confidence"] = _to_dict(
            oc.absorb(update["overall_confidence"], note=f"loop {loop}")
        )

    # Append the loop note if one was provided.
    # Prefix it with the loop number so it's easy to find in history.
    if update.get("loop_note"):
        data.setdefault("loop_notes", []).append(f"[loop {loop}] {update['loop_note']}")

    # Stamp the new updated_at time before converting back to typed objects
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Convert the plain dict back into a proper typed ArchitectureState object
    return ArchitectureState._from_dict(data)


# ── helpers ─────────────────────────────────────────────────────────
# These small helper functions are used by all the _merge_* functions below.
# They're private (underscore prefix) because they're implementation details.


def _find_by_name(collection: dict, name: str) -> tuple[str | None, dict | None]:
    """
    Search a collection dict for an item whose name (or title) matches.
    The comparison is case-insensitive so "API Gateway" and "api gateway"
    are treated as the same component.

    Returns (key, item_dict) if found, or (None, None) if not found.

    This is how the merger decides "is this a NEW component, or an UPDATE
    to something we already know about?"

    Parameters
    ----------
    collection : A dict of {id: item_dict} (e.g. from data["components"]).
    name       : The name to search for.
    """
    nl = name.lower()
    for k, v in collection.items():
        # Items use either "name" (components, subsystems) or "title" (decisions)
        item_name = v.get("name", "") or v.get("title", "")
        if item_name.lower() == nl:
            return k, v
    return None, None


def _pop_confidence(raw: dict) -> tuple[float | None, str | None]:
    """
    Extract and remove the confidence fields from a raw update dict.

    The AI sends confidence data as two separate fields:
      "confidence_value": 0.8   (the new score)
      "confidence_note":  "Confirmed by research loop 47"  (optional)

    We "pop" them (read and remove) rather than "get" so that when we copy
    the remaining fields into the existing record, these special fields
    don't overwrite the structured ConfidenceScore object.

    Returns (confidence_value_or_None, confidence_note_or_None).
    """
    return raw.pop("confidence_value", None), raw.pop("confidence_note", None)


def _apply_confidence(ex: dict, cv: float | None, cn: str | None) -> None:
    """
    Update the "confidence" field of an existing item dict using absorb().

    This modifies `ex` in-place (it's a plain dict, not a dataclass).

    Parameters
    ----------
    ex : The existing item dict (already in the collection).
    cv : The new confidence value (0.0–1.0), or None to skip the update.
    cn : An optional note explaining why the confidence changed.
    """
    if cv is not None:
        # Reconstruct the existing ConfidenceScore from the dict,
        # absorb the new value into it, then store the result back as a dict
        old = _from_dict_confidence(ex.get("confidence"))
        ex["confidence"] = _to_dict(old.absorb(cv, cn))


# ── per-collection merges ────────────────────────────────────────────
# Each function below handles one collection type.
# They all share the same pattern:
#   1. For each item in the `items` list from the update:
#      a. Extract the confidence fields (which are handled specially).
#      b. Look for an existing item with the same name/title.
#      c. If not found → create a new item and add it to `col`.
#      d. If found → update the existing item's fields.
# All functions modify `col` (the collection dict) in-place.


def _merge_components(col: dict, items: list, loop: int) -> None:
    """
    Merge a list of component dicts from the update into the component collection.

    For a NEW component:
    - Create a fresh Component dataclass with first_seen_loop=loop.
    - If confidence_value was provided, override the default 0.5 with it.
    - Serialise to dict and store in col under the component's ID.

    For an EXISTING component (same name found):
    - Overwrite simple fields (description, subsystem, tags, metadata)
      only if the update provides non-empty values.
    - APPEND new responsibilities to the existing list (never replace it).
    - Update last_updated_loop.
    - Apply the new confidence score via absorb() if one was provided.

    Parameters
    ----------
    col   : The components dict from data["components"] — modified in-place.
    items : List of raw component dicts from the AI update payload.
    loop  : The current macro-loop number (recorded on new/updated items).
    """
    for raw in items:
        raw = dict(raw)  # make a mutable copy so we can pop fields safely
        cv, cn = _pop_confidence(raw)  # extract and remove confidence fields
        name = raw.get("name", "")
        ek, ex = _find_by_name(col, name)  # look for existing component

        if ex is None:
            # ── CREATE new component ──
            c = Component(
                name=name,
                description=raw.get("description", ""),
                responsibilities=raw.get("responsibilities", []),
                subsystem=raw.get("subsystem"),
                tags=raw.get("tags", []),
                metadata=raw.get("metadata", {}),
                first_seen_loop=loop,  # stamp when we first saw this
                last_updated_loop=loop,
            )
            if cv is not None:
                # Override the default confidence score if the AI specified one
                c.confidence = ConfidenceScore(
                    value=cv, evidence_count=1, notes=[cn] if cn else []
                )
            # Serialise to plain dict for storage (col holds plain dicts, not dataclasses)
            col[c.id] = _to_dict(c)
        else:
            # ── UPDATE existing component ──
            ex = dict(ex)  # make a mutable copy of the existing dict

            # Only overwrite simple scalar fields if the update has something new
            for f in ("description", "subsystem", "tags", "metadata"):
                if raw.get(f):
                    ex[f] = raw[f]

            # GROW the responsibilities list — never replace it
            # Only add new responsibilities that aren't already listed
            for r in raw.get("responsibilities", []):
                if r not in ex.get("responsibilities", []):
                    ex.setdefault("responsibilities", []).append(r)

            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)  # blend in the new confidence score
            col[ek] = ex  # write back the updated dict


def _merge_relationships(col: dict, items: list, loop: int) -> None:
    """
    Merge a list of relationship dicts from the update into the relationships collection.

    Relationships are identified by a "signature" of (source, target, kind).
    Two relationships are considered the same if all three match.  This is
    different from components (which match by name) because the same source
    might have multiple different kinds of relationship with the same target.

    The signature string looks like: "component_a::component_b::calls"

    Parameters
    ----------
    col   : The relationships dict — modified in-place.
    items : List of raw relationship dicts from the update.
    loop  : Current macro-loop number.
    """
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)

        # The source and target can be specified as IDs or as names
        src = raw.get("source_id") or raw.get("source_name", "")
        tgt = raw.get("target_id") or raw.get("target_name", "")
        kind = raw.get("kind", "depends_on")

        # Build a unique signature string for this relationship
        sig = f"{src}::{tgt}::{kind}"

        # Search for an existing relationship with the same signature
        ek = next(
            (
                k
                for k, v in col.items()
                if f"{v.get('source_id', '')}{v.get('source_name', '')}::"
                f"{v.get('target_id', '')}{v.get('target_name', '')}::{v.get('kind', '')}"
                == sig
            ),
            None,
        )

        if ek is None:
            # ── CREATE new relationship ──
            r = Relationship(
                source_id=src,
                target_id=tgt,
                kind=kind,
                description=raw.get("description", ""),
                interface_contract=raw.get("interface_contract", ""),
                first_seen_loop=loop,
                last_updated_loop=loop,
            )
            if cv is not None:
                r.confidence = ConfidenceScore(
                    value=cv, evidence_count=1, notes=[cn] if cn else []
                )
            col[r.id] = _to_dict(r)
        else:
            # ── UPDATE existing relationship ──
            ex = dict(col[ek])
            for f in ("description", "interface_contract"):
                if raw.get(f):
                    ex[f] = raw[f]
            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[ek] = ex


def _merge_decisions(col: dict, items: list, loop: int) -> None:
    """
    Merge a list of design decision dicts into the decisions collection.

    Decisions are matched by title (case-insensitive).  If the update
    changes a decision's status (e.g. from "proposed" to "accepted"),
    that change is applied.

    The `alternatives_considered` list is GROWN (not replaced) just like
    `responsibilities` in components — new alternatives are appended.

    Parameters
    ----------
    col   : The decisions dict — modified in-place.
    items : List of raw decision dicts from the update.
    loop  : Current macro-loop number.
    """
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)
        title = raw.get("title", "")
        ek, ex = _find_by_name(col, title)

        if ex is None:
            # ── CREATE new decision ──
            d = DesignDecision(
                title=title,
                description=raw.get("description", ""),
                rationale=raw.get("rationale", ""),
                status=raw.get("status", "proposed"),  # default to proposed
                subsystem=raw.get("subsystem"),
                alternatives_considered=raw.get("alternatives_considered", []),
                tags=raw.get("tags", []),
                first_seen_loop=loop,
                last_updated_loop=loop,
            )
            if cv is not None:
                d.confidence = ConfidenceScore(
                    value=cv, evidence_count=1, notes=[cn] if cn else []
                )
            col[d.id] = _to_dict(d)
        else:
            # ── UPDATE existing decision ──
            ex = dict(ex)
            for f in ("description", "rationale", "status", "subsystem", "tags"):
                if raw.get(f):
                    ex[f] = raw[f]
            # GROW the alternatives list, same pattern as responsibilities
            for alt in raw.get("alternatives_considered", []):
                if alt not in ex.get("alternatives_considered", []):
                    ex.setdefault("alternatives_considered", []).append(alt)
            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[ek] = ex


def _merge_rejected(col: dict, items: list, loop: int) -> None:
    """
    Merge a list of rejected alternative dicts into the collection.

    Rejected alternatives are NEVER updated once written — they're
    immutable historical records.  If we see a title we already have,
    we just skip it.  Only genuinely new rejections are added.

    (There's no status to update and no confidence to track — a rejection
    is a final, permanent record of a decision that was considered and ruled out.)

    Parameters
    ----------
    col   : The rejected_alternatives dict — modified in-place.
    items : List of raw rejected alternative dicts from the update.
    loop  : Current macro-loop number (recorded as loop_rejected).
    """
    for raw in items:
        raw = dict(raw)
        title = raw.get("title", "")
        _, ex = _find_by_name(col, title)
        if ex is None:
            # Only add — never update.  The underscore (_) in `_, ex` means
            # "I don't need the key here, just the value".
            r = RejectedAlternative(
                title=title,
                description=raw.get("description", ""),
                rejection_reason=raw.get("rejection_reason", ""),
                related_decision_id=raw.get("related_decision_id"),
                loop_rejected=loop,
            )
            col[r.id] = _to_dict(r)
        # If it already exists, we intentionally do nothing — rejections are final.


def _merge_questions(col: dict, items: list, loop: int) -> None:
    """
    Merge a list of open question dicts into the questions collection.

    Questions are matched by their text (case-insensitive).  If the update
    marks a previously open question as resolved (resolved=True), the
    resolution and resolved_loop are recorded.

    Unlike components (matched by name field), questions are matched by
    the full question text because questions don't have a separate "name".

    Parameters
    ----------
    col   : The open_questions dict — modified in-place.
    items : List of raw question dicts from the update.
    loop  : Current macro-loop number.
    """
    for raw in items:
        raw = dict(raw)
        text = raw.get("question", "")

        # Search for an existing question with the same text
        ek = next(
            (
                k
                for k, v in col.items()
                if v.get("question", "").lower() == text.lower()
            ),
            None,
        )

        if ek is None:
            # ── CREATE new question ──
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
            # ── UPDATE existing question ──
            # Allow ANY of these fields to be updated, including resolved/resolution
            # This is how a question gets "answered" in a later loop.
            ex = dict(col[ek])
            for f in (
                "context",
                "subsystem",
                "priority",
                "resolved",
                "resolution",
                "resolved_loop",
            ):
                # Use `is not None` (not just `if raw.get(f)`) because False and 0
                # are valid values that we want to store (e.g. resolved=False)
                if raw.get(f) is not None:
                    ex[f] = raw[f]
            col[ek] = ex


def _merge_subsystems(col: dict, items: list, loop: int) -> None:
    """
    Merge a list of subsystem summary dicts into the subsystems collection.

    Subsystems use their *name* as the dict key (not a UUID) so the
    collection looks like {"auth": {...}, "data-pipeline": {...}}.

    The `components` list inside each subsystem is GROWN (not replaced) —
    new component names are appended to the existing list.

    Parameters
    ----------
    col   : The subsystems dict (keyed by subsystem name) — modified in-place.
    items : List of raw subsystem dicts from the update.
    loop  : Current macro-loop number.
    """
    for raw in items:
        raw = dict(raw)
        cv, cn = _pop_confidence(raw)
        name = raw.get("name", "")
        _, ex = _find_by_name(col, name)

        if ex is None:
            # ── CREATE new subsystem ──
            s = SubsystemSummary(
                name=name,
                purpose=raw.get("purpose", ""),
                components=raw.get("components", []),
                design_notes=raw.get("design_notes", ""),
                last_updated_loop=loop,
            )
            if cv is not None:
                s.confidence = ConfidenceScore(
                    value=cv, evidence_count=1, notes=[cn] if cn else []
                )
            # Note: subsystems use the name as key, not the ID, for readability
            col[name] = _to_dict(s)
        else:
            # ── UPDATE existing subsystem ──
            ex = dict(ex)
            for f in ("purpose", "design_notes"):
                if raw.get(f):
                    ex[f] = raw[f]

            # cn_ is used here as the loop variable name to avoid shadowing
            # the outer `cn` variable (which holds the confidence note)
            for cn_ in raw.get("components", []):
                if cn_ not in ex.get("components", []):
                    ex.setdefault("components", []).append(cn_)

            ex["last_updated_loop"] = loop
            _apply_confidence(ex, cv, cn)
            col[name] = ex
