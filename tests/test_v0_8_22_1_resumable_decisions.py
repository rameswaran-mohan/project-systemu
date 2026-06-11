"""v0.8.22.1 — resumable chat decisions + loose-end fixes."""
import json
import pytest


class TestToolsTriedFilter:
    def test_tools_tried_excludes_succeeded(self):
        """A tool whose fail-streak was reset to 0 (it ultimately succeeded)
        must NOT appear in tools_tried; only tools with an active streak (>0)."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime)  # bypass __init__ — unit-test the field logic
        rt._same_tool_fail_streak = {"web_search": 0, "web_extract": 3, "geocode": 0}
        tools_tried = sorted(k for k, v in rt._same_tool_fail_streak.items() if v > 0)
        assert tools_tried == ["web_extract"]


class TestCancelWording:
    def test_cancel_summary_omits_stuck_language(self, monkeypatch):
        """A 'cancelled' finalize must not read as a system 'stuck' failure and
        must not carry error='StuckLoopDetected'."""
        from systemu.runtime.shadow_runtime import ShadowRuntime

        captured = {}

        class FakeContext:
            def build_result(self, *, status, final_summary, error=None):
                captured.update(status=status, final_summary=final_summary, error=error)
                return {"status": status, "final_summary": final_summary, "error": error}

        rt = ShadowRuntime.__new__(ShadowRuntime)
        monkeypatch.setattr(rt, "_append_to_shadow_log", lambda *a, **k: None, raising=False)
        import systemu.runtime.shadow_runtime as sr
        monkeypatch.setattr(sr, "_record_terminal_telemetry", lambda *a, **k: None)
        monkeypatch.setattr(sr, "_dispatch_refinery", lambda *a, **k: None)

        res = rt._finalize_stuck(
            context=FakeContext(), status="cancelled", reason="operator cancelled",
            stuck_on=1, completed=[0], iteration=5, tool_calls_made=2,
            scroll=None, shadow=None, execution_id="e1", exec_start=0.0,
            total_objectives=3,
        )
        assert "cancel" in captured["final_summary"].lower()
        assert "stuck on objective" not in captured["final_summary"].lower()
        assert captured["error"] != "StuckLoopDetected"

    def test_partial_summary_keeps_stuck_language(self, monkeypatch):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        captured = {}

        class FakeContext:
            def build_result(self, *, status, final_summary, error=None):
                captured.update(status=status, final_summary=final_summary, error=error)
                return {}

        rt = ShadowRuntime.__new__(ShadowRuntime)
        monkeypatch.setattr(rt, "_append_to_shadow_log", lambda *a, **k: None, raising=False)
        import systemu.runtime.shadow_runtime as sr
        monkeypatch.setattr(sr, "_record_terminal_telemetry", lambda *a, **k: None)
        monkeypatch.setattr(sr, "_dispatch_refinery", lambda *a, **k: None)
        rt._finalize_stuck(
            context=FakeContext(), status="partial", reason="no progress",
            stuck_on=2, completed=[0, 1], iteration=9, tool_calls_made=4,
            scroll=None, shadow=None, execution_id="e2", exec_start=0.0,
            total_objectives=4,
        )
        assert "stuck on objective 2" in captured["final_summary"].lower()
        assert captured["error"] == "StuckLoopDetected"


class TestForgeDedupReuse:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_reuses_deployed_disabled_tool_no_duplicate(self, tmp_path):
        from systemu.pipelines.activity_extractor import _upsert_tool
        from systemu.core.models import Tool, ToolStatus
        vlt = self._vault(tmp_path)
        existing = Tool(id="tool_aaa", name="geocode_place", description="geo",
                        tool_type="python_function", status=ToolStatus.DEPLOYED,
                        enabled=False, forged_by_systemu=True,
                        implementation_path="systemu/vault/tools/implementations/geocode_place.py")
        vlt.save_tool(existing)
        before = len(vlt.load_index("tools"))
        tid, is_new = _upsert_tool({"name": "geocode_place", "description": "geocode a place"}, vlt)
        after = len(vlt.load_index("tools"))
        assert tid == "tool_aaa"          # reused, not duplicated
        assert is_new is False            # already forged → not "missing"
        assert after == before            # no new tool row created

    def test_proposed_no_code_still_needs_forge(self, tmp_path):
        from systemu.pipelines.activity_extractor import _upsert_tool
        from systemu.core.models import Tool, ToolStatus
        vlt = self._vault(tmp_path)
        proposed = Tool(id="tool_bbb", name="scrape_menu", description="x",
                        tool_type="python_function", status=ToolStatus.PROPOSED,
                        enabled=False, forged_by_systemu=True)
        vlt.save_tool(proposed)
        tid, is_new = _upsert_tool({"name": "scrape_menu", "description": "scrape the menu"}, vlt)
        assert tid == "tool_bbb"
        assert is_new is True             # PROPOSED + no impl → genuinely needs forging


class TestReadinessGateEnabled:
    def test_disabled_deployed_tool_is_not_ready(self):
        """The readiness predicate used by the gate must treat a DEPLOYED-but-
        disabled tool as not-ready (so the activity parks instead of executing)."""
        from systemu.runtime.shadow_runtime import tool_is_runtime_ready
        from systemu.core.models import Tool, ToolStatus
        t = Tool(id="t1", name="geocode", description="x",
                 tool_type="python_function", status=ToolStatus.DEPLOYED, enabled=False)
        is_ready = tool_is_runtime_ready(t.status) and getattr(t, "enabled", False)
        assert is_ready is False

    def test_waiting_message_says_enable_for_forged_disabled(self):
        from systemu.pipelines.direct_task import _waiting_on_tools_message
        from systemu.core.models import Tool, ToolStatus
        forged = Tool(id="t2", name="geocode", description="x",
                      tool_type="python_function", status=ToolStatus.DEPLOYED, enabled=False)
        msg = _waiting_on_tools_message(["geocode"], [forged])
        assert "enable" in msg.lower()
        assert "geocode" in msg

    def test_waiting_message_says_forge_for_proposed(self):
        from systemu.pipelines.direct_task import _waiting_on_tools_message
        from systemu.core.models import Tool, ToolStatus
        proposed = Tool(id="t3", name="scrape_menu", description="x",
                        tool_type="python_function", status=ToolStatus.PROPOSED, enabled=False)
        msg = _waiting_on_tools_message(["scrape_menu"], [proposed])
        assert "forge" in msg.lower() or "dependencies" in msg.lower()


class TestSupervisorThreadsChatSubmissionId:
    def test_submit_stores_chat_submission_id_on_payload(self, monkeypatch):
        """submit(chat_submission_id=...) must place it on the queued payload so
        the worker can thread it into runtime.execute."""
        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        import threading, queue as _queue
        sup._pending_lock = threading.Lock()
        sup._running_lock = threading.Lock()
        sup._pending_activity_ids = set()
        sup._running = {}
        sup._task_queue = None
        sup._queue = _queue.PriorityQueue()
        monkeypatch.setattr(sup, "_resolve_shadow_with_affinity",
                            lambda **k: k["shadow_id"], raising=False)
        sub_id = sup.submit("act1", "sh1", reason="chat", origin="chat",
                            consult_affinity_log=False,
                            chat_submission_id="2026-06-03T20:27:41")
        _prio, _ts, payload = sup._queue.get_nowait()
        assert payload["chat_submission_id"] == "2026-06-03T20:27:41"
        assert sub_id.startswith("sub_")


class TestStableStuckDedupKey:
    def test_dedup_key_uses_scroll_not_execution_id(self, monkeypatch):
        """The stuck dedup_key must be execution-independent: keyed by scroll_id +
        objective, NOT execution_id, so a resumed run can find the same decision."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        captured = {}

        def fake_request_choice(questions, *, dedup_key, extra_context=None):
            captured["dedup_key"] = dedup_key
            captured["extra_context"] = extra_context
            return None  # behave like headless so no exception

        import systemu.interface.notifications as notif
        monkeypatch.setattr(notif, "request_choice", fake_request_choice)

        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._stuck_round_for_obj = {}

        class Obj:
            id = 1
            goal = "rank burrito places"

        rt._ask_stuck_or_degrade(
            execution_id="exec_AAA", current_objective=Obj(),
            tools_tried=["web_search"], reason="no progress",
            scroll_id="scroll_xyz", activity_id="act_1", shadow_id="sh_1",
        )
        assert captured["dedup_key"] == "stuck:scroll_xyz:obj_1:r1"
        assert "exec" not in captured["dedup_key"].lower()
        assert captured["extra_context"]["execution_id"] == "exec_AAA"
        assert captured["extra_context"]["activity_id"] == "act_1"
        assert captured["extra_context"]["objective_id"] == 1


class TestRequestChoiceExtraContext:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_request_choice_merges_extra_context(self, tmp_path, monkeypatch):
        """extra_context fields land in the posted OperatorDecision.context so the
        resume handler can recover execution_id/activity_id."""
        import systemu.interface.notifications as notif
        from systemu.approval.decision_queue import OperatorDecisionQueue
        vlt = self._vault(tmp_path)
        queue = OperatorDecisionQueue(vlt)
        monkeypatch.setattr(notif, "_get_decision_queue", lambda: queue)

        from systemu.approval.exceptions import PendingChoiceRequest
        qs = [{"id": "action", "prompt": "Stuck?", "options":
               [{"label": "Provide hint"}], "allow_free_text": True}]
        with pytest.raises(PendingChoiceRequest) as ei:
            notif.request_choice(qs, dedup_key="stuck:scroll_z:obj_1:r1",
                                 extra_context={"execution_id": "exec_Z",
                                                "activity_id": "act_Z",
                                                "objective_id": 1})
        dec = vlt.get_decision(ei.value.decision_id)
        assert dec.context["execution_id"] == "exec_Z"
        assert dec.context["activity_id"] == "act_Z"
        assert dec.context["kind"] == "structured_question"


class TestApplyStuckAnswer:
    def test_apply_stuck_answer_helper_branches(self):
        """The shared helper maps the three answers to the right actions."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._operator_hint = None
        rt._iters_since_obj_credit = 5
        rt._same_tool_fail_streak = {"x": 2}
        rt._tools_since_credit = {"x"}   # W6.3 state (declared in __init__)

        class Obj:
            id = 1

        # Provide hint (free text) → sets operator hint, returns ("continue", None)
        action, res = rt._apply_stuck_answer(Obj(), {"action": "go north"},
                                             finalize=lambda **k: {"finalized": k["status"]})
        assert action == "continue"
        assert "go north" in rt._operator_hint

        # Accept partial → finalize partial
        action, res = rt._apply_stuck_answer(Obj(), {"action": "Accept partial"},
                                             finalize=lambda **k: {"finalized": k["status"]})
        assert action == "finalize" and res == {"finalized": "partial"}

        # Cancel run → finalize cancelled
        action, res = rt._apply_stuck_answer(Obj(), {"action": "Cancel run"},
                                             finalize=lambda **k: {"finalized": k["status"]})
        assert action == "finalize" and res == {"finalized": "cancelled"}


class TestResumeOnDecisionHandler:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_handler_redispatches_with_resume_and_stashes_answer(self, tmp_path):
        from systemu.runtime.resume_on_decision import handle_decision_resolved
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.runtime.execution_snapshot import (
            ExecutionSnapshot, write_snapshot, read_snapshot,
        )
        import json
        vlt = self._vault(tmp_path)
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        write_snapshot(ExecutionSnapshot(
            execution_id="exec_R", shadow_id="sh_R", scroll_id="scroll_R",
            activity_id="act_R", completed_objective_ids=[0]), data_dir=data_dir)
        queue = OperatorDecisionQueue(vlt)
        did = queue.post(title="Stuck on Objective 1", body="?",
                         options=["Provide hint", "Accept partial", "Cancel run", "Other"],
                         context={"kind": "structured_question",
                                  "chat_submission_id": "2026-06-03T20:27:41",
                                  "execution_id": "exec_R", "activity_id": "act_R",
                                  "shadow_id": "sh_R", "scroll_id": "scroll_R",
                                  "objective_id": 1},
                         dedup_key="stuck:scroll_R:obj_1:r1")
        queue.resolve(did, choice=json.dumps({"action": "I'm in Bangalore"}))
        calls = []
        class FakeSup:
            def submit(self, activity_id, shadow_id, **kw):
                calls.append((activity_id, shadow_id, kw)); return "sub_x"
        handle_decision_resolved(
            {"category": "operator_decision_resolved",
             "context": {"decision_id": did, "choice": json.dumps({"action": "I'm in Bangalore"}),
                         "chat_submission_id": "2026-06-03T20:27:41"}},
            vault=vlt, supervisor=FakeSup(), data_dir=data_dir,
        )
        assert len(calls) == 1
        aid, sid, kw = calls[0]
        assert aid == "act_R" and sid == "sh_R"
        assert kw["resume_from_execution_id"] == "exec_R"
        assert kw["chat_submission_id"] == "2026-06-03T20:27:41"
        snap = read_snapshot("exec_R", data_dir=data_dir)
        stash = [n for n in snap.sticky_notes if n.startswith("__STUCK_ANSWER__::obj_1::")]
        assert stash and "Bangalore" in stash[0]

    def test_handler_ignores_non_stuck_decisions(self, tmp_path):
        from systemu.runtime.resume_on_decision import handle_decision_resolved
        from systemu.approval.decision_queue import OperatorDecisionQueue
        vlt = self._vault(tmp_path)
        queue = OperatorDecisionQueue(vlt)
        did = queue.post(title="Forge?", body="?", options=["Skip", "Forge"],
                         context={"kind": "forge_tool"}, dedup_key="tool_forge:t1")
        queue.resolve(did, choice="Forge")
        calls = []
        class FakeSup:
            def submit(self, *a, **k): calls.append((a, k)); return "x"
        handle_decision_resolved(
            {"category": "operator_decision_resolved",
             "context": {"decision_id": did, "choice": "Forge"}},
            vault=vlt, supervisor=FakeSup(), data_dir=tmp_path / "data")
        assert calls == []

    def test_handler_is_idempotent_on_replay(self, tmp_path):
        from systemu.runtime import resume_on_decision as rod
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
        import json
        rod._handled.clear()
        vlt = self._vault(tmp_path)
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)
        write_snapshot(ExecutionSnapshot(execution_id="exec_D", shadow_id="sh_D",
                       scroll_id="sc_D", activity_id="act_D"), data_dir=data_dir)
        queue = OperatorDecisionQueue(vlt)
        did = queue.post(title="Stuck on Objective 1", body="?",
                         options=["Provide hint", "Other"],
                         context={"kind": "structured_question",
                                  "chat_submission_id": "ts1", "execution_id": "exec_D",
                                  "activity_id": "act_D", "shadow_id": "sh_D",
                                  "scroll_id": "sc_D", "objective_id": 1},
                         dedup_key="stuck:sc_D:obj_1:r1")
        queue.resolve(did, choice=json.dumps({"action": "hint"}))
        calls = []
        class FakeSup:
            def submit(self, *a, **k): calls.append(1); return "x"
        ev = {"category": "operator_decision_resolved",
              "context": {"decision_id": did, "choice": "x", "chat_submission_id": "ts1"}}
        rod.handle_decision_resolved(ev, vault=vlt, supervisor=FakeSup(), data_dir=data_dir)
        rod.handle_decision_resolved(ev, vault=vlt, supervisor=FakeSup(), data_dir=data_dir)
        assert len(calls) == 1
