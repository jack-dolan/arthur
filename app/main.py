import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.routers import auth, dashboard, health, webhooks
from app.settings import settings

_LOG_HANDLER_NAME = "app-stdout"


def _resolve_log_level(raw: object) -> int:
    """Map a configured level name to a logging level, defaulting to INFO.

    A malformed LOG_LEVEL must never stop the service booting: an unattended
    rental workflow failing to start over a log setting would be a far worse
    outcome than logging at the wrong verbosity.
    """
    if isinstance(raw, str):
        return logging.getLevelNamesMapping().get(raw.strip().upper(), logging.INFO)
    return logging.INFO


def _configure_logging() -> None:
    """Send app-module logs to stdout at the configured level.

    Nothing configured logging before, so the root logger had no handler and
    Python fell back to `logging.lastResort`, which emits WARNING and above only.
    Every log.info() in the codebase was therefore discarded — the poller's
    ingest lines, the dispatcher's per-task lines and the DocuSign webhook's
    "envelope_id=... status=..." line included. Those are the operational record
    of a service that otherwise runs unattended, so INFO must reach the logs.

    Scoped to the "app" namespace (not root) to leave uvicorn's own handlers
    alone, and idempotent so repeated calls cannot duplicate output.
    """
    level = _resolve_log_level(settings.log_level)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    for existing in app_logger.handlers:
        if getattr(existing, "name", None) == _LOG_HANDLER_NAME:
            existing.setLevel(level)
            return
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _LOG_HANDLER_NAME
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    app_logger.addHandler(handler)
    # Propagation is deliberately left ON. logging.lastResort only fires when no
    # handler is found anywhere in the chain, and this handler satisfies that, so
    # WARNING+ still prints exactly once — while records keep reaching any root
    # handler a test harness (pytest's caplog) or future config installs.

_REQUIRED_CREDENTIALS = [
    ("google_client_id", "GOOGLE_CLIENT_ID"),
    ("google_client_secret", "GOOGLE_CLIENT_SECRET"),
    ("gmail_booking_feed_refresh_token", "GMAIL_BOOKING_FEED_REFRESH_TOKEN"),
    ("gmail_alerts_refresh_token", "GMAIL_ALERTS_REFRESH_TOKEN"),
    ("google_sheets_refresh_token", "GOOGLE_SHEETS_REFRESH_TOKEN"),
    ("docusign_account_id", "DOCUSIGN_ACCOUNT_ID"),
    ("docusign_client_id", "DOCUSIGN_CLIENT_ID"),
    ("docusign_client_secret", "DOCUSIGN_CLIENT_SECRET"),
    ("docusign_refresh_token", "DOCUSIGN_REFRESH_TOKEN"),
    ("docusign_hmac_key", "DOCUSIGN_HMAC_KEY"),
    ("seam_api_key", "SEAM_API_KEY"),
    ("google_oauth_client_id", "GOOGLE_OAUTH_CLIENT_ID"),
    ("google_oauth_client_secret", "GOOGLE_OAUTH_CLIENT_SECRET"),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    if settings.secret_key == "insecure-default-change-in-production":
        raise ValueError(
            "secret_key is set to the default placeholder. "
            "Set SECRET_KEY in .env before starting the server. "
            "(It signs the dashboard session cookie — must be a strong secret.)"
        )
    for field_name, env_name in _REQUIRED_CREDENTIALS:
        if not getattr(settings, field_name):
            raise ValueError(
                f"{field_name} is not set. "
                f"Add {env_name}=<your-value> to .env and restart."
            )
    if "change_me" in settings.database_url:
        raise ValueError(
            "DATABASE_URL still contains the default placeholder 'change_me'. "
            "Set DATABASE_URL in .env before starting the server."
        )
    Path("/app/data/pdfs").mkdir(parents=True, exist_ok=True)

    # State the DocuSign environment on every boot — the go-live cutover
    # (DOCUSIGN_SANDBOX=false) is confirmed from this line.
    from app.integrations.docusign.client import log_docusign_target

    log_docusign_target()

    from app.ingestion.poller import poll_booking_feed
    from app.tasks.scheduled import (
        check_classifier_drift,
        check_daily_reminders,
        check_hoa_window,
        complete_past_bookings,
        refresh_docusign_token,
        requeue_stalled_automations,
        send_monthly_status_email,
        verify_access_codes,
        verify_credentials,
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_booking_feed, "interval", minutes=5, id="poll_booking_feed")
    scheduler.add_job(
        check_daily_reminders,
        "cron",
        hour=8,
        timezone="US/Eastern",
        id="check_daily_reminders",
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.add_job(check_hoa_window, "interval", hours=1, id="check_hoa_window")
    # F17: daily retry of FAILED automations + re-dispatch of orphaned PENDING
    # ones + stalled-tasks owner digest. 08:30 ET, after the reminder job.
    scheduler.add_job(
        requeue_stalled_automations,
        "cron",
        hour=8,
        minute=30,
        timezone="US/Eastern",
        id="requeue_stalled_automations",
        misfire_grace_time=None,
        coalesce=True,
    )
    # F10: daily confirmation that upcoming bookings' door codes actually
    # exist on the lock (Seam programs devices asynchronously).
    scheduler.add_job(
        verify_access_codes,
        "cron",
        hour=9,
        timezone="US/Eastern",
        id="verify_access_codes",
        misfire_grace_time=None,
        coalesce=True,
    )
    # F15: flip ACTIVE bookings whose stay has ended to COMPLETED so the
    # dashboard's active list and every daily scan don't grow forever.
    # Early morning, ahead of the other daily jobs.
    scheduler.add_job(
        complete_past_bookings,
        "cron",
        hour=2,
        timezone="US/Eastern",
        id="complete_past_bookings",
        misfire_grace_time=None,
        coalesce=True,
    )
    # R5 (Step 19): keep the DocuSign refresh token alive during quiet periods.
    # Weekly is a 4x safety margin inside the 30-day expiry; it sends no envelope.
    # Cron, NOT interval (sustainability audit item 2): interval jobs restart
    # their countdown on every container restart, so a run of deploys spaced
    # under 7 days apart would starve the keep-alive indefinitely.
    scheduler.add_job(
        refresh_docusign_token,
        "cron",
        day_of_week="mon",
        hour=3,
        minute=30,
        timezone="US/Eastern",
        id="refresh_docusign_token",
        misfire_grace_time=None,
        coalesce=True,
    )
    # Sustainability audit item 3: daily read-only proof that every integration
    # credential still works; pings the sentinel heartbeat only when all pass.
    scheduler.add_job(
        verify_credentials,
        "cron",
        hour=7,
        timezone="US/Eastern",
        id="verify_credentials",
        misfire_grace_time=None,
        coalesce=True,
    )
    # Sustainability audit item 4: weekly human-review digest of platform-domain
    # emails that fell to OTHER (the silent classifier-drift failure mode).
    scheduler.add_job(
        check_classifier_drift,
        "cron",
        day_of_week="sun",
        hour=9,
        timezone="US/Eastern",
        id="check_classifier_drift",
        misfire_grace_time=None,
        coalesce=True,
    )
    # Sustainability audit item 3: monthly positive-confirmation email — proves
    # the alert send path end-to-end; its absence is itself a signal.
    scheduler.add_job(
        send_monthly_status_email,
        "cron",
        day=1,
        hour=8,
        timezone="US/Eastern",
        id="send_monthly_status_email",
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="Home Rental Automation", lifespan=lifespan)

# Signed session cookie for the Google-login flow. Secure (https-only) except on
# localhost dev; SameSite=lax so the cookie survives the OAuth redirect back
# from Google (a top-level GET navigation).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.domain not in ("localhost", "127.0.0.1"),
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(health.router)
app.include_router(webhooks.router)
