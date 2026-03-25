"""
agents/grub/tests/test_tools.py
========================
Tests for grub/tools/* — file I/O, shell commands, code analysis.

All tests use tmp_path (pytest's built-in temporary directory fixture)
so they never touch real project files.
"""

import pytest
import sys
from pathlib import Path

from agents.grub.tools.file_ops import (
    read_file,
    write_file,
    append_file,
    list_files,
    ensure_dir,
)
from agents.grub.tools.shell import run_command, check_syntax
from agents.grub.tools.code_analysis import (
    count_lines,
    extract_functions,
    extract_imports,
    summarise_file,
)


# ═══════════════════════════════════════════════════════════════════════════════
# file_ops
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileOps:
    def test_write_then_read(self, tmp_path):
        p = tmp_path / "hello.txt"
        ok, path = write_file(p, "hello world")
        assert ok is True
        ok2, content = read_file(p)
        assert ok2 is True
        assert content == "hello world"

    def test_write_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "c" / "file.txt"
        ok, _ = write_file(p, "data")
        assert ok is True
        assert p.exists()

    def test_read_missing_file(self, tmp_path):
        ok, msg = read_file(tmp_path / "does_not_exist.txt")
        assert ok is False
        assert "not found" in msg.lower() or "File" in msg

    def test_append_creates_file(self, tmp_path):
        p = tmp_path / "log.txt"
        ok, _ = append_file(p, "line 1\n")
        assert ok is True
        ok2, _ = append_file(p, "line 2\n")
        assert ok2 is True
        _, content = read_file(p)
        assert "line 1" in content
        assert "line 2" in content

    def test_list_files_returns_files(self, tmp_path):
        (tmp_path / "a.py").write_text("pass")
        (tmp_path / "b.py").write_text("pass")
        ok, files = list_files(tmp_path, "*.py")
        assert ok is True
        assert len(files) == 2

    def test_list_files_missing_dir(self, tmp_path):
        ok, files = list_files(tmp_path / "nonexistent")
        assert ok is False
        assert files == []

    def test_ensure_dir_creates_nested(self, tmp_path):
        target = tmp_path / "x" / "y" / "z"
        ok, path = ensure_dir(target)
        assert ok is True
        assert target.exists()

    def test_ensure_dir_idempotent(self, tmp_path):
        """Calling ensure_dir on existing dir should not raise."""
        ok, _ = ensure_dir(tmp_path)
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# shell
# ═══════════════════════════════════════════════════════════════════════════════


class TestShell:
    def test_run_command_success(self):
        result = run_command([sys.executable, "-c", "print('hello')"])
        assert result.succeeded is True
        assert "hello" in result.stdout

    def test_run_command_failure(self):
        result = run_command([sys.executable, "-c", "import sys; sys.exit(1)"])
        assert result.succeeded is False
        assert result.returncode == 1

    def test_run_command_timeout(self):
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.5,
        )
        assert result.succeeded is False
        assert "timeout" in result.stderr.lower() or result.returncode == -1

    def test_check_syntax_valid_file(self, tmp_path):
        p = tmp_path / "valid.py"
        p.write_text("x = 1\ndef foo():\n    return x\n")
        result = check_syntax(p)
        assert result.succeeded is True

    def test_check_syntax_invalid_file(self, tmp_path):
        p = tmp_path / "invalid.py"
        p.write_text("def foo(\n")  # syntax error: unclosed paren
        result = check_syntax(p)
        assert result.succeeded is False


# ═══════════════════════════════════════════════════════════════════════════════
# code_analysis
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_PY = """\
import os
import sys
from pathlib import Path


CONSTANT = 42


class MyClass:
    def method_one(self, x: int) -> int:
        '''Return double x.'''
        return x * 2

    def _private(self) -> None:
        pass


def top_level(name: str) -> str:
    return f"Hello {name}"
"""


class TestCodeAnalysis:
    @pytest.fixture
    def sample_file(self, tmp_path) -> Path:
        p = tmp_path / "sample.py"
        p.write_text(SAMPLE_PY)
        return p

    def test_count_lines_total(self, sample_file):
        result = count_lines(sample_file)
        assert result["total"] > 0
        assert result["error"] is None

    def test_count_lines_blank_and_code(self, sample_file):
        result = count_lines(sample_file)
        assert result["blank"] > 0
        assert result["code"] > 0

    def test_extract_functions_finds_methods_and_top_level(self, sample_file):
        funcs = extract_functions(sample_file)
        names = [f["name"] for f in funcs]
        assert "method_one" in names
        assert "_private" in names
        assert "top_level" in names

    def test_extract_functions_marks_async(self, tmp_path):
        p = tmp_path / "async_code.py"
        p.write_text("async def fetch(): pass\ndef sync(): pass\n")
        funcs = extract_functions(p)
        by_name = {f["name"]: f for f in funcs}
        assert by_name["fetch"]["is_async"] is True
        assert by_name["sync"]["is_async"] is False

    def test_extract_imports(self, sample_file):
        imports = extract_imports(sample_file)
        assert "os" in imports
        assert "sys" in imports
        assert "pathlib" in imports

    def test_summarise_file_returns_expected_keys(self, sample_file):
        summary = summarise_file(sample_file)
        assert "path" in summary
        assert "lines" in summary
        assert "functions" in summary
        assert "classes" in summary
        assert "imports" in summary

    def test_summarise_file_finds_class(self, sample_file):
        summary = summarise_file(sample_file)
        assert "MyClass" in summary["classes"]

    def test_extract_functions_on_missing_file(self, tmp_path):
        result = extract_functions(tmp_path / "ghost.py")
        assert result == []  # graceful empty return

    def test_count_lines_on_missing_file(self, tmp_path):
        result = count_lines(tmp_path / "ghost.py")
        assert result["error"] is not None
