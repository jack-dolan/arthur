"""Anthropic (Claude API) client factory.

One caller only: the weekly inbox drift reviewer
(:mod:`app.ingestion.inbox_reviewer`), plus the daily credential sentinel's
read-only probe of the key.

This module is the single construction choke point for the Claude API, which is
what makes it the right place for both the preview-mode stub and the test
suite's live-API guard. Anywhere further out and a caller could slip past both.

The package is ``claude`` rather than ``anthropic`` on purpose: a package of
that name sitting next to ``from anthropic import Anthropic`` invites a
shadowing bug the day someone changes how the tree is imported.
"""
from __future__ import annotations

import logging

from anthropic import Anthropic

from app.preview import inert_client, is_preview_mode
from app.settings import settings

log = logging.getLogger(__name__)

# The model the reviewer runs on. Deliberately the small, cheap tier: the job
# answers a two-way classification question over a few hundred characters of
# email, a handful of times a week. The credential sentinel probes this exact
# id, so a model retirement surfaces as a sentinel failure rather than as a
# silently broken weekly job.
REVIEW_MODEL = "claude-haiku-4-5"


class AnthropicNotConfigured(RuntimeError):
    """ANTHROPIC_API_KEY is not set."""


def get_anthropic_client() -> Anthropic:
    """Return an authenticated Anthropic client built from settings.

    Raises :class:`AnthropicNotConfigured` when the key is unset. Callers decide
    what that means: the reviewer logs and skips, the credential sentinel counts
    it as a dead credential (a key that was removed and a key that was never set
    are indistinguishable from here, and both stop the reviewer working).

    In preview mode this returns an inert stub instead: a demonstration copy has
    no capability to reach the outside world (app/preview.py).
    """
    if is_preview_mode():
        return inert_client("Anthropic")
    if not settings.anthropic_api_key:
        raise AnthropicNotConfigured(
            "ANTHROPIC_API_KEY is not set. Add it to the environment store; "
            "see .env.template."
        )
    return Anthropic(api_key=settings.anthropic_api_key)
