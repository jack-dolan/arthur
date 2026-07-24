"""Deployment guard: the IANA time-zone database must ship with the app.

The production image is built on ``python:3.12-slim``, which contains **no**
system time-zone database (no ``/usr/share/zoneinfo``). Every ET-based domain
rule (access-code 4 PM→11 AM window, HOA send window, the 8 AM daily-reminders
cron) resolves a zone via :class:`zoneinfo.ZoneInfo`. If the tz data is not
bundled as the ``tzdata`` PyPI package, ``ZoneInfo("US/Eastern")`` raises
``ZoneInfoNotFoundError`` and the app crashes on startup.

These tests fail on any environment that relies on the OS tz database instead
of the packaged one — exactly the slim-container case that broke the deploy.
"""

import importlib.metadata

import pytest
from zoneinfo import ZoneInfo


def test_tzdata_package_is_installed():
    """`tzdata` must be an installed distribution, not an OS-provided file tree.

    RED when tzdata is absent from dependencies (PackageNotFoundError);
    GREEN once it is declared in pyproject and installed.
    """
    # Raises importlib.metadata.PackageNotFoundError if tzdata is not installed.
    version = importlib.metadata.version("tzdata")
    assert version


@pytest.mark.parametrize("key", ["US/Eastern", "America/New_York"])
def test_app_timezone_keys_resolve(key):
    """Every tz key the app constructs must be resolvable."""
    assert ZoneInfo(key) is not None
