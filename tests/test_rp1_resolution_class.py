"""R-P1 SEC-1 — the load-bearing remote-resolution gate.

``classify_resolution(context) -> {"remotely_resolvable","floor"}`` is an
ALLOWLIST-style predicate: it defaults to ``"floor"`` and only stamps
``"remotely_resolvable"`` on POSITIVELY-recognized safe decision shapes.
Only ``remotely_resolvable`` decisions may be resolved from a phone
(Telegram); ``floor`` decisions stay dashboard-only. Buttons are opt-in per
recognized shape, never the default.

STEP-0 findings baked into these values (see the module docstring in
decision_bridge.py):
  * The decision-context verdict key is ``"verdict"`` and its value is a
    ``HarnessDecision`` enum value string: ``"grant"``/``"deny"``/``"escalate"``
    (harness_review.py). The command/tool gate seam also uses the string
    ``"require_approval"`` (interface/command/gate.py from_tool). We accept
    both families; only ``"deny"`` floors.
  * There is NO persisted typed-confirm boolean today: the DENY typed-confirm
    is an unbuilt follow-up (gate.py "S1b.1"), and the amend-then-approve
    band-increase typed-confirm is derived LIVE at dashboard resolve time from
    ``evaluate_amendment(...)["band_increase"]`` (insights.py) — it is not
    stamped into context at post(). We therefore (a) recognize a
    ``requires_typed_confirm``/``typed_confirm`` key defensively if a future
    caller sets one, and (b) floor the posture/policy gate types that carry
    those confirms by class instead.
  * ``is_secret_field(field: dict)`` (runtime/elicitation.py) reads
    ``field["format"]`` and ``field["name"]`` — an elicitation ``properties``
    entry has NO ``name`` key, so the caller must inject it.
"""
import pytest

from systemu.messaging.decision_bridge import classify_resolution as rc


# ── POSITIVE safe shapes → remotely_resolvable ────────────────────────────────

def test_normal_tool_gate_remote():
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
               "effect_tags": ["net_mutate"]}) == "remotely_resolvable"


def test_command_gate_remote():
    assert rc({"kind": "gate", "gate_type": "command", "verdict": "require_approval",
               "effect_tags": ["shell_exec"]}) == "remotely_resolvable"


def test_single_enum_elicitation_remote():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"x": {"enum": ["a", "b", "c"]}}}}) \
        == "remotely_resolvable"


def test_single_freetext_elicitation_remote():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"note": {"type": "string"}}}}) \
        == "remotely_resolvable"


def test_gate_without_explicit_verdict_floors():
    # R-P1 SEC-1 TIGHTENING (finding root cause + finding 5): this previously
    # asserted a forge gate with NO verdict was remotely_resolvable — TWO
    # fail-open bugs at once. Now: (a) forge is NOT in the remote gate set (only
    # command/tool are), and (b) a POSITIVE verdict is required — absence floors.
    # Both make this shape correctly floor.
    assert rc({"kind": "gate", "gate_type": "forge"}) == "floor"
    # And a command/tool gate with the verdict key ABSENT also floors (the real
    # bug: the sandbox extras used to omit it, so the money/deny floors were dead).
    assert rc({"kind": "gate", "gate_type": "tool",
               "effect_tags": ["net_mutate"]}) == "floor"        # no verdict key
    assert rc({"kind": "gate", "gate_type": "tool",
               "verdict": "require_approval"}) == "floor"        # no effect_tags key


def test_enum_at_boundary_four_is_remote():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"x": {"enum": ["a", "b", "c", "d"]}}}}) \
        == "remotely_resolvable"


# ── FLOOR — the §2.1 exclusion set ────────────────────────────────────────────

def test_money_move_floor():
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
               "effect_tags": ["money_move"]}) == "floor"


def test_irreversible_tag_floor():
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
               "effect_tags": ["irreversible"]}) == "floor"


def test_deny_floor():
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "deny"}) == "floor"
    # deny floors even with an otherwise-clean effect_tags list.
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "deny",
               "effect_tags": ["net_mutate"]}) == "floor"


def test_escalate_and_empty_verdict_floor():
    # Only grant / require_approval / allow permit remote; escalate / "" floor.
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "escalate",
               "effect_tags": []}) == "floor"
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "",
               "effect_tags": []}) == "floor"


def test_absent_verdict_key_floors():
    # POSITIVE-evidence rule: the verdict KEY must be present. Absence floors
    # (this is the exact hole the real gate paths fell through).
    assert rc({"kind": "gate", "gate_type": "tool",
               "effect_tags": ["net_mutate"]}) == "floor"


def test_absent_effect_tags_key_floors():
    # The effect_tags KEY must be present AND a list. Absence floors.
    assert rc({"kind": "gate", "gate_type": "tool",
               "verdict": "require_approval"}) == "floor"
    # Present-but-not-a-list (e.g. a stray string) also floors.
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
               "effect_tags": "net_mutate"}) == "floor"


def test_destructive_flag_floors_clean_gate():
    # A clean verdict + empty effect_tags still floors when destructive is set.
    assert rc({"kind": "gate", "gate_type": "command", "verdict": "require_approval",
               "effect_tags": [], "destructive": True}) == "floor"


def test_both_present_clean_is_remote():
    # The one remote shape: command/tool + affirmative verdict + a list carrying a
    # POSITIVE classification that is disjoint from the money/irreversible floor.
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "allow",
               "effect_tags": ["net_mutate"]}) == "remotely_resolvable"
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "grant",
               "effect_tags": ["local_write"]}) == "remotely_resolvable"


def test_empty_effect_tags_is_not_a_clean_list():
    # TIGHTENED: this case used to assert "remotely_resolvable" — it encoded the
    # fail-open reality that an EMPTY list satisfies "present + a list + disjoint
    # from the floor set". Empty is the ABSENCE of a classification, not a finding
    # of "no effect", so it now floors. See classify_resolution step 3 and
    # tests/test_empty_effect_tags_floor.py for the real-path coverage.
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "grant",
               "effect_tags": []}) == "floor"
    assert rc({"kind": "gate", "gate_type": "tool", "verdict": "grant",
               "effect_tags": ["unknown"]}) == "floor"


def test_typed_confirm_floor():
    # Defensive: recognize a future persisted typed-confirm boolean.
    assert rc({"kind": "gate", "gate_type": "command",
               "requires_typed_confirm": True}) == "floor"


def test_posture_change_floor():
    assert rc({"kind": "gate", "gate_type": "evolution"}) == "floor"
    assert rc({"kind": "gate", "gate_type": "recovery"}) == "floor"
    assert rc({"kind": "gate", "gate_type": "tools_blocked"}) == "floor"
    assert rc({"kind": "gate", "gate_type": "sampling"}) == "floor"


def test_multifield_elicitation_floor():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"a": {"type": "string"},
                                                   "b": {"type": "string"}}}}) == "floor"


def test_5plus_enum_floor():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"x": {"enum": ["a", "b", "c", "d", "e"]}}}}) \
        == "floor"


def test_secret_field_elicitation_floor():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"api_key": {"type": "string"}}}}) == "floor"


def test_password_format_field_elicitation_floor():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {"pw": {"type": "string",
                                                          "format": "password"}}}}) == "floor"


def test_zero_field_elicitation_floor():
    assert rc({"kind": "structured_question",
               "requested_schema": {"properties": {}}}) == "floor"


def test_unknown_or_empty_defaults_floor():
    assert rc({}) == "floor"
    assert rc({"gate_type": "mystery"}) == "floor"
    assert rc({"kind": "gate", "gate_type": "mcp_oauth"}) == "floor"  # credential handoff
    # R-P1 SEC-1 TIGHTENING (finding 3): mcp_call is no longer in the remote gate
    # set. It has NO working cross-process resume rail for a remote approval (it
    # needs inbox.resolve_gate to actually CALL the tool, which the messaging
    # resolver deliberately does not do), so a remotely-"approved" mcp_call would
    # be marked resolved yet never run. It now floors → dashboard-only.
    assert rc({"kind": "gate", "gate_type": "mcp_call"}) == "floor"


def test_non_dict_input_floor():
    # Fail-closed for garbage input — never raises.
    assert rc(None) == "floor"
    assert rc("not a dict") == "floor"
    assert rc(42) == "floor"


def test_gate_type_alone_without_kind_gate_remote():
    # A recognized gate_type identifies a gate even when kind is absent. R-P1
    # SEC-1 TIGHTENING: to STAY remote it must now carry BOTH the verdict AND the
    # effect_tags evidence (a clean, disjoint-from-floor list) — absence of either
    # floors. This is the correct fail-closed reality; a safe gate proves it.
    assert rc({"gate_type": "tool", "verdict": "require_approval",
               "effect_tags": ["net_mutate"]}) == "remotely_resolvable"


# ── Stamp at post() — persisted, additive ─────────────────────────────────────

def _fake_vault():
    from unittest.mock import MagicMock
    v = MagicMock()
    v.load_index.return_value = []
    saved = []
    v.save_decision.side_effect = lambda d: saved.append(d)
    return v, saved


def test_stamp_on_post_writes_resolution_class():
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vault, saved = _fake_vault()
    q = OperatorDecisionQueue(vault)
    q.post(
        title="Run tool",
        body="?",
        options=["Deny", "Approve"],
        context={"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
                 "effect_tags": ["net_mutate"]},
        dedup_key="tool:sig1",
    )
    assert len(saved) == 1
    ctx = saved[0].context
    assert ctx["resolution_class"] == "remotely_resolvable"
    # additive — original keys survive
    assert ctx["gate_type"] == "tool"
    # persisted — it goes in the saved dict()
    assert saved[0].to_dict()["context"]["resolution_class"] == "remotely_resolvable"


def test_stamp_on_post_floors_a_money_gate():
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vault, saved = _fake_vault()
    q = OperatorDecisionQueue(vault)
    q.post(
        title="Move money",
        body="?",
        options=["Deny", "Approve"],
        context={"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
                 "effect_tags": ["money_move"]},
        dedup_key="tool:sig2",
    )
    assert saved[0].context["resolution_class"] == "floor"


def test_stamp_defaults_floor_on_empty_context():
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vault, saved = _fake_vault()
    q = OperatorDecisionQueue(vault)
    q.post(title="Mystery", body="?", options=["Skip"], context=None, dedup_key="m:1")
    assert saved[0].context["resolution_class"] == "floor"


def test_stamp_respects_caller_provided_class():
    # A caller that already stamped resolution_class is not overwritten.
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vault, saved = _fake_vault()
    q = OperatorDecisionQueue(vault)
    q.post(
        title="Pre-stamped",
        body="?",
        options=["Skip"],
        context={"kind": "gate", "gate_type": "tool", "verdict": "require_approval",
                 "resolution_class": "floor"},
        dedup_key="pre:1",
    )
    # Even though this shape would classify remotely_resolvable, the explicit
    # caller value wins (fail-closed: a caller can only tighten, and re-stamping
    # a resumed decision must not silently upgrade it).
    assert saved[0].context["resolution_class"] == "floor"
