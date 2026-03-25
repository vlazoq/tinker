"""tinker/dashboard/panels/log_stream.py — Live log tail panel."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

from .log_handler import LEVEL_COLOURS, LogRecord, get_log_buffer

LEVEL_ORDER = ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]


def _format_record(rec: LogRecord) -> Text:
    ts_str = rec.timestamp.strftime("%H:%M:%S.%f")[:-3]
    colour = LEVEL_COLOURS.get(rec.level, "white")

    line = Text()
    line.append(f"{ts_str} ", style="dim")
    line.append(f"{rec.level:<8}", style=colour + " bold")
    line.append(f" {rec.source:<30} ", style="dim")
    line.append(rec.message, style="white")
    return line


class LogStreamPanel(Widget):
    DEFAULT_CSS = """
    LogStreamPanel {
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #log-controls {
        height: 1;
        layout: horizontal;
    }
    #log-richlog {
        height: 1fr;
    }
    """

    # Minimum log level to display (index into LEVEL_ORDER)
    _min_level_idx: int = 2  # INFO

    # Cursor into the log buffer (last seen position)
    _cursor: int = 0

    def compose(self) -> ComposeResult:
        yield Static(" Live Logs  filter: INFO+", id="log-header")
        yield RichLog(
            id="log-richlog", highlight=False, markup=False, wrap=True, max_lines=500
        )

    def on_mount(self) -> None:
        buf = get_log_buffer()
        # Seed with existing tail so panel isn't blank on launch
        seed, self._cursor = buf.since(0)
        rich_log = self.query_one("#log-richlog", RichLog)
        for rec in seed[-80:]:
            if self._passes_filter(rec):
                rich_log.write(self._format(rec))

        # Poll for new lines every 250 ms
        self.set_interval(0.25, self._poll)

    def _passes_filter(self, rec: LogRecord) -> bool:
        idx = LEVEL_ORDER.index(rec.level) if rec.level in LEVEL_ORDER else 2
        return idx >= self._min_level_idx

    def _format(self, rec: LogRecord) -> Text:
        return _format_record(rec)

    def _poll(self) -> None:
        buf = get_log_buffer()
        new_records, self._cursor = buf.since(self._cursor)
        if not new_records:
            return
        rich_log = self.query_one("#log-richlog", RichLog)
        for rec in new_records:
            if self._passes_filter(rec):
                rich_log.write(self._format(rec))

    def set_min_level(self, level: str) -> None:
        """Called by keybindings in the app to change filter."""
        if level in LEVEL_ORDER:
            self._min_level_idx = LEVEL_ORDER.index(level)
            hdr = self.query_one("#log-header", Static)
            hdr.update(f" Live Logs  filter: {level}+")

    def action_cycle_level(self) -> None:
        levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        cur = LEVEL_ORDER[self._min_level_idx]
        nxt = levels[(levels.index(cur) + 1) % len(levels)] if cur in levels else "INFO"
        self.set_min_level(nxt)

    def action_clear(self) -> None:
        self.query_one("#log-richlog", RichLog).clear()
