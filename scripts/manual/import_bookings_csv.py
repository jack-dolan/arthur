#!/usr/bin/env python3
"""One-time importer for bookings made BEFORE the system went live (so they were
never captured by the booking-feed poller). Reads a CSV and creates each row as
a normal Booking + task checklist — identical to a poller-ingested booking — so
they show up in the dashboard and can be worked the usual way (enter contact
info, then dispatch).

CSV columns (header row required):
    platform,external_id,guest_first_name,guest_last_name,check_in_date,check_out_date,guest_email,guest_phone

  - platform: "airbnb" or "vrbo"
  - external_id: the reservation/confirmation code (e.g. HM… for Airbnb, HA-… for VRBO)
  - dates: YYYY-MM-DD (also accepts M/D/YYYY or M/D/YY)
  - guest_email / guest_phone: OPTIONAL — leave blank to fill in later via the dashboard.
    Providing them starts the matching task PENDING (ready to dispatch) instead of WAITING.

Behavior:
  - Skips any row whose (platform, external_id) already exists — safe to re-run.
  - Does NOT send the per-booking alert email (would flood the alerts inbox).
  - Does NOT auto-dispatch: nothing is sent until you open the booking in the
    dashboard and save (same control as a normal booking).

Usage (run where .env/config are available — e.g. inside the app container):
    python import_bookings_csv.py bookings.csv            # import
    python import_bookings_csv.py bookings.csv --dry-run   # preview only, no writes
"""
from __future__ import annotations

import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select

from app.db.models import Booking, BookingStatus, Platform
from app.db.session import AsyncSessionLocal
from app.ingestion.poller import _initial_tasks

REQUIRED = ["platform", "external_id", "guest_first_name", "guest_last_name",
            "check_in_date", "check_out_date"]
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]


@dataclass
class Row:
    platform: Platform
    external_id: str
    first: str
    last: str
    check_in: date
    check_out: date
    email: str | None
    phone: str | None


def _parse_date(s: str) -> date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {s!r} (use YYYY-MM-DD)")


def parse_row(raw: dict, lineno: int) -> Row:
    missing = [c for c in REQUIRED if not (raw.get(c) or "").strip()]
    if missing:
        raise ValueError(f"line {lineno}: missing required column(s): {', '.join(missing)}")
    plat = (raw["platform"] or "").strip().lower()
    if plat not in ("airbnb", "vrbo"):
        raise ValueError(f"line {lineno}: platform must be 'airbnb' or 'vrbo', got {plat!r}")
    ci = _parse_date(raw["check_in_date"])
    co = _parse_date(raw["check_out_date"])
    if co <= ci:
        raise ValueError(f"line {lineno}: check_out_date must be after check_in_date")
    email = (raw.get("guest_email") or "").strip() or None
    phone = (raw.get("guest_phone") or "").strip() or None
    return Row(
        platform=Platform.AIRBNB if plat == "airbnb" else Platform.VRBO,
        external_id=raw["external_id"].strip(),
        first=raw["guest_first_name"].strip(),
        last=raw["guest_last_name"].strip(),
        check_in=ci, check_out=co, email=email, phone=phone,
    )


async def _import(rows: list[Row], dry_run: bool) -> None:
    from app.config import load_config
    try:
        property_id = load_config().properties[0].id
    except Exception:
        property_id = "property_1"

    created = skipped = 0
    async with AsyncSessionLocal() as s:
        for r in rows:
            exists = (await s.execute(
                select(Booking).where(
                    Booking.platform == r.platform, Booking.external_id == r.external_id
                )
            )).scalar_one_or_none()
            if exists is not None:
                print(
                    f"  SKIP  {r.platform.value:6s} {r.external_id:12s} "
                    f"{r.first} {r.last} — already in system"
                )
                skipped += 1
                continue
            contact = []
            if r.email:
                contact.append("email")
            if r.phone:
                contact.append("phone")
            tag = (
                ("[" + "+".join(contact) + " → ready to dispatch]")
                if contact
                else "[no contact → WAITING]"
            )
            print(f"  {'WOULD ADD' if dry_run else 'ADD  '}  "
                  f"{r.platform.value:6s} {r.external_id:12s} "
                  f"{r.first} {r.last}  {r.check_in}→{r.check_out}  {tag}")
            if dry_run:
                created += 1
                continue
            booking = Booking(
                platform=r.platform, external_id=r.external_id, property_id=property_id,
                guest_first_name=r.first, guest_last_name=r.last,
                guest_phone=r.phone, guest_email=r.email,
                check_in_date=r.check_in, check_out_date=r.check_out,
                status=BookingStatus.ACTIVE,
                source_email_message_id=f"csv-import:{r.platform.value}:{r.external_id}",
            )
            booking.tasks.extend(
                _initial_tasks(r.platform, has_phone=bool(r.phone), has_email=bool(r.email))
            )
            s.add(booking)
            created += 1
        if not dry_run:
            await s.commit()

    verb = "would be created" if dry_run else "created"
    print(f"\n{created} {verb}, {skipped} skipped (already present).")
    if dry_run:
        print("Dry run — nothing written. Re-run without --dry-run to import.")


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    if len(args) != 1:
        print("usage: import_bookings_csv.py <file.csv> [--dry-run]")
        return 1
    path = args[0]
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            print(f"CSV is missing required column(s): {', '.join(missing)}")
            print(f"Found columns: {header}")
            return 1
        rows, errors = [], []
        for i, raw in enumerate(reader, start=2):  # line 1 is the header
            try:
                rows.append(parse_row(raw, i))
            except ValueError as e:
                errors.append(str(e))
    if errors:
        print("Row errors — fix these and re-run (nothing imported):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"Parsed {len(rows)} row(s) from {path}:\n")
    asyncio.run(_import(rows, dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
