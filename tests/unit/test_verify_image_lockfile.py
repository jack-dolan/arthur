"""Self-tests for ``scripts/verify_image_lockfile.py`` — the image drift guard.

The guard's job is to fail the build when the production image's installed
packages stop matching ``uv.lock``. That regression is not hypothetical: the
image previously installed ``-e .``, re-resolved the ranges in
``pyproject.toml`` at build time, and shipped 39 of 83 packages at versions
nothing had tested.

The comparison is a pure function, so it is tested here directly with
synthetic package sets — no Docker, no network, no image. Only ``main()``
shells out, and it is not exercised by these tests.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_image_lockfile.py"


def _load_guard():
    """Import the script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("verify_image_lockfile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def expect(version: str, applies: bool | None = True) -> dict:
    """Shape of one entry in the image's exported requirements manifest."""
    return {"version": version, "applies": applies}


# A minimal but realistic trio: two runtime packages and one dev-only package
# that the lockfile pins but the image should not contain.
LOCK = {"fastapi": "0.136.1", "seam": "1.145.0", "pytest": "8.4.2"}


def test_matching_image_reports_no_problems():
    result = guard.compare(
        lock=LOCK,
        installed={"fastapi": "0.136.1", "seam": "1.145.0"},
        expected={"fastapi": expect("0.136.1"), "seam": expect("1.145.0")},
    )
    assert result.problems == []


def test_version_drift_is_reported_with_both_versions():
    """The original defect: the image re-resolved and installed something newer."""
    result = guard.compare(
        lock=LOCK,
        installed={"fastapi": "0.140.0", "seam": "1.145.0"},
        expected={"fastapi": expect("0.136.1"), "seam": expect("1.145.0")},
    )
    assert len(result.problems) == 1
    assert "0.140.0" in result.problems[0] and "0.136.1" in result.problems[0]


def test_package_installed_but_absent_from_the_lockfile_is_reported():
    result = guard.compare(
        lock=LOCK,
        installed={"fastapi": "0.136.1", "seam": "1.145.0", "requests": "2.32.3"},
        expected={"fastapi": expect("0.136.1"), "seam": expect("1.145.0")},
    )
    assert any("requests" in p for p in result.problems)


def test_allowlisted_packaging_plumbing_may_be_unlocked():
    """pip/setuptools/wheel can be seeded by an installer; that is not drift."""
    result = guard.compare(
        lock=LOCK,
        installed={"fastapi": "0.136.1", "seam": "1.145.0", "pip": "25.2"},
        expected={"fastapi": expect("0.136.1"), "seam": expect("1.145.0")},
    )
    assert result.problems == []
    assert result.allowed_present == ["pip==25.2"]


def test_allowlist_never_forgives_a_version_mismatch():
    """The allowlist forgives absence from the lockfile, not a wrong version."""
    lock = {**LOCK, "setuptools": "83.0.0"}
    result = guard.compare(
        lock=lock,
        installed={"fastapi": "0.136.1", "seam": "1.145.0", "setuptools": "70.0.0"},
        expected={"fastapi": expect("0.136.1"), "seam": expect("1.145.0")},
    )
    assert any("setuptools" in p for p in result.problems)


def test_expected_package_missing_from_the_image_is_reported():
    """Catches a partial or silently-failed install."""
    result = guard.compare(
        lock=LOCK,
        installed={"fastapi": "0.136.1"},
        expected={"fastapi": expect("0.136.1"), "seam": expect("1.145.0")},
    )
    assert any("seam" in p for p in result.problems)


def test_inapplicable_marker_is_not_a_missing_package():
    """`colorama ; sys_platform == 'win32'` is exported but not installed here."""
    lock = {**LOCK, "colorama": "0.4.6"}
    result = guard.compare(
        lock=lock,
        installed={"fastapi": "0.136.1", "seam": "1.145.0"},
        expected={
            "fastapi": expect("0.136.1"),
            "seam": expect("1.145.0"),
            "colorama": expect("0.4.6", applies=False),
        },
    )
    assert result.problems == []
    assert result.not_applicable == ["colorama"]


def test_unevaluated_marker_is_not_treated_as_a_failure():
    """If `packaging` is unavailable in the image the marker is unknown, not false."""
    lock = {**LOCK, "colorama": "0.4.6"}
    result = guard.compare(
        lock=lock,
        installed={"fastapi": "0.136.1", "seam": "1.145.0"},
        expected={
            "fastapi": expect("0.136.1"),
            "seam": expect("1.145.0"),
            "colorama": expect("0.4.6", applies=None),
        },
    )
    assert result.problems == []


def test_manifest_pin_disagreeing_with_the_lockfile_is_reported():
    """The image was built from some other lockfile than the committed one."""
    result = guard.compare(
        lock=LOCK,
        installed={"fastapi": "0.140.0", "seam": "1.145.0"},
        expected={"fastapi": expect("0.140.0"), "seam": expect("1.145.0")},
    )
    assert any("different lockfile" in p for p in result.problems)


def test_image_without_the_exported_manifest_fails_loudly():
    """An image built the OLD way has no manifest at all.

    This is the regression the guard exists to catch, so it must produce a
    clear verdict rather than an exception or a vacuous pass.
    """
    result = guard.compare(lock=LOCK, installed={"fastapi": "0.140.0"}, expected=None)
    assert result.problems
    assert any("not built from the lockfile" in p for p in result.problems)


def test_an_empty_image_is_not_a_vacuous_pass():
    """No installed packages and no manifest must never read as 'no drift'."""
    result = guard.compare(lock=LOCK, installed={}, expected=None)
    assert result.problems


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("Google_Auth", "google-auth"),
        ("zope.interface", "zope-interface"),
        ("PyYAML", "pyyaml"),
    ],
)
def test_names_are_pep503_normalised(raw, normalised):
    """`google_auth` and `google-auth` must not read as two different packages."""
    assert guard.normalise(raw) == normalised
