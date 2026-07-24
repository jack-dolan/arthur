"""Async task handler for adding a booking row to the cleaner schedule spreadsheet.

This module provides ``handle_cleaner_sheet(booking, task, session)``, which
inserts a new row into the Google Sheets cleaner schedule in correct
chronological position, just after the last existing booking above the sentinel.

Column layout written (A–F):
  A  Cleaner checkbox — explicitly written as False (unchecked) so it can
     never accidentally inherit True from the row above
  B  Guest name (First Last)
  C  Check-in date  (M/D/YYYY)
  D  Check-out date (M/D/YYYY)
  E  Time In        "4:00:00 PM"
  F  Time Out       "11:00:00 AM"

Blocking Google Sheets SDK calls are wrapped in ``asyncio.to_thread`` to avoid
blocking the async event loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_config
from app.db.models import Booking, BookingTask, TaskState
from app.integrations.sheets.client import (
    SENTINEL_PATTERN,
    _find_insertion_index,
    _find_sheet_id,
    get_sheets_service,
)

log = logging.getLogger(__name__)

# F16 (bug hunt 2026-07-22): sheet mutations are read-index -> insert -> fill,
# three separate API calls. Two bookings dispatched near-simultaneously could
# both compute the same insertion index; the second insert then shifts the
# first's blank row and a fill lands on the wrong row. All mutations in this
# process serialize on one lock. Created lazily per event loop (asyncio
# primitives bind to the loop that first uses them; tests run one loop per
# test) — production has a single long-lived loop, so this is one lock.
_sheet_lock: asyncio.Lock | None = None
_sheet_lock_loop: object | None = None


def _get_sheet_lock() -> asyncio.Lock:
    global _sheet_lock, _sheet_lock_loop
    loop = asyncio.get_running_loop()
    if _sheet_lock is None or _sheet_lock_loop is not loop:
        _sheet_lock = asyncio.Lock()
        _sheet_lock_loop = loop
    return _sheet_lock


def _find_sentinel_index(rows: list[list[str]], pattern: str = SENTINEL_PATTERN) -> int:
    """Return the 0-based row index of the sentinel row.

    Scans every cell in each row so the sentinel is found regardless of which
    column it occupies (e.g. a merged cell starting at column B has an empty
    column A and the text in column B).  Match is case-insensitive after strip().
    Raises ValueError if not found.
    """
    pattern_lower = pattern.strip().lower()
    for i, row in enumerate(rows):
        if any(cell.strip().lower() == pattern_lower for cell in row):
            return i
    raise ValueError(f"Sentinel row '{pattern}' not found in sheet")


def _fmt_date(d: date) -> str:
    """Format a date as M/D/YYYY (no zero-padding) to match the sheet's style."""
    return f"{d.month}/{d.day}/{d.year}"


def _build_row(booking: Booking) -> list:
    """Build the 6-column row for columns A–F of the cleaner schedule.

    Column A is explicitly set to False (unchecked checkbox) so it is never
    accidentally inherited as True from the row above.
    """
    return [
        False,  # col A: cleaner checkbox — always start unchecked
        f"{booking.guest_first_name} {booking.guest_last_name}",
        _fmt_date(booking.check_in_date),
        _fmt_date(booking.check_out_date),
        "4:00:00 PM",
        "11:00:00 AM",
    ]


async def handle_cleaner_sheet(
    booking: Booking,
    task: BookingTask,
    session: AsyncSession,
) -> None:
    """Insert a cleaner schedule row for *booking* in chronological order.

    Steps:
    1. Resolve property config (raises ValueError if property_id unknown).
    2. Build an authenticated Sheets service.
    3. Fetch the spreadsheet metadata to get the sheet's numeric sheetId.
    4. Fetch the current values in the schedule range.
    5. Locate the sentinel row and compute the insertion index.
    6. Insert a blank row at that index via insertDimension.
    7. Write the booking data into the new row via values().update.
    8. Set task state to COMPLETE.

    All SDK ``.execute()`` calls are wrapped in ``asyncio.to_thread``.

    Raises:
        ValueError: if property_id not found in config, or sentinel row missing.
    """
    task.state = TaskState.IN_PROGRESS

    # --- 1. Resolve config for this property ---
    config = load_config()
    try:
        prop = next(p for p in config.properties if p.id == booking.property_id)
    except StopIteration:
        raise ValueError(f"Property '{booking.property_id}' not found in config")

    spreadsheet_id = prop.cleaner_schedule.spreadsheet_id
    sheet_name = prop.cleaner_schedule.sheet_name

    # Serialize all sheet mutations in this process (F16): index reads and
    # row inserts from concurrent dispatches must not interleave.
    async with _get_sheet_lock():
        # --- 2. Build Sheets service ---
        service = get_sheets_service()

        # --- 3. Fetch spreadsheet metadata (sheetId) ---
        spreadsheet = await asyncio.to_thread(
            service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute
        )
        sheet_id = _find_sheet_id(spreadsheet, sheet_name)

        # --- 4. Fetch current schedule rows (A:F to cover all app-written columns) ---
        result = await asyncio.to_thread(
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A:F")
            .execute
        )
        rows: list[list[str]] = result.get("values", [])

        # --- 5. Compute insertion index ---
        sentinel_index = _find_sentinel_index(rows, prop.cleaner_schedule.sentinel_pattern)
        insert_idx = _find_insertion_index(rows, booking.check_in_date, sentinel_index)

        # --- 6. Insert blank row via insertDimension ---
        batch_body = {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": insert_idx,
                            "endIndex": insert_idx + 1,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        }
        await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=batch_body)
            .execute
        )

        # --- 7. Write booking data into columns A–F of the new row ---
        # Sheets API row numbers are 1-based; insert_idx is 0-based → add 1.
        # Column A (checkbox) is explicitly written as False so it is never
        # accidentally inherited as True from the row above.
        #
        # Step 17 (partial failure): the insert (step 6) and this fill are two separate
        # Sheets calls. If the fill fails, we must NOT leave the just-inserted blank row
        # behind — otherwise the task goes FAILED and a retry inserts a SECOND blank row,
        # accumulating orphans in the real cleaner schedule. Roll the blank row back on
        # failure so a retry starts from a clean sheet, then re-raise for the dispatcher.
        a1_range = f"{sheet_name}!A{insert_idx + 1}:F{insert_idx + 1}"
        row = _build_row(booking)
        try:
            await asyncio.to_thread(
                service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=a1_range,
                    valueInputOption="RAW",
                    body={"values": [row]},
                )
                .execute
            )
        except Exception:
            log.error(
                "cleaner sheet: fill failed for booking %s; rolling back the blank row "
                "inserted at index %d",
                booking.id,
                insert_idx,
            )
            rollback_body = {
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": insert_idx,
                                "endIndex": insert_idx + 1,
                            }
                        }
                    }
                ]
            }
            try:
                await asyncio.to_thread(
                    service.spreadsheets()
                    .batchUpdate(spreadsheetId=spreadsheet_id, body=rollback_body)
                    .execute
                )
            except Exception:
                log.exception(
                    "cleaner sheet: ROLLBACK of the blank row for booking %s at index %d "
                    "also failed — the sheet may contain an orphan blank row needing "
                    "manual cleanup",
                    booking.id,
                    insert_idx,
                )
            raise

    # --- 8. Mark complete ---
    task.state = TaskState.COMPLETE
    task.completed_at = datetime.now(UTC)
    log.info(
        "Inserted cleaner-sheet row for booking %s at index %d",
        booking.id,
        insert_idx,
    )
