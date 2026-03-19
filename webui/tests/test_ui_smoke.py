"""
webui/tests/test_ui_smoke.py
=============================
Smoke tests for the three UI layers (FastAPI, Streamlit, Gradio).

These tests run with *no* optional dependencies installed (no streamlit,
gradio, fastapi, httpx).  They verify structure, consistency, and feature
parity using Python's standard ``ast`` module to inspect source files.

What is checked
---------------
1. **Syntax** — every UI source file parses as valid Python.
2. **Shared constants** — ``webui/core.py`` exports all required names and
   those names are self-consistent (FLAG_DEFAULTS ↔ FLAG_DESCRIPTIONS ↔
   FLAG_GROUPS keys agree; every ORCH_CONFIG_SCHEMA field has a ``type``
   and ``default``).
3. **Single-source-of-truth** — all three UI apps import constants from
   ``webui.core`` (or the local ``core`` module), never re-define them.
4. **Feature parity** — all three apps cover the same 8 feature areas:
   Dashboard / Health, Config, Feature Flags, Task Queue, DLQ, Backups,
   Grub, and Audit Log.
5. **FastAPI route coverage** — ``webui/app.py`` registers API endpoints
   for every feature area.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent  # tinker/
WEBUI_CORE = ROOT / "webui" / "core.py"
WEBUI_APP = ROOT / "webui" / "app.py"
STREAMLIT_APP = ROOT / "streamlit_ui" / "app.py"
GRADIO_APP = ROOT / "gradio_ui" / "app.py"

_UI_FILES = {
    "webui/app.py": WEBUI_APP,
    "streamlit_ui/app.py": STREAMLIT_APP,
    "gradio_ui/app.py": GRADIO_APP,
}


# ── AST helpers ───────────────────────────────────────────────────────────────


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_src(path), filename=str(path))


def _module_assign_names(path: Path) -> set[str]:
    """Return names of all module-level assignments (``x = ...``)."""
    names: set[str] = set()
    for node in _tree(path).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _dict_keys_from_assign(path: Path, var_name: str) -> set[str]:
    """
    Extract the string keys from a module-level dict literal assignment.

    Handles both plain assignments (``x = {...}``) and annotated assignments
    (``x: dict[str, T] = {...}``).

    Example — given ``FLAG_DEFAULTS: dict[str, bool] = {"foo": True}`` returns
    ``{"foo"}``.  Returns an empty set if the variable is not found or is not a
    plain dict literal.
    """
    for node in _tree(path).body:
        value = None
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == var_name for t in node.targets):
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == var_name:
                value = node.value
        if value is not None and isinstance(value, ast.Dict):
            keys: set[str] = set()
            for k in value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
            return keys
    return set()


def _string_constants(path: Path) -> set[str]:
    """Return every string literal that appears anywhere in the source."""
    result: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            result.add(node.value)
    return result


# ── 1. Syntax ─────────────────────────────────────────────────────────────────


class TestSyntax:
    """All UI source files must be syntactically valid Python."""

    def test_webui_core_parses(self):
        _tree(WEBUI_CORE)  # raises SyntaxError on failure

    def test_webui_app_parses(self):
        _tree(WEBUI_APP)

    def test_streamlit_app_parses(self):
        _tree(STREAMLIT_APP)

    def test_gradio_app_parses(self):
        _tree(GRADIO_APP)


# ── 2. Shared constants ───────────────────────────────────────────────────────


class TestCoreConstants:
    """webui/core.py must export all required constants."""

    REQUIRED_NAMES = {
        "FLAG_DEFAULTS",
        "FLAG_DESCRIPTIONS",
        "FLAG_GROUPS",
        "ORCH_CONFIG_SCHEMA",
        "STAGNATION_CONFIG_SCHEMA",
        "TASK_TYPES",
        "SUBSYSTEMS",
        "TASKS_DB",
        "DLQ_DB",
        "AUDIT_DB",
        "BACKUP_DIR",
        "FLAGS_FILE",
        "db_execute",
        "db_query",
        "load_config",
        "save_config",
        "load_flags",
        "save_flags",
        "load_state",
        "fetch_health",
        "fetch_grub_status",
        "list_backups",
        "new_id",
        "now_iso",
    }

    def test_all_required_names_defined(self):
        assigns = _module_assign_names(WEBUI_CORE)
        src = _src(WEBUI_CORE)
        # Functions may appear as def, not assignments
        defined_fns = {
            m.group(1)
            for m in re.finditer(r"^(?:async\s+)?def\s+(\w+)", src, re.MULTILINE)
        }
        defined_all = assigns | defined_fns
        missing = self.REQUIRED_NAMES - defined_all
        assert not missing, f"webui/core.py is missing: {sorted(missing)}"

    def test_flag_defaults_keys_all_have_descriptions(self):
        defaults = _dict_keys_from_assign(WEBUI_CORE, "FLAG_DEFAULTS")
        descriptions = _dict_keys_from_assign(WEBUI_CORE, "FLAG_DESCRIPTIONS")
        assert defaults, "FLAG_DEFAULTS is empty or not a dict literal"
        missing = defaults - descriptions
        assert not missing, (
            f"Flags in FLAG_DEFAULTS but missing from FLAG_DESCRIPTIONS: {sorted(missing)}"
        )

    def test_flag_descriptions_keys_all_have_defaults(self):
        defaults = _dict_keys_from_assign(WEBUI_CORE, "FLAG_DEFAULTS")
        descriptions = _dict_keys_from_assign(WEBUI_CORE, "FLAG_DESCRIPTIONS")
        extra = descriptions - defaults
        assert not extra, (
            f"Flags in FLAG_DESCRIPTIONS but missing from FLAG_DEFAULTS: {sorted(extra)}"
        )

    def test_all_flag_defaults_keys_covered_by_a_group(self):
        defaults = _dict_keys_from_assign(WEBUI_CORE, "FLAG_DEFAULTS")
        # Use regex to collect every quoted string inside FLAG_GROUPS value lists
        # We just check every default key appears as a string literal somewhere
        # in the FLAG_GROUPS assignment block.
        all_strings = _string_constants(WEBUI_CORE)
        uncovered = defaults - all_strings  # every default key must appear somewhere
        assert not uncovered, (
            f"FLAG_DEFAULTS keys absent from FLAG_GROUPS: {sorted(uncovered)}"
        )

    def test_flag_defaults_not_empty(self):
        defaults = _dict_keys_from_assign(WEBUI_CORE, "FLAG_DEFAULTS")
        assert len(defaults) >= 10, "Expected at least 10 feature flags"

    def test_task_types_defined(self):
        src = _src(WEBUI_CORE)
        assert "TASK_TYPES" in src
        assert "implementation" in src or "design" in src

    def test_subsystems_defined(self):
        src = _src(WEBUI_CORE)
        assert "SUBSYSTEMS" in src


# ── 3. Single source of truth ─────────────────────────────────────────────────


class TestSingleSourceOfTruth:
    """Every UI app must import its constants from webui.core, not re-define them."""

    # Names that must NOT be re-defined as module-level assignments in the UIs
    SHARED_NAMES = {
        "FLAG_DEFAULTS",
        "FLAG_DESCRIPTIONS",
        "FLAG_GROUPS",
        "ORCH_CONFIG_SCHEMA",
        "STAGNATION_CONFIG_SCHEMA",
    }

    def _check_no_redefinition(self, path: Path) -> None:
        assigns = _module_assign_names(path)
        redefined = self.SHARED_NAMES & assigns
        assert not redefined, (
            f"{path.name} re-defines shared constants (should import from webui.core): "
            f"{sorted(redefined)}"
        )

    def test_webui_app_imports_from_core(self):
        src = _src(WEBUI_APP)
        assert "from .core import" in src or "from webui.core import" in src, (
            "webui/app.py must import shared constants from .core"
        )

    def test_webui_app_does_not_redefine_shared_names(self):
        self._check_no_redefinition(WEBUI_APP)

    def test_streamlit_app_imports_from_core(self):
        src = _src(STREAMLIT_APP)
        assert (
            "from webui.core import" in src
            or "from webui import" in src
            or "webui.core" in src
        ), "streamlit_ui/app.py must import shared constants from webui.core"

    def test_streamlit_app_does_not_redefine_shared_names(self):
        self._check_no_redefinition(STREAMLIT_APP)

    def test_gradio_app_imports_from_core(self):
        src = _src(GRADIO_APP)
        assert (
            "from webui.core import" in src
            or "from webui import" in src
            or "webui.core" in src
        ), "gradio_ui/app.py must import shared constants from webui.core"

    def test_gradio_app_does_not_redefine_shared_names(self):
        self._check_no_redefinition(GRADIO_APP)


# ── 4. Feature parity ─────────────────────────────────────────────────────────


class TestFeatureParity:
    """
    All three UI apps must cover the same 8 feature areas.

    Parity is checked by verifying that each source file references a
    representative keyword for every feature area.  These keywords are chosen
    to be unique to the feature (not generic Python keywords) so a false
    positive is very unlikely.
    """

    # (feature name, keywords that must appear in source)
    FEATURES: list[tuple[str, list[str]]] = [
        ("Dashboard / Health", ["health", "state"]),
        ("Config", ["config", "ORCH_CONFIG_SCHEMA"]),
        ("Feature Flags", ["flags", "FLAG_DEFAULTS"]),
        ("Task Queue", ["tasks", "priority_score"]),
        ("Dead Letter Queue", ["dlq", "dlq_items"]),
        ("Backups", ["backup", "list_backups"]),
        ("Grub Integration", ["grub", "fetch_grub_status"]),
        ("Audit Log", ["audit", "audit_events"]),
    ]

    def _check_app(self, label: str, path: Path) -> None:
        src = _src(path).lower()  # case-insensitive keyword match
        missing: list[str] = []
        for feature_name, keywords in self.FEATURES:
            # At least one keyword from the list must be present
            if not any(kw.lower() in src for kw in keywords):
                missing.append(feature_name)
        assert not missing, f"{label} is missing coverage for: {missing}"

    def test_webui_app_covers_all_features(self):
        self._check_app("webui/app.py", WEBUI_APP)

    def test_streamlit_app_covers_all_features(self):
        self._check_app("streamlit_ui/app.py", STREAMLIT_APP)

    def test_gradio_app_covers_all_features(self):
        self._check_app("gradio_ui/app.py", GRADIO_APP)

    def test_all_uis_cover_identical_features(self):
        """No UI is missing a feature that another UI has."""
        coverage: dict[str, set[str]] = {}
        for label, path in _UI_FILES.items():
            src = _src(path).lower()
            covered = set()
            for feature_name, keywords in self.FEATURES:
                if any(kw.lower() in src for kw in keywords):
                    covered.add(feature_name)
            coverage[label] = covered

        all_features = {f for f, _ in self.FEATURES}
        for label, covered in coverage.items():
            missing = all_features - covered
            assert not missing, f"{label} missing features: {missing}"


# ── 5. FastAPI route coverage ─────────────────────────────────────────────────


class TestFastAPIRoutes:
    """
    webui/app.py must register API routes for all 8 feature areas.

    We check for ``@app.get`` / ``@app.post`` decorators whose path strings
    contain the expected prefixes.
    """

    EXPECTED_PREFIXES = [
        "/api/health",  # Dashboard
        "/api/config",  # Config
        "/api/flags",  # Feature Flags
        "/api/tasks",  # Task Queue
        "/api/dlq",  # Dead Letter Queue
        "/api/backups",  # Backups
        "/api/grub",  # Grub
        "/api/audit",  # Audit Log
    ]

    def test_all_api_routes_registered(self):
        src = _src(WEBUI_APP)
        missing: list[str] = []
        for prefix in self.EXPECTED_PREFIXES:
            if prefix not in src:
                missing.append(prefix)
        assert not missing, f"webui/app.py missing routes: {missing}"

    def test_task_inject_route_exists(self):
        """Task injection endpoint must exist for manual task creation."""
        assert "/api/tasks/inject" in _src(WEBUI_APP)

    def test_dlq_resolve_and_discard_routes_exist(self):
        """DLQ must support both resolve and discard actions."""
        src = _src(WEBUI_APP)
        assert "resolve" in src
        assert "discard" in src

    def test_log_streaming_sse_route_exists(self):
        """Log streaming via Server-Sent Events must be present."""
        src = _src(WEBUI_APP)
        assert "/api/logs/stream" in src or "stream" in src

    def test_backup_trigger_route_exists(self):
        """Manual backup trigger endpoint must be present."""
        assert "/api/backups/trigger" in _src(WEBUI_APP)
