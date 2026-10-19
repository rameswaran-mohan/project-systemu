"""IMPL-11 (queue item 2f) - an answer that arrives after cancellation.

Witnessed gap: ``OperatorDecisionQueue.resolve`` never looked at the card's
status. A card expired because its run was cancelled could still be answered
from the dashboard or the CLI: the answer flipped it to ``resolved``, published
``operator_decision_resolved``, counted toward the approval metrics - and the
resume rail re-dispatched the run the operator had stopped.

Ruling under test: a late answer to a card expired by cancellation records
nothing (status stays ``expired``, no choice, no resolved event, no metric),
resumes nothing (neither the EventBus fast path nor the reconciler), and is
answered with the plain notice "That run was cancelled; this answer was not
needed." - raised as ``AnswerNotNeeded`` (a ``ValueError``, so every existing
caller that reports a refused answer shows it) and written as an event the
operator can read.

Real FileVault under tmp_path; a fake supervisor (no daemon, no LLM).
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue

NOTICE = "That run was cancelled; this answer was not needed."
RUN = {"execution_id": "exec_late1", "activity_id": "act_late1",
       "shadow_id": "sh_late1", "chat_submission_id": "2026-10-19T12:00:00.000003"}


def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _post_question(q):
    """A parked stuck-question card with full resume coords (chat lane) - the
    shape both resume paths act on."""
    ctx = {"kind": "structured_question", "objective_id": 1,
           "questions": [{"id": "answer", "prompt": "hint?", "options": [],
                          "allow_free_text": True}]}
    ctx.update(RUN)
    return q.post(title="Stuck on objective 1", body="b", options=["Submit"],
                  context=ctx, dedup_key="stuck:late1")


def _post_gate(q):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_name": "thing"}
    ctx.update(RUN)
    return q.post(title="Run tool: thing", body="b",
                  options=["Deny", "Approve once", "Always allow"],
                  context=ctx, dedup_key="tool:late-gate")


class _FakeSupervisor:
    def __init__(self):
        self.calls = []
        self._running = {}
        self._running_lock = threading.Lock()
        self._pending_activity_ids = set()
        self._pending_lock = threading.Lock()

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append((activity_id, shadow_id, kw))
        return "sub_x"


@pytest.fixture
def events():
    from systemu.interface.event_bus import EventBus
    seen = []
    unsub = EventBus.get().subscribe(seen.append, replay=False)
    try:
        yield seen
    finally:
        unsub()


@pytest.fixture
def metric_calls(monkeypatch):
    """Records every approval-metric write the queue makes (the S1b sink)."""
    calls = []

    class _Store:
        def incr(self, name, *a, **k):
            calls.append(("incr", name))

        def record_resolution(self, *a, **k):
            calls.append(("record_resolution",))

    monkeypatch.setattr(dq, "_metrics", lambda vault: _Store())
    return calls


def _cancelled_card(tmp_path, post=_post_question):
    vault = _vault(tmp_path)
    q = OperatorDecisionQueue(vault)
    did = post(q)
    assert q.expire_cards_for_run(activity_id=RUN["activity_id"]) == [did]
    return vault, q, did


# ────────────────────────────────────────────── the queue refuses it ─────────

class TestALateAnswerRecordsNothing:
    @pytest.mark.parametrize("answer", [
        pytest.param(lambda q, did: q.resolve(did, choice='{"answer": "go"}'),
                     id="resolve"),
        pytest.param(lambda q, did: q.resolve_with_context_patch(
            did, choice='{"answer": "go"}', context_patch={"x": 1}),
                     id="resolve_with_context_patch"),
    ])
    def test_late_answer_is_refused_with_the_notice(self, tmp_path, events,
                                                    metric_calls, answer):
        vault, q, did = _cancelled_card(tmp_path)
        before = vault.get_decision(did)
        metric_calls.clear()

        with pytest.raises(dq.AnswerNotNeeded) as ei:
            answer(q, did)

        assert str(ei.value) == NOTICE
        assert isinstance(ei.value, ValueError), (
            "callers that already report a refused answer (CLI, dashboard) must "
            "show the notice, not crash")
        after = vault.get_decision(did)
        assert after.status == "expired", "MUTATION PIN: a late answer was recorded"
        assert after.choice is None
        assert after.resolved_at == before.resolved_at
        assert "x" not in after.context
        assert not [e for e in events
                    if e.get("category") == "operator_decision_resolved"]
        assert ("record_resolution",) not in metric_calls

    def test_the_late_answer_is_an_event_the_operator_can_read(self, tmp_path,
                                                               events):
        vault, q, did = _cancelled_card(tmp_path)
        with pytest.raises(dq.AnswerNotNeeded):
            q.resolve(did, choice='{"answer": "go"}')
        notes = [e for e in events if e.get("message") == NOTICE]
        assert notes, "silence: the operator's answer vanished without a trace"
        assert notes[-1]["context"]["decision_id"] == did
        NOTICE.encode("ascii")

    def test_the_notice_is_the_ruled_text(self):
        assert dq.LATE_ANSWER_NOTICE == NOTICE


# ──────────────────────────────────────────────── nothing resumes ───────────

class TestALateAnswerResumesNothing:
    def test_neither_resume_path_dispatches(self, tmp_path):
        from systemu.runtime import resume_on_decision as rod
        from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

        vault, q, did = _cancelled_card(tmp_path)
        sup = _FakeSupervisor()
        unsub = rod.register(vault, sup, data_dir=tmp_path / "data")
        try:
            # The refusal is asserted elsewhere; this test pins what the RAIL does
            # with the answer, so it must not stop at a missing raise.
            try:
                q.resolve(did, choice='{"answer": "go"}')
            except dq.AnswerNotNeeded:
                pass
        finally:
            unsub()
        assert sup.calls == [], "MUTATION PIN: the EventBus rail resumed a cancelled run"
        assert reconcile_resolved_stuck_decisions(
            vault, sup, data_dir=tmp_path / "data") == 0
        assert sup.calls == [], "MUTATION PIN: the reconciler resumed a cancelled run"


# ─────────────────────────────────────────────── every answer surface ───────

class TestEverySurfaceShowsTheNotice:
    def test_cli_decisions_resolve(self, tmp_path):
        from click.testing import CliRunner
        from systemu.interface.cli_commands import decisions_group

        vault, q, did = _cancelled_card(tmp_path, post=_post_gate)
        with patch("systemu.interface.cli_commands._get_vault_and_config",
                   return_value=(MagicMock(), vault)):
            result = CliRunner().invoke(
                decisions_group, ["resolve", did, "--choice", "Approve once"])
        assert NOTICE in result.output
        assert result.exit_code == 0  # a late answer is not an error: nothing failed
        assert vault.get_decision(did).status == "expired"

    def test_dashboard_inbox_card(self, tmp_path):
        from systemu.interface.pages.inbox_page import _resolve_and_execute_gate

        vault, q, did = _cancelled_card(tmp_path, post=_post_gate)
        with pytest.raises(dq.AnswerNotNeeded) as ei:
            _resolve_and_execute_gate(did, "Approve once", vault)
        assert str(ei.value) == NOTICE
        assert vault.get_decision(did).status == "expired"

    def test_dashboard_needs_you_rail(self, tmp_path):
        from systemu.interface.components.inbox_rail import _reject_descriptor

        vault, q, did = _cancelled_card(tmp_path, post=_post_gate)
        descriptor = MagicMock()
        descriptor.options = ["Deny", "Approve once"]
        with pytest.raises(dq.AnswerNotNeeded):
            _reject_descriptor(did, descriptor, vault=vault)
        assert vault.get_decision(did).status == "expired"
