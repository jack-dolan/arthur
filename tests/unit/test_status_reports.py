"""Unit tests for the drift-digest and monthly-status alert builders
(sustainability audit 2026-07-23, items 3 & 4). Builders are pure —
no mocking needed."""
from __future__ import annotations


def test_build_classifier_drift_digest_content():
    from app.ingestion.alerts import build_classifier_drift_digest

    items = [
        {
            "date": "2026-07-20",
            "sender": "automated@airbnb.com",
            "subject": "Your reservation is confirmed - Jane Doe",
        },
        {
            "date": "2026-07-22",
            "sender": "no-reply@vrbo.com",
            "subject": "Booking update",
        },
    ]
    subject, body = build_classifier_drift_digest(
        items=items, dashboard_base_url="https://arthur.example"
    )
    assert "2" in subject
    assert "automated@airbnb.com" in body
    assert "Your reservation is confirmed - Jane Doe" in body
    assert "no-reply@vrbo.com" in body
    # The digest must tell the owner what to actually do with a hit.
    assert "manually" in body.lower() or "by hand" in body.lower()


def test_build_monthly_status_report_content():
    from app.ingestion.alerts import build_monthly_status_report

    stats = {
        "active_bookings": 9,
        "completed_last_30d": 3,
        "failed_tasks": 0,
        "stuck_in_progress": 0,
        "dead_letters_other_30d": 12,
        "dead_letters_error_30d": 1,
        "docusign_token_store_age_days": 4,
    }
    subject, body = build_monthly_status_report(
        stats=stats, dashboard_base_url="https://arthur.example"
    )
    assert "status" in subject.lower()
    assert "9" in body  # active bookings
    assert "12" in body  # other dead letters
    # The email's absence is the alarm — the body must say so.
    assert "absence" in body.lower() or "stops arriving" in body.lower()


def test_build_monthly_status_report_handles_missing_token_store():
    from app.ingestion.alerts import build_monthly_status_report

    stats = {
        "active_bookings": 0,
        "completed_last_30d": 0,
        "failed_tasks": 2,
        "stuck_in_progress": 1,
        "dead_letters_other_30d": 0,
        "dead_letters_error_30d": 0,
        "docusign_token_store_age_days": None,
    }
    subject, body = build_monthly_status_report(
        stats=stats, dashboard_base_url="https://arthur.example"
    )
    # Must render without raising and still surface the attention items.
    assert "2" in body and "1" in body
