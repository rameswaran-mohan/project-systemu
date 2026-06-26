"""Wave 1.2 — a readiness-parked task must produce an ACTIONABLE Inbox gate.

The Stage-3.5 readiness gate parks tasks whose tools aren't deployed+enabled —
on a fresh install that's every first task, and the park was previously a log
line + chat status with no operator path forward.  Now it posts a unified
``tools_blocked`` gate: "Enable & run" resolves through resolve_gate → the
canonical Gate-3 ``tools_enable`` verb (Gate-3.5 dry-run rule stays enforced).
"""
import pytest

from systemu.core.models import Activity, ActivityStatus, Tool, ToolStatus
from systemu.interface.command.gate import GateDescriptor
from systemu.interface.command.inbox import InboxQueue, resolve_gate
from systemu.interface.readiness_gate import ensure_tools_blocked_gate
from systemu.storage.file_vault import FileVault
from systemu.vault.vault import Vault


@pytest.fixture()
def vault(tmp_path):
    return FileVault(Vault(str(tmp_path / "vault")))


def _tool(i: int, *, enabled=False, dry="passed", status=ToolStatus.FORGED) -> Tool:
    return Tool(
        id=f"tool_{i}", name=f"tool_{i}", description=f"test tool {i}",
        tool_type="api_call", status=status, enabled=enabled, dry_run_status=dry,
        implementation_path=f"vault/tools/implementations/tool_{i}.py",
    )


def _activity(vault, tool_ids) -> Activity:
    act = Activity(
        id="act_blocked", name="Blocked task", scroll_id="scr_1",
        status=ActivityStatus.PARTIAL, required_tool_ids=list(tool_ids),
        missing_tools=[f"tool_{i}" for i in range(len(tool_ids))],
    )
    vault.save_activity(act)
    return act


class TestDescriptor:
    def test_from_blocked_tools_shape(self, vault):
        tools = [_tool(1), _tool(2, dry="not_run")]
        act = _activity(vault, [t.id for t in tools])
        d = GateDescriptor.from_blocked_tools(act, tools)
        assert "2 tool(s)" in d.title
        assert d.dedup == "tools_blocked:act_blocked"
        assert d.options == ["Dismiss", "Enable & run"]
        assert d.safe_default == "Dismiss"
        assert "tool_1" in d.inspect and "dry-run: not_run" in d.inspect


class TestProducer:
    def test_enqueues_once_idempotently(self, vault):
        tools = [_tool(1)]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        dec1 = ensure_tools_blocked_gate(vault, act, tools)
        dec2 = ensure_tools_blocked_gate(vault, act, tools)
        assert dec1 == dec2
        queue = InboxQueue(vault)
        matches = [i for i, d in queue.list_descriptors()
                   if d.dedup == "tools_blocked:act_blocked"]
        assert len(matches) == 1

    def test_context_carries_tool_ids(self, vault):
        tools = [_tool(1), _tool(2)]
        act = _activity(vault, [t.id for t in tools])
        dec_id = ensure_tools_blocked_gate(vault, act, tools)
        decision = vault.get_decision(dec_id)
        assert decision.context.get("gate_type") == "tools_blocked"
        assert decision.context.get("tool_ids") == ["tool_1", "tool_2"]


class TestExecutor:
    def _resolved(self, vault, act, tools, choice):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        dec_id = ensure_tools_blocked_gate(vault, act, tools)
        OperatorDecisionQueue(vault).resolve(dec_id, choice=choice)
        return vault.get_decision(dec_id)

    def test_approve_enables_dry_run_passed_tool(self, vault):
        tools = [_tool(1, dry="passed")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools, "Enable & run")
        result = resolve_gate(decision, vault=vault)
        assert result.status.value == "ok"
        assert vault.get_tool("tool_1").enabled is True

    def test_approve_reports_not_dry_run_tool_without_enabling(self, vault):
        tools = [_tool(1, dry="not_run")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools, "Enable & run")
        result = resolve_gate(decision, vault=vault)
        assert result.status.value == "error"          # blocked, loudly
        assert "dry_run_status" in result.summary
        assert vault.get_tool("tool_1").enabled is False   # Gate-3.5 held

    def test_dismiss_is_noop(self, vault):
        tools = [_tool(1, dry="passed")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools, "Dismiss")
        result = resolve_gate(decision, vault=vault)
        assert result.status.value == "noop"
        assert vault.get_tool("tool_1").enabled is False

    def test_enable_and_run_fires_heal_sweep(self, vault, monkeypatch):
        """v0.9.43: resolving "Enable & run" must FIRE the heal sweep that
        re-dispatches the parked task. Previously only the Tools page called it,
        so the Inbox/forge path enabled the tool and then left the task stuck —
        the reported forge-demo hang."""
        tools = [_tool(1, dry="passed")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])

        calls = []
        import systemu.pipelines.tool_service as ts
        monkeypatch.setattr(
            ts, "heal_activities_for_tool",
            lambda tid, cfg, v: calls.append(tid))
        import sharing_on.config as cfgmod
        monkeypatch.setattr(
            cfgmod.Config, "from_env", classmethod(lambda cls: object()))
        # Run the spawned heal thread synchronously for a deterministic assert.
        import threading

        class _SyncThread:
            def __init__(self, target=None, args=(), daemon=None):
                self._t, self._a = target, args

            def start(self):
                self._t(*self._a)

        monkeypatch.setattr(threading, "Thread", _SyncThread)

        decision = self._resolved(vault, act, tools, "Enable & run")
        result = resolve_gate(decision, vault=vault)
        assert result.status.value == "ok"
        assert vault.get_tool("tool_1").enabled is True
        assert calls == ["tool_1"]      # heal sweep fired for the enabled tool

    def test_blocked_tool_does_not_fire_heal_sweep(self, vault, monkeypatch):
        """A tool held by Gate-3.5 (dry-run not passed) is never enabled, so the
        heal sweep must NOT fire — there is nothing newly ready to re-dispatch."""
        tools = [_tool(1, dry="not_run")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])

        calls = []
        import systemu.pipelines.tool_service as ts
        monkeypatch.setattr(
            ts, "heal_activities_for_tool",
            lambda tid, cfg, v: calls.append(tid))

        decision = self._resolved(vault, act, tools, "Enable & run")
        result = resolve_gate(decision, vault=vault)
        assert result.status.value == "error"
        assert calls == []


class TestWiring:
    def test_direct_task_posts_the_gate(self):
        import inspect
        from systemu.pipelines import direct_task
        assert "ensure_tools_blocked_gate" in inspect.getsource(direct_task)

    def test_resolver_wires_the_heal_sweep(self):
        """Guard: the tools_blocked resolver must reference the heal sweep so the
        re-dispatch can never silently disappear again (v0.9.43 regression fix)."""
        import inspect
        from systemu.interface.command import inbox
        assert "heal_activities_for_tool" in inspect.getsource(inbox)

    def test_work_page_warn_tint(self):
        from systemu.interface.pages.work import _status_class
        assert _status_class("waiting_on_tools") == "warn"
        assert _status_class("partial") == "warn"

    def test_detail_blocked_tools_model(self, vault):
        from systemu.interface.pages.workflow_detail import blocked_tools_of
        act = _activity(vault, ["tool_1"])
        assert blocked_tools_of(act) == ["tool_0"]
        act.status = ActivityStatus.COMPLETED
        assert blocked_tools_of(act) == []


class TestReconcilerCompletesDeferredEnable:
    """v0.9.44: the Enable&run / dry-run RACE — operator approves "Enable & run"
    before the reconciler finishes the dry-run, so Gate-3.5 holds the enable and
    the tool ends DEPLOYED-but-disabled. The reconciler sweep must complete it."""

    def _resolved_enable_gate(self, vault, tools):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        act = _activity(vault, [t.id for t in tools])
        dec_id = ensure_tools_blocked_gate(vault, act, tools)
        OperatorDecisionQueue(vault).resolve(dec_id, choice="Enable & run")
        return act

    def test_deferred_enable_after_dry_run_race(self, vault, monkeypatch):
        import systemu.pipelines.tool_service as ts
        import systemu.scheduler.tool_reconciler as recon
        # the race state: dry-run PASSED, DEPLOYED, but still disabled
        tools = [_tool(1, dry="passed", status=ToolStatus.DEPLOYED, enabled=False)]
        for t in tools:
            vault.save_tool(t)
        self._resolved_enable_gate(vault, tools)

        healed = []
        monkeypatch.setattr(ts, "heal_activities_for_tool",
                            lambda tid, cfg, v: healed.append(tid))

        class _Sync:   # run the heal thread synchronously for a deterministic assert
            def __init__(self, target=None, args=(), daemon=None):
                self._t, self._a = target, args

            def start(self):
                self._t(*self._a)
        monkeypatch.setattr("threading.Thread", _Sync)

        recon._complete_deferred_enables(vault, None)
        assert vault.get_tool("tool_1").enabled is True      # enabled now
        assert healed == ["tool_1"]                          # and the heal fired

    def test_deferred_enable_skips_tool_without_dry_run(self, vault, monkeypatch):
        import systemu.pipelines.tool_service as ts
        import systemu.scheduler.tool_reconciler as recon
        # dry-run NOT passed -> Gate-3.5 holds: the sweep must NOT enable it
        tools = [_tool(1, dry="not_run", status=ToolStatus.FORGED, enabled=False)]
        for t in tools:
            vault.save_tool(t)
        self._resolved_enable_gate(vault, tools)
        healed = []
        monkeypatch.setattr(ts, "heal_activities_for_tool",
                            lambda *a, **k: healed.append(a))
        recon._complete_deferred_enables(vault, None)
        assert vault.get_tool("tool_1").enabled is False
        assert healed == []

    def test_deferred_enable_idempotent_when_already_enabled(self, vault, monkeypatch):
        import systemu.pipelines.tool_service as ts
        import systemu.scheduler.tool_reconciler as recon
        tools = [_tool(1, dry="passed", status=ToolStatus.DEPLOYED, enabled=True)]
        for t in tools:
            vault.save_tool(t)
        self._resolved_enable_gate(vault, tools)
        healed = []
        monkeypatch.setattr(ts, "heal_activities_for_tool",
                            lambda *a, **k: healed.append(a))
        recon._complete_deferred_enables(vault, None)
        assert healed == []          # already enabled -> no re-enable, no re-heal

    def test_reconciler_wires_deferred_enable(self):
        import inspect

        from systemu.scheduler import tool_reconciler
        src = inspect.getsource(tool_reconciler)
        assert "_complete_deferred_enables" in src
        assert src.count("_complete_deferred_enables(vault, config)") >= 2  # both exit paths


def _failed_tool(i: int, *, error="OOXMLFile.encrypt() missing 1 required positional argument: 'outfile'"):
    """A tool whose dry-run is permanently FAILED (can never deploy), with the
    dry-run error captured in evidence the way the dry-run pipeline records it."""
    return Tool(
        id=f"tool_{i}", name=f"tool_{i}", description=f"test tool {i}",
        tool_type="api_call", status=ToolStatus.FORGED, enabled=False,
        dry_run_status="failed", dry_run_evidence={"error": error},
        implementation_path=f"vault/tools/implementations/tool_{i}.py",
    )


class TestExecutorFinalizesUndeployable:
    """Phase 4.1 (Defect D): when the tools_blocked gate's activity awaits a
    REQUIRED tool that can never deploy (dry_run_status == "failed") and nothing
    was newly enabled, the parked activity must be FINALIZED FAILED with the
    dry-run error surfaced — not left PARTIAL/waiting forever. A `not_run` tool
    is still retryable (stays PARTIAL); a `skipped` tool is NEVER reaped."""

    def _resolved(self, vault, act, tools, choice="Enable & run"):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        dec_id = ensure_tools_blocked_gate(vault, act, tools)
        OperatorDecisionQueue(vault).resolve(dec_id, choice=choice)
        return vault.get_decision(dec_id)

    def test_failed_required_tool_finalizes_activity(self, vault):
        tools = [_failed_tool(1)]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools)
        result = resolve_gate(decision, vault=vault)

        assert result.status.value == "error"
        assert vault.get_activity("act_blocked").status == ActivityStatus.FAILED
        # the dry-run error is surfaced to the operator in the result summary
        assert "outfile" in result.summary

    def test_failed_tool_flips_waiting_chat_entry(self, vault):
        tools = [_failed_tool(1)]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        vault.append_chat_history(
            {"ts": "t-1", "activity_id": "act_blocked", "status": "waiting_on_tools"})
        decision = self._resolved(vault, act, tools)
        resolve_gate(decision, vault=vault)

        flipped = [e for e in vault.load_chat_history(limit=50)
                   if e.get("ts") == "t-1"]
        assert flipped and flipped[0]["status"] == "failed"

    def test_not_run_tool_keeps_activity_partial(self, vault):
        # a never-dry-run tool is transient/retryable — must NOT be reaped.
        tools = [_tool(1, dry="not_run")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools)
        result = resolve_gate(decision, vault=vault)

        assert result.status.value == "error"   # held by Gate-3.5, loudly
        assert vault.get_activity("act_blocked").status == ActivityStatus.PARTIAL

    def test_skipped_tool_is_never_reaped(self, vault):
        # Defect-B guard: a `skipped` tool (incl. an operator_verify skip) is
        # NOT permanent — the activity must stay PARTIAL, never FAILED.
        tools = [_tool(1, dry="skipped")]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools)
        resolve_gate(decision, vault=vault)

        assert vault.get_activity("act_blocked").status != ActivityStatus.FAILED

    def test_failed_but_something_enabled_does_not_reap(self, vault):
        # If at least one tool was newly enabled, the activity may still proceed —
        # the `and not enabled` guard means we don't reap on a mixed result.
        tools = [_tool(1, dry="passed"), _failed_tool(2)]
        for t in tools:
            vault.save_tool(t)
        act = _activity(vault, [t.id for t in tools])
        decision = self._resolved(vault, act, tools)
        resolve_gate(decision, vault=vault)

        assert vault.get_activity("act_blocked").status != ActivityStatus.FAILED


class TestReconcilerReapsUnsatisfiable:
    """Phase 4.2: the reconciler is the safety net for a parked activity whose
    awaited REQUIRED tool can never deploy — even if the operator never resolves
    the gate. Every reconcile tick, any PARTIAL activity whose required-tool set
    has a `dry_run_status == "failed"` tool is finalized FAILED (idempotent)."""

    def _partial_activity(self, vault, act_id, tool_ids):
        act = Activity(
            id=act_id, name="Blocked", scroll_id="scr_1",
            status=ActivityStatus.PARTIAL, required_tool_ids=list(tool_ids),
        )
        vault.save_activity(act)
        return act

    def test_partial_with_failed_tool_is_reaped(self, vault):
        import systemu.scheduler.tool_reconciler as recon
        vault.save_tool(_failed_tool(1))
        self._partial_activity(vault, "act_reap", ["tool_1"])

        recon._fail_unsatisfiable_blocked_activities(vault, None)
        assert vault.get_activity("act_reap").status == ActivityStatus.FAILED

    def test_partial_with_not_run_tool_left_partial(self, vault):
        import systemu.scheduler.tool_reconciler as recon
        vault.save_tool(_tool(1, dry="not_run"))
        self._partial_activity(vault, "act_wait", ["tool_1"])

        recon._fail_unsatisfiable_blocked_activities(vault, None)
        assert vault.get_activity("act_wait").status == ActivityStatus.PARTIAL

    def test_partial_with_skipped_tool_left_partial(self, vault):
        # Defect-B guard: a `skipped` tool is not permanent — never reaped.
        import systemu.scheduler.tool_reconciler as recon
        vault.save_tool(_tool(1, dry="skipped"))
        self._partial_activity(vault, "act_skip", ["tool_1"])

        recon._fail_unsatisfiable_blocked_activities(vault, None)
        assert vault.get_activity("act_skip").status == ActivityStatus.PARTIAL

    def test_reap_is_idempotent(self, vault):
        # Second tick must be a no-op — an already-terminal activity is skipped.
        import systemu.scheduler.tool_reconciler as recon
        vault.save_tool(_failed_tool(1))
        self._partial_activity(vault, "act_idem", ["tool_1"])

        recon._fail_unsatisfiable_blocked_activities(vault, None)
        assert vault.get_activity("act_idem").status == ActivityStatus.FAILED
        # second pass: still FAILED, no crash
        recon._fail_unsatisfiable_blocked_activities(vault, None)
        assert vault.get_activity("act_idem").status == ActivityStatus.FAILED

    def test_reconciler_wires_reaper_on_both_paths(self):
        import inspect
        from systemu.scheduler import tool_reconciler
        src = inspect.getsource(tool_reconciler)
        assert "_fail_unsatisfiable_blocked_activities" in src
        # called on BOTH reconcile_once exit paths (next to _complete_deferred_enables)
        assert src.count("_fail_unsatisfiable_blocked_activities(vault, config)") >= 2
