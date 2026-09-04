"""Preview mode: the app booted with no capacity to touch the outside world.

Preview mode exists so a non-production copy of this service can be run and
demonstrated safely. A second ordinary instance is dangerous by design: it
polls the same shared booking inbox and holds the same DocuSign, Seam and
Google Sheets credentials, so it would double-send envelopes and email and
program a real door lock (see the repo conventions doc, "Portability", and the
single-instance rule).

Preview mode removes the capability rather than the intent:

* no scheduler jobs are registered at all;
* every outbound client factory returns an inert stub instead of a real client,
  at exactly the construction choke points the test guard blocks;
* startup refuses outright if any credential-shaped setting looks real, so a
  copy-pasted production environment cannot boot in preview;
* the database is seeded with obviously-fake demo bookings;
* every page carries a PREVIEW banner.

These tests pin all five, plus the normal-mode behaviour they must not change.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import load_config as real_load_config
from app.db.models import BookingStatus, Platform, TaskState
from app.preview import (
    PLACEHOLDER_SCANNED_SETTINGS,
    PreviewModeError,
    assert_no_real_credentials,
    build_demo_bookings,
    inert_client,
    is_preview_mode,
)
from app.settings import settings

# `real_load_config` is imported at module scope deliberately: it captures the
# real function before the autouse fixture in tests/unit/conftest.py replaces
# `app.config.load_config` with a stub for every unit test.

# A value that looks like a real credential. Invented, not copied from anywhere.
REAL_LOOKING = "1//0gL9xAqR7fKmZ2pWvT4hN8sBdE3yUcJi"

PREVIEW_DATABASE_URL = (
    "postgresql+asyncpg://rental_automation:preview@db:5432/rental_automation_preview"
)
PRODUCTION_DATABASE_URL = (
    "postgresql+asyncpg://rental_automation:hunter2@db:5432/rental_automation"
)


@pytest.fixture
def preview_env():
    """settings patched into a valid preview configuration.

    Every credential-shaped field is emptied and the database URL is marked as
    a preview database, so `assert_no_real_credentials` passes and tests can
    focus on one behaviour at a time.
    """
    overrides = {name: "" for name, _ in PLACEHOLDER_SCANNED_SETTINGS}
    overrides["preview_mode"] = True
    overrides["database_url"] = PREVIEW_DATABASE_URL
    with patch.multiple(settings, **overrides):
        yield settings


# ---------------------------------------------------------------------------
# The switch itself
# ---------------------------------------------------------------------------


def test_preview_mode_is_off_by_default():
    """Nothing about preview mode may be opt-out; it is opt-in only."""
    from app.settings import Settings

    assert Settings(_env_file=None).preview_mode is False


def test_is_preview_mode_follows_the_setting(preview_env):
    assert is_preview_mode() is True


def test_is_preview_mode_false_when_unset():
    with patch.object(settings, "preview_mode", False):
        assert is_preview_mode() is False


# ---------------------------------------------------------------------------
# Fail fast on real-looking credentials
# ---------------------------------------------------------------------------


def test_empty_credentials_are_accepted(preview_env):
    """Empty is the normal preview state: no credential is configured at all."""
    assert_no_real_credentials()  # must not raise


@pytest.mark.parametrize(
    "value",
    [
        "preview-placeholder",
        "placeholder",
        "not-a-real-token",
        "example-client-secret",
        "fake-seam-key",
        "dummy",
        "change_me",
        "xxxxxxxx",
        "00000000-0000-0000-0000-000000000000",
    ],
)
def test_placeholder_shaped_credentials_are_accepted(preview_env, value):
    with patch.object(settings, "seam_api_key", value):
        assert_no_real_credentials()  # must not raise


def test_real_looking_credential_refuses_to_boot(preview_env):
    with patch.object(settings, "gmail_booking_feed_refresh_token", REAL_LOOKING):
        with pytest.raises(PreviewModeError) as exc:
            assert_no_real_credentials()
    # The message must name the environment variable, because the operator
    # fixes this by editing an environment, not a Python field.
    assert "GMAIL_BOOKING_FEED_REFRESH_TOKEN" in str(exc.value)
    assert REAL_LOOKING not in str(exc.value), (
        "the error message must not echo the credential value — preview logs "
        "are the least-protected place this app writes to"
    )


def test_every_real_looking_credential_is_reported_not_just_the_first(preview_env):
    with (
        patch.object(settings, "seam_api_key", REAL_LOOKING),
        patch.object(settings, "docusign_client_secret", REAL_LOOKING),
    ):
        with pytest.raises(PreviewModeError) as exc:
            assert_no_real_credentials()
    message = str(exc.value)
    assert "SEAM_API_KEY" in message
    assert "DOCUSIGN_CLIENT_SECRET" in message


def test_production_looking_database_url_refuses_to_boot(preview_env):
    """A preview must never seed fake bookings into the production database."""
    with patch.object(settings, "database_url", PRODUCTION_DATABASE_URL):
        with pytest.raises(PreviewModeError) as exc:
            assert_no_real_credentials()
    assert "DATABASE_URL" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        PREVIEW_DATABASE_URL,
        "postgresql+asyncpg://u:p@db:5432/rental_automation_staging",
        "postgresql+asyncpg://u:p@localhost:5432/rental_automation_test",
        "postgresql+asyncpg://u:p@db:5432/demo",
    ],
)
def test_marked_throwaway_database_urls_are_accepted(preview_env, url):
    with patch.object(settings, "database_url", url):
        assert_no_real_credentials()  # must not raise


def test_credential_scan_covers_every_credential_main_requires():
    """Drift guard: a credential added to the boot check must also be scanned.

    Without this, a future credential could be added to app/main.py's required
    list and silently escape the preview placeholder check — which is the exact
    hole preview mode's belt-and-braces check exists to close.
    """
    from app.main import _REQUIRED_CREDENTIALS

    scanned = {name for name, _ in PLACEHOLDER_SCANNED_SETTINGS}
    missing = {name for name, _ in _REQUIRED_CREDENTIALS} - scanned
    assert not missing, (
        f"credential settings required at boot but not scanned in preview mode: "
        f"{sorted(missing)}. Add them to PLACEHOLDER_SCANNED_SETTINGS."
    )


def test_credential_scan_covers_every_credential_shaped_setting():
    """Drift guard on the Settings model itself, not just on main.py's list."""
    from app.settings import Settings

    credential_shaped = re.compile(
        r"(token|secret|_key|client_id|password)$|api_key|ping_url"
    )
    scanned = {name for name, _ in PLACEHOLDER_SCANNED_SETTINGS}
    missing = {
        name
        for name in Settings.model_fields
        if credential_shaped.search(name) and name not in scanned
    }
    assert not missing, (
        f"credential-shaped settings not scanned in preview mode: "
        f"{sorted(missing)}. Add them to PLACEHOLDER_SCANNED_SETTINGS."
    )


def test_scanned_setting_env_names_are_the_real_env_names():
    """Each scanned entry must name the env var pydantic actually reads."""
    from app.settings import Settings

    for field_name, env_name in PLACEHOLDER_SCANNED_SETTINGS:
        assert field_name in Settings.model_fields, f"{field_name} is not a setting"
        assert env_name == field_name.upper(), (
            f"{field_name} is read from {field_name.upper()}, not {env_name}"
        )


# ---------------------------------------------------------------------------
# Inert outbound clients
# ---------------------------------------------------------------------------
#
# The five factories below are the same construction choke points the test
# guard blocks (tests/conftest.py::_block_live_external_apis). Hooking preview
# mode in at the identical boundary is deliberate: one place per integration,
# already proven to be the only way through to the network.


def test_inert_client_raises_on_any_use():
    client = inert_client("Seam")
    with pytest.raises(PreviewModeError) as exc:
        client.access_codes.create(device_id="x")
    assert "preview mode" in str(exc.value).lower()
    assert "Seam" in str(exc.value)


def test_inert_client_is_falsy_and_reprs_safely():
    """It must be obvious in a log line and in a debugger what this object is."""
    client = inert_client("Gmail")
    assert "inert" in repr(client).lower()
    assert "Gmail" in repr(client)


def test_gmail_booking_feed_service_is_inert_in_preview(preview_env):
    from app.integrations.gmail import oauth

    with patch.object(oauth, "build") as real_build:
        service = oauth.get_booking_feed_service()
    real_build.assert_not_called()
    with pytest.raises(PreviewModeError):
        service.users().getProfile(userId="me").execute()


def test_gmail_alerts_service_is_inert_in_preview(preview_env):
    from app.integrations.gmail import oauth

    with patch.object(oauth, "build") as real_build:
        service = oauth.get_alerts_service()
    real_build.assert_not_called()
    with pytest.raises(PreviewModeError):
        service.users().messages().send(userId="me", body={}).execute()


def test_sheets_service_is_inert_in_preview(preview_env):
    from app.integrations.sheets import client as sheets_client

    with patch.object(sheets_client, "build") as real_build:
        service = sheets_client.get_sheets_service()
    real_build.assert_not_called()
    with pytest.raises(PreviewModeError):
        service.spreadsheets().get(spreadsheetId="x").execute()


def test_seam_client_is_inert_in_preview(preview_env):
    from app.integrations.seam import client as seam_client

    with patch.object(seam_client, "Seam") as real_seam:
        client = seam_client.get_seam_client()
    real_seam.assert_not_called()
    with pytest.raises(PreviewModeError):
        client.devices.get(device_id="x")


def test_docusign_envelope_api_is_inert_in_preview(preview_env):
    from app.integrations.docusign import client as ds_client

    with patch.object(ds_client, "_refresh_access_token") as refresh:
        envelopes_api, account_id = ds_client.get_envelope_api()
    refresh.assert_not_called(), "preview mode must not exchange a DocuSign token"
    assert account_id == ""
    with pytest.raises(PreviewModeError):
        envelopes_api.create_envelope(account_id, envelope_definition={})


def test_normal_mode_still_builds_real_clients():
    """The whole point of the switch is that nothing changes when it is off.

    The real builders are patched, so this asserts the code path reaches them
    without any network call.
    """
    from app.integrations.gmail import oauth
    from app.integrations.seam import client as seam_client
    from app.integrations.sheets import client as sheets_client

    with patch.object(settings, "preview_mode", False):
        with patch.object(oauth, "build") as gmail_build:
            oauth.get_alerts_service()
        gmail_build.assert_called_once()

        with patch.object(sheets_client, "build") as sheets_build:
            sheets_client.get_sheets_service()
        sheets_build.assert_called_once()

        with patch.object(seam_client, "Seam") as seam_ctor:
            seam_client.get_seam_client()
        seam_ctor.assert_called_once()


def test_normal_mode_still_exchanges_a_docusign_token():
    from app.integrations.docusign import client as ds_client

    with (
        patch.object(settings, "preview_mode", False),
        patch.object(settings, "docusign_account_id", "acct-placeholder"),
        patch.object(ds_client, "_refresh_access_token", return_value="tok") as refresh,
    ):
        envelopes_api, account_id = ds_client.get_envelope_api()

    refresh.assert_called_once()
    assert account_id == "acct-placeholder"
    assert not isinstance(envelopes_api, type(inert_client("DocuSign")))


# ---------------------------------------------------------------------------
# Seeded demo data
# ---------------------------------------------------------------------------


def test_demo_bookings_use_only_placeholder_identities():
    """Seeded data ships in the image and is browsed by anyone with the URL."""
    bookings = build_demo_bookings(today=date(2026, 7, 30))
    assert bookings, "preview mode must seed at least one booking"

    for booking in bookings:
        assert booking.guest_last_name == "Example", (
            f"{booking.external_id}: guest surnames must be the literal "
            f"'Example' placeholder, got {booking.guest_last_name!r}"
        )
        if booking.guest_email:
            assert booking.guest_email.endswith("@example.com")
        if booking.guest_phone:
            assert booking.guest_phone.isdigit(), (
                f"{booking.external_id}: demo phones must be stored the way the "
                "dashboard's manual-entry validator stores them (digits only), "
                f"got {booking.guest_phone!r}"
            )
            assert booking.guest_phone.startswith("555"), (
                f"{booking.external_id}: demo phones must be 555 placeholders, "
                f"got {booking.guest_phone!r}"
            )
        assert re.fullmatch(r"HMFAKE\d{4}|HA-FAKE\d{2}", booking.external_id), (
            f"{booking.external_id!r} is not an obviously-fake confirmation "
            "code in the real platform shape"
        )
        assert "preview" in booking.source_email_message_id


def test_demo_bookings_cover_the_paths_worth_demonstrating():
    bookings = build_demo_bookings(today=date(2026, 7, 30))
    platforms = {b.platform for b in bookings}
    assert platforms == {Platform.AIRBNB, Platform.VRBO}
    # Airbnb confirmations carry no phone and VRBO carries no email; both are
    # entered by hand on the dashboard, so the demo must show both states.
    assert any(b.guest_phone is None for b in bookings)
    assert any(b.guest_email is None for b in bookings)
    assert any(b.status == BookingStatus.ACTIVE for b in bookings)


def test_demo_bookings_are_dated_relative_to_today():
    """A persistent preview must not drift into showing only past stays."""
    today = date(2026, 7, 30)
    bookings = build_demo_bookings(today=today)
    assert any(b.check_in_date > today for b in bookings)
    for booking in bookings:
        assert booking.check_out_date > booking.check_in_date


def test_demo_bookings_carry_tasks_and_provenance():
    bookings = build_demo_bookings(today=date(2026, 7, 30))
    for booking in bookings:
        assert booking.tasks, f"{booking.external_id} has no tasks to display"
    assert any(dp.field_name == "access_code" for b in bookings for dp in b.data_points), (
        "at least one demo booking should show a door code, since that panel is "
        "the dashboard's most distinctive feature"
    )


def test_completed_demo_tasks_carry_completed_at():
    """The dashboard displays completed_at; COMPLETE without it is a bug."""
    for booking in build_demo_bookings(today=date(2026, 7, 30)):
        for task in booking.tasks:
            if task.state == TaskState.COMPLETE:
                assert task.completed_at is not None, (
                    f"{booking.external_id}/{task.task_type} is COMPLETE with no "
                    "completed_at"
                )
            else:
                assert task.completed_at is None


def test_demo_bookings_belong_to_a_property_the_config_knows(patch_load_config):
    """A booking on an unknown property id cannot resolve a property config, so
    its handlers fail and the dashboard omits the HOA panel — which is exactly
    what happened the first time this ran in a container.

    ``patch_load_config`` is the autouse fixture's config, which is the one
    ``demo_property_id`` reads here.
    """
    from app.preview import demo_property_id

    configured = {prop.id for prop in patch_load_config.properties}
    for booking in build_demo_bookings(property_id=demo_property_id()):
        assert booking.property_id in configured, (
            f"{booking.external_id} is on property {booking.property_id!r}, which "
            f"no configured property matches (config has {sorted(configured)})"
        )


def test_demo_property_id_comes_from_the_loaded_config():
    from app.config import AppConfig
    from app.preview import demo_property_id

    config = real_load_config("config.example.yaml")
    patched = AppConfig.model_validate(
        {
            **config.model_dump(),
            "properties": [{**config.properties[0].model_dump(), "id": "beach_house"}],
        }
    )
    with patch("app.config.load_config", return_value=patched):
        assert demo_property_id() == "beach_house"


def test_demo_property_id_falls_back_when_the_config_cannot_be_read():
    """Seeding must never be the reason a preview fails to boot."""
    from app.preview import DEFAULT_DEMO_PROPERTY_ID, demo_property_id

    with patch("app.config.load_config", side_effect=OSError("no config here")):
        assert demo_property_id() == DEFAULT_DEMO_PROPERTY_ID


def test_demo_task_states_are_all_known_values():
    for booking in build_demo_bookings(today=date(2026, 7, 30)):
        for task in booking.tasks:
            assert isinstance(task.state, TaskState)


# ---------------------------------------------------------------------------
# Lifespan: no jobs, seeded data, fail-fast
# ---------------------------------------------------------------------------


def _mock_settings(**overrides):
    """A MagicMock that looks like a valid settings object (see the pattern in
    tests/unit/test_main_lifespan.py)."""
    mock = MagicMock()
    mock.secret_key = "secure-secret-key"
    mock.database_url = PREVIEW_DATABASE_URL
    mock.log_level = "INFO"
    for key, value in overrides.items():
        setattr(mock, key, value)
    return mock


@pytest.mark.asyncio
async def test_preview_lifespan_registers_no_scheduler_jobs():
    from app import main

    scheduler = MagicMock()
    with (
        patch.object(main, "AsyncIOScheduler", MagicMock(return_value=scheduler)),
        patch.object(main, "is_preview_mode", return_value=True),
        patch.object(main, "assert_no_real_credentials"),
        patch.object(main, "seed_demo_bookings", new=AsyncMock(return_value=0)),
        patch.object(Path, "mkdir"),
    ):
        async with main.lifespan(MagicMock()):
            pass

    scheduler.add_job.assert_not_called()
    scheduler.start.assert_not_called()


@pytest.mark.asyncio
async def test_preview_lifespan_seeds_demo_bookings():
    from app import main

    seed_calls = []

    async def fake_seed():
        seed_calls.append(True)
        return 3

    with (
        patch.object(main, "AsyncIOScheduler", MagicMock()),
        patch.object(main, "is_preview_mode", return_value=True),
        patch.object(main, "assert_no_real_credentials"),
        patch.object(main, "seed_demo_bookings", fake_seed),
        patch.object(Path, "mkdir"),
    ):
        async with main.lifespan(MagicMock()):
            pass

    assert seed_calls == [True]


@pytest.mark.asyncio
async def test_preview_lifespan_refuses_to_start_on_real_credentials():
    from app import main

    boom = PreviewModeError("SEAM_API_KEY looks real")
    with (
        patch.object(main, "AsyncIOScheduler", MagicMock()),
        patch.object(main, "is_preview_mode", return_value=True),
        patch.object(main, "assert_no_real_credentials", side_effect=boom),
        patch.object(main, "seed_demo_bookings", new=AsyncMock()),
        patch.object(Path, "mkdir"),
    ):
        with pytest.raises(PreviewModeError):
            async with main.lifespan(MagicMock()):
                pass


@pytest.mark.asyncio
async def test_preview_lifespan_does_not_require_production_credentials():
    """Preview boots with an empty environment; the normal boot check would
    refuse it, so that check must be replaced, not merely supplemented."""
    from app import main

    with (
        patch.object(main, "AsyncIOScheduler", MagicMock()),
        patch.object(main, "settings", _mock_settings(**{
            field: "" for field, _ in main._REQUIRED_CREDENTIALS
        })),
        patch.object(main, "is_preview_mode", return_value=True),
        patch.object(main, "assert_no_real_credentials"),
        patch.object(main, "seed_demo_bookings", new=AsyncMock(return_value=0)),
        patch.object(Path, "mkdir"),
    ):
        async with main.lifespan(MagicMock()):
            pass  # must not raise


@pytest.mark.asyncio
async def test_normal_lifespan_still_registers_every_job():
    """Normal mode is unaffected: all ten scheduled jobs still register."""
    from app import main

    scheduler = MagicMock()
    with (
        patch.object(main, "AsyncIOScheduler", MagicMock(return_value=scheduler)),
        patch.object(main, "settings", _mock_settings()),
        patch.object(main, "is_preview_mode", return_value=False),
        patch.object(Path, "mkdir"),
    ):
        async with main.lifespan(MagicMock()):
            pass

    job_ids = {call.kwargs["id"] for call in scheduler.add_job.call_args_list}
    assert job_ids == {
        "poll_booking_feed",
        "check_daily_reminders",
        "check_hoa_window",
        "requeue_stalled_automations",
        "verify_access_codes",
        "complete_past_bookings",
        "refresh_docusign_token",
        "verify_credentials",
        "check_classifier_drift",
        "review_dead_letters",
        "send_monthly_status_email",
    }
    scheduler.start.assert_called_once()


@pytest.mark.asyncio
async def test_normal_lifespan_never_seeds_demo_data():
    from app import main

    with (
        patch.object(main, "AsyncIOScheduler", MagicMock()),
        patch.object(main, "settings", _mock_settings()),
        patch.object(main, "is_preview_mode", return_value=False),
        patch.object(main, "seed_demo_bookings", new=AsyncMock()) as seed,
        patch.object(Path, "mkdir"),
    ):
        async with main.lifespan(MagicMock()):
            pass
    seed.assert_not_called()


# ---------------------------------------------------------------------------
# The banner
# ---------------------------------------------------------------------------


def _render(page: str) -> str:
    """Render a real template through the app's own Jinja environment."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.requests import Request
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.templating import templates

    async def render(request: Request):
        return templates.TemplateResponse(request=request, name=page, context={})

    app = Starlette(
        middleware=[Middleware(SessionMiddleware, secret_key="test-only")],
        routes=[Route("/p", render)],
    )
    with TestClient(app) as client:
        return client.get("/p").text


def test_banner_is_rendered_in_preview_mode(preview_env):
    html = _render("login.html")
    assert "preview-banner" in html
    assert "PREVIEW" in html


def test_banner_is_absent_in_normal_mode():
    with patch.object(settings, "preview_mode", False):
        html = _render("login.html")
    assert "preview-banner" not in html
    assert "PREVIEW" not in html


def test_banner_says_the_data_is_fake_and_nothing_is_sent(preview_env):
    """An unmissable banner has to say what it means, not just shout."""
    html = _render("login.html").lower()
    assert "fake" in html or "demo" in html
    assert "no" in html and ("email" in html or "outbound" in html)


# ---------------------------------------------------------------------------
# Dashboard access, and the last two outbound paths
# ---------------------------------------------------------------------------


def test_require_user_grants_a_demo_identity_in_preview(preview_env):
    """Google sign-in cannot work with placeholder OIDC credentials, so the
    dashboard would otherwise be unreachable in a preview."""
    from app.preview import PREVIEW_USER
    from app.routers.auth import require_user

    request = MagicMock()
    request.session = {}
    assert require_user(request) == PREVIEW_USER


def test_require_user_still_demands_a_session_in_normal_mode():
    """The preview bypass must be reachable only through the switch."""
    from starlette.exceptions import HTTPException

    from app.routers.auth import require_user

    request = MagicMock()
    request.session = {}
    with patch.object(settings, "preview_mode", False):
        with pytest.raises(HTTPException) as exc:
            require_user(request)
    assert exc.value.status_code == 303


def test_google_oauth_redirect_makes_no_outbound_call_in_preview(preview_env):
    """authorize_redirect fetches Google's OIDC metadata document — outbound."""
    import asyncio

    from app.routers import auth

    request = MagicMock()
    with patch.object(auth.oauth, "google") as google:
        response = asyncio.run(auth.auth_login(request))
    google.authorize_redirect.assert_not_called()
    assert response.status_code == 303


def test_preview_falls_back_to_the_committed_example_config(preview_env):
    """A preview has no config.yaml, and must not be given the real one: it
    carries the owners' names, the HOA contact and the spreadsheet id."""
    config = real_load_config("config.does-not-exist.yaml")
    assert config.properties, "the example config should have loaded"
    assert config.owners.primary_name


def test_missing_config_still_raises_in_normal_mode():
    with patch.object(settings, "preview_mode", False):
        with pytest.raises(FileNotFoundError):
            real_load_config("config.does-not-exist.yaml")


def test_heartbeat_pings_are_suppressed_in_preview(preview_env):
    """A preview pinging the production monitor would mask a dead production."""
    from app import monitoring

    with patch.object(monitoring.httpx, "get") as http_get:
        monitoring.ping_heartbeat("https://example.com/ping/preview", label="poller")
    http_get.assert_not_called()


def test_heartbeat_pings_still_send_in_normal_mode():
    from app import monitoring

    with patch.object(settings, "preview_mode", False):
        with patch.object(monitoring.httpx, "get") as http_get:
            monitoring.ping_heartbeat("https://example.com/ping/x", label="poller")
    http_get.assert_called_once()


def test_banner_has_a_stylesheet_rule():
    """Same contract the status badges have: a class with no CSS is invisible."""
    css = Path("app/static/style.css").read_text()
    assert ".preview-banner" in css, (
        "the PREVIEW banner class has no CSS rule, so it would render as "
        "unstyled text — see the badge-CSS test in tests/unit/test_dashboard.py"
    )
