"""Task queue and Dead Letter Queue endpoints."""

from fastapi import APIRouter, Request

from ui.core import (
    DLQ_DB,
    SUBSYSTEMS,
    TASK_TYPES,
    TASKS_DB,
    db_execute,
    db_query,
    new_id,
    now_iso,
)

router = APIRouter()


# ── Task Queue ──────────────────────────────────────────────────────────────


@router.get("/api/tasks")
async def api_tasks():
    tasks = await db_query(
        TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score, "
        "created_at, attempt_count, is_exploration, description "
        "FROM tasks ORDER BY priority_score DESC, created_at ASC LIMIT 200",
    )
    stats_rows = await db_query(
        TASKS_DB, "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
    )
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {
        "tasks": tasks,
        "stats": stats,
        "task_types": TASK_TYPES,
        "subsystems": SUBSYSTEMS,
    }


@router.post("/api/tasks/inject")
async def api_tasks_inject(request: Request):
    body = await request.json()
    task_id = new_id()
    ts = now_iso()
    ok = await db_execute(
        TASKS_DB,
        """INSERT INTO tasks
           (id, title, description, type, subsystem, status,
            confidence_gap, is_exploration, created_at, updated_at,
            priority_score, staleness_hours, dependency_depth,
            last_subsystem_work_hours, attempt_count,
            dependencies, outputs, tags, metadata)
           VALUES (?,?,?,?,?,'pending',?,?,?,?,0.5,0.0,0,0.0,0,'[]','[]','[]','{}')""",
        (
            task_id,
            body.get("title", "Untitled"),
            body.get("description", ""),
            body.get("type", "design"),
            body.get("subsystem", "cross_cutting"),
            float(body.get("confidence_gap", 0.5)),
            1 if body.get("is_exploration") else 0,
            ts,
            ts,
        ),
    )
    return {"ok": ok, "id": task_id}


# ── Dead Letter Queue ───────────────────────────────────────────────────────


@router.get("/api/dlq")
async def api_dlq():
    items = await db_query(
        DLQ_DB,
        "SELECT id, operation, error, status, created_at, retry_count, notes "
        "FROM dlq_items ORDER BY created_at DESC LIMIT 100",
    )
    stats_rows = await db_query(
        DLQ_DB, "SELECT status, COUNT(*) as count FROM dlq_items GROUP BY status"
    )
    stats = {r["status"]: r["count"] for r in stats_rows}
    return {"items": items, "stats": stats}


@router.post("/api/dlq/{item_id}/resolve")
async def api_dlq_resolve(item_id: str, request: Request):
    body = await request.json()
    ts = now_iso()
    ok = await db_execute(
        DLQ_DB,
        "UPDATE dlq_items SET status='resolved', resolved_at=?, updated_at=?, notes=? WHERE id=?",
        (ts, ts, body.get("notes", "Resolved via web UI"), item_id),
    )
    return {"ok": ok}


@router.post("/api/dlq/{item_id}/discard")
async def api_dlq_discard(item_id: str, request: Request):
    body = await request.json()
    ts = now_iso()
    ok = await db_execute(
        DLQ_DB,
        "UPDATE dlq_items SET status='discarded', resolved_at=?, updated_at=?, notes=? WHERE id=?",
        (ts, ts, body.get("notes", "Discarded via web UI"), item_id),
    )
    return {"ok": ok}
