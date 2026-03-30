"""File I/O utilities with atomic writes and safe JSON handling."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write(path: Path, content: str | bytes, mode: str = "w") -> None:
    """Write *content* to *path* atomically.

    Creates a temporary file in the same directory, writes to it, then
    replaces the target via ``os.replace()`` so readers never see a
    partial write.
    """
    path = Path(path)
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)

    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode=mode,
            dir=dir_,
            delete=False,
            suffix=".tmp",
        ) as fd:
            tmp_name = fd.name
            fd.write(content)
            fd.flush()
            os.fsync(fd.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        raise


def safe_json_load(path: Path, default: Any = None) -> Any:
    """Load JSON from *path*, returning *default* on any error.

    Handles missing files, permission errors, and invalid JSON gracefully.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def safe_json_dump(path: Path, data: Any, indent: int = 2) -> None:
    """Atomically write *data* as JSON to *path*."""
    atomic_write(Path(path), json.dumps(data, indent=indent, ensure_ascii=False) + "\n")
