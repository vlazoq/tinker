"""
Diagram Generator Tool
Accepts a component/relationship description as structured JSON,
renders a Graphviz .dot file, and produces a PNG image.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolSchema

DEFAULT_DIAGRAM_DIR = os.getenv("DIAGRAM_OUTPUT_DIR", "./artifacts/diagrams")

# Graphviz layout engines available
LAYOUT_ENGINES = ("dot", "neato", "fdp", "circo", "twopi", "sfdp")


class DiagramGeneratorTool(BaseTool):
    """Generate Graphviz architecture diagrams from component descriptions."""

    def __init__(self, output_dir: str = DEFAULT_DIAGRAM_DIR) -> None:
        self._root = Path(output_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="diagram_generator",
            description=(
                "Generate a Graphviz architecture diagram from a structured description "
                "of components and their relationships. Returns paths to the .dot source "
                "and the rendered PNG."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "diagram_name": {
                        "type": "string",
                        "description": "Short name for the diagram file (no spaces).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable diagram title shown inside the graph.",
                    },
                    "components": {
                        "type": "array",
                        "description": "List of nodes in the diagram.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Unique identifier for the node (no spaces).",
                                },
                                "label": {
                                    "type": "string",
                                    "description": "Display label shown in the diagram.",
                                },
                                "shape": {
                                    "type": "string",
                                    "description": "Graphviz shape: box, ellipse, cylinder, diamond, etc.",
                                    "default": "box",
                                },
                                "style": {
                                    "type": "string",
                                    "description": "Graphviz style: filled, dashed, rounded, etc.",
                                    "default": "filled",
                                },
                                "color": {
                                    "type": "string",
                                    "description": "Fill color name or hex (e.g. '#4A90D9').",
                                    "default": "#D6EAF8",
                                },
                                "group": {
                                    "type": "string",
                                    "description": "Optional cluster/subgraph name to group related nodes.",
                                },
                            },
                            "required": ["id", "label"],
                        },
                    },
                    "relationships": {
                        "type": "array",
                        "description": "Directed edges between components.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {
                                    "type": "string",
                                    "description": "Source component ID.",
                                },
                                "to": {
                                    "type": "string",
                                    "description": "Target component ID.",
                                },
                                "label": {
                                    "type": "string",
                                    "description": "Edge label (e.g. 'HTTP/REST', 'async event').",
                                    "default": "",
                                },
                                "style": {
                                    "type": "string",
                                    "description": "Edge style: solid, dashed, dotted.",
                                    "default": "solid",
                                },
                                "direction": {
                                    "type": "string",
                                    "enum": ["forward", "back", "both", "none"],
                                    "default": "forward",
                                },
                            },
                            "required": ["from", "to"],
                        },
                    },
                    "layout_engine": {
                        "type": "string",
                        "enum": list(LAYOUT_ENGINES),
                        "description": "Graphviz layout engine. 'dot' is best for hierarchies.",
                        "default": "dot",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["LR", "TB", "RL", "BT"],
                        "description": "Overall graph direction. LR=left-right, TB=top-bottom.",
                        "default": "LR",
                    },
                },
                "required": ["diagram_name", "components", "relationships"],
            },
            returns=(
                "Dict: {dot_path, png_path, diagram_name, node_count, edge_count, "
                "rendered}"
            ),
        )

    # ------------------------------------------------------------------
    # DOT generation
    # ------------------------------------------------------------------

    def _safe_id(self, s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", s)

    def _escape(self, s: str) -> str:
        return s.replace('"', '\\"').replace("\n", "\\n")

    def _build_dot(
        self,
        title: str,
        components: list[dict],
        relationships: list[dict],
        direction: str,
    ) -> str:
        lines = [
            "digraph G {",
            f'    label="{self._escape(title)}";',
            '    labelloc="t";',
            '    fontname="Helvetica";',
            f"    rankdir={direction};",
            '    node [fontname="Helvetica", fontsize=11];',
            '    edge [fontname="Helvetica", fontsize=9];',
            "",
        ]

        # Collect groups for subgraph clustering
        groups: dict[str, list[dict]] = {}
        ungrouped: list[dict] = []
        for comp in components:
            g = comp.get("group")
            if g:
                groups.setdefault(g, []).append(comp)
            else:
                ungrouped.append(comp)

        # Ungrouped nodes
        for comp in ungrouped:
            nid = self._safe_id(comp["id"])
            label = self._escape(comp.get("label", comp["id"]))
            shape = comp.get("shape", "box")
            style = comp.get("style", "filled")
            color = comp.get("color", "#D6EAF8")
            lines.append(
                f'    {nid} [label="{label}", shape={shape}, '
                f'style="{style}", fillcolor="{color}"];'
            )

        # Grouped nodes in subgraphs (Graphviz clusters)
        for g_idx, (group_name, members) in enumerate(groups.items()):
            lines.append(f"    subgraph cluster_{g_idx} {{")
            lines.append(f'        label="{self._escape(group_name)}";')
            lines.append('        style="dashed";')
            lines.append('        color="#888888";')
            for comp in members:
                nid = self._safe_id(comp["id"])
                label = self._escape(comp.get("label", comp["id"]))
                shape = comp.get("shape", "box")
                style = comp.get("style", "filled")
                color = comp.get("color", "#D6EAF8")
                lines.append(
                    f'        {nid} [label="{label}", shape={shape}, '
                    f'style="{style}", fillcolor="{color}"];'
                )
            lines.append("    }")

        lines.append("")

        # Edges
        for rel in relationships:
            src = self._safe_id(rel["from"])
            dst = self._safe_id(rel["to"])
            lbl = self._escape(rel.get("label", ""))
            style = rel.get("style", "solid")
            direction_attr = rel.get("direction", "forward")
            lines.append(
                f'    {src} -> {dst} [label="{lbl}", style={style}, '
                f"dir={direction_attr}];"
            )

        lines.append("}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(           # type: ignore[override]
        self,
        diagram_name: str,
        components: list[dict],
        relationships: list[dict],
        title: str = "",
        layout_engine: str = "dot",
        direction: str = "LR",
        **_: Any,
    ) -> dict:
        if layout_engine not in LAYOUT_ENGINES:
            layout_engine = "dot"

        safe_name = re.sub(r"[^A-Za-z0-9_\-]", "_", diagram_name)
        dot_path = self._root / f"{safe_name}.dot"
        png_path = self._root / f"{safe_name}.png"

        dot_src = self._build_dot(
            title=title or diagram_name,
            components=components,
            relationships=relationships,
            direction=direction,
        )
        dot_path.write_text(dot_src, encoding="utf-8")

        # Try to render PNG via Graphviz subprocess
        rendered = False
        render_error = None
        try:
            proc = await asyncio.create_subprocess_exec(
                layout_engine,
                "-Tpng",
                str(dot_path),
                "-o",
                str(png_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                rendered = True
            else:
                render_error = stderr.decode().strip()
        except (FileNotFoundError, asyncio.TimeoutError) as exc:
            render_error = str(exc)

        return {
            "dot_path": str(dot_path),
            "png_path": str(png_path) if rendered else None,
            "diagram_name": diagram_name,
            "node_count": len(components),
            "edge_count": len(relationships),
            "rendered": rendered,
            "render_error": render_error,
            "dot_source": dot_src,
        }
