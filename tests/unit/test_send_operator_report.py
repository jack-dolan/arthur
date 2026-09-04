"""Tests for `python -m app.send_operator_report`.

The weekly health audit runs on the host, outside the container, and has no
mail credentials of its own. Rather than give the host a second copy of the
Gmail credentials, it pipes its report into this command inside the running app
container, where the alerts account already lives.

Two behaviours carry operational weight:

- **A failure must exit non-zero.** The cron line chains the healthchecks.io
  ping behind this command with `&&`, so a silent failure here would ping the
  dead-man's switch for an email that never arrived — the worst possible
  outcome for a monitor.
- **An empty report is refused.** An empty email is indistinguishable from a
  healthy quiet week, and would train the reader to ignore the whole thing.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import load_config

FIXTURES = Path(__file__).parents[1] / "fixtures"
_CONFIG = load_config(FIXTURES / "config.test.yaml")
ALERTS_ADDRESS = _CONFIG.email.alerts


def _run(body: str, *args: str, service: MagicMock | None = None,
         email_config=None):
    """Run the command with both external seams patched at the caller.

    ``email`` is a REAL EmailConfig, not a MagicMock attribute: the To header is
    built by the model, and a mock would let that logic drift untested.
    """
    from app import send_operator_report

    service = service if service is not None else MagicMock()
    config = MagicMock()
    config.email = email_config if email_config is not None else _CONFIG.email

    with patch.object(send_operator_report, "get_alerts_service", return_value=service):
        with patch.object(send_operator_report, "load_config", return_value=config):
            code = send_operator_report.main(list(args), stdin=io.StringIO(body))
    return code, service


def _sent_message(service: MagicMock) -> dict[str, str]:
    """Decode the MIME message the command handed to the Gmail client."""
    send_call = service.users().messages().send.call_args
    raw = send_call.kwargs["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    headers, _, text = decoded.partition("\n\n")
    parsed = dict(
        line.split(": ", 1) for line in headers.splitlines() if ": " in line
    )
    parsed["body"] = text
    return parsed


def test_report_also_reaches_every_additional_recipient():
    """The co-owner gets the weekly audit too, while From stays the mailbox the
    automation reads."""
    from app.config import EmailConfig

    email_config = EmailConfig(
        booking_feed=_CONFIG.email.booking_feed,
        alerts=ALERTS_ADDRESS,
        alerts_additional_recipients=["second@example.com"],
    )
    code, service = _run("healthy\n\nNothing needs action.",
                         "--subject", "Weekly audit: healthy",
                         email_config=email_config)

    assert code == 0
    message = _sent_message(service)
    assert message["To"] == f"{ALERTS_ADDRESS}, second@example.com"
    assert message["From"] == ALERTS_ADDRESS


def test_sends_the_report_with_the_given_subject_to_the_alerts_address():
    code, service = _run("healthy\n\nNothing needs action.", "--subject", "Weekly audit: healthy")

    assert code == 0
    message = _sent_message(service)
    assert message["Subject"] == "Weekly audit: healthy"
    assert message["To"] == ALERTS_ADDRESS
    assert message["From"] == ALERTS_ADDRESS
    assert "Nothing needs action." in message["body"]


def test_empty_report_is_refused_and_nothing_is_sent():
    code, service = _run("   \n  \n", "--subject", "Weekly audit")

    assert code == 2
    service.users().messages().send.assert_not_called()


def test_missing_subject_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        _run("a report", service=MagicMock())
    assert excinfo.value.code != 0


def test_send_failure_exits_non_zero_so_the_heartbeat_is_withheld():
    service = MagicMock()
    service.users().messages().send.return_value.execute.side_effect = RuntimeError("boom")

    code, _ = _run("a report", "--subject", "Weekly audit", service=service)

    assert code == 1


def test_oversized_report_is_truncated_rather_than_dropped():
    """A runaway report must still deliver its verdict, which leads the body."""
    body = "needs attention\n\n" + ("x" * 200_000)

    code, service = _run(body, "--subject", "Weekly audit")

    assert code == 0
    message = _sent_message(service)
    assert len(message["body"]) < 200_000
    assert message["body"].startswith("needs attention")
    assert "truncated" in message["body"].lower()
