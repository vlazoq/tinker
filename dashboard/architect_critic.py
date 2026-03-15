"""tinker/dashboard/panels/architect_critic.py — Last Architect + Critic outputs."""

from __future__ import annotations

from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..state import TinkerState


class ArchitectPanel(Widget):
    DEFAULT_CSS = """
    ArchitectPanel {
        height: 8;
        border: round #4a90d9;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(" Last Architect Output", style="bold dim"))
        yield Static("", id="arch-body")

    def refresh_state(self, state: TinkerState) -> None:
        a = state.last_architect
        if a is None:
            self.query_one("#arch-body", Static).update(
                Text("  — no output yet —", style="dim italic"))
            return

        ts = a.timestamp.strftime("%H:%M:%S")
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=10)
        tbl.add_column()
        tbl.add_row("time",    Text(ts, style="dim"))
        tbl.add_row("task",    Text(a.task_id or "—", style="dim cyan"))
        # Wrap summary at ~80 chars
        summary = a.summary[:160] + ("…" if len(a.summary) > 160 else "")
        tbl.add_row("summary", Text(summary, style="bright_white"))
        self.query_one("#arch-body", Static).update(tbl)


# ─────────────────────────────────────────────────────────────────

def _score_style(score: float) -> str:
    if score >= 8.0:
        return "bold bright_green"
    if score >= 6.0:
        return "bright_yellow"
    if score >= 4.0:
        return "yellow"
    return "bold bright_red"


def _score_bar(score: float, width: int = 20) -> str:
    filled = int(round(score / 10.0 * width))
    empty  = width - filled
    style  = _score_style(score)
    bar    = "█" * filled + "░" * empty
    return bar


class CriticPanel(Widget):
    DEFAULT_CSS = """
    CriticPanel {
        height: 8;
        border: round #d94a4a;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(" Last Critic Score", style="bold dim"))
        yield Static("", id="critic-body")

    def refresh_state(self, state: TinkerState) -> None:
        c = state.last_critic
        if c is None:
            self.query_one("#critic-body", Static).update(
                Text("  — no critique yet —", style="dim italic"))
            return

        ts    = c.timestamp.strftime("%H:%M:%S")
        style = _score_style(c.score)
        bar   = _score_bar(c.score)

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=10)
        tbl.add_column()

        score_text = Text()
        score_text.append(f"{c.score:4.1f}/10  ", style=style + " bold")
        score_text.append(bar, style=style)

        tbl.add_row("time",      Text(ts, style="dim"))
        tbl.add_row("score",     score_text)
        objection = c.top_objection[:120] + ("…" if len(c.top_objection) > 120 else "")
        tbl.add_row("objection", Text(objection, style="bright_red"))

        self.query_one("#critic-body", Static).update(tbl)
