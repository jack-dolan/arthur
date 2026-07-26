#!/usr/bin/env python3
"""Fail if a built image's installed packages diverge from ``uv.lock``.

Why this exists
---------------
The Dockerfile used to run ``uv pip install -e .``, which re-resolved the
version *ranges* in ``pyproject.toml`` against PyPI at build time and never
read ``uv.lock``. The result: 39 of the image's 83 packages differed from the
lockfile, so the test suite and the dependency audit -- both of which install
``uv.lock`` -- were describing a dependency set that production did not run.

The Dockerfile now exports the lockfile with ``uv export --locked`` and
installs exactly that. This script is the mechanical guard that keeps it true:
turn the rule into something a machine enforces, or it lasts until the first
person who edits the Dockerfile in a hurry.

What it checks (all three, because each catches a different regression)
----------------------------------------------------------------------
1. Every distribution installed in the image is pinned by ``uv.lock`` at the
   same version. Catches the original defect (a fresh resolve) and any
   hand-added ``pip install`` in the Dockerfile.
2. Every distribution the image's own ``requirements.lock.txt`` names is
   actually installed. Catches a partial or silently-failed install.
3. Every pin in that ``requirements.lock.txt`` matches ``uv.lock``. The file is
   produced inside the build, so without this check the guard could be
   comparing the image against an export of some *other* lockfile; this makes
   the repo's committed ``uv.lock`` the authority.

Usage:
    python3 scripts/verify_image_lockfile.py IMAGE[:TAG] [--lock uv.lock]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # noqa: S404 - runs docker with a literal argv, never a shell string
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Distributions permitted to be installed without appearing in uv.lock.
#
# These are packaging plumbing, not application dependencies: an installer may
# seed them into a virtualenv, and their presence says nothing about what the
# application will import at runtime. Note the guard is still strict about
# them WHEN they are locked -- `setuptools` is currently a genuine transitive
# dependency of this project and so is version-checked like anything else; the
# allowlist only forgives absence from the lockfile, never a version mismatch.
#
# Nothing else belongs here. An unexpected package is a finding, not noise.
ALLOWED_UNLOCKED = {"pip", "setuptools", "wheel"}

# The requirements file the builder stage exported and copied into the image.
IMAGE_REQUIREMENTS = "/opt/venv/requirements.lock.txt"

# Run inside the image, with the image's own interpreter, so both halves of the
# comparison come from exactly the environment the entrypoint will use. That
# matters twice over: `importlib.metadata` sees the real venv, and the exported
# requirements carry environment markers (`colorama ; sys_platform == 'win32'`)
# that are only meaningful when evaluated against the target platform -- doing
# that on the CI runner instead would report phantom missing packages.
_INSPECT = r"""
import importlib.metadata as md, json, re, sys

installed = {d.metadata["Name"]: d.version for d in md.distributions()}

try:
    from packaging.requirements import Requirement
except ImportError:      # packaging is a transitive dep, not a guarantee
    Requirement = None

try:
    lines = open("%s").readlines()
except FileNotFoundError:
    # An image built the OLD way (`uv pip install -e .`) has no manifest.
    # `None` is the signal for that, and the caller treats it as a failure --
    # never as "nothing to compare, therefore fine".
    lines = None

expected = None if lines is None else {}
for line in lines or []:
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)(?:\s*;\s*(.+?))?\s*\\?$", line)
    if not m:
        continue
    name, version, marker = m.group(1), m.group(2), m.group(3)
    if marker is None:
        applies = True
    elif Requirement is None:
        applies = None   # unknown: caller must not treat absence as a failure
    else:
        applies = Requirement(f"{name}; {marker}").marker.evaluate()
    expected[name] = {"version": version, "applies": applies}

json.dump({"installed": installed, "expected": expected}, sys.stdout)
""" % IMAGE_REQUIREMENTS


def normalise(name: str) -> str:
    """PEP 503 normalisation: `Foo.Bar_baz` and `foo-bar-baz` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def docker_capture(image: str, argv: list[str]) -> str:
    """Run a command in the image and return stdout, or exit with its error."""
    result = subprocess.run(  # noqa: S603 - literal argv, image name comes from the caller
        ["docker", "run", "--rm", "--entrypoint", argv[0], image, *argv[1:]],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"error: `{' '.join(argv)}` failed in {image}:\n{result.stderr.strip()}")
    return result.stdout


@dataclass
class Comparison:
    """Verdict of one image-vs-lockfile comparison.

    `problems` non-empty means the build must fail. The other two lists are
    reported for transparency and are never, on their own, a failure.
    """

    problems: list[str] = field(default_factory=list)
    allowed_present: list[str] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)


def compare(
    lock: dict[str, str],
    installed: dict[str, str],
    expected: dict[str, dict] | None,
) -> Comparison:
    """Compare an image's packages against the lockfile. Pure; see module docs.

    `expected` is the image's own exported manifest, or `None` when the image
    does not carry one -- which means it was not built from the lockfile at
    all, and is itself the finding.
    """
    result = Comparison()

    if expected is None:
        result.problems.append(
            f"{IMAGE_REQUIREMENTS} is absent: the image was not built from the lockfile "
            "(an image that re-resolves pyproject.toml's ranges looks exactly like this)"
        )
        expected = {}

    # 1. every installed distribution is pinned by the lockfile, at this version
    for name, version in sorted(installed.items()):
        if name in lock:
            if lock[name] != version:
                result.problems.append(f"{name}: image has {version}, uv.lock pins {lock[name]}")
        elif name in ALLOWED_UNLOCKED:
            result.allowed_present.append(f"{name}=={version}")
        else:
            result.problems.append(f"{name}: installed ({version}) but absent from uv.lock")

    # 2. everything the image's own manifest names is actually installed
    for name, entry in sorted(expected.items()):
        if name in installed:
            continue
        if entry["applies"] is False:
            # e.g. `colorama ; sys_platform == 'win32'` -- exported because the
            # lockfile is cross-platform, correctly not installed on this one.
            result.not_applicable.append(name)
        elif entry["applies"] is None:
            result.not_applicable.append(f"{name} (marker not evaluated: packaging missing)")
        else:
            result.problems.append(f"{name}: named in {IMAGE_REQUIREMENTS} but not installed")

    # 3. that manifest was exported from THIS lockfile, not some other one
    for name, entry in sorted(expected.items()):
        if name not in lock:
            result.problems.append(f"{name}: in {IMAGE_REQUIREMENTS} but absent from uv.lock")
        elif lock[name] != entry["version"]:
            result.problems.append(
                f"{name}: {IMAGE_REQUIREMENTS} pins {entry['version']}, uv.lock pins "
                f"{lock[name]} (the image was built from a different lockfile)"
            )

    return result


def parse_lock(path: Path) -> dict[str, str]:
    """Read every package pinned by uv.lock, dev and non-dev alike.

    Deliberately not filtered to the runtime subset: the question this answers
    is "is this installed version the version the lockfile resolved?", and a
    dev-only package showing up in the image is caught by check 1 anyway (it
    would not be in requirements.lock.txt, but it WOULD match the lock, which
    is the honest verdict -- version drift is what this guard is for).
    """
    data = tomllib.loads(path.read_text())
    return {normalise(p["name"]): p["version"] for p in data.get("package", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="image reference to inspect, e.g. ghcr.io/x/y:sha-abc1234")
    parser.add_argument("--lock", default="uv.lock", type=Path, help="path to uv.lock")
    args = parser.parse_args()

    if not args.lock.is_file():
        sys.exit(f"error: {args.lock} not found")

    lock = parse_lock(args.lock)
    report = json.loads(docker_capture(args.image, ["python", "-c", _INSPECT]))
    installed = {normalise(n): v for n, v in report["installed"].items()}
    raw_expected = report["expected"]
    expected = None if raw_expected is None else {normalise(n): e for n, e in raw_expected.items()}

    result = compare(lock=lock, installed=installed, expected=expected)

    print(f"image:     {args.image}")
    print(f"lockfile:  {args.lock} ({len(lock)} packages pinned)")
    print(f"installed: {len(installed)} distributions")
    print(f"expected:  {len(expected or {})} distributions ({IMAGE_REQUIREMENTS})")
    if result.allowed_present:
        print(f"allowlisted (present, not locked): {', '.join(result.allowed_present)}")
    if result.not_applicable:
        print(f"not installed, marker does not apply here: {', '.join(result.not_applicable)}")

    if result.problems:
        print(f"\nMISMATCHES: {len(result.problems)}")
        for problem in result.problems:
            print(f"  - {problem}")
        print(
            "\nThe image does not install what uv.lock pins. Rebuild from the lockfile "
            "(`uv export --locked`) rather than re-resolving pyproject.toml's ranges."
        )
        return 1

    print("\nMISMATCHES: 0 — the image installs exactly what uv.lock pins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
