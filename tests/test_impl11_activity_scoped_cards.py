"""IMPL-11 ruling 2 - cancelling a run never expires a card about the ACTIVITY.

Witnessed gap: ``expire_cards_for_run`` matched any pending card whose context
carried the run's ``activity_id``. Some cards carry an activity_id because they are
about the activity (or its tools / scroll), not about the run being cancelled -
the readiness gate "Task blocked -- N tool(s) not ready" (``tools_blocked``) is the
named example: approving it enables tools for every future run of the task.
Cancelling one run withdrew it.

Ruling under test: activity_id alone matches only RUN-scoped card types (the cards
a run posts while it executes). Activity-scoped types are excluded from the
activity match - they still expire on an exact decision id or on this run's
execution / chat submission. Every gate type in the tree must be classified as
one or the other, so a new gate type cannot silently fall on the wrong side.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue

ACT = "act_scope1"

# The activity-scoped shapes the ruling excludes from activity_id matching.
ACTIVITY_SCOPED = [
    pytest.param({"kind": "gate", "gate_type": "tools_blocked", "tool_ids": ["t1"]},
                 id="gate:tools_blocked"),
    pytest.param({"kind": "gate", "gate_type": "forge"}, id="gate:forge"),
    pytest.param({"kind": "gate", "gate_type": "dep"}, id="gate:dep"),
    pytest.param({"kind": "gate", "gate_type": "scroll"}, id="gate:scroll"),
    pytest.param({"kind": "gate", "gate_type": "evolution"}, id="gate:evolution"),
    pytest.param({"kind": "gate", "gate_type": "recovery"}, id="gate:recovery"),
    pytest.param({"kind": "gate", "gate_type": "tool_bulk"}, id="gate:tool_bulk"),
    pytest.param({"notification_type": "shadow_assignment"},
                 id="notify:shadow_assignment"),
    pytest.param({"kind": "credential_batch", "keys": ["k"]}, id="credential_batch"),
]

# The run-scoped shapes a run posts while it executes - matched on activity_id.
RUN_SCOPED = [
    pytest.param({"kind": "gate", "gate_type": g}, id=f"gate:{g}")
    for g in ("tool", "command", "harness", "mcp_call", "mcp_oauth", "sampling",
              "operator")
] + [pytest.param({"kind": "structured_question", "questions": []},
                  id="structured_question")]


def _queue(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return OperatorDecisionQueue(FileVault(Vault(str(tmp_path / "v"))))


def _post(q, ctx, n=0):
    return q.post(title=f"card {n}", body="b", options=["Dismiss", "Approve"],
                  context={**ctx, "activity_id": ACT}, dedup_key=f"scope:{n}:{ctx}")


@pytest.mark.parametrize("ctx", ACTIVITY_SCOPED)
def test_an_activity_scoped_card_survives_the_runs_cancel(tmp_path, ctx):
    q = _queue(tmp_path)
    did = _post(q, ctx)
    assert q.expire_cards_for_run(activity_id=ACT) == [], (
        "MUTATION PIN: cancelling one run withdrew a card about the activity")
    assert q._vault.get_decision(did).status == "pending"


@pytest.mark.parametrize("ctx", RUN_SCOPED)
def test_a_run_scoped_card_is_matched_on_the_activity(tmp_path, ctx):
    q = _queue(tmp_path)
    did = _post(q, ctx)
    assert q.expire_cards_for_run(activity_id=ACT) == [did]


def test_an_activity_scoped_card_still_expires_when_named_exactly(tmp_path):
    """The exclusion is about the activity coordinate only: a caller that names the
    card the run is parked on (the chat row) still withdraws it."""
    q = _queue(tmp_path)
    did = _post(q, {"kind": "gate", "gate_type": "tools_blocked"})
    assert q.expire_cards_for_run(activity_id=ACT, decision_id=did) == [did]


def _gate_types_in_tree() -> set:
    root = Path(dq.__file__).resolve().parent.parent
    found = set()
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        found.update(re.findall(r'gate_type="([a-z_]+)"', text))
    return found


def test_every_gate_type_in_the_tree_is_classified():
    """A new gate type must be put on one side on purpose: run-scoped (matched on
    the activity) or activity-scoped (never withdrawn by one run's cancel)."""
    classified = dq.RUN_SCOPED_GATE_TYPES | dq.ACTIVITY_SCOPED_GATE_TYPES
    assert not (dq.RUN_SCOPED_GATE_TYPES & dq.ACTIVITY_SCOPED_GATE_TYPES)
    in_tree = _gate_types_in_tree()
    assert in_tree, "the scan found no gate types - is the path right?"
    assert in_tree <= classified, sorted(in_tree - classified)
