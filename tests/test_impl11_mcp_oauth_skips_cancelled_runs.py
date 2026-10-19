"""IMPL-11 - the MCP OAuth reconciler never resumes a cancelled run.

Witnessed gap: ``jobs.reconcile_resolved_mcp_oauth`` resumes the parked run
(``Supervisor.resume_after_grant``) from every resolved OAuth follow-up card. An
OAuth card answered before its run was cancelled was dispatched on the next tick -
the stopped run resumed.

Ruling under test: it consults the durable cancelled-run marker exactly like the
harness-grant reconciler, stamps ``resume_refused`` and never dispatches (never
stamps ``mcp_oauth_dispatched``). Other runs dispatch as before.

Real FileVault under tmp_path; a fake supervisor; no network.
"""
from __future__ import annotations

from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.scheduler import jobs

RUN = {"execution_id": "exec_oa1", "activity_id": "act_oa1", "shadow_id": "sh_oa1",
       "chat_submission_id": "2026-10-19T20:00:00.000001"}
OTHER = {"execution_id": "exec_oa2", "activity_id": "act_oa2", "shadow_id": "sh_oa2",
         "chat_submission_id": "2026-10-19T20:30:00.000002"}


class _FakeSupervisor:
    def __init__(self):
        self.resumed = []

    def resume_after_grant(self, **kw):
        self.resumed.append(kw.get("execution_id"))


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _oauth_card(q, coords, choice="Approve"):
    ctx = {"kind": "gate", "gate_type": "mcp_oauth", "follow_up": "mcp_oauth",
           "harness_kind": "mcp", "server_id": "srv_x"}
    ctx.update(coords)
    did = q.post(title="Authorize MCP server", body="b", options=["Deny", "Approve"],
                 context=ctx, dedup_key="oauth:" + coords["execution_id"])
    q.resolve(did, choice=choice)
    return did


def test_a_cancelled_runs_answered_oauth_card_is_not_dispatched(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    did = _oauth_card(q, RUN)
    q.expire_cards_for_run(execution_id=RUN["execution_id"],
                           chat_submission_id=RUN["chat_submission_id"])
    sup = _FakeSupervisor()

    n = jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup,
                                          data_dir=tmp_path / "data")

    assert sup.resumed == [], "MUTATION PIN: a cancelled run was resumed after OAuth"
    assert n == 0
    ctx = vault.get_decision(did).context
    assert ctx.get("resume_refused") == "run cancelled"
    assert not ctx.get("mcp_oauth_dispatched"), "a skip must not claim a dispatch"


def test_the_refusal_sticks_across_ticks(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    _oauth_card(q, RUN)
    q.expire_cards_for_run(execution_id=RUN["execution_id"])
    sup = _FakeSupervisor()
    jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup, data_dir=tmp_path)
    jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup, data_dir=tmp_path)
    assert sup.resumed == []


def test_another_runs_oauth_card_is_still_dispatched(tmp_path):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    _oauth_card(q, RUN)
    _oauth_card(q, OTHER)
    q.expire_cards_for_run(execution_id=RUN["execution_id"])
    sup = _FakeSupervisor()

    n = jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup,
                                          data_dir=tmp_path / "data")

    assert sup.resumed == [OTHER["execution_id"]]
    assert n == 1
