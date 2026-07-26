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

# --- builder: uv + the locked dependency set, into /opt/venv -----------------
FROM python:3.12-slim AS builder

# uv is pinned (an unpinned installer is the same class of problem as unpinned
# dependencies) and installed from PyPI rather than copied from a registry
# image. `COPY --from=ghcr.io/astral-sh/uv:<tag>` reads better and was tried
# first, but it makes every build depend on registry auth: a host with a stale
# credential for that registry has it sent on every request and gets `denied`
# even for a public image, rather than falling back to anonymous. A build that
# can fail on someone else's expired token is not worth the tidier syntax.
# Trade-off accepted knowingly: this pin is not watched by any automated
# updater (they read `FROM` lines), so bump it by hand when uv matters.
RUN pip install --no-cache-dir uv==0.11.8

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
FROM python:3.12-slim

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
