from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://rental_automation:change_me@localhost:5432/rental_automation"

    # Google OAuth (shared client for Gmail + Sheets — desktop/installed flow)
    google_client_id: str = ""
    google_client_secret: str = ""
    gmail_booking_feed_refresh_token: str = ""
    gmail_alerts_refresh_token: str = ""
    google_sheets_refresh_token: str = ""

    # Google OIDC "Sign in with Google" for the dashboard (a separate WEB OAuth
    # client — redirect-based — not the desktop client above).
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    # DocuSign
    docusign_sandbox: bool = True  # True = Developer sandbox; False = production (go-live only)
    # Production REST API base URI for THIS account's region, from OAuth userinfo
    # (e.g. https://na4.docusign.net/restapi). DocuSign is multi-region; calls must
    # go to the account's own base, not a global host. Empty falls back to the
    # legacy www host (only correct for accounts on that instance). Ignored in sandbox.
    docusign_api_base_uri: str = ""
    docusign_account_id: str = ""
    docusign_client_id: str = ""
    docusign_client_secret: str = ""
    docusign_refresh_token: str = ""
    docusign_hmac_key: str = ""

    # Seam
    seam_api_key: str = ""

    # Anthropic (Claude API). Used by ONE job: the weekly LLM inbox reviewer,
    # which asks Claude whether any platform-domain OTHER dead-letter is a
    # booking the keyword classifier missed. Alert-only — it never writes to
    # the classifier, tasks or dead letters. Empty = the reviewer logs and
    # skips; the daily credential sentinel treats empty as a dead credential.
    anthropic_api_key: str = ""

    # App
    # Preview/demo deployment switch (PREVIEW_MODE=1). Off in production, and
    # off by default so it can only ever be opted into. When on, the app
    # registers no scheduler jobs, returns inert stubs from every outbound
    # client factory, refuses to start if any credential above looks real, and
    # seeds fake demo bookings. See app/preview.py.
    preview_mode: bool = False
    # S105: this placeholder is a deliberate tripwire, not a hardcoded secret.
    # main.py's lifespan refuses to start the server while it is still in force.
    secret_key: str = "insecure-default-change-in-production"  # noqa: S105
    domain: str = "localhost"
    # Level for the "app" logger namespace. INFO keeps the operational record
    # (poller ingests, task dispatch, DocuSign webhook events) visible in
    # `docker compose logs app`; the service runs unattended, so this is the
    # only place that record exists.
    log_level: str = "INFO"

    # External heartbeat monitoring (sustainability audit 2026-07-23, item 1).
    # Unique healthchecks.io ping URLs; empty = not configured = pings are
    # silently skipped. The external monitor alerts the owner when pings STOP,
    # through its own channels — independent of this app's Gmail tokens.
    healthchecks_ping_url_poller: str = ""
    healthchecks_ping_url_sentinel: str = ""
    healthchecks_ping_url_keepalive: str = ""


settings = Settings()
