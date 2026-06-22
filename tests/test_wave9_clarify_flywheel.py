"""W9.4 — clarify-first prompts + the quick-lane flywheel signal.

(a) The refiner only asked when the LLM volunteered questions — the stuck
spa run asked nothing and guessed the operator's location. Both prompts now
NAME the office essentials that warrant one question when absent (recipient,
source, date range, amount/threshold, account/client/vendor).

(b) The quick lane — the default since v0.9.18 — bypassed Stage-5 entirely:
the evolution engine and episodic memory received zero signal from it.
submit_quick_task now reuses the SAME capture hook the workflow lane uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.core.utils import load_prompt
from systemu.vault.vault import Vault

_ESSENTIALS = ("recipient", "source", "date range", "threshold")


class TestClarifyFirstPrompts:
    def test_refiner_names_the_office_essentials(self):
        prompt = load_prompt("refine_scroll.md").lower()
        assert "clarifying_questions" in prompt
        for needle in _ESSENTIALS:
            assert needle in prompt, f"refiner must name essential: {needle}"

    def test_quick_lane_names_them_and_defers_to_profile(self):
        prompt = load_prompt("quick_task.md").lower()
        for needle in _ESSENTIALS:
            assert needle in prompt, f"quick lane must name essential: {needle}"
        # The profile block (W9.2) may already answer them — check it first.
        assert "profile" in prompt


class TestQuickLaneFlywheelSignal:
    @pytest.fixture
    def vault(self, tmp_path: Path) -> Vault:
        for sub in ["tools/implementations", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_submit_quick_task_fires_episodic_capture(self, vault, monkeypatch):
        import systemu.runtime.shadow_runtime as srt
        from systemu.pipelines.quick_task import submit_quick_task

        captured = {}

        def _fake_capture(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(srt, "_trigger_episodic_capture", _fake_capture)

        def llm(*, system, user, config=None):
            return {"action": "ANSWER", "answer_md": "the report is ready"}

        res = submit_quick_task("compile the weekly report", None, vault,
                                llm_json=llm)
        assert res.status == "success"
        assert captured["intent"] == "compile the weekly report"
        assert captured["status"] == "success"
        assert captured["chat_result"] == "the report is ready"
        assert captured["raw_chat_id"] == captured["session_id"]

    def test_capture_failure_never_breaks_the_run(self, vault, monkeypatch):
        import systemu.runtime.shadow_runtime as srt
        from systemu.pipelines.quick_task import submit_quick_task

        def _boom(**kwargs):
            raise RuntimeError("episodic store down")

        monkeypatch.setattr(srt, "_trigger_episodic_capture", _boom)
        res = submit_quick_task(
            "hello", None, vault,
            llm_json=lambda **k: {"action": "ANSWER", "answer_md": "hi"})
        assert res.status == "success"