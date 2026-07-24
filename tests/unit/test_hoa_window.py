"""HOA window calculator tests (HOA-01, revised per bug hunt 2026-07-22 F8).

Requirements covered:
  HOA-01: Window calculator returns (earliest, latest) valid send dates, accounting
          for HOA closed Sundays and the 7-day/2-open-day-lead-time constraints.

Algorithm under test (owner adjudication 2026-07-22 — CONTEXT.md is the source
of truth: "Sunday check-in → email must be sent by Thursday at latest"):
  earliest = check_in_date - 7 calendar days
  latest   = the most recent day that still leaves days_min (2) full HOA-open
             days STRICTLY BETWEEN the send day and check-in. The send day
             itself never counts as lead time (it may even be a closed day —
             the email waits in the HOA inbox).

`latest` is the last *acceptable* day for the scheduled send. It is NOT a
send-blocker: a form signed after `latest` is still sent immediately (late is
better than never) — that behavior is covered in test_hoa_email.py.

HOA open_days = [1, 2, 3, 4, 5, 6] (Mon=1 through Sat=6; Sunday=7 is always closed).
"""
from __future__ import annotations

from datetime import date

# Standard HOA open days (Mon-Sat)
OPEN_DAYS = [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# hoa_window — date boundary tests
# ---------------------------------------------------------------------------

def test_hoa_window_earliest_is_check_in_minus_7_days():
    """Wed 2026-08-05 → earliest 2026-07-29 (exactly 7 calendar days before)."""
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 8, 5)  # Wednesday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert earliest == date(2026, 7, 29)


def test_hoa_window_monday_checkin_latest_is_thursday():
    """Mon 2026-08-03 check-in → latest is Thu 2026-07-30.

    Open days strictly between Thu and Mon: Fri, Sat = 2 (Sun closed). Sending
    Friday would leave only Saturday — 1 open day of lead time — too late.
    """
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 8, 3)  # Monday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert latest == date(2026, 7, 30)  # Thursday


def test_hoa_window_tuesday_checkin_latest_is_friday():
    """Tue 2026-08-04 check-in → latest is Fri 2026-07-31.

    Open days strictly between Fri and Tue: Sat, Mon = 2 (Sun closed).
    """
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 8, 4)  # Tuesday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert latest == date(2026, 7, 31)  # Friday


def test_hoa_window_wednesday_checkin_latest_is_sunday():
    """Wed 2026-08-05 check-in → latest is Sun 2026-08-02.

    The send day may itself be closed: an email sent Sunday sits in the HOA
    inbox and still leaves Mon, Tue = 2 full open days before check-in.
    """
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 8, 5)  # Wednesday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert latest == date(2026, 8, 2)  # Sunday


def test_hoa_window_saturday_checkin_latest_is_wednesday():
    """Sat 2026-08-08 check-in → latest is Wed 2026-08-05.

    Open days strictly between Wed and Sat: Thu, Fri = 2.
    """
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 8, 8)  # Saturday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert latest == date(2026, 8, 5)  # Wednesday


def test_hoa_window_sunday_checkin_canonical_context_md_example():
    """Sun 2026-08-02 check-in → earliest 2026-07-26, latest Thu 2026-07-30.

    This is CONTEXT.md's documented example verbatim: "Sunday check-in → last
    open day is Saturday → email must be sent by Thursday at latest." The code
    previously said Friday (2 open days counted back from check-in itself),
    which left the HOA only Saturday to prepare the packet (bug hunt F8).
    """
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 8, 2)  # Sunday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert earliest == date(2026, 7, 26)
    assert latest == date(2026, 7, 30)  # Thursday


def test_hoa_window_returns_inclusive_dates():
    """Returned values are date objects and earliest <= latest."""
    from app.integrations.hoa.window import hoa_window

    check_in = date(2026, 9, 10)  # Thursday
    earliest, latest = hoa_window(check_in, open_days=OPEN_DAYS, days_min=2, days_max=7)
    assert isinstance(earliest, date)
    assert isinstance(latest, date)
    assert earliest <= latest


# ---------------------------------------------------------------------------
# today_et — the one date the window is ever compared against (F2)
# ---------------------------------------------------------------------------

def test_today_et_is_the_eastern_date_not_server_date():
    """today_et() must be the US/Eastern calendar date. The production container
    runs UTC, where date.today() is already 'tomorrow' from 8 PM ET to midnight
    — every window comparison must go through this helper (bug hunt F2)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.integrations.hoa.window import today_et

    expected = datetime.now(ZoneInfo("America/New_York")).date()
    assert today_et() == expected
