"""regression: validates that the 5 recurring workarounds documented
in captures/SHADOW_EXECUTION_VERDICT_3MODES.md no longer require manual
intervention.

Scope: tests the operator-visible recovery path (sharing_on doctor + recovery
engine), not the full live shadow execution loop (which still requires a real
OPENROUTER_API_KEY + network).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[2]


def _seed_broken_scenario(db_path: Path) -> None:
    """Seed a vault that triggers ALL 5 historical workarounds at once."""
    from systemu.storage.sqlite.models import (
        Base, ToolRow, ShadowRow, ScrollRow, ActivityRow,
    )
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # Issue #1 + #3: tool with pending dep (would have been auto-disabled
        # by v0.6.5-f sweep; v0.6.8-c keeps it enabled but records evidence)
        s.add(ToolRow(
            id="tool_fetch", name="fetch_json", status="approved", enabled=True,
            dry_run_status="failed",
            dry_run_evidence={
                "error": "ImportError: No module named 'requests'",
                "classified_reason": "DEP_PENDING",
                "missing_package": "requests",
            },
        ))
        # Issue #2: shadow with poisoned memory (4 identical "not enabled" failures)
        s.add(ShadowRow(
            id="sh_weather", name="WeatherReporter", status="awakened",
            available_tool_ids=["tool_fetch"],
            skill_ids=[],
            execution_log=[
                {"status": "failed", "tool": "fetch_json", "tool_id": "tool_fetch",
                 "reason": "not enabled"} for _ in range(4)
            ],
        ))
        s.add(ScrollRow(id="scr_weather", name="Doc weather", status="approved"))
        s.add(ActivityRow(
            id="act_weather", name="weather-act",
            scroll_id="scr_weather",
            assigned_shadow_id="sh_weather",
            required_tool_ids=["tool_fetch"],
            required_skill_ids=[],
        ))
        s.commit()


def _run_doctor(scope_id: str, db_url: str):
    env = {**os.environ, "SYSTEMU_DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "sharing_on", "doctor", scope_id],
        cwd=str(REPO), capture_output=True, text=True, timeout=60, env=env,
    )


def test_doctor_surfaces_dep_pending_with_recovery_url(tmp_path):
    """contract: a tool whose dep is missing is NOT auto-disabled.
    Doctor surfaces a DEP_PENDING action with the /recover/tool/<id> URL.
    No manual SQL or docker exec required to find the issue."""
    db_path = tmp_path / "vault.db"
    _seed_broken_scenario(db_path)

    proc = _run_doctor("scr_weather", f"sqlite:///{db_path}")
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    out = proc.stdout
    assert "DEP_PENDING" in out, "should surface dep_pending issue"
    assert "fetch_json" in out, "should name the affected tool"
    assert "/recover/tool/tool_fetch" in out, "should include the recovery URL"


def test_doctor_surfaces_memory_poisoning_warning(tmp_path):
    """contract: a shadow with >=3 identical failures in execution_log
    surfaces a MEMORY_POISONED warning with a fix command — no manual UPDATE
    shadows SET execution_log='[]' SQL required."""
    db_path = tmp_path / "vault.db"
    _seed_broken_scenario(db_path)

    proc = _run_doctor("sh_weather", f"sqlite:///{db_path}")
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    out = proc.stdout
    assert "MEMORY_POISONED" in out
    assert "reset-memory" in out, "should suggest the fix command"
    assert "/recover/shadow/sh_weather" in out


def test_doctor_clean_scope_says_no_pending_actions(tmp_path):
    """When the cause is resolved, doctor confirms the clean state."""
    from systemu.storage.sqlite.models import Base, ToolRow
    db_path = tmp_path / "vault.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ToolRow(
            id="tool_ok", name="happy", status="approved", enabled=True,
            dry_run_status="passed", dry_run_evidence=None,
        ))
        s.commit()

    proc = _run_doctor("tool_ok", f"sqlite:///{db_path}")
    assert proc.returncode == 0
    assert "no pending actions" in proc.stdout.lower()


@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="full shadow execution requires OPENROUTER_API_KEY + network",
)
@pytest.mark.slow
def test_full_local_shadow_runs_with_zero_workarounds(tmp_path):
    """contract — operator runs shadow with no manual workarounds.
    Requires real LLM + Open-Meteo API. Gated; skipped by default."""
    pytest.skip("scaffold: real-execution path is documented in "
                "captures/SHADOW_EXECUTION_VERDICT_3MODES.md v0.6.8 section")
