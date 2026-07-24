# DocuSign credential recovery

## Contents
- How the token normally stays alive (know before "fixing")
- Re-minting the refresh token
- Full credential inventory
- Environment/host rules
- Old patterns (historical quirks)

## How the token normally stays alive

The refresh token lives 30 days and is **rotated on every exchange**. Two
mechanisms keep it fresh without human action:

1. Every real DocuSign call and a **weekly keep-alive job**
   (`refresh_docusign_token`) perform an exchange, resetting the clock.
2. Each rotation is persisted to the durable store
   **`/app/data/docusign_refresh_token`** (on the `pdf_data` volume, so it
   survives container restarts). The exchange prefers the stored token and
   falls back to the `.env` token on `invalid_grant` — so an operator
   re-minting `.env` always wins over a stale store.

**Therefore:** a single keep-alive failure alert may be transient. Re-mint
only when the failure repeats, both token sources are dead
(`invalid_grant` on every call), the `pdf_data` volume was lost, or the
stack was down >30 days.

## Re-minting the refresh token

From the dev machine (the callback listener runs on the VPS; forward its
port). Resolve the SSH target from `.env` (`DEPLOY_VPS_SSH`,
`DEPLOY_VPS_REPO_DIR`):

```bash
ssh -t -L 8765:localhost:8765 "$VPS" \
  "cd $DIR && python3 scripts/manual/get_docusign_refresh_token.py production"
```

- The redirect URI `http://localhost:8765/callback` must be registered on
  the **production** integration key's Apps & Keys page.
- The script writes the token to `.env` itself and never echoes it.
- Then reload: `docker compose up -d --force-recreate app` and confirm the
  boot banner (`DocuSign target: PRODUCTION ... api=<regional base>`); if a
  stale durable store might hold a dead token, remove it first:
  `docker compose exec app rm -f /app/data/docusign_refresh_token`.
- Verify with a read call (envelope get via `get_envelope_api` — see the
  `triaging-stuck-tasks` skill's read-only snippet).

## Full credential inventory

`.env`: `DOCUSIGN_ACCOUNT_ID`, `DOCUSIGN_CLIENT_ID`, `DOCUSIGN_CLIENT_SECRET`,
`DOCUSIGN_REFRESH_TOKEN`, `DOCUSIGN_HMAC_KEY` (Connect webhook signing),
`DOCUSIGN_SANDBOX` (false in production), `DOCUSIGN_API_BASE_URI` (the
account's **regional** REST base from OAuth userinfo — DocuSign is
multi-region and a wrong host 401s/misroutes every call).
`config.yaml`: `docusign_template_id`, `docusign_signer_role`.

## Environment/host rules

- `DOCUSIGN_SANDBOX` switches hosts only; sandbox and production credential
  sets are entirely separate and cannot coexist in one `.env`.
- The boot banner line is the definitive check of which tier is live.
- Changing the Connect webhook config or HMAC key requires updating
  `DOCUSIGN_HMAC_KEY` in lockstep, or every webhook is rejected (400).

## Old patterns (historical quirks, kept for context)

<details>
<summary>Promotion and demo-tier quirks (resolved 2026-07)</summary>

- Promotion to production does NOT copy redirect URIs or client secrets —
  both had to be re-created on the production key; the promoted key GUID
  stays the same.
- Go-live verification envelopes are rejected for accounts whose admin
  email is on a generic domain (anti-fraud) — a custom-domain account was
  required.
- Demo tier throttles/batches signing-request email after bursts and
  suppresses self-notification when signer == account owner. Not applicable
  in production.
- Sandbox refresh tokens died repeatedly from 30-day idle expiry before the
  keep-alive + durable store existed.
</details>
