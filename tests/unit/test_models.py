"""Unit tests for SQLAlchemy model shape (no DB required)."""
from __future__ import annotations


def test_platform_enum_has_airbnb_and_vrbo():
    from app.db.models import Platform
    assert Platform.AIRBNB.value == "airbnb"
    assert Platform.VRBO.value == "vrbo"


def test_booking_status_enum_has_active_and_cancelled():
    from app.db.models import BookingStatus
    assert BookingStatus.ACTIVE.value == "active"
    assert BookingStatus.CANCELLED.value == "cancelled"


def test_task_type_enum_covers_all_known_tasks():
    from app.db.models import TaskType
    expected = {
        "owner_alert_new_booking",
        "docusign_send",
        "cleaner_sheet_add",
        "access_code_create",
        "hoa_email",
        "owner_alert_missing_phone_7d",
        "owner_alert_missing_phone_4d",
        "owner_alert_missing_email_7d",
        "owner_alert_missing_email_4d",
        "owner_alert_docusign_unsigned_7d",
        "owner_alert_docusign_unsigned_4d",
        "owner_alert_cancellation_hoa",
        "owner_alert_cancellation_cleaner",
    }
    assert {t.value for t in TaskType} == expected


def test_task_state_enum_values():
    from app.db.models import TaskState
    assert {s.value for s in TaskState} == {
        "pending", "waiting", "in_progress", "complete", "failed", "skipped"
    }


def test_data_point_source_enum_values():
    from app.db.models import DataPointSource
    assert {s.value for s in DataPointSource} == {
        "email_parse", "manual_entry", "web_scrape", "docusign_webhook",
        "seam_api", "system_default"
    }


def test_booking_table_required_columns():
    from app.db.models import Booking
    cols = {c.name: c for c in Booking.__table__.columns}
    required_non_null = {
        "id", "platform", "external_id", "property_id",
        "guest_first_name", "guest_last_name",
        "check_in_date", "check_out_date", "status",
        "source_email_message_id", "created_at", "updated_at",
    }
    assert required_non_null.issubset(cols.keys())
    for name in required_non_null:
        assert cols[name].nullable is False, f"{name} should be NOT NULL"


def test_booking_table_nullable_columns():
    from app.db.models import Booking
    cols = {c.name: c for c in Booking.__table__.columns}
    for name in ("guest_phone", "guest_email", "cancellation_email_message_id"):
        assert cols[name].nullable is True, f"{name} should be nullable"


def test_booking_unique_constraints():
    from app.db.models import Booking
    constraints = {
        tuple(sorted(c.name for c in con.columns))
        for con in Booking.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    # source_email_message_id unique either via UniqueConstraint or unique=True on column
    unique_cols = {c.name for c in Booking.__table__.columns if c.unique}
    assert ("external_id", "platform") in constraints
    assert "source_email_message_id" in unique_cols
    assert "cancellation_email_message_id" in unique_cols


def test_booking_has_relationships_to_tasks_and_data_points():
    from app.db.models import Booking
    rels = {r.key for r in Booking.__mapper__.relationships}
    assert "tasks" in rels
    assert "data_points" in rels


def test_booking_task_table_columns_and_fk():
    from app.db.models import BookingTask
    cols = {c.name: c for c in BookingTask.__table__.columns}
    for name in (
        "id", "booking_id", "task_type", "state", "attempt_count", "created_at", "updated_at"
    ):
        assert name in cols
        assert cols[name].nullable is False, f"{name} should be NOT NULL"
    for name in ("external_ref", "last_error", "completed_at"):
        assert cols[name].nullable is True

    fk = next(iter(cols["booking_id"].foreign_keys))
    assert fk.column.table.name == "bookings"
    assert fk.ondelete == "CASCADE"


def test_booking_task_unique_on_booking_and_task_type():
    from app.db.models import BookingTask
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in BookingTask.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("booking_id", "task_type") in uniques


def test_data_point_table_columns_and_fk():
    from app.db.models import DataPoint
    cols = {c.name: c for c in DataPoint.__table__.columns}
    for name in ("id", "booking_id", "field_name", "value", "source", "recorded_at"):
        assert cols[name].nullable is False, f"{name} should be NOT NULL"
    assert cols["notes"].nullable is True
    fk = next(iter(cols["booking_id"].foreign_keys))
    assert fk.column.table.name == "bookings"
    assert fk.ondelete == "CASCADE"
