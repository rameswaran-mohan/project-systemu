"""v0.9.4 L5 Recipe Fast-Paths — SkillManifest + skill_loader tests."""
import os
from pathlib import Path
from unittest.mock import patch
import pytest

from sharing_on.config import Config
from systemu.core.models import SkillManifest


class TestSkillManifestModel:
    def _make(self, **overrides):
        kwargs = dict(
            name="burrito-delivery",
            description="Find and rank burrito places in a given city",
            version="1.0.0",
            platforms=["linux", "macos", "windows"],
            tags=["food", "scraping"],
            related_skills=["restaurant-research"],
            prerequisites_commands=["curl"],
            requires_toolsets=["file", "web"],
            fallback_for_toolsets=[],
            body="# Burrito Delivery\n\n## When to Use\n\n## Procedure\n",
        )
        kwargs.update(overrides)
        return SkillManifest(**kwargs)

    def test_minimal_construction(self):
        s = self._make()
        assert s.name == "burrito-delivery"
        assert s.version == "1.0.0"

    def test_defaults_for_optional_lists(self):
        s = SkillManifest(name="x", description="d", version="1.0", body="b")
        assert s.platforms == []
        assert s.tags == []
        assert s.related_skills == []
        assert s.prerequisites_commands == []
        assert s.requires_toolsets == []
        assert s.fallback_for_toolsets == []

    def test_json_round_trip(self):
        s = self._make()
        rebuilt = SkillManifest.model_validate_json(s.model_dump_json())
        assert rebuilt.name == s.name
        assert rebuilt.requires_toolsets == s.requires_toolsets
        assert rebuilt.body == s.body


class TestConfigSkillFields:
    _KEYS = (
        "SYSTEMU_SKILL_LOADER_ENABLED",
        "SYSTEMU_SKILLS_BUNDLED_DIR",
        "SYSTEMU_SKILLS_USER_DIR",
    )

    def test_defaults(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        assert cfg.skill_loader_enabled is True
        assert "systemu/skills" in cfg.skills_bundled_dir.replace("\\\\", "/")
        assert cfg.skills_user_dir == ""

    def test_env_overrides(self):
        env = {
            "SYSTEMU_SKILL_LOADER_ENABLED": "false",
            "SYSTEMU_SKILLS_BUNDLED_DIR": "/custom/skills",
            "SYSTEMU_SKILLS_USER_DIR": "/home/user/.systemu/skills",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env()
        assert cfg.skill_loader_enabled is False
        assert cfg.skills_bundled_dir == "/custom/skills"
        assert cfg.skills_user_dir == "/home/user/.systemu/skills"


class TestElderIntakeAntiBotNudge:
    """Guard that the elder_intake.md prompt instructs the LLM to follow
    the anti_bot_blocked retry hint instead of faking completion."""

    def test_prompt_mentions_anti_bot_blocked(self):
        from pathlib import Path
        p = Path("systemu/prompts/elder_intake.md")
        content = p.read_text(encoding="utf-8")
        assert "anti_bot_blocked" in content, (
            "elder_intake.md must mention 'anti_bot_blocked' so the LLM "
            "is nudged to retry on web_extract anti-bot responses"
        )

    def test_prompt_mentions_duckduckgo_or_google_retry(self):
        from pathlib import Path
        content = Path("systemu/prompts/elder_intake.md").read_text(encoding="utf-8")
        assert "duckduckgo" in content.lower() or "google.com/search" in content.lower()

    def test_prompt_says_do_not_fake(self):
        from pathlib import Path
        content = Path("systemu/prompts/elder_intake.md").read_text(encoding="utf-8")
        # The prompt should explicitly tell the LLM NOT to claim completion on these errors.
        lower = content.lower()
        assert "do not" in lower and ("complete" in lower or "claim" in lower)


class TestSkillLoaderParse:
    def test_parses_yaml_frontmatter_and_body(self, tmp_path):
        from systemu.runtime.skill_loader import parse_skill_md
        skill_dir = tmp_path / "demo-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\n"
            "name: demo-skill\n"
            "description: Demo for testing\n"
            "version: 1.0.0\n"
            "platforms: [linux, macos, windows]\n"
            "metadata:\n"
            "  systemu:\n"
            "    tags: [demo, test]\n"
            "    related_skills: [other-skill]\n"
            "prerequisites:\n"
            "  commands: [curl, jq]\n"
            "requires_toolsets: [file, web]\n"
            "fallback_for_toolsets: []\n"
            "---\n"
            "# Demo Skill\n"
            "\n"
            "## When to Use\n"
            "Body content here.\n",
            encoding="utf-8",
        )
        manifest = parse_skill_md(skill_md)
        assert manifest.name == "demo-skill"
        assert manifest.description == "Demo for testing"
        assert manifest.version == "1.0.0"
        assert manifest.platforms == ["linux", "macos", "windows"]
        assert manifest.tags == ["demo", "test"]
        assert manifest.related_skills == ["other-skill"]
        assert manifest.prerequisites_commands == ["curl", "jq"]
        assert manifest.requires_toolsets == ["file", "web"]
        assert "# Demo Skill" in manifest.body
        assert "Body content here." in manifest.body
        assert manifest.source_path == str(skill_md)

    def test_rejects_missing_frontmatter(self, tmp_path):
        from systemu.runtime.skill_loader import parse_skill_md, SkillManifestError
        p = tmp_path / "bad.md"
        p.write_text("# Just markdown, no frontmatter\n", encoding="utf-8")
        with pytest.raises(SkillManifestError):
            parse_skill_md(p)

    def test_rejects_missing_required_field(self, tmp_path):
        from systemu.runtime.skill_loader import parse_skill_md, SkillManifestError
        p = tmp_path / "bad.md"
        p.write_text(
            "---\nname: foo\nversion: 1.0\n---\n# Body\n",
            encoding="utf-8",
        )
        # missing description
        with pytest.raises(SkillManifestError):
            parse_skill_md(p)


class TestSkillLoaderDiscover:
    def test_discover_finds_skills_in_directory(self, tmp_path):
        from systemu.runtime.skill_loader import discover_skills
        # Create two skills + one non-skill file
        for name in ("alpha", "beta"):
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: d\nversion: 1.0\n---\n# {name}\n",
                encoding="utf-8",
            )
        # Random file that should be ignored
        (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")

        manifests = discover_skills(tmp_path)
        names = sorted(m.name for m in manifests)
        assert names == ["alpha", "beta"]

    def test_discover_empty_dir_returns_empty(self, tmp_path):
        from systemu.runtime.skill_loader import discover_skills
        out = discover_skills(tmp_path)
        assert out == []

    def test_discover_nonexistent_dir_returns_empty(self, tmp_path):
        from systemu.runtime.skill_loader import discover_skills
        out = discover_skills(tmp_path / "does-not-exist")
        assert out == []


class TestSkillLoaderGating:
    def test_check_prerequisites_ok_when_empty(self):
        from systemu.runtime.skill_loader import check_prerequisites
        manifest_dict = {"prerequisites_commands": []}
        assert check_prerequisites(manifest_dict) is True

    def test_check_prerequisites_ok_when_python_present(self):
        """python is always present in our test env."""
        from systemu.runtime.skill_loader import check_prerequisites
        manifest_dict = {"prerequisites_commands": ["python"]}
        assert check_prerequisites(manifest_dict) is True

    def test_check_prerequisites_false_when_missing(self):
        from systemu.runtime.skill_loader import check_prerequisites
        manifest_dict = {"prerequisites_commands": ["definitely-not-a-real-binary-xyz"]}
        assert check_prerequisites(manifest_dict) is False

    def test_check_toolsets_ok_when_all_present(self):
        from systemu.runtime.skill_loader import check_toolsets
        available = {"file", "web", "vault"}
        manifest_dict = {"requires_toolsets": ["file", "web"]}
        assert check_toolsets(manifest_dict, available) is True

    def test_check_toolsets_false_when_missing(self):
        from systemu.runtime.skill_loader import check_toolsets
        available = {"file"}  # web not available
        manifest_dict = {"requires_toolsets": ["file", "web"]}
        assert check_toolsets(manifest_dict, available) is False

    def test_check_toolsets_ok_when_no_requirements(self):
        from systemu.runtime.skill_loader import check_toolsets
        manifest_dict = {"requires_toolsets": []}
        assert check_toolsets(manifest_dict, set()) is True


class TestLoadAllSkills:
    def test_load_all_combines_bundled_and_user(self, tmp_path):
        from systemu.runtime.skill_loader import load_all_skills
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        user = tmp_path / "user"
        user.mkdir()
        (bundled / "alpha").mkdir()
        (bundled / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: d\nversion: 1.0\n---\n# alpha\n",
            encoding="utf-8",
        )
        (user / "beta").mkdir()
        (user / "beta" / "SKILL.md").write_text(
            "---\nname: beta\ndescription: d\nversion: 1.0\n---\n# beta\n",
            encoding="utf-8",
        )
        all_skills = load_all_skills(str(bundled), str(user))
        names = sorted(m.name for m in all_skills)
        assert names == ["alpha", "beta"]

    def test_load_all_empty_paths(self, tmp_path):
        from systemu.runtime.skill_loader import load_all_skills
        out = load_all_skills("", "")
        assert out == []


class TestBundledSkills:
    """v0.9.6 architectural invariant: systemu/skills/ ships near-empty by design.

    Skills are EARNED via auto_skill_extractor (Odysseus pattern), not
    PRESCRIBED. Earlier overfit examples (burrito-delivery, find-nearby) have
    been moved to docs/skill-examples/ as format references.
    """

    def test_bundled_dir_has_readme_explaining_design(self):
        from pathlib import Path
        readme = Path("systemu/skills/README.md")
        assert readme.exists(), (
            "systemu/skills/ must include a README.md explaining that the "
            "directory ships near-empty by design and skills are earned via "
            "auto_skill_extractor (Odysseus pattern), not bundled by us."
        )
        content = readme.read_text(encoding="utf-8").lower()
        assert "auto" in content and ("extract" in content or "earned" in content)

    def test_overfit_examples_moved_to_docs(self):
        from pathlib import Path
        # burrito-delivery and find-nearby should NOT be in bundled dir
        assert not Path("systemu/skills/burrito-delivery").exists(), (
            "burrito-delivery is overfit — move to docs/skill-examples/"
        )
        assert not Path("systemu/skills/find-nearby").exists(), (
            "find-nearby is overfit — move to docs/skill-examples/"
        )
        # But the format references should be available
        assert Path("docs/skill-examples/burrito-delivery-SKILL.md").exists(), (
            "Keep burrito-delivery as a format reference in docs/skill-examples/"
        )

    def test_summarize_page_still_bundled_as_truly_generic(self):
        """summarize-page is generic enough to bundle — works for any URL."""
        from pathlib import Path
        from systemu.runtime.skill_loader import parse_skill_md
        p = Path("systemu/skills/summarize-page/SKILL.md")
        assert p.exists()
        m = parse_skill_md(p)
        assert m.name == "summarize-page"

    def test_discover_skills_returns_summarize_page_only(self):
        from systemu.runtime.skill_loader import discover_skills
        manifests = discover_skills("systemu/skills")
        names = {m.name for m in manifests}
        # Truly generic only
        assert "summarize-page" in names
        # Overfit examples should NOT be discoverable in bundled dir
        assert "burrito-delivery" not in names
        assert "find-nearby" not in names

    def test_format_reference_files_parse_cleanly(self):
        """The format-reference SKILL.md files in docs/ should still be
        parseable so they remain a valid spec for operators copying the
        format into their own skill libraries."""
        from pathlib import Path
        from systemu.runtime.skill_loader import parse_skill_md
        for name in ("burrito-delivery-SKILL.md", "find-nearby-SKILL.md"):
            p = Path("docs/skill-examples") / name
            assert p.exists()
            m = parse_skill_md(p)
            assert m.name in ("burrito-delivery", "find-nearby")
            assert m.description


class TestCliSkill:
    def _set_skills_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SYSTEMU_SKILLS_BUNDLED_DIR", str(tmp_path))
        d = tmp_path / "demo-skill"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: A demo for CLI test\nversion: 1.0\n"
            "metadata:\n  systemu:\n    tags: [test, demo]\n"
            "requires_toolsets: [file]\n---\n# Demo Skill\n\n## When to Use\n\nTest body.\n",
            encoding="utf-8",
        )

    def test_skill_list(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import skill_cli
        self._set_skills_dir(monkeypatch, tmp_path)
        result = CliRunner().invoke(skill_cli, ["list"])
        assert result.exit_code == 0, f"stdout={result.output!r}"
        assert "demo-skill" in result.output

    def test_skill_view(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import skill_cli
        self._set_skills_dir(monkeypatch, tmp_path)
        result = CliRunner().invoke(skill_cli, ["view", "demo-skill"])
        assert result.exit_code == 0, f"stdout={result.output!r}"
        assert "demo-skill" in result.output
        assert "A demo for CLI test" in result.output
        assert "# Demo Skill" in result.output  # body included

    def test_skill_view_not_found(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import skill_cli
        self._set_skills_dir(monkeypatch, tmp_path)
        result = CliRunner().invoke(skill_cli, ["view", "nonexistent"])
        assert result.exit_code == 0  # CLI shouldn't error, just say not found
        assert "nonexistent" in result.output.lower() or "not found" in result.output.lower()


class TestSkillTools:
    """LLM-facing tools surface SKILL.md recipes via v0.9.3 v2 registry."""

    def _set_skills_dir(self, monkeypatch, tmp_path):
        """Point the loader at a tmp_path containing a small skill."""
        monkeypatch.setenv("SYSTEMU_SKILLS_BUNDLED_DIR", str(tmp_path))
        d = tmp_path / "demo-skill"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\nversion: 1.0\n"
            "metadata:\n  systemu:\n    tags: [test]\n"
            "requires_toolsets: [file]\n---\n# Demo\n\nBody here\n",
            encoding="utf-8",
        )

    def test_skill_list_skills_returns_dicts(self, tmp_path, monkeypatch):
        from systemu.runtime.tools.skill_tools import skill_list_skills
        self._set_skills_dir(monkeypatch, tmp_path)
        from sharing_on.config import Config
        cfg = Config.from_env()
        results = skill_list_skills(config=cfg)
        assert isinstance(results, list)
        assert len(results) >= 1
        names = [r["name"] for r in results]
        assert "demo-skill" in names
        for r in results:
            assert "name" in r and "description" in r and "version" in r

    def test_skill_view_skill_returns_full_manifest(self, tmp_path, monkeypatch):
        from systemu.runtime.tools.skill_tools import skill_view_skill
        self._set_skills_dir(monkeypatch, tmp_path)
        from sharing_on.config import Config
        cfg = Config.from_env()
        result = skill_view_skill(name="demo-skill", config=cfg)
        assert result is not None
        assert result["name"] == "demo-skill"
        assert "body" in result
        assert "# Demo" in result["body"]

    def test_skill_view_skill_returns_none_for_unknown(self, tmp_path, monkeypatch):
        from systemu.runtime.tools.skill_tools import skill_view_skill
        self._set_skills_dir(monkeypatch, tmp_path)
        from sharing_on.config import Config
        cfg = Config.from_env()
        result = skill_view_skill(name="totally-not-a-skill", config=cfg)
        assert result is None

    def test_skill_loader_disabled_returns_empty(self, tmp_path, monkeypatch):
        from systemu.runtime.tools.skill_tools import skill_list_skills
        self._set_skills_dir(monkeypatch, tmp_path)
        monkeypatch.setenv("SYSTEMU_SKILL_LOADER_ENABLED", "false")
        from sharing_on.config import Config
        cfg = Config.from_env()
        assert skill_list_skills(config=cfg) == []


class TestSkillToolsRegistered:
    """The two skill tools must be registered in the v2 tool registry."""

    def test_skill_list_skills_registered(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        # Force-import the skill_tools module so the register() calls fire.
        import systemu.runtime.tools.skill_tools  # noqa: F401
        entry = singleton.get("skill_list_skills")
        assert entry is not None
        assert entry.toolset == "skill"

    def test_skill_view_skill_registered(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.skill_tools  # noqa: F401
        entry = singleton.get("skill_view_skill")
        assert entry is not None
        assert entry.toolset == "skill"
