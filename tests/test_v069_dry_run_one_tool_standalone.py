"""v0.6.9: dry_run_one_tool works without prior init_jobs(),
lazy-initializing its dependencies from env."""
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_dry_run_one_tool_lazy_inits_when_called_standalone(tmp_path, monkeypatch):
    """When called outside the daemon, the function should create a fresh
    vault + config from env and run the dry-run successfully."""
    from systemu.storage.sqlite.models import Base, ToolRow
    from systemu.scheduler import jobs

    # Reset module-level state to simulate "called before init_jobs()"
    monkeypatch.setattr(jobs, "_vault", None)
    monkeypatch.setattr(jobs, "_config", None)

    db = tmp_path / "v.db"
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", f"sqlite:///{db}")

    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ToolRow(
            id="tool_x", name="dummy", status="deployed", enabled=True,
            dry_run_status="not_run",
            implementation_path="vault/tools/implementations/dummy.py",
        ))
        s.commit()

    def fake_dr(tool, *, vault=None, config=None):
        r = MagicMock(); r.success = True; r.error = None
        r.status = "passed"; r.evidence = {}
        return r
    monkeypatch.setattr(jobs._dr, "dry_run_tool", fake_dr)

    # Call standalone — should NOT silently no-op
    jobs.dry_run_one_tool("tool_x")

    with Session(eng) as s:
        t = s.query(ToolRow).filter_by(id="tool_x").one()
        assert t.dry_run_status == "passed", \
            f"expected status=passed after standalone dry-run, got {t.dry_run_status!r}"


def test_dry_run_one_tool_with_init_jobs_still_works(monkeypatch):
    """Backward-compat: after init_jobs sets _vault + _config, lazy-init
    should be skipped."""
    from systemu.scheduler import jobs

    fake_vault = MagicMock()
    fake_tool = MagicMock(id="tool_x", name="dummy", enabled=True)
    fake_vault.get_tool.return_value = fake_tool
    fake_config = MagicMock()
    monkeypatch.setattr(jobs, "_vault", fake_vault)
    monkeypatch.setattr(jobs, "_config", fake_config)

    def fake_dr(tool, *, vault=None, config=None):
        r = MagicMock(); r.success = True; r.error = None
        r.status = "passed"; r.evidence = {}
        return r
    monkeypatch.setattr(jobs._dr, "dry_run_tool", fake_dr)

    jobs.dry_run_one_tool("tool_x")
    fake_vault.save_tool.assert_called()


def test_dry_run_one_tool_missing_env_returns_silently(monkeypatch):
    """If SYSTEMU_DATABASE_URL isn't set AND init_jobs hasn't run, return
    silently with a warning log (don't crash)."""
    from systemu.scheduler import jobs
    monkeypatch.setattr(jobs, "_vault", None)
    monkeypatch.setattr(jobs, "_config", None)
    monkeypatch.delenv("SYSTEMU_DATABASE_URL", raising=False)
    # Should not raise
    jobs.dry_run_one_tool("anything")


def test_dry_run_one_tool_handles_dry_run_exception(tmp_path, monkeypatch):
    """If the dry_run pipeline itself raises, record as failed + classify."""
    from systemu.storage.sqlite.models import Base, ToolRow
    from systemu.scheduler import jobs

    monkeypatch.setattr(jobs, "_vault", None)
    monkeypatch.setattr(jobs, "_config", None)
    db = tmp_path / "v.db"
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", f"sqlite:///{db}")

    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ToolRow(id="tool_x", name="dummy", status="deployed", enabled=True,
                      dry_run_status="not_run"))
        s.commit()

    def raise_import(tool, *, vault=None, config=None):
        raise ImportError("No module named 'requests'")
    monkeypatch.setattr(jobs._dr, "dry_run_tool", raise_import)

    jobs.dry_run_one_tool("tool_x")
    with Session(eng) as s:
        t = s.query(ToolRow).filter_by(id="tool_x").one()
        assert t.dry_run_status == "failed"
        assert t.dry_run_evidence is not None
        assert t.dry_run_evidence.get("classified_reason") == "DEP_PENDING"
        assert t.dry_run_evidence.get("missing_package") == "requests"
