"""W8.3 — chat integration: Quick by default, factory one click away.

The Compose page gains a lane control (Quick answer | Workflow run-now |
Workflow queue, default Quick). Quick submissions run the 8.2 executor in a
worker thread; the answer renders as FULL markdown in the thread (no more
120-char truncation); a successful quick run offers "Save as workflow",
which promotes the prompt into the factory pipeline WITHOUT executing it.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace


class TestPromoteToWorkflow:
    def test_creates_scroll_without_executing(self, monkeypatch, tmp_path):
        from systemu.pipelines import quick_task
        import systemu.pipelines.scroll_refiner as sr

        captured = {}

        def _fake_refine(prompt, vault, config, prior_task=None):
            captured["prompt"] = prompt
            return SimpleNamespace(id="scroll_promoted", name="Promoted")

        monkeypatch.setattr(sr, "refine_from_text", _fake_refine)
        scroll = quick_task.promote_to_workflow("find spas weekly", None, vault=object())
        assert scroll.id == "scroll_promoted"
        assert captured["prompt"] == "find spas weekly"
        # Promotion must NOT touch the runtime — no execution, no activity.
        src = inspect.getsource(quick_task.promote_to_workflow)
        for forbidden in ("run_direct_task", "ShadowRuntime", "extract_and_process",
                          "decide_shadow"):
            assert forbidden not in src


class TestChatWiring:
    def _src(self):
        from systemu.interface.pages import chat_page
        return inspect.getsource(chat_page)

    def test_lane_control_defaults_to_quick(self):
        src = self._src()
        assert '"quick"' in src and "Quick answer" in src
        assert 'value="quick"' in src, "Quick must be the default lane"

    def test_quick_path_uses_submit_quick_task_in_thread(self):
        src = self._src()
        assert "submit_quick_task" in src
        # Runs inside the existing _run worker thread (not on the UI loop).
        assert "threading.Thread(target=_run" in src

    def test_quick_answers_render_full_markdown(self):
        src = self._src()
        assert 'entry.get("lane") == "quick"' in src
        assert "ui.markdown" in src

    def test_save_as_workflow_offered_off_the_loop(self):
        src = self._src()
        assert "promote_to_workflow" in src
        assert "asyncio.to_thread(" in src, \
            "promotion runs an LLM call — it must not block the UI loop (W7.1)"

    def test_workflow_lanes_still_route_to_direct_task(self):
        src = self._src()
        assert "run_direct_task" in src
        assert "route_through_supervisor" in src
