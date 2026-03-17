"""
tinker/dashboard/panels/health_arch.py
────────────────────────────────────────
Three panels:
  • ArchStatePanel    – architecture version + last-commit + summary
  • HealthPanel       – stagnation monitor, model latency, error rate
  • MemoryPanel       – session artifacts, research archive, token usage
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from .state import TinkerState


# ─────────────────────────────────────────────────────────────────
# Architecture State
# ─────────────────────────────────────────────────────────────────

class ArchStatePanel(Widget):
    DEFAULT_CSS = """
    ArchStatePanel {
        height: 9;
        border: round #6a5acd;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(" Architecture State", style="bold dim"))
        yield Static("", id="as-body")

    def refresh_state(self, state: TinkerState) -> None:
        arch = state.arch_state
        if arch is None:
            self.query_one("#as-body", Static).update(
                Text("  — no architecture committed yet —", style="dim italic"))
            return

        ts = (arch.last_commit_time.strftime("%Y-%m-%d %H:%M:%S")
              if arch.last_commit_time else "—")

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=12)
        tbl.add_column()

        tbl.add_row("version",     Text(arch.version, style="bold bright_magenta"))
        tbl.add_row("last commit", Text(ts, style="dim cyan"))
        summary = arch.summary[:160] + ("…" if len(arch.summary) > 160 else "")
        tbl.add_row("summary",     Text(summary, style="white"))

        self.query_one("#as-body", Static).update(tbl)


# ─────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────

def _latency_style(ms: float) -> str:
    if ms < 500:
        return "bright_green"
    if ms < 2000:
        return "bright_yellow"
    return "bright_red"


def _error_rate_style(rate: float) -> str:
    if rate < 0.01:
        return "bright_green"
    if rate < 0.05:
        return "bright_yellow"
    return "bold bright_red"


def _stagnation_style(score: float) -> str:
    if score < 0.3:
        return "bright_green"
    if score < 0.6:
        return "bright_yellow"
    return "bold bright_red"


class HealthPanel(Widget):
    DEFAULT_CSS = """
    HealthPanel {
        height: 16;
        border: round #d9a84a;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(" System Health", style="bold dim"))
        yield Static("", id="hp-stagnation")
        yield Static("", id="hp-model")
        yield Static("", id="hp-events")

    def refresh_state(self, state: TinkerState) -> None:
        stag = state.stagnation
        mm   = state.model_metrics

        # ── stagnation ──────────────────────────────────
        s_tbl = Table.grid(padding=(0, 1))
        s_tbl.add_column(style="dim", width=16)
        s_tbl.add_column()

        stag_score_style = _stagnation_style(stag.stagnation_score)
        stag_flag = Text()
        if stag.is_stagnant:
            stag_flag.append("⚠  STAGNANT", style="bold bright_red on dark_red")
        else:
            stag_flag.append("✓  flowing",  style="bright_green")

        bar_width = 16
        filled = int(stag.stagnation_score * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        s_tbl.add_row(Text("STAGNATION", style="bold dim"), Text(""))
        s_tbl.add_row("status",   stag_flag)
        s_tbl.add_row("score",    Text(
            f"{stag.stagnation_score:.2f}  {bar}", style=stag_score_style))
        s_tbl.add_row("monitor",  Text(stag.monitor_status, style="dim"))
        self.query_one("#hp-stagnation", Static).update(s_tbl)

        # ── model metrics ────────────────────────────────
        m_tbl = Table.grid(padding=(0, 1))
        m_tbl.add_column(style="dim", width=16)
        m_tbl.add_column()

        m_tbl.add_row(Text("MODEL METRICS", style="bold dim"), Text(""))
        m_tbl.add_row("avg latency",
            Text(f"{mm.avg_latency_ms:.0f} ms", style=_latency_style(mm.avg_latency_ms)))
        m_tbl.add_row("p99 latency",
            Text(f"{mm.p99_latency_ms:.0f} ms", style=_latency_style(mm.p99_latency_ms)))
        m_tbl.add_row("error rate",
            Text(f"{mm.error_rate*100:.1f}%", style=_error_rate_style(mm.error_rate)))
        m_tbl.add_row("total calls",
            Text(f"{mm.total_calls:,}", style="bright_white"))
        self.query_one("#hp-model", Static).update(m_tbl)

        # ── recent stagnation events ─────────────────────
        ev_tbl = Table.grid(padding=(0, 1))
        ev_tbl.add_column(style="dim", width=10)
        ev_tbl.add_column()

        ev_tbl.add_row(Text("EVENTS", style="bold dim"), Text(""))
        events = stag.recent_events[-3:]
        if not events:
            ev_tbl.add_row("", Text("none", style="dim italic"))
        for ev in events:
            ts_str = ev.timestamp.strftime("%H:%M:%S")
            ev_tbl.add_row(
                Text(ts_str, style="dim"),
                Text(ev.description[:60], style="yellow"),
            )
        self.query_one("#hp-events", Static).update(ev_tbl)


# ─────────────────────────────────────────────────────────────────
# Memory
# ─────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


class MemoryPanel(Widget):
    DEFAULT_CSS = """
    MemoryPanel {
        height: 7;
        border: round #3a9a6e;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(" Memory Stats", style="bold dim"))
        yield Static("", id="mem-body")

    def refresh_state(self, state: TinkerState) -> None:
        m = state.memory_stats

        tbl = Table.grid(padding=(0, 2))
        tbl.add_column(style="dim", width=22)
        tbl.add_column(justify="right")

        tbl.add_row(
            "session artifacts",
            Text(f"{m.session_artifact_count:,}", style="bold bright_cyan"))
        tbl.add_row(
            "research archive",
            Text(_fmt_size(m.research_archive_size), style="bold bright_blue"))
        tbl.add_row(
            "working mem tokens",
            Text(f"{m.working_memory_tokens:,}", style="bold bright_green"))

        self.query_one("#mem-body", Static).update(tbl)
