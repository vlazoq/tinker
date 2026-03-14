"""
tinker/task_engine/generator.py
────────────────────────────────
TaskGenerator

Parses Architect JSON outputs and extracts well-formed Task objects
from the ``candidate_tasks`` field.

Expected Architect output shape
────────────────────────────────
{
  "architect_version": "1.0",
  "subsystem": "memory_manager",
  "outputs": ["artefact:mem_schema_v3"],
  "candidate_tasks": [
    {
      "title": "Evaluate vector index strategies",
      "description": "Compare HNSW vs IVF for long-term memory retrieval...",
      "type": "research",
      "subsystem": "memory_manager",
      "dependencies": [],
      "confidence_gap": 0.8,
      "tags": ["memory", "performance"],
      "metadata": {}
    },
    ...
  ]
}

Any missing or invalid field is silently replaced with a safe default
so that a partially-malformed Architect output never crashes the engine.
"""

from __future__ import annotations

import logging
from typing import Any

from .schema import (
    Subsystem,
    Task,
    TaskStatus,
    TaskType,
)

log = logging.getLogger(__name__)

# Fallbacks used when Architect output is missing a field
_TYPE_MAP: dict[str, TaskType] = {t.value: t for t in TaskType}
_SUBSYSTEM_MAP: dict[str, Subsystem] = {s.value: s for s in Subsystem}


class TaskGenerator:
    """
    Converts raw Architect JSON into a list of Task objects.

    Usage
    ─────
    generator = TaskGenerator()
    tasks = generator.from_architect_output(architect_json_dict, parent_task_id)
    """

    def __init__(
        self,
        default_confidence_gap: float = 0.5,
        max_tasks_per_output: int = 10,
    ):
        self.default_confidence_gap = default_confidence_gap
        self.max_tasks_per_output = max_tasks_per_output

    # ── Public API ────────────────────────────────────────────────────────

    def from_architect_output(
        self,
        output: dict[str, Any],
        parent_task_id: str | None = None,
    ) -> list[Task]:
        """
        Parse ``output`` and return a list of new PENDING Tasks.

        Parameters
        ----------
        output:          The full Architect JSON dict.
        parent_task_id:  The task that triggered this Architect run.
        """
        raw_candidates = output.get("candidate_tasks", [])
        if not isinstance(raw_candidates, list):
            log.warning("candidate_tasks is not a list; got %s", type(raw_candidates))
            return []

        # Infer the source subsystem for default-filling
        source_subsystem = self._parse_subsystem(
            output.get("subsystem", Subsystem.CROSS_CUTTING.value)
        )

        tasks: list[Task] = []
        for raw in raw_candidates[: self.max_tasks_per_output]:
            try:
                task = self._parse_candidate(raw, parent_task_id, source_subsystem)
                tasks.append(task)
                log.debug("Generated task '%s' (%s)", task.title, task.id)
            except Exception as exc:
                log.error("Failed to parse candidate task %s: %s", raw, exc)

        log.info(
            "TaskGenerator produced %d task(s) from Architect output", len(tasks)
        )
        return tasks

    def make_exploration_task(
        self,
        title: str,
        description: str,
        subsystem: Subsystem = Subsystem.CROSS_CUTTING,
    ) -> Task:
        """
        Manually create a random exploration task.
        Used by the anti-stagnation / exploration-slot logic.
        """
        return Task(
            title=title,
            description=description,
            type=TaskType.EXPLORATION,
            subsystem=subsystem,
            status=TaskStatus.PENDING,
            confidence_gap=0.9,   # Exploration tasks are high-uncertainty by design
            is_exploration=True,
            tags=["exploration", "auto-generated"],
        )

    # ── Parsing helpers ───────────────────────────────────────────────────

    def _parse_candidate(
        self,
        raw: dict[str, Any],
        parent_id: str | None,
        source_subsystem: Subsystem,
    ) -> Task:
        title       = str(raw.get("title", "Untitled task")).strip() or "Untitled task"
        description = str(raw.get("description", "")).strip()
        task_type   = self._parse_type(raw.get("type", TaskType.DESIGN.value))
        subsystem   = self._parse_subsystem(raw.get("subsystem", source_subsystem.value))
        deps        = self._parse_str_list(raw.get("dependencies", []))
        tags        = self._parse_str_list(raw.get("tags", []))
        metadata    = raw.get("metadata", {}) if isinstance(raw.get("metadata"), dict) else {}
        conf_gap    = self._clamp_float(
            raw.get("confidence_gap", self.default_confidence_gap), 0.0, 1.0
        )

        return Task(
            parent_id=parent_id,
            title=title,
            description=description,
            type=task_type,
            subsystem=subsystem,
            status=TaskStatus.PENDING,
            dependencies=deps,
            confidence_gap=conf_gap,
            tags=tags,
            metadata=metadata,
        )

    @staticmethod
    def _parse_type(raw: Any) -> TaskType:
        if isinstance(raw, TaskType):
            return raw
        val = str(raw).strip().lower()
        if val in _TYPE_MAP:
            return _TYPE_MAP[val]
        log.warning("Unknown task type '%s', defaulting to DESIGN", raw)
        return TaskType.DESIGN

    @staticmethod
    def _parse_subsystem(raw: Any) -> Subsystem:
        if isinstance(raw, Subsystem):
            return raw
        val = str(raw).strip().lower()
        if val in _SUBSYSTEM_MAP:
            return _SUBSYSTEM_MAP[val]
        log.warning("Unknown subsystem '%s', defaulting to CROSS_CUTTING", raw)
        return Subsystem.CROSS_CUTTING

    @staticmethod
    def _parse_str_list(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if x]

    @staticmethod
    def _clamp_float(val: Any, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(val)))
        except (TypeError, ValueError):
            return (lo + hi) / 2
