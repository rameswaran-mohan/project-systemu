import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Spin up a fresh sqlite vault with one disabled tool, one shadow,
    one scroll, one activity."""
    from systemu.storage.sqlite.models import (
        Base, ToolRow, ShadowRow, ScrollRow, ActivityRow,
    )
    url = f"sqlite:///{tmp_path/'vault.db'}"
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ToolRow(
            id="tool_a", name="fetch_json", status="approved", enabled=False,
            dry_run_status="failed",
            dry_run_evidence={"error": "ImportError: No module named 'requests'"},
        ))
        s.add(ShadowRow(id="sh1", name="WeatherReporter", status="awakened",
                        available_tool_ids=["tool_a"], skill_ids=[], execution_log=[]))
        s.add(ScrollRow(id="scr1", name="Doc weather", status="approved"))
        s.add(ActivityRow(id="act1", name="Doc weather activity",
                          scroll_id="scr1", assigned_shadow_id="sh1",
                          required_tool_ids=["tool_a"], required_skill_ids=[]))
        s.commit()
    return url


def _run(args, db_url):
    import os
    env = {**os.environ, "SYSTEMU_DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "sharing_on", *args],
        cwd=str(REPO), capture_output=True, text=True, timeout=60, env=env,
    )


def test_doctor_scroll_lists_blockers(db):
    proc = _run(["doctor", "scr1"], db)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    out = proc.stdout
    assert "DEP_PENDING" in out
    assert "fetch_json" in out
    assert "/recover/tool/tool_a" in out


def test_doctor_tool_dep_pending(db):
    proc = _run(["doctor", "tool_a"], db)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "DEP_PENDING" in proc.stdout


def test_doctor_tool_clean_says_ok(db, tmp_path):
    eng = create_engine(db)
    with eng.begin() as c:
        c.execute(text("UPDATE tools SET enabled=1, dry_run_status='passed', dry_run_evidence=NULL WHERE id='tool_a'"))
    proc = _run(["doctor", "tool_a"], db)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "no pending actions" in proc.stdout.lower()


def test_doctor_unknown_id_exits_nonzero(db):
    proc = _run(["doctor", "tool_totally_made_up"], db)
    assert proc.returncode != 0
    assert "not found" in proc.stderr.lower()


def test_doctor_unrecognized_id_prefix(db):
    proc = _run(["doctor", "wat_x"], db)
    assert proc.returncode != 0
