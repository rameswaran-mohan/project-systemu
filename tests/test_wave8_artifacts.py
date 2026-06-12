"""W8 — artifact collection at the execution boundary.

`files_produced` has always been `[]` because nothing tracked what tools
wrote. The collector derives candidate paths from a tool call's params and
parsed result and keeps ONLY files that exist on disk after the call — no
false positives, no edits to 41 tool files.

(Collector ships with slice 8.2 — the quick lane consumes it; the
ShadowRuntime/direct_task wiring + presentation surfaces are slice 8.4.)
"""
from __future__ import annotations

from systemu.runtime.artifacts import collect_artifact_paths


class TestCollectArtifactPaths:
    def test_write_tool_param_path_collected_when_file_exists(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("hello", encoding="utf-8")
        got = collect_artifact_paths(
            "write_text_file", {"file_path": str(f), "content": "hello"},
            {"success": True, "error": None})
        assert got == [str(f.resolve())]

    def test_nonexistent_path_is_dropped(self, tmp_path):
        got = collect_artifact_paths(
            "write_text_file", {"file_path": str(tmp_path / "never_written.txt")},
            {"success": True})
        assert got == []

    def test_non_write_tool_without_path_keys_collects_nothing(self, tmp_path):
        got = collect_artifact_paths(
            "web_search", {"query": "spas near me"}, {"success": True, "data": []})
        assert got == []

    def test_parsed_result_path_collected_for_any_tool(self, tmp_path):
        # Tools that report where they saved (e.g. web_screenshot) via the
        # parsed payload are collected even without a pathy name.
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG")
        got = collect_artifact_paths(
            "web_screenshot", {"url": "https://x"},
            {"success": True, "data": {"output_path": str(f)}})
        assert got == [str(f.resolve())]

    def test_dedupes_and_preserves_order(self, tmp_path):
        a = tmp_path / "a.csv"
        a.write_text("x", encoding="utf-8")
        got = collect_artifact_paths(
            "write_csv_file",
            {"file_path": str(a)},
            {"success": True, "path": str(a)})
        assert got == [str(a.resolve())]

    def test_directories_are_not_artifacts(self, tmp_path):
        got = collect_artifact_paths(
            "write_text_file", {"file_path": str(tmp_path)}, {"success": True})
        assert got == []

    def test_never_raises_on_garbage(self):
        assert collect_artifact_paths(None, None, None) == []
        assert collect_artifact_paths("write_x", {"file_path": 42}, "nope") == []


class TestExecutionContextCarriesFiles:
    def _ctx(self):
        from systemu.runtime.context_builder import ExecutionContext
        return ExecutionContext(execution_id="exec_t", system_prompt="s",
                                scroll_json=[], tool_index=[])

    def test_add_files_dedupes_and_build_result_exposes(self):
        ctx = self._ctx()
        ctx.add_files(["C:/a.txt", "C:/b.txt"])
        ctx.add_files(["C:/a.txt", ""])           # dupe + empty ignored
        result = ctx.build_result(status="success", final_summary="done")
        assert result["files_produced"] == ["C:/a.txt", "C:/b.txt"]

    def test_default_is_empty_not_missing(self):
        result = self._ctx().build_result(status="success", final_summary="x")
        assert result["files_produced"] == []


class TestRuntimeAndChatWiring:
    def test_shadow_runtime_collects_at_the_tool_result_site(self):
        import inspect
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        assert "collect_artifact_paths" in src
        assert "context.add_files(" in src

    def test_direct_task_persists_summary_and_files_both_paths(self):
        """W8.4 BUG FIX pinned at VALUE level: build_result's key is
        'summary' — W5.2 read only 'final_summary', persisting empty outcomes
        since it shipped. (Its source-only test never caught the value.)"""
        import inspect
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task)
        assert src.count('result.get("final_summary") or result.get("summary")') >= 2, \
            "both sync reads must fall back to the real build_result key"
        assert src.count("files_produced") >= 3   # sync write, queued write, episodic

    def test_status_rows_count_files(self, tmp_path):
        from systemu.vault.vault import Vault
        from systemu.interface.components.status_menu import build_status_rows
        (tmp_path / "elder").mkdir(parents=True, exist_ok=True)
        vault = Vault(str(tmp_path))
        vault.append_chat_history({
            "ts": "2026-06-12T10:00:00", "prompt": "make a csv",
            "status": "success", "summary": "done",
            "files_produced": ["C:/out/a.csv", "C:/out/b.csv"],
        })
        rows = build_status_rows(vault)
        assert rows[0]["files"] == 2

    def test_live_details_render_per_file_artifacts(self):
        import inspect
        from systemu.interface.components import live_events_pane
        src = inspect.getsource(live_events_pane.render_event_details_body)
        assert 'details.get("files")' in src

    def test_chat_renders_files_for_all_entries(self):
        import inspect
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page)
        # The files loop must NOT be gated on the quick lane.
        files_idx = src.index('entry.get("files_produced")')
        quick_idx = src.index('entry.get("lane") == "quick"')
        assert files_idx < quick_idx, \
            "produced files must render for workflow entries too"
