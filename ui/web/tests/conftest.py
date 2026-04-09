"""Shared fixtures for ui/web/tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Clear per-IP rate-limiter buckets so each test starts fresh."""
    from ui.web.app import _ip_limiters

    _ip_limiters.clear()
