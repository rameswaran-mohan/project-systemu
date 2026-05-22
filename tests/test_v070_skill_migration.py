"""skill layout migrator transforms Systemu-internal skills to
spec-conformant Anthropic Agent Skills format."""
from pathlib import Path
import pytest
import yaml

from systemu.storage.skill_migrator import migrate_skill_layout


def _seed(tmp_path: Path, dirname: str, frontmatter: dict, body: str = "Body.") -> Path:
    skill_dir = tmp_path / "skills" / dirname
    skill_dir.mkdir(parents=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body,
        encoding="utf-8",
    )
    return skill_dir


def test_renames_dir_and_kebabs_name(tmp_path):
    _seed(tmp_path, "skill_skill_abc12345",
          {"name": "email_thread_management",
           "description": "Manage email threads",
           "category": "communication"})
    report = migrate_skill_layout(tmp_path)
    assert (tmp_path / "skills" / "email-thread-management" / "SKILL.md").exists()
    assert not (tmp_path / "skills" / "skill_skill_abc12345").exists()
    assert report.migrated == 1


def test_moves_non_spec_fields_to_metadata(tmp_path):
    _seed(tmp_path, "skill_skill_def67890",
          {"name": "foo",
           "description": "d",
           "category": "communication",
           "proficiency_level": "intermediate",
           "required_tools": ["x", "y"]})
    migrate_skill_layout(tmp_path)
    md_text = (tmp_path / "skills" / "foo" / "SKILL.md").read_text("utf-8")
    fm = yaml.safe_load(md_text.split("---")[1])
    assert set(fm.keys()) == {"name", "description", "metadata"}
    assert fm["metadata"]["category"] == "communication"
    assert fm["metadata"]["proficiency_level"] == "intermediate"
    assert fm["metadata"]["required_tools"] == ["x", "y"]


def test_idempotent_when_already_conformant(tmp_path):
    _seed(tmp_path, "already-conformant",
          {"name": "already-conformant",
           "description": "d",
           "metadata": {"category": "x"}})
    r1 = migrate_skill_layout(tmp_path)
    r2 = migrate_skill_layout(tmp_path)
    assert r1.migrated == 0 and r2.migrated == 0
    assert r1.skipped >= 1


def test_preserves_body_content(tmp_path):
    _seed(tmp_path, "skill_skill_aaa11111",
          {"name": "foo_bar", "description": "d"},
          body="## How to use\n\nDetailed instructions here.\n")
    migrate_skill_layout(tmp_path)
    text = (tmp_path / "skills" / "foo-bar" / "SKILL.md").read_text("utf-8")
    assert "How to use" in text
    assert "Detailed instructions here." in text


def test_collision_keeps_existing_and_logs(tmp_path):
    """If migration target already exists, skip + report (don't clobber)."""
    _seed(tmp_path, "foo-bar", {"name": "foo-bar", "description": "old"})
    _seed(tmp_path, "skill_skill_bbb22222", {"name": "foo_bar", "description": "new"})
    report = migrate_skill_layout(tmp_path)
    assert report.collisions == 1
    text = (tmp_path / "skills" / "foo-bar" / "SKILL.md").read_text("utf-8")
    assert "old" in text


def test_returns_report_with_all_counts(tmp_path):
    _seed(tmp_path, "skill_skill_ccc33333", {"name": "needs_kebab", "description": "d"})
    _seed(tmp_path, "already-fine",
          {"name": "already-fine", "description": "d", "metadata": {}})
    report = migrate_skill_layout(tmp_path)
    assert report.migrated == 1
    assert report.skipped >= 1
    assert report.collisions == 0
