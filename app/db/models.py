"""SQLAlchemy ORM models for the booking workflow.

`bookings` holds the current state of each reservation. `booking_tasks` is the
state machine of pre-arrival work for each booking. `data_points` is the
provenance audit log — every value the system records about a booking has a
row here so the dashboard can show *how* a field came to be its current value.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class UnknownEnumValue(str):
    """A database enum value introduced by a newer application release.

    SQLAlchemy normally raises ``LookupError`` while materializing a row whose
    PostgreSQL enum value is absent from the Python enum bundled in this build.
    Returning a string-like sentinel keeps rollback reads alive while making
    the semantic gap explicit to callers and templates.
    """

    @property
    def value(self) -> str:
        return str(self)


class _TolerantEnumResult:
    def _object_value_for_elem(self, elem):  # noqa: ANN001
        try:
            return super()._object_value_for_elem(elem)
        except LookupError:
            return UnknownEnumValue(elem)


_TOLERANT_DIALECT_TYPES: dict[type, type] = {}


class TolerantSAEnum(_TolerantEnumResult, SAEnum):
    """SQLAlchemy Enum that preserves unknown database values as sentinels."""

    def adapt(self, cls, **kw):  # noqa: ANN001, ANN003
        # PostgreSQL replaces sqlalchemy.Enum with a driver-specific ENUM
        # implementation. Preserve the tolerant result conversion in that
        # adapted class as well; otherwise the generic override never runs.
        # The generic/default dialect adapts the type to its own class; that
        # class is already tolerant and must not receive the mixin twice.
        if issubclass(cls, _TolerantEnumResult):
            return super().adapt(cls, **kw)
        tolerant_cls = _TOLERANT_DIALECT_TYPES.get(cls)
        if tolerant_cls is None:
            tolerant_cls = type(
                f"Tolerant{cls.__name__}",
                (_TolerantEnumResult, cls),
                {},
            )
            _TOLERANT_DIALECT_TYPES[cls] = tolerant_cls
        return super().adapt(tolerant_cls, **kw)


class Platform(str, enum.Enum):
    AIRBNB = "airbnb"
    VRBO = "vrbo"


class BookingStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class TaskType(str, enum.Enum):
    OWNER_ALERT_NEW_BOOKING = "owner_alert_new_booking"
    DOCUSIGN_SEND = "docusign_send"
    CLEANER_SHEET_ADD = "cleaner_sheet_add"
    ACCESS_CODE_CREATE = "access_code_create"
    HOA_EMAIL = "hoa_email"
    OWNER_ALERT_MISSING_PHONE_7D = "owner_alert_missing_phone_7d"
    OWNER_ALERT_MISSING_PHONE_4D = "owner_alert_missing_phone_4d"
    OWNER_ALERT_MISSING_EMAIL_7D = "owner_alert_missing_email_7d"
    OWNER_ALERT_MISSING_EMAIL_4D = "owner_alert_missing_email_4d"
    OWNER_ALERT_DOCUSIGN_UNSIGNED_7D = "owner_alert_docusign_unsigned_7d"
    OWNER_ALERT_DOCUSIGN_UNSIGNED_4D = "owner_alert_docusign_unsigned_4d"
    OWNER_ALERT_CANCELLATION_HOA = "owner_alert_cancellation_hoa"
    OWNER_ALERT_CANCELLATION_CLEANER = "owner_alert_cancellation_cleaner"


class TaskState(str, enum.Enum):
    PENDING = "pending"
    WAITING = "waiting"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class DataPointSource(str, enum.Enum):
    EMAIL_PARSE = "email_parse"
    MANUAL_ENTRY = "manual_entry"
    WEB_SCRAPE = "web_scrape"
    DOCUSIGN_WEBHOOK = "docusign_webhook"
    SEAM_API = "seam_api"
    SYSTEM_DEFAULT = "system_default"


_platform_enum = TolerantSAEnum(
    Platform, name="platform", values_callable=lambda e: [m.value for m in e]
)
_status_enum = TolerantSAEnum(
    BookingStatus, name="booking_status", values_callable=lambda e: [m.value for m in e]
)
_task_type_enum = TolerantSAEnum(
    TaskType, name="task_type", values_callable=lambda e: [m.value for m in e]
)
_task_state_enum = TolerantSAEnum(
    TaskState, name="task_state", values_callable=lambda e: [m.value for m in e]
)
_data_point_source_enum = TolerantSAEnum(
    DataPointSource, name="data_point_source", values_callable=lambda e: [m.value for m in e]
)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[Platform] = mapped_column(_platform_enum, nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    property_id: Mapped[str] = mapped_column(String(64), nullable=False)
    guest_first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    guest_last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    guest_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guest_email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        _status_enum,
        nullable=False,
        default=BookingStatus.ACTIVE,
        server_default=BookingStatus.ACTIVE.value,
    )
    source_email_message_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    cancellation_email_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    signed_pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[list["BookingTask"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", passive_deletes=True
    )
    data_points: Mapped[list["DataPoint"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_bookings_platform_external_id"),
        Index("ix_bookings_check_in_date", "check_in_date"),
        Index("ix_bookings_status", "status"),
    )


class BookingTask(Base):
    __tablename__ = "booking_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[TaskType] = mapped_column(_task_type_enum, nullable=False)
    state: Mapped[TaskState] = mapped_column(
        _task_state_enum,
        nullable=False,
        default=TaskState.PENDING,
        server_default=TaskState.PENDING.value,
    )
    external_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    booking: Mapped[Booking] = relationship(back_populates="tasks")

    __table_args__ = (
        UniqueConstraint("booking_id", "task_type", name="uq_booking_tasks_booking_id_task_type"),
        Index("ix_booking_tasks_state", "state"),
    )


class DataPoint(Base):
    __tablename__ = "data_points"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[DataPointSource] = mapped_column(_data_point_source_enum, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    booking: Mapped[Booking] = relationship(back_populates="data_points")

    __table_args__ = (
        Index("ix_data_points_booking_field", "booking_id", "field_name"),
    )


class ProcessedMessage(Base):
    """Dead-letter record for a booking-feed message that produced no booking.

    The poller's normal idempotency comes from
    ``bookings.source_email_message_id`` / ``cancellation_email_message_id``.
    But a message classified as OTHER, or classified as a booking yet failing to
    parse, never creates a booking — so without this table it would be re-fetched
    on every 5-minute poll forever (Step 19 / R3b). Recording those terminal
    outcomes here (and unioning ``message_id`` into the poller's ``already_seen``
    set) makes them idempotent.

    Deliberately NOT recorded here: a cancellation whose booking does not exist
    yet. That case self-heals once the booking arrives, so it must keep being
    retried (R3a).
    """

    __tablename__ = "processed_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # 'other' (not a booking/cancellation) | 'parse_error' (booking that wouldn't parse)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    classified_as: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Original From header and subject. Populated for 'other' rows since
    # migration 0007 (older rows are NULL): the weekly classifier-drift digest
    # reviews platform-domain OTHER dead-letters, which needs both.
    sender: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
