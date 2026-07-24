"""One-time backfill: reconcile task rows left stale by the pre-fix display bugs.

Before the R7 / stale-reminder fixes, three kinds of task rows sat PENDING
forever even though their real-world outcome was settled:

  1. OWNER_ALERT_NEW_BOOKING — the alert email really was sent at ingestion,
     but the row was never flipped COMPLETE. (For bookings loaded by the CSV
     importer no alert was ever sent, so those flip to SKIPPED instead —
     identified by their "csv-import:" source_email_message_id prefix.)
  2. Reminders whose condition has since resolved (email entered, form signed)
     — flip to SKIPPED, same as the daily job now does.
  3. Reminders on past-check-in or cancelled bookings — can never fire; SKIPPED.

Idempotent: rows already COMPLETE/SKIPPED are never touched, so re-running is a
no-op. Run inside the app container:

    docker compose exec app python scripts/manual/backfill_task_states.py --dry-run
    docker compose exec app python scripts/manual/backfill_task_states.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Make `app` importable when run as a plain script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import Booking, TaskState, TaskType
from app.db.session import AsyncSessionLocal
from app.tasks.scheduled import _stale_reminders, sweep_moot_reminders

CSV_IMPORT_PREFIX = "csv-import:"


async def run(dry_run: bool) -> None:
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    now = datetime.now(UTC)

    async with AsyncSessionLocal() as session:
        bookings = (
            (
                await session.execute(
                    select(Booking).options(selectinload(Booking.tasks))
                )
            )
            .scalars()
            .all()
        )

        alert_completed = alert_skipped = stale_skipped = 0

        for booking in bookings:
            tasks_by_type = {t.task_type: t for t in booking.tasks}

            # 1. New-booking alert rows stuck PENDING (R7).
            alert = tasks_by_type.get(TaskType.OWNER_ALERT_NEW_BOOKING)
            if alert is not None and alert.state == TaskState.PENDING:
                if booking.source_email_message_id.startswith(CSV_IMPORT_PREFIX):
                    # Importer deliberately sent no alert — "not needed".
                    alert.state = TaskState.SKIPPED
                    alert_skipped += 1
                    verb = "SKIPPED (csv import, alert never sent)"
                else:
                    alert.state = TaskState.COMPLETE
                    alert.completed_at = now
                    alert_completed += 1
                    verb = "COMPLETE (alert was sent at ingestion)"
                print(f"  {booking.external_id}: new-booking alert -> {verb}")

            # 2. Resolved-condition reminders (same predicate the daily job uses).
            for task in _stale_reminders(booking, tasks_by_type):
                task.state = TaskState.SKIPPED
                stale_skipped += 1
                print(
                    f"  {booking.external_id}: {task.task_type.value} -> "
                    "SKIPPED (condition resolved)"
                )

        # 3. Past-check-in / cancelled bookings (bulk, shared with the daily job).
        moot_skipped = await sweep_moot_reminders(session, today_et)

        print(
            f"\nSummary: {alert_completed} alert(s) -> COMPLETE, "
            f"{alert_skipped} alert(s) -> SKIPPED, "
            f"{stale_skipped} resolved reminder(s) -> SKIPPED, "
            f"{moot_skipped} moot reminder(s) -> SKIPPED"
        )

        if dry_run:
            await session.rollback()
            print("DRY RUN — rolled back, nothing changed.")
        else:
            await session.commit()
            print("Committed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change, then roll back.",
    )
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
