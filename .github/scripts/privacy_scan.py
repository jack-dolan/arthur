#!/usr/bin/env python3
"""Pattern-only privacy scan for this repository's CI.

This repository automates a *real* short-term rental property, so its history
and its docs are written against real bookings. Everything published here uses
reserved placeholder values instead:

    emails   -> ``*@example.com`` and the other RFC 2606 reserved names
    IPv4     -> RFC 5737 documentation ranges (192.0.2/24, 198.51.100/24,
                203.0.113/24)
    IPv6     -> RFC 3849 ``2001:db8::/32``
    phones   -> the NANP ``555`` placeholder space

This script fails the build if anything that is *not* a placeholder slips in.
It matches on shape only -- there is no list of names, addresses, or other
literals anywhere in this repo -- so it catches a newly introduced real value
without itself being an inventory of what to look for.

Checks (file CONTENTS and file NAMES/paths):

  PHONE  NANP-shaped numbers, minus the 555 space and repeated-digit runs
  IPV4   IPv4 addresses, minus documentation ranges, loopback, unspecified,
         broadcast, multicast, and "version 1.2.3.4" style false positives
  IPV6   IPv6 addresses, minus 2001:db8::/32, ::1, fe80::/10, and the clock
         times and docker port maps that look like one
  EMAIL  email addresses, minus the reserved domains, ``localhost`` forms, and
         the automated platform senders the ingestion code parses by address

Usage:  python3 .github/scripts/privacy_scan.py [root]     (default: ".")
Exit:   0 = clean, 1 = hits found.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path

# Directories that hold third-party or generated content. They are never part
# of what this repo publishes, and scanning them buries the report in noise.
PRUNE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "htmlcov",
    "dist",
    "build",
    ".eggs",
    ".idea",
    ".vscode",
}

PHONE_RE = re.compile(
    r"(?<![\w+.-])(?:\+?1[ .\-]?)?\(?[2-9]\d{2}\)?[ .\-]?\d{3}[ .\-]?\d{4}(?![\w.-])"
)
IPV4_RE = re.compile(r"(?<![\w.-])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\w.-])")
IPV6_RE = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:.])")
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
    r"\.[A-Za-z]{2,}(?![\w.-])"
)

# Automated sender addresses belonging to the booking platforms. The classifier
# and parsers match on these, so they are load-bearing code, not personal data.
PLATFORM_SENDERS = {
    "automated@airbnb.com",
    "noreply@airbnb.com",
    "no-reply@vrbo.com",
    "vrbo@partners.expediagroup.com",
    "sender@messages.homeaway.com",
    "dsdevcenter@docusign.com",
}
RESERVED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_EMAIL_TLDS = (".example", ".invalid", ".test", ".localhost")

DOC_V4_NETS = [
    ipaddress.ip_network("192.0.2.0/24"),  # RFC 5737 TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # RFC 5737 TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # RFC 5737 TEST-NET-3
]
DOC_V6_NET = ipaddress.ip_network("2001:db8::/32")  # RFC 3849
VERSIONISH = re.compile(r"(?i)\bversion|\brelease\b|__version__|>=|==|~=")


def is_placeholder_phone(match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return True
    if digits[:3] == "555" or digits[3:6] == "555":
        return True
    if len(set(digits)) == 1:
        return True
    return False


def keep_ipv4(match: str, line: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(match)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        return False
    if str(addr) == "255.255.255.255":
        return False
    if any(addr in net for net in DOC_V4_NETS):
        return False
    octets = [int(o) for o in match.split(".")]
    if all(o < 100 for o in octets) and VERSIONISH.search(line):
        return False
    return True


def keep_ipv6(match: str) -> bool:
    # Clock times (04:00:00) and docker port maps (127.0.0.1:5432:5432) have
    # neither "::" nor the seven colons of a full-form address.
    if "::" not in match and match.count(":") != 7:
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", match):
        return False
    try:
        addr = ipaddress.IPv6Address(match)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_unspecified or addr.is_link_local:
        return False
    if addr in DOC_V6_NET:
        return False
    return True


def keep_email(match: str) -> bool:
    lowered = match.lower()
    if lowered in PLATFORM_SENDERS:
        return False
    domain = lowered.rsplit("@", 1)[-1]
    if domain in RESERVED_EMAIL_DOMAINS or domain.endswith(
        tuple("." + d for d in RESERVED_EMAIL_DOMAINS)
    ):
        return False
    if domain.endswith(RESERVED_EMAIL_TLDS):
        return False
    if domain == "localhost" or domain.endswith(".localhost"):
        return False
    return True


def scan_text(text: str, where: str, hits: dict[str, list[str]]) -> None:
    for lineno, line in enumerate(text.splitlines(), start=1):
        loc = where if where.startswith("PATH:") else "%s:%d" % (where, lineno)
        for m in PHONE_RE.finditer(line):
            if not is_placeholder_phone(m.group()):
                hits["PHONE"].append("%s  %s" % (loc, m.group()))
        for m in IPV4_RE.finditer(line):
            if keep_ipv4(m.group(), line):
                hits["IPV4"].append("%s  %s" % (loc, m.group()))
        for m in IPV6_RE.finditer(line):
            if keep_ipv6(m.group()):
                hits["IPV6"].append("%s  %s" % (loc, m.group()))
        for m in EMAIL_RE.finditer(line):
            if keep_email(m.group()):
                hits["EMAIL"].append("%s  %s" % (loc, m.group()))


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    hits: dict[str, list[str]] = {"PHONE": [], "IPV4": [], "IPV6": [], "EMAIL": []}
    scanned = 0

    for path in iter_files(root):
        rel = str(path.relative_to(root))
        scan_text(rel, "PATH: %s" % rel, hits)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary
        scanned += 1
        scan_text(data.decode("utf-8", "replace"), rel, hits)

    print("privacy_scan: %s" % root)
    print("privacy_scan: %d text file(s) scanned" % scanned)
    print()

    total = 0
    for kind in ("PHONE", "IPV4", "IPV6", "EMAIL"):
        found = sorted(set(hits[kind]))
        total += len(found)
        print("  %-6s %s" % (kind, "ok" if not found else "%d HIT(S)" % len(found)))
        for line in found[:60]:
            print("      %s" % line)
        if len(found) > 60:
            print("      ... and %d more" % (len(found) - 60))

    print()
    if total == 0:
        print("RESULT: PASS -- every value matched a reserved placeholder range.")
        return 0
    print("RESULT: FAIL -- %d value(s) are not placeholders." % total)
    print("        Replace them with the reserved ranges listed at the top of")
    print("        this script; see CLAUDE.md for the conventions.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
