"""Classify an incoming email.Message as one of the known booking event types.

Classification is intentionally simple: sender domain + subject keyword matching.
Both direct emails (auto-forwarded by Gmail, which preserves the original From)
and manual-forward wrappers (where the outer From is a personal Gmail account)
are handled by extracting the "effective" sender and subject before matching.
"""
from __future__ import annotations

import enum
import re
from email.message import Message


class EmailType(str, enum.Enum):
    AIRBNB_BOOKING = "airbnb_booking"
    AIRBNB_CANCELLATION = "airbnb_cancellation"
    AIRBNB_ALTERATION = "airbnb_alteration"
    VRBO_BOOKING = "vrbo_booking"
    VRBO_CANCELLATION = "vrbo_cancellation"
    VRBO_ALTERATION = "vrbo_alteration"
    OTHER = "other"


# Domains that identify each platform as the true sender of the original email.
_AIRBNB_DOMAINS = {"airbnb.com", "automated.airbnb.com"}
_VRBO_DOMAINS = {"messages.homeaway.com", "partners.expediagroup.com", "vrbo.com"}

# Pattern that marks the start of a Gmail manual-forward block.
_FORWARD_HEADER_RE = re.compile(
    r"-{5,}\s*Forwarded message\s*-{5,}",
    re.IGNORECASE,
)
# Lines inside the forward block that carry From/Subject.
_FWD_FROM_RE = re.compile(r"^From:\s*(.+)", re.IGNORECASE)
_FWD_SUBJECT_RE = re.compile(r"^Subject:\s*(.+)", re.IGNORECASE)

# F9 (bug hunt 2026-07-22): deliberately loose — no real "reservation changed"
# sample was available to write a precise rule against. A booking or
# cancellation subject already matches its own (checked-first) branch, so a
# false positive here just costs one alert; a false negative would silently
# leave a booking's stale dates driving the door code, HOA window and
# cleaner row (the OTHER path is silent by design). Revisit once a real
# sample email shows up.
_ALTERATION_RE = re.compile(
    r"\b(updated?|changed?|modif(?:y|ied|ication)|alter(?:ed|ation))\b",
    re.IGNORECASE,
)


def _extract_domain(address: str) -> str:
    """Return the lowercase domain from an RFC 5322 address string."""
    match = re.search(r"@([\w.\-]+)", address)
    return match.group(1).lower() if match else ""


def _get_text_body(msg: Message) -> str:
    """Return the first text/plain payload, decoded to str."""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content()
    return ""


def _effective_sender_and_subject(msg: Message) -> tuple[str, str]:
    """Return (from_address, subject) for the *original* platform email.

    For auto-forwarded emails the outer From *is* the platform sender, so we
    just return the outer headers.  For manual forwards (outer From is a
    personal Gmail, body contains a forwarded-message block) we parse the
    inner From/Subject from the forwarded block.
    """
    outer_from = msg.get("From", "")
    outer_subject = msg.get("Subject", "")
    outer_domain = _extract_domain(outer_from)

    # If the outer sender is already a known platform domain, no unwrapping needed.
    if outer_domain in _AIRBNB_DOMAINS | _VRBO_DOMAINS:
        return outer_from, outer_subject

    # Look for a manual-forward block in the text body.
    body = _get_text_body(msg)
    if not _FORWARD_HEADER_RE.search(body):
        return outer_from, outer_subject

    # Parse inner From/Subject from the forwarded-message header lines.
    inner_from = outer_from
    inner_subject = outer_subject
    in_header = False
    for line in body.splitlines():
        if _FORWARD_HEADER_RE.search(line):
            in_header = True
            continue
        if in_header:
            if not line.strip():
                # Blank line ends the forwarded-message header block.
                break
            m = _FWD_FROM_RE.match(line)
            if m:
                inner_from = m.group(1).strip()
            m = _FWD_SUBJECT_RE.match(line)
            if m:
                inner_subject = m.group(1).strip()

    return inner_from, inner_subject


def classify(msg: Message) -> EmailType:
    """Return the EmailType for *msg*."""
    sender, subject = _effective_sender_and_subject(msg)
    domain = _extract_domain(sender)
    subj_lower = subject.lower()

    if domain in _AIRBNB_DOMAINS:
        if "reservation confirmed" in subj_lower:
            return EmailType.AIRBNB_BOOKING
        if "canceled" in subj_lower or "cancelled" in subj_lower:
            return EmailType.AIRBNB_CANCELLATION
        if _ALTERATION_RE.search(subj_lower):
            return EmailType.AIRBNB_ALTERATION
        return EmailType.OTHER

    if domain in _VRBO_DOMAINS:
        if "instant booking from" in subj_lower:
            return EmailType.VRBO_BOOKING
        if "was canceled" in subj_lower or "was cancelled" in subj_lower:
            return EmailType.VRBO_CANCELLATION
        if _ALTERATION_RE.search(subj_lower):
            return EmailType.VRBO_ALTERATION
        return EmailType.OTHER

    return EmailType.OTHER
