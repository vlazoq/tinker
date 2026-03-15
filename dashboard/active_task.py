"""tinker/dashboard/panels/active_task.py — Currently running task."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..state import TaskInfo, TaskStatus, TaskType, TinkerState

TYPE_STYLE: dict[TaskType, str] = {
    TaskType.DESIGN:           "bright_cyan",
    TaskType.CRITIQUE:         "bright_yellow",
    TaskType.REFINE:           "bright_green",
    TaskType.RESEARCH:         "bright_blue",
    TaskType.COMMIT:           "bright_magenta",
    TaskType.STAGNATION_BREAK: "bright_red",
}

STATUS_STYLE: dict[TaskStatus, str] = {
    TaskStatus.PENDING:  "dim yellow",
    TaskStatus.ACTIVE:   "bold bright_green",
    TaskStatus.COMPLETE: "dim green",
    TaskStatus.FAILED:   "bold bright_red",
    TaskStatus.SKIPPED:  "dim white",
}


def _elapsed(started: datetime | None) -> str:
    if not started:
        return "—"
    delta = datetime.utcnow() - started.replace(tzinfo=None)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s"


class ActiveTaskPanel(Widget):
    DEFAULT_CSS = """
    ActiveTaskPanel {
        height: 10;
        border: round $accent;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="at-header")
        yield Static("", id="at-body")

    def refresh_state(self, state: TinkerState) -> None:
        task = state.active_task

        header = Text()
        header.append(" Active Task", style="bold dim")
        if state.connected:
            header.append("  ● LIVE", style="bold bright_green")
        else:
            header.append("  ○ DISCONNECTED", style="bold bright_red")
        self.query_one("#at-header", Static).update(header)

        if task is None:
            self.query_one("#at-body", Static).update(
                Text("  — idle —", style="dim italic"))
            return

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=12)
        tbl.add_column()

        type_style = TYPE_STYLE.get(task.type, "white")
        status_style = STATUS_STYLE.get(task.status, "white")

        tbl.add_row("id",        Text(task.id, style="bold"))
        tbl.add_row("type",      Text(f"[{task.type.value}]", style=type_style))
        tbl.add_row("subsystem", Text(task.subsystem, style="cyan"))
        tbl.add_row("status",    Text(task.status.value.upper(), style=status_style))
        tbl.add_row("elapsed",   Text(_elapsed(task.started_at), style="bright_white"))
        tbl.add_row("desc",      Text(task.description[:80], style="white"))

        self.query_one("#at-body", Static).update(tbl)
