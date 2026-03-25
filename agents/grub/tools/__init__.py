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

from .file_ops import read_file, write_file, append_file, list_files, ensure_dir
from .shell import run_command, run_tests
from .git_ops import git_status, git_diff, git_add, git_commit
from .code_analysis import count_lines, extract_functions, extract_imports

__all__ = [
    "read_file",
    "write_file",
    "append_file",
    "list_files",
    "ensure_dir",
    "run_command",
    "run_tests",
    "git_status",
    "git_diff",
    "git_add",
    "git_commit",
    "count_lines",
    "extract_functions",
    "extract_imports",
]
