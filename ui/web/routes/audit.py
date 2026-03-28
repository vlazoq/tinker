"""Audit log and error detail endpoints."""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ui.core import AUDIT_DB, DLQ_DB, db_query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/audit")
async def api_audit(
    event_type: str = "",
    actor: str = "",
    trace_id: str = "",
    page: int = 1,
    limit: int = 50,
):
    offset = (page - 1) * limit
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if actor:
        conditions.append("actor = ?")
        params.append(actor)
    if trace_id:
        conditions.append("trace_id = ?")
        params.append(trace_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    items = await db_query(
        AUDIT_DB,
        f"SELECT id, event_type, actor, resource, outcome, trace_id, created_at, details "
        f"FROM audit_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    types_rows = await db_query(
        AUDIT_DB, "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type"
    )
    return {
        "items": items,
        "event_types": [r["event_type"] for r in types_rows],
        "page": page,
        "has_next": len(items) == limit,
    }


@router.get("/api/errors/recent")
async def api_errors_recent(limit: int = 20):
    """Return the most recent DLQ entries."""
    limit = max(1, min(limit, 200))
    items = await db_query(
        DLQ_DB,
        "SELECT id, operation, error, status, retry_count, "
        "created_at, updated_at, resolved_at, notes "
        "FROM dlq_items ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return {"items": items, "count": len(items)}


@router.get("/api/errors/{trace_id}")
async def api_error_detail(trace_id: str):
    """Return detailed error information for a given trace ID."""
    dlq_entries = await db_query(
        DLQ_DB,
        "SELECT id, operation, error, status, retry_count, "
        "created_at, updated_at, resolved_at, notes "
        "FROM dlq_items WHERE id = ?",
        (trace_id,),
    )

    try:
        trace_matches = await db_query(
            DLQ_DB,
            "SELECT id, operation, error, status, retry_count, "
            "created_at, updated_at, resolved_at, notes "
            "FROM dlq_items WHERE trace_id = ?",
            (trace_id,),
        )
        seen_ids = {e["id"] for e in dlq_entries}
        for row in trace_matches:
            if row["id"] not in seen_ids:
                dlq_entries.append(row)
    except Exception as exc:
        logger.debug("DLQ trace_id lookup failed (non-fatal): %s", exc)

    if not dlq_entries:
        return JSONResponse(
            {"error": "Not found", "detail": f"No DLQ entry for trace_id '{trace_id}'."},
            status_code=404,
        )

    audit_events = await db_query(
        AUDIT_DB,
        "SELECT id, event_type, actor, resource, outcome, trace_id, "
        "created_at, details "
        "FROM audit_events WHERE trace_id = ? ORDER BY created_at ASC",
        (trace_id,),
    )

    return {
        "trace_id": trace_id,
        "dlq_entries": dlq_entries,
        "audit_events": audit_events,
        "dlq_count": len(dlq_entries),
        "audit_count": len(audit_events),
    }
