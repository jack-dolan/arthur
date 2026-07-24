import pytest
from pathlib import Path
from pydantic import ValidationError

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_load_valid_config_returns_app_config():
    from app.config import load_config, AppConfig
    config = load_config(FIXTURES / "config.test.yaml")
    assert isinstance(config, AppConfig)


def test_owners_loaded_correctly():
    from app.config import load_config
    config = load_config(FIXTURES / "config.test.yaml")
    assert config.owners.primary_name == "TestOwner"
    assert config.owners.cohost_name == "TestCohost"


def test_owners_signature_name_falls_back_to_primary_name():
    """signature_name (used for HOA sign-off + From display) uses primary_full_name
    when set, otherwise falls back to primary_name."""
    from app.config import OwnersConfig
    o = OwnersConfig(primary_name="TestOwner", cohost_name="TestCohost")
    assert o.signature_name == "TestOwner"
    o2 = OwnersConfig(
        primary_name="TestOwner", cohost_name="TestCohost",
        primary_full_name="TestOwner Example",
    )
    assert o2.signature_name == "TestOwner Example"


def test_email_addresses_loaded():
    from app.config import load_config
    config = load_config(FIXTURES / "config.test.yaml")
    assert config.email.booking_feed == "feed@example.com"
    assert config.email.alerts == "alerts@example.com"


def test_single_property_loaded():
    from app.config import load_config, PropertyConfig
    config = load_config(FIXTURES / "config.test.yaml")
    assert len(config.properties) == 1
    assert isinstance(config.properties[0], PropertyConfig)
    assert config.properties[0].id == "test_property"


def test_hoa_enabled_config():
    from app.config import load_config, HOAConfig
    config = load_config(FIXTURES / "config.test.yaml")
    hoa = config.properties[0].hoa
    assert isinstance(hoa, HOAConfig)
    assert hoa.enabled is True
    assert hoa.email == "hoa@example.com"
    assert hoa.open_days == [1, 2, 3, 4, 5, 6]
    assert hoa.email_window_days_min == 2
    assert hoa.email_window_days_max == 7


def test_hoa_disabled_requires_no_email():
    from app.config import AppConfig
    data = {
        "owners": {"primary_name": "A", "cohost_name": "B"},
        "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
        "properties": [{
            "id": "p1",
            "hoa": {"enabled": False},
            "cleaner_schedule": {"type": "google_sheets", "spreadsheet_id": "sid1", "sheet_name": "Sheet"},
            "seam_device_id": "dev1",
            "docusign_template_id": "tpl1",
        }],
    }
    config = AppConfig.model_validate(data)
    assert config.properties[0].hoa.enabled is False
    assert config.properties[0].hoa.email is None


def test_multiple_properties_supported():
    from app.config import AppConfig
    data = {
        "owners": {"primary_name": "A", "cohost_name": "B"},
        "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
        "properties": [
            {
                "id": "p1",
                "hoa": {"enabled": False},
                "cleaner_schedule": {"type": "google_sheets", "spreadsheet_id": "sid1", "sheet_name": "Sheet1"},
                "seam_device_id": "dev1",
                "docusign_template_id": "tpl1",
            },
            {
                "id": "p2",
                "hoa": {"enabled": True, "email": "hoa@example.com"},
                "cleaner_schedule": {"type": "google_sheets", "spreadsheet_id": "sid2", "sheet_name": "Sheet2"},
                "seam_device_id": "dev2",
                "docusign_template_id": "tpl2",
            },
        ],
    }
    config = AppConfig.model_validate(data)
    assert len(config.properties) == 2
    assert config.properties[0].id == "p1"
    assert config.properties[1].id == "p2"


def test_missing_owners_cohost_raises_validation_error():
    from app.config import AppConfig
    with pytest.raises(ValidationError):
        AppConfig.model_validate({
            "owners": {"primary_name": "A"},
            "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
            "properties": [],
        })


def test_cleaner_schedule_config():
    from app.config import load_config
    config = load_config(FIXTURES / "config.test.yaml")
    schedule = config.properties[0].cleaner_schedule
    assert schedule.type == "google_sheets"
    assert schedule.sheet_name == "Test Cleaner Sheet"


# ── Phase 4 config extension tests ────────────────────────────────────────────

def test_config_requires_spreadsheet_id():
    """CleanerScheduleConfig must reject configs missing spreadsheet_id."""
    from app.config import AppConfig
    with pytest.raises(ValidationError) as exc_info:
        AppConfig.model_validate({
            "owners": {"primary_name": "A", "cohost_name": "B"},
            "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
            "properties": [{
                "id": "p1",
                "hoa": {"enabled": False},
                "cleaner_schedule": {"type": "google_sheets", "sheet_name": "Sheet"},
                "seam_device_id": "dev1",
                "docusign_template_id": "tpl1",
            }],
        })
    assert "spreadsheet_id" in str(exc_info.value)


def test_config_loads_spreadsheet_id():
    """CleanerScheduleConfig stores spreadsheet_id when provided."""
    from app.config import AppConfig
    config = AppConfig.model_validate({
        "owners": {"primary_name": "A", "cohost_name": "B"},
        "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
        "properties": [{
            "id": "p1",
            "hoa": {"enabled": False},
            "cleaner_schedule": {
                "type": "google_sheets",
                "spreadsheet_id": "abc123",
                "sheet_name": "Tab Name",
            },
            "seam_device_id": "dev1",
            "docusign_template_id": "tpl1",
        }],
    })
    prop = config.properties[0]
    assert prop.cleaner_schedule.spreadsheet_id == "abc123"
    assert prop.cleaner_schedule.sheet_name == "Tab Name"


def test_config_docusign_signer_role_default_is_signer():
    """PropertyConfig defaults docusign_signer_role to 'signer'."""
    from app.config import AppConfig
    config = AppConfig.model_validate({
        "owners": {"primary_name": "A", "cohost_name": "B"},
        "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
        "properties": [{
            "id": "p1",
            "hoa": {"enabled": False},
            "cleaner_schedule": {
                "type": "google_sheets",
                "spreadsheet_id": "abc123",
                "sheet_name": "Sheet",
            },
            "seam_device_id": "dev1",
            "docusign_template_id": "tpl1",
        }],
    })
    assert config.properties[0].docusign_signer_role == "signer"


def test_cleaner_schedule_config_sentinel_pattern_default():
    """CleanerScheduleConfig.sentinel_pattern defaults to '--- end ---'."""
    from app.config import CleanerScheduleConfig
    config = CleanerScheduleConfig(type="google_sheets", spreadsheet_id="x", sheet_name="y")
    assert config.sentinel_pattern == "--- end ---"


def test_cleaner_schedule_config_sentinel_pattern_custom():
    """CleanerScheduleConfig accepts a custom sentinel_pattern value."""
    from app.config import CleanerScheduleConfig
    config = CleanerScheduleConfig(
        type="google_sheets", spreadsheet_id="x", sheet_name="y",
        sentinel_pattern="** fin **",
    )
    assert config.sentinel_pattern == "** fin **"


def test_config_docusign_signer_role_override():
    """PropertyConfig respects docusign_signer_role when explicitly set."""
    from app.config import AppConfig
    config = AppConfig.model_validate({
        "owners": {"primary_name": "A", "cohost_name": "B"},
        "email": {"booking_feed": "feed@example.com", "alerts": "alerts@example.com"},
        "properties": [{
            "id": "p1",
            "hoa": {"enabled": False},
            "cleaner_schedule": {
                "type": "google_sheets",
                "spreadsheet_id": "abc123",
                "sheet_name": "Sheet",
            },
            "seam_device_id": "dev1",
            "docusign_template_id": "tpl1",
            "docusign_signer_role": "Guest",
        }],
    })
    assert config.properties[0].docusign_signer_role == "Guest"
