"""
core/tools/examples.py
======================

Example tools demonstrating Tinker's plugin pattern.

These tools show how easy it is to extend Tinker with custom capabilities.
Each tool is a BaseTool subclass that can be registered in the ToolRegistry
and called by the AI model or the orchestrator.

To register an example tool::

    from core.tools.examples import FileReaderTool, ShellTool, DatabaseQueryTool
    registry.register(FileReaderTool(allowed_dirs=["./tinker_workspace"]))

All tools operate locally — no cloud services, no external APIs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .base import BaseTool, ToolSchema

logger = logging.getLogger("tinker.tools.examples")


class FileReaderTool(BaseTool):
    """Read local files from allowed directories.

    Parameters
    ----------
    allowed_dirs : list[str]
        Directories the tool is allowed to read from.  Paths outside
        these directories are rejected for safety.
    max_size_bytes : int
        Maximum file size to read (default 1 MB).
    """

    def __init__(
        self,
        allowed_dirs: list[str] | None = None,
        max_size_bytes: int = 1_048_576,
    ) -> None:
        self._allowed = [
            Path(d).resolve()
            for d in (allowed_dirs or [os.getenv("TINKER_WORKSPACE", "./tinker_workspace")])
        ]
        self._max_size = max_size_bytes

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="file_reader",
            description=(
                "Read the contents of a local file. Restricted to configured "
                "workspace directories for safety."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read (relative or absolute).",
                    },
                    "encoding": {
                        "type": "string",
                        "description": "Text encoding (default utf-8).",
                    },
                },
                "required": ["path"],
            },
            returns="Dict with path, content, size_bytes, and encoding.",
        )

    async def _execute(self, path: str, encoding: str = "utf-8", **_) -> dict:
        resolved = Path(path).resolve()

        # Security: check path is within allowed directories
        if not any(self._is_subpath(resolved, allowed) for allowed in self._allowed):
            raise PermissionError(
                f"Path '{resolved}' is outside allowed directories: {self._allowed}"
            )

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {resolved}")

        size = resolved.stat().st_size
        if size > self._max_size:
            raise ValueError(f"File too large ({size} bytes, max {self._max_size})")

        content = resolved.read_text(encoding=encoding)
        return {
            "path": str(resolved),
            "content": content,
            "size_bytes": size,
            "encoding": encoding,
        }

    @staticmethod
    def _is_subpath(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


class ShellTool(BaseTool):
    """Execute shell commands locally with timeout and output limits.

    Parameters
    ----------
    allowed_commands : list[str] | None
        Whitelist of allowed command prefixes (e.g. ["ls", "cat", "grep"]).
        If None, all commands are allowed (use with caution).
    timeout : float
        Max seconds for command execution (default 30).
    max_output : int
        Max characters of stdout+stderr to return (default 10000).
    """

    def __init__(
        self,
        allowed_commands: list[str] | None = None,
        timeout: float = 30.0,
        max_output: int = 10_000,
    ) -> None:
        raw = os.getenv("TINKER_SHELL_ALLOWED_COMMANDS", "")
        self._allowed = allowed_commands or (raw.split(",") if raw else None)
        self._timeout = timeout
        self._max_output = max_output

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="shell",
            description=(
                "Execute a shell command locally and return stdout/stderr. "
                "Commands can be restricted to a whitelist for safety."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional).",
                    },
                },
                "required": ["command"],
            },
            returns="Dict with returncode, stdout, stderr, and timed_out flag.",
        )

    async def _execute(self, command: str, cwd: str | None = None, **_) -> dict:
        # Security: check command whitelist
        if self._allowed:
            cmd_base = command.strip().split()[0] if command.strip() else ""
            if cmd_base not in self._allowed:
                raise PermissionError(f"Command '{cmd_base}' not in allowed list: {self._allowed}")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            return {
                "returncode": proc.returncode,
                "stdout": stdout.decode(errors="replace")[: self._max_output],
                "stderr": stderr.decode(errors="replace")[: self._max_output],
                "timed_out": False,
            }
        except TimeoutError:
            proc.kill()
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {self._timeout}s",
                "timed_out": True,
            }


class DatabaseQueryTool(BaseTool):
    """Execute read-only SQL queries against a local SQLite database.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file.
    max_rows : int
        Maximum rows to return (default 100).
    """

    def __init__(
        self,
        db_path: str | None = None,
        max_rows: int = 100,
    ) -> None:
        self._db_path = db_path or os.getenv("TINKER_TASK_DB", "tinker_tasks_engine.sqlite")
        self._max_rows = max_rows

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="database_query",
            description=(
                "Execute a read-only SQL query against the local SQLite "
                "database. Only SELECT statements are allowed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL SELECT query to execute.",
                    },
                },
                "required": ["query"],
            },
            returns="Dict with columns, rows, and row_count.",
        )

    async def _execute(self, query: str, **_) -> dict:
        import sqlite3

        # Security: only allow SELECT queries
        normalized = query.strip().upper()
        if not normalized.startswith("SELECT"):
            raise PermissionError("Only SELECT queries are allowed.")

        # Block dangerous keywords
        dangerous = ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "ATTACH"]
        for keyword in dangerous:
            if keyword in normalized:
                raise PermissionError(f"Query contains forbidden keyword: {keyword}")

        def _run():
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(query)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = [dict(row) for row in cursor.fetchmany(self._max_rows)]
                return columns, rows
            finally:
                conn.close()

        loop = asyncio.get_running_loop()
        columns, rows = await loop.run_in_executor(None, _run)

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) >= self._max_rows,
        }
