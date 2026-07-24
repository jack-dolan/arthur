"""Stateful fakes that replay recorded external-API responses offline.

Step 18: the per-integration "live isolation" tests (``test_live_sheets.py``,
``test_live_seam.py``, ``test_live_docusign.py``, ``test_live_gmail.py``) run in
two modes:

  * **offline (default)** — the test drives one of the fakes below, which is
    seeded from a hand-written recording under ``tests/fixtures/recorded/`` and
    keeps enough in-memory state to satisfy a full round-trip
    (read → create → verify → delete). No credentials, no network.
  * **live (`--run-live` / `RUN_LIVE=1`)** — the test drives the *real* client
    against the sandbox instead.

The fakes deliberately mimic the exact call chains the production code uses
(e.g. ``service.spreadsheets().values().get(...).execute()``), so the offline
path exercises the real orchestration/parsing code — only the transport is
substituted. This is the hand-written-fixture equivalent of a VCR cassette.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

RECORDED_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "recorded"


def load_recorded(name: str) -> dict:
    """Load a recorded-response JSON fixture by filename."""
    return json.loads((RECORDED_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


class _Request:
    """A googleapiclient-style request object whose .execute() returns a value."""

    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeValues:
    def __init__(self, service: "FakeSheetsService"):
        self._s = service

    def get(self, spreadsheetId=None, range=None):  # noqa: N803 (match SDK kwarg)
        return _Request(lambda: {"values": [list(r) for r in self._s.rows]})

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None):  # noqa: N803
        row_index = self._s._row_index_from_a1(range)
        values = body["values"][0]

        def _do():
            # Grow the sheet if needed, then write the row in place.
            while len(self._s.rows) <= row_index:
                self._s.rows.append([])
            self._s.rows[row_index] = [str(v) for v in values]
            return {"updatedRows": 1, "updatedRange": range}

        return _Request(_do)


class _FakeSpreadsheets:
    def __init__(self, service: "FakeSheetsService"):
        self._s = service

    def get(self, spreadsheetId=None, fields=None):  # noqa: N803
        def _meta():
            return {
                "spreadsheetId": spreadsheetId,
                "properties": {"title": self._s.title},
                "sheets": [
                    {"properties": {"title": self._s.sheet_name, "sheetId": self._s.sheet_id}}
                ],
            }

        return _Request(_meta)

    def values(self):
        return _FakeValues(self._s)

    def batchUpdate(self, spreadsheetId=None, body=None):  # noqa: N803
        def _apply():
            for req in body["requests"]:
                if "insertDimension" in req:
                    rng = req["insertDimension"]["range"]
                    self._s.rows.insert(rng["startIndex"], [])
                elif "deleteDimension" in req:
                    rng = req["deleteDimension"]["range"]
                    del self._s.rows[rng["startIndex"]:rng["endIndex"]]
            return {"replies": []}

        return _Request(_apply)


class FakeSheetsService:
    """In-memory stand-in for a googleapiclient Sheets v4 service.

    Supports the exact call chains used by the sheets client and the cleaner-
    sheet handler: ``spreadsheets().get()``, ``spreadsheets().values().get()``,
    ``spreadsheets().batchUpdate()`` (insert/delete dimension) and
    ``spreadsheets().values().update()``. Row state mutates so a round-trip of
    insert → read-back → delete behaves like the real sheet.
    """

    def __init__(self, recording: dict):
        self.title = recording["spreadsheet_title"]
        self.sheet_name = recording["sheet_name"]
        self.sheet_id = recording["sheet_id"]
        self.rows = [list(r) for r in recording["rows"]]

    @staticmethod
    def _row_index_from_a1(a1_range: str) -> int:
        """Parse a range like 'Sheet Name!A5:F5' → 0-based row index (4)."""
        cell = a1_range.split("!", 1)[-1]
        m = re.search(r"[A-Z]+(\d+)", cell)
        if not m:
            raise ValueError(f"Cannot parse row from range {a1_range!r}")
        return int(m.group(1)) - 1

    def spreadsheets(self):
        return _FakeSpreadsheets(self)


# ---------------------------------------------------------------------------
# Seam
# ---------------------------------------------------------------------------


class _FakeAccessCodes:
    def __init__(self, client: "FakeSeamClient"):
        self._c = client

    def create(self, device_id=None, code=None, name=None, starts_at=None, ends_at=None):
        code_id = self._c.next_id or str(uuid.uuid4())
        self._c.next_id = None
        record = SimpleNamespace(
            access_code_id=code_id,
            device_id=device_id,
            code=code,
            name=name,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        self._c.codes[code_id] = record
        return record

    def list(self, device_id=None):
        return [c for c in self._c.codes.values() if device_id in (None, c.device_id)]

    def get(self, access_code_id=None):
        return self._c.codes[access_code_id]

    def delete(self, access_code_id=None):
        self._c.codes.pop(access_code_id, None)
        return SimpleNamespace(action_attempt_id=str(uuid.uuid4()))


class FakeSeamClient:
    """In-memory stand-in for the Seam SDK client (``.access_codes.*``)."""

    def __init__(self, seed_id: str | None = None):
        self.codes: dict[str, SimpleNamespace] = {}
        self.next_id = seed_id  # id handed out by the next .create() call
        self.access_codes = _FakeAccessCodes(self)


# ---------------------------------------------------------------------------
# DocuSign
# ---------------------------------------------------------------------------


class FakeEnvelopesApi:
    """In-memory stand-in for docusign_esign.EnvelopesApi."""

    def __init__(self, envelope_id: str, status: str, pdf_bytes: bytes):
        self._envelope_id = envelope_id
        self._status = status
        self._pdf = pdf_bytes
        self.voided: list[str] = []

    def create_envelope(self, account_id, envelope_definition=None):
        return SimpleNamespace(envelope_id=self._envelope_id)

    def get_envelope(self, account_id, envelope_id):
        status = "voided" if envelope_id in self.voided else self._status
        return SimpleNamespace(status=status, envelope_id=envelope_id)

    def get_document(self, account_id, document_id, envelope_id):
        return self._pdf

    def update(self, account_id, envelope_id, envelope=None):
        self.voided.append(envelope_id)
        return SimpleNamespace(envelope_id=envelope_id)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class _FakeMessages:
    def __init__(self, service: "FakeGmailService"):
        self._s = service

    def send(self, userId=None, body=None):  # noqa: N803
        self._s.sent.append(body)
        return _Request(lambda: {"id": self._s.message_id})


class _FakeUsers:
    def __init__(self, service: "FakeGmailService"):
        self._s = service

    def getProfile(self, userId=None):  # noqa: N803
        return _Request(lambda: {"emailAddress": self._s.profile_email})

    def messages(self):
        return _FakeMessages(self._s)


class FakeGmailService:
    """In-memory stand-in for a googleapiclient Gmail v1 service."""

    def __init__(self, profile_email: str, message_id: str):
        self.profile_email = profile_email
        self.message_id = message_id
        self.sent: list[dict] = []

    def users(self):
        return _FakeUsers(self)
