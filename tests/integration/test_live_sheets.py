"""Google Sheets isolation test — offline (recorded) by default, live with --run-live.

Consolidates the Phase 2 manual script ``scripts/manual/test_sheets.py`` into
the pytest suite. One shared round-trip (read → insert chronologically →
verify → delete) runs against:

  * a stateful recorded fake by default (no credentials), and
  * the real configured spreadsheet under ``--run-live`` / ``RUN_LIVE=1``.

The live test writes a clearly-labelled far-future row so it lands just above
the sentinel, verifies it, then deletes it (search-by-marker teardown, safe to
re-run and safe on assertion failure).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.integrations.sheets.client import (
    _find_insertion_index,
    _find_sheet_id,
    get_sheets_service,
)
from app.tasks.handlers.cleaner_sheet import _find_sentinel_index
from tests.integration.fakes import FakeSheetsService, load_recorded

# Far-future so it always lands just above the sentinel, clearly labelled.
TEST_GUEST = "ZZ TEST — delete me"
TEST_ROW = ["FALSE", TEST_GUEST, "12/28/2099", "12/31/2099", "4:00:00 PM", "11:00:00 AM"]
TEST_CHECKIN = date(2099, 12, 28)


def _read_rows(service, spreadsheet_id, sheet_name):
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A:F")
        .execute()
        .get("values", [])
    )


def _index_of_guest(rows, guest):
    for i, row in enumerate(rows):
        if len(row) > 1 and row[1] == guest:
            return i
    return None


def _delete_row(service, spreadsheet_id, sheet_id, row_index):
    body = {
        "requests": [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_index,
                        "endIndex": row_index + 1,
                    }
                }
            }
        ]
    }
    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


def _sheets_roundtrip(service, spreadsheet_id, sheet_name, sentinel):
    """Read → insert chronologically → verify → delete. Asserts throughout."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id = _find_sheet_id(meta, sheet_name)

    rows_before = _read_rows(service, spreadsheet_id, sheet_name)
    sentinel_index = _find_sentinel_index(rows_before, sentinel)
    assert sentinel_index > 0, "sheet should have data rows above the sentinel"
    assert _index_of_guest(rows_before, TEST_GUEST) is None, "stale test row present"

    insert_idx = _find_insertion_index(rows_before, TEST_CHECKIN, sentinel_index)
    assert insert_idx == sentinel_index, "far-future row should land just above the sentinel"

    # Insert a blank row, then write the booking data into it (mirrors the handler).
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
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
        },
    ).execute()

    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A{insert_idx + 1}:F{insert_idx + 1}",
            valueInputOption="RAW",
            body={"values": [TEST_ROW]},
        ).execute()

        rows_after = _read_rows(service, spreadsheet_id, sheet_name)
        landed = rows_after[insert_idx]
        assert landed[1] == TEST_GUEST
        assert landed[2] == "12/28/2099"
        # Still above the sentinel, and chronologically after the last real row.
        new_sentinel_index = _find_sentinel_index(rows_after, sentinel)
        assert insert_idx < new_sentinel_index
    finally:
        # Search-by-marker teardown: safe on assertion failure and on re-run.
        rows_now = _read_rows(service, spreadsheet_id, sheet_name)
        target = _index_of_guest(rows_now, TEST_GUEST)
        if target is not None:
            _delete_row(service, spreadsheet_id, sheet_id, target)

    rows_final = _read_rows(service, spreadsheet_id, sheet_name)
    assert _index_of_guest(rows_final, TEST_GUEST) is None, "cleanup failed — test row remains"


def test_sheets_offline_roundtrip():
    """Offline: full round-trip against the recorded stateful fake (no creds)."""
    rec = load_recorded("sheets_schedule.json")
    service = FakeSheetsService(rec)
    _sheets_roundtrip(service, "fake-spreadsheet-id", rec["sheet_name"], rec["sentinel_pattern"])


@pytest.mark.live
def test_sheets_live_roundtrip():
    """Live: same round-trip against the real configured spreadsheet."""
    from app.config import load_config

    prop = load_config().properties[0]
    service = get_sheets_service()
    _sheets_roundtrip(
        service,
        prop.cleaner_schedule.spreadsheet_id,
        prop.cleaner_schedule.sheet_name,
        prop.cleaner_schedule.sentinel_pattern,
    )
