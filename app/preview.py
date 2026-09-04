"""Preview mode — the app booted with no capacity to reach the outside world.

A second ordinary instance of this service is dangerous by design. It polls the
same shared booking inbox, holds the same DocuSign, Seam and Google Sheets
credentials, and owns the same physical door lock, so two instances double-send
envelopes and email (the single-instance rule in the repo conventions doc's Portability
section). That makes a throwaway copy for demonstration or review unsafe to
boot by accident.

Preview mode removes the *capability* rather than trusting the configuration:

1. **No scheduler jobs.** The lifespan registers nothing at all, so no poller,
   no keep-alives, no digests, no reminder scans.
2. **Inert outbound clients.** Every integration's client factory (including the
   Claude API the inbox drift reviewer uses) returns an
   :class:`_InertClient` instead of a real client. The hook sits at the same
   construction choke point the test suite's live-API guard blocks, which is
   the only way through to the network for each integration.
3. **Fail fast on real-looking credentials.** Startup refuses if any
   credential-shaped setting is not placeholder-shaped, and refuses if
   ``DATABASE_URL`` does not name a throwaway database. A copy-pasted
   production environment therefore cannot boot in preview, which is the
   belt-and-braces half of (2).
4. **Seeded demo data.** Obviously-fake bookings are inserted at boot so the
   dashboard has something to show, using the placeholder conventions the test
   suite uses (``<First> Example`` names, 555 phone numbers, ``HMFAKE0001``
   confirmation codes, ``@example.com`` addresses).
5. **An unmissable banner** on every page, and an open dashboard: preview mode
   cannot use Google sign-in (its OAuth credentials are placeholders), so
   ``require_user`` grants a fixed demo identity instead.

Enable it with ``PREVIEW_MODE=1``. It is off by default and must never be on in
production.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    DataPoint,
    DataPointSource,
    Platform,
    TaskState,
    TaskType,
)
from app.settings import settings

log = logging.getLogger(__name__)


class PreviewModeError(RuntimeError):
    """Raised when preview mode refuses to start, or when inert code is used."""


def is_preview_mode() -> bool:
    """True when this process is running as a preview/demo deployment."""
    return bool(settings.preview_mode)


# ---------------------------------------------------------------------------
# Fail fast on anything that looks like a real credential
# ---------------------------------------------------------------------------
#
# Every credential-shaped setting, paired with the environment variable it is
# read from (pydantic-settings uppercases the field name). Two drift guards in
# tests/unit/test_preview_mode.py keep this list complete: one against
# app/main.py's required-at-boot list, one against the Settings model itself.

PLACEHOLDER_SCANNED_SETTINGS: list[tuple[str, str]] = [
    ("google_client_id", "GOOGLE_CLIENT_ID"),
    ("google_client_secret", "GOOGLE_CLIENT_SECRET"),
    ("gmail_booking_feed_refresh_token", "GMAIL_BOOKING_FEED_REFRESH_TOKEN"),
    ("gmail_alerts_refresh_token", "GMAIL_ALERTS_REFRESH_TOKEN"),
    ("google_sheets_refresh_token", "GOOGLE_SHEETS_REFRESH_TOKEN"),
    ("google_oauth_client_id", "GOOGLE_OAUTH_CLIENT_ID"),
    ("google_oauth_client_secret", "GOOGLE_OAUTH_CLIENT_SECRET"),
    ("docusign_account_id", "DOCUSIGN_ACCOUNT_ID"),
    ("docusign_client_id", "DOCUSIGN_CLIENT_ID"),
    ("docusign_client_secret", "DOCUSIGN_CLIENT_SECRET"),
    ("docusign_refresh_token", "DOCUSIGN_REFRESH_TOKEN"),
    ("docusign_hmac_key", "DOCUSIGN_HMAC_KEY"),
    ("seam_api_key", "SEAM_API_KEY"),
    ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    ("secret_key", "SECRET_KEY"),
    ("healthchecks_ping_url_poller", "HEALTHCHECKS_PING_URL_POLLER"),
    ("healthchecks_ping_url_sentinel", "HEALTHCHECKS_PING_URL_SENTINEL"),
    ("healthchecks_ping_url_keepalive", "HEALTHCHECKS_PING_URL_KEEPALIVE"),
]

# A value containing one of these reads as deliberately fake. The list is an
# allowlist on purpose: anything unrecognised is treated as real and refused,
# because the cost of a false alarm (an operator picks a clearer placeholder) is
# nothing next to the cost of a miss (a preview holding live credentials).
_PLACEHOLDER_MARKERS = (
    "placeholder",
    "preview",
    "staging",
    "example",
    "fake",
    "dummy",
    "change_me",
    "changeme",
    "not-a-real",
    "notreal",
    "insecure-default",
    "unset",
)

# Characters a filler value is built from: zeroed GUIDs, rows of x, dashes.
_FILLER_CHARACTERS = set("x0-_ ")

# A preview database must say so in its name, so a preview can never seed fake
# bookings into (or migrate) the production database.
_THROWAWAY_DATABASE_MARKERS = ("preview", "staging", "test", "demo", "scratch")


def _is_placeholder(value: object) -> bool:
    """True when *value* is empty, filler, or carries a placeholder marker."""
    if not isinstance(value, str):
        return not value
    text = value.strip().lower()
    if not text:
        return True
    if set(text) <= _FILLER_CHARACTERS:
        return True
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def _database_name(url: str) -> str:
    """The database name from a SQLAlchemy URL, without query parameters."""
    return url.split("?", 1)[0].rsplit("/", 1)[-1].lower()


def assert_no_real_credentials() -> None:
    """Refuse to boot a preview whose environment holds anything real.

    Raises :class:`PreviewModeError` naming every offending environment
    variable — never its value, because a preview's logs are the least
    protected place this app writes to.
    """
    offenders = [
        env_name
        for field_name, env_name in PLACEHOLDER_SCANNED_SETTINGS
        if not _is_placeholder(getattr(settings, field_name, ""))
    ]

    database_name = _database_name(settings.database_url)
    if not any(marker in database_name for marker in _THROWAWAY_DATABASE_MARKERS):
        offenders.append("DATABASE_URL")

    if offenders:
        raise PreviewModeError(
            "PREVIEW_MODE=1 but the environment does not look like a preview. "
            f"These variables must hold placeholders, not real values: "
            f"{', '.join(sorted(offenders))}. "
            "Credentials must be empty or contain one of "
            f"{', '.join(_PLACEHOLDER_MARKERS)}; DATABASE_URL must name a "
            "throwaway database (one containing "
            f"{', '.join(_THROWAWAY_DATABASE_MARKERS)}). "
            "Refusing to start rather than risk acting on the real world."
        )
    log.warning(
        "PREVIEW MODE: credential check passed — every credential is a "
        "placeholder and the database is a throwaway"
    )


# ---------------------------------------------------------------------------
# Inert outbound clients
# ---------------------------------------------------------------------------


class _InertClient:
    """Stands in for an API client and raises on any attempt to use it.

    Attribute access returns another inert node, so a whole SDK call chain
    (``client.access_codes.create(...)``) builds up and then raises at the call,
    naming the path it was asked to perform. Raising is the point: it is louder
    than a silent no-op, and a caller that records the error leaves the failure
    visible on the dashboard instead of reporting a success that never happened.
    """

    __slots__ = ("_name", "_path")

    def __init__(self, name: str, path: str = "") -> None:
        self._name = name
        self._path = path

    def __getattr__(self, attribute: str) -> _InertClient:
        # Dunder/private probing (copy, pickle, IPython) must look absent
        # rather than return something callable.
        if attribute.startswith("_"):
            raise AttributeError(attribute)
        path = f"{self._path}.{attribute}" if self._path else attribute
        return _InertClient(self._name, path)

    def __call__(self, *args: object, **kwargs: object) -> None:
        raise PreviewModeError(
            f"Blocked an outbound {self._name} call in preview mode "
            f"({self._path or 'client'}). Preview deployments hold no real "
            "credentials and make no outbound calls."
        )

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        suffix = f" at {self._path}" if self._path else ""
        return f"<inert {self._name} client (preview mode){suffix}>"


def inert_client(name: str) -> _InertClient:
    """Return an inert stand-in for the *name* API client, and log that fact."""
    log.warning(
        "PREVIEW MODE: returning an inert %s client — no outbound call will be made",
        name,
    )
    return _InertClient(name)


# ---------------------------------------------------------------------------
# Seeded demo data
# ---------------------------------------------------------------------------
#
# Placeholder conventions, identical to the test suite's (the `testing-safely`
# skill): surnames are the literal "Example", phone numbers are 555 numbers,
# email addresses are @example.com, and confirmation codes are obviously fake
# inside the real platform shape (Airbnb HMxxxxxxxxx, VRBO HA-xxxxxx).

_DEMO_ACCESS_CODE = "5551"

# The property id the demo bookings belong to. It has to match a property in the
# loaded config or the handlers and the dashboard's HOA panel cannot resolve one
# — so it defaults to the id in the committed config template, which is what a
# preview loads when it has no config.yaml of its own.
DEFAULT_DEMO_PROPERTY_ID = "property_1"

# Phone numbers are stored the way the dashboard's manual-entry validator stores
# them: digits only. Anything prettier would not match what a real booking holds.
_DEMO_PHONES = ("5550100001", "5550100002", "5550100003")

_PARSED_FIELDS = (
    "guest_first_name",
    "guest_last_name",
    "guest_phone",
    "guest_email",
    "check_in_date",
    "check_out_date",
)


def _complete(task_type: TaskType, *, days_ago: int = 1) -> BookingTask:
    """A COMPLETE task, with the completed_at the dashboard expects."""
    return BookingTask(
        task_type=task_type,
        state=TaskState.COMPLETE,
        completed_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


def _task(task_type: TaskType, state: TaskState) -> BookingTask:
    return BookingTask(task_type=task_type, state=state)


def _provenance(booking: Booking) -> list[DataPoint]:
    """Provenance rows for the fields a confirmation email would have carried."""
    points: list[DataPoint] = []
    for field_name in _PARSED_FIELDS:
        value = getattr(booking, field_name)
        if value is None:
            continue
        points.append(
            DataPoint(
                field_name=field_name,
                value=str(value),
                source=DataPointSource.EMAIL_PARSE,
                notes="Seeded demo data (preview mode)",
            )
        )
    return points


def demo_property_id() -> str:
    """The property id the demo bookings should use.

    Read from the loaded configuration so the demo works against whatever
    config a preview was given (its own, or the committed template it falls back
    to). Falls back to the template's id if the config cannot be read at all,
    because seeding must not be what stops a preview from booting.
    """
    try:
        from app.config import load_config

        properties = load_config().properties
        if properties:
            return properties[0].id
    except Exception as exc:  # noqa: BLE001 - a preview must boot regardless
        log.warning(
            "PREVIEW MODE: could not read a property id from config (%s); "
            "seeding demo bookings against %s",
            exc,
            DEFAULT_DEMO_PROPERTY_ID,
        )
    return DEFAULT_DEMO_PROPERTY_ID


def build_demo_bookings(
    *, today: date | None = None, property_id: str = DEFAULT_DEMO_PROPERTY_ID
) -> list[Booking]:
    """Build the demo bookings, unsaved. Pure: no database, no I/O.

    Dates are relative to *today* so a long-lived preview keeps showing
    upcoming stays rather than drifting into an archive of past ones.
    """
    day = today or date.today()

    bookings: list[Booking] = []

    # 1. The happy path: every automation done, door code issued.
    fully_automated = Booking(
        platform=Platform.AIRBNB,
        external_id="HMFAKE0001",
        property_id=property_id,
        guest_first_name="Ada",
        guest_last_name="Example",
        guest_phone=_DEMO_PHONES[0],
        guest_email="ada.example@example.com",
        check_in_date=day + timedelta(days=12),
        check_out_date=day + timedelta(days=15),
        status=BookingStatus.ACTIVE,
        source_email_message_id="<preview-hmfake0001@example.com>",
    )
    fully_automated.tasks.extend(
        [
            _complete(TaskType.OWNER_ALERT_NEW_BOOKING, days_ago=6),
            _complete(TaskType.CLEANER_SHEET_ADD, days_ago=6),
            _complete(TaskType.DOCUSIGN_SEND, days_ago=6),
            _complete(TaskType.HOA_EMAIL, days_ago=2),
            _complete(TaskType.ACCESS_CODE_CREATE, days_ago=2),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_4D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_4D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, TaskState.SKIPPED),
        ]
    )
    fully_automated.data_points.extend(_provenance(fully_automated))
    fully_automated.data_points.append(
        DataPoint(
            field_name="access_code",
            value=_DEMO_ACCESS_CODE,
            source=DataPointSource.SEAM_API,
            notes="Seeded demo data (preview mode) — no lock was programmed",
        )
    )
    bookings.append(fully_automated)

    # 2. VRBO confirmations carry no guest email, so the form cannot be sent
    #    until the owner types one into the dashboard.
    awaiting_email = Booking(
        platform=Platform.VRBO,
        external_id="HA-FAKE01",
        property_id=property_id,
        guest_first_name="Bo",
        guest_last_name="Example",
        guest_phone=_DEMO_PHONES[1],
        guest_email=None,
        check_in_date=day + timedelta(days=5),
        check_out_date=day + timedelta(days=9),
        status=BookingStatus.ACTIVE,
        source_email_message_id="<preview-hafake01@example.com>",
    )
    awaiting_email.tasks.extend(
        [
            _complete(TaskType.OWNER_ALERT_NEW_BOOKING, days_ago=9),
            _complete(TaskType.CLEANER_SHEET_ADD, days_ago=9),
            _task(TaskType.DOCUSIGN_SEND, TaskState.WAITING),
            _task(TaskType.HOA_EMAIL, TaskState.PENDING),
            _complete(TaskType.ACCESS_CODE_CREATE, days_ago=1),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_4D, TaskState.SKIPPED),
            _complete(TaskType.OWNER_ALERT_MISSING_EMAIL_7D, days_ago=2),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_4D, TaskState.PENDING),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, TaskState.WAITING),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, TaskState.WAITING),
        ]
    )
    awaiting_email.data_points.extend(_provenance(awaiting_email))
    awaiting_email.data_points.append(
        DataPoint(
            field_name="access_code",
            value=_DEMO_ACCESS_CODE,
            source=DataPointSource.SEAM_API,
            notes="Seeded demo data (preview mode) — no lock was programmed",
        )
    )
    bookings.append(awaiting_email)

    # 3. Airbnb confirmations carry no phone number, so the door code waits on
    #    the owner entering one.
    awaiting_phone = Booking(
        platform=Platform.AIRBNB,
        external_id="HMFAKE0002",
        property_id=property_id,
        guest_first_name="Cleo",
        guest_last_name="Example",
        guest_phone=None,
        guest_email="cleo.example@example.com",
        check_in_date=day + timedelta(days=25),
        check_out_date=day + timedelta(days=28),
        status=BookingStatus.ACTIVE,
        source_email_message_id="<preview-hmfake0002@example.com>",
    )
    awaiting_phone.tasks.extend(
        [
            _complete(TaskType.OWNER_ALERT_NEW_BOOKING, days_ago=3),
            _complete(TaskType.CLEANER_SHEET_ADD, days_ago=3),
            _complete(TaskType.DOCUSIGN_SEND, days_ago=3),
            _task(TaskType.HOA_EMAIL, TaskState.WAITING),
            _task(TaskType.ACCESS_CODE_CREATE, TaskState.WAITING),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_7D, TaskState.PENDING),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_4D, TaskState.PENDING),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_4D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, TaskState.PENDING),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, TaskState.WAITING),
        ]
    )
    awaiting_phone.data_points.extend(_provenance(awaiting_phone))
    bookings.append(awaiting_phone)

    # 4. A cancellation, so the dashboard's cancelled state is visible too.
    cancelled = Booking(
        platform=Platform.VRBO,
        external_id="HA-FAKE02",
        property_id=property_id,
        guest_first_name="Dev",
        guest_last_name="Example",
        guest_phone=_DEMO_PHONES[2],
        guest_email=None,
        check_in_date=day + timedelta(days=3),
        check_out_date=day + timedelta(days=6),
        status=BookingStatus.CANCELLED,
        source_email_message_id="<preview-hafake02@example.com>",
        cancellation_email_message_id="<preview-hafake02-cancel@example.com>",
    )
    cancelled.tasks.extend(
        [
            _complete(TaskType.OWNER_ALERT_NEW_BOOKING, days_ago=11),
            _task(TaskType.CLEANER_SHEET_ADD, TaskState.SKIPPED),
            _task(TaskType.DOCUSIGN_SEND, TaskState.SKIPPED),
            _task(TaskType.HOA_EMAIL, TaskState.SKIPPED),
            _task(TaskType.ACCESS_CODE_CREATE, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_PHONE_4D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_MISSING_EMAIL_4D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, TaskState.SKIPPED),
            _task(TaskType.OWNER_ALERT_CANCELLATION_HOA, TaskState.PENDING),
            _task(TaskType.OWNER_ALERT_CANCELLATION_CLEANER, TaskState.PENDING),
        ]
    )
    cancelled.data_points.extend(_provenance(cancelled))
    bookings.append(cancelled)

    return bookings


async def seed_demo_bookings(session_factory=None) -> int:  # noqa: ANN001
    """Insert the demo bookings that aren't already there. Returns how many.

    Idempotent by ``(platform, external_id)``: a preview container restarts
    whenever the platform redeploys it, and a restart must not duplicate the
    demo data. One session per booking, per the scheduled-job convention — a
    shared session's rollback would expire every instance loaded before it.
    """
    from app.db.session import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    created = 0

    for booking in build_demo_bookings(property_id=demo_property_id()):
        async with factory() as session:
            existing = (
                await session.execute(
                    select(Booking.id).where(
                        Booking.platform == booking.platform,
                        Booking.external_id == booking.external_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(booking)
            await session.commit()
            created += 1

    log.warning(
        "PREVIEW MODE: seeded %d demo booking(s); all data is fake", created
    )
    return created


# ---------------------------------------------------------------------------
# The dashboard in preview mode
# ---------------------------------------------------------------------------

# Preview deployments hold placeholder Google OIDC credentials, so nobody can
# sign in. The dashboard is the thing worth demonstrating, so preview mode
# grants this fixed identity to every visitor instead. Safe only because a
# preview's data is entirely fabricated — which `assert_no_real_credentials`
# is what guarantees.
PREVIEW_USER = "preview@example.com"

_BANNER_TEXT = (
    "PREVIEW — demo deployment. All bookings and guests shown are fake, and no "
    "email, e-signature, door code or spreadsheet update is ever sent."
)


def preview_banner_text() -> str:
    return _BANNER_TEXT


def register_template_globals(templates) -> None:  # noqa: ANN001
    """Expose preview state to Jinja templates.

    ``preview_mode`` is registered as the *function*, not its value, so a
    template rendered after the setting changes (tests, and a restart with a
    different environment) reflects the current state rather than the state at
    import time.
    """
    templates.env.globals["preview_mode"] = is_preview_mode
    templates.env.globals["preview_banner_text"] = preview_banner_text


__all__ = [
    "PLACEHOLDER_SCANNED_SETTINGS",
    "PREVIEW_USER",
    "PreviewModeError",
    "assert_no_real_credentials",
    "build_demo_bookings",
    "demo_property_id",
    "inert_client",
    "is_preview_mode",
    "preview_banner_text",
    "register_template_globals",
    "seed_demo_bookings",
]
