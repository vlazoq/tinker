"""tinker/dashboard/panels/task_queue.py — Queue depth and breakdown."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..state import TaskStatus, TaskType, TinkerState

STATUS_COLOURS = {
    "pending":  "yellow",
    "active":   "bright_green",
    "complete": "bright_black",
    "failed":   "bright_red",
    "skipped":  "dim white",
}

TYPE_COLOURS = {
    "design":           "bright_cyan",
    "critique":         "bright_yellow",
    "refine":           "bright_green",
    "research":         "bright_blue",
    "commit":           "bright_magenta",
    "stagnation_break": "bright_red",
}


def _small_bar(count: int, total: int, width: int = 10) -> str:
    if total == 0:
        return "░" * width
    filled = max(1, int(round(count / total * width))) if count else 0
    return "█" * filled + "░" * (width - filled)


class TaskQueuePanel(Widget):
    DEFAULT_CSS = """
    TaskQueuePanel {
        height: 18;
        border: round $secondary;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="tq-header")
        yield Static("", id="tq-status")
        yield Static("", id="tq-type")
        yield Static("", id="tq-recent")

    def refresh_state(self, state: TinkerState) -> None:
        qs = state.queue_stats

        # ── header ──────────────────────────────────────
        hdr = Text()
        hdr.append(" Task Queue  depth ", style="bold dim")
        hdr.append(str(qs.total_depth), style="bold bright_white")
        self.query_one("#tq-header", Static).update(hdr)

        total = qs.total_depth or 1

        # ── by status ────────────────────────────────────
        status_tbl = Table.grid(padding=(0, 1))
        status_tbl.add_column(width=10, style="dim")
        status_tbl.add_column(width=12)
        status_tbl.add_column(width=6, justify="right")

        status_tbl.add_row(Text("BY STATUS", style="bold dim"), Text(""), Text(""))
        for key in ("active", "pending", "complete", "failed", "skipped"):
            n = qs.by_status.get(key, 0)
            colour = STATUS_COLOURS.get(key, "white")
            bar    = _small_bar(n, total)
            row_txt = Text(bar, style=colour)
            status_tbl.add_row(
                Text(key, style=colour),
                row_txt,
                Text(str(n), style=colour + " bold"),
            )
        self.query_one("#tq-status", Static).update(status_tbl)

        # ── by type ──────────────────────────────────────
        type_tbl = Table.grid(padding=(0, 1))
        type_tbl.add_column(width=18, style="dim")
        type_tbl.add_column(width=12)
        type_tbl.add_column(width=6, justify="right")

        type_tbl.add_row(Text("BY TYPE", style="bold dim"), Text(""), Text(""))
        for key, colour in TYPE_COLOURS.items():
            n = qs.by_type.get(key, 0)
            if n == 0:
                continue
            bar = _small_bar(n, total)
            type_tbl.add_row(
                Text(key, style=colour),
                Text(bar, style=colour),
                Text(str(n), style=colour + " bold"),
            )
        self.query_one("#tq-type", Static).update(type_tbl)

        # ── recent task list ─────────────────────────────
        recent_tbl = Table.grid(padding=(0, 1))
        recent_tbl.add_column(style="dim", width=8)
        recent_tbl.add_column(width=12)
        recent_tbl.add_column()

        recent_tbl.add_row(Text("RECENT", style="bold dim"), Text(""), Text(""))
        for t in state.recent_tasks[-5:]:
            status_col = STATUS_COLOURS.get(t.status.value, "white")
            type_col   = TYPE_COLOURS.get(t.type.value, "white")
            recent_tbl.add_row(
                Text(t.id[-8:], style="dim"),
                Text(t.type.value, style=type_col),
                Text(t.description[:40], style="white"),
            )
        self.query_one("#tq-recent", Static).update(recent_tbl)
