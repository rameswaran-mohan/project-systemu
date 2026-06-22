"""v0.9.6 L7 auto_skill_extractor tests."""
from pathlib import Path
from unittest.mock import patch
import pytest


class TestExtractSkillCandidate:
    def test_skips_when_too_few_rounds_and_tools(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_skill_candidate
        called = []
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: called.append(1) or None,
        )
        from sharing_on.config import Config
        result = extract_skill_candidate(
            intent="x", chat_result=None, n_rounds=1, n_tool_calls=1,
            tools_called=["t1"], config=Config(),
        )
        # Below threshold — no LLM call, no skill
        assert result is None
        assert called == []

    def test_runs_when_threshold_met_2_rounds(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_skill_candidate
        def fake_call(**kw):
            return {
                "name": "test-skill",
                "description": "A test skill",
                "procedure": ["step 1", "step 2"],
                "pitfalls": [],
                "confidence": 0.8,
            }
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json", fake_call,
        )
        from sharing_on.config import Config
        result = extract_skill_candidate(
            intent="do thing X", chat_result="done", n_rounds=2, n_tool_calls=2,
            tools_called=["t1", "t2"], config=Config(),
        )
        assert result is not None
        assert result["name"] == "test-skill"
        assert result["confidence"] == 0.8

    def test_runs_when_threshold_met_2_tools(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_skill_candidate
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: {"name": "tool-skill", "description": "d", "procedure": ["x"],
                          "pitfalls": [], "confidence": 0.7},
        )
        from sharing_on.config import Config
        result = extract_skill_candidate(
            intent="x", chat_result=None, n_rounds=1, n_tool_calls=2,
            tools_called=["a", "b"], config=Config(),
        )
        assert result is not None

    def test_rejects_low_confidence(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_skill_candidate
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: {"name": "t", "description": "d", "procedure": ["x"],
                          "pitfalls": [], "confidence": 0.3},
        )
        from sharing_on.config import Config
        result = extract_skill_candidate(
            intent="x", chat_result=None, n_rounds=3, n_tool_calls=3,
            tools_called=["t1"], config=Config(),
        )
        # Below MIN_CONFIDENCE — return None
        assert result is None

    def test_handles_llm_failure_gracefully(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_skill_candidate
        def boom(**kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json", boom,
        )
        from sharing_on.config import Config
        result = extract_skill_candidate(
            intent="x", chat_result=None, n_rounds=3, n_tool_calls=3,
            tools_called=["t1"], config=Config(),
        )
        # Must not raise
        assert result is None

    def test_disabled_returns_none(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_skill_candidate
        called = []
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: called.append(1) or {},
        )
        from sharing_on.config import Config
        cfg = Config()
        cfg.auto_skill_extract_enabled = False
        result = extract_skill_candidate(
            intent="x", chat_result=None, n_rounds=3, n_tool_calls=3,
            tools_called=["t1"], config=cfg,
        )
        assert result is None
        assert called == []


class TestPersistSkillCandidate:
    def test_writes_skill_md(self, tmp_path):
        from systemu.runtime.auto_skill_extractor import persist_skill_candidate
        candidate = {
            "name": "demo-extracted",
            "description": "A demo extracted skill",
            "procedure": ["step 1", "step 2"],
            "pitfalls": ["watch out for X"],
            "confidence": 0.8,
        }
        path = persist_skill_candidate(candidate, skills_dir=str(tmp_path))
        assert path is not None
        skill_md = Path(path)
        assert skill_md.exists()
        content = skill_md.read_text(encoding="utf-8")
        # Must be valid YAML frontmatter + body
        assert content.startswith("---")
        assert "name: demo-extracted" in content
        assert "step 1" in content

    def test_skill_md_parses_back_via_skill_loader(self, tmp_path):
        from systemu.runtime.auto_skill_extractor import persist_skill_candidate
        from systemu.runtime.skill_loader import parse_skill_md
        path = persist_skill_candidate({
            "name": "roundtrip",
            "description": "demo",
            "procedure": ["s1"],
            "pitfalls": [],
            "confidence": 0.9,
        }, skills_dir=str(tmp_path))
        m = parse_skill_md(Path(path))
        assert m.name == "roundtrip"
        assert m.description == "demo"


class TestConfigAutoSkillExtractFields:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_AUTO_SKILL_EXTRACT_ENABLED", raising=False)
        monkeypatch.delenv("SYSTEMU_AUTO_SKILL_EXTRACT_MIN_CONFIDENCE", raising=False)
        from sharing_on.config import Config
        cfg = Config()
        assert cfg.auto_skill_extract_enabled is True
        assert cfg.auto_skill_extract_min_confidence == 0.6


class TestRunCompletionWiring:
    """v0.9.6 regression guard: auto_skill_extractor + consolidate_run must be
    CALLED from the run-completion path, not just exist as modules.

    Before this guard, both had zero production callers — green unit tests, but
    the "PRIMARY skill source" never fired. These tests pin the production wire
    via source inspection AND a behavioural proof that a successful run writes a
    SKILL.md.
    """

    def test_direct_task_defines_post_run_hook(self):
        from systemu.pipelines import direct_task
        assert hasattr(direct_task, "_maybe_extract_skill_and_consolidate")

    def test_run_direct_task_calls_post_run_hook(self):
        import inspect
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task.run_direct_task)
        assert "_maybe_extract_skill_and_consolidate" in src, (
            "run_direct_task must call the L7 post-run hook at the completion "
            "seam — otherwise auto-skill extraction never fires in production."
        )

    def test_hook_references_extractor_and_consolidator(self):
        import inspect
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task._maybe_extract_skill_and_consolidate)
        assert "extract_skill_candidate" in src
        assert "persist_skill_candidate" in src
        assert "consolidate_run" in src

    def test_build_result_exposes_run_metadata(self):
        """The hook depends on result['tools_called'/'tool_calls'/'rounds'];
        build_result must actually populate them from the history."""
        from systemu.runtime.context_builder import ExecutionContext
        import inspect
        src = inspect.getsource(ExecutionContext.build_result)
        assert "tools_called" in src and "tool_calls" in src and "rounds" in src

    def test_successful_run_writes_skill_md(self, tmp_path, monkeypatch):
        """End-to-end behavioural proof: a successful, multi-step run drives the
        hook to persist a SKILL.md into the configured user skills dir."""
        from systemu.pipelines import direct_task

        # Stub the Tier-1 extractor so no real LLM is hit; return a candidate.
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.extract_skill_candidate",
            lambda **kw: {
                "name": "earned-skill",
                "description": "Earned from a successful run",
                "procedure": ["do a", "do b"],
                "pitfalls": [],
                "confidence": 0.9,
            },
        )
        # Skip the consolidation LLM path entirely.
        monkeypatch.setattr(
            "systemu.runtime.memory_consolidator.consolidate_run",
            lambda **kw: None,
        )

        class _Cfg:
            auto_skill_extract_enabled = True
            auto_skill_extract_min_confidence = 0.6
            memory_consolidation_enabled = False
            skills_user_dir = str(tmp_path / "user_skills")

        class _Scroll:
            intent = "Summarize a web page into markdown"

        class _Shadow:
            execution_log = []

        class _Vault:
            root = str(tmp_path / "vault")

        result = {
            "status": "success",
            "summary": "Wrote summary.md (3 paragraphs).",
            "tools_called": ["web_fetch_page", "write_file"],
            "tool_calls": 2,
            "rounds": 3,
        }
        direct_task._maybe_extract_skill_and_consolidate(
            vault=_Vault(), config=_Cfg(), scroll=_Scroll(),
            shadow=_Shadow(), result=result,
        )
        written = tmp_path / "user_skills" / "earned-skill" / "SKILL.md"
        assert written.exists(), "successful run must persist an earned SKILL.md"
        body = written.read_text(encoding="utf-8")
        assert "earned-skill" in body

    def test_failed_run_does_not_extract_skill(self, tmp_path, monkeypatch):
        from systemu.pipelines import direct_task
        calls = {"extract": 0}
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.extract_skill_candidate",
            lambda **kw: calls.__setitem__("extract", calls["extract"] + 1) or None,
        )
        monkeypatch.setattr(
            "systemu.runtime.memory_consolidator.consolidate_run",
            lambda **kw: None,
        )

        class _Cfg:
            auto_skill_extract_enabled = True
            auto_skill_extract_min_confidence = 0.6
            memory_consolidation_enabled = False
            skills_user_dir = str(tmp_path / "user_skills")

        class _Scroll:
            intent = "x"

        class _Shadow:
            execution_log = []

        class _Vault:
            root = str(tmp_path / "vault")

        # Non-success status must NOT trigger extraction.
        direct_task._maybe_extract_skill_and_consolidate(
            vault=_Vault(), config=_Cfg(), scroll=_Scroll(), shadow=_Shadow(),
            result={"status": "pending_decision", "summary": "parked"},
        )
        assert calls["extract"] == 0
