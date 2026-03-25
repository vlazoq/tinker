"""
Tests for features/flags.py
==============================

Verifies flag defaults, env-var overrides, programmatic set/get,
change callbacks, and file-based loading.
"""

from __future__ import annotations

import json
import pytest

from tinker_platform.features.flags import FeatureFlags


@pytest.fixture
def flags():
    """A fresh FeatureFlags instance with no env overrides."""
    return FeatureFlags(config_file=None)


class TestFeatureFlagDefaults:
    def test_circuit_breakers_enabled_by_default(self, flags):
        assert flags.is_enabled("circuit_breakers_enabled") is True

    def test_rate_limiting_enabled_by_default(self, flags):
        assert flags.is_enabled("rate_limiting_enabled") is True

    def test_unknown_flag_returns_false(self, flags):
        # An unknown flag should default to False (safe default)
        assert flags.is_enabled("completely_unknown_flag_xyz") is False


class TestProgrammaticOverride:
    def test_set_and_get(self, flags):
        flags.set("researcher_calls_enabled", False)
        assert flags.is_enabled("researcher_calls_enabled") is False

    def test_set_back_to_true(self, flags):
        flags.set("researcher_calls_enabled", False)
        flags.set("researcher_calls_enabled", True)
        assert flags.is_enabled("researcher_calls_enabled") is True

    def test_all_returns_dict(self, flags):
        result = flags.all()
        assert isinstance(result, dict)
        assert len(result) > 0


class TestEnvVarOverride:
    def test_env_var_overrides_default(self, monkeypatch, flags):
        # TINKER_FLAG_MESO_SYNTHESIS_ENABLED=false should disable the flag
        monkeypatch.setenv("TINKER_FLAG_MESO_SYNTHESIS_ENABLED", "false")
        # Create a new instance so it picks up the env var
        fresh_flags = FeatureFlags(config_file=None)
        assert fresh_flags.is_enabled("meso_synthesis_enabled") is False

    def test_env_var_true_enables_flag(self, monkeypatch):
        monkeypatch.setenv("TINKER_FLAG_SOME_CUSTOM_FLAG", "true")
        fresh_flags = FeatureFlags(config_file=None)
        assert fresh_flags.is_enabled("some_custom_flag") is True


class TestOnChangeCallback:
    def test_callback_called_on_set(self, flags):
        changes = []
        flags.on_change("researcher_calls_enabled", lambda k, v: changes.append((k, v)))
        flags.set("researcher_calls_enabled", False)
        assert len(changes) == 1
        assert changes[0] == ("researcher_calls_enabled", False)

    def test_callback_not_called_if_value_unchanged(self, flags):
        changes = []
        current = flags.is_enabled("researcher_calls_enabled")
        flags.on_change("researcher_calls_enabled", lambda k, v: changes.append((k, v)))
        flags.set("researcher_calls_enabled", current)  # same value
        # Callback should not fire for no-op changes
        assert len(changes) == 0


class TestFileBasedFlags:
    def test_loads_from_json_file(self, tmp_path):
        flags_file = tmp_path / "flags.json"
        flags_file.write_text(json.dumps({"custom_flag": True, "another_flag": False}))
        fresh_flags = FeatureFlags(config_file=str(flags_file))
        assert fresh_flags.is_enabled("custom_flag") is True
        assert fresh_flags.is_enabled("another_flag") is False

    def test_missing_file_uses_defaults(self):
        fresh_flags = FeatureFlags(config_file="/nonexistent/path/flags.json")
        # Should not crash; defaults should apply
        assert isinstance(fresh_flags.all(), dict)
