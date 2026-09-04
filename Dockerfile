# Production image. Two stages, for two independent reasons.
#
# 1. WHAT IT INSTALLS. The builder resolves nothing: `uv export --locked`
#    renders uv.lock to a fully pinned, hashed requirements file, and FAILS if
#    the lockfile has drifted from pyproject.toml rather than papering over it.
#    That silent re-resolution is exactly the defect this replaced. The image
#    used to install `-e .`, so it resolved pyproject.toml's version RANGES
#    fresh at build time and 39 of its 83 packages differed from the lockfile
#    -- meaning the test suite, which installs uv.lock, was not testing the
#    versions production ran.
#
#    `--locked`, not `--frozen`: both stop the export re-resolving, but
#    `--frozen` means "use uv.lock as-is, do not check it" and so builds
#    happily from a lockfile that no longer matches pyproject.toml (verified:
#    a build with a dependency added to pyproject.toml and absent from the
#    lockfile SUCCEEDS under --frozen, silently shipping without it).
#    `--locked` asserts the lockfile is current and exits non-zero when it is
#    not, which is the loud failure this build wants.
#
# 2. WHAT IT SHIPS. uv is a 64MB binary and its download cache was another
#    262MB; neither is used at runtime, and once installed into the final
#    image no later layer can remove them. Keeping uv in the builder and
#    copying only the finished virtualenv drops both.

# --- uv, as a pinned stage so an updater can see the pin ---------------------
# Declaring uv as a `FROM` is what makes the version watchable: Dependabot and
# friends parse `FROM` lines and nothing else, so the `pip install uv==` form
# this replaced received no automated updates at all.
#
# The cost is that the build now needs to reach ghcr.io. That is a real
# failure mode rather than a theoretical one -- a host holding a stale
# credential for a registry sends it on every request and gets `denied` even
# for a public image, instead of falling back to anonymous -- so it was
# avoided until both build hosts were verified able to pull anonymously
# (2026-07-28). Builds now happen on GitHub's runners in any case; the VPS
# path was the one that broke.
FROM ghcr.io/astral-sh/uv:0.12.2 AS uv

# --- builder: uv + the locked dependency set, into /opt/venv -----------------
FROM python:3.14-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /src

# Only the two files the export needs, so the (slow) dependency layer is not
# invalidated by an unrelated application change.
COPY pyproject.toml uv.lock ./

# --no-emit-project: the project itself is not installed here; see the final
# stage's PYTHONPATH note for how `import app` resolves.
# The exported file travels into the final image on purpose: it is the image's
# own record of which distributions and versions it is supposed to contain, so
# a drift check can compare the installed set against it without rebuilding.
RUN uv venv /opt/venv \
 && uv export --locked --no-dev --no-emit-project --format requirements-txt \
      > /opt/venv/requirements.lock.txt \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
      --requirement /opt/venv/requirements.lock.txt

# --- final: clean base + the virtualenv, nothing else ------------------------
FROM python:3.14-slim

# Take every Debian security fix available at build time.
#
# The base image tag lags its own security archive. On 2026-08-26 the tag still
# shipped openssl 3.5.6 while the archive already had 3.5.7-1~deb13u2 with the
# fix for CVE-2026-14456, and the build gate blocked the deploy on it -- the
# gate blocks only findings that HAVE a fix, so "wait for the base image to be
# rebuilt" is the one response that leaves red on the board indefinitely.
#
# Blanket `upgrade` rather than naming packages, because naming them means the
# next lagging package is another blocked deploy and another one-line commit.
# This does not make builds less reproducible than they already are: the base is
# the floating `python:3.14-slim` tag rather than a digest, so what lands here
# already depends on the day. The Python side is unaffected -- that comes from
# uv.lock, and the lockfile gate in the build workflow checks it separately.
#
# If a Debian upgrade ever breaks the image, the two smoke tests in the same
# workflow fail before anything is published.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*

# The base image's own pip is removed, and this is a security fix rather than a
# size one.
#
# pip vendors private copies of its dependencies under `pip/_vendor/`. Those
# copies carry their own advisories, and pip 26.2.1 is the first version in
# these images to ship `pip/_vendor/bom.cdx.json` -- a CycloneDX SBOM that
# names every vendored package and version. Trivy reads that SBOM, so the
# vendored set became visible to the scanner for the first time and the build
# gate began failing on msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and setuptools
# 70.3.0 (CVE-2025-47273).
#
# Neither is reachable at runtime. Nothing in docker-entrypoint.sh invokes pip:
# `alembic`, `uvicorn` and `python` all resolve to /opt/venv, which uv builds
# without pip, and the venv does not expose the base interpreter's
# site-packages. The vendored code was dead weight before it was a finding.
#
# NOTE for whoever bumps python:3.12-slim -> anything newer next: setuptools
# 70.3.0 was vendored by pip 25.0.1 in the 3.12 image too. It was never absent
# from this image, only invisible, because that pip shipped no SBOM. Deleting
# pip is what actually removes it.
RUN rm -rf /usr/local/lib/python*/site-packages/pip \
           /usr/local/lib/python*/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

COPY --from=builder /opt/venv /opt/venv

# The venv's bin comes first, so `uvicorn` and `alembic` in docker-entrypoint.sh
# resolve to it and `python` is the venv interpreter.
ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The old `-e .` install put /app on sys.path via a .pth file in site-packages,
# so `import app` worked from any directory. The venv does not install the
# project, and relying instead on the two implicit mechanisms that would
# otherwise cover it -- uvicorn's `--app-dir` defaulting to "." and
# alembic.ini's `prepend_sys_path = .` -- would make the import contingent on
# the CWD and on two unrelated tools' defaults. This states it once,
# explicitly, and restores the old behaviour exactly.
ENV PYTHONPATH=/app

WORKDIR /app

COPY . .

RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
