"""
ui/web/routes/workflow.py
─────────────────────────
Live workflow visualization — serves the orchestrator's current state
as a Mermaid flowchart diagram that the dashboard renders in real time.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger("tinker.web.workflow")


def _read_state() -> dict:
    """Read the latest orchestrator state from tinker_state.json."""
    state_file = Path(os.getenv("TINKER_STATE_FILE", "tinker_state.json"))
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _build_mermaid(state: dict) -> str:
    """Convert orchestrator state dict into a Mermaid flowchart string."""
    lines = ["graph TD"]

    # Core loop nodes
    lines.append('    START(["🚀 Problem Input"]) --> MACRO["♻️ Macro Loop"]')
    lines.append('    MACRO --> MESO["🔄 Meso Loop"]')
    lines.append('    MESO --> TASK_GEN["📋 Task Generation"]')
    lines.append('    TASK_GEN --> MICRO["⚡ Micro Loop"]')
    lines.append('    MICRO --> ARCHITECT["🏗️ Architect"]')
    lines.append('    ARCHITECT --> CRITIC["🔍 Critic / Judge"]')
    lines.append('    CRITIC --> |"score >= threshold"| SYNTH["✨ Synthesizer"]')
    lines.append('    CRITIC --> |"score < threshold"| REFINE["🔧 Refine"]')
    lines.append('    REFINE --> ARCHITECT')
    lines.append('    SYNTH --> STAG{"🔎 Stagnation?"}')
    lines.append('    STAG --> |No| MESO')
    lines.append('    STAG --> |Yes| INTERVENE["⚠️ Intervention"]')
    lines.append('    INTERVENE --> MESO')

    # External integrations
    lines.append('    MICRO -.-> RESEARCH["🌐 Research"]')
    lines.append('    MICRO -.-> TOOLS["🔧 Tools"]')
    lines.append('    CRITIC -.-> HUMAN["👤 Human Judge"]')
    lines.append('    SYNTH -.-> MEMORY["💾 Memory"]')
    lines.append('    SYNTH -.-> WEBHOOK["📡 Webhooks"]')

    # Style the current active node based on state
    loops = state.get("loops", {})
    current_level = loops.get("current_level", "")

    if current_level == "micro":
        lines.append('    style MICRO fill:#4CAF50,color:#fff,stroke:#333')
    elif current_level == "meso":
        lines.append('    style MESO fill:#4CAF50,color:#fff,stroke:#333')
    elif current_level == "macro":
        lines.append('    style MACRO fill:#4CAF50,color:#fff,stroke:#333')

    # Highlight stagnation if detected
    stag_events = loops.get("stagnation_events", 0)
    if stag_events and int(stag_events) > 0:
        lines.append('    style STAG fill:#FF9800,color:#fff,stroke:#333')

    return "\n".join(lines)


@router.get("/api/workflow")
async def get_workflow():
    """Return the current workflow state as a Mermaid diagram string."""
    state = _read_state()
    mermaid = _build_mermaid(state)

    loops = state.get("loops", {})

    return {
        "mermaid": mermaid,
        "current_level": loops.get("current_level", "idle"),
        "micro_count": loops.get("micro", 0),
        "meso_count": loops.get("meso", 0),
        "macro_count": loops.get("macro", 0),
        "stagnation_events": loops.get("stagnation_events", 0),
    }


@router.get("/api/workflow/task-graph")
async def get_task_graph():
    """Return a Mermaid diagram of active tasks and their statuses."""
    state = _read_state()
    tasks = state.get("tasks", {})

    lines = ["graph LR"]

    if not tasks:
        lines.append('    EMPTY["No active tasks"]')
        return {"mermaid": "\n".join(lines), "task_count": 0}

    # Group tasks by status
    by_status = {}
    for tid, info in tasks.items():
        if isinstance(info, dict):
            status = info.get("status", "unknown")
        else:
            status = str(info)
        by_status.setdefault(status, []).append((tid, info))

    status_styles = {
        "active": "fill:#4CAF50,color:#fff",
        "pending": "fill:#2196F3,color:#fff",
        "complete": "fill:#9E9E9E,color:#fff",
        "failed": "fill:#f44336,color:#fff",
    }

    node_idx = 0
    for status, items in by_status.items():
        for tid, info in items:
            short_id = tid[:8] if len(tid) > 8 else tid
            label = info.get("title", short_id) if isinstance(info, dict) else short_id
            node_name = f"T{node_idx}"
            lines.append(f'    {node_name}["{label}"]')
            style = status_styles.get(status, "fill:#607D8B,color:#fff")
            lines.append(f'    style {node_name} {style}')
            node_idx += 1

    return {"mermaid": "\n".join(lines), "task_count": node_idx}
