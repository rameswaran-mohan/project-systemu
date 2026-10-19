"""IMPL-11 (queue item 2f) - the phone lane's answer to a cancelled run's card.

Witnessed gap: a Telegram tap (or ``/answer``) on a card whose run was cancelled
was told "That decision is already resolved. Nothing to do." - a false statement
(nobody resolved it; the run was stopped), and nothing the operator could read
anywhere else recorded that the answer arrived. When the cancel landed between
the bridge's lookup and its resolve, the reply was "That option couldn't be
applied." - wrong again.

Ruling under test: the phone gets the same notice every other surface gets,
"That run was cancelled; this answer was not needed.", nothing is recorded, and
the answer is written as an event the operator can read.

Real FileVault under tmp_path; the real queue; no gateway, no network.
"""
from __future__ import annotations

import pytest

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue
from systemu.messaging import decision_bridge as db

NOTICE = "That run was cancelled; this answer was not needed."
RUN = {"execution_id": "exec_ph1", "activity_id": "act_ph1", "shadow_id": "sh_ph1"}


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")
    db._rate_hits.clear()
    yield
    db._rate_hits.clear()


@pytest.fixture
def events():
    from systemu.interface.event_bus import EventBus
    seen = []
    unsub = EventBus.get().subscribe(seen.append, replay=False)
    try:
        yield seen
    finally:
        unsub()


def _queue(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return OperatorDecisionQueue(FileVault(Vault(str(tmp_path / "v"))))


def _post_remote_gate(q):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_name": "thing",
           "resolution_class": db.RESOLUTION_REMOTE}
    ctx.update(RUN)
    return q.post(title="Run tool: thing", body="b", options=["Deny", "Approve"],
                  context=ctx, dedup_key="tool:phone")


@pytest.mark.parametrize("choice,value", [
    pytest.param("a2", None, id="button-tap"),
    pytest.param(None, "Approve", id="typed-answer"),
])
def test_a_tap_on_a_cancelled_runs_card_gets_the_notice(tmp_path, events,
                                                        choice, value):
    q = _queue(tmp_path)
    did = _post_remote_gate(q)
    assert q.expire_cards_for_run(activity_id=RUN["activity_id"]) == [did]

    outcome, msg = db.resolve_from_channel(
        db.decision_tag(did), choice, sender_id="111", channel="telegram",
        queue=q, value=value)

    assert outcome == "EXPIRED"
    assert msg == NOTICE, "the phone was told something other than the ruled notice"
    dec = q._vault.get_decision(did)
    assert dec.status == "expired" and dec.choice is None
    notes = [e for e in events if e.get("message") == NOTICE]
    assert notes, "silence: the phone answer left no event the operator can read"
    assert notes[-1]["context"]["decision_id"] == did
    assert notes[-1]["context"]["channel"] == "telegram"
    msg.encode("ascii")


def test_a_cancel_landing_between_lookup_and_resolve_gets_the_notice(tmp_path,
                                                                    events):
    q = _queue(tmp_path)
    did = _post_remote_gate(q)

    def _cancel_then_resolve(decision_id, mapped_choice):
        q.expire_cards_for_run(activity_id=RUN["activity_id"])
        return q.resolve(decision_id, choice=mapped_choice)

    outcome, msg = db.resolve_from_channel(
        db.decision_tag(did), "a2", sender_id="111", channel="telegram",
        queue=q, resolver=_cancel_then_resolve)

    assert (outcome, msg) == ("EXPIRED", NOTICE)
    assert q._vault.get_decision(did).status == "expired"


def test_an_ordinary_resolved_card_keeps_its_reply(tmp_path):
    q = _queue(tmp_path)
    did = _post_remote_gate(q)
    q.resolve(did, choice="Deny")
    outcome, msg = db.resolve_from_channel(
        db.decision_tag(did), "a2", sender_id="111", channel="telegram", queue=q)
    assert outcome == "EXPIRED"
    assert msg != NOTICE
