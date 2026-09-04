import logging
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

log = logging.getLogger(__name__)

# The committed template. Preview deployments fall back to it (see load_config),
# so demonstrating the app never requires hand-building a config file — and
# never tempts anyone into mounting the real one, which carries the owners'
# names, the HOA contact and the cleaner spreadsheet id.
EXAMPLE_CONFIG_PATH = Path("config.example.yaml")


class HOAConfig(BaseModel):
    enabled: bool
    email: Optional[str] = None
    open_days: list[int] = [1, 2, 3, 4, 5, 6]
    email_window_days_min: int = 2
    email_window_days_max: int = 7


class CleanerScheduleConfig(BaseModel):
    # Master switch for cleaner-sheet writes on this property. Set false and the
    # app stops touching the spreadsheet entirely: no row insert, no Sheets
    # client construction, no credential use. Turned off in production on
    # 2026-08-02 because the cleaning company's own script now watches the
    # booking calendars and writes the row itself; two writers would duplicate
    # every row. Everything else about the booking workflow is unaffected.
    #
    # Defaults True so an existing config keeps its behaviour on upgrade, and so
    # the flag has to be set deliberately rather than acquired by accident.
    # spreadsheet_id and sheet_name stay REQUIRED even when disabled: they are
    # what makes re-enabling a one-word edit rather than a scavenger hunt.
    enabled: bool = True
    type: str
    spreadsheet_id: str
    sheet_name: str
    sentinel_pattern: str = "--- end ---"


class PropertyConfig(BaseModel):
    id: str
    hoa: HOAConfig
    cleaner_schedule: CleanerScheduleConfig
    seam_device_id: str
    docusign_template_id: str
    docusign_signer_role: str = "signer"


class OwnersConfig(BaseModel):
    primary_name: str
    cohost_name: str
    # Full name for the HOA email sign-off and the outbound "From" display name.
    # Optional; falls back to primary_name when unset (see signature_name).
    primary_full_name: str | None = None

    @property
    def signature_name(self) -> str:
        """Name used to sign HOA emails and as the From display name."""
        return self.primary_full_name or self.primary_name


class EmailConfig(BaseModel):
    """The two system mailboxes, plus anyone else who should see owner alerts.

    ``alerts`` is the mailbox the automation sends *from*, and it is always a
    recipient too. ``alerts_additional_recipients`` adds further addresses to
    the To header of every owner-facing alert — a co-owner who wants the same
    notifications on their own phone. It never affects the From header, so a
    reply still lands in the mailbox the automation reads, and it never affects
    the HOA email, which addresses the HOA and nobody else.
    """

    booking_feed: str
    alerts: str
    alerts_additional_recipients: list[str] = []

    @property
    def alerts_to_header(self) -> str:
        """The To header for owner alerts: the alerts mailbox, then the extras.

        Blank entries are dropped. An empty address in a comma-joined To header
        is not a harmless no-op — Gmail rejects the whole message, which would
        silence every alert at once.
        """
        extras = [r.strip() for r in self.alerts_additional_recipients if r.strip()]
        return ", ".join([self.alerts, *extras])


class DashboardConfig(BaseModel):
    """Owner-dashboard access control.

    ``allowed_emails`` is the allowlist of Google accounts permitted to sign in.
    Anyone can authenticate with Google, but only these emails are authorized;
    an empty list means nobody can log in (fail closed).
    """

    allowed_emails: list[str] = []

    @property
    def allowed_emails_normalized(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails if e.strip()}


class AppConfig(BaseModel):
    owners: OwnersConfig
    email: EmailConfig
    properties: list[PropertyConfig]
    # Optional so pre-existing configs still load; empty allowlist fails closed.
    dashboard: DashboardConfig = DashboardConfig()


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    resolved = Path(path)
    if not resolved.is_file():
        # Local import: app.config is otherwise dependency-free.
        from app.preview import is_preview_mode

        if is_preview_mode() and EXAMPLE_CONFIG_PATH.is_file():
            log.warning(
                "PREVIEW MODE: %s is absent, loading the committed template %s",
                resolved,
                EXAMPLE_CONFIG_PATH,
            )
            resolved = EXAMPLE_CONFIG_PATH
    with open(resolved) as f:
        data = yaml.safe_load(f)
    return AppConfig.model_validate(data)
