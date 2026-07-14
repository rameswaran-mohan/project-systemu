"""v0.10.21 — a chat task parked on a tool/command approval gate during its
FIRST tool call (iteration 1, BEFORE any resume snapshot is written) must still
resume after the operator approves.

Root cause (live tryout bug, "task got stuck even after approval"):

  * ``resume_on_decision`` derives the resume coords (activity_id/shadow_id)
    from the parked run's execution SNAPSHOT. But the tool gate raises
    ``PendingOperatorDecision`` from ``ToolSandbox`` on the run's FIRST tool
    call — before any park-rail inside ``ShadowRuntime.execute`` has written a
    snapshot. No snapshot ⇒ no coords ⇒ "missing resume coords — skipping",
    forever. The task never resumes.
  * Coupled: for a TOOL gate the standing "Always allow" is recorded ONLY inside
    ``_dispatch_resume`` (AFTER the coords check), so when that bails the allow
    is never persisted either — even a manual re-run re-hits the same gate.

The fix carries activity_id + shadow_id as run-scoped contextvars (mirroring the
v0.9.52 ``execution_id`` carrier) and stamps them into the gate decision context
at park time, so the resumer reads coords from the context — no snapshot needed.
And ``_dispatch_resume`` records the gate approval + stamps ``resume_dispatched``
even when coords are absent, so an already-stuck decision stops re-logging and a
manual re-run succeeds.
"""
from __future__ import annotations

import asyncio

from types import SimpleNamespace

from systemu.core.models import ActivityStatus
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature


# ── 1. the new run-coord contextvar carriers ─────────────────────────────────

def test_ctx_carriers_activity_and_shadow_roundtrip():
    from systemu.runtime import chat_submission_ctx as ctx

    assert ctx.current_activity_id() is None
    assert ctx.current_shadow_id() is None

    at = ctx.set_activity_id("act_99")
    st = ctx.set_shadow_id("sh_99")
    try:
        assert ctx.current_activity_id() == "act_99"
        assert ctx.current_shadow_id() == "sh_99"
    finally:
        ctx.set_activity_id(None, reset_token=at)
        ctx.set_shadow_id(None, reset_token=st)

    assert ctx.current_activity_id() is None
    assert ctx.current_shadow_id() is None


# ── 2. the tool gate stamps the run coords into the decision context ──────────

def _sandbox_with_store(tmp_path, vault=None):
    from systemu.runtime.tool_sandbox import ToolSandbox
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=vault, command_approvals=store)
    return sb, store


def _make_tool(tmp_path, *, name, effect_tags):
    from systemu.core.models import Tool, ToolType
    impl_dir = tmp_path.parent / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_file = impl_dir / f"{name}.py"
    impl_file.write_text("def run():\n    return {'success': True}\n", encoding="utf-8")
    rel = impl_file.relative_to(tmp_path.parent)
    return Tool(
        id=f"tool_{name}", name=name, description=f"test {name}",
        tool_type=ToolType.PYTHON_FUNCTION, implementation_path=str(rel),
        effect_tags=list(effect_tags), version=1,
    )


def test_tool_gate_stamps_resume_coords(tmp_path, monkeypatch):
    """A tool gate posted while the run-coord carriers are set stamps
    activity_id + shadow_id into context_extras (so the resumer needs no
    snapshot)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import chat_submission_ctx as ctx

    posted = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass
        def enqueue(self, descriptor, *, gate_type, context_extras=None, **kw):
            posted["extras"] = context_extras or {}
            return "dec_tool_c"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())
    tool = _make_tool(tmp_path, name="web_search", effect_tags=["net_mutate"])

    at = ctx.set_activity_id("act_iter1")
    st = ctx.set_shadow_id("sh_iter1")
    et = ctx.set_execution_id("exec_iter1")
    try:
        try:
            asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))
        except PendingOperatorDecision:
            pass
    finally:
        ctx.set_activity_id(None, reset_token=at)
        ctx.set_shadow_id(None, reset_token=st)
        ctx.set_execution_id(None, reset_token=et)

    assert posted["extras"].get("activity_id") == "act_iter1"
    assert posted["extras"].get("shadow_id") == "sh_iter1"
    assert posted["extras"].get("execution_id") == "exec_iter1"


def test_command_gate_stamps_resume_coords(tmp_path, monkeypatch):
    """The command gate stamps the same run coords (symmetry — a command parked
    at iteration 1 is resumable too)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import chat_submission_ctx as ctx

    posted = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass
        def enqueue(self, descriptor, *, gate_type, context_extras=None, **kw):
            posted["extras"] = context_extras or {}
            return "dec_cmd_c"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    sb, _store = _sandbox_with_store(tmp_path, vault=object())

    at = ctx.set_activity_id("act_cmd")
    st = ctx.set_shadow_id("sh_cmd")
    try:
        try:
            sb._maybe_gate_command("run_command", {"command": "rm -rf build", "cwd": "/proj"})
        except PendingOperatorDecision:
            pass
    finally:
        ctx.set_activity_id(None, reset_token=at)
        ctx.set_shadow_id(None, reset_token=st)

    assert posted["extras"].get("activity_id") == "act_cmd"
    assert posted["extras"].get("shadow_id") == "sh_cmd"


# ── 3. the resumer uses context coords when NO snapshot exists ────────────────

SIG = tool_signature("web_search", "bodyhash", ["net_mutate"], host_class="")


class _Dec:
    def __init__(self, ctx, choice, did="dec_iter1"):
        self.id, self.context, self.choice = did, ctx, choice


class _Vault:
    def save_decision(self, d):
        pass
    def get_activity(self, aid):
        return SimpleNamespace(status=ActivityStatus.PARTIAL)
    def save_activity(self, a):
        pass


class _Sup:
    def __init__(self):
        self.submits = []
    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append((activity_id, shadow_id, kw.get("resume_from_execution_id")))


def _bind_no_snapshot(monkeypatch, tmp_path):
    """No snapshot on disk: read_snapshot returns None (the iteration-1 case)."""
    from systemu.runtime import execution_snapshot as es
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: None)
    store = ca.CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    return store


def _ctx(**extra):
    base = {"kind": "gate", "gate_type": "tool", "tool_signature": SIG,
            "tool_name": "web_search", "execution_id": "exec_iter1",
            "chat_submission_id": "sub_iter1"}
    base.update(extra)
    return base


def test_resume_uses_context_coords_without_snapshot(monkeypatch, tmp_path):
    """THE CORE FIX: a tool-gate decision that carries activity_id+shadow_id in
    its context resumes even with NO snapshot — the run re-submits with the
    stamped coords + resume_from_execution_id."""
    from systemu.runtime import resume_on_decision as rod
    _bind_no_snapshot(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    dec = _Dec(_ctx(activity_id="act_iter1", shadow_id="sh_iter1"), "Always allow")
    ok = rod._dispatch_resume(dec, vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is True
    assert sup.submits == [("act_iter1", "sh_iter1", "exec_iter1")]


def test_missing_coords_gate_records_allow_and_stamps(monkeypatch, tmp_path):
    """PART B (rescue): a tool-gate decision with NO coords anywhere (no context
    coords, no snapshot) still records the STANDING allow and stamps
    resume_dispatched — so the 15s re-log stops and a manual re-run succeeds.
    It does NOT re-submit (there are no coords to submit)."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_no_snapshot(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ctx = _ctx()  # no activity_id / shadow_id
    dec = _Dec(ctx, "Always allow")
    ok = rod._dispatch_resume(dec, vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert sup.submits == []                       # nothing to resume
    assert store.is_approved(SIG) is True          # standing allow persisted
    assert ctx.get("resume_dispatched") is True    # stops the reconciler re-log
    assert ok is True


def test_refused_snapshot_gate_does_not_record_or_stamp(monkeypatch, tmp_path):
    """DEC-9 GUARD: a REFUSED snapshot (newer schema — not merely absent) means the
    parked run may have done effectful work this build can't read. The coords-less
    rescue must NOT fire: no approval recorded, not stamped, returns False (honest
    skip). Distinguishes 'snapshot absent' (safe iteration-1) from 'snapshot refused'."""
    from systemu.runtime import execution_snapshot as es
    from systemu.runtime.snapshot_migrations import SnapshotRefused
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod

    rod._handled.clear()
    store = ca.CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)

    def _refuse(eid, data_dir=None):
        raise SnapshotRefused(999, 2)   # (version, current) — newer than this build
    monkeypatch.setattr(es, "read_snapshot", _refuse)

    v, sup = _Vault(), _Sup()
    ctx = _ctx()  # no context coords → must rely on snapshot, which is refused
    dec = _Dec(ctx, "Always allow")
    ok = rod._dispatch_resume(dec, vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is False
    assert sup.submits == []
    assert store.is_approved(SIG) is False          # NOT recorded
    assert ctx.get("resume_dispatched") is not True  # NOT stamped


def test_missing_coords_approve_once_records_no_dangling_bridge(monkeypatch, tmp_path):
    """PART B safety (adversarial finding #1): a coords-less "Approve once" must NOT
    persist a SINGLE-USE bridge — with no run to consume it immediately, a dangling
    one-shot keyed on the params-independent tool signature could later be spent by an
    UNRELATED call. So: nothing standing, NO bridge, but still stamp dispatched (stop
    the re-log). A manual re-run re-asks — correct "approve once" semantics."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_no_snapshot(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ctx = _ctx()
    dec = _Dec(ctx, "Approve once")
    rod._dispatch_resume(dec, vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert store.is_approved(SIG) is False              # not standing
    assert store.consume_resume_approved(SIG) is False  # NO dangling one-shot bridge
    assert sup.submits == []
    assert ctx.get("resume_dispatched") is True         # stops the reconciler re-log
