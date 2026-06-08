"""v0.9.7 (Phase 4.2) learn-from-failure corrective (anti-pattern) skills.

Mirrors the v0.9.6 success-only auto-skill-extraction tests but for the FAILURE
path: when a run with real activity fails/partials, an anti-pattern SKILL.md is
extracted so future runs are warned. All LLM calls are mocked — NO network.
"""
from pathlib import Path
import inspect


class TestExtractCorrectiveCandidate:
    def test_returns_none_on_low_confidence(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_corrective_candidate
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: {
                "name": "weak-lesson",
                "description": "d",
                "procedure": ["x"],
                "pitfalls": ["y"],
                "confidence": 0.3,
            },
        )
        from sharing_on.config import Config
        result = extract_corrective_candidate(
            intent="do thing X",
            failure_reason="something went wrong",
            n_rounds=3, n_tool_calls=3,
            tools_called=["t1", "t2"], config=Config(),
        )
        # Below MIN_CONFIDENCE → no anti-pattern captured.
        assert result is None

    def test_returns_none_when_llm_yields_null(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_corrective_candidate
        # The prompt may emit literal null → llm_call_json returns None.
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: None,
        )
        from sharing_on.config import Config
        result = extract_corrective_candidate(
            intent="x", failure_reason="transient blip",
            n_rounds=2, n_tool_calls=2,
            tools_called=["t1", "t2"], config=Config(),
        )
        assert result is None

    def test_skips_below_activity_threshold(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_corrective_candidate
        called = []
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: called.append(1) or None,
        )
        from sharing_on.config import Config
        result = extract_corrective_candidate(
            intent="x", failure_reason="oops",
            n_rounds=1, n_tool_calls=1,
            tools_called=["t1"], config=Config(),
        )
        # Not enough activity — no LLM call, no candidate.
        assert result is None
        assert called == []

    def test_returns_anti_pattern_on_high_confidence(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_corrective_candidate
        def fake_call(**kw):
            return {
                "name": "avoid-writing-outside-workspace",
                "description": "When writing files, keep them inside the workspace.",
                "procedure": ["resolve the output path under the workspace root",
                              "retry the write"],
                "pitfalls": ["wrote to an absolute path outside the workspace",
                             "did not check write permissions first"],
                "confidence": 0.85,
            }
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json", fake_call,
        )
        from sharing_on.config import Config
        result = extract_corrective_candidate(
            intent="write a report to /etc/report.md",
            failure_reason="PermissionError writing /etc/report.md",
            n_rounds=2, n_tool_calls=2,
            tools_called=["write_file", "write_file"], config=Config(),
        )
        assert result is not None
        assert result["name"] == "avoid-writing-outside-workspace"
        assert result["confidence"] == 0.85
        assert result["_type"] == "anti_pattern"

    def test_handles_llm_failure_gracefully(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_corrective_candidate
        def boom(**kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json", boom,
        )
        from sharing_on.config import Config
        result = extract_corrective_candidate(
            intent="x", failure_reason="oops",
            n_rounds=3, n_tool_calls=3,
            tools_called=["t1"], config=Config(),
        )
        assert result is None

    def test_disabled_returns_none(self, monkeypatch):
        from systemu.runtime.auto_skill_extractor import extract_corrective_candidate
        called = []
        monkeypatch.setattr(
            "systemu.runtime.auto_skill_extractor.llm_call_json",
            lambda **kw: called.append(1) or {},
        )
        from sharing_on.config import Config
        cfg = Config()
        cfg.auto_skill_extract_enabled = False
        result = extract_corrective_candidate(
            intent="x", failure_reason="oops",
            n_rounds=3, n_tool_calls=3,
            tools_called=["t1"], config=cfg,
        )
        assert result is None
        assert called == []


class TestPersistAntiPatternFrontmatter:
    def test_writes_type_marker_for_anti_pattern(self, tmp_path):
        from systemu.runtime.auto_skill_extractor import persist_skill_candidate
        candidate = {
            "name": "avoid-bad-thing",
            "description": "When doing X, don't do Y.",
            "procedure": ["do Z instead"],
            "pitfalls": ["did Y, which failed"],
            "confidence": 0.8,
            "_type": "anti_pattern",
        }
        path = persist_skill_candidate(candidate, skills_dir=str(tmp_path))
        assert path is not None
        content = Path(path).read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "_type: anti_pattern" in content
        assert "name: avoid-bad-thing" in content
        assert "do Z instead" in content

    def test_success_frontmatter_unchanged_without_marker(self, tmp_path):
        from systemu.runtime.auto_skill_extractor import persist_skill_candidate
        candidate = {
            "name": "ordinary-skill",
            "description": "A normal earned skill.",
            "procedure": ["step 1"],
            "pitfalls": [],
            "confidence": 0.9,
        }
        path = persist_skill_candidate(candidate, skills_dir=str(tmp_path))
        content = Path(path).read_text(encoding="utf-8")
        # Marker must be ABSENT on the success path.
        assert "_type:" not in content
        assert "source: auto-extracted" in content


class TestCompletionHookFailureBranch:
    """Source-inspection guards: the completion hook must actually call the
    corrective extractor on failure, and leave the success branch intact."""

    def test_hook_references_corrective_and_failure_branch(self):
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task._maybe_extract_skill_and_consolidate)
        assert "extract_corrective_candidate" in src
        # A status-in-(...) failure/partial branch must exist.
        assert "status in (" in src
        assert "failure" in src and "partial" in src

    def test_success_branch_still_references_extract_skill_candidate(self):
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task._maybe_extract_skill_and_consolidate)
        # The original success path must be untouched.
        assert "extract_skill_candidate" in src
        assert 'status == "success"' in src
