"""
grub/tests/test_config.py
=========================
Tests for GrubConfig — loading, saving, validation, mode switching.
"""

import json
import pytest
from pathlib import Path

from grub.config import GrubConfig


class TestGrubConfig:

    def test_defaults_are_sequential_mode(self):
        cfg = GrubConfig()
        assert cfg.execution_mode == "sequential"

    def test_validate_passes_for_defaults(self):
        cfg    = GrubConfig()
        errors = cfg.validate()
        assert errors == []

    def test_validate_rejects_unknown_mode(self):
        cfg = GrubConfig(execution_mode="telekinesis")
        errors = cfg.validate()
        assert any("execution_mode" in e for e in errors)

    def test_validate_rejects_threshold_out_of_range(self):
        cfg = GrubConfig(quality_threshold=1.5)
        errors = cfg.validate()
        assert any("quality_threshold" in e for e in errors)

    def test_validate_rejects_zero_max_iterations(self):
        cfg = GrubConfig(max_iterations=0)
        errors = cfg.validate()
        assert any("max_iterations" in e for e in errors)

    def test_to_dict_round_trip(self):
        cfg  = GrubConfig(execution_mode="parallel", quality_threshold=0.8)
        d    = cfg.to_dict()
        cfg2 = GrubConfig.from_dict(d)
        assert cfg2.execution_mode    == "parallel"
        assert cfg2.quality_threshold == 0.8

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "grub_config.json"
        cfg  = GrubConfig(execution_mode="queue", max_iterations=3)
        cfg.save(path)

        loaded = GrubConfig.load(path)
        assert loaded.execution_mode == "queue"
        assert loaded.max_iterations == 3

    def test_load_creates_file_if_missing(self, tmp_path):
        path = tmp_path / "new_config.json"
        assert not path.exists()
        cfg = GrubConfig.load(path)
        # File should now exist with defaults
        assert path.exists()
        assert cfg.execution_mode == "sequential"

    def test_load_handles_corrupt_json(self, tmp_path, capsys):
        path = tmp_path / "bad.json"
        path.write_text("{ not valid json }")
        # Should not raise — returns defaults
        cfg = GrubConfig.load(path)
        assert cfg.execution_mode == "sequential"

    def test_mode_switch_documented_values(self):
        """All three documented modes must pass validation."""
        for mode in ("sequential", "parallel", "queue"):
            cfg    = GrubConfig(execution_mode=mode)
            errors = cfg.validate()
            assert errors == [], f"Mode '{mode}' failed validation: {errors}"
