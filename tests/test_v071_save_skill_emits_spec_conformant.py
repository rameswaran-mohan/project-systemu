"""v0.7.1: vault.save_skill() emits spec-conformant SKILL.md natively.

Previously: skills landed in skills/skill_<id>/SKILL.md with top-level
category/proficiency_level/required_tools and required a daemon-boot
migration. Now save_skill() writes to skills/<kebab-name>/SKILL.md with
only name+description top-level and a metadata: block.
"""
from pathlib import Path

import pytest
import yaml

from systemu.core.models import Skill
from systemu.vault.vault import Vault


def _make_vault(tmp_path: Path) -> Vault:
    """Build a file-backed Vault rooted at tmp_path."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "scrolls").mkdir()
    (tmp_path / "activities").mkdir()
    (tmp_path / "shadows").mkdir()
    return Vault(root=tmp_path)


def _make_skill(**overrides) -> Skill:
    defaults = dict(
        id="abc123",
        name="email_thread_summary",
        description="Summarize email threads into bullet points.",
        category="communication",
        proficiency_level="intermediate",
        required_tool_names=["fetch_email", "summarize_text"],
        instructions_md="## Step 1\n\nFetch the thread.",
        evidence_scroll_ids=["scr_xyz"],
    )
    defaults.update(overrides)
    return Skill(**defaults)


def test_save_skill_writes_kebab_dir(tmp_path):
    vault = _make_vault(tmp_path)
    vault.save_skill(_make_skill())
    assert (tmp_path / "skills" / "email-thread-summary" / "SKILL.md").exists()
    # Old layout MUST NOT be created.
    assert not (tmp_path / "skills" / "skill_abc123").exists()


def test_save_skill_frontmatter_is_spec_conformant(tmp_path):
    vault = _make_vault(tmp_path)
    vault.save_skill(_make_skill())
    md_path = tmp_path / "skills" / "email-thread-summary" / "SKILL.md"
    text = md_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    # Top-level: ONLY name + description + metadata.
    assert set(fm.keys()) <= {"name", "description", "metadata"}
    assert fm["name"] == "email-thread-summary"
    assert fm["description"].startswith("Summarize")
    # Internal fields live under metadata.
    assert fm["metadata"]["category"] == "communication"
    assert fm["metadata"]["proficiency_level"] == "intermediate"
    assert fm["metadata"]["required_tools"] == ["fetch_email", "summarize_text"]


def test_save_skill_no_compatibility_admission(tmp_path):
    """The 'A future release will emit fully spec-conformant...' template
    line must not appear in any newly-written SKILL.md."""
    vault = _make_vault(tmp_path)
    vault.save_skill(_make_skill())
    md_path = tmp_path / "skills" / "email-thread-summary" / "SKILL.md"
    text = md_path.read_text(encoding="utf-8")
    assert "future release will emit" not in text.lower()
    assert "compatibility:" not in text.lower()


def test_save_skill_rename_moves_dir(tmp_path):
    """If a skill's name changes, the on-disk dir is renamed to match."""
    vault = _make_vault(tmp_path)
    skill = _make_skill()
    vault.save_skill(skill)
    assert (tmp_path / "skills" / "email-thread-summary").exists()
    skill.name = "email_digest"
    vault.save_skill(skill)
    assert (tmp_path / "skills" / "email-digest").exists()
    assert not (tmp_path / "skills" / "email-thread-summary").exists()


def test_save_skill_collision_suffixes_dir(tmp_path):
    """Two skills with the same kebab-name get suffix disambiguation."""
    vault = _make_vault(tmp_path)
    vault.save_skill(_make_skill(id="abc123", name="archive_management"))
    vault.save_skill(_make_skill(
        id="def456", name="archive_management",
        description="Different skill, same name.",
    ))
    skills_dir = tmp_path / "skills"
    children = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
    # First wins the clean name, second is suffixed.
    assert "archive-management" in children
    assert any(c.startswith("archive-management-") for c in children)


def test_save_skill_idempotent_rewrite(tmp_path):
    """Re-saving the same skill is a content rewrite, not a dir clobber."""
    vault = _make_vault(tmp_path)
    skill = _make_skill()
    vault.save_skill(skill)
    skill.instructions_md = "## Step 1\n\nFetch the thread.\n\n## Step 2\n\nSummarise."
    vault.save_skill(skill)
    md_path = tmp_path / "skills" / "email-thread-summary" / "SKILL.md"
    assert "Step 2" in md_path.read_text(encoding="utf-8")
    # Dir count unchanged.
    skills_root = tmp_path / "skills"
    assert sum(1 for p in skills_root.iterdir() if p.is_dir()) == 1
