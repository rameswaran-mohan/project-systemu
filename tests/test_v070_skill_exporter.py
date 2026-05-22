from pathlib import Path
from unittest.mock import MagicMock
import yaml
import pytest

from systemu.pipelines.skill_exporter import export_skill


def _fake_skill(id="skill_x", name="email_summary", description="Summarize email threads"):
    s = MagicMock()
    s.id = id
    s.name = name
    s.description = description
    s.category = "communication"
    s.proficiency_level = "intermediate"
    s.required_tools = ["fetch_email", "create_word_doc"]
    s.instructions_md = "## How to summarize\n\nStep 1: ...\n"
    return s


def test_export_creates_kebab_named_directory(tmp_path):
    vault = MagicMock()
    vault.get_skill.return_value = _fake_skill()
    out = export_skill(skill_id="skill_x", target_dir=tmp_path, vault=vault)
    assert out == tmp_path / "email-summary"
    assert (out / "SKILL.md").exists()


def test_export_writes_spec_conformant_frontmatter(tmp_path):
    vault = MagicMock()
    vault.get_skill.return_value = _fake_skill()
    out = export_skill(skill_id="skill_x", target_dir=tmp_path, vault=vault)
    text = (out / "SKILL.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["name"] == "email-summary"
    assert fm["description"] == "Summarize email threads"
    assert "metadata" in fm
    assert fm["metadata"]["category"] == "communication"
    assert "category" not in fm  # not at top level


def test_export_preserves_instructions_in_body(tmp_path):
    vault = MagicMock()
    vault.get_skill.return_value = _fake_skill()
    out = export_skill(skill_id="skill_x", target_dir=tmp_path, vault=vault)
    text = (out / "SKILL.md").read_text(encoding="utf-8")
    assert "How to summarize" in text


def test_export_raises_on_missing_skill(tmp_path):
    vault = MagicMock()
    vault.get_skill.side_effect = KeyError("not found")
    with pytest.raises(KeyError):
        export_skill(skill_id="skill_missing", target_dir=tmp_path, vault=vault)


def test_export_collision_refuses_to_clobber(tmp_path):
    (tmp_path / "email-summary").mkdir()
    (tmp_path / "email-summary" / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\n---\n", encoding="utf-8"
    )
    vault = MagicMock()
    vault.get_skill.return_value = _fake_skill()
    with pytest.raises(FileExistsError):
        export_skill(skill_id="skill_x", target_dir=tmp_path, vault=vault)
