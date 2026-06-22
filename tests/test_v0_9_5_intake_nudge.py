"""v0.9.5 elder_intake.md prompts skill discovery."""
from pathlib import Path


def test_prompt_mentions_skill_list_skills():
    content = Path("systemu/prompts/elder_intake.md").read_text(encoding="utf-8")
    assert "skill_list_skills" in content


def test_prompt_mentions_skill_view_skill():
    content = Path("systemu/prompts/elder_intake.md").read_text(encoding="utf-8")
    assert "skill_view_skill" in content


def test_prompt_describes_recipe_workflow():
    content = Path("systemu/prompts/elder_intake.md").read_text(encoding="utf-8")
    lower = content.lower()
    assert "procedure" in lower and "recipe" in lower
