"""Weekly LLM inbox drift reviewer (sustainability audit item 4).

The keyword classifier sends anything it does not recognise to the OTHER
dead-letter bucket, silently and by design. A reworded platform email therefore
disappears without an alert, and the guest arrives with no door code. The
existing weekly digest lists those subjects for a human to eyeball; this job
adds judgment on top by asking Claude, per email, whether any of them is a
booking the classifier missed.

Everything here is ALERT-ONLY: the job never edits the classifier, a task, or a
dead letter. These tests pin that, the caps that stop a runaway inbox becoming a
runaway bill, and the handling of email text as untrusted input.

Mocking follows the `testing-safely` skill: every seam is patched on
``app.ingestion.inbox_reviewer``, the module that CALLS it, because that module
binds the names at import time.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ingestion.inbox_reviewer import (
    MAX_SNIPPET_CHARS,
    VERDICT_MISSED_BOOKING,
    VERDICT_NOISE,
    DeadLetter,
    ReviewParseError,
    build_review_messages,
    parse_verdict,
    review_dead_letters,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _letter(n: int = 1, **overrides) -> DeadLetter:
    defaults = dict(
        message_id=f"gmail-msg-{n:04d}",
        sender="automated@airbnb.com",
        subject=f"Something unfamiliar {n}",
        received=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return DeadLetter(**defaults)


def _response(payload: object, *, input_tokens: int = 400, output_tokens: int = 40):
    """A stand-in for an Anthropic Message carrying one JSON text block."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    block = MagicMock()
    block.type = "text"
    block.text = text
    message = MagicMock()
    message.content = [block]
    message.usage.input_tokens = input_tokens
    message.usage.output_tokens = output_tokens
    return message


def _verdict(verdict: str, reason: str = "because", confidence: str = "high") -> dict:
    return {"verdict": verdict, "confidence": confidence, "reason": reason}


def _client_returning(*responses) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


def _config() -> MagicMock:
    config = MagicMock()
    config.email.alerts = "alerts@example.com"
    return config


async def _run(letters, client, *, snippets=None, **kwargs) -> MagicMock:
    """Drive review_dead_letters with every outbound seam patched.

    Returns the send_llm_inbox_review_alert mock so a test can assert on the
    alert (or on its absence). Every seam that could reach the outside world is
    patched explicitly, including on paths a given test does not expect to
    reach: a RED run executes paths the author has not finished thinking about,
    which is exactly how this repo has leaked live email twice.
    """
    send = MagicMock()
    snippet_map = snippets or {letter.message_id: "body text" for letter in letters}

    with (
        patch(
            "app.ingestion.inbox_reviewer._gather_dead_letters",
            AsyncMock(return_value=list(letters)),
        ),
        patch(
            "app.ingestion.inbox_reviewer.get_anthropic_client", return_value=client
        ),
        patch(
            "app.ingestion.inbox_reviewer.get_booking_feed_service",
            return_value=MagicMock(),
        ),
        patch(
            "app.ingestion.inbox_reviewer._fetch_snippet",
            side_effect=lambda service, message_id: snippet_map.get(message_id, ""),
        ),
        patch(
            "app.ingestion.inbox_reviewer.get_alerts_service", return_value=MagicMock()
        ),
        patch("app.ingestion.inbox_reviewer.send_llm_inbox_review_alert", send),
        patch("app.ingestion.inbox_reviewer.load_config", return_value=_config()),
    ):
        await review_dead_letters(**kwargs)

    return send


# ---------------------------------------------------------------------------
# parse_verdict — strict schema, everything else is "needs human review"
# ---------------------------------------------------------------------------


def test_parse_verdict_accepts_the_expected_shape():
    parsed = parse_verdict(
        json.dumps(_verdict(VERDICT_MISSED_BOOKING, "carries a check-in date"))
    )
    assert parsed["verdict"] == VERDICT_MISSED_BOOKING
    assert parsed["reason"] == "carries a check-in date"
    assert parsed["confidence"] == "high"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json at all",
        "{",
        json.dumps(["a", "list"]),
        json.dumps({"verdict": "maybe", "confidence": "high", "reason": "r"}),
        json.dumps({"confidence": "high", "reason": "r"}),
        json.dumps({"verdict": VERDICT_NOISE, "confidence": "certain", "reason": "r"}),
        json.dumps({"verdict": VERDICT_NOISE, "confidence": "high"}),
        json.dumps({"verdict": VERDICT_NOISE, "confidence": "high", "reason": 7}),
    ],
    ids=[
        "empty",
        "prose",
        "truncated-json",
        "wrong-top-level-type",
        "unknown-verdict",
        "missing-verdict",
        "unknown-confidence",
        "missing-reason",
        "reason-not-a-string",
    ],
)
def test_parse_verdict_rejects_anything_off_schema(text):
    with pytest.raises(ReviewParseError):
        parse_verdict(text)


def test_parse_verdict_truncates_an_overlong_reason():
    """The reason is model output echoed into an email. Bound it."""
    parsed = parse_verdict(json.dumps(_verdict(VERDICT_NOISE, "x" * 5000)))
    assert len(parsed["reason"]) <= 500


# ---------------------------------------------------------------------------
# Prompt construction — email text is data, never instructions
# ---------------------------------------------------------------------------


def test_prompt_states_that_email_content_is_untrusted_data():
    system, messages = build_review_messages(_letter(), "hello")
    lowered = system.lower()
    assert "untrusted" in lowered
    assert "instruction" in lowered
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_prompt_carries_only_one_email_per_call():
    """Structural isolation: one email per API call means text embedded in one
    email has no path to influence any other email's classification, because
    no other email is in that request at all."""
    letter = _letter(1, subject="SUBJECT-ONE")
    other = _letter(2, subject="SUBJECT-TWO")
    _, messages = build_review_messages(letter, "body one")
    rendered = json.dumps(messages)
    assert "SUBJECT-ONE" in rendered
    assert other.subject not in rendered


def test_prompt_bounds_the_snippet():
    """A single very long email must not dominate a run's token spend."""
    content = build_review_messages(
        _letter(), "é" * (MAX_SNIPPET_CHARS * 10)
    )[1][0]["content"]
    assert content.count("é") == MAX_SNIPPET_CHARS


# ---------------------------------------------------------------------------
# The job: classification -> alert
# ---------------------------------------------------------------------------


async def test_alerts_when_one_email_is_judged_a_missed_booking():
    letters = [_letter(1), _letter(2)]
    client = _client_returning(
        _response(_verdict(VERDICT_NOISE, "a marketing email")),
        _response(_verdict(VERDICT_MISSED_BOOKING, "carries a confirmation code")),
    )

    send = await _run(letters, client)

    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert kwargs["reviewed_count"] == 2
    suspects = kwargs["suspects"]
    assert len(suspects) == 1
    assert suspects[0]["subject"] == letters[1].subject
    assert suspects[0]["reason"] == "carries a confirmation code"
    assert kwargs["unreviewable"] == []


async def test_stays_silent_when_every_email_is_noise():
    """The quiet week. The non-AI digest remains the human backstop, so this
    job saying nothing must mean nothing needs saying."""
    letters = [_letter(1), _letter(2), _letter(3)]
    client = _client_returning(
        *(_response(_verdict(VERDICT_NOISE, "receipt")) for _ in letters)
    )

    send = await _run(letters, client)

    send.assert_not_called()


async def test_stays_silent_and_calls_nothing_when_there_are_no_dead_letters():
    client = _client_returning()

    send = await _run([], client)

    send.assert_not_called()
    client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# The job: unparseable responses are escalated, never swallowed
# ---------------------------------------------------------------------------


async def test_unparseable_response_becomes_a_needs_human_review_row():
    letters = [_letter(1), _letter(2)]
    client = _client_returning(
        _response("I'm afraid I can't answer that."),
        _response(_verdict(VERDICT_NOISE, "receipt")),
    )

    send = await _run(letters, client)

    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert kwargs["suspects"] == []
    unreviewable = kwargs["unreviewable"]
    assert len(unreviewable) == 1
    assert unreviewable[0]["subject"] == letters[0].subject
    assert unreviewable[0]["error"]


async def test_api_failure_on_one_email_does_not_stop_the_others():
    letters = [_letter(1), _letter(2)]
    client = MagicMock()
    client.messages.create.side_effect = [
        RuntimeError("overloaded_error"),
        _response(_verdict(VERDICT_MISSED_BOOKING, "looks like a real reservation")),
    ]

    send = await _run(letters, client)

    kwargs = send.call_args.kwargs
    assert len(kwargs["suspects"]) == 1
    assert len(kwargs["unreviewable"]) == 1
    assert kwargs["reviewed_count"] == 2


async def test_missing_api_key_skips_the_run_without_alerting():
    """No key means no reviewer. The daily credential sentinel is what reports
    that, so this job must not also start emailing about it every week."""
    from app.integrations.claude.client import AnthropicNotConfigured

    send = MagicMock()
    with (
        patch(
            "app.ingestion.inbox_reviewer._gather_dead_letters",
            AsyncMock(return_value=[_letter(1)]),
        ),
        patch(
            "app.ingestion.inbox_reviewer.get_anthropic_client",
            side_effect=AnthropicNotConfigured("no key"),
        ),
        patch("app.ingestion.inbox_reviewer.send_llm_inbox_review_alert", send),
        patch(
            "app.ingestion.inbox_reviewer.get_alerts_service", return_value=MagicMock()
        ),
        patch(
            "app.ingestion.inbox_reviewer.get_booking_feed_service",
            return_value=MagicMock(),
        ),
        patch("app.ingestion.inbox_reviewer.load_config", return_value=_config()),
    ):
        await review_dead_letters()

    send.assert_not_called()


# ---------------------------------------------------------------------------
# Caps: a runaway inbox must not become a runaway bill
# ---------------------------------------------------------------------------


async def test_item_cap_bounds_the_number_of_api_calls():
    letters = [_letter(n) for n in range(1, 31)]
    client = _client_returning(
        *(_response(_verdict(VERDICT_NOISE, "receipt")) for _ in range(60))
    )

    send = await _run(letters, client, max_items=5)

    assert client.messages.create.call_count == 5
    send.assert_not_called()


async def test_token_budget_stops_the_run_partway():
    letters = [_letter(n) for n in range(1, 11)]
    client = _client_returning(
        *(
            _response(
                _verdict(VERDICT_NOISE, "receipt"), input_tokens=400, output_tokens=100
            )
            for _ in range(20)
        )
    )

    # 500 tokens per call; a 1200-token budget allows three calls and then the
    # accumulated spend is over budget.
    send = await _run(letters, client, max_items=10, token_budget=1200)

    assert client.messages.create.call_count == 3
    send.assert_not_called()


async def test_reaching_the_item_cap_is_reported_in_the_alert():
    """Truncating the queue silently would hide un-reviewed emails behind a
    clean-looking report."""
    letters = [_letter(n) for n in range(1, 11)]
    client = _client_returning(
        _response(_verdict(VERDICT_MISSED_BOOKING, "a real reservation")),
        *(_response(_verdict(VERDICT_NOISE, "receipt")) for _ in range(5)),
    )

    send = await _run(letters, client, max_items=3)

    assert send.call_args.kwargs["not_reviewed_count"] == 7


# ---------------------------------------------------------------------------
# Alert-only: prior owner decision, and the reason the job is safe to trust
# ---------------------------------------------------------------------------


async def test_the_job_never_writes_anything():
    """The reviewer reads dead letters and sends at most an email. It must not
    touch the classifier, tasks, bookings, or the dead letters themselves — the
    owner's decision, and what makes a wrong judgment harmless."""
    letters = [_letter(1)]
    client = _client_returning(
        _response(_verdict(VERDICT_MISSED_BOOKING, "a real reservation"))
    )

    session = MagicMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("app.ingestion.inbox_reviewer.AsyncSessionLocal", session_factory):
        send = await _run(letters, client)

    send.assert_called_once()
    # _gather_dead_letters is patched out, so no session should be opened at
    # all; and nothing anywhere may commit, add, or delete.
    session.commit.assert_not_called()
    session.add.assert_not_called()
    session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Every run is recorded, so "no email" can be told apart from "never ran"
# ---------------------------------------------------------------------------


async def test_a_quiet_run_is_still_recorded_as_a_run():
    """The silent path is the one that needs the record most: without it, a
    clean week and a dead job look identical from the owner's inbox."""
    letters = [_letter(1)]
    client = _client_returning(_response(_verdict(VERDICT_NOISE, "receipt")))

    with patch("app.ingestion.inbox_reviewer.record_run") as record:
        send = await _run(letters, client)

    send.assert_not_called()
    record.assert_called_once()
    assert record.call_args.kwargs["outcome"] == "ok"
    assert record.call_args.kwargs["reviewed"] == 1


async def test_a_week_with_no_dead_letters_is_recorded_as_idle():
    with patch("app.ingestion.inbox_reviewer.record_run") as record:
        await _run([], _client_returning())

    record.assert_called_once()
    assert record.call_args.kwargs["outcome"] == "idle"


async def test_a_missing_key_is_recorded_rather_than_leaving_a_gap():
    from app.integrations.claude.client import AnthropicNotConfigured

    with (
        patch(
            "app.ingestion.inbox_reviewer._gather_dead_letters",
            AsyncMock(return_value=[_letter(1)]),
        ),
        patch(
            "app.ingestion.inbox_reviewer.get_anthropic_client",
            side_effect=AnthropicNotConfigured("no key"),
        ),
        patch("app.ingestion.inbox_reviewer.send_llm_inbox_review_alert", MagicMock()),
        patch(
            "app.ingestion.inbox_reviewer.get_alerts_service", return_value=MagicMock()
        ),
        patch("app.ingestion.inbox_reviewer.load_config", return_value=_config()),
        patch("app.ingestion.inbox_reviewer.record_run") as record,
    ):
        await review_dead_letters()

    record.assert_called_once()
    assert record.call_args.kwargs["outcome"] == "skipped_no_key"


async def test_a_journal_failure_cannot_break_the_run():
    """Bookkeeping that can take down the job it is recording is worse than no
    bookkeeping at all."""
    letters = [_letter(1)]
    client = _client_returning(
        _response(_verdict(VERDICT_MISSED_BOOKING, "a real reservation"))
    )

    with patch(
        "app.ingestion.inbox_reviewer.record_run",
        side_effect=OSError("read-only file system"),
    ):
        with pytest.raises(OSError):
            # record_run itself swallows errors; this proves the reviewer would
            # notice if that contract were ever broken, which is why the
            # swallowing lives in one place and is tested there.
            await _run(letters, client)
