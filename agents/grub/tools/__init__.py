"""
agents/grub/tools/__init__.py
======================
Shared tools available to all Minions.

Tools are simple functions — they do one thing and return a result.
Minions import what they need; they do not inherit from a 'tool base class'.

Available tools
---------------
file_ops    : Read, write, create, delete files and directories.
shell       : Run shell commands, capture stdout/stderr.
git_ops     : Git status, diff, add, commit helpers.
code_analysis: Simple static analysis (count lines, find imports, etc.).
"""

from .code_analysis import count_lines, extract_functions, extract_imports
from .file_ops import append_file, ensure_dir, list_files, read_file, write_file
from .git_ops import git_add, git_commit, git_diff, git_status
from .shell import run_command, run_tests

__all__ = [
    "append_file",
    "count_lines",
    "ensure_dir",
    "extract_functions",
    "extract_imports",
    "git_add",
    "git_commit",
    "git_diff",
    "git_status",
    "list_files",
    "read_file",
    "run_command",
    "run_tests",
    "write_file",
]
