"""
tinker/dashboard/app.py
────────────────────────
Main Textual application for the Tinker Observability Dashboard.

Layout (two-column)
───────────────────

╔══════════════════════════════════════════════════════════════════╗
║  TINKER  ● LIVE   [loop level]  μ:1234  M:56  Σ:7    HH:MM:SS  ║
╠══════════════════╦═══════════════════════════════════════════════╣
║  Loop Status     ║  Active Task                                  ║
╠══════════════════╣  ─────────────────────────────────────────── ║
║  Task Queue      ║  Architect Output                             ║
║                  ║  ─────────────────────────────────────────── ║
║                  ║  Critic Score                                 ║
║                  ║  ─────────────────────────────────────────── ║
╠══════════════════╣  Architecture State                           ║
║  Health          ║  ─────────────────────────────────────────── ║
║                  ║  Memory Stats                                 ║
╠══════════════════╩═══════════════════════════════════════════════╣
║  Live Log Stream (bottom, full width)                           ║
╠══════════════════════════════════════════════════════════════════╣
║  Footer: keybindings                                            ║
╚══════════════════════════════════════════════════════════════════╝

Keybindings
───────────
  q / ctrl+c   quit
  d            open detail view for active task
  a            open detail view for last architect output
  c            open detail view for last critic output
  s            open detail view for architecture state
  l            cycle log level filter (DEBUG → INFO → WARNING → ERROR)
  x            clear log panel
  r            force UI refresh
  f1           help overlay
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, Static

from .detail_view import (
    DetailScreen,
    detail_for_arch_state,
    detail_for_architect,
    detail_for_critic,
    detail_for_task,
)
from .panels import (
    ActiveTaskPanel,
    ArchitectPanel,
    ArchStatePanel,
    CriticPanel,
    GrubPanel,
    HealthPanel,
    LogStreamPanel,
    LoopStatusPanel,
    MemoryPanel,
    TaskQueuePanel,
)
from .state import LoopLevel, TinkerState, get_store
from .subscriber import BaseSubscriber, QueueSubscriber


# ──────────────────────────────────────────
# Help overlay
# ──────────────────────────────────────────

HELP_TEXT = """\
╔═══════════════════════════════════╗
║       TINKER DASHBOARD HELP       ║
╠═══════════════════════════════════╣
║  q / ctrl+c    Quit               ║
║  d             Detail: active task║
║  a             Detail: architect  ║
║  c             Detail: critic     ║
║  s             Detail: arch state ║
║  l             Cycle log filter   ║
║  x             Clear log          ║
║  r             Force refresh      ║
║  Esc           Close modal        ║
║  f1            This help          ║
╚═══════════════════════════════════╝
"""


class HelpScreen(DetailScreen):
    def __init__(self) -> None:
        super().__init__(title="Help", content=HELP_TEXT, render_as="text")


# ──────────────────────────────────────────
# Status bar (header supplement)
# ──────────────────────────────────────────

class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    def render_status(self, state: TinkerState) -> str:
        conn = (
            "[bold bright_green]● LIVE[/]"
            if state.connected
            else "[bold bright_red]○ DISCONNECTED[/]"
        )
        level_styles = {
            LoopLevel.MICRO: "[bright_cyan]μ MICRO[/]",
            LoopLevel.MESO:  "[bright_yellow]M MESO[/]",
            LoopLevel.MACRO: "[bright_magenta]Σ MACRO[/]",
        }
        loop = level_styles.get(state.loop_level, str(state.loop_level))
        ts   = datetime.utcnow().strftime("%H:%M:%S UTC")
        return (
            f" TINKER  {conn}   {loop}  "
            f"μ:[bright_cyan]{state.micro_count:,}[/]  "
            f"M:[bright_yellow]{state.meso_count:,}[/]  "
            f"Σ:[bright_magenta]{state.macro_count:,}[/]   "
            f"[dim]{ts}[/]"
        )

    def update_state(self, state: TinkerState) -> None:
        self.update(self.render_status(state))


# ──────────────────────────────────────────
# Main app
# ──────────────────────────────────────────

class TinkerDashboard(App[None]):
    """Tinker Observability Dashboard."""

    TITLE    = "Tinker Dashboard"
    CSS_PATH = "css/dashboard.tcss"

    BINDINGS = [
        Binding("q",      "quit",            "Quit",            priority=True),
        Binding("d",      "detail_task",     "Detail: task"),
        Binding("a",      "detail_architect","Detail: architect"),
        Binding("c",      "detail_critic",   "Detail: critic"),
        Binding("s",      "detail_arch",     "Detail: arch"),
        Binding("l",      "cycle_log_level", "Log level"),
        Binding("x",      "clear_log",       "Clear log"),
        Binding("r",      "refresh_ui",      "Refresh"),
        Binding("f1",     "help",            "Help"),
    ]

    # ── init ────────────────────────────────

    def __init__(
        self,
        subscriber: Optional[BaseSubscriber] = None,
        refresh_interval: float = 1.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._subscriber      = subscriber or QueueSubscriber(
            on_update=self._on_state_update)
        self._refresh_interval = refresh_interval
        self._sub_task: Optional[asyncio.Task] = None

    # ── compose ─────────────────────────────

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")

        with Horizontal(id="main-columns"):
            # Left column
            with Vertical(id="left-col"):
                yield LoopStatusPanel(id="panel-loop")
                yield TaskQueuePanel(id="panel-queue")
                yield HealthPanel(id="panel-health")

            # Right column
            with Vertical(id="right-col"):
                yield ActiveTaskPanel(id="panel-active")
                yield ArchitectPanel(id="panel-architect")
                yield CriticPanel(id="panel-critic")
                yield ArchStatePanel(id="panel-arch-state")
                yield MemoryPanel(id="panel-memory")
                yield GrubPanel(id="panel-grub")

        yield LogStreamPanel(id="panel-log")
        yield Footer()

    # ── lifecycle ────────────────────────────

    def on_mount(self) -> None:
        # Start the subscriber as an asyncio background task
        self._sub_task = asyncio.create_task(self._subscriber.run())

        # Periodic UI refresh
        self.set_interval(self._refresh_interval, self._tick)

        # Initial render
        self._refresh_all_panels(get_store().snapshot())

    async def on_unmount(self) -> None:
        self._subscriber.stop()
        if self._sub_task:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except asyncio.CancelledError:
                pass

    # ── state update pipeline ────────────────

    def _on_state_update(self) -> None:
        """Called by subscriber thread after each patch — schedules a UI tick."""
        self.call_from_thread(self._tick)

    def _tick(self) -> None:
        state = get_store().snapshot()
        self._refresh_all_panels(state)

    def _refresh_all_panels(self, state: TinkerState) -> None:
        self.query_one("#status-bar",       StatusBar).update_state(state)
        self.query_one("#panel-loop",       LoopStatusPanel).refresh_state(state)
        self.query_one("#panel-queue",      TaskQueuePanel).refresh_state(state)
        self.query_one("#panel-health",     HealthPanel).refresh_state(state)
        self.query_one("#panel-active",     ActiveTaskPanel).refresh_state(state)
        self.query_one("#panel-architect",  ArchitectPanel).refresh_state(state)
        self.query_one("#panel-critic",     CriticPanel).refresh_state(state)
        self.query_one("#panel-arch-state", ArchStatePanel).refresh_state(state)
        self.query_one("#panel-memory",     MemoryPanel).refresh_state(state)

    # ── actions ─────────────────────────────

    def action_detail_task(self) -> None:
        state = get_store().snapshot()
        if state.active_task:
            self.push_screen(detail_for_task(state.active_task))

    def action_detail_architect(self) -> None:
        state = get_store().snapshot()
        if state.last_architect:
            self.push_screen(detail_for_architect(state.last_architect))

    def action_detail_critic(self) -> None:
        state = get_store().snapshot()
        if state.last_critic:
            self.push_screen(detail_for_critic(state.last_critic))

    def action_detail_arch(self) -> None:
        state = get_store().snapshot()
        if state.arch_state:
            self.push_screen(detail_for_arch_state(state.arch_state))

    def action_cycle_log_level(self) -> None:
        self.query_one("#panel-log", LogStreamPanel).action_cycle_level()

    def action_clear_log(self) -> None:
        self.query_one("#panel-log", LogStreamPanel).action_clear()

    def action_refresh_ui(self) -> None:
        self._tick()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
