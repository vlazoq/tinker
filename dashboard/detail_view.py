"""
tinker/dashboard/detail_view.py
────────────────────────────────
A full-screen modal that shows the raw/full content of any selectable item
(TaskInfo, ArchitectOutput, CriticOutput, ArchitectureState).

Usage
─────
    self.app.push_screen(DetailScreen(title="Task detail", content=some_text))

Close with Escape, q, or Enter.
"""

from __future__ import annotations

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static


class DetailScreen(ModalScreen[None]):
    """
    Modal overlay that renders arbitrary content with Rich formatting.

    Parameters
    ──────────
    title   : str           – panel title bar text
    content : str           – raw text / markdown to display
    render_as : str         – "text" | "markdown" | "json"
    """

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 90%;
        height: 90%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    #detail-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    #detail-log {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    #detail-footer {
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
    ]

    def __init__(
        self,
        title: str = "Detail",
        content: str = "",
        render_as: str = "text",  # "text" | "markdown" | "json"
    ) -> None:
        super().__init__()
        self._title = title
        self._content = content
        self._render_as = render_as

    def compose(self) -> ComposeResult:
        with Static(id="detail-container"):
            yield Static(f"  {self._title}", id="detail-title")
            yield RichLog(
                id="detail-log",
                highlight=True,
                markup=False,
                wrap=True,
                max_lines=10_000,
            )
            yield Static(
                Text(" Esc / q / Enter — close", style="dim"), id="detail-footer"
            )

    def on_mount(self) -> None:
        log = self.query_one("#detail-log", RichLog)
        content = self._content or "(empty)"

        if self._render_as == "markdown":
            log.write(Markdown(content))
        elif self._render_as == "json":
            log.write(Syntax(content, "json", theme="monokai", word_wrap=True))
        else:
            log.write(Text(content))

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


# ──────────────────────────────────────────
# Convenience builders
# ──────────────────────────────────────────

from .state import (  # noqa: E402
    ArchitectOutput,
    ArchitectureState,
    CriticOutput,
    TaskInfo,
)


def detail_for_task(task: TaskInfo) -> DetailScreen:
    lines = [
        f"# Task  {task.id}",
        "",
        f"**Type**      {task.type.value}",
        f"**Subsystem** {task.subsystem}",
        f"**Status**    {task.status.value}",
        f"**Created**   {task.created_at}",
        f"**Started**   {task.started_at or '—'}",
        f"**Completed** {task.completed_at or '—'}",
        "",
        "## Description",
        task.description,
    ]
    if task.result_summary:
        lines += ["", "## Result Summary", task.result_summary]
    if task.full_content:
        lines += ["", "## Full Content", task.full_content]
    return DetailScreen(
        title=f"Task Detail — {task.id}",
        content="\n".join(lines),
        render_as="markdown",
    )


def detail_for_architect(a: ArchitectOutput) -> DetailScreen:
    lines = [
        "# Architect Output",
        f"**Timestamp** {a.timestamp}",
        f"**Task**      {a.task_id or '—'}",
        "",
        "## Summary",
        a.summary,
        "",
        "## Full Content",
        a.full_content,
    ]
    return DetailScreen(
        title="Architect Output — Detail",
        content="\n".join(lines),
        render_as="markdown",
    )


def detail_for_critic(c: CriticOutput) -> DetailScreen:
    lines = [
        "# Critic Output",
        f"**Timestamp** {c.timestamp}",
        f"**Score**     {c.score:.1f}/10",
        f"**Task**      {c.task_id or '—'}",
        "",
        "## Top Objection",
        c.top_objection,
        "",
        "## Full Critique",
        c.full_content,
    ]
    return DetailScreen(
        title="Critic Output — Detail",
        content="\n".join(lines),
        render_as="markdown",
    )


def detail_for_arch_state(a: ArchitectureState) -> DetailScreen:
    lines = [
        f"# Architecture State  v{a.version}",
        f"**Last Commit** {a.last_commit_time or '—'}",
        "",
        "## Summary",
        a.summary,
        "",
        "## Full Specification",
        a.full_content,
    ]
    return DetailScreen(
        title=f"Architecture State — v{a.version}",
        content="\n".join(lines),
        render_as="markdown",
    )
