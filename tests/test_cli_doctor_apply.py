"""Subprocess tests for `doctor --apply` (P2-T8).

`doctor --apply` must actually apply recovery actions through the SAME
dispatchers the web recovery panel uses (recover.py:_handle_action) — one
apply path. These tests exercise the headless apply path end-to-end.

The seeded tool is ``status="deployed"``, ``enabled=False``,
``dry_run_status="passed"`` — the exact state RecoveryEngine.diagnose_tool
classifies as ``GATE_3_DISABLED`` (a runtime-ready tool that is gate-3
disabled). ``deployed`` (not ``approved``) is a real ``ToolStatus`` member,
so the enable dispatcher's ``get_tool`` can load it — exercising the genuine
end-to-end apply path. Applying it flips the tool to enabled, after which a
second diagnose reports no GATE_3_DISABLED.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    from systemu.storage.sqlite.models import Base, ToolRow, ScrollRow
    url = f"sqlite:///{tmp_path/'vault.db'}"
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ToolRow(id="tool_a", name="fetch_json", status="deployed",
                      enabled=False, dry_run_status="passed", dry_run_evidence={}))
        s.add(ScrollRow(id="scr1", name="Doc weather", status="approved"))
        s.commit()
    return url


def _run(args, db_url):
    env = {**os.environ, "SYSTEMU_DATABASE_URL": db_url}
    return subprocess.run([sys.executable, "-m", "sharing_on", *args],
                          cwd=str(REPO), capture_output=True, text=True,
                          timeout=120, env=env)


def test_doctor_apply_enables_disabled_tool(db):
    proc = _run(["doctor", "tool_a", "--apply"], db)
    assert proc.returncode == 0, f"out:\n{proc.stdout}\nerr:\n{proc.stderr}"
    assert "GATE_3_DISABLED" in proc.stdout
    assert "Applied" in proc.stdout or "enabled" in proc.stdout.lower()
    proc2 = _run(["doctor", "tool_a"], db)
    assert "GATE_3_DISABLED" not in proc2.stdout


def test_doctor_without_apply_is_unchanged(db):
    proc = _run(["doctor", "tool_a"], db)
    assert proc.returncode == 0
    assert "GATE_3_DISABLED" in proc.stdout
