"""dep_approvals must DEGRADE (not crash) when SYSTEMU_DATABASE_URL is unset or
points at an unreachable DB — e.g. a postgresql:// URL while psycopg2 isn't
installed (SQLAlchemy raises ModuleNotFoundError on first connect). This was a
live crash on the tool dependency-check path (dependency_installer →
is_allowlisted → _load_allowlist)."""


def test_load_allowlist_degrades_on_unreachable_db(monkeypatch):
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/nope")
    from systemu.runtime import dep_approvals
    # _dep_engine returns None (driver missing OR connection refused) → degrade
    assert dep_approvals._dep_engine() is None
    assert dep_approvals._load_allowlist() == set()
    assert dep_approvals.is_allowlisted("anything") is False
    assert dep_approvals.list_unbaked_approvals() == []


def test_dep_engine_none_when_url_unset(monkeypatch):
    monkeypatch.delenv("SYSTEMU_DATABASE_URL", raising=False)
    from systemu.runtime import dep_approvals
    assert dep_approvals._dep_engine() is None
    assert dep_approvals._load_allowlist() == set()


def test_persist_approval_noop_when_db_unavailable(monkeypatch):
    monkeypatch.delenv("SYSTEMU_DATABASE_URL", raising=False)
    from systemu.runtime import dep_approvals
    # must not raise even though no DB is reachable
    dep_approvals._persist_approval(dep_approvals._make_approval(package="x", source="test"))
