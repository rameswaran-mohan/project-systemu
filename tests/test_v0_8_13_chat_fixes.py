"""v0.8.13 — chat failure hardening."""
import pytest
from systemu.core.models import Tool, ToolType, coerce_tool_type


class TestCoerceToolType:
    def test_exact_enum_passthrough(self):
        assert coerce_tool_type("api_call") is ToolType.API_CALL
        assert coerce_tool_type(ToolType.BROWSER_ACTION) is ToolType.BROWSER_ACTION

    def test_web_synonyms_map_to_api_call(self):
        for raw in ("web", "WEB", "http", "https", "url", "rest", "fetch", " web "):
            assert coerce_tool_type(raw) is ToolType.API_CALL

    def test_render_synonyms_map_to_browser_action(self):
        for raw in ("scrape", "scraping", "browser", "render", "screen_capture", "screenshot", "screen"):
            assert coerce_tool_type(raw) is ToolType.BROWSER_ACTION

    def test_unknown_and_empty_fall_back_to_default(self):
        assert coerce_tool_type("banana") is ToolType.PYTHON_FUNCTION
        assert coerce_tool_type("") is ToolType.PYTHON_FUNCTION
        assert coerce_tool_type(None) is ToolType.PYTHON_FUNCTION

    def test_tool_model_coerces_web_instead_of_raising(self):
        # The exact production crash: Tool(tool_type='web') used to raise.
        t = Tool(id="tool_x", name="search_restaurants", description="d", tool_type="web")
        assert t.tool_type is ToolType.API_CALL


class TestExtractionPromptHardening:
    def _prompt(self):
        from systemu.pipelines.activity_extractor import load_prompt
        return load_prompt("extract_skills_tools.md")

    def test_no_invalid_screen_capture_tool_type(self):
        # screen_capture is NOT a valid ToolType; it must not appear as a tool_type row.
        assert "`screen_capture`" not in self._prompt()

    def test_has_hard_never_web_constraint(self):
        p = self._prompt().lower()
        assert "never invent" in p and "'web'" in p


class TestExtractorRoutesThroughValidator:
    def test_web_spec_becomes_api_call_not_python_function(self, tmp_path):
        from systemu.vault.vault import Vault
        from systemu.core.models import ToolType
        from systemu.pipelines.activity_extractor import _upsert_tool
        vault = Vault(vault_dir=tmp_path)
        tid, is_new = _upsert_tool(
            {"name": "fetch_menu", "description": "fetch a web page",
             "tool_type": "web", "dependencies": ["requests"]},
            vault,
        )
        assert is_new
        assert vault.get_tool(tid).tool_type is ToolType.API_CALL  # was python_function

    def test_missing_tool_type_defaults_python_function(self, tmp_path):
        from systemu.vault.vault import Vault
        from systemu.core.models import ToolType
        from systemu.pipelines.activity_extractor import _upsert_tool
        vault = Vault(vault_dir=tmp_path)
        tid, _ = _upsert_tool({"name": "do_thing", "description": "pure logic"}, vault)
        assert vault.get_tool(tid).tool_type is ToolType.PYTHON_FUNCTION


class TestRecoveryModelFields:
    def test_scroll_status_has_extraction_failed(self):
        from systemu.core.models import ScrollStatus
        assert ScrollStatus.EXTRACTION_FAILED.value == "extraction_failed"

    def test_scroll_has_recovery_attempts_default_zero(self):
        from systemu.core.models import Scroll
        s = Scroll(id="scroll_x", name="n", source_session_id="s",
                   raw_instructions_path="p", narrative_md="m")
        assert s.recovery_attempts == 0


class TestRecoveryInitAndBounding:
    def test_recovery_calls_init_pipeline_and_bounds_retries(self, monkeypatch, tmp_path):
        import systemu.scheduler.jobs as jobs
        from systemu.core.models import Scroll, ScrollStatus
        from systemu.vault.vault import Vault
        from sharing_on.config import Config

        vault = Vault(vault_dir=tmp_path)
        scroll = Scroll(id="scroll_zombie", name="z", source_session_id="s",
                        raw_instructions_path="p", narrative_md="m",
                        status=ScrollStatus.APPROVED, recovery_attempts=2)
        vault.save_scroll(scroll)

        jobs.init_jobs(Config(), vault)

        init_calls = []
        monkeypatch.setattr("systemu.pipelines.activity_extractor.init_pipeline",
                            lambda c, v: init_calls.append((c, v)))
        # Make re-extraction a no-op so we isolate the bounding logic.
        monkeypatch.setattr("systemu.pipelines.scroll_refiner.approve_pending_scroll",
                            lambda *a, **k: None)

        jobs.startup_recovery_sweep()

        assert init_calls, "init_pipeline must be called by the recovery sweep"
        # recovery_attempts was already 2 (== N) -> scroll terminated, not retried.
        reloaded = vault.get_scroll("scroll_zombie")
        assert reloaded.status == ScrollStatus.EXTRACTION_FAILED


class TestRuntimeReadyPredicate:
    def test_ready_statuses(self):
        from systemu.core.models import ToolStatus
        from systemu.runtime.shadow_runtime import tool_is_runtime_ready
        ready = {ToolStatus.DEPLOYED, ToolStatus.TESTED, ToolStatus.UPGRADED}
        for s in ToolStatus:
            assert tool_is_runtime_ready(s) is (s in ready)


class TestWaitingOnToolsGate:
    def test_gate_parks_task_when_tools_not_ready(self, monkeypatch, tmp_path):
        import systemu.pipelines.direct_task as dt
        from systemu.vault.vault import Vault
        from sharing_on.config import Config
        from systemu.core.models import (Scroll, ScrollStatus, Activity, ActivityStatus,
                                          Tool, ToolStatus, ToolType, Shadow)

        vault = Vault(vault_dir=tmp_path)
        tool = Tool(id="tool_p", name="search_restaurants", description="d",
                    tool_type=ToolType.API_CALL, status=ToolStatus.PROPOSED)
        vault.save_tool(tool)
        scroll = Scroll(id="scroll_g", name="find food", source_session_id="s",
                        raw_instructions_path="p", narrative_md="m",
                        status=ScrollStatus.APPROVED)
        vault.save_scroll(scroll)
        activity = Activity(id="act_g", name="find food", scroll_id="scroll_g",
                            required_tool_ids=["tool_p"], missing_tools=["search_restaurants"],
                            status=ActivityStatus.PARTIAL)
        vault.save_activity(activity)
        shadow = Shadow(id="shadow_w", name="Wild Card", description="generalist")
        vault.save_shadow(shadow)

        # Stub the pipeline stages so only the gate logic is exercised.
        monkeypatch.setattr(dt, "_run_coroutine", lambda *a, **k: pytest.fail("must not execute"))
        monkeypatch.setattr("systemu.pipelines.scroll_refiner.refine_from_text", lambda *a, **k: scroll)
        monkeypatch.setattr("systemu.pipelines.activity_extractor.extract_and_process", lambda *a, **k: activity)
        monkeypatch.setattr("systemu.pipelines.shadow_decision.decide_shadow", lambda *a, **k: shadow)
        monkeypatch.setattr("systemu.pipelines.activity_extractor.init_pipeline", lambda *a, **k: None)

        dt.run_direct_task("find food", Config(), vault, route_through_supervisor=False)

        hist = vault.load_chat_history(limit=5)
        latest = hist[-1]
        assert latest["status"] == "waiting_on_tools"
        assert latest.get("activity_id") == "act_g"
        assert "search_restaurants" in (latest.get("missing_tools") or [])


class TestStorageAwareApprove:
    def test_file_mode_uses_json_store_and_installs(self, monkeypatch, tmp_path):
        import systemu.runtime.dep_approvals as da

        monkeypatch.delenv("SYSTEMU_DATABASE_URL", raising=False)
        monkeypatch.chdir(tmp_path)            # so Path("data")/dep_approvals.json is here
        calls = {"pip": [], "dry": []}
        monkeypatch.setattr(da, "_run_pip_install", lambda pkg: calls["pip"].append(pkg) or 0)
        monkeypatch.setattr(da, "_rerun_dry_run", lambda tid: calls["dry"].append(tid))

        da.approve_and_install(tool_id="tool_z", package="beautifulsoup4", source="test")

        store = da.DepApprovalStore(tmp_path / "data" / "dep_approvals.json")
        assert store.is_approved("beautifulsoup4")     # JSON store, not sqlite
        assert calls["pip"] == ["beautifulsoup4"]
        assert calls["dry"] == ["tool_z"]


class TestActionableDepNotification:
    def test_dep_audit_emits_per_package_install_actions(self, monkeypatch, tmp_path):
        import systemu.scheduler.jobs as jobs

        captured = {}

        class FakeVault:
            def list_tools(self, status=None):
                return [{"id": "tool_d", "name": "fetch_menu", "enabled": True,
                         "dependencies": ["definitely_missing_pkg_xyz"], "status": "deployed"}]
            def list_pending_notifications(self):
                return []
            # _startup_dep_audit persists via vault.queue_notification(notif).
            def queue_notification(self, notif):
                captured["notif"] = notif

        jobs._startup_dep_audit(FakeVault())
        notif = captured["notif"]
        ctx = notif.context if hasattr(notif, "context") else notif["context"]
        actions = notif.actions if hasattr(notif, "actions") else notif["actions"]
        assert ctx["notification_type"] == "dep_approval"
        assert any(a.startswith("Install ") for a in actions)
        assert "definitely_missing_pkg_xyz" in ctx["pkg_tool_map"]


class TestDepApprovalDispatch:
    def test_install_action_calls_approve_and_install(self, monkeypatch):
        import systemu.interface.pages.notifications_page as npg

        calls = []
        monkeypatch.setattr("systemu.runtime.dep_approvals.approve_and_install",
                            lambda *, tool_id, package, source="dashboard": calls.append((tool_id, package)))
        # ui.notify / refresh are UI side effects — stub them.
        monkeypatch.setattr(npg.ui, "notify", lambda *a, **k: None)

        ctx = {"notification_type": "dep_approval",
               "pkg_tool_map": {"beautifulsoup4": "tool_d"}}
        npg._dispatch_notification_action("Install beautifulsoup4", ctx, vault=None, refresh_fn=None)

        assert calls == [("tool_d", "beautifulsoup4")]


class TestChatPageWaitingStatus:
    def test_status_color_map_includes_waiting_on_tools(self):
        import inspect, systemu.interface.pages.chat_page as cp
        src = inspect.getsource(cp.build_chat_page)
        assert "waiting_on_tools" in src


class TestPass2AutoRunUpdatesChat:
    def test_pass2_flips_waiting_entry_to_running(self, monkeypatch, tmp_path):
        import systemu.scheduler.jobs as jobs
        from systemu.vault.vault import Vault
        from sharing_on.config import Config
        from systemu.core.models import Activity, ActivityStatus, Tool, ToolStatus, ToolType

        vault = Vault(vault_dir=tmp_path)
        # tool now DEPLOYED (became ready)
        vault.save_tool(Tool(id="tool_r", name="search_restaurants", description="d",
                             tool_type=ToolType.API_CALL, status=ToolStatus.DEPLOYED, enabled=True))
        activity = Activity(id="act_r", name="find food", scroll_id="scroll_r",
                            required_tool_ids=["tool_r"], missing_tools=["search_restaurants"],
                            status=ActivityStatus.PARTIAL)
        vault.save_activity(activity)
        vault.append_chat_history({"ts": "2026-05-31T00:00:00", "prompt": "find food",
                                   "status": "waiting_on_tools", "activity_id": "act_r"})

        jobs.init_jobs(Config(), vault)
        monkeypatch.setattr("systemu.pipelines.shadow_decision.decide_shadow", lambda *a, **k: None)
        monkeypatch.setattr("systemu.pipelines.activity_extractor.init_pipeline", lambda *a, **k: None)
        # neutralise other passes that might touch the vault
        monkeypatch.setattr(jobs, "_resubmit_unexecuted_assigned", lambda v: None)
        monkeypatch.setattr(jobs, "_startup_dep_audit", lambda v: None)
        monkeypatch.setattr(jobs, "dry_run_all_pending_tools", lambda v, c: None)
        monkeypatch.setattr(jobs, "_backfill_tool_headers_v061", lambda v: None)

        jobs.startup_recovery_sweep()

        latest = vault.load_chat_history(limit=5)[-1]
        assert latest["status"] == "running"


class TestRefreshGuard:
    def test_disconnected_client_not_scheduled(self):
        from systemu.interface.pages.chat_page import _should_schedule_refresh
        class Disc:  has_socket_connection = False
        class Conn:  has_socket_connection = True
        class NoAttr: pass
        assert _should_schedule_refresh(Disc()) is False
        assert _should_schedule_refresh(Conn()) is True
        assert _should_schedule_refresh(NoAttr()) is False
        assert _should_schedule_refresh(None) is False


class TestLoopClosurePartAB:
    def test_gate_parks_activity_as_partial(self, monkeypatch, tmp_path):
        import systemu.pipelines.direct_task as dt
        from systemu.vault.vault import Vault
        from sharing_on.config import Config
        from systemu.core.models import (Scroll, ScrollStatus, Activity, ActivityStatus,
                                          Tool, ToolStatus, ToolType, Shadow)
        vault = Vault(vault_dir=tmp_path)
        vault.save_tool(Tool(id="tool_p", name="search_x", description="d",
                             tool_type=ToolType.API_CALL, status=ToolStatus.PROPOSED))
        scroll = Scroll(id="scr", name="n", source_session_id="s",
                        raw_instructions_path="p", narrative_md="m", status=ScrollStatus.APPROVED)
        vault.save_scroll(scroll)
        # activity intentionally UNASSIGNED (reused-tool case) to prove the gate re-marks it PARTIAL
        act = Activity(id="act_u", name="n", scroll_id="scr", required_tool_ids=["tool_p"],
                       missing_tools=[], status=ActivityStatus.UNASSIGNED)
        vault.save_activity(act)
        sh = Shadow(id="sh", name="Wild Card", description="g"); vault.save_shadow(sh)
        monkeypatch.setattr(dt, "_run_coroutine", lambda *a, **k: pytest.fail("must not execute"))
        monkeypatch.setattr("systemu.pipelines.scroll_refiner.refine_from_text", lambda *a, **k: scroll)
        monkeypatch.setattr("systemu.pipelines.activity_extractor.extract_and_process", lambda *a, **k: act)
        monkeypatch.setattr("systemu.pipelines.shadow_decision.decide_shadow", lambda *a, **k: sh)
        monkeypatch.setattr("systemu.pipelines.activity_extractor.init_pipeline", lambda *a, **k: None)
        dt.run_direct_task("n", Config(), vault, route_through_supervisor=False)
        reloaded = vault.get_activity("act_u")
        assert reloaded.status == ActivityStatus.PARTIAL
        assert "search_x" in (reloaded.missing_tools or [])

    def test_heal_resumes_waiting_chat_entry(self, monkeypatch, tmp_path):
        from systemu.pipelines import tool_service
        from systemu.vault.vault import Vault
        from sharing_on.config import Config
        from systemu.core.models import Activity, ActivityStatus, Tool, ToolStatus, ToolType
        vault = Vault(vault_dir=tmp_path)
        vault.save_tool(Tool(id="tool_r", name="search_x", description="d",
                             tool_type=ToolType.API_CALL, status=ToolStatus.DEPLOYED, enabled=True))
        act = Activity(id="act_h", name="n", scroll_id="scr", required_tool_ids=["tool_r"],
                       missing_tools=["search_x"], status=ActivityStatus.PARTIAL)
        vault.save_activity(act)
        vault.append_chat_history({"ts": "2026-05-31T00:00:00", "prompt": "n",
                                   "status": "waiting_on_tools", "activity_id": "act_h"})
        monkeypatch.setattr("systemu.pipelines.shadow_decision.decide_shadow", lambda *a, **k: None)
        tool_service._heal_partial_activities("tool_r", Config(), vault)
        latest = vault.load_chat_history(limit=5)[-1]
        assert latest["status"] == "running"


class TestReviewedApproveInstallsEnables:
    def test_helper_installs_enables_heals(self, monkeypatch, tmp_path):
        import systemu.interface.pages.tools as tools_pg
        from systemu.vault.vault import Vault
        from sharing_on.config import Config
        from systemu.core.models import Tool, ToolStatus, ToolType
        vault = Vault(vault_dir=tmp_path)
        vault.save_tool(Tool(id="tool_e", name="search_x", description="d",
                             tool_type=ToolType.API_CALL, status=ToolStatus.FORGED,
                             dependencies=["requests"], implementation_path="x.py"))
        installs, heals = [], []
        monkeypatch.setattr("systemu.runtime.dep_approvals.approve_and_install",
                            lambda *, tool_id, package, source="": installs.append((tool_id, package)))
        monkeypatch.setattr("systemu.pipelines.tool_service.heal_activities_for_tool",
                            lambda tid, c, v: heals.append(tid))
        tools_pg._approve_install_and_enable("tool_e", vault, Config())
        assert installs == [("tool_e", "requests")]
        assert vault.get_tool("tool_e").enabled is True
        assert heals == ["tool_e"]


class TestDepReminderRoutesToReview:
    def test_dep_reminder_dispatch_opens_review_dialog(self, monkeypatch):
        import systemu.interface.pages.notifications_page as npg
        opened = []
        monkeypatch.setattr(npg, "_open_forge_dialog", lambda tid: opened.append(tid))
        monkeypatch.setattr(npg.ui, "notify", lambda *a, **k: None)
        ctx = {"notification_type": "dep_reminder", "tool_id": "tool_z"}
        npg._dispatch_notification_action("Review & approve", ctx, vault=None, refresh_fn=None)
        assert opened == ["tool_z"]
