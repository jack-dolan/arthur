"""One-time backfill: record the door code Seam actually set for existing bookings.

New bookings record the code from the Seam create response as an access_code
DataPoint. Bookings created before that change have only the access_code_id
(task.external_ref) — this script fetches each of those codes from the LIVE
Seam API (read-only get) and records the returned value, so the dashboard
shows what is really on the lock, never a re-derivation of the phone number.

Idempotent: bookings that already have an access_code DataPoint are skipped.
Codes Seam no longer knows (e.g. deleted after a cancellation) are reported
and skipped. Run inside the app container:

    docker compose exec app python scripts/manual/backfill_access_codes.py --dry-run
    docker compose exec app python scripts/manual/backfill_access_codes.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make `app` importable when run as a plain script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import Booking, DataPoint, DataPointSource, TaskState, TaskType
from app.db.session import AsyncSessionLocal
from app.integrations.seam.client import get_seam_client


async def run(dry_run: bool) -> None:
    client = get_seam_client()
    recorded = skipped_existing = failed = 0

    async with AsyncSessionLocal() as session:
        bookings = (
            (
                await session.execute(
                    select(Booking).options(
                        selectinload(Booking.tasks), selectinload(Booking.data_points)
                    )
                )
            )
            .scalars()
            .all()
        )

        for booking in bookings:
            task = next(
                (
                    t
                    for t in booking.tasks
                    if t.task_type == TaskType.ACCESS_CODE_CREATE
                    and t.state == TaskState.COMPLETE
                    and t.external_ref
                ),
                None,
            )
            if task is None:
                continue
            if any(dp.field_name == "access_code" for dp in booking.data_points):
                skipped_existing += 1
                continue

            try:
                code_obj = await asyncio.to_thread(
                    client.access_codes.get, access_code_id=task.external_ref
                )
            except Exception as exc:
                failed += 1
                print(f"  {booking.external_id}: Seam get failed for {task.external_ref}: {exc}")
                continue

            code = getattr(code_obj, "code", None)
            if not code:
                failed += 1
                print(f"  {booking.external_id}: Seam returned no code for {task.external_ref}")
                continue

            session.add(
                DataPoint(
                    booking_id=booking.id,
                    field_name="access_code",
                    value=str(code),
                    source=DataPointSource.SEAM_API,
                    notes=f"Backfilled from Seam (access_code_id {task.external_ref})",
                )
            )
            recorded += 1
            print(f"  {booking.external_id}: access_code {code} recorded (from Seam)")

        print(
            f"\nSummary: {recorded} recorded, {skipped_existing} already had a code, "
            f"{failed} unavailable in Seam"
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
        help="Report what would change (still calls Seam read-only), then roll back.",
    )
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
