"""Unit test configuration.

Provides shared fixtures for unit tests that must not touch the filesystem or
real external services.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Config fixture — patches load_config so unit tests run without config.yaml
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def patch_load_config():
    """Auto-patch app.config.load_config for every unit test.

    Returns a real AppConfig loaded from the committed test fixture so handlers
    that call load_config() work without a real config.yaml on disk.
    """
    from app.config import load_config as real_load_config

    cfg = real_load_config(_FIXTURES / "config.test.yaml")

    with patch("app.config.load_config", return_value=cfg):
        yield cfg
