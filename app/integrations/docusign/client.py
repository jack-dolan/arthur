"""DocuSign eSign API client with manual refresh-token exchange.

Risk 1 (from RESEARCH): The docusign-esign SDK's generate_access_token() exchanges
an auth code, not a refresh token. Refreshing requires a direct HTTP POST to the
token endpoint via httpx.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import docusign_esign as ds
import httpx

from app.settings import settings

log = logging.getLogger(__name__)

# Repo-root .env — kept updated where it exists (dev hosts). In the container
# .env does NOT exist (creds arrive as env vars via compose env_file), which is
# why the durable store below is the authoritative persistence.
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

# Restart-durable rotation store (bug hunt F4). /app/data is the compose-
# mounted pdf_data volume, so a token written here survives container
# recreation — unlike the in-memory settings (lost on restart) or /app/.env
# (doesn't exist in the container). The exchange prefers this token; the
# env-var token is the fallback (and wins on invalid_grant, which covers an
# operator re-minting the host .env after a full expiry).
_TOKEN_STORE = Path("/app/data/docusign_refresh_token")


def _read_token_store() -> str | None:
    """Return the durably-stored rotated refresh token, or None."""
    try:
        if _TOKEN_STORE.exists():
            token = _TOKEN_STORE.read_text().strip()
            return token or None
    except OSError as exc:
        log.error("Could not read DocuSign token store %s: %s", _TOKEN_STORE, exc)
    return None


def _write_token_store(new_token: str) -> None:
    """Atomically write the rotated token to the durable store (0600)."""
    try:
        if not _TOKEN_STORE.parent.exists():
            # Dev host without /app/data — .env persistence below covers it.
            return
        tmp = _TOKEN_STORE.parent / (_TOKEN_STORE.name + ".tmp")
        tmp.write_text(new_token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _TOKEN_STORE)
        log.info("Persisted rotated DocuSign refresh token to %s", _TOKEN_STORE)
    except OSError as exc:
        log.error(
            "Could not persist rotated DocuSign refresh token to %s: %s",
            _TOKEN_STORE,
            exc,
        )


def _persist_refresh_token(new_token: str) -> None:
    """Persist a rotated DocuSign refresh token everywhere it matters.

    DocuSign rotates the refresh token on every refresh-token grant and gives the
    new one a fresh 30-day life (R5 / Step 19). Persistence targets, in order:

    1. In-memory settings — the running process uses it immediately.
    2. The durable store on /app/data (F4) — survives container restarts; this
       is what makes a restart >30 days after the last manual mint safe.
    3. The repo-root .env where it exists (dev hosts; also keeps the CLAUDE.md
       migration procedure's "copy .env" step carrying a live token). Missing
       .env (the container case) logs at INFO — the store above has it covered.

    Filesystem errors are logged but never raised — the freshly rotated token
    is still usable in memory for this run.
    """
    settings.docusign_refresh_token = new_token
    _write_token_store(new_token)
    try:
        if not _ENV_PATH.exists():
            log.info(
                "No %s to update (expected in the container; the durable store "
                "holds the rotation)",
                _ENV_PATH,
            )
            return
        lines = _ENV_PATH.read_text().splitlines()
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith("DOCUSIGN_REFRESH_TOKEN="):
                lines[i] = f"DOCUSIGN_REFRESH_TOKEN={new_token}"
                replaced = True
                break
        if not replaced:
            lines.append(f"DOCUSIGN_REFRESH_TOKEN={new_token}")
        tmp = _ENV_PATH.parent / (_ENV_PATH.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, _ENV_PATH)
        log.info("Persisted rotated DocuSign refresh token to %s", _ENV_PATH)
    except OSError as exc:
        log.error(
            "Could not persist rotated DocuSign refresh token to %s: %s",
            _ENV_PATH,
            exc,
        )

def _docusign_hosts() -> tuple[str, str]:
    """Return (oauth_host, api_host) based on DOCUSIGN_SANDBOX setting.

    Sandbox (account-d.docusign.com / demo.docusign.net) uses entirely
    different credentials from production — see docs/credential-setup.md.
    Default is sandbox (True) so misconfiguration never hits production.
    """
    if settings.docusign_sandbox:
        return "account-d.docusign.com", "https://demo.docusign.net/restapi"
    # DocuSign is multi-region: an account lives on a specific instance
    # (na2/na3/na4/eu/au/ca/...) and REST calls must target that instance's base
    # URI (from OAuth userinfo), NOT a global host. DOCUSIGN_API_BASE_URI carries
    # the account's own base (e.g. https://na4.docusign.net/restapi). The www
    # fallback is correct only for accounts that actually live on that instance.
    api_host = settings.docusign_api_base_uri or "https://www.docusign.net/restapi"
    return "account.docusign.com", api_host


def log_docusign_target() -> tuple[str, str]:
    """Announce which DocuSign environment and hosts this process will call.

    DOCUSIGN_SANDBOX is the highest-stakes setting in the app: false routes every
    envelope at the production tier, which costs real money. Logging the resolved
    hosts at startup makes the sandbox/production cutover confirmable from the
    container logs, rather than inferred from a .env file nobody can see.

    Returns the (oauth_host, api_host) it reported, so callers/tests can assert.
    """
    oauth_host, api_host = _docusign_hosts()
    environment = "SANDBOX (demo tier)" if settings.docusign_sandbox else "PRODUCTION (real envelopes, real money)"
    log.info(
        "DocuSign target: %s | oauth=%s | api=%s",
        environment,
        oauth_host,
        api_host,
    )
    return oauth_host, api_host


def _refresh_access_token() -> str:
    """Exchange the stored refresh token for a new DocuSign access token.

    Uses a direct httpx POST — the docusign-esign SDK does not support
    refresh token exchange natively (Risk 1 from RESEARCH).

    Token selection (F4): the durable store (most recent rotation, survives
    restarts) is tried first, then the env-var token from settings. A stale
    candidate rejected with invalid_grant falls through to the next, so an
    operator re-minting the host .env token always wins over a dead store.
    """
    oauth_host, _ = _docusign_hosts()

    candidates: list[tuple[str, str]] = []
    stored = _read_token_store()
    if stored:
        candidates.append(("store", stored))
    if settings.docusign_refresh_token and settings.docusign_refresh_token not in (
        t for _, t in candidates
    ):
        candidates.append(("settings", settings.docusign_refresh_token))
    if not candidates:
        raise RuntimeError(
            "No DocuSign refresh token configured (DOCUSIGN_REFRESH_TOKEN empty "
            f"and no token store at {_TOKEN_STORE})"
        )

    for i, (source, token) in enumerate(candidates):
        response = httpx.post(
            f"https://{oauth_host}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
            },
            auth=(settings.docusign_client_id, settings.docusign_client_secret),
            timeout=10.0,
        )
        is_last = i == len(candidates) - 1
        if (
            not is_last
            and response.status_code == 400
            and "invalid_grant" in getattr(response, "text", "")
        ):
            log.warning(
                "DocuSign refresh token from %s rejected (invalid_grant); "
                "trying the next candidate",
                source,
            )
            continue
        response.raise_for_status()
        data = response.json()

        # R5 (Step 19): DocuSign returns a rotated refresh token with a fresh
        # 30-day life on every exchange. Persist it so the token never silently
        # expires (and so the durable store always holds the newest rotation).
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != token:
            _persist_refresh_token(new_refresh)

        return data["access_token"]

    raise AssertionError("unreachable: candidate loop always returns or raises")


def get_envelope_api() -> tuple[ds.EnvelopesApi, str]:
    """Return an authenticated (EnvelopesApi, account_id) tuple.

    Obtains a fresh access token via refresh-token exchange on every call.
    Tokens are never persisted (T-04-D2 mitigation).
    """
    _, api_host = _docusign_hosts()
    access_token = _refresh_access_token()
    api_client = ds.ApiClient()
    api_client.host = api_host
    api_client.set_default_header("Authorization", f"Bearer {access_token}")
    log.debug("DocuSign EnvelopesApi initialised for account %s", settings.docusign_account_id)
    return ds.EnvelopesApi(api_client), settings.docusign_account_id
