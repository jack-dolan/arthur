"""Email an operator report, reading the report body from stdin.

    docker exec <app> python -m app.send_operator_report --subject "..." < report.md

**Why this exists.** The weekly health audit (CI/CD Step 13) runs on the host,
outside the container. It has a report to deliver and no way to send mail. The
alternatives were all worse: a second copy of the Gmail refresh token on the
host, an SMTP relay with its own credential, or a new HTTP endpoint on the app
that would need authenticating. Running one command inside the container reuses
the alerts account the app already holds and adds no credential anywhere.

**Exit codes are load-bearing.** The cron line chains the healthchecks.io ping
behind this command with `&&`. A non-zero exit therefore withholds the
heartbeat, and the dead-man's switch fires — which is exactly right, because a
report that was generated but never delivered is an outage of the monitor.
"""
from __future__ import annotations

import argparse
import base64
import sys
from email.mime.text import MIMEText
from typing import TextIO

from app.config import load_config
from app.integrations.gmail.oauth import get_alerts_service

# Gmail accepts far more than this. The cap is not a protocol limit, it is a
# guard against a runaway generator turning one bad run into an unreadable
# mail. The verdict leads the report, so a truncated body still says what
# happened.
MAX_BODY_CHARS = 100_000

_TRUNCATION_NOTE = (
    "\n\n[... report truncated at {limit} characters. "
    "Read the full output on the host.]\n"
)


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.send_operator_report",
        description="Email an operator report (body on stdin) via the alerts account.",
    )
    parser.add_argument("--subject", required=True, help="Email subject line.")
    args = parser.parse_args(argv)

    body = (stdin if stdin is not None else sys.stdin).read()
    if not body.strip():
        print("send_operator_report: refusing to send an empty report", file=sys.stderr)
        return 2

    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + _TRUNCATION_NOTE.format(limit=MAX_BODY_CHARS)

    try:
        email_config = load_config().email
        mime = MIMEText(body, "plain")
        mime["To"] = email_config.alerts_to_header
        mime["From"] = email_config.alerts
        mime["Subject"] = args.subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        get_alerts_service().users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
    except Exception as exc:  # noqa: BLE001 — the exit code is the contract
        print(f"send_operator_report: send failed: {exc}", file=sys.stderr)
        return 1

    print(f"send_operator_report: sent to {email_config.alerts_to_header}")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via main()
    raise SystemExit(main())
