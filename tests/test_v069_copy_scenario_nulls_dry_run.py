"""copying a scenario from one vault to another must NULL out
dry_run_status + dry_run_evidence (env-specific fields)."""
import subprocess
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from systemu.storage.sqlite.models import (
    Base, ScrollRow, ActivityRow, ShadowRow, ToolRow,
)

REPO = Path(__file__).resolve().parents[1]


def test_copy_nulls_dry_run_status_and_evidence(tmp_path):
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{src}"))
    Base.metadata.create_all(create_engine(f"sqlite:///{dst}"))

    with Session(create_engine(f"sqlite:///{src}")) as s:
        s.add(ToolRow(
            id="tool_x", name="fetch_json", status="deployed", enabled=True,
            dry_run_status="failed",
            dry_run_evidence={"error": "ImportError: No module named 'requests'",
                              "classified_reason": "DEP_PENDING",
                              "missing_package": "requests"},
        ))
        s.add(ShadowRow(id="sh1", name="W", status="awakened",
                        available_tool_ids=["tool_x"], skill_ids=[]))
        s.add(ScrollRow(id="scr1", name="s", status="approved"))
        s.add(ActivityRow(id="act1", name="a", scroll_id="scr1",
                          assigned_shadow_id="sh1",
                          required_tool_ids=["tool_x"], required_skill_ids=[]))
        s.commit()

    rc = subprocess.call([
        sys.executable, str(REPO / "scripts" / "copy_shadow_scenario.py"),
        f"sqlite:///{src}", f"sqlite:///{dst}", "scr1", "sh1",
    ])
    assert rc == 0

    with Session(create_engine(f"sqlite:///{dst}")) as s:
        t = s.query(ToolRow).filter_by(id="tool_x").one()
        # The killer assertion: env-specific fields MUST be reset
        assert t.dry_run_status in (None, "not_run"), \
            f"dry_run_status should be NULLed on copy, got {t.dry_run_status!r}"
        assert t.dry_run_evidence in (None, {}), \
            f"dry_run_evidence should be cleared, got {t.dry_run_evidence!r}"
        # Other fields preserved
        assert t.name == "fetch_json"
        assert t.enabled is True


def test_copy_preserves_tool_when_dry_run_already_null(tmp_path):
    """Tools without dry_run state should round-trip unchanged."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{src}"))
    Base.metadata.create_all(create_engine(f"sqlite:///{dst}"))

    with Session(create_engine(f"sqlite:///{src}")) as s:
        s.add(ToolRow(
            id="tool_y", name="t", status="deployed", enabled=True,
            dry_run_status=None, dry_run_evidence=None,
        ))
        s.add(ShadowRow(id="sh1", name="W", status="awakened",
                        available_tool_ids=["tool_y"], skill_ids=[]))
        s.add(ScrollRow(id="scr1", name="s", status="approved"))
        s.add(ActivityRow(id="act1", name="a", scroll_id="scr1",
                          assigned_shadow_id="sh1",
                          required_tool_ids=["tool_y"], required_skill_ids=[]))
        s.commit()

    rc = subprocess.call([
        sys.executable, str(REPO / "scripts" / "copy_shadow_scenario.py"),
        f"sqlite:///{src}", f"sqlite:///{dst}", "scr1", "sh1",
    ])
    assert rc == 0

    with Session(create_engine(f"sqlite:///{dst}")) as s:
        t = s.query(ToolRow).filter_by(id="tool_y").one()
        assert t.dry_run_status in (None, "not_run")
        assert t.dry_run_evidence in (None, {})
