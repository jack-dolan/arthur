"""Google Sheets integration client for the cleaner schedule spreadsheet.

Sheet column layout (A–F):
  A  cleaner checkbox (filled by cleaners only — app never writes here)
  B  Guest name (First Last)
  C  Check-in date  (M/D/YYYY)
  D  Check-out date (M/D/YYYY)
  E  Time In        (h:mm:ss AM/PM)
  F  Time Out       (h:mm:ss AM/PM)
  G–K  cleaner-use columns — app never touches these

The app writes columns B–F only, leaving A blank for the cleaners.

This module provides:
  - ``get_sheets_service()`` — authenticated Google Sheets API v4 client.
  - ``_find_insertion_index(rows, check_in_date, sentinel_index)`` — pure,
    I/O-free function that returns the 0-based row index at which a new booking
    row should be inserted to keep the schedule chronological.
  - ``_find_sheet_id(spreadsheet_dict, sheet_name)`` — extracts the numeric
    ``sheetId`` from a spreadsheets.get() response.

Sentinel convention
-------------------
The sheet contains exactly one sentinel row (any cell matching the configured
``sentinel_pattern``, case-insensitive).  New rows are always inserted above it,
specifically just after the last row with a parseable check-in date in column C.
"""
from __future__ import annotations

import logging
from datetime import date

import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.settings import settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SENTINEL_PATTERN = "--- end ---"

# httplib2's default timeout is None — a hung Sheets socket would wedge its
# worker thread forever (bug hunt F11).
HTTP_TIMEOUT_SECONDS = 30


def get_sheets_service():
    """Return an authenticated Google Sheets API v4 client."""
    creds = Credentials(
        token=None,
        refresh_token=settings.google_sheets_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    authed_http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS)
    )
    return build("sheets", "v4", http=authed_http)


def _parse_date(value: str) -> date | None:
    """Parse a date in ISO (YYYY-MM-DD) or US (M/D/YYYY) format, or return None."""
    v = value.strip()
    try:
        return date.fromisoformat(v)
    except (ValueError, AttributeError):
        pass
    try:
        parts = v.split("/")
        if len(parts) == 3:
            return date(int(parts[2]), int(parts[0]), int(parts[1]))
    except (ValueError, TypeError):
        pass
    return None


def _find_insertion_index(
    rows: list[list[str]],
    check_in_date: date,
    sentinel_index: int,
) -> int:
    """Return the 0-based row index at which to insert a new booking row.

    Scans rows before the sentinel (rows[0:sentinel_index]) backwards and
    finds the correct chronological position for ``check_in_date`` using the
    check-in date in column C (index 2).  Rows without a parseable date in
    column C are skipped.  For duplicate dates the new row is inserted after
    the last existing row with the same date (stable order).

    If no existing rows have a parseable date (empty sheet), returns
    ``sentinel_index`` so the new row lands just above the sentinel rather than
    at the very top of the sheet.
    """
    insert_at = sentinel_index  # default: just above the sentinel
    any_parseable = False

    for i in range(sentinel_index - 1, -1, -1):
        row = rows[i]
        if len(row) < 3:
            continue
        existing_date = _parse_date(row[2])
        if existing_date is None:
            continue
        any_parseable = True
        if existing_date <= check_in_date:
            insert_at = i + 1
            break
    else:
        if any_parseable:
            # All parseable dates are later → insert before the first data row.
            insert_at = _find_first_data_row(rows, sentinel_index)
        # No parseable dates → insert_at stays at sentinel_index.

    return insert_at


def _find_first_data_row(rows: list[list[str]], sentinel_index: int) -> int:
    """Return the index of the first row with a parseable date in column C (index 2)."""
    for i in range(sentinel_index):
        row = rows[i]
        if len(row) >= 3 and _parse_date(row[2]) is not None:
            return i
    return sentinel_index


def _find_sheet_id(spreadsheet_dict: dict, sheet_name: str) -> int:
    """Return the numeric sheetId for *sheet_name* from a spreadsheets.get() dict.

    Raises:
        ValueError: if no sheet with the given title is found.
    """
    for sheet in spreadsheet_dict.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == sheet_name:
            return props["sheetId"]
    raise ValueError(f"Sheet '{sheet_name}' not found in spreadsheet")
