"""tinker/dashboard/panels/loop_status.py — Current loop level and counters."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from .state import LoopLevel, TinkerState

LEVEL_STYLE = {
    LoopLevel.MICRO: ("bold bright_cyan",   "μ  MICRO"),
    LoopLevel.MESO:  ("bold bright_yellow", "M  MESO"),
    LoopLevel.MACRO: ("bold bright_magenta","Σ  MACRO"),
}


class LoopStatusPanel(Widget):
    """Shows current loop level and micro/meso/macro counters."""

    DEFAULT_CSS = """
    LoopStatusPanel {
        height: 7;
        border: round $primary;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="loop-level")
        yield Static("", id="loop-counters")
        yield Static("", id="loop-updated")

    def refresh_state(self, state: TinkerState) -> None:
        style, label = LEVEL_STYLE.get(
            state.loop_level, ("white", str(state.loop_level))
        )

        # Level line
        level_text = Text()
        level_text.append(" Loop Level  ", style="bold dim")
        level_text.append(f" {label} ", style=f"{style} on #1a1a2e")
        self.query_one("#loop-level", Static).update(level_text)

        # Counter line
        counter_text = Text()
        counter_text.append("  μ micro ", style="dim")
        counter_text.append(f"{state.micro_count:>6,}", style="bright_cyan bold")
        counter_text.append("   M meso ", style="dim")
        counter_text.append(f"{state.meso_count:>5,}", style="bright_yellow bold")
        counter_text.append("   Σ macro ", style="dim")
        counter_text.append(f"{state.macro_count:>4,}", style="bright_magenta bold")
        self.query_one("#loop-counters", Static).update(counter_text)

        # Timestamp
        ts = state.last_update
        ts_str = ts.strftime("%H:%M:%S") if ts else "—"
        upd = Text(f"  last update  {ts_str}", style="dim")
        self.query_one("#loop-updated", Static).update(upd)

    def on_mount(self) -> None:
        from ..state import get_store
        self.refresh_state(get_store().snapshot())
