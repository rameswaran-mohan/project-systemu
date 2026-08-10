"""F34 — the CLI said "not approved" for a choice that was, in fact, honoured.

Found by driving the product's own differentiator end to end on the release
wheel: a task needing a capability that did not exist, forged, dependency-gated,
then an action gate offering ['Deny', 'Approve once', 'Always allow'].

Resolving it with "Always allow" printed:

    Gate tool not approved (Always allow).

and yet the standing allow WAS recorded — re-running the task afterwards ran the
tool and produced the artifact. The sentence is simply false, and it is the only
feedback the operator gets. I believed it, concluded the approval had failed,
and spent twenty minutes chasing a defect that did not exist.

WHY IT HAPPENS. `resolve_gate` early-exits when the choice is not in
`_APPROVE_LABELS` = {approve, approve & apply, approve & install, forge,
enable & run}. The action gate's own labels are Deny / Approve once / Always
allow, so NONE of them match and every resolution takes the "not approved"
branch — including the affirmative ones.

WHY THAT EXIT IS OTHERWISE CORRECT. These gates are caller-reads-choice, exactly
like the `operator` gate already in `_RENDER_ONLY_GATES`: the parked run re-reads
the answer via `OperatorDecisionQueue.get_resolved_choice`, and
`resume_on_decision` dispatches a resume for `gate_type` in {command, tool}. The
inbox must NOT re-execute anything. So the bug is not the no-op — it is the
CLAIM attached to it. DEC-34: a false assertion about what just happened is a
defect in its own right, and this one actively misleads the person who has just
granted a standing permission.

The fence is over the CLASS: every gate whose resolution is consumed by the
parked caller must acknowledge the choice without asserting it was refused.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

CALLER_READS_CHOICE = ["operator", "command", "tool"]
AFFIRMATIVE = ["Approve once", "Always allow"]
NEGATIVE = ["Deny"]


def _decision(gate_type: str, choice: str):
    return SimpleNamespace(
        id="dec_test",
        choice=choice,
        context={"gate_type": gate_type, "kind": "gate"},
        dedup_key=f"{gate_type}:abc123",
    )


@pytest.mark.parametrize("gate_type", CALLER_READS_CHOICE)
@pytest.mark.parametrize("choice", AFFIRMATIVE)
def test_an_affirmative_choice_is_never_reported_as_not_approved(gate_type, choice):
    from systemu.interface.command.inbox import resolve_gate

    res = resolve_gate(_decision(gate_type, choice), vault=None)
    assert "not approved" not in (res.summary or "").lower(), (
        f"gate_type={gate_type!r} choice={choice!r} produced {res.summary!r}. The "
        f"operator granted permission and was told it was refused; the permission "
        f"is in fact honoured when the parked run retries."
    )


@pytest.mark.parametrize("gate_type", CALLER_READS_CHOICE)
@pytest.mark.parametrize("choice", AFFIRMATIVE + NEGATIVE)
def test_the_choice_is_echoed_so_the_operator_can_see_what_was_recorded(gate_type, choice):
    from systemu.interface.command.inbox import resolve_gate

    res = resolve_gate(_decision(gate_type, choice), vault=None)
    assert choice.lower() in (res.summary or "").lower(), (
        f"the acknowledgement must name the recorded choice; got {res.summary!r}"
    )


@pytest.mark.parametrize("gate_type", CALLER_READS_CHOICE)
def test_a_denial_is_not_dressed_up_as_an_approval(gate_type):
    """The opposite error would be worse: never let Deny read as granted."""
    from systemu.interface.command.inbox import resolve_gate

    res = resolve_gate(_decision(gate_type, "Deny"), vault=None)
    text = (res.summary or "").lower()
    assert "approved" not in text or "not approved" in text, (
        f"a Deny must not read as an approval; got {res.summary!r}"
    )


def test_a_genuinely_unknown_gate_still_reports_honestly():
    """This widens the accurate-acknowledgement set; it must not blanket-silence
    the not-approved branch for gates that really do require an approve label."""
    from systemu.interface.command.inbox import resolve_gate

    res = resolve_gate(_decision("scroll", "Reject"), vault=None)
    assert "not approved" in (res.summary or "").lower(), (
        "a non-approve choice on an EXECUTING gate must still say so; got "
        f"{res.summary!r}"
    )
