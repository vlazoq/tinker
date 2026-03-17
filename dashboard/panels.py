"""
dashboard/panels.py
────────────────────
Re-exports all Textual panel widgets and adds GrubPanel.

Import target for app.py:
    from .panels import ActiveTaskPanel, ArchitectPanel, ..., GrubPanel
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

# ── Re-export all existing panels ─────────────────────────────────────────────
from .active_task     import ActiveTaskPanel
from .architect_critic import ArchitectPanel, CriticPanel
from .health_arch     import ArchStatePanel, HealthPanel, MemoryPanel
from .log_stream      import LogStreamPanel
from .loop_status     import LoopStatusPanel
from .task_queue      import TaskQueuePanel


# ── Grub panel ────────────────────────────────────────────────────────────────

_BASE = Path(os.getenv("TINKER_BASE_DIR", Path(__file__).parent.parent))
_GRUB_QUEUE_DB     = Path(os.getenv("GRUB_QUEUE_DB",     _BASE / "grub_queue.sqlite"))
_GRUB_ARTIFACTS    = Path(os.getenv("GRUB_ARTIFACTS_DIR", _BASE / "grub_artifacts"))
_TINKER_TASKS_DB   = Path(os.getenv("TINKER_TASK_DB",     _BASE / "tinker_tasks_engine.sqlite"))


def _query_db(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query; return empty list if DB doesn't exist."""
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _score_style(score: float) -> str:
    if score >= 0.8:
        return "bold bright_green"
    if score >= 0.6:
        return "bright_yellow"
    if score >= 0.4:
        return "yellow"
    return "bold bright_red"


def _score_bar(score: float, width: int = 12) -> str:
    filled = int(round(score * width))
    empty  = width - filled
    return "█" * filled + "░" * empty


class GrubPanel(Widget):
    """
    Grub Pipeline Status Panel.

    Reads directly from the Grub queue SQLite and grub_artifacts/ directory
    so it works even when Grub is running in a separate process.
    """

    DEFAULT_CSS = """
    GrubPanel {
        height: 22;
        border: round #4a8a4a;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="grub-header")
        yield Static("", id="grub-tinker-tasks")
        yield Static("", id="grub-queue")
        yield Static("", id="grub-recent")

    def on_mount(self) -> None:
        self._refresh_grub()
        self.set_interval(5.0, self._refresh_grub)

    def _refresh_grub(self) -> None:
        self._update_header()
        self._update_tinker_tasks()
        self._update_queue()
        self._update_recent_artifacts()

    # ── Header ────────────────────────────────────────────────────────────────

    def _update_header(self) -> None:
        queue_exists    = _GRUB_QUEUE_DB.exists()
        artifact_exists = _GRUB_ARTIFACTS.exists()
        hdr = Text()
        hdr.append(" Grub Pipeline", style="bold")
        if queue_exists or artifact_exists:
            hdr.append("  ● active", style="bold bright_green")
        else:
            hdr.append("  ○ no data", style="dim")
        self.query_one("#grub-header", Static).update(hdr)

    # ── Tinker task counts ────────────────────────────────────────────────────

    def _update_tinker_tasks(self) -> None:
        rows = _query_db(
            _TINKER_TASKS_DB,
            "SELECT type, status, COUNT(*) as n "
            "FROM tasks WHERE type IN ('implementation','review') "
            "GROUP BY type, status",
        )

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=16)
        tbl.add_column(width=10)
        tbl.add_column(width=6, justify="right")
        tbl.add_row(Text("TINKER TASKS", style="bold dim"), Text(""), Text(""))

        if not rows:
            tbl.add_row("", Text("no implementation tasks", style="dim italic"), Text(""))
        else:
            counts: dict[str, dict[str, int]] = {}
            for r in rows:
                counts.setdefault(r["type"], {})[r["status"]] = r["n"]

            type_styles = {
                "implementation": ("bright_cyan",   "implement"),
                "review":         ("bright_magenta","review"),
            }
            status_styles = {
                "pending":   "yellow",
                "active":    "bright_green",
                "completed": "dim green",
                "failed":    "bright_red",
            }
            for t, (ts, label) in type_styles.items():
                for s, ss in status_styles.items():
                    n = counts.get(t, {}).get(s, 0)
                    if n == 0:
                        continue
                    tbl.add_row(
                        Text(label, style=ts),
                        Text(s, style=ss),
                        Text(str(n), style=ss + " bold"),
                    )

        self.query_one("#grub-tinker-tasks", Static).update(tbl)

    # ── Grub queue stats ──────────────────────────────────────────────────────

    def _update_queue(self) -> None:
        rows = _query_db(
            _GRUB_QUEUE_DB,
            "SELECT status, COUNT(*) as n FROM grub_queue GROUP BY status",
        )

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=16)
        tbl.add_column(width=10)
        tbl.add_column(width=6, justify="right")
        tbl.add_row(Text("GRUB QUEUE", style="bold dim"), Text(""), Text(""))

        if not rows:
            tbl.add_row("", Text("queue empty / not started", style="dim italic"), Text(""))
        else:
            status_styles = {
                "pending":   "yellow",
                "claimed":   "bright_green",
                "done":      "dim green",
                "failed":    "bright_red",
            }
            for r in rows:
                s  = r["status"]
                n  = r["n"]
                ss = status_styles.get(s, "white")
                tbl.add_row(
                    Text(s, style=ss),
                    Text("", style=""),
                    Text(str(n), style=ss + " bold"),
                )

        self.query_one("#grub-queue", Static).update(tbl)

    # ── Recent artifacts ──────────────────────────────────────────────────────

    def _update_recent_artifacts(self) -> None:
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=10)
        tbl.add_column(width=20)
        tbl.add_column(width=6, justify="right")
        tbl.add_row(Text("RECENT RESULTS", style="bold dim"), Text(""), Text(""))

        if not _GRUB_ARTIFACTS.exists():
            tbl.add_row("", Text("no artifacts yet", style="dim italic"), Text(""))
            self.query_one("#grub-recent", Static).update(tbl)
            return

        # List last 4 .md files sorted newest first
        files = sorted(
            _GRUB_ARTIFACTS.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:4]

        if not files:
            tbl.add_row("", Text("no artifacts yet", style="dim italic"), Text(""))
            self.query_one("#grub-recent", Static).update(tbl)
            return

        for f in files:
            # Parse score from filename or first line
            score_str = "—"
            score_style = "dim"
            try:
                first_lines = f.read_text(encoding="utf-8").splitlines()[:10]
                for line in first_lines:
                    if "score" in line.lower() and any(c.isdigit() for c in line):
                        import re
                        m = re.search(r"(\d+\.\d+)", line)
                        if m:
                            score = float(m.group(1))
                            if score > 1.0:          # e.g. 8.5 out of 10
                                score = score / 10.0
                            score_str   = f"{score:.2f}"
                            score_style = _score_style(score)
                            break
            except Exception:
                pass

            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M")
            name  = f.stem[:18]
            tbl.add_row(
                Text(mtime, style="dim"),
                Text(name, style="bright_white"),
                Text(score_str, style=score_style),
            )

        self.query_one("#grub-recent", Static).update(tbl)
