# Credential Setup Guide

This document describes every external account and credential required to run
the rental automation system, what each one does, and how to obtain it.

---

## Overview: What accounts you need

| Account | Type | Purpose |
|---|---|---|
| Google Cloud project | New | OAuth app that authorizes all three Google tokens |
| Booking feed Gmail | New Gmail account | System-only inbox that receives forwarded Airbnb/VRBO emails |
| Alerts Gmail | New Gmail account | System-only account that *sends* all outbound emails (alerts, HOA) |
| Personal Google account | Existing | Used only for Google Sheets access (you already have access to the spreadsheet) |
| DocuSign Developer account | New (free) | Sandbox account for testing envelope sends without spending real envelopes |
| DocuSign production account | Existing | Used only at go-live after sandbox testing is complete |
| Seam | Existing | Sandbox workspace for testing, production workspace for go-live |

---

## Google: Three tokens, one OAuth app

The system uses **one Google OAuth 2.0 client** (one `GOOGLE_CLIENT_ID` +
`GOOGLE_CLIENT_SECRET`) and **three separate refresh tokens**, one per role.
Each refresh token is obtained by authorizing the same OAuth app with a
different Google account.

### Step 1: Create the OAuth app

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. "Rental Automation")
3. Enable two APIs: **Gmail API** and **Google Sheets API**
4. Go to Credentials → Create Credentials → OAuth 2.0 Client ID
5. Application type: **Desktop app**
6. Download the JSON file (`credentials.json`)
7. Copy the values into `.env`:
   ```
   GOOGLE_CLIENT_ID=<your client_id>
   GOOGLE_CLIENT_SECRET=<your client_secret>
   ```

You must add authorized redirect URIs for `http://localhost` when running the
token-fetch script below.

### Step 2: Create the two Gmail accounts

**Booking feed Gmail** (`GMAIL_BOOKING_FEED_REFRESH_TOKEN`):
- Create a new Gmail account, e.g. `your-rental-bookings@example.com`
- This inbox is polled every 5 minutes by the system for new booking emails
- It only reads mail — it never sends
- After creating the account, set up auto-forwarding in your **personal**
  Airbnb-linked email and your **personal** VRBO-linked email to forward
  booking confirmation and cancellation emails to this address

**Alerts Gmail** (`GMAIL_ALERTS_REFRESH_TOKEN`):
- Create a new Gmail account, e.g. `your-rental-alerts@example.com`
- This account *sends* all outbound mail: new-booking alerts, reminder alerts
  (7-day, 4-day thresholds), and HOA notification emails with attached PDFs
- The system sends **from** this account **to** the address in
  `config.yaml → email.alerts`
- If you set `email.alerts` in `config.yaml` to your personal email address,
  alerts land in your normal inbox. If you set it to the alerts Gmail address
  itself, you check that Gmail account for notifications

### Step 3: Forwarding rules in Airbnb and VRBO

In your **existing personal accounts** where you receive booking emails:

**Airbnb:** Settings → Notifications → Email → add a filter/forward rule that
forwards emails from `automated@airbnb.com` (and `noreply@airbnb.com`) to
the booking feed Gmail address.

**VRBO:** Settings → Account → Email notifications → add forwarding or set the
notification email to the booking feed Gmail address directly.

Exact steps vary by platform UI. The goal: every booking confirmation and
cancellation email that Airbnb/VRBO sends to you also lands in the booking
feed Gmail inbox.

### Step 4: Obtain the three refresh tokens

Use this script (save it anywhere outside the repo, run it three times):

```python
from google_auth_oauthlib.flow import InstalledAppFlow
import json

# Adjust scopes per token:
# Booking feed: gmail.readonly only (add gmail.send if needed)
# Alerts: gmail.send only
# Sheets: spreadsheets only
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # "https://www.googleapis.com/auth/spreadsheets",  # use this for the Sheets token
]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", scopes=SCOPES)
creds = flow.run_local_server(port=0)
print("Refresh token:", creds.refresh_token)
```

- Run once signed in to **booking feed Gmail** → `GMAIL_BOOKING_FEED_REFRESH_TOKEN`
- Run once signed in to **alerts Gmail** → `GMAIL_ALERTS_REFRESH_TOKEN`
- Run once with `SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]`
  signed in to **your personal Google account** → `GOOGLE_SHEETS_REFRESH_TOKEN`

### Step 5: Google Sheets setup

The system writes to the spreadsheet identified by
`config.yaml → properties[n].cleaner_schedule.spreadsheet_id`.

- Use the spreadsheet you already manage
- The account whose refresh token you use for Sheets must have **Editor**
  access to the spreadsheet
- If you used your personal account's token, no sharing change is needed
- If you want to use the alerts Gmail account instead, share the spreadsheet
  with that Gmail address and use its refresh token

`config.yaml → properties[n].cleaner_schedule.sheet_name` must match the
exact tab name (the sheet tab title, not the spreadsheet title).

The spreadsheet must have a sentinel row marking the bottom of the schedule.
Its text is configured per property as
`config.yaml → properties[n].cleaner_schedule.sentinel_pattern` (default
`--- end ---`), matched case-insensitively against **every cell** in a row —
not just column A, because a real sentinel is often a merged cell that starts
in a later column. New cleaner rows are always inserted above this sentinel
row, in chronological order.

---

## DocuSign: Sandbox first, production later

**Never configure production DocuSign credentials until end-to-end sandbox
testing is complete.** DocuSign envelopes in production cost real money and
count against your plan's envelope limit.

### The sandbox vs production distinction is not just URLs

DocuSign sandbox (`account-d.docusign.com` / `demo.docusign.net`) and
production (`account.docusign.com` / `www.docusign.net`) are entirely separate
systems with:

- **Different OAuth credentials** — your sandbox `client_id`, `client_secret`,
  `account_id`, and `refresh_token` are from your DocuSign Developer account.
  They are completely different values from your production account. You cannot
  mix them.
- **Different templates** — the guest form template must be created separately
  in your sandbox account. The `docusign_template_id` in `config.yaml` is the
  sandbox template ID during testing, and must be swapped for the production
  template ID at go-live.
- **Different HMAC keys** — DocuSign Connect (the webhook) is configured per-account.
  Your sandbox account needs its own Connect configuration, its own webhook URL,
  and its own HMAC secret. The `DOCUSIGN_HMAC_KEY` value is different for
  sandbox and production.
- **Different email behavior** — sandbox envelopes are sent to real email
  addresses but the signing UI is clearly marked as a test environment.
  Sandbox DocuSign does create accounts for recipients automatically.
- **Webhook delivery** — DocuSign sandbox sends webhooks to your configured
  endpoint just like production. During local development you need a tunnel
  (e.g. ngrok) to receive these. The E2E test does *not* require webhooks to
  fire synchronously — it asserts `HOA_EMAIL == WAITING` precisely because
  the webhook fires out-of-band.

### Step 1: Create a DocuSign Developer account

1. Go to [developers.docusign.com](https://developers.docusign.com) → Sign Up (free)
2. This creates a sandbox account separate from any production account you have
3. Note your **Account ID** (visible in the account settings) →
   `DOCUSIGN_ACCOUNT_ID`

### Step 2: Create the OAuth app in DocuSign sandbox

1. In the Developer account: Settings → Integrations → Apps and Keys → Add App
2. Name it (e.g. "Rental Automation Test")
3. Grant it the `signature` scope. (`impersonation` is JWT-grant only — this
   client uses the Authorization Code Grant with a refresh-token exchange, so
   it is not needed.)
4. Set the Redirect URI to `http://localhost` (for the refresh-token script)
5. Copy **Integration Key** → `DOCUSIGN_CLIENT_ID`
6. Generate a **Secret Key** → `DOCUSIGN_CLIENT_SECRET`

### Step 3: Obtain the sandbox refresh token

DocuSign uses Authorization Code Grant. The refresh token is obtained once and
stored; the system exchanges it for a short-lived access token on every call.

Use the DocuSign OAuth playground at
`https://account-d.docusign.com/oauth/auth` with your sandbox credentials to
complete the flow and retrieve the refresh token. There is no first-party CLI
for this; you must either:
- Use the DocuSign OAuth Playground (in the developer portal under Tools)
- Or write a short script using `httpx` to complete the PKCE / auth code flow

Once obtained: `DOCUSIGN_REFRESH_TOKEN`

### Step 4: Create the guest form template in sandbox

1. In your DocuSign Developer account: Templates → New Template
2. Build or upload your guest form (the same form you intend to use in
   production)
3. Define the signer role name — it must exactly match the value you set in
   `config.yaml → properties[n].docusign_signer_role` (default: `"signer"`)
4. Note the Template ID → `config.yaml → properties[n].docusign_template_id`

### Step 5: Configure DocuSign Connect (webhooks) in sandbox

1. In the DocuSign Developer account: Settings → Connect → Add Configuration
2. Set the endpoint URL to your server's `/webhooks/docusign` route
   (for local testing: your ngrok/tunnel URL + `/webhooks/docusign`)
3. Select envelope events to send: `Completed`, `Voided`, `Declined`
4. Copy the HMAC key from the Connect configuration → `DOCUSIGN_HMAC_KEY`

### Step 6: Confirm `DOCUSIGN_SANDBOX=true` is set in `.env`

The code (`app/integrations/docusign/client.py`) reads `DOCUSIGN_SANDBOX`
from `.env` and automatically routes to sandbox or production hosts at runtime.
The default when the variable is absent is sandbox (safe). As long as
`DOCUSIGN_SANDBOX=true`, all calls go to `account-d.docusign.com` /
`demo.docusign.net`. See "DocuSign sandbox vs production: how the code
switches" below for the full host table.

### At go-live: swap all five DocuSign credentials

When testing is complete and you're ready to go live, replace all five values
in `.env` with production account values:
- `DOCUSIGN_ACCOUNT_ID` → production account ID
- `DOCUSIGN_CLIENT_ID` → production app integration key
- `DOCUSIGN_CLIENT_SECRET` → production app secret
- `DOCUSIGN_REFRESH_TOKEN` → production refresh token
- `DOCUSIGN_HMAC_KEY` → production Connect HMAC key

And update `config.yaml` with:
- `docusign_template_id` → production template ID

---

## Seam + Schlage

Seam has separate **Sandbox** and **Production** workspaces. Never configure
a production Seam key until you're ready to go live with the real lock.

### Sandbox testing (no physical lock required)

1. Log in to [app.seam.co](https://app.seam.co)
2. Select the **Sandbox** workspace (created automatically)
3. In sandbox you can add virtual test devices that simulate a Schlage lock
4. Go to Devices → Add device → pick a Schlage virtual device
5. Note the **Device ID** → `config.yaml → properties[n].seam_device_id`
6. Go to Settings → API Keys → copy the sandbox API key → `SEAM_API_KEY`

Sandbox access codes are simulated — no real lock is involved. The Seam SDK
calls succeed and return results as if the lock responded.

### Connecting your real Schlage lock (production only)

1. In Seam, switch to the **Production** workspace
2. Devices → Connect a device → Schlage
3. Follow the Schlage Connect OAuth flow (you'll need your Schlage Home
   account credentials)
4. After connection, the lock appears as a device with a real Device ID
5. Copy the production workspace API key → `SEAM_API_KEY` (replaces sandbox key)

At go-live you swap both: the API key (sandbox → production) and the device ID
(virtual device → real lock device ID in config.yaml).

---

## The `.env` file: all required fields

```dotenv
# Database
DATABASE_URL=postgresql+asyncpg://rental_automation:<password>@localhost:5432/rental_automation

# Google OAuth (shared client for all three tokens below)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Gmail: booking feed inbox (read-only; receives forwarded Airbnb/VRBO emails)
GMAIL_BOOKING_FEED_REFRESH_TOKEN=

# Gmail: alerts sender (sends all outbound notifications and HOA emails)
GMAIL_ALERTS_REFRESH_TOKEN=

# Google Sheets: cleaner schedule spreadsheet (Editor access required)
GOOGLE_SHEETS_REFRESH_TOKEN=

# DocuSign (use sandbox credentials until go-live)
DOCUSIGN_ACCOUNT_ID=
DOCUSIGN_CLIENT_ID=
DOCUSIGN_CLIENT_SECRET=
DOCUSIGN_REFRESH_TOKEN=
DOCUSIGN_HMAC_KEY=

# Seam (use sandbox workspace API key until go-live)
SEAM_API_KEY=

# Dashboard login — a SEPARATE Google *Web application* OAuth client, distinct
# from the desktop client above. Redirect URIs: https://<domain>/auth/callback
# and http://localhost:8000/auth/callback. The list of accounts allowed to sign
# in lives in config.yaml (dashboard.allowed_emails), not here.
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=

# App
# SECRET_KEY signs the dashboard session cookie; rotating it just logs everyone out.
SECRET_KEY=<generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
DOMAIN=localhost
```

`.env.template` is the authoritative, always-current list — this block covers
the credentials this guide walks you through. The template additionally carries
the deploy-target, external-heartbeat and off-site-backup settings, all optional
for a local run.

> **Removed:** `DASHBOARD_PASSWORD`. The dashboard used a single shared HTTP
> Basic password until 2026-07-05; it is now per-user Google login with an
> allowlist, and the startup guard requires the two `GOOGLE_OAUTH_*` values
> instead.

---

## DocuSign sandbox vs production: how the code switches

`app/integrations/docusign/client.py` reads `settings.docusign_sandbox` (set
via `DOCUSIGN_SANDBOX` in `.env`, default `true`) and selects the correct
hosts at runtime:

| `DOCUSIGN_SANDBOX` | OAuth host | API host |
|---|---|---|
| `true` (default) | `account-d.docusign.com` | `demo.docusign.net/restapi` |
| `false` | `account.docusign.com` | `DOCUSIGN_API_BASE_URI`, falling back to `www.docusign.net/restapi` |

**Production is multi-region.** A production account lives on a specific
DocuSign instance, and the global `www.docusign.net` host is correct only for
`www`-instance accounts — every REST call from any other account 401s or
misroutes against it. Read the account's own `base_uri` from the OAuth
`userinfo` response and set it as `DOCUSIGN_API_BASE_URI` (e.g.
`https://na4.docusign.net/restapi`). Leave it blank for sandbox. The app logs
the resolved environment and hosts at every boot — grep the startup log for
`DocuSign target` to confirm which tier and instance are live.

**The default is sandbox.** A server that starts with no `DOCUSIGN_SANDBOX`
entry in `.env` will always call sandbox endpoints. You must explicitly set
`DOCUSIGN_SANDBOX=false` to reach production, which means you cannot
accidentally send real envelopes by forgetting to configure the flag.

Add this to your `.env`:
```dotenv
DOCUSIGN_SANDBOX=true    # change to false only at go-live
```

**Important:** the credentials in `.env` (`DOCUSIGN_ACCOUNT_ID`, `DOCUSIGN_CLIENT_ID`,
`DOCUSIGN_CLIENT_SECRET`, `DOCUSIGN_REFRESH_TOKEN`, `DOCUSIGN_HMAC_KEY`) must
match the environment you selected. Sandbox credentials only work against
sandbox hosts, and production credentials only work against production hosts.
You must swap all five values when switching environments.

---

## The `config.yaml` file: property-specific values

`config.example.yaml` is the committed template — copy it to `config.yaml` and
fill it in. The shape:

```yaml
owners:
  primary_name: "Your Name"          # first name; used in alert messages
  cohost_name: "Co-owner Name"       # used in alert messages; omit if solo
  primary_full_name: "Your Full Name"  # HOA email sign-off + From display name

email:
  booking_feed: "your-rental-bookings@example.com"   # the booking feed Gmail address
  alerts: "destination@example.com"                # where alert emails are sent TO

dashboard:
  # Google accounts allowed to sign in. Fail-closed: an empty list locks
  # everyone out rather than letting everyone in.
  allowed_emails:
    - "owner-a@example.com"
    - "owner-b@example.com"

properties:
  - id: "property_1"
    hoa:
      enabled: true
      email: "your-hoa@example.com"
      open_days: [1, 2, 3, 4, 5, 6]   # 1=Mon 6=Sat; closed Sunday
      email_window_days_min: 2
      email_window_days_max: 7
    cleaner_schedule:
      type: "google_sheets"
      spreadsheet_id: "<ID from the spreadsheet URL>"
      sheet_name: "<exact tab name>"
      sentinel_pattern: "--- end ---"   # match your real sheet's bottom row
    seam_device_id: "<from Seam dashboard>"
    docusign_template_id: "<sandbox template ID during testing>"
    docusign_signer_role: "signer"    # must exactly match the role name in the template
```

> **Removed fields:** `listing_name_pattern`, `airbnb_listing_id` and
> `vrbo_listing_id` were dropped as dead config when scraping was descoped
> (ADR-0004). A `config.yaml` that still sets them is harmless but they are
> read by nothing.
