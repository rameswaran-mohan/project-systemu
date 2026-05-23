"""v0.7.1: end-to-end orchestrator for capture session → exported SKILL.md.

Uses monkeypatch.setattr (per CI-stability lesson from
test_v070_memory_backend_mem0.py) to stub Tier 1 calls rather than
patch().return_value chaining.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Scroll, ScrollStatus, Skill


def _make_session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "captures" / "test_session"
    d.mkdir(parents=True)
    (d / "instructions.md").write_text(
        "# Recorded task\n\n1. Open inbox\n2. Summarise top 3 threads\n",
        encoding="utf-8",
    )
    (d / "session.json").write_text(
        json.dumps({
            "name": "email digest",
            "session_id": "cap_test",
            "platform": "win32",
            "start_time": "2026-05-23T10:00:00",
            "end_time": "2026-05-23T10:05:00",
        }),
        encoding="utf-8",
    )
    return d


def test_orchestrator_happy_path(tmp_path, monkeypatch):
    """capture_to_skill.export_skill_from_capture: session dir → bundle dir."""
    from sharing_on.config import Config
    from systemu.pipelines import capture_to_skill

    session_dir = _make_session_dir(tmp_path)
    output_dir = tmp_path / "out"

    fake_scroll = Scroll(
        id="scr_test", source_session_id="cap_test",
        title="Email digest", status=ScrollStatus.APPROVED,
        action_blocks=[], objectives=[],
        name="email_digest",
        raw_instructions_path=str(session_dir / "instructions.md"),
        narrative_md="Open inbox and summarise top 3 threads.",
    )
    fake_skill = Skill(
        id="sk_test", name="email_digest",
        description="Summarise the top emails.",
        category="communication", proficiency_level="intermediate",
        required_tool_names=["fetch_email"],
        instructions_md="## Step 1\n\nFetch.",
        evidence_scroll_ids=["scr_test"],
    )

    vault = MagicMock()
    vault.get_skill.return_value = fake_skill
    vault.list_skills.return_value = []   # No prior skills.
    # Stub refine_scroll to return our scroll without touching LLM router.
    monkeypatch.setattr(
        "systemu.pipelines.capture_to_skill.refine_scroll",
        lambda session_dir, config, vault, **kw: fake_scroll,
    )
    # Stub extract_and_process to return a fake skill_id list.
    monkeypatch.setattr(
        "systemu.pipelines.capture_to_skill.extract_and_process",
        lambda scroll, config, vault: {"skill_ids": ["sk_test"], "tool_ids": []},
    )

    out = capture_to_skill.export_skill_from_capture(
        session_dir=session_dir,
        target_dir=output_dir,
        config=Config(),
        vault=vault,
    )

    assert out == output_dir / "email-digest"
    assert (out / "SKILL.md").exists()


def test_orchestrator_missing_session_json_raises(tmp_path):
    from sharing_on.config import Config
    from systemu.pipelines import capture_to_skill

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="session.json"):
        capture_to_skill.export_skill_from_capture(
            session_dir=empty,
            target_dir=tmp_path / "out",
            config=Config(),
            vault=MagicMock(),
        )


def test_orchestrator_reuses_existing_skill_when_already_extracted(tmp_path, monkeypatch):
    """If the scroll already has a Skill linked via evidence_scroll_ids,
    skip the extraction LLM call and export the existing Skill directly."""
    from sharing_on.config import Config
    from systemu.pipelines import capture_to_skill

    session_dir = _make_session_dir(tmp_path)
    output_dir = tmp_path / "out"

    fake_scroll = Scroll(
        id="scr_test", source_session_id="cap_test",
        title="Email digest", status=ScrollStatus.APPROVED,
        action_blocks=[], objectives=[],
        name="email_digest",
        raw_instructions_path=str(session_dir / "instructions.md"),
        narrative_md="Open inbox and summarise top 3 threads.",
    )
    fake_skill = Skill(
        id="sk_existing", name="email_digest",
        description="Summarise the top emails.",
        category="communication", proficiency_level="intermediate",
        required_tool_names=["fetch_email"],
        instructions_md="## Step 1\n\nFetch.",
        evidence_scroll_ids=["scr_test"],
    )

    vault = MagicMock()
    vault.get_skill.return_value = fake_skill
    vault.list_skills.return_value = [
        {"id": "sk_existing", "name": "email_digest",
         "evidence_scroll_ids": ["scr_test"]},
    ]
    monkeypatch.setattr(
        "systemu.pipelines.capture_to_skill.refine_scroll",
        lambda session_dir, config, vault, **kw: fake_scroll,
    )
    # extract_and_process must NOT be called.
    called = {"n": 0}
    def _should_not_be_called(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(
        "systemu.pipelines.capture_to_skill.extract_and_process",
        _should_not_be_called,
    )

    out = capture_to_skill.export_skill_from_capture(
        session_dir=session_dir,
        target_dir=output_dir,
        config=Config(),
        vault=vault,
    )

    assert called["n"] == 0
    assert (out / "SKILL.md").exists()
