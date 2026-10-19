"""IMPL-11 - the EventBus fast resume path refuses a cancelled run.

Witnessed gap: ``resume_on_decision._dispatch_resume`` acted on the decision AS
THE EVENT CARRIED IT. It never re-read the card's status and never consulted the
cancelled-run marker, so an answer and a cancel landing together resumed the run
the operator had stopped:
  * the cancel recorded the run in the marker while the answered card stayed
    ``resolved`` (it was no longer pending, so nothing expired it);
  * or the cancel's expiry landed on disk after the answer, so the card on disk
    is ``expired`` while the event still carries the ``resolved`` copy.

Ruling under test: immediately before any side effect (approval bridge, snapshot
stash, deny finalize, re-dispatch) the rail re-reads the card and the marker and
refuses - an operator-readable event, no resume - when the card is expired or its
run is marked cancelled. ONE re-read: the money gate's "already spent" check uses
the same fresh status (integrated, not duplicated).

Real FileVault under tmp_path; a fake supervisor; the approval-store writer is
replaced by a recorder (it would otherwise write under ./data).
"""
from __future__ import annotations

import pytest

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.runtime import resume_on_decision as rod

NOTICE = "That run was cancelled; this answer was not needed."
TS = "2026-10-19T21:00:00.000001"
RUN = {"execution_id": "exec_fp1", "activity_id": "act_fp1", "shadow_id": "sh_fp1",
       "chat_submission_id": TS}


class _Sup:
    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append((activity_id, kw.get("resume_from_execution_id")))
        return "sub"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    rod._handled.clear()
    minted = []
    monkeypatch.setattr(rod, "_record_gate_approval",
                        lambda dctx, **kw: minted.append(kw))
    yield minted
    rod._handled.clear()


@pytest.fixture
def events():
    from systemu.interface.event_bus import EventBus
    seen = []
    unsub = EventBus.get().subscribe(seen.append, replay=False)
    try:
        yield seen
    finally:
        unsub()


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _question(q):
    ctx = {"kind": "structured_question", "objective_id": 1,
           "questions": [{"id": "answer", "prompt": "hint?", "options": [],
                          "allow_free_text": True}], **RUN}
    did = q.post(title="Stuck on objective 1", body="b", options=["Submit"],
                 context=ctx, dedup_key="stuck:fp1")
    return q.resolve(did, choice='{"answer": "go"}')


def _gate(q, choice):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_name": "thing", **RUN}
    did = q.post(title="Run tool: thing", body="b",
                 options=["Deny", "Approve once", "Always allow"],
                 context=ctx, dedup_key="tool:fp1")
    return q.resolve(did, choice=choice)


def _seed_activity(vault, status):
    from systemu.core.models import Activity
    vault.save_activity(Activity(id=RUN["activity_id"], name="a", scroll_id="s",
                                 status=status))


def test_a_run_marked_cancelled_after_the_answer_is_not_resumed(tmp_path, events,
                                                                 _isolate):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    as_the_event_carried_it = _question(q)
    q.expire_cards_for_run(chat_submission_id=TS)      # the cancel lands now
    sup = _Sup()

    assert rod._dispatch_resume(as_the_event_carried_it, vault=vault,
                                supervisor=sup, data_dir=tmp_path / "data") is False

    assert sup.submits == [], "MUTATION PIN: the fast path resumed a cancelled run"
    assert any(e.get("message") == NOTICE for e in events), "refused in silence"
    assert vault.get_decision(as_the_event_carried_it.id).context.get(
        "resume_refused") == "run cancelled"


def test_a_card_expired_on_disk_after_the_event_is_not_resumed(tmp_path, events):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    as_the_event_carried_it = _question(q)
    on_disk = vault.get_decision(as_the_event_carried_it.id)
    on_disk.status = "expired"           # the cancel's expiry saved after the answer
    on_disk.context = {**on_disk.context, "expired_reason": "run cancelled"}
    vault.save_decision(on_disk)
    sup = _Sup()

    assert rod._dispatch_resume(as_the_event_carried_it, vault=vault,
                                supervisor=sup, data_dir=tmp_path / "data") is False

    assert sup.submits == []
    assert any(e.get("message") == NOTICE for e in events)
    assert vault.get_decision(on_disk.id).status == "expired", (
        "the refusal must never write the stale resolved copy back over the expiry")


def test_a_card_withdrawn_for_another_reason_is_not_resumed_either(tmp_path, events):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    as_the_event_carried_it = _question(q)
    on_disk = vault.get_decision(as_the_event_carried_it.id)
    on_disk.status = "expired"
    vault.save_decision(on_disk)
    sup = _Sup()

    assert rod._dispatch_resume(as_the_event_carried_it, vault=vault,
                                supervisor=sup, data_dir=tmp_path / "data") is False
    assert sup.submits == []
    assert any("withdrawn" in str(e.get("message", "")) for e in events)


def test_no_approval_is_minted_for_a_cancelled_run(tmp_path, _isolate):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    as_the_event_carried_it = _gate(q, "Approve once")
    q.expire_cards_for_run(execution_id=RUN["execution_id"])
    sup = _Sup()

    rod._dispatch_resume(as_the_event_carried_it, vault=vault, supervisor=sup,
                         data_dir=tmp_path / "data")

    assert _isolate == [], "a single-use approval was minted for a stopped run"
    assert sup.submits == []


def test_a_late_deny_never_rewrites_a_cancelled_run_as_failed(tmp_path):
    from systemu.core.models import ActivityStatus
    vault = _vault(tmp_path)
    _seed_activity(vault, ActivityStatus.CANCELLED)
    q = OperatorDecisionQueue(vault)
    as_the_event_carried_it = _gate(q, "Deny")
    q.expire_cards_for_run(execution_id=RUN["execution_id"])

    rod._dispatch_resume(as_the_event_carried_it, vault=vault, supervisor=_Sup(),
                         data_dir=tmp_path / "data")

    assert vault.get_activity(RUN["activity_id"]).status is ActivityStatus.CANCELLED


def test_a_run_nobody_cancelled_still_resumes(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    as_the_event_carried_it = _question(q)
    sup = _Sup()
    assert rod._dispatch_resume(as_the_event_carried_it, vault=vault,
                                supervisor=sup, data_dir=tmp_path / "data") is True
    assert sup.submits == [(RUN["activity_id"], RUN["execution_id"])]


def test_the_event_bus_path_end_to_end(tmp_path):
    """resolve -> operator_decision_resolved -> the registered subscriber: a run
    marked cancelled between the answer and the dispatch is not resumed."""
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    ctx = {"kind": "structured_question", "objective_id": 1,
           "questions": [{"id": "answer", "prompt": "hint?", "options": [],
                          "allow_free_text": True}], **RUN}
    did = q.post(title="Stuck", body="b", options=["Submit"], context=ctx,
                 dedup_key="stuck:fp-e2e")
    dq.record_cancelled_run(vault, {"chat_submission_id": [TS]})
    sup = _Sup()
    unsub = rod.register(vault, sup, data_dir=tmp_path / "data")
    try:
        q.resolve(did, choice='{"answer": "go"}')
    finally:
        unsub()
    assert sup.submits == []
