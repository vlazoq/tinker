"""
lineage/tracker.py
===================

Data lineage tracking for Tinker artifacts.

Why lineage tracking?
----------------------
Without lineage, Tinker is a "black box" — you can see the final architecture
documents but you can't answer questions like:
  - Which micro loop tasks contributed to this meso synthesis?
  - Which research notes informed the Architect's decision about database sharding?
  - Why did the architecture evolve from monolith to microservices at loop 47?
  - Can I reproduce the results from session X?

Data lineage tracks parent-child relationships between all data entities.
This creates a provenance graph that makes the entire reasoning process
auditable and reproducible.

Lineage graph structure
------------------------
Nodes represent data entities:
  - Tasks (from the task engine)
  - Artifacts (from micro loops)
  - Syntheses (from meso/macro loops)
  - Research notes (from the researcher)

Edges represent derivation relationships:
  - task → artifact (this task produced this artifact)
  - artifact → synthesis (this artifact was included in this synthesis)
  - research → artifact (this research informed this artifact)
  - synthesis → macro (this synthesis contributed to the macro snapshot)

Key capabilities
-----------------
- ``record_derivation``: write a parent→child edge with cycle detection
- ``get_parents`` / ``get_children``: immediate neighbours
- ``get_full_ancestry``: all ancestors up to a depth limit
- ``get_descendants``: all downstream entities (impact analysis)
- ``get_by_type``: all edges involving a given entity type
- ``get_by_operation``: all edges created by a given operation
- ``get_stats``: summary counts (total edges, breakdown by type/operation)

Usage
------
::

    lineage = LineageTracker("tinker_lineage.sqlite")
    await lineage.connect()

    # Record task → artifact:
    await lineage.record_derivation(
        parent_id=task_id,   parent_type="task",
        child_id=artifact_id, child_type="artifact",
        operation="micro_loop",
        metadata={"iteration": 42, "critic_score": 0.85},
    )

    # Query ancestry of an artifact:
    parents = await lineage.get_parents(artifact_id)
    ancestors = await lineage.get_full_ancestry(artifact_id)

    # Find all artifacts that contributed to a synthesis:
    children = await lineage.get_children(synthesis_id)

    # Impact analysis — what will be affected if this task changes?
    descendants = await lineage.get_descendants(task_id)

    # Statistics:
    stats = await lineage.get_stats()
    # {"total_edges": 142, "by_type": {"task": 42, ...}, "by_operation": {...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LineageTracker:
    """
    SQLite-backed data lineage tracker.

    Records parent-child derivation relationships between all Tinker data
    entities (tasks, artifacts, syntheses, research notes).

    Parameters
    ----------
    db_path : Path to the SQLite lineage database (default: "tinker_lineage.sqlite").
    """

    def __init__(self, db_path: str = "tinker_lineage.sqlite") -> None:
        self._db_path = db_path
        self._conn = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Create the lineage database and schema."""
        try:
            import aiosqlite

            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS lineage_edges (
                    id          TEXT PRIMARY KEY,
                    parent_id   TEXT NOT NULL,
                    parent_type TEXT NOT NULL,   -- task, artifact, synthesis, research
                    child_id    TEXT NOT NULL,
                    child_type  TEXT NOT NULL,
                    operation   TEXT NOT NULL,   -- micro_loop, meso_synthesis, etc.
                    metadata    TEXT,            -- JSON
                    created_at  TEXT NOT NULL
                )
            """)
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS lineage_parent_idx ON lineage_edges (parent_id)"
            )
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS lineage_child_idx ON lineage_edges (child_id)"
            )
            # Composite index speeds up the common cycle-detection pattern:
            # "does (child_id, parent_id) pair already exist as (parent_id, child_id)?"
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS lineage_pair_idx ON lineage_edges (parent_id, child_id)"
            )
            await self._conn.commit()
            logger.info("LineageTracker connected to %s", self._db_path)
        except ImportError:
            logger.warning("aiosqlite not available — LineageTracker disabled")
        except Exception as exc:
            logger.warning("LineageTracker connect failed: %s — lineage disabled", exc)

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def record_derivation(
        self,
        parent_id: str,
        parent_type: str,
        child_id: str,
        child_type: str,
        operation: str,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Record a parent→child derivation edge with cycle detection.

        Cycle detection: if recording this edge would create a cycle
        (i.e. ``parent_id`` is already a descendant of ``child_id``),
        the edge is rejected and ``None`` is returned with a warning.

        Parameters
        ----------
        parent_id   : ID of the source entity (e.g. task ID).
        parent_type : Type of parent ("task", "artifact", "synthesis", "research").
        child_id    : ID of the derived entity (e.g. artifact ID).
        child_type  : Type of child ("artifact", "synthesis", "macro").
        operation   : What operation created the relationship
                      (e.g. "micro_loop", "meso_synthesis", "researcher_call").
        metadata    : Optional additional metadata.

        Returns
        -------
        str : The edge ID, or None if the tracker is disabled or cycle detected.
        """
        if not self._conn:
            return None

        # Cycle detection: would parent_id become a descendant of child_id?
        # That happens when child_id already appears in the ancestry of parent_id,
        # i.e. child_id is an ancestor of parent_id — adding parent→child would
        # create a cycle.
        ancestors = await self.get_full_ancestry(parent_id, max_depth=50)
        ancestor_ids = {e["parent_id"] for e in ancestors}
        if child_id in ancestor_ids or child_id == parent_id:
            logger.warning(
                "LineageTracker: cycle detected — refusing to add edge %s→%s",
                parent_id,
                child_id,
            )
            return None

        edge_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            try:
                await self._conn.execute(
                    """
                    INSERT INTO lineage_edges
                        (id, parent_id, parent_type, child_id, child_type, operation, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        edge_id,
                        parent_id,
                        parent_type,
                        child_id,
                        child_type,
                        operation,
                        json.dumps(metadata) if metadata else None,
                        now,
                    ),
                )
                await self._conn.commit()
                return edge_id
            except Exception as exc:
                logger.error("LineageTracker.record_derivation failed: %s", exc)
                return None

    async def get_parents(self, entity_id: str) -> list[dict]:
        """
        Get the direct parents (immediate sources) of an entity.

        Parameters
        ----------
        entity_id : The ID of the entity to look up.

        Returns
        -------
        list[dict] : List of parent edge dicts.
        """
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT * FROM lineage_edges WHERE child_id=? ORDER BY created_at",
                (entity_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.error("LineageTracker.get_parents failed: %s", exc)
            return []

    async def get_children(self, entity_id: str) -> list[dict]:
        """
        Get the direct children (immediate derivatives) of an entity.

        Parameters
        ----------
        entity_id : The ID of the entity to look up.

        Returns
        -------
        list[dict] : List of child edge dicts.
        """
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT * FROM lineage_edges WHERE parent_id=? ORDER BY created_at",
                (entity_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.error("LineageTracker.get_children failed: %s", exc)
            return []

    async def get_full_ancestry(
        self, entity_id: str, max_depth: int = 10
    ) -> list[dict]:
        """
        Iteratively get all ancestors of an entity up to ``max_depth`` levels.

        Uses BFS to avoid Python's recursion limit for deep provenance graphs.

        Parameters
        ----------
        entity_id : Starting entity ID.
        max_depth : Maximum traversal depth (prevents infinite loops).

        Returns
        -------
        list[dict] : All ancestor edges, breadth-first from immediate parents outward.
        """
        visited: set[str] = set()
        all_edges: list[dict] = []
        # Queue holds (entity_id, current_depth) pairs
        queue: list[tuple[str, int]] = [(entity_id, 0)]

        while queue:
            eid, depth = queue.pop(0)
            if depth >= max_depth or eid in visited:
                continue
            visited.add(eid)
            parents = await self.get_parents(eid)
            for edge in parents:
                all_edges.append(edge)
                queue.append((edge["parent_id"], depth + 1))

        return all_edges

    async def get_descendants(self, entity_id: str, max_depth: int = 10) -> list[dict]:
        """
        Iteratively get all descendants of an entity (downstream impact analysis).

        Uses BFS to avoid Python's recursion limit for deep lineage graphs.

        Parameters
        ----------
        entity_id : Starting entity ID.
        max_depth : Maximum traversal depth.

        Returns
        -------
        list[dict] : All descendant edges, breadth-first from immediate children outward.
        """
        visited: set[str] = set()
        all_edges: list[dict] = []
        queue: list[tuple[str, int]] = [(entity_id, 0)]

        while queue:
            eid, depth = queue.pop(0)
            if depth >= max_depth or eid in visited:
                continue
            visited.add(eid)
            children = await self.get_children(eid)
            for edge in children:
                all_edges.append(edge)
                queue.append((edge["child_id"], depth + 1))

        return all_edges

    async def get_by_type(self, entity_type: str, role: str = "either") -> list[dict]:
        """
        Get all edges where an entity of ``entity_type`` appears.

        Parameters
        ----------
        entity_type : Entity type to filter on ("task", "artifact", "synthesis",
                      "research", "macro").
        role        : Which side to match: "parent", "child", or "either"
                      (default).

        Returns
        -------
        list[dict] : Matching edges.
        """
        if not self._conn:
            return []
        try:
            if role == "parent":
                cursor = await self._conn.execute(
                    "SELECT * FROM lineage_edges WHERE parent_type=? ORDER BY created_at",
                    (entity_type,),
                )
            elif role == "child":
                cursor = await self._conn.execute(
                    "SELECT * FROM lineage_edges WHERE child_type=? ORDER BY created_at",
                    (entity_type,),
                )
            else:
                cursor = await self._conn.execute(
                    "SELECT * FROM lineage_edges WHERE parent_type=? OR child_type=? ORDER BY created_at",
                    (entity_type, entity_type),
                )
            rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.error("LineageTracker.get_by_type failed: %s", exc)
            return []

    async def get_by_operation(self, operation: str) -> list[dict]:
        """
        Get all edges created by a specific operation.

        Parameters
        ----------
        operation : Operation name (e.g. "micro_loop", "meso_synthesis").

        Returns
        -------
        list[dict] : All edges with that operation, ordered by creation time.
        """
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT * FROM lineage_edges WHERE operation=? ORDER BY created_at",
                (operation,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.error("LineageTracker.get_by_operation failed: %s", exc)
            return []

    async def get_stats(self) -> dict:
        """
        Return summary statistics for the lineage graph.

        Returns
        -------
        dict with keys:
          - ``total_edges``: int
          - ``by_parent_type``: dict[str, int]
          - ``by_child_type``: dict[str, int]
          - ``by_operation``: dict[str, int]
        """
        stats: dict = {
            "total_edges": 0,
            "by_parent_type": {},
            "by_child_type": {},
            "by_operation": {},
        }
        if not self._conn:
            return stats

        try:
            cur = await self._conn.execute("SELECT COUNT(*) FROM lineage_edges")
            row = await cur.fetchone()
            stats["total_edges"] = row[0] if row else 0

            cur = await self._conn.execute(
                "SELECT parent_type, COUNT(*) FROM lineage_edges GROUP BY parent_type"
            )
            for r in await cur.fetchall():
                stats["by_parent_type"][r[0]] = r[1]

            cur = await self._conn.execute(
                "SELECT child_type, COUNT(*) FROM lineage_edges GROUP BY child_type"
            )
            for r in await cur.fetchall():
                stats["by_child_type"][r[0]] = r[1]

            cur = await self._conn.execute(
                "SELECT operation, COUNT(*) FROM lineage_edges GROUP BY operation"
            )
            for r in await cur.fetchall():
                stats["by_operation"][r[0]] = r[1]

        except Exception as exc:
            logger.error("LineageTracker.get_stats failed: %s", exc)

        return stats

    def _row_to_dict(self, row: Any) -> dict:
        """Convert an aiosqlite Row to a plain dict with parsed metadata."""
        d = dict(row)
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                pass
        return d
