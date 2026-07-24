from pathlib import Path
from typing import Optional
import yaml
from pydantic import BaseModel


class HOAConfig(BaseModel):
    enabled: bool
    email: Optional[str] = None
    open_days: list[int] = [1, 2, 3, 4, 5, 6]
    email_window_days_min: int = 2
    email_window_days_max: int = 7


class CleanerScheduleConfig(BaseModel):
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
    booking_feed: str
    alerts: str


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
    with open(path) as f:
        data = yaml.safe_load(f)
    return AppConfig.model_validate(data)
