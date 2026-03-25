"""
agents/grub/tools/code_analysis.py
============================
Simple static analysis helpers.

These are used by the Reviewer Minion to gather facts about the code
before asking the LLM to evaluate it.  Feeding the LLM structured facts
("this file has 200 lines, 8 functions, imports os/sys/json") produces
better reviews than just sending raw code.

STATUS: FULLY IMPLEMENTED
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


def count_lines(path: Union[str, Path]) -> dict:
    """
    Count total lines, code lines, comment lines, and blank lines.

    Parameters
    ----------
    path : Path to a Python file.

    Returns
    -------
    dict with keys: total, code, comments, blank, error
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        blank = sum(1 for ln in lines if not ln.strip())
        comments = sum(1 for ln in lines if ln.strip().startswith("#"))
        code = len(lines) - blank - comments
        return {
            "total": len(lines),
            "code": code,
            "comments": comments,
            "blank": blank,
            "error": None,
        }
    except Exception as exc:
        return {"total": 0, "code": 0, "comments": 0, "blank": 0, "error": str(exc)}


def extract_functions(path: Union[str, Path]) -> list[dict]:
    """
    Extract all function and method definitions from a Python file.

    Uses Python's built-in AST parser — works on any valid Python file
    without executing it.

    Parameters
    ----------
    path : Path to a Python file.

    Returns
    -------
    List of dicts, each with:
      name       : function name
      lineno     : line number where it's defined
      args       : list of argument names
      is_async   : True if it's an 'async def'
      docstring  : first line of docstring, or ""
      class_name : name of the containing class, or "" for top-level functions
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        logger.warning("extract_functions: could not parse %s: %s", path, exc)
        return []

    functions = []

    def _visit(node, class_name: str = ""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Get argument names (skip 'self', 'cls')
            args = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
            # Get first line of docstring if present
            docstring = ""
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring = node.body[0].value.value.split("\n")[0].strip()

            functions.append(
                {
                    "name": node.name,
                    "lineno": node.lineno,
                    "args": args,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "docstring": docstring,
                    "class_name": class_name,
                }
            )
            # Recurse into nested functions
            for child in ast.iter_child_nodes(node):
                _visit(child, class_name)

        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                _visit(child, class_name=node.name)
        else:
            for child in ast.iter_child_nodes(node):
                _visit(child, class_name)

    _visit(tree)
    return functions


def extract_imports(path: Union[str, Path]) -> list[str]:
    """
    Extract all import statements from a Python file.

    Parameters
    ----------
    path : Path to a Python file.

    Returns
    -------
    Sorted list of imported module names.
    Example: ["asyncio", "json", "pathlib", "typing"]
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        logger.warning("extract_imports: could not parse %s: %s", path, exc)
        return []

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])  # top-level module
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return sorted(imports)


def summarise_file(path: Union[str, Path]) -> dict:
    """
    Produce a structured summary of a Python file.

    Combines count_lines, extract_functions, and extract_imports into
    one convenient call.  Used by the Reviewer Minion to feed the LLM
    a concise description of the code before it reads the full text.

    Parameters
    ----------
    path : Path to a Python file.

    Returns
    -------
    dict with keys: path, lines, functions, imports, classes
    """
    path = Path(path)
    lines = count_lines(path)
    functions = extract_functions(path)
    imports = extract_imports(path)

    # Extract class names separately
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ]
    except Exception as exc:
        logger.warning("analyze_file: could not parse classes in %s: %s", path, exc)
        classes = []

    return {
        "path": str(path),
        "lines": lines,
        "functions": [f["name"] for f in functions],
        "classes": classes,
        "imports": imports,
    }
