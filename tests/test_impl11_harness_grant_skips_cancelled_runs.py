"""IMPL-11 ruling 4 - the harness-grant reconciler never resumes a cancelled run.

Witnessed gap: ``jobs.reconcile_resolved_harness_grants`` resumes a parked run
(``Supervisor.resume_after_grant``) from every resolved harness card - and on
Approve it first MATERIALISES the capability through the Governor. A harness card
answered before its run was cancelled was dispatched on the next tick: the stopped
run was resumed (and, on Approve, a capability granted for a run nobody wanted).

Ruling under test: the reconciler consults the same durable cancelled-run marker
the resume reconciler does, BEFORE any Governor.grant call, and stamps the skip so later
ticks drop the card. Other runs are dispatched as before.

Real FileVault under tmp_path; a fake supervisor and a fake Governor.
"""
from __future__ import annotations

import pytest

from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.scheduler import jobs

RUN = {"execution_id": "exec_hg1", "activity_id": "act_hg1", "shadow_id": "sh_hg1",
       "chat_submission_id": "2026-10-19T19:00:00.000001"}
OTHER = {"execution_id": "exec_hg2", "activity_id": "act_hg2", "shadow_id": "sh_hg2",
         "chat_submission_id": "2026-10-19T19:30:00.000002"}


class _FakeSupervisor:
    def __init__(self):
        self.resumed = []

    def resume_after_grant(self, **kw):
        self.resumed.append(kw.get("execution_id"))


class _FakeGovernor:
    calls = []

    def __init__(self, *a, **k):
        pass

    def grant(self, *a, **k):
        """The Approve path's side effect: Governor.grant materialises ONCE."""
        type(self).calls.append(a)
        return {"result": {"access": {"granted": True}}}


@pytest.fixture(autouse=True)
def _no_real_governor(monkeypatch):
    _FakeGovernor.calls = []
    monkeypatch.setattr(jobs, "Governor", _FakeGovernor, raising=False)


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _harness_card(q, coords, choice):
    ctx = {"kind": "gate", "gate_type": "harness", "harness_kind": "access",
           "request_id": "req_" + coords["execution_id"], "spec": {},
           "verdict": "escalate", "rationale": "needs a key"}
    ctx.update(coords)
    did = q.post(title="Grant capability", body="b", options=["Deny", "Approve"],
                 context=ctx, dedup_key="harness:" + coords["execution_id"])
    q.resolve(did, choice=choice)
    return did


def test_a_cancelled_runs_answered_harness_card_is_not_dispatched(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    did = _harness_card(q, RUN, "Deny")
    q.expire_cards_for_run(execution_id=RUN["execution_id"],
                           activity_id=RUN["activity_id"],
                           chat_submission_id=RUN["chat_submission_id"])
    sup = _FakeSupervisor()

    n = jobs.reconcile_resolved_harness_grants(vault=vault, supervisor=sup,
                                               data_dir=tmp_path / "data")

    assert sup.resumed == [], "MUTATION PIN: a cancelled run was resumed after a grant"
    assert n == 0
    ctx = vault.get_decision(did).context
    assert ctx.get("resume_refused") == "run cancelled"
    assert not ctx.get("harness_grant_dispatched"), "a skip must not claim a dispatch"


def test_no_capability_is_granted_for_a_cancelled_run(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    _harness_card(q, RUN, "Approve")
    _harness_card(q, OTHER, "Approve")      # proves the fake Governor IS reached
    q.expire_cards_for_run(execution_id=RUN["execution_id"])
    sup = _FakeSupervisor()
    jobs.reconcile_resolved_harness_grants(vault=vault, supervisor=sup,
                                           data_dir=tmp_path / "data")
    assert len(_FakeGovernor.calls) == 1, (
        "a capability was granted for a run the operator stopped")
    assert sup.resumed == [OTHER["execution_id"]]


def test_another_runs_harness_card_is_still_dispatched(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    _harness_card(q, RUN, "Deny")
    _harness_card(q, OTHER, "Deny")
    q.expire_cards_for_run(execution_id=RUN["execution_id"])
    sup = _FakeSupervisor()

    n = jobs.reconcile_resolved_harness_grants(vault=vault, supervisor=sup,
                                               data_dir=tmp_path / "data")

    assert sup.resumed == [OTHER["execution_id"]]
    assert n == 1
