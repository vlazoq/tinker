"""
Diagram Generator Tool
======================

Accepts a component/relationship description as structured JSON,
renders a Graphviz .dot file **or** a Mermaid .mmd file, and produces
a PNG image (when the CLI renderer is available on PATH).

Two rendering back-ends are supported:

  1. **Graphviz** (default) — generates a ``.dot`` file, renders via the
     ``dot`` (or other layout engine) command.
  2. **Mermaid** — generates a ``.mmd`` file, renders via ``mmdc``
     (the Mermaid CLI, ``@mermaid-js/mermaid-cli``).

The caller selects the back-end via the ``render_format`` parameter
(``"graphviz"`` or ``"mermaid"``).  If the chosen CLI tool is not
installed, the source file is still saved and ``rendered: false`` is
returned so downstream code can handle it gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolSchema

# Standard Python logging — messages appear under "tools.diagram_generator".
logger = logging.getLogger(__name__)

DEFAULT_DIAGRAM_DIR = os.getenv("DIAGRAM_OUTPUT_DIR", "./artifacts/diagrams")

# Graphviz layout engines available
LAYOUT_ENGINES = ("dot", "neato", "fdp", "circo", "twopi", "sfdp")

# Supported render formats — used for parameter validation
RENDER_FORMATS = ("graphviz", "mermaid")


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
                "Generate an architecture diagram from a structured description "
                "of components and their relationships.  Supports two rendering "
                "back-ends: Graphviz (.dot → PNG) and Mermaid (.mmd → PNG/SVG).  "
                "Returns paths to the source file and the rendered image."
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
                    "render_format": {
                        "type": "string",
                        "enum": ["graphviz", "mermaid"],
                        "description": (
                            "Which rendering back-end to use.  'graphviz' produces "
                            "a .dot file and renders via the Graphviz CLI.  'mermaid' "
                            "produces a .mmd file and renders via the Mermaid CLI "
                            "(mmdc).  Default: 'graphviz'."
                        ),
                        "default": "graphviz",
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
                "Dict: {source_path, image_path, diagram_name, node_count, "
                "edge_count, rendered, render_format}.  For Graphviz: source_path "
                "is .dot, image_path is .png.  For Mermaid: source_path is .mmd, "
                "image_path is .png (or .svg)."
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
                f'    {src} -> {dst} [label="{lbl}", style={style}, dir={direction_attr}];'
            )

        lines.append("}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Mermaid generation
    # ------------------------------------------------------------------

    def _mermaid_direction(self, direction: str) -> str:
        """
        Map the user-facing direction string to a Mermaid graph direction.

        Mermaid uses the same abbreviations as Graphviz (LR, TB, RL, BT),
        so this is mostly a validation pass — if the caller sends an
        unrecognised value we fall back to ``LR`` (left-to-right).
        """
        valid = {"LR", "TB", "RL", "BT"}
        return direction if direction in valid else "LR"

    def _mermaid_edge_style(self, style: str) -> str:
        """
        Convert a Graphviz edge style name to a Mermaid arrow syntax.

        Mermaid does not support all Graphviz edge styles natively, so we
        approximate:
          - "solid"  → ``-->``  (solid arrow)
          - "dashed" → ``-.->`` (dashed arrow)
          - "dotted" → ``-.->`` (Mermaid has no separate dotted; use dashed)

        Returns the arrow string (e.g. ``"-->"``).
        """
        if style in ("dashed", "dotted"):
            return "-.->"
        # Default: solid arrow
        return "-->"

    def _build_mermaid(
        self,
        title: str,
        components: list[dict],
        relationships: list[dict],
        direction: str,
    ) -> str:
        """
        Build a Mermaid diagram definition string from components and edges.

        Mermaid syntax overview (for beginners):
          - ``graph LR`` starts a left-to-right flowchart.
          - ``A[Label]`` defines a node with id ``A`` and display text ``Label``.
          - ``A-->B`` draws a solid arrow from A to B.
          - ``A-.->B`` draws a dashed arrow.
          - ``A-->|text|B`` adds an edge label.
          - ``subgraph Title ... end`` groups nodes visually.

        We convert the same JSON structure used by the Graphviz path into
        valid Mermaid syntax so callers don't need to change their input.
        """
        mermaid_dir = self._mermaid_direction(direction)
        lines: list[str] = []

        # Header — "graph" creates a flowchart; direction follows immediately.
        lines.append(f"graph {mermaid_dir}")

        # Optional title comment (Mermaid doesn't have a native graph title,
        # but the %% comment serves as documentation in the source file).
        if title:
            lines.append(f"    %% Title: {self._escape(title)}")
            lines.append("")

        # ----- Nodes -----
        # Collect groups for subgraph clustering (same logic as Graphviz path).
        groups: dict[str, list[dict]] = {}
        ungrouped: list[dict] = []
        for comp in components:
            g = comp.get("group")
            if g:
                groups.setdefault(g, []).append(comp)
            else:
                ungrouped.append(comp)

        # Emit ungrouped nodes first.
        for comp in ungrouped:
            nid = self._safe_id(comp["id"])
            label = comp.get("label", comp["id"])
            # Mermaid node shapes: [...] = rectangle (default).
            # We use the rectangle shape for all nodes to keep it simple;
            # Mermaid's shape options are more limited than Graphviz.
            lines.append(f'    {nid}["{self._escape(label)}"]')

        # Emit grouped nodes inside Mermaid subgraphs.
        for group_name, members in groups.items():
            lines.append(f'    subgraph {self._safe_id(group_name)}["{self._escape(group_name)}"]')
            for comp in members:
                nid = self._safe_id(comp["id"])
                label = comp.get("label", comp["id"])
                lines.append(f'        {nid}["{self._escape(label)}"]')
            lines.append("    end")

        lines.append("")

        # ----- Edges -----
        for rel in relationships:
            src = self._safe_id(rel["from"])
            dst = self._safe_id(rel["to"])
            lbl = rel.get("label", "")
            arrow = self._mermaid_edge_style(rel.get("style", "solid"))

            if lbl:
                # Edge with label: A -->|label| B
                lines.append(f"    {src} {arrow}|{self._escape(lbl)}| {dst}")
            else:
                # Edge without label: A --> B
                lines.append(f"    {src} {arrow} {dst}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    async def _execute(  # type: ignore[override]
        self,
        diagram_name: str,
        components: list[dict],
        relationships: list[dict],
        title: str = "",
        render_format: str = "graphviz",
        layout_engine: str = "dot",
        direction: str = "LR",
        **_: Any,
    ) -> dict:
        """
        Generate a diagram source file and attempt to render it to an image.

        This method dispatches to one of two rendering paths based on the
        ``render_format`` parameter:

          - ``"graphviz"`` (default): builds a ``.dot`` file and shells out
            to the Graphviz ``dot`` (or other layout engine) command to
            produce a PNG.
          - ``"mermaid"``: builds a ``.mmd`` (Mermaid) file and shells out
            to ``mmdc`` (the Mermaid CLI) to produce a PNG.

        In both cases, if the CLI tool is not installed on the system, the
        source file is still saved to disk and ``rendered: false`` is
        returned.  This lets the caller display the source or render it
        later in a browser.
        """
        # Validate render_format — fall back to "graphviz" if unrecognised.
        if render_format not in RENDER_FORMATS:
            logger.warning(
                "Unknown render_format '%s', falling back to 'graphviz'.",
                render_format,
            )
            render_format = "graphviz"

        # Sanitise the diagram name so it's safe for use as a filename.
        safe_name = re.sub(r"[^A-Za-z0-9_\-]", "_", diagram_name)

        # -----------------------------------------------------------------
        # PATH 1: Graphviz rendering (the original, unchanged behaviour)
        # -----------------------------------------------------------------
        if render_format == "graphviz":
            return await self._render_graphviz(
                safe_name=safe_name,
                diagram_name=diagram_name,
                title=title,
                components=components,
                relationships=relationships,
                layout_engine=layout_engine,
                direction=direction,
            )

        # -----------------------------------------------------------------
        # PATH 2: Mermaid rendering (new)
        # -----------------------------------------------------------------
        return await self._render_mermaid(
            safe_name=safe_name,
            diagram_name=diagram_name,
            title=title,
            components=components,
            relationships=relationships,
            direction=direction,
        )

    # ------------------------------------------------------------------
    # Graphviz render path (extracted from the original _execute)
    # ------------------------------------------------------------------

    async def _render_graphviz(
        self,
        safe_name: str,
        diagram_name: str,
        title: str,
        components: list[dict],
        relationships: list[dict],
        layout_engine: str,
        direction: str,
    ) -> dict:
        """
        Build a Graphviz .dot file and attempt to render it to PNG.

        This is the original rendering logic, extracted into its own method
        so that ``_execute`` can cleanly dispatch between Graphviz and
        Mermaid without deeply nested if/else blocks.
        """
        if layout_engine not in LAYOUT_ENGINES:
            layout_engine = "dot"

        dot_path = self._root / f"{safe_name}.dot"
        png_path = self._root / f"{safe_name}.png"

        # Build the DOT source from the structured component/edge data.
        dot_src = self._build_dot(
            title=title or diagram_name,
            components=components,
            relationships=relationships,
            direction=direction,
        )
        dot_path.write_text(dot_src, encoding="utf-8")

        # Try to render PNG via the Graphviz CLI subprocess.
        # If Graphviz is not installed, we still have the .dot file.
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
        except (TimeoutError, FileNotFoundError) as exc:
            render_error = str(exc)

        return {
            "dot_path": str(dot_path),
            "png_path": str(png_path) if rendered else None,
            # Backward-compatible aliases so existing callers keep working:
            "source_path": str(dot_path),
            "image_path": str(png_path) if rendered else None,
            "diagram_name": diagram_name,
            "node_count": len(components),
            "edge_count": len(relationships),
            "rendered": rendered,
            "render_format": "graphviz",
            "render_error": render_error,
            "dot_source": dot_src,
        }

    # ------------------------------------------------------------------
    # Mermaid render path (new)
    # ------------------------------------------------------------------

    async def _render_mermaid(
        self,
        safe_name: str,
        diagram_name: str,
        title: str,
        components: list[dict],
        relationships: list[dict],
        direction: str,
    ) -> dict:
        """
        Build a Mermaid .mmd file and attempt to render it to PNG via mmdc.

        ``mmdc`` is the Mermaid CLI tool, installed separately via npm:
            npm install -g @mermaid-js/mermaid-cli

        If ``mmdc`` is not found on PATH, we still save the ``.mmd`` source
        file so the user can:
          - Open it in a Mermaid-compatible editor (VS Code, GitHub, etc.)
          - Paste it into https://mermaid.live for online rendering
          - Install mmdc later and re-render

        The ``.mmd`` extension is the conventional Mermaid file extension.
        """
        mmd_path = self._root / f"{safe_name}.mmd"
        png_path = self._root / f"{safe_name}.png"

        # Build the Mermaid source from the structured component/edge data.
        mmd_src = self._build_mermaid(
            title=title or diagram_name,
            components=components,
            relationships=relationships,
            direction=direction,
        )
        mmd_path.write_text(mmd_src, encoding="utf-8")

        # Check whether the Mermaid CLI (mmdc) is available on PATH.
        # shutil.which() returns the full path if found, or None if not.
        mmdc_available = shutil.which("mmdc") is not None

        rendered = False
        render_error = None

        if mmdc_available:
            # Attempt to render the .mmd file to PNG via mmdc.
            # The command:  mmdc -i input.mmd -o output.png
            try:
                proc = await asyncio.create_subprocess_exec(
                    "mmdc",
                    "-i",
                    str(mmd_path),
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
            except (TimeoutError, FileNotFoundError) as exc:
                render_error = str(exc)
        else:
            # mmdc is not installed — this is a soft failure, not a crash.
            # The .mmd source file is still useful on its own.
            render_error = (
                "Mermaid CLI (mmdc) not found on PATH.  Install it with: "
                "npm install -g @mermaid-js/mermaid-cli"
            )
            logger.info(
                "mmdc not available; saved Mermaid source to %s without rendering.",
                mmd_path,
            )

        return {
            "mmd_path": str(mmd_path),
            "png_path": str(png_path) if rendered else None,
            # Unified keys that work the same for both formats:
            "source_path": str(mmd_path),
            "image_path": str(png_path) if rendered else None,
            "diagram_name": diagram_name,
            "node_count": len(components),
            "edge_count": len(relationships),
            "rendered": rendered,
            "render_format": "mermaid",
            "render_error": render_error,
            # Include the raw Mermaid source so callers can display it
            # in a browser or editor even when rendering fails.
            "mermaid_source": mmd_src,
        }
