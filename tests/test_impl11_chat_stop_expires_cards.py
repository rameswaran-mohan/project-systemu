"""IMPL-11 (queue item 2f) - the chat Stop withdraws the run's cards through the
ONE queue function.

Witnessed gap: ``cancel_chat_task`` withdrew only the single card the chat row
names, through ``expire_by_dedup_key`` - so (a) the card was expired WITHOUT the
``run cancelled`` reason, and a late answer to it was recorded and resumed the
stopped run; (b) any other pending card of the run stayed answerable; (c) a dedup
key is params-independent, so the first pending card with that key - possibly
another run's - was the one expired.

Real FileVault under tmp_path for the decisions; the chat-history row is a
stand-in (the vault's chat history is not under test).
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue

TS = "2026-10-19T15:00:00.000001"
RUN = {"execution_id": "exec_cs1", "activity_id": "act_cs1", "shadow_id": "sh_cs1",
       "chat_submission_id": TS}
OTHER = {"execution_id": "exec_cs2", "activity_id": "act_cs2", "shadow_id": "sh_cs2",
         "chat_submission_id": "2026-10-19T16:00:00.000002"}


class _ChatVault:
    """Real decisions (FileVault) + an in-memory chat history."""

    def __init__(self, inner, entries):
        self._inner = inner
        self.entries = list(entries)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def load_chat_history(self, limit=50):
        return list(self.entries[-limit:])

    def update_chat_history_entry(self, ts, updates):
        for e in self.entries:
            if e.get("ts") == ts:
                e.update(updates)


def _vault(tmp_path, entries):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return _ChatVault(FileVault(Vault(str(tmp_path / "v"))), entries)


def _gate(q, coords, dedup):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_name": "thing"}
    ctx.update(coords)
    return q.post(title="Run tool: thing", body="b", options=["Deny", "Approve once"],
                  context=ctx, dedup_key=dedup)


def _parked_row(decision_id, dedup_key):
    return {"ts": TS, "prompt": "tidy the reports folder", "status": "pending_decision",
            "activity_id": RUN["activity_id"], "decision_id": decision_id,
            "dedup_key": dedup_key}


def test_chat_stop_expires_the_runs_cards_with_the_cancel_reason(tmp_path):
    from systemu.runtime.chat_task_registry import cancel_chat_task

    vault = _vault(tmp_path, [])
    q = OperatorDecisionQueue(vault)
    theirs = _gate(q, OTHER, "tool:shared-sig")          # posted FIRST
    parked = _gate(q, RUN, "tool:mine")
    side = _gate(q, {"chat_submission_id": TS}, "tool:side")
    vault.entries.append(_parked_row(parked, "tool:mine"))

    report = cancel_chat_task(vault, TS, queue=q)

    assert report["stopped"] is True
    assert report["gate_withdrawn"] is True and report["gate_note"] == ""
    for did in (parked, side):
        dec = vault.get_decision(did)
        assert dec.status == "expired", "MUTATION PIN: chat Stop left a card answerable"
        assert dec.context["expired_reason"] == "run cancelled"
    assert vault.get_decision(theirs).status == "pending"


def test_a_late_answer_after_chat_stop_gets_the_notice(tmp_path):
    from systemu.runtime.chat_task_registry import cancel_chat_task

    vault = _vault(tmp_path, [])
    q = OperatorDecisionQueue(vault)
    parked = _gate(q, RUN, "tool:mine")
    vault.entries.append(_parked_row(parked, "tool:mine"))
    cancel_chat_task(vault, TS, queue=q)

    try:
        q.resolve(parked, choice="Approve once")
        raised = None
    except dq.AnswerNotNeeded as exc:
        raised = exc
    assert raised is not None and str(raised) == dq.LATE_ANSWER_NOTICE
    assert vault.get_decision(parked).status == "expired"


def test_chat_stop_never_expires_another_runs_card_by_a_shared_dedup_key(tmp_path):
    """A gate dedup key is params-INDEPENDENT; the row's key can name another
    run's pending card. Withdrawal is by the run's coordinates and the exact id."""
    from systemu.runtime.chat_task_registry import cancel_chat_task

    vault = _vault(tmp_path, [])
    q = OperatorDecisionQueue(vault)
    theirs = _gate(q, OTHER, "tool:same-signature")
    vault.entries.append(_parked_row("dec_gone", "tool:same-signature"))

    cancel_chat_task(vault, TS, queue=q)

    assert vault.get_decision(theirs).status == "pending"


def _calls_in(func) -> set:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_chat_stop_reaches_the_one_function_and_nothing_ad_hoc():
    from systemu.runtime import chat_task_registry as reg
    reachable = _calls_in(reg.cancel_chat_task) | _calls_in(reg._withdraw_gate)
    assert "expire_cards_for_run" in reachable
    assert "expire_by_dedup_key" not in reachable, (
        "the chat Stop still withdraws a card ad hoc, outside the one function")
