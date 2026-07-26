import logging

import pytest


def _catalog():
    from app.schema_guard import RevisionCatalog

    return RevisionCatalog(
        ordered_revisions=("0001", "0002", "0003"),
        down_revisions={
            "0001": (),
            "0002": ("0001",),
            "0003": ("0002",),
        },
        heads=frozenset({"0003"}),
    )


def test_guard_starts_when_database_matches_build_head(caplog):
    from app.schema_guard import SchemaAction, check_schema_state

    with caplog.at_level(logging.INFO, logger="app.schema_guard"):
        result = check_schema_state({"0003"}, _catalog())

    assert result.action is SchemaAction.START
    assert result.pending_revisions == ()
    assert "database revision == build head (0003); starting application" in caplog.text


def test_guard_warns_and_starts_when_database_revision_is_unknown(caplog):
    from app.schema_guard import SchemaAction, check_schema_state

    with caplog.at_level(logging.WARNING, logger="app.schema_guard"):
        result = check_schema_state({"0004"}, _catalog())

    assert result.action is SchemaAction.START
    assert result.unknown_revisions == ("0004",)
    assert "UNKNOWN TO THIS BUILD" in caplog.text
    assert "starting application under the N-1 compatibility guarantee" in caplog.text


def test_guard_refuses_when_database_is_behind_and_names_pending_revisions(caplog):
    from app.schema_guard import SchemaBehindError, check_schema_state

    with (
        caplog.at_level(logging.ERROR, logger="app.schema_guard"),
        pytest.raises(SchemaBehindError, match=r"pending revisions: 0002, 0003"),
    ):
        check_schema_state({"0001"}, _catalog())

    assert "REFUSING TO START" in caplog.text
    assert "0002, 0003" in caplog.text
