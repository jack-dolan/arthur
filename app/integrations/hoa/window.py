"""HOA window calculator — pure date math, plus the one canonical "today".

Domain rule (CONTEXT.md, owner-adjudicated 2026-07-22 / bug hunt F8):
    earliest = check_in_date - days_max calendar days (default: 7)
    latest   = the most recent day that still leaves `days_min` (default: 2)
               full HOA-open days STRICTLY BETWEEN the send day and check-in.
               The send day itself never counts as lead time, and may itself be
               a closed day (the email just waits in the HOA inbox).

Canonical example (CONTEXT.md): Sunday check-in → the HOA's last open day is
Saturday → the email must be sent by **Thursday** at latest (Friday + Saturday
are the two full open days of lead time).

`latest` is the last *acceptable* day for the scheduled send — it is NOT a
send-blocker. A form signed after `latest` is still sent immediately (late is
better than never); see app/tasks/handlers/hoa.py.

7-day matrix (days_min=2, open_days=Mon–Sat):
    Monday    → latest = Thursday  (between: Fri, Sat; Sun closed)
    Tuesday   → latest = Friday    (between: Sat, Mon)
    Wednesday → latest = Sunday    (between: Mon, Tue — Sunday send is fine)
    Thursday  → latest = Monday    (between: Tue, Wed)
    Friday    → latest = Tuesday   (between: Wed, Thu)
    Saturday  → latest = Wednesday (between: Thu, Fri)
    Sunday    → latest = Thursday  (between: Fri, Sat)

HOA open_days convention: [1, 2, 3, 4, 5, 6] means Mon–Sat; isoweekday=7
(Sunday) is always closed and must not be in open_days.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def today_et() -> date:
    """The US/Eastern calendar date — the ONLY "today" window logic may use.

    The production container runs UTC, where ``date.today()`` is already
    tomorrow from 8 PM ET until midnight (bug hunt F2). Comparing the HOA
    window against the server date opened the window a day early at one edge
    and closed it a day early at the other; every caller must go through this
    helper instead.
    """
    return datetime.now(ET).date()


def _latest_send_day(check_in: date, days_min: int, open_days_set: set[int]) -> date:
    """Most recent day leaving >= *days_min* open days strictly before check-in.

    Walks backwards from check-in; a day qualifies once the days after it (and
    before check-in) include *days_min* open days. The qualifying day itself is
    never counted as lead time and need not be open.
    """
    count = 0
    candidate = check_in
    while True:
        candidate -= timedelta(days=1)
        if count >= days_min:
            return candidate
        if candidate.isoweekday() in open_days_set:
            count += 1


def hoa_window(
    check_in_date: date,
    open_days: list[int],
    days_min: int = 2,
    days_max: int = 7,
) -> tuple[date, date]:
    """Return (earliest, latest) valid HOA send dates, inclusive.

    Parameters
    ----------
    check_in_date:
        Guest check-in date.
    open_days:
        List of isoweekday integers on which the HOA office is open.
        Typically [1, 2, 3, 4, 5, 6] (Mon–Sat); Sunday (7) is always closed.
    days_min:
        Number of full HOA-open days that must remain strictly between the
        send day and check-in (the HOA's packet lead time). Defaults to 2.
    days_max:
        Maximum number of calendar days before check-in that the HOA allows
        advance notification.  Defaults to 7.

    Returns
    -------
    (earliest, latest) as ``date`` objects, both inclusive. ``latest`` is the
    last acceptable day for the *scheduled* send; a late signature still sends
    immediately (the deadline is not a blocker).
    """
    earliest = check_in_date - timedelta(days=days_max)
    latest = _latest_send_day(check_in_date, days_min, set(open_days))
    return earliest, latest
