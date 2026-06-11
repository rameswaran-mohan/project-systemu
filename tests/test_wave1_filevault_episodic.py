"""Wave 1.5 — the storage adapters must forward the episodic session-summary
surface (append/query/search).

Root cause: ``append_session_summary`` / ``query_session_summaries`` /
``search_session_summaries`` were added to the inner ``Vault`` (v0.9.2) but
NEITHER adapter (FileVault, ParallelVault) was updated to proxy them — the
same wrapper-drift class as the v0.8.0.1 decisions incident.  In
``SYSTEMU_STORAGE=file`` mode (the default) every episodic capture failed
with a non-fatal AttributeError, silently disabling episodic memory.
"""
from datetime import datetime, timezone

import pytest

from systemu.core.models import SessionSummary
from systemu.storage.file_vault import FileVault
from systemu.vault.vault import Vault


def _summary(i: int, *, user_id="u1", status="completed") -> SessionSummary:
    now = datetime.now(timezone.utc)
    return SessionSummary(
        id=f"ss_{i}", session_id=f"sess_{i}", user_id=user_id,
        started_at=now, completed_at=now, status=status,
        intent=f"do thing {i}", outcome_summary=f"did thing {i}",
        tags=["alpha"] if i % 2 == 0 else ["beta"],
    )


@pytest.fixture()
def file_vault(tmp_path):
    return FileVault(Vault(str(tmp_path / "vault")))


class TestFileVaultEpisodicForwarding:
    def test_append_then_query_roundtrip(self, file_vault):
        file_vault.append_session_summary(_summary(1))
        file_vault.append_session_summary(_summary(2))
        out = file_vault.query_session_summaries(limit=None)
        assert [s.id for s in out] == ["ss_1", "ss_2"]

    def test_query_filters_forwarded(self, file_vault):
        file_vault.append_session_summary(_summary(1, user_id="u1"))
        file_vault.append_session_summary(_summary(2, user_id="u2"))
        out = file_vault.query_session_summaries(user_id="u2", limit=None)
        assert [s.id for s in out] == ["ss_2"]

    def test_search_forwarded(self, file_vault):
        file_vault.append_session_summary(_summary(1))
        out = file_vault.search_session_summaries("thing 1")
        assert [s.id for s in out] == ["ss_1"]


class TestParallelVaultEpisodicForwarding:
    def test_append_writes_primary_and_query_reads_primary(self, tmp_path):
        from systemu.storage.parallel_vault import ParallelVault

        primary = FileVault(Vault(str(tmp_path / "vault")))
        calls = []

        class _Secondary:
            def __getattr__(self, name):
                def _rec(*a, **k):
                    calls.append(name)
                return _rec

        pv = ParallelVault(primary, _Secondary())
        pv.append_session_summary(_summary(1))
        assert "append_session_summary" in calls  # dual-write fired
        out = pv.query_session_summaries(limit=None)
        assert [s.id for s in out] == ["ss_1"]
        assert pv.search_session_summaries("thing 1")
