"""S1b Step 3 (Task 4): a chat task parked on a gate_type="tool" action gate is
RESUMED when the operator resolves it — mirroring the v0.9.52 command-gate resume
path, plus the NEW three-way split that wires "Always allow" to a STANDING approval.

  * Deny            → finalize the activity FAILED, no re-submit (no re-ask loop).
  * Approve once    → SINGLE-USE resume approval (consumed once, never promoted).
  * Always allow    → STANDING allow-list entry (store.is_approved(sig) is True).

The tool signature is the value STAMPED into the decision context by the gate
(Task 3) — the resume path reads it back, it is NOT recomputed here. The
choice normalization is ``(decision.choice or "").strip().lower()``; the gate's
option label "Always allow" therefore matches as the literal ``"always allow"``.
"""
from __future__ import annotations

import json

from types import SimpleNamespace

from systemu.core.models import ActivityStatus
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature


# A representative stamped tool signature (order-insensitive over effect_tags).
SIG = tool_signature("send_email", "bodyhash123", ["send_message"], host_class="")


# ── unit-level dispatch (mirrors tests/test_command_gate_resume.py) ───────────

class _Dec:
    def __init__(self, ctx, choice):
        self.id, self.context, self.choice = "dec_tool_x", ctx, choice


class _Snap:
    activity_id, shadow_id = "act_1", "shadow_1"


class _Vault:
    def __init__(self):
        self.activity_status = None
    def save_decision(self, d):
        pass
    def get_activity(self, aid):
        return SimpleNamespace(status=ActivityStatus.PARTIAL)
    def save_activity(self, a):
        self.activity_status = a.status


class _Sup:
    def __init__(self):
        self.submits = []
    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append((activity_id, shadow_id, kw.get("resume_from_execution_id")))


def _tool_dec(choice, sig=SIG):
    return _Dec({"kind": "gate", "gate_type": "tool", "tool_signature": sig,
                 "tool_name": "send_email",
                 "execution_id": "exec_A", "chat_submission_id": "sub_1"}, choice)


def _bind_store(monkeypatch, tmp_path):
    """Point the resume path's default store at a REAL store in tmp (no mocks)."""
    from systemu.runtime import execution_snapshot as es
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: _Snap())
    store = ca.CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    return store


def test_tool_gate_discriminator_resumes(monkeypatch, tmp_path):
    """1. A resolved gate_type="tool" decision + snapshot with coords resumes:
    _dispatch_resume returns True and re-submits with resume_from_execution_id."""
    from systemu.runtime import resume_on_decision as rod
    _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_tool_dec("Approve once"), vault=v, supervisor=sup,
                              data_dir=str(tmp_path))
    assert ok is True
    assert sup.submits == [("act_1", "shadow_1", "exec_A")]


def test_tool_gate_deny_finalizes_failed(monkeypatch, tmp_path):
    """2. Deny finalizes the activity FAILED and does NOT re-submit."""
    from systemu.runtime import resume_on_decision as rod
    _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_tool_dec("Deny"), vault=v, supervisor=sup,
                              data_dir=str(tmp_path))
    assert ok is True
    assert sup.submits == []
    assert v.activity_status == ActivityStatus.FAILED


def test_tool_gate_always_allow_is_standing(monkeypatch, tmp_path):
    """3. THE DRIFT GUARD: "Always allow" records a STANDING approval keyed by the
    STAMPED tool_signature — store.is_approved(SIG) is True afterwards."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_tool_dec("Always allow"), vault=v, supervisor=sup,
                              data_dir=str(tmp_path))
    assert ok is True
    assert sup.submits == [("act_1", "shadow_1", "exec_A")]   # still resumes
    assert store.is_approved(SIG) is True                     # standing allow-list
    # A standing approval is NOT a single-use bridge.
    assert store.consume_resume_approved(SIG) is False


def test_tool_gate_approve_once_is_single_use(monkeypatch, tmp_path):
    """4. "Approve once" records a SINGLE-USE resume approval — NOT standing, and
    consumable exactly once."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_tool_dec("Approve once"), vault=v, supervisor=sup,
                              data_dir=str(tmp_path))
    assert ok is True
    assert store.is_approved(SIG) is False                    # NOT promoted to standing
    assert store.consume_resume_approved(SIG) is True         # honored once
    assert store.consume_resume_approved(SIG) is False        # single-use


# ── cross-process reconciler (mirrors test_v0_8_22_1_cross_process_resume.py) ──

def _make_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _seed_snapshot(tmp_path, *, execution_id="exec_T", shadow_id="sh_T",
                   scroll_id="scroll_T", activity_id="act_T"):
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    data_dir = tmp_path / "data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    write_snapshot(
        ExecutionSnapshot(
            execution_id=execution_id, shadow_id=shadow_id,
            scroll_id=scroll_id, activity_id=activity_id,
            completed_objective_ids=[0],
        ),
        data_dir=data_dir,
    )
    return data_dir


class _FakeSupervisor:
    def __init__(self):
        self.calls = []
    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append((activity_id, shadow_id, kw))
        return f"sub_{len(self.calls)}"


def test_reconciler_dispatches_tool_gate(monkeypatch, tmp_path):
    """5. A resolved gate_type="tool" decision written to a REAL vault index +
    snapshot is re-dispatched by the cross-process reconciler (which today skips
    gate_type=="tool"). Returns 1 with the right resume coords."""
    from systemu.runtime import resume_on_decision as rod
    import systemu.runtime.command_approvals as ca
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

    rod._handled.clear()
    # Keep the standing-approval side-effect hermetic (no write to ./data).
    store = ca.CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)

    vlt = _make_vault(tmp_path)
    data_dir = _seed_snapshot(tmp_path)

    queue = OperatorDecisionQueue(vlt)
    did = queue.post(
        title="Run tool: send_email", body="?",
        options=["Deny", "Approve once", "Always allow"],
        context={
            "kind": "gate", "gate_type": "tool",
            "tool_signature": SIG, "tool_name": "send_email",
            "chat_submission_id": "2026-07-05T10:00:00",
            "execution_id": "exec_T",
            # activity_id/shadow_id NOT in context — derived from the snapshot.
        },
        dedup_key=f"tool:{SIG}",
    )
    queue.resolve(did, choice="Always allow")

    before = vlt.get_decision(did)
    assert before.status == "resolved"
    assert not (before.context or {}).get("resume_dispatched")

    sup = _FakeSupervisor()
    dispatched = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)

    assert dispatched == 1
    assert len(sup.calls) == 1
    aid, sid, kw = sup.calls[0]
    assert aid == "act_T"
    assert sid == "sh_T"
    assert kw["resume_from_execution_id"] == "exec_T"
    assert kw["chat_submission_id"] == "2026-07-05T10:00:00"
    # Always-allow persisted the STANDING approval keyed by the stamped signature.
    assert store.is_approved(SIG) is True
    # Idempotent: a second tick does not re-dispatch.
    assert reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir) == 0
    assert len(sup.calls) == 1
