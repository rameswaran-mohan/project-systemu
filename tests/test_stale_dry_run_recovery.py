"""v0.9.51 — a tool whose dry-run failed under an OLDER systemu version is
re-validated after an upgrade (the fix may now make it pass), bounded so a
current-version failure never loops."""
from __future__ import annotations

import systemu
from systemu.core.models import Tool, ToolType, ToolStatus
from systemu.scheduler.tool_reconciler import recover_stale_dry_run_failures

CUR = systemu.__version__


def _forged_failed(tid, version):
    return Tool(
        id=tid, name=tid, description="d", tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.FORGED, dry_run_status="failed",
        dry_run_evidence={"error": "boom", "systemu_version": version},
    )


class _Vault:
    def __init__(self, tools):
        self._tools = {t.id: t for t in tools}
        self.saved = []

    def load_index(self, kind):
        return [{"id": t.id, "status": "forged", "dry_run_status": t.dry_run_status}
                for t in self._tools.values()]

    def get_tool(self, tid):
        return self._tools[tid]

    def save_tool(self, t):
        self.saved.append(t.id)


def test_resets_tool_failed_under_older_version():
    stale = _forged_failed("t_old", "0.9.49")
    v = _Vault([stale])
    n = recover_stale_dry_run_failures(v)
    assert n == 1
    assert stale.dry_run_status == "not_run" and stale.dry_run_evidence == {}
    assert v.saved == ["t_old"]


def test_leaves_current_version_failure_alone():
    cur = _forged_failed("t_cur", CUR)
    v = _Vault([cur])
    assert recover_stale_dry_run_failures(v) == 0
    assert cur.dry_run_status == "failed"          # no loop
    assert v.saved == []


def test_resets_unstamped_legacy_failure():
    # a pre-v0.9.51 failure has no version stamp → treated as stale → reset once
    legacy = _forged_failed("t_legacy", None)
    legacy.dry_run_evidence = {"error": "TypeError: run() got an unexpected keyword argument 'dry_run'"}
    v = _Vault([legacy])
    assert recover_stale_dry_run_failures(v) == 1
    assert legacy.dry_run_status == "not_run"


def test_ignores_passed_and_skipped_tools():
    passed = _forged_failed("t_pass", "0.9.49"); passed.dry_run_status = "passed"
    v = _Vault([passed])
    assert recover_stale_dry_run_failures(v) == 0
    assert v.saved == []


def test_to_evidence_stamps_current_version():
    from systemu.pipelines.tool_dry_run import DryRunResult
    ev = DryRunResult(success=False, status="failed", error="x").to_evidence()
    assert ev["systemu_version"] == CUR


# ── demand-driven re-validation: a parked task's stale-failed tool gets a fresh
#    dry-run under current code, ALWAYS-ON (not just startup), bounded per pair ──

class _ActVault:
    def __init__(self, tool):
        self.tool = tool
        self.saved = []

    class _Act:
        required_tool_ids = ["zip"]

    def get_activity(self, aid):
        return self._Act()

    def get_tool(self, tid):
        return self.tool

    def save_tool(self, t):
        self.saved.append(t.id)


def test_demand_revalidate_recovers_passing_tool(monkeypatch):
    from systemu.scheduler import tool_reconciler as tr
    import systemu.pipelines.tool_dry_run as dr_mod
    tr._revalidated_pairs.clear()
    tool = _forged_failed("zip", "0.9.49")
    v = _ActVault(tool)

    class _Res:
        success, status, error = True, "passed", None
        def to_evidence(self): return {"status": "passed", "systemu_version": CUR}

    monkeypatch.setattr(dr_mod, "dry_run_tool", lambda tool, **kw: _Res())
    n = tr.revalidate_blocking_failed_tools(v, object(), "act1")
    assert n == 1
    assert tool.status == ToolStatus.DEPLOYED and tool.dry_run_status == "passed"
    assert v.saved == ["zip"]


def test_demand_revalidate_bounded_once_per_pair(monkeypatch):
    from systemu.scheduler import tool_reconciler as tr
    import systemu.pipelines.tool_dry_run as dr_mod
    tr._revalidated_pairs.clear()
    tool = _forged_failed("zip", "0.9.49")
    v = _ActVault(tool)
    calls = []

    class _Res:
        success, status, error = False, "failed", "still broken"
        def to_evidence(self): return {"status": "failed", "systemu_version": CUR}

    monkeypatch.setattr(dr_mod, "dry_run_tool", lambda tool, **kw: (calls.append(1), _Res())[1])
    tr.revalidate_blocking_failed_tools(v, object(), "act1")
    tr.revalidate_blocking_failed_tools(v, object(), "act1")   # 2nd call → no re-run
    assert len(calls) == 1                       # bounded: re-validated once, never loops
    assert tool.status == ToolStatus.FORGED and tool.dry_run_status == "failed"


def test_demand_revalidate_separate_activity_gets_its_own_attempt(monkeypatch):
    from systemu.scheduler import tool_reconciler as tr
    import systemu.pipelines.tool_dry_run as dr_mod
    tr._revalidated_pairs.clear()
    tool = _forged_failed("zip", "0.9.49")
    v = _ActVault(tool)
    calls = []

    class _Res:
        success, status, error = False, "failed", "x"
        def to_evidence(self): return {"status": "failed"}

    monkeypatch.setattr(dr_mod, "dry_run_tool", lambda tool, **kw: (calls.append(1), _Res())[1])
    tr.revalidate_blocking_failed_tools(v, object(), "act1")
    tr.revalidate_blocking_failed_tools(v, object(), "act2")   # different task → fresh attempt
    assert len(calls) == 2


def test_demand_revalidate_force_bypasses_bound(monkeypatch):
    # the operator's explicit "Enable & run" must re-validate even if the automatic
    # reaper already tried this (activity, tool) this session.
    from systemu.scheduler import tool_reconciler as tr
    import systemu.pipelines.tool_dry_run as dr_mod
    tr._revalidated_pairs.clear()
    tool = _forged_failed("zip", "0.9.49")
    v = _ActVault(tool)
    calls = []

    class _Res:
        success, status, error = False, "failed", "x"
        def to_evidence(self): return {"status": "failed"}

    monkeypatch.setattr(dr_mod, "dry_run_tool", lambda tool, **kw: (calls.append(1), _Res())[1])
    tr.revalidate_blocking_failed_tools(v, object(), "act1")               # 1: runs
    tr.revalidate_blocking_failed_tools(v, object(), "act1")               # 2: bounded, skipped
    tr.revalidate_blocking_failed_tools(v, object(), "act1", force=True)   # 3: force → runs
    assert len(calls) == 2
