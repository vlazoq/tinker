"""
Artifact Writer Tool — tools/artifact_writer.py
=================================================

What this file does
--------------------
This file defines ``ArtifactWriterTool``, which saves Tinker's research
findings and analysis to disk as files.

Any time an AI agent (Researcher, Architect, Critic, etc.) produces output
worth keeping, it calls this tool to persist that output.  The tool takes a
title, content, and metadata, and writes a well-structured file — either a
Markdown document (`.md`) or a JSON file (`.json`) — to an output directory.

What makes it more than just "write a file"
--------------------------------------------
Every artifact gets:
  - A **unique ID** (12-character hash derived from the task, title, and timestamp),
    so you can reference it later even if you don't know the filename.
  - A **metadata header** (YAML front matter for Markdown, top-level keys for JSON)
    that records who created it, when, what type it is, and what sources were used.
  - Organised into a **subdirectory per task** so all artifacts from one task
    are grouped together.

This structure makes it easy to:
  - Browse artifacts for a task by looking in the task's folder.
  - Search for artifacts by type, ID, or tags.
  - Feed artifacts back into future Tinker loops by reading the stored files.

Why it exists
-------------
Without persisted artifacts, Tinker's work is lost the moment a run finishes.
With artifacts on disk, findings accumulate over multiple runs, mistakes can
be reviewed, and architecture decisions are documented.

How it fits into Tinker
-----------------------
Registered as "artifact_writer" in the ToolRegistry.  Typically called by the
Researcher agent at the end of a research loop to save its research note, or by
the Orchestrator to save synthesis documents.

The output directory defaults to the ARTIFACT_OUTPUT_DIR environment variable,
or "./artifacts" if not set.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .base import BaseTool, ToolSchema

# Read the output directory from the environment at module load time.
# Operators can set ARTIFACT_OUTPUT_DIR to control where files are written.
DEFAULT_OUTPUT_DIR = os.getenv("ARTIFACT_OUTPUT_DIR", "./artifacts")

# The allowed artifact types.  Using Literal[] means type-checkers will warn
# if anyone passes a type string that isn't in this list.
ArtifactType = Literal[
    "research_note",  # findings from a Researcher loop
    "architecture_analysis",  # analysis of an existing system
    "source_summary",  # summary of a specific source (URL, paper, etc.)
    "decision_log",  # record of an architectural decision and its rationale
    "diagram_spec",  # specification for generating a diagram
    "raw_data",  # unprocessed data (API responses, downloaded files, etc.)
    "report",  # a human-readable report (e.g. end-of-cycle summary)
]


class ArtifactWriterTool(BaseTool):
    """
    Write structured research artifacts to disk with rich metadata.

    This tool creates files with a consistent structure so they can be:
      - Easily read by humans browsing the artifacts directory.
      - Parsed by other tools that need to load artifacts back into memory.
      - Indexed and searched by the MemoryManager.

    File naming convention:
        <output_dir>/<task_slug>/<YYYYMMDD_HHMMSS>_<title_slug>.<ext>

    For example:
        ./artifacts/design_memory_manager/20240315_142301_memory_manager_design.md
    """

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR) -> None:
        """
        Initialise the artifact writer.

        Args:
            output_dir:
                Directory where artifact files will be written.
                Created automatically if it doesn't exist (including all
                intermediate parent directories).
        """
        self._root = Path(output_dir)
        # mkdir with parents=True creates the full path, like "mkdir -p".
        # exist_ok=True means we don't crash if the directory already exists.
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        """
        Describe this tool to the ToolRegistry and the AI model.

        The ``artifact_type`` field classifies the artifact so it can be
        filtered and retrieved later.  The ``sources`` field is important for
        citation — it records where the information came from.
        """
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
                "Dict: {artifact_id, file_path, task_id, artifact_type, created_at, size_bytes}"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _artifact_id(self, task_id: str, title: str, ts: str) -> str:
        """
        Generate a short, unique ID for an artifact.

        We combine task_id + title + timestamp into a string, compute its
        SHA-256 hash, and take the first 12 hex characters.

        Why not use a UUID?
        -------------------
        UUIDs are random — two artifacts with the same title might get different
        IDs if generated at the same millisecond on different machines.  A hash
        of the content is deterministic: given the same inputs, you always get
        the same ID.  This is useful for deduplication.

        12 characters of hex gives us 16^12 ≈ 281 trillion possible IDs, which
        is more than enough to avoid collisions in practice.

        Args:
            task_id: The task this artifact belongs to.
            title:   The artifact title.
            ts:      The ISO-8601 timestamp of creation.

        Returns:
            A 12-character hex string (e.g. "a3f9c2d1b4e8").
        """
        raw = f"{task_id}:{title}:{ts}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _slug(self, text: str) -> str:
        """
        Convert arbitrary text into a safe filesystem slug.

        A "slug" is a URL/filename-safe version of text: all lowercase, spaces
        and special characters replaced with underscores, length capped.

        For example:
            "Memory Manager: SQLite Design (v2)" → "memory_manager_sqlite_design_v2"

        Args:
            text: Any text string.

        Returns:
            A filesystem-safe slug, max 60 characters.
        """
        import re

        # Replace any character that's not a letter or digit with an underscore.
        # Then strip leading/trailing underscores that might appear from
        # leading/trailing special chars in the input.
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]

    def _build_markdown(self, metadata: dict, content: str, sources: list[str]) -> str:
        """
        Build the full Markdown file content with YAML front matter.

        YAML front matter is a convention used by static site generators
        (Jekyll, Hugo, Obsidian, etc.) where metadata is written at the top
        of a Markdown file between ``---`` delimiters.  It looks like this:

            ---
            artifact_id: a3f9c2d1b4e8
            title: "Memory Manager Design"
            artifact_type: architecture_analysis
            task_id: design_memory_manager
            created_at: 2024-03-15T14:23:01.456789+00:00
            tags: [sqlite, memory, asyncio]
            sources:
              - https://www.sqlite.org/wal.html
            ---

            # Memory Manager Design

            The Memory Manager wraps a local SQLite database...

        This format means the file is both human-readable and machine-parsable.

        Args:
            metadata: Dict of artifact metadata fields.
            content:  The main article body (Markdown text).
            sources:  List of source URLs or references.

        Returns:
            Complete Markdown file content as a string.
        """
        lines = [
            "---",
            f"artifact_id: {metadata['artifact_id']}",
            f'title: "{metadata["title"]}"',
            f"artifact_type: {metadata['artifact_type']}",
            f"task_id: {metadata['task_id']}",
            f"created_at: {metadata['created_at']}",
            f"tags: [{', '.join(metadata['tags'])}]",
        ]
        if sources:
            # Write sources as a YAML list under the "sources" key.
            lines.append("sources:")
            for s in sources:
                lines.append(f"  - {s}")
        # The "---" ends the YAML front matter; then an empty line before the content.
        lines += ["---", "", content]
        return "\n".join(lines)

    def _build_json(self, metadata: dict, content: str, sources: list[str]) -> str:
        """
        Build the full JSON file content — metadata plus content in one object.

        For JSON artifacts, we try to parse ``content`` as JSON.  If it's
        already a JSON string (e.g. the Architect's output), we embed it as a
        nested object.  If it's plain text, we embed it as a string value.

        Example output:

            {
              "artifact_id": "a3f9c2d1b4e8",
              "title": "Memory Manager Design",
              ...
              "sources": ["https://..."],
              "content": {
                "artifact_type": "design_proposal",
                ...
              }
            }

        Args:
            metadata: Dict of artifact metadata fields.
            content:  The artifact body — either a JSON string or plain text.
            sources:  List of source URLs or references.

        Returns:
            Pretty-printed JSON string.
        """
        try:
            # Try parsing content as JSON first.  If it succeeds, we embed it
            # as a proper nested object (not a string-within-a-string).
            body = json.loads(content)
        except json.JSONDecodeError:
            # Content is plain text — embed it as a string value.
            body = content
        # Merge metadata, sources, and content into one flat dict.
        payload = {**metadata, "sources": sources, "content": body}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(  # type: ignore[override]
        self,
        title: str,
        content: str,
        artifact_type: ArtifactType,
        task_id: str,
        tags: list[str] | None = None,
        format: str = "markdown",
        sources: list[str] | None = None,
        **_: Any,  # absorb any unexpected kwargs
    ) -> dict:
        """
        Write an artifact to disk and return its metadata.

        Steps:
          1. Set defaults for optional fields (tags, sources).
          2. Generate a unique artifact_id from task+title+timestamp.
          3. Determine the file path (task subdirectory + timestamped filename).
          4. Build the file content (Markdown or JSON format).
          5. Write the file to disk.
          6. Return a dict describing the artifact (ID, path, size, etc.).

        Args:
            title:         Human-readable title for this artifact.
            content:       The artifact body text or JSON string.
            artifact_type: Category of this artifact (see ArtifactType).
            task_id:       ID of the task that produced this artifact.
            tags:          Optional list of tags for retrieval.
            format:        "markdown" (default) or "json".
            sources:       Optional list of source URLs cited in this artifact.
            **_:           Ignored extra arguments.

        Returns:
            Dict with keys: artifact_id, file_path, task_id, artifact_type,
            created_at, size_bytes.
        """
        tags = tags or []
        sources = sources or []
        # Use UTC time for all timestamps — consistent regardless of server location.
        now = datetime.now(UTC)
        ts = now.isoformat()  # e.g. "2024-03-15T14:23:01.456789+00:00"

        # Generate the unique ID for this artifact.
        artifact_id = self._artifact_id(task_id, title, ts)

        # Determine file extension based on format.
        ext = "md" if format == "markdown" else "json"

        # Build filename: timestamp prefix + title slug + extension.
        # Example: "20240315_142301_memory_manager_design.md"
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{self._slug(title)}.{ext}"

        # Sub-directory per task — keeps all artifacts for a task together.
        # Example: "./artifacts/design_memory_manager/"
        # Sub-directory per task
        task_dir = self._root / self._slug(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        file_path = task_dir / filename

        # Collect all metadata into a dict for use by both _build_markdown
        # and _build_json.
        metadata = {
            "artifact_id": artifact_id,
            "title": title,
            "artifact_type": artifact_type,
            "task_id": task_id,
            "created_at": ts,
            "tags": tags,
        }

        # Build the file content in the requested format.
        if format == "markdown":
            text = self._build_markdown(metadata, content, sources)
        else:
            text = self._build_json(metadata, content, sources)

        # Write the file — UTF-8 encoding to support international characters.
        file_path.write_text(text, encoding="utf-8")

        # Return a summary dict so the caller knows where the file ended up
        # and has the artifact_id to reference it later.
        return {
            "artifact_id": artifact_id,
            "file_path": str(file_path),
            "task_id": task_id,
            "artifact_type": artifact_type,
            "created_at": ts,
            # st_size gives the file size in bytes — useful for observability.
            "size_bytes": file_path.stat().st_size,
        }
