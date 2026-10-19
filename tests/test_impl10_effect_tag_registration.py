"""IMPL-10 -- a new effect tag is dangerous until proven, and severity never goes
down.

Requirement (IMPL-10, tag-registration governance): a newly-registered ``EffectTag``
enters at UNKNOWN-band severity until code or an explicit operator action maps
it; registration can NEVER lower an existing tag's severity -- extension grows
the vocabulary, never launders it.

What the code did instead (latent: ``register_effect_tag`` has no production
caller yet, which is exactly when a privilege-downgrade path should be closed):

* ``register_effect_tag(value)`` defaulted ``high_severity=False`` and the gate
  then scored the tag as a KNOWN, benign class -- ``evaluate_action`` returned
  ALLOW for an effect no rule in the gate had ever classified, where an
  unregistered spelling of the same tag was REQUIRE_APPROVAL. Registering a
  tag was a way to make it frictionless.
* a re-registration overwrote the stored severity, so ``high_severity=False``
  on an existing high-severity tag LOWERED it -- the b29 effect-tag downgrade
  class, through the registry instead of a tool declaration.

Ruled behaviour, pinned here:

* every registered extension tag scores in the UNKNOWN band (the two-band rule
  ``action_governance`` already applies to unclassifiable effects): REQUIRE_
  APPROVAL, or DENY with a high-severity signal; registering it high-severity
  makes the tag ITSELF that signal, so it sits on the DENY floor (operator-
  remediable by IMPL-2 reclassification, never rubber-stampable);
* severity is monotone: raising is accepted, lowering is REFUSED AS A VALUE
  (a ``TagRegistration`` whose outcome says so -- DEC-32: a fence is a value,
  not a raise) and logged; the stored severity is untouched.
"""
from __future__ import annotations

import logging

import pytest

from systemu.runtime import action_governance as ag
from systemu.runtime import effect_tags as et
from systemu.runtime.action_governance import ActionContext, Verdict, evaluate_action

# A tool name with no verb-map token (hologram / widget), so only the TAG decides.
_TOOL = "hologram_widget"
_UNKNOWN_BAND_REASON = "unclassifiable effect"
_RANK = {Verdict.ALLOW: 0, Verdict.MASK: 0, Verdict.REQUIRE_APPROVAL: 1,
         Verdict.DENY: 2}


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """The registry is process-global; never leak a test's tags into another."""
    monkeypatch.setattr(et, "_EXTENSIONS", {})


def _verdict(tags, **kw):
    return evaluate_action(ActionContext(tool=_TOOL, effect_tags=set(tags), **kw))


# -- A. a new tag enters the UNKNOWN band ------------------------------------

def test_a_new_tag_registered_with_defaults_is_gated_in_the_UNKNOWN_band():
    et.register_effect_tag("holo_actuate")
    verdict, why = _verdict({"holo_actuate"})
    assert verdict is Verdict.REQUIRE_APPROVAL, (verdict, why)
    assert _UNKNOWN_BAND_REASON in why
    # ...the SAME band an unregistered spelling of an effect lands in.
    assert _verdict({"never_registered_effect"}) == (verdict, why)


def test_a_new_tag_with_a_high_severity_signal_hits_the_DENY_floor():
    et.register_effect_tag("holo_actuate")
    verdict, why = _verdict({"holo_actuate"}, irreversible=True)
    assert verdict is Verdict.DENY, (verdict, why)
    assert _verdict({"never_registered_effect"}, irreversible=True)[0] is Verdict.DENY


def test_a_tag_registered_high_severity_is_its_own_DENY_signal():
    et.register_effect_tag("holo_actuate", high_severity=True)
    verdict, why = _verdict({"holo_actuate"})
    assert verdict is Verdict.DENY, (verdict, why)


def test_operator_reclassification_still_remedies_a_new_tag_but_never_to_ALLOW():
    """IMPL-2 composes with IMPL-10: the operator's typed-confirm class defeats
    the UNKNOWN conjunct, and the action lands on an approval card -- never
    frictionless."""
    et.register_effect_tag("holo_actuate", high_severity=True)
    verdict, _why = _verdict({"holo_actuate"}, operator_assigned_class="local_write")
    assert verdict is Verdict.REQUIRE_APPROVAL


def test_the_scorer_discloses_both_the_tag_and_its_UNKNOWN_band():
    et.register_effect_tag("holo_actuate")
    tags = ag.effective_tags(ActionContext(tool=_TOOL, effect_tags={"holo_actuate"}))
    assert {"holo_actuate", et.EffectTag.UNKNOWN.value} <= tags


def test_a_new_tag_is_still_never_batch_approvable():
    et.register_effect_tag("holo_actuate")
    ok, _why = ag.batch_approvable(ActionContext(tool=_TOOL,
                                                 effect_tags={"holo_actuate"}))
    assert ok is False


# -- B. registration is monotone ---------------------------------------------

def test_lowering_is_refused_as_a_value_and_logged(caplog):
    first = et.register_effect_tag("holo_actuate", high_severity=True)
    assert type(first) is et.TagRegistration
    assert first.outcome == et.REGISTERED and first.high_severity is True

    with caplog.at_level(logging.WARNING, logger=et.__name__):
        second = et.register_effect_tag("holo_actuate", high_severity=False)

    # refused AS A VALUE: no exception crossed the boundary, the result says so
    assert type(second) is et.TagRegistration
    assert second.outcome == et.REFUSED_LOWERING
    assert second.refused is True
    assert second.high_severity is True          # the severity IN FORCE
    assert et.is_high_severity("holo_actuate") is True
    assert _verdict({"holo_actuate"})[0] is Verdict.DENY
    assert any("holo_actuate" in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records), caplog.text


def test_raising_is_accepted_and_only_ever_tightens_the_gate():
    first = et.register_effect_tag("holo_actuate")
    assert first.outcome == et.REGISTERED and first.high_severity is False
    before = _verdict({"holo_actuate"})[0]

    raised = et.register_effect_tag("holo_actuate", high_severity=True)
    assert raised.outcome == et.RAISED and raised.refused is False
    assert et.is_high_severity("holo_actuate") is True
    assert _RANK[_verdict({"holo_actuate"})[0]] >= _RANK[before]


@pytest.mark.parametrize("kw", [
    {},
    {"irreversible": True},
    {"is_destructive_param": True},
    {"target": "api.example.com", "target_is_network": True},
    {"operator_assigned_class": "local_write"},
    {"classification_trusted": False},
])
@pytest.mark.parametrize("tool", [_TOOL, "send_hologram", "delete_widget"])
def test_raising_a_tags_severity_never_lowers_any_verdict(kw, tool):
    """Quantified over the escalators the gate knows: whatever else is on the
    call, raising the tag's severity moves the verdict up or leaves it."""
    et.register_effect_tag("holo_actuate")
    ctx = dict(tool=tool, effect_tags={"holo_actuate"}, **kw)
    before = evaluate_action(ActionContext(**ctx))[0]
    et.register_effect_tag("holo_actuate", high_severity=True)
    after = evaluate_action(ActionContext(**ctx))[0]
    assert _RANK[after] >= _RANK[before], (kw, tool, before, after)
    assert after is not Verdict.ALLOW


def test_re_registering_the_same_severity_is_unchanged():
    et.register_effect_tag("holo_actuate", high_severity=True)
    again = et.register_effect_tag("HOLO_ACTUATE ", high_severity=True)
    assert again.outcome == et.UNCHANGED and again.value == "holo_actuate"


def test_a_canonical_tag_cannot_be_lowered_through_the_registry():
    r = et.register_effect_tag("money_move", high_severity=False)
    assert r.outcome == et.REFUSED_LOWERING and r.high_severity is True
    assert et.is_high_severity("money_move") is True
    assert "money_move" not in et._EXTENSIONS


def test_only_a_literal_True_registers_high_severity():
    """DEC-36: ``bool(x)`` dispatches ``__bool__``; the severity flag is read with
    ``is True``. A truthy non-bool does not silently promote -- and, since a new
    tag defaults to the UNKNOWN band, nothing is lost by not promoting it."""
    class _Truthy:
        def __bool__(self):
            raise AssertionError("severity must not be read through __bool__")

    r = et.register_effect_tag("holo_actuate", high_severity=_Truthy())
    assert r.high_severity is False


def test_an_empty_value_is_still_a_programming_error():
    with pytest.raises(ValueError):
        et.register_effect_tag("   ")


# -- C. the module says so, truthfully ---------------------------------------

def test_the_docstrings_state_IMPL10_and_drop_the_false_claim():
    doc = et.__doc__ or ""
    assert "IMPL-10" in doc
    assert "UNKNOWN band" in doc
    assert "never lowered" in doc
    reg = et.register_effect_tag.__doc__ or ""
    # the old claim: "the gate then treats it by its declared severity" -- false
    # for a default registration, which the gate now treats as UNKNOWN.
    assert "treats it by its declared severity" not in reg
    assert "UNKNOWN band" in reg and "REFUSED" in reg
    gate_doc = ag.__doc__ or ""
    assert "IMPL-10" in gate_doc
