#!/usr/bin/env python3
"""One-time helper: obtain a DocuSign refresh token via the Authorization Code
Grant and write it into .env. Works for BOTH the demo/sandbox and the production
environments — pick one explicitly as the first argument.

Why this exists
---------------
app/integrations/docusign/client.py refreshes access tokens with a plain
``grant_type=refresh_token`` POST (HTTP Basic = client_id:client_secret). That
refresh token has to be minted once via an interactive Authorization Code Grant —
this script does exactly that, against whichever OAuth host matches the target
environment:

    sandbox     -> account-d.docusign.com   (demo)
    production  -> account.docusign.com      (real; go-live must be complete)

Usage
-----
    python3 scripts/manual/get_docusign_refresh_token.py {sandbox|production}

Stdlib only (no httpx), so it runs on a bare host. It reads DOCUSIGN_CLIENT_ID /
DOCUSIGN_CLIENT_SECRET from .env — after go-live the SAME integration key is
promoted, so those values are unchanged; minting successfully here also PROVES
they work in the chosen environment.

Headless flow
-------------
Run on the host that holds .env. It listens on 127.0.0.1:8765. The DocuSign app's
registered Redirect URI must be http://localhost:8765/callback. Bridge from your
laptop in the SAME command:

    ssh -L 8765:localhost:8765 <host> \\
        'cd <repo-dir-on-that-host> && \\
         python3 scripts/manual/get_docusign_refresh_token.py production'

Then open the printed URL in your laptop browser, sign in with the DocuSign
account for that environment, and approve. DOCUSIGN_REFRESH_TOKEN is written to
.env (never printed). The visible accounts (account id / name / base_uri) ARE
printed — those are identifiers, not secrets — so you can capture the production
API Account ID for DOCUSIGN_ACCOUNT_ID.

Safe to re-run: overwrites only the DOCUSIGN_REFRESH_TOKEN line.
"""
from __future__ import annotations

import base64
import http.server
import json
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOSTS = {
    "sandbox": "account-d.docusign.com",
    "production": "account.docusign.com",
}
SCOPE = "signature"  # eSignature send/status; NOT impersonation (that's JWT-only)

# 8080 is already in use on this host, so use 8765. This MUST equal the port in
# the Redirect URI registered on the DocuSign app (Apps & Keys -> your app ->
# Redirect URIs): http://localhost:8765/callback
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_captured: dict[str, str] = {}


def read_env(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip()
    return vals


def write_env_value(path: Path, key: str, value: str) -> None:
    """Replace (or append) a single KEY=value line, preserving everything else."""
    lines = path.read_text().splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


def _post_form(url: str, data: dict[str, str], basic: tuple[str, str]) -> dict:
    body = urllib.parse.urlencode(data).encode()
    token = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, bearer: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _captured["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>DocuSign authorization received.</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
            )
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No authorization code in callback.")

    def log_message(self, *args):  # silence default request logging
        pass


def main() -> None:
    # Over a non-interactive `ssh host 'python3 ...'` there is no TTY, so Python
    # block-buffers stdout — the auth URL would sit unflushed while we wait on the
    # callback, looking like a hang. Force line buffering so prompts appear live.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if len(sys.argv) != 2 or sys.argv[1] not in HOSTS:
        sys.exit("usage: get_docusign_refresh_token.py {sandbox|production}")
    env_name = sys.argv[1]
    oauth_host = HOSTS[env_name]

    env = read_env(ENV_PATH)
    cid = env.get("DOCUSIGN_CLIENT_ID", "")
    secret = env.get("DOCUSIGN_CLIENT_SECRET", "")
    if not cid or not secret:
        sys.exit("ERROR: DOCUSIGN_CLIENT_ID / DOCUSIGN_CLIENT_SECRET missing in .env")

    auth_url = (
        f"https://{oauth_host}/oauth/auth?response_type=code"
        f"&scope={SCOPE}&client_id={cid}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )

    print(f"\n=== Target: {env_name.upper()} (oauth host {oauth_host}) ===")
    print(
        f"\nStep 1 — make 127.0.0.1:{REDIRECT_PORT} reachable from your browser machine\n"
        f"    (already done if you launched this via `ssh -L {REDIRECT_PORT}:localhost:{REDIRECT_PORT} <host> ...`)"
    )
    print(f"\nStep 2 — open this URL and sign in with your {env_name.upper()} DocuSign account:\n")
    print("    " + auth_url + "\n")
    print(f"Waiting for the OAuth callback on {REDIRECT_URI} ...")

    with socketserver.TCPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler) as httpd:
        while "code" not in _captured:
            httpd.handle_request()

    print("Authorization code received. Exchanging for tokens...")
    try:
        data = _post_form(
            f"https://{oauth_host}/oauth/token",
            {"grant_type": "authorization_code", "code": _captured["code"]},
            (cid, secret),
        )
    except urllib.error.HTTPError as exc:
        sys.exit(f"ERROR: token exchange failed ({exc.code}): {exc.read().decode()[:300]}")

    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token")
    if not refresh_token:
        sys.exit(f"ERROR: no refresh_token in response: {data}")

    write_env_value(ENV_PATH, "DOCUSIGN_REFRESH_TOKEN", refresh_token)
    print(
        f"\nDOCUSIGN_REFRESH_TOKEN written to .env (length={len(refresh_token)}, not shown)."
    )

    # Surface the visible accounts so you can capture DOCUSIGN_ACCOUNT_ID.
    try:
        ui = _get_json(f"https://{oauth_host}/oauth/userinfo", access_token)
    except urllib.error.HTTPError as exc:
        print(f"WARNING: userinfo check returned {exc.code} — token written but unverified.")
        return

    accounts = ui.get("accounts", [])
    print(f"\nuserinfo OK — signed in as {ui.get('email','?')}; {len(accounts)} account(s) visible:")
    for a in accounts:
        base = a.get("base_uri", "")
        tag = "DEMO" if base.startswith("https://demo") else "PROD"
        default = " (default)" if a.get("is_default") else ""
        print(f"    [{tag}] account_id={a.get('account_id')}  name={a.get('account_name')!r}{default}")
        print(f"          base_uri={base}")
    if env_name == "production":
        prod = [a for a in accounts if not a.get("base_uri", "").startswith("https://demo")]
        if prod:
            print(
                "\n-> Set DOCUSIGN_ACCOUNT_ID in .env to the account_id of the PROD account "
                "you want to send from (usually the default)."
            )
        else:
            print("\nWARNING: no production account visible — did you sign in to the right account?")
    print("\nPASS: token exchange + access-token validation succeeded.")


if __name__ == "__main__":
    main()
