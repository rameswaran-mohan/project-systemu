"""DEC-44 - pins that the conftest substrate says what it does.

Fences, each of which was inert or dishonest before DEC-44:

1. an UNRESOLVED quick-lane gate under the conftest clamp RAISES (it used to
   hand the caller ``None``, which the product reads as a Deny verdict -- a
   product decision manufactured by a test fixture and asserted by nobody);
2. a RESOLVED gate is returned untouched, so the clamp still costs a test that
   does its job nothing;
3. ``@pytest.mark.slow_gate_polls`` really does hand back the unwrapped product
   function, so a test whose subject IS the timeout can still say so;
4. the situational-inventory survey stub is OPT-IN: an unmarked test gets the
   REAL ``survey_situation``, and ``@pytest.mark.stub_survey`` is what swaps in
   the instant-empty double.

Reachability: delete the raise in ``conftest._bound_gate_block_polls`` and (1)
goes red; re-add ``autouse=True`` to ``_stub_situation_survey`` and (4a) goes
red; drop the ``_wire_stub_survey_marker`` call and (4b) goes red.
"""
from __future__ import annotations

import pytest

from systemu.pipelines import quick_task


class _NeverResolves:
    def __init__(self, vault):
        pass

    def get_resolved_choice(self, dedup_key):
        return None


def test_unresolved_gate_under_the_clamp_raises_instead_of_denying(monkeypatch):
    """The DEC-44 fence: silence becomes a loud failure, not a Deny."""
    import systemu.approval.decision_queue as dq

    monkeypatch.setattr(dq, "OperatorDecisionQueue", _NeverResolves)
    with pytest.raises(AssertionError, match="unresolved after clamped"):
        quick_task._poll_command_choice(object(), "command:dec44", timeout=0.05)


def test_resolved_gate_still_returns_the_operator_choice(monkeypatch):
    """The clamp must stay free for a test that actually resolves its gate."""
    import systemu.approval.decision_queue as dq

    class _Resolved(_NeverResolves):
        def get_resolved_choice(self, dedup_key):
            return "Approve once"

    monkeypatch.setattr(dq, "OperatorDecisionQueue", _Resolved)
    assert quick_task._poll_command_choice(object(), "command:dec44") == "Approve once"


@pytest.mark.slow_gate_polls
def test_the_marker_hands_back_the_real_unwrapped_poll(monkeypatch):
    """``slow_gate_polls`` must yield the PRODUCT function, not a wrapper."""
    import systemu.approval.decision_queue as dq

    monkeypatch.setattr(dq, "OperatorDecisionQueue", _NeverResolves)
    assert quick_task._poll_command_choice(object(), "k", timeout=0.05) is None


def test_situation_survey_is_real_unless_a_test_opts_in():
    """DEC-44: the survey stub is opt-in. Unmarked -- like ~every test in this
    suite -- this gets the REAL ``survey_situation``, not conftest's double."""
    import systemu.runtime.situational_inventory as si

    assert si.survey_situation.__module__ == si.__name__
    assert si.survey_situation.__qualname__ == "survey_situation"


@pytest.mark.stub_survey  # pins the opt-in itself: this test IS the marker's test
def test_the_stub_survey_marker_swaps_in_the_instant_empty_double():
    """...and WITH the marker the double is installed, so the opt-in works and a
    test that legitimately needs unit isolation can still get it."""
    import systemu.runtime.situational_inventory as si

    assert si.survey_situation.__qualname__ != "survey_situation"
    assert si.survey_situation.__module__ != si.__name__
