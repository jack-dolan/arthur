# Google credential recovery (Gmail booking-feed, Gmail alerts, Sheets)

## Contents
- The credential model
- Re-issuing a Gmail refresh token
- Re-issuing the Sheets refresh token
- Dashboard-login OAuth (separate client)
- Old patterns

## The credential model

One Google Cloud **desktop** OAuth client (`GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET` in `.env`) is shared by three long-lived refresh
tokens, one per account/scope:

| Token (.env key) | Account role | Used by |
|---|---|---|
| `GMAIL_BOOKING_FEED_REFRESH_TOKEN` | booking-feed inbox (read) | poller |
| `GMAIL_ALERTS_REFRESH_TOKEN` | alerts account (send) | every outbound email |
| `GOOGLE_SHEETS_REFRESH_TOKEN` | spreadsheet owner | cleaner-schedule writes |

The OAuth consent screen is **published to Production** — tokens do not
expire on a 7-day clock (see Old patterns). A token dying today means it was
revoked, the account password/security changed, or the client secret was
rotated (which invalidates ALL three tokens at once).

## Re-issuing a Gmail refresh token

The helper runs its local callback listener on port 8765; run it where a
browser can reach that port (dev machine directly, or on the VPS through
`ssh -L 8765:localhost:8765`):

```bash
python3 scripts/manual/get_gmail_refresh_tokens.py --account booking-feed
python3 scripts/manual/get_gmail_refresh_tokens.py --account alerts
```

Sign in as the **matching** Gmail account when the browser opens (the two
accounts are distinct; a token minted under the wrong account authenticates
but reads/sends from the wrong mailbox). The script writes `.env` and
verifies the token live (`users.getProfile`).

## Re-issuing the Sheets refresh token

Same OAuth client, Sheets scope, authorized as the spreadsheet-owning
account. Historically minted with a small local `run_local_server(port=8765,
open_browser=False)` flow — if no dedicated script exists, mirror
`get_gmail_refresh_tokens.py` with the spreadsheets scope. Verify by reading
the schedule sheet metadata afterwards.

## After any token change

On the VPS: update `.env` (no-echo via `scripts/manual/set_env_secret.sh` if
pasting), `docker compose up -d --force-recreate app`, confirm the credential
guard passes at boot, and watch one poller cycle / send one test alert.

## Dashboard-login OAuth (separate client)

"Sign in with Google" for the dashboard uses a **different, web-type** OAuth
client: `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`, with the
callback path `/auth/callback` on the public domain registered as a redirect
URI. Rotating ITS secret only affects dashboard login (re-verify by signing
in), not Gmail/Sheets. The allowlist lives in `config.yaml`
`dashboard.allowed_emails` and fails closed when empty.

## Old patterns

<details>
<summary>7-day token expiry (resolved 2026-07-02)</summary>

While the OAuth consent screen was in **Testing** mode, refresh tokens for
test users expired every 7 days — both Gmail tokens died this way once.
Publishing the app to Production (External, unverified is fine) ended it.
If tokens ever start dying weekly again, check the consent screen's
publishing status first.
</details>
