"""
Artifact Writer Tool
Writes structured Markdown or JSON artifacts to disk with rich metadata.
Used by the Researcher to persist findings, summaries, and intermediate notes.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .base import BaseTool, ToolSchema

DEFAULT_OUTPUT_DIR = os.getenv("ARTIFACT_OUTPUT_DIR", "./artifacts")

ArtifactType = Literal[
    "research_note",
    "architecture_analysis",
    "source_summary",
    "decision_log",
    "diagram_spec",
    "raw_data",
    "report",
]


class ArtifactWriterTool(BaseTool):
    """Write structured research artifacts to disk with full metadata headers."""

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR) -> None:
        self._root = Path(output_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="artifact_writer",
            description=(
                "Write a research artifact (Markdown or JSON) to disk with metadata "
                "including timestamp, task ID, type, and tags. Returns the file path "
                "and a unique artifact ID."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Human-readable artifact title.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The artifact body — Markdown text or JSON string.",
                    },
                    "artifact_type": {
                        "type": "string",
                        "enum": [
                            "research_note",
                            "architecture_analysis",
                            "source_summary",
                            "decision_log",
                            "diagram_spec",
                            "raw_data",
                            "report",
                        ],
                        "description": "Semantic category of this artifact.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "The task ID this artifact belongs to.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Free-form tags for retrieval (e.g. ['microservices', 'caching']).",
                        "default": [],
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "json"],
                        "description": "File format. Default 'markdown'.",
                        "default": "markdown",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "URLs or references used to produce this artifact.",
                        "default": [],
                    },
                },
                "required": ["title", "content", "artifact_type", "task_id"],
            },
            returns=(
                "Dict: {artifact_id, file_path, task_id, artifact_type, "
                "created_at, size_bytes}"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _artifact_id(self, task_id: str, title: str, ts: str) -> str:
        raw = f"{task_id}:{title}:{ts}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _slug(self, text: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]

    def _build_markdown(
        self, metadata: dict, content: str, sources: list[str]
    ) -> str:
        lines = [
            "---",
            f"artifact_id: {metadata['artifact_id']}",
            f"title: \"{metadata['title']}\"",
            f"artifact_type: {metadata['artifact_type']}",
            f"task_id: {metadata['task_id']}",
            f"created_at: {metadata['created_at']}",
            f"tags: [{', '.join(metadata['tags'])}]",
        ]
        if sources:
            lines.append("sources:")
            for s in sources:
                lines.append(f"  - {s}")
        lines += ["---", "", content]
        return "\n".join(lines)

    def _build_json(self, metadata: dict, content: str, sources: list[str]) -> str:
        try:
            body = json.loads(content)
        except json.JSONDecodeError:
            body = content
        payload = {**metadata, "sources": sources, "content": body}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(           # type: ignore[override]
        self,
        title: str,
        content: str,
        artifact_type: ArtifactType,
        task_id: str,
        tags: list[str] | None = None,
        format: str = "markdown",       # noqa: A002
        sources: list[str] | None = None,
        **_: Any,
    ) -> dict:
        tags = tags or []
        sources = sources or []
        now = datetime.now(timezone.utc)
        ts = now.isoformat()

        artifact_id = self._artifact_id(task_id, title, ts)
        ext = "md" if format == "markdown" else "json"
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{self._slug(title)}.{ext}"

        # Sub-directory per task
        task_dir = self._root / self._slug(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        file_path = task_dir / filename

        metadata = {
            "artifact_id": artifact_id,
            "title": title,
            "artifact_type": artifact_type,
            "task_id": task_id,
            "created_at": ts,
            "tags": tags,
        }

        if format == "markdown":
            text = self._build_markdown(metadata, content, sources)
        else:
            text = self._build_json(metadata, content, sources)

        file_path.write_text(text, encoding="utf-8")

        return {
            "artifact_id": artifact_id,
            "file_path": str(file_path),
            "task_id": task_id,
            "artifact_type": artifact_type,
            "created_at": ts,
            "size_bytes": file_path.stat().st_size,
        }
