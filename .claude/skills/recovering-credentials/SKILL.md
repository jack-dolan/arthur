---
name: recovering-credentials
description: Diagnoses and repairs expired, rejected, or invalid credentials for the rental-automation integrations — DocuSign refresh token, Google OAuth tokens (Gmail x2 + Sheets), and the Seam API key. Use on invalid_grant or 401 errors, DocuSign keep-alive failure alerts, poller "Gmail auth failed" logs, or when re-minting or rotating any integration credential.
---

# Recovering credentials

Match the symptom, then read **only** the matching reference file (each is
self-contained):

| Symptom | Integration | Read |
|---|---|---|
| `invalid_grant` on the token exchange; keep-alive failure alert; every DocuSign call 401s | DocuSign | [reference/docusign.md](reference/docusign.md) |
| `Gmail auth failed` in poller logs; alert emails not sending; Sheets writes 401 | Google (Gmail ×2 / Sheets) | [reference/google.md](reference/google.md) |
| Seam calls 401; access-code create/get failing with auth errors | Seam | [reference/seam.md](reference/seam.md) |

Ground rules for every recovery:

- **Values never go in committed files.** Secrets live in `.env` on the VPS
  (and the dev copy); enter them no-echo with
  `scripts/manual/set_env_secret.sh`. Never paste a secret into a commit,
  the Session Log, or chat output.
- The app reads `.env` at boot — after changing it on the VPS, reload with
  `docker compose up -d --force-recreate app` (app-only; this host's caddy
  also fronts a second tenant — see the `deploying-to-vps` skill's rules).
- Interactive OAuth flows run on the **dev machine** with a port-forward to
  wherever the callback listener runs; the registered redirect URI uses
  port **8765** (8080 is taken on the usual hosts).
- After any recovery, verify end-to-end: boot clean (credential guard
  passes), then exercise one real read call for that integration, then a
  Session Log entry (what was rotated, why — no secret values).
