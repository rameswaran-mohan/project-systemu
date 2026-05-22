"""regression: ``scripts/copy_shadow_scenario.py`` must also copy
the skills referenced by the source shadow / activity.

Previously the script copied the shadow, scroll, activity, and required tools,
but silently dropped any skills the shadow references — the destination shadow
then warned "Skill skill_X not found in vault" at execution time, forcing the
operator into a manual workaround.
"""
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from systemu.storage.sqlite.models import (
    ActivityRow,
    Base,
    ScrollRow,
    ShadowRow,
    SkillRow,
    ToolRow,
)

REPO = Path(__file__).resolve().parents[1]


def _setup_db(path: Path):
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    return eng


def test_copy_scenario_includes_skill(tmp_path):
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    src_eng = _setup_db(src)
    _setup_db(dst)

    with Session(src_eng) as s:
        s.add(SkillRow(
            id="skill_x",
            name="weather_data_collection",
            effectiveness_score=0.9,
        ))
        s.add(ToolRow(
            id="tool_a", name="fetch_json", enabled=True, status="approved",
        ))
        s.add(ShadowRow(
            id="sh1",
            name="W",
            status="awakened",
            available_tool_ids=["tool_a"],
            skill_ids=["skill_x"],
        ))
        s.add(ScrollRow(id="scr1", name="x", status="approved"))
        s.add(ActivityRow(
            id="act1",
            name="act-1",
            scroll_id="scr1",
            assigned_shadow_id="sh1",
            required_tool_ids=["tool_a"],
            required_skill_ids=["skill_x"],
        ))
        s.commit()

    rc = subprocess.call(
        [
            sys.executable,
            str(REPO / "scripts" / "copy_shadow_scenario.py"),
            f"sqlite:///{src}",
            f"sqlite:///{dst}",
            "scr1",
            "sh1",
        ],
        timeout=30,
    )
    assert rc == 0

    dst_eng = create_engine(f"sqlite:///{dst}")
    with Session(dst_eng) as s:
        skills = s.query(SkillRow).all()
    assert any(k.id == "skill_x" for k in skills), (
        "v0.6.8-f: copy_shadow_scenario.py must copy referenced skills"
    )
