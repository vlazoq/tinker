"""
agents/grub/tools/file_ops.py
======================
File system helpers for Minions.

Why these wrappers instead of using open() directly?
-----------------------------------------------------
1. They all return (success, data/error) tuples — Minions never crash on a
   missing file, they get a clear error message they can include in their result.
2. They log every operation, so you can audit what the system wrote.
3. They create parent directories automatically — Minions shouldn't have to
   think about whether 'output/api_gateway/' exists before writing to it.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_file(path: str | Path) -> tuple[bool, str]:
    """
    Read a text file.

    Parameters
    ----------
    path : File path to read.

    Returns
    -------
    (True, content)        on success
    (False, error_message) on failure
    """
    try:
        p = Path(path)
        if not p.exists():
            return False, f"File not found: {path}"
        content = p.read_text(encoding="utf-8")
        logger.debug("read_file: %s (%d chars)", path, len(content))
        return True, content
    except Exception as exc:
        logger.warning("read_file failed for %s: %s", path, exc)
        return False, str(exc)


def write_file(path: str | Path, content: str) -> tuple[bool, str]:
    """
    Write content to a file, creating parent directories if needed.

    This OVERWRITES the file if it already exists.

    Parameters
    ----------
    path    : File path to write.
    content : Text content to write.

    Returns
    -------
    (True, absolute_path_string) on success
    (False, error_message)       on failure
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info("write_file: %s (%d chars)", p.resolve(), len(content))
        return True, str(p.resolve())
    except Exception as exc:
        logger.warning("write_file failed for %s: %s", path, exc)
        return False, str(exc)


def append_file(path: str | Path, content: str) -> tuple[bool, str]:
    """
    Append content to a file (creates the file if it doesn't exist).

    Parameters
    ----------
    path    : File path to append to.
    content : Text to append.

    Returns
    -------
    (True, absolute_path_string) on success
    (False, error_message)       on failure
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        logger.debug("append_file: %s (+%d chars)", p.resolve(), len(content))
        return True, str(p.resolve())
    except Exception as exc:
        logger.warning("append_file failed for %s: %s", path, exc)
        return False, str(exc)


def list_files(
    directory: str | Path,
    pattern: str = "**/*",
    include_dirs: bool = False,
) -> tuple[bool, list[str]]:
    """
    List files in a directory.

    Parameters
    ----------
    directory    : Directory to list.
    pattern      : Glob pattern. Default '**/*' lists all files recursively.
                   Use '*.py' for Python files only, etc.
    include_dirs : If True, include directory entries in the result.

    Returns
    -------
    (True, [paths...]) on success
    (False, [])        on failure (directory doesn't exist, etc.)
    """
    try:
        d = Path(directory)
        if not d.exists():
            return False, []
        paths = []
        for p in d.glob(pattern):
            if include_dirs or p.is_file():
                paths.append(str(p))
        paths.sort()
        logger.debug("list_files: %s → %d items", directory, len(paths))
        return True, paths
    except Exception as exc:
        logger.warning("list_files failed for %s: %s", directory, exc)
        return False, []


def ensure_dir(path: str | Path) -> tuple[bool, str]:
    """
    Create a directory and all parents if they don't exist.

    Safe to call even if the directory already exists.

    Returns
    -------
    (True, absolute_path_string) on success
    (False, error_message)       on failure
    """
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return True, str(p.resolve())
    except Exception as exc:
        logger.warning("ensure_dir failed for %s: %s", path, exc)
        return False, str(exc)


def delete_file(path: str | Path) -> tuple[bool, str]:
    """
    Delete a file.  Does NOT delete directories.

    Returns
    -------
    (True, "deleted")          on success
    (True, "not found")        if file didn't exist (not an error)
    (False, error_message)     on failure
    """
    try:
        p = Path(path)
        if not p.exists():
            return True, "not found"
        if p.is_dir():
            return False, f"{path} is a directory; use shutil.rmtree() instead"
        p.unlink()
        logger.info("delete_file: %s", path)
        return True, "deleted"
    except Exception as exc:
        logger.warning("delete_file failed for %s: %s", path, exc)
        return False, str(exc)
