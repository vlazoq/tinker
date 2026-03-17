"""
webui/core.py
─────────────
Shared data-access helpers used by the FastAPI web UI.
Reads/writes SQLite databases, JSON config files, and feature flags.
All database calls use asyncio.to_thread() to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ── File paths (all overridable via env vars) ─────────────────────────────────
BASE_DIR   = Path(os.getenv("TINKER_BASE_DIR", Path(__file__).parent.parent))
TASKS_DB   = Path(os.getenv("TINKER_TASK_DB",       BASE_DIR / "tinker_tasks_engine.sqlite"))
DLQ_DB     = Path(os.getenv("TINKER_DLQ_PATH",      BASE_DIR / "tinker_dlq.sqlite"))
AUDIT_DB   = Path(os.getenv("TINKER_AUDIT_LOG_PATH",BASE_DIR / "tinker_audit.sqlite"))
BACKUP_DIR = Path(os.getenv("TINKER_BACKUP_DIR",    BASE_DIR / "tinker_backups"))
FLAGS_FILE = Path(os.getenv("TINKER_FLAGS_FILE",     BASE_DIR / "tinker_flags.json"))
CONFIG_FILE= Path(os.getenv("TINKER_WEBUI_CONFIG",   BASE_DIR / "tinker_webui_config.json"))
STATE_FILE      = Path(os.getenv("TINKER_STATE_PATH",     BASE_DIR / "tinker_state.json"))
HEALTH_URL      = os.getenv("TINKER_HEALTH_URL", "http://localhost:8081")
GRUB_QUEUE_DB   = Path(os.getenv("GRUB_QUEUE_DB",      BASE_DIR / "grub_queue.sqlite"))
GRUB_ARTIFACTS  = Path(os.getenv("GRUB_ARTIFACTS_DIR", BASE_DIR / "grub_artifacts"))

# ── Default flag values (mirrors features/flags.py) ──────────────────────────
FLAG_DEFAULTS: dict[str, bool] = {
    "researcher_calls": True, "meso_synthesis": True, "macro_synthesis": True,
    "stagnation_detection": True, "context_assembly": True,
    "circuit_breakers": True, "distributed_locking": True,
    "idempotency_cache": True, "rate_limiting": True, "backpressure": True,
    "structured_logging": True, "tracing": True, "audit_log": True,
    "sla_tracking": True, "health_endpoints": True,
    "slack_alerts": True, "webhook_alerts": True,
    "auto_backup": False, "memory_compression": True,
    "ab_testing": False, "lineage_tracking": False,
}

FLAG_DESCRIPTIONS: dict[str, str] = {
    "researcher_calls": "Architect knowledge gap research",
    "meso_synthesis": "Subsystem-level synthesis",
    "macro_synthesis": "Architectural snapshot commits",
    "stagnation_detection": "Anti-stagnation monitor",
    "context_assembly": "Prior context fetching",
    "circuit_breakers": "Circuit breakers for external services",
    "distributed_locking": "Redis distributed locks",
    "idempotency_cache": "Idempotency key caching",
    "rate_limiting": "AI call rate limiting",
    "backpressure": "Queue backpressure",
    "structured_logging": "JSON structured logging",
    "tracing": "Span tracing",
    "audit_log": "Immutable audit log",
    "sla_tracking": "SLA measurement",
    "health_endpoints": "HTTP health server",
    "slack_alerts": "Slack alerting",
    "webhook_alerts": "Webhook alerting",
    "auto_backup": "Auto-backup (off by default)",
    "memory_compression": "Automatic memory compression",
    "ab_testing": "A/B prompt variant testing (experimental)",
    "lineage_tracking": "Data lineage graph tracking (experimental)",
}

FLAG_GROUPS: dict[str, list[str]] = {
    "Core Loop":    ["researcher_calls", "meso_synthesis", "macro_synthesis",
                     "stagnation_detection", "context_assembly"],
    "Resilience":   ["circuit_breakers", "distributed_locking", "idempotency_cache",
                     "rate_limiting", "backpressure"],
    "Observability":["structured_logging", "tracing", "audit_log",
                     "sla_tracking", "health_endpoints"],
    "Alerting":     ["slack_alerts", "webhook_alerts"],
    "Storage":      ["auto_backup", "memory_compression"],
    "Experimental": ["ab_testing", "lineage_tracking"],
}

# ── Config schema (mirrors OrchestratorConfig + StagnationMonitorConfig) ──────
ORCH_CONFIG_SCHEMA: dict[str, Any] = {
    "micro_loop": {
        "label": "Micro Loop",
        "fields": {
            "meso_trigger_count":            {"type": "int",   "default": 5,      "min": 1,   "label": "Meso Trigger Count",         "help": "Successful micro loops before meso synthesis"},
            "max_consecutive_failures":      {"type": "int",   "default": 3,      "min": 1,   "label": "Max Consecutive Failures",   "help": "Failures before backoff sleep"},
            "failure_backoff_seconds":       {"type": "float", "default": 10.0,   "min": 0.0, "label": "Failure Backoff (s)"},
            "micro_loop_idle_seconds":       {"type": "float", "default": 0.0,    "min": 0.0, "label": "Idle Delay (s)",             "help": "0 = run flat-out"},
            "max_researcher_calls_per_loop": {"type": "int",   "default": 3,      "min": 0,   "label": "Max Researcher Calls/Loop"},
            "context_max_artifacts":         {"type": "int",   "default": 10,     "min": 1,   "label": "Context Max Artifacts"},
        },
    },
    "meso_loop": {
        "label": "Meso Loop",
        "fields": {
            "meso_min_artifacts": {"type": "int", "default": 2, "min": 1, "label": "Min Artifacts for Synthesis"},
        },
    },
    "macro_loop": {
        "label": "Macro Loop",
        "fields": {
            "macro_interval_seconds": {"type": "float", "default": 14400.0, "min": 1.0,
                                       "label": "Macro Interval (s)", "help": "Default 14400 = 4 hours"},
        },
    },
    "timeouts": {
        "label": "Timeouts",
        "fields": {
            "architect_timeout":   {"type": "float", "default": 120.0, "min": 1.0, "label": "Architect Timeout (s)"},
            "critic_timeout":      {"type": "float", "default": 60.0,  "min": 1.0, "label": "Critic Timeout (s)"},
            "synthesizer_timeout": {"type": "float", "default": 180.0, "min": 1.0, "label": "Synthesizer Timeout (s)"},
            "tool_timeout":        {"type": "float", "default": 30.0,  "min": 1.0, "label": "Tool Timeout (s)"},
        },
    },
}

STAGNATION_CONFIG_SCHEMA: dict[str, Any] = {
    "semantic_loop":       {"label": "Semantic Loop",       "fields": {"window_size": {"type":"int","default":6,"min":1,"label":"Window Size"}, "similarity_threshold": {"type":"float","default":0.92,"min":0.0,"label":"Similarity Threshold"}, "min_breach_count": {"type":"int","default":3,"min":1,"label":"Min Breach Count"}}},
    "subsystem_fixation":  {"label": "Subsystem Fixation",  "fields": {"window_size": {"type":"int","default":10,"min":1,"label":"Window Size"}, "fixation_threshold": {"type":"float","default":0.70,"min":0.0,"label":"Fixation Threshold"}}},
    "critique_collapse":   {"label": "Critique Collapse",   "fields": {"window_size": {"type":"int","default":8,"min":1,"label":"Window Size"}, "collapse_threshold": {"type":"float","default":0.85,"min":0.0,"label":"Collapse Threshold"}, "min_samples": {"type":"int","default":4,"min":1,"label":"Min Samples"}}},
    "research_saturation": {"label": "Research Saturation", "fields": {"window_size": {"type":"int","default":6,"min":1,"label":"Window Size"}, "overlap_threshold": {"type":"float","default":0.60,"min":0.0,"label":"Overlap Threshold"}, "min_url_count": {"type":"int","default":3,"min":1,"label":"Min URL Count"}}},
    "task_starvation":     {"label": "Task Starvation",     "fields": {"low_depth_threshold": {"type":"int","default":3,"min":1,"label":"Low Depth Threshold"}, "window_size": {"type":"int","default":5,"min":1,"label":"Window Size"}, "consecutive_negative_threshold": {"type":"int","default":3,"min":1,"label":"Consecutive Negative"}}},
}

TASK_TYPES    = ["design", "research", "critique", "synthesis", "exploration", "validation"]
SUBSYSTEMS    = ["model_client", "memory_manager", "tool_layer", "agent_prompts", "task_engine",
                 "context_assembler", "orchestrator", "arch_state_manager", "anti_stagnation",
                 "observability", "cross_cutting"]

# ── SQLite helpers ────────────────────────────────────────────────────────────

def db_query_sync(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        con.close()
        return rows
    except Exception:
        return []

def db_execute_sync(db_path: Path, sql: str, params: tuple = ()) -> bool:
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.execute(sql, params)
        con.commit()
        con.close()
        return True
    except Exception:
        return False

async def db_query(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    return await asyncio.to_thread(db_query_sync, db_path, sql, params)

async def db_execute(db_path: Path, sql: str, params: tuple = ()) -> bool:
    return await asyncio.to_thread(db_execute_sync, db_path, sql, params)

# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}

def save_config(data: dict) -> None:
    data["_saved_at"] = datetime.now(timezone.utc).isoformat()
    CONFIG_FILE.write_text(json.dumps(data, indent=2))

# ── Feature flags helpers ─────────────────────────────────────────────────────

def load_flags() -> dict[str, bool]:
    flags = dict(FLAG_DEFAULTS)
    if FLAGS_FILE.exists():
        try:
            flags.update({k: bool(v) for k, v in json.loads(FLAGS_FILE.read_text()).items()})
        except Exception:
            pass
    for key in list(flags):
        env = os.getenv(f"TINKER_FLAG_{key.upper()}")
        if env is not None:
            flags[key] = env.lower() not in ("false", "0", "no", "off", "disabled")
    return flags

def save_flags(flags: dict[str, bool]) -> None:
    FLAGS_FILE.write_text(json.dumps(flags, indent=2))

# ── State / health helpers ────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def _dlq_stats_sync() -> dict:
    """Read DLQ counts from SQLite — used as fallback when health server is offline."""
    stats = {"pending": 0, "resolved": 0, "discarded": 0, "total": 0}
    for row in db_query_sync(DLQ_DB, "SELECT status, COUNT(*) n FROM dlq_items GROUP BY status"):
        stats[row["status"]] = row["n"]
        stats["total"] += row["n"]
    return stats


def _state_to_health(state: dict) -> dict:
    """
    Transform tinker_state.json (OrchestratorState.to_dict() output) into the
    same shape as the /health HTTP endpoint so the React dashboard always gets
    a consistent structure regardless of whether the orchestrator is running.

    State file keys:  uptime_seconds, status, current_level, current_task_id,
                      totals.{micro,meso,macro,consecutive_failures},
                      subsystem_micro_counts, micro_history, ...
    Health endpoint:  loops.{micro,meso,macro,consecutive_failures,
                              current_level,stagnation_events},
                      dlq.{pending,resolved,discarded,total},
                      circuit_breakers, memory, rate_limiters, sla
    """
    totals = state.get("totals", {})
    # Derive last critic score from most recent micro history entry
    micro_hist = state.get("micro_history", [])
    last_critic = micro_hist[-1].get("critic_score") if micro_hist else None
    # stagnation_events_total is not written to the state file; count from audit
    stagnation = sum(
        r["n"] for r in db_query_sync(
            AUDIT_DB,
            "SELECT COUNT(*) n FROM audit_events WHERE event_type='stagnation_detected'"
        )
    ) if AUDIT_DB.exists() else None

    return {
        "online": False,
        "from_state_file": True,
        "status": state.get("status", "unknown"),
        "uptime_seconds": state.get("uptime_seconds"),
        "loops": {
            "micro":               totals.get("micro", 0),
            "meso":                totals.get("meso", 0),
            "macro":               totals.get("macro", 0),
            "consecutive_failures":totals.get("consecutive_failures", 0),
            "current_level":       state.get("current_level", "idle"),
            "stagnation_events":   stagnation,
        },
        "current_task_id":      state.get("current_task_id"),
        "current_subsystem":    state.get("current_subsystem"),
        "last_critic_score":    last_critic,
        "subsystem_micro_counts": state.get("subsystem_micro_counts", {}),
        "dlq":             _dlq_stats_sync(),
        "circuit_breakers": {},   # only available from live health endpoint
        "memory":          {},
        "rate_limiters":   {},
        "sla":             {},
    }


async def fetch_health() -> dict:
    """
    Try the orchestrator's /health endpoint first (port 8080).
    Falls back to reading tinker_state.json and reshaping it into the same
    structure so callers never need to handle two different shapes.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{HEALTH_URL}/health")
            if resp.status_code == 200:
                return {"online": True, **resp.json()}
    except Exception:
        pass
    state = load_state()
    if not state:
        return {"online": False, "from_state_file": False,
                "loops": {}, "dlq": {}, "circuit_breakers": {}, "memory": {}}
    return _state_to_health(state)

# ── Backup helpers ────────────────────────────────────────────────────────────

def list_backups() -> list[dict]:
    result = []
    if not BACKUP_DIR.exists():
        return result
    for d in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest: dict = {}
        mp = d / "manifest.json"
        if mp.exists():
            try:
                manifest = json.loads(mp.read_text())
            except Exception:
                pass
        total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        result.append({
            "id": d.name,
            "created_at": manifest.get("created_at", d.name),
            "size_mb": round(total / 1_000_000, 2),
            "file_count": len(list(d.rglob("*"))),
            "errors": manifest.get("errors", []),
        })
    return result

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

# ── Grub status helpers ───────────────────────────────────────────────────────

def fetch_grub_status_sync() -> dict:
    """
    Read Grub pipeline status from SQLite DBs and grub_artifacts/.
    Returns a dict safe for JSON serialisation.
    """
    import re

    # ── Tinker tasks of type implementation / review ──────────────────────────
    task_rows = db_query_sync(
        TASKS_DB,
        "SELECT type, status, COUNT(*) as n "
        "FROM tasks WHERE type IN ('implementation','review') "
        "GROUP BY type, status",
    )
    task_counts: dict[str, dict[str, int]] = {}
    for r in task_rows:
        task_counts.setdefault(r["type"], {})[r["status"]] = r["n"]

    # ── Grub queue stats ──────────────────────────────────────────────────────
    queue_rows = db_query_sync(
        GRUB_QUEUE_DB,
        "SELECT status, COUNT(*) as n FROM grub_queue GROUP BY status",
    ) if GRUB_QUEUE_DB.exists() else []
    queue_counts = {r["status"]: r["n"] for r in queue_rows}

    # ── Recent artifacts ──────────────────────────────────────────────────────
    artifacts: list[dict] = []
    if GRUB_ARTIFACTS.exists():
        files = sorted(
            GRUB_ARTIFACTS.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:10]
        for f in files:
            score: float | None = None
            subsystem = ""
            try:
                text = f.read_text(encoding="utf-8")
                for line in text.splitlines()[:20]:
                    m = re.search(r"score[:\s]+(\d+(?:\.\d+)?)", line, re.IGNORECASE)
                    if m:
                        v = float(m.group(1))
                        score = v / 10.0 if v > 1.0 else v
                        break
                # Try to extract subsystem from filename or content
                ss_m = re.search(r"subsystem[:\s]+(\w+)", text, re.IGNORECASE)
                if ss_m:
                    subsystem = ss_m.group(1)
            except Exception:
                pass
            artifacts.append({
                "name":       f.stem,
                "mtime":      datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "score":      score,
                "subsystem":  subsystem,
                "size_bytes": f.stat().st_size,
            })

    return {
        "task_counts":    task_counts,
        "queue_counts":   queue_counts,
        "artifacts":      artifacts,
        "queue_db_exists": GRUB_QUEUE_DB.exists(),
        "artifacts_dir_exists": GRUB_ARTIFACTS.exists(),
        "artifacts_dir":  str(GRUB_ARTIFACTS),
    }


async def fetch_grub_status() -> dict:
    return await asyncio.to_thread(fetch_grub_status_sync)
