"""v0.9.52: a chat task parked on a run_command approval is now RESUMED when the
operator resolves it (previously only structured_question questions resumed, so a
command gate hung forever). Approve → one-shot honor + re-submit; Deny → finalize."""
from __future__ import annotations

from types import SimpleNamespace

from systemu.core.models import ActivityStatus
from systemu.runtime.command_approvals import CommandApprovalStore, command_signature


# ── the one-shot resume-approval store ────────────────────────────────────────

def test_store_one_shot_resume_approval(tmp_path):
    s = CommandApprovalStore(tmp_path / "ca.json")
    sig = command_signature("dir C:\\x", cwd="C:\\")
    assert s.consume_resume_approved(sig) is False     # nothing marked yet
    s.mark_resume_approved(sig)
    assert s.consume_resume_approved(sig) is True       # honored once
    assert s.consume_resume_approved(sig) is False      # single-use, not a standing allow
    assert s.is_approved(sig) is False                  # NOT promoted to "Always allow"


# ── resume dispatch for a command gate ────────────────────────────────────────

class _Dec:
    def __init__(self, ctx, choice):
        self.id, self.context, self.choice = "dec_x", ctx, choice


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


def _cmd_dec(choice, command="dir", cwd="C:\\"):
    return _Dec({"kind": "gate", "gate_type": "command", "command": command, "cwd": cwd,
                 "execution_id": "exec_A", "chat_submission_id": "sub_1"}, choice)


def test_command_gate_approve_resumes(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    from systemu.runtime import execution_snapshot as es
    import systemu.runtime.command_approvals as ca
    rod._handled.clear()
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: _Snap())
    store = ca.CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)

    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_cmd_dec("Approve once"), vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is True
    assert sup.submits == [("act_1", "shadow_1", "exec_A")]      # re-submitted with resume_from
    # the resumed run will honor the command exactly once
    assert store.consume_resume_approved(command_signature("dir", cwd="C:\\")) is True


def test_command_gate_deny_finalizes_no_loop(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    from systemu.runtime import execution_snapshot as es
    rod._handled.clear()
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: _Snap())

    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_cmd_dec("Deny", command="rm -rf /"), vault=v, supervisor=sup,
                              data_dir=str(tmp_path))
    assert ok is True
    assert sup.submits == []                            # NOT re-submitted → no re-ask loop
    assert v.activity_status == ActivityStatus.FAILED   # finalized cleanly


def test_structured_question_still_resumes(monkeypatch, tmp_path):
    # regression: the original path (coords in context, snapshot answer stash) holds
    from systemu.runtime import resume_on_decision as rod
    from systemu.runtime import execution_snapshot as es
    rod._handled.clear()
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: None)
    dec = _Dec({"kind": "structured_question", "execution_id": "exec_A", "activity_id": "act_1",
                "shadow_id": "shadow_1", "chat_submission_id": "sub_1", "objective_id": "o1"}, "42")
    v, sup = _Vault(), _Sup()
    assert rod._dispatch_resume(dec, vault=v, supervisor=sup, data_dir=str(tmp_path)) is True
    assert sup.submits == [("act_1", "shadow_1", "exec_A")]


def test_non_command_gate_not_resumed(monkeypatch, tmp_path):
    # a capability/forge gate (kind="gate" but gate_type != "command") must NOT be
    # treated as a resumable command gate
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    dec = _Dec({"kind": "gate", "gate_type": "capability", "execution_id": "exec_A",
                "chat_submission_id": "sub_1"}, "Approve")
    assert rod._dispatch_resume(dec, vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path)) is False
