"""IMPL-11 (queue item 2f) - the resume reconciler never resumes a cancelled run.

Witnessed gap: ``reconcile_resolved_stuck_decisions`` walks RESOLVED cards and
re-dispatches each one's run. A run parked with two cards - one answered from
the CLI (a separate process, so only this poll will ever act on it) and one
still pending - was cancelled before the next poll: the cancellation expired the
pending card, but the poll then re-dispatched the run from the answered one,
resurrecting the run the operator stopped.

Ruling under test: a resolved card whose run had a card expired by cancellation
(same execution_id or same chat submission - the per-run coordinates; never the
activity alone, which a later re-run of the same task shares) is skipped, and is
stamped so the next poll does not reconsider it. Other runs - including a fresh
re-run of the same activity - still resume.

Real FileVault under tmp_path; a fake supervisor; no daemon, no LLM.
"""
from __future__ import annotations

import threading

from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

CANCELLED = {"execution_id": "exec_rc1", "activity_id": "act_rc1",
             "shadow_id": "sh_rc1", "chat_submission_id": "2026-10-19T13:00:00.000001"}
OTHER = {"execution_id": "exec_rc2", "activity_id": "act_rc2",
         "shadow_id": "sh_rc2", "chat_submission_id": "2026-10-19T13:05:00.000002"}
RERUN = {"execution_id": "exec_rc3", "activity_id": "act_rc1",   # same task, new run
         "shadow_id": "sh_rc1", "chat_submission_id": "2026-10-19T14:00:00.000003"}


class _FakeSupervisor:
    def __init__(self):
        self.calls = []
        self._running = {}
        self._running_lock = threading.Lock()
        self._pending_activity_ids = set()
        self._pending_lock = threading.Lock()

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append((activity_id, kw.get("resume_from_execution_id")))
        return "sub"


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _question(q, coords, n):
    ctx = {"kind": "structured_question", "objective_id": n,
           "questions": [{"id": "answer", "prompt": "hint?", "options": [],
                          "allow_free_text": True}]}
    ctx.update(coords)
    return q.post(title=f"Stuck on objective {n}", body="b", options=["Submit"],
                  context=ctx, dedup_key=f"stuck:{coords['execution_id']}:{n}")


def _gate(q, coords):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_name": "thing"}
    ctx.update(coords)
    return q.post(title="Run tool: thing", body="b", options=["Deny", "Approve once"],
                  context=ctx, dedup_key=f"tool:{coords['execution_id']}")


def _answered_then_cancelled(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    answered = _question(q, CANCELLED, 1)
    still_pending = _gate(q, CANCELLED)
    # answered from the CLI: resolved, nothing in this process dispatched it
    q.resolve(answered, choice='{"answer": "try the other folder"}')
    expired = q.expire_cards_for_run(
        execution_id=CANCELLED["execution_id"], activity_id=CANCELLED["activity_id"],
        chat_submission_id=CANCELLED["chat_submission_id"])
    assert expired == [still_pending]
    assert vault.get_decision(answered).status == "resolved"
    return vault, q, answered


def test_a_cancelled_runs_answered_card_is_not_resumed(tmp_path):
    vault, q, answered = _answered_then_cancelled(tmp_path)
    sup = _FakeSupervisor()

    n = reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data")

    assert sup.calls == [], "MUTATION PIN: the reconciler resurrected a cancelled run"
    assert n == 0


def test_the_skip_is_stamped_and_sticks(tmp_path):
    vault, q, answered = _answered_then_cancelled(tmp_path)
    sup = _FakeSupervisor()
    reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data")
    ctx = vault.get_decision(answered).context
    assert ctx.get("resume_refused") == "run cancelled"
    assert not ctx.get("resume_dispatched"), "a skip must not claim a dispatch"
    reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data")
    assert sup.calls == []


def test_other_runs_still_resume_in_the_same_poll(tmp_path):
    vault, q, _ = _answered_then_cancelled(tmp_path)
    other = _question(q, OTHER, 2)
    q.resolve(other, choice='{"answer": "ok"}')
    rerun = _question(q, RERUN, 3)
    q.resolve(rerun, choice='{"answer": "ok"}')
    sup = _FakeSupervisor()

    n = reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data")

    assert n == 2
    assert sorted(sup.calls) == sorted([
        (OTHER["activity_id"], OTHER["execution_id"]),
        (RERUN["activity_id"], RERUN["execution_id"]),
    ]), "the skip must key on the run, not on the activity a re-run shares"


def test_a_card_expired_by_cancel_is_never_resumed_even_if_later_resolved(tmp_path):
    """The card itself is the witness: if any writer that bypasses the queue's
    late-answer refusal (or the documented get->save race) flips a card expired
    by cancellation back to resolved, its own persisted expired_reason still
    keeps the reconciler from resuming the run."""
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    did = _question(q, CANCELLED, 4)
    assert q.expire_cards_for_run(activity_id=CANCELLED["activity_id"]) == [did]
    dec = vault.get_decision(did)
    dec.status, dec.choice = "resolved", '{"answer": "late"}'
    vault.save_decision(dec)   # the bypass: no refusal on this path
    sup = _FakeSupervisor()

    assert reconcile_resolved_stuck_decisions(vault, sup, data_dir=tmp_path / "data") == 0
    assert sup.calls == [], "MUTATION PIN: a late answer resumed a cancelled run"
