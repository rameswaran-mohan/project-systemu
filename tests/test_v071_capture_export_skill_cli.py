"""v0.7.1: sharing_on capture export-skill <session> --output <dir>.

Subprocess test (same pattern as tests/test_v070_skill_export_cli.py)
that exercises the full Click → orchestrator wiring.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session_dir(tmp_path):
    d = tmp_path / "captures" / "email_digest_cap_test"
    d.mkdir(parents=True)
    (d / "instructions.md").write_text(
        "# Email digest\n\n1. Open inbox\n2. Summarise top threads\n",
        encoding="utf-8",
    )
    (d / "session.json").write_text(json.dumps({
        "name": "email digest",
        "session_id": "cap_test",
        "platform": "win32",
        "start_time": "2026-05-23T10:00:00",
        "end_time": "2026-05-23T10:05:00",
    }), encoding="utf-8")
    return d


@pytest.fixture
def db(tmp_path, monkeypatch):
    """SqliteVault pre-loaded with one Skill+Scroll linked via evidence."""
    from systemu.storage.sqlite.models import (
        Base, SkillRow, ScrollRow,
    )
    url = f"sqlite:///{tmp_path / 'v.db'}"
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ScrollRow(
            id="scr_test", source_session_id="cap_test",
            name="Email digest", status="approved",
        ))
        s.add(SkillRow(
            id="sk_test", name="email_digest",
            description="Summarise top emails.",
            category="communication", proficiency_level="intermediate",
            required_tool_names=["fetch_email"],
            evidence_scroll_ids=["scr_test"],
            instructions_md="## Step 1\n\nFetch.",
        ))
        s.commit()
    return url


def test_capture_export_skill_happy_path(session_dir, db, tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, "-m", "sharing_on",
         "capture", "export-skill", str(session_dir), "--output", str(out)],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
        env={**os.environ,
             "SYSTEMU_DATABASE_URL": db,
             "SYSTEMU_STORAGE": "sqlite",
             # No OpenRouter key needed — the existing scroll+skill mean
             # neither refine_scroll nor extract_and_process makes an LLM call.
             # (refine_scroll dedupes on session_id; the skill is pre-linked.)
             "SYSTEMU_HEADLESS": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "email-digest" / "SKILL.md").exists()


def test_capture_export_skill_missing_session_json_exits_2(tmp_path):
    empty = tmp_path / "empty_session"
    empty.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "sharing_on",
         "capture", "export-skill", str(empty), "--output", str(tmp_path / "o")],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
        env={**os.environ, "SYSTEMU_STORAGE": "sqlite"},
    )
    assert proc.returncode == 2
    assert "session.json" in proc.stderr or "instructions.md" in proc.stderr
