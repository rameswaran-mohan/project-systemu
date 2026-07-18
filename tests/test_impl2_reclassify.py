"""IMPL-2 — a DENY is operator-remediable, but never rubber-stampable.

The DENY band (unclassifiable effect ∩ a high-severity signal) is a refusal, not a
prompt. That is right, but a false positive must not be a dead end. The remedy is a
*reclassify* flow: the operator assigns the real effect class under typed confirmation,
and the gate then **re-arbitrates from the raw signals**.

The load-bearing rule — and the whole reason this can't be a simple override — is that
reclassification defeats ONLY the "we couldn't classify it" conjunct. It can never clear
a signal the gate computed independently. So you cannot reclassify your way out of an
action that is destructive, irreversible, or financial: those are properties of the call,
not of the label on it.

Spec AC-d: a DENY-floored action offers reclassify; the gate re-arbitrates on the new
class; no path approves the original verdict as-is; and a DENY caused by an INDEPENDENT
high-severity signal, after reclassification to a benign class, does NOT reach ALLOW.
"""
from __future__ import annotations

from systemu.runtime.action_governance import (
    ActionContext, Verdict, evaluate_action,
)


def _ctx(**kw):
    base = dict(tool="sync_records", effect_tags=set())
    base.update(kw)
    return ActionContext(**base)


# ── the baseline this remedies ───────────────────────────────────────────────

def test_an_unclassifiable_destructive_call_is_denied():
    v, _ = evaluate_action(_ctx(is_destructive_param=True))
    assert v == Verdict.DENY


# ── AC-d: reclassification cannot rescue an independently dangerous action ───

def test_reclassifying_a_destructive_call_does_not_reach_allow():
    # The destructive-parameter signal is a property of the CALL, not of the label, so
    # it survives reclassification — the action becomes approvable, never silent.
    v, _ = evaluate_action(_ctx(is_destructive_param=True,
                                operator_assigned_class="local_read"))
    assert v == Verdict.REQUIRE_APPROVAL
    assert v != Verdict.ALLOW


def test_reclassifying_an_irreversible_call_does_not_reach_allow():
    v, _ = evaluate_action(_ctx(irreversible=True, operator_assigned_class="local_read"))
    assert v == Verdict.REQUIRE_APPROVAL


def test_reclassifying_cannot_strip_a_name_derived_money_escalator():
    # "this wire-transfer tool is really just a local read" must not disarm it. The name
    # verb map is an INDEPENDENT signal and the operator's class is additive, so the
    # money escalator survives and the call still needs approval rather than running.
    # (It must also not be made STRICTER than it was without the reclassification —
    # using the remedy should never cost the operator an option they already had.)
    baseline, _ = evaluate_action(_ctx(tool="wire_funds_to_vendor"))
    v, _ = evaluate_action(_ctx(tool="wire_funds_to_vendor",
                                operator_assigned_class="local_read"))
    assert baseline == Verdict.REQUIRE_APPROVAL
    assert v == Verdict.REQUIRE_APPROVAL


def test_reclassifying_cannot_strip_a_network_target_escalation():
    v, _ = evaluate_action(_ctx(tool="push_record", target="api.example.com",
                                target_is_network=True, irreversible=True,
                                operator_assigned_class="local_read"))
    assert v == Verdict.REQUIRE_APPROVAL


# ── the remedy must actually REMEDY (an inert remedy is a dead end) ──────────

def test_reclassifying_a_denied_action_makes_it_approvable():
    """The whole point of IMPL-2: a DENY is a refusal, not a dead end.

    Before: unclassifiable + destructive ⇒ DENY, with no way forward. After the operator
    says what the effect actually is, the gate re-runs the same ladder — the destructive
    signal still stands, so it lands on an honest approval card instead of a refusal."""
    assert evaluate_action(_ctx(is_destructive_param=True))[0] == Verdict.DENY
    v, why = evaluate_action(_ctx(is_destructive_param=True,
                                  operator_assigned_class="local_write"))
    assert v == Verdict.REQUIRE_APPROVAL          # remediated, not still refused
    assert v != Verdict.DENY


def test_a_garbage_class_is_not_a_reclassification():
    # An unrecognised value classifies nothing, so it must not defeat the "we couldn't
    # classify it" conjunct — otherwise it would strip UNKNOWN and put nothing in its
    # place, quietly turning a refusal into an approval on no information at all.
    for junk in ("GARBAGE", "not_a_tag", "   ", "", "unknown"):
        v, _ = evaluate_action(_ctx(is_destructive_param=True, operator_assigned_class=junk))
        assert v == Verdict.DENY, junk


def test_a_tag_derived_deny_is_also_remediable():
    # The other route into DENY: a tool whose stored tags mix a real high-severity tag
    # with an unrecognised one (a legacy, backfilled or misspelled tag coerces to
    # UNKNOWN). Reachable from real Tool records, so it needs the remedy too.
    denied, _ = evaluate_action(_ctx(effect_tags={"money_move", "novel_modality_tag"}))
    assert denied == Verdict.DENY
    v, _ = evaluate_action(_ctx(effect_tags={"money_move", "novel_modality_tag"},
                                operator_assigned_class="net_read"))
    assert v == Verdict.REQUIRE_APPROVAL          # money_move survives ⇒ still gated
    assert v != Verdict.ALLOW


# ── the remedy actually works when the DENY was only about not knowing ───────

def test_how_the_deny_tier_is_actually_reached():
    """The reachability facts this file depends on, pinned so they can't drift.

    A name that implies money is itself a positive classification, so it defeats
    "unclassifiable" and lands on approval — NOT on the refusal tier. The refusal tier
    needs the effect to still be unclassifiable AND an escalator: either a raw signal
    (destructive parameters / irreversibility) or a high-severity tag sitting alongside
    an unrecognised one."""
    assert evaluate_action(_ctx(tool="process_payment"))[0] == Verdict.REQUIRE_APPROVAL
    assert evaluate_action(_ctx(is_destructive_param=True))[0] == Verdict.DENY
    assert evaluate_action(_ctx(irreversible=True))[0] == Verdict.DENY
    assert evaluate_action(_ctx(effect_tags={"money_move", "novel_modality_tag"}))[0] == Verdict.DENY


def test_reclassification_never_widens_the_verdict():
    # Using the remedy must never open a path that did not exist before, for ANY class.
    for assigned in ("local_read", "local_write", "net_read", "net_mutate",
                     "send_message", "money_move"):
        v, _ = evaluate_action(_ctx(tool="benign_widget", operator_assigned_class=assigned))
        assert v in (Verdict.REQUIRE_APPROVAL, Verdict.DENY), assigned


def test_reclassification_never_makes_a_verdict_stricter_either():
    # …and it must not COST the operator an option they already had: an action that was
    # approvable before reclassification stays approvable after it.
    for kw in ({"tool": "wire_funds_to_vendor"},
               {"tool": "send_note", "effect_tags": {"send_message"}},
               {"effect_tags": {"net_mutate"}}):
        before, _ = evaluate_action(_ctx(**kw))
        after, _ = evaluate_action(_ctx(operator_assigned_class="local_read", **kw))
        assert before == Verdict.REQUIRE_APPROVAL and after == Verdict.REQUIRE_APPROVAL, kw


def test_a_reclassified_action_is_never_frictionless():
    # It was refused once; the operator must approve the NEW classification on a card.
    # It must never fall through to ALLOW, which would run it with no prompt at all.
    for assigned in ("local_read", "local_write", "net_read"):
        v, _ = evaluate_action(_ctx(tool="benign_widget", operator_assigned_class=assigned))
        assert v == Verdict.REQUIRE_APPROVAL, assigned


def test_no_path_approves_the_original_verdict_as_is():
    # Without a reclassification the DENY stands — there is no "approve anyway".
    v, _ = evaluate_action(_ctx(is_destructive_param=True, operator_assigned_class=None))
    assert v == Verdict.DENY


# ── the whole-space properties, swept ───────────────────────────────────────

def test_reclassification_properties_hold_across_the_input_space():
    """A sweep, because the per-case tests above can each pass for the wrong reason.

    Four properties, over the cross-product of tag sets, tool names and every boolean
    signal:
      1. a valid reclassification can never reach ALLOW (it must never buy silence);
      2. the ONLY softening it may cause is the remediation itself — refusal becomes an
         approval card. Nothing else may loosen, and nothing may become frictionless;
      3. an unrecognised class changes NOTHING (it classifies nothing, so it is not a
         reclassification and must not defeat "unclassifiable");
      4. it genuinely REMEDIES — refusals do become approvable. An inert remedy is a
         dead end, which is the failure mode this feature exists to remove.
    """
    from itertools import combinations, product

    tags = ["local_read", "local_write", "net_read", "net_mutate", "send_message",
            "money_move", "local_delete", "novel_modality_tag", "unknown"]
    names = ["sync_records", "wire_funds", "send_note", "read_a_file", "delete_rows"]
    valid = ["local_read", "local_write", "net_read", "net_mutate", "send_message", "money_move"]
    junk = ["GARBAGE", "", "   ", "unknown", "not_a_tag"]
    tagsets = [set()] + [set(c) for n in (1, 2) for c in combinations(tags, n)]
    rank = {Verdict.ALLOW: 0, Verdict.REQUIRE_APPROVAL: 1, Verdict.DENY: 2}

    remediated = 0
    for ts, name, net, irr, dp, tr in product(tagsets, names, (0, 1), (0, 1), (0, 1), (0, 1)):
        kw = dict(tool=name, effect_tags=ts, target_is_network=bool(net),
                  irreversible=bool(irr), is_destructive_param=bool(dp),
                  classification_trusted=bool(tr))
        base, _ = evaluate_action(ActionContext(**kw))
        for cls in valid:
            v, _ = evaluate_action(ActionContext(operator_assigned_class=cls, **kw))
            assert v != Verdict.ALLOW, (kw, cls)                    # 1
            if rank[v] < rank[base]:                                # 2
                assert base == Verdict.DENY and v == Verdict.REQUIRE_APPROVAL, (kw, cls)
            if base == Verdict.DENY and v != Verdict.DENY:
                remediated += 1
        for cls in junk:
            v, _ = evaluate_action(ActionContext(operator_assigned_class=cls, **kw))
            assert v == base, (kw, cls)                             # 3
    assert remediated > 0, "the remedy is inert — a reclassified refusal never becomes approvable"


def test_a_mistyped_field_on_the_security_context_is_loud():
    # extra="forbid": a typo'd field name must not silently score the call as though the
    # signal were never supplied.
    import pytest
    with pytest.raises(Exception):
        ActionContext(tool="t", operator_assigned_klass="local_read")


# ── the ordinary path is untouched ──────────────────────────────────────────

def test_ordinary_calls_are_unaffected_by_the_new_field():
    assert evaluate_action(_ctx(tool="read_a_file", effect_tags={"local_read"}))[0] == Verdict.ALLOW
    assert evaluate_action(_ctx(tool="send_email", effect_tags={"send_message"}))[0] == Verdict.REQUIRE_APPROVAL
    assert evaluate_action(_ctx())[0] == Verdict.REQUIRE_APPROVAL          # UNKNOWN, no escalator
    assert evaluate_action(_ctx(denied_by_policy=True))[0] == Verdict.DENY


def test_an_explicit_policy_denial_is_not_reclassifiable():
    # A policy denial is not an "we couldn't tell" problem, so the remedy must not apply.
    v, why = evaluate_action(_ctx(denied_by_policy=True, operator_assigned_class="local_read"))
    assert v == Verdict.DENY and "policy" in why


def test_a_mask_verdict_would_not_run_ungated():
    """MASK has no producer today, but it was listed in the gate's frictionless
    early-return — the one branch there that RUNS a call. Listing an unreachable verdict
    in a fail-open position is a latent hazard: the day it becomes producible, it runs
    ungated. The gate now returns early only for ALLOW."""
    import inspect
    from systemu.runtime import tool_sandbox
    src = inspect.getsource(tool_sandbox.ToolSandbox._maybe_gate_tool)
    assert "Verdict.MASK)" not in src and "Verdict.MASK," not in src


def test_the_security_context_validates_post_construction_assignment():
    """The gate SETS operator_assigned_class after building the context, so assignment
    must be validated too — extra="forbid" alone only covers construction."""
    import pytest
    ctx = ActionContext(tool="t", effect_tags=set())
    with pytest.raises(Exception):
        ctx.operator_assigned_class = 123        # not a str-or-None
