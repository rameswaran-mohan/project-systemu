"""v0.9.2 mode-specific integration tests."""
from datetime import datetime, timezone
from pathlib import Path

from systemu.core.models import SessionSummary
from systemu.vault.vault import Vault


def _make_sqlite_vault(tmp_path: Path) -> Vault:
    v = Vault(root=tmp_path)
    v._storage_backend = "sqlite"
    v._sqlite_url = f"sqlite:///{tmp_path}/vault.db"
    from systemu.vault.backend.sqlite_backend import ensure_schema
    ensure_schema(v)
    return v


def _make_summary(**overrides):
    kwargs = dict(
        id="ss_1", session_id="sess_1",
        started_at=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 6, 7, 12, 5, tzinfo=timezone.utc),
        status="success", intent="find burrito places near me",
        outcome_summary="Ranked top 5 burrito spots in Bangalore",
        key_facts_learned=[], files_produced=[], tags=["food", "bangalore"],
    )
    kwargs.update(overrides)
    return SessionSummary(**kwargs)


class TestSqliteSessionSummaries:
    def test_append_writes_row(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        v.append_session_summary(_make_summary(id="ss_x", session_id="sx"))
        out = v.query_session_summaries(limit=10)
        assert len(out) == 1
        assert out[0].session_id == "sx"

    def test_search_uses_fts5(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        v.append_session_summary(_make_summary(
            id="ss_a", session_id="a",
            intent="find burrito places near me",
            outcome_summary="Listed top burrito spots",
            tags=["food"],
        ))
        v.append_session_summary(_make_summary(
            id="ss_b", session_id="b",
            intent="find ramen shops in Tokyo",
            outcome_summary="Listed top ramen shops",
            tags=["food", "tokyo"],
        ))
        out = v.search_session_summaries("burrito", limit=5)
        assert [s.session_id for s in out] == ["a"]

    def test_search_matches_tag(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        v.append_session_summary(_make_summary(
            id="ss_a", session_id="a",
            intent="find food near me",
            outcome_summary="Listed local spots",
            tags=["food", "bangalore"],
        ))
        v.append_session_summary(_make_summary(
            id="ss_b", session_id="b",
            intent="plan a trip",
            outcome_summary="Created travel plan",
            tags=["travel", "tokyo"],
        ))
        out = v.search_session_summaries("bangalore", limit=5)
        assert [s.session_id for s in out] == ["a"]

    def test_query_filters_by_user(self, tmp_path):
        v = _make_sqlite_vault(tmp_path)
        v.append_session_summary(_make_summary(id="ss_a", session_id="a", user_id="alice"))
        v.append_session_summary(_make_summary(id="ss_b", session_id="b", user_id="bob"))
        out = v.query_session_summaries(user_id="alice", limit=10)
        assert [s.session_id for s in out] == ["a"]


# ---------------------------------------------------------------------------
# Postgres tests — skipped unless SYSTEMU_POSTGRES_URL is set
# ---------------------------------------------------------------------------

import os
import pytest

_POSTGRES_URL = os.getenv("SYSTEMU_POSTGRES_URL", "")
_POSTGRES_SKIP = "set SYSTEMU_POSTGRES_URL to run postgres tests"


@pytest.mark.skipif(not _POSTGRES_URL, reason=_POSTGRES_SKIP)
class TestPostgresSessionSummaries:
    """Smoke tests for postgres backend. Skipped unless SYSTEMU_POSTGRES_URL is set."""

    def _make_pg_vault(self):
        v = Vault(root=Path("."))
        v._storage_backend = "postgres"
        v._postgres_url = _POSTGRES_URL
        from systemu.vault.backend.postgres_backend import ensure_schema
        ensure_schema(v)
        return v

    def test_append_writes_row(self):
        v = self._make_pg_vault()
        v.append_session_summary(_make_summary(id="pg_ss_1", session_id="pg_sess_1"))
        out = v.query_session_summaries(limit=10)
        assert any(s.session_id == "pg_sess_1" for s in out)

    def test_search_uses_tsvector(self):
        v = self._make_pg_vault()
        v.append_session_summary(_make_summary(
            id="pg_ss_a", session_id="pg_a",
            intent="find vegan burrito places"))
        v.append_session_summary(_make_summary(
            id="pg_ss_b", session_id="pg_b",
            intent="find sushi shops in Tokyo", tags=["food", "tokyo"]))
        out = v.search_session_summaries("burrito", limit=5)
        assert any(s.session_id == "pg_a" for s in out)
