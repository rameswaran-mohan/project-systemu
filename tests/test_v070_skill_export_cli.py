import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    from systemu.storage.sqlite.models import Base, SkillRow
    url = f"sqlite:///{tmp_path / 'v.db'}"
    eng = create_engine(url)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(SkillRow(
            id="skill_x", name="email_summary",
            description="Summarize email threads",
            category="communication", proficiency_level="intermediate",
            required_tool_names=["fetch_email"],
            instructions_md="## Step 1\n\nDo a thing.",
        ))
        s.commit()
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", url)
    return url


def test_skills_export_writes_conformant_bundle(db, tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, "-m", "sharing_on", "skills", "export",
         "skill_x", "--output", str(out)],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
        env={**os.environ, "SYSTEMU_DATABASE_URL": db,
             # v0.7-a-fix5: open_vault() defaults to SYSTEMU_STORAGE=file
             # when unset (no .env on CI), so the subprocess opens a
             # file-backend vault rather than the SQLite test fixture.
             "SYSTEMU_STORAGE": "sqlite"},
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "email-summary" / "SKILL.md").exists()


def test_skills_export_unknown_id_exits_nonzero(db, tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "sharing_on", "skills", "export",
         "skill_nope", "--output", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
        env={**os.environ, "SYSTEMU_DATABASE_URL": db,
             # v0.7-a-fix5: open_vault() defaults to SYSTEMU_STORAGE=file
             # when unset (no .env on CI), so the subprocess opens a
             # file-backend vault rather than the SQLite test fixture.
             "SYSTEMU_STORAGE": "sqlite"},
    )
    assert proc.returncode != 0
    assert "not found" in proc.stderr.lower()
