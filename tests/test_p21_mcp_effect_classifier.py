"""P21 -- the MCP effect classifier, rule by rule (no server, no vault).

``systemu.runtime.mcp.effect_class.classify_mcp_call`` scores an MCP call with the
native gate's machinery. These pins hold each rule at the classifier's own boundary;
``tests/test_p21_mcp_effect_classes.py`` holds them end to end through a real server.
"""
from __future__ import annotations

import pytest

from systemu.runtime.mcp.effect_class import (classify_mcp_call, hint_tier,
                                              money_signal, operator_declared_classes)


def _c(tool, annotations=None, *, params=None, schema=None, operator=None,
       trusted=True):
    return classify_mcp_call(tool=tool, params=params or {}, annotations=annotations,
                             schema=schema or {}, operator_classes=operator,
                             classification_trusted=trusted)


# ── DEC-34: server hints only raise ──────────────────────────────────────────

def test_destructive_hint_is_irreversible_even_with_no_name_signal():
    r = _c("reset_ledger", {"destructiveHint": True})
    assert r.irreversible == ("irreversible",)
    assert "irreversible" in r.effect_tags
    assert r.tier == "D"


@pytest.mark.parametrize("ann", [{"destructive_hint": True},     # mcp 2.x spelling
                                 {"destructiveHint": True, "readOnlyHint": True}])
def test_destructive_hint_wire_spellings_and_contradictions_raise(ann):
    assert _c("reset_ledger", ann).irreversible == ("irreversible",)


@pytest.mark.parametrize("name", ["remove_user", "delete_records", "purge_cache",
                                  "wipeDisk"])
def test_read_only_hint_never_hides_a_delete(name):
    r = _c(name, {"readOnlyHint": True})
    assert "local_delete" in r.irreversible
    assert r.tier == "D"


@pytest.mark.parametrize("name", ["send_digest", "create_issue", "post_update"])
def test_read_only_hint_never_hides_another_class(name):
    """A non-irreversible class the name map finds still un-gates nothing."""
    r = _c(name, {"readOnlyHint": True})
    assert r.tier != "R"
    assert r.irreversible == ()


def test_read_only_hint_never_hides_a_destructive_parameter():
    r = _c("lookup_status", {"readOnlyHint": True}, params={"q": "rm -rf /"})
    assert r.tier != "R"


def test_honest_read_only_hint_is_the_ungated_tier():
    r = _c("lookup_status", {"readOnlyHint": True})
    assert r.tier == "R" and r.irreversible == ()
    assert r.effect_tags == ("unknown",)


@pytest.mark.parametrize("ann", [None, {}, {"title": "x"},
                                 {"readOnlyHint": "true"}, {"destructiveHint": 1},
                                 {"readOnlyHint": None}, "not-a-dict", ["x"]])
def test_missing_or_unparseable_hints_are_the_unknown_band(ann):
    r = _c("sync_data", ann)
    assert r.effect_tags == ("unknown",)
    assert r.verdict == "require_approval"
    assert r.tier == "D"          # per-call, no session trust, never auto-allowed
    assert r.irreversible == ()


def test_unparseable_destructive_hint_does_not_lower_a_read_only_claim_to_R():
    assert _c("lookup_status", {"readOnlyHint": True, "destructiveHint": "yes"}).tier == "D"


def test_hint_tier_matrix_reads_both_wire_spellings():
    assert hint_tier({"readOnlyHint": True}) == "R"
    assert hint_tier({"read_only_hint": True}) == "R"
    assert hint_tier({"read_only_hint": False, "destructive_hint": False}) == "A"
    assert hint_tier({"destructive_hint": True, "read_only_hint": True}) == "D"
    assert hint_tier({"readOnlyHint": "True"}) == "D"


# ── money: the native heuristics, no second list ─────────────────────────────

@pytest.mark.parametrize("name", ["transfer_funds", "pay_invoice", "issue_refund",
                                  "createPayment", "wire_money", "checkout"])
def test_money_by_name(name):
    r = _c(name, {"readOnlyHint": False, "destructiveHint": False})
    assert "money_move" in r.irreversible, (name, r)


def test_money_by_schema_property_names():
    schema = {"type": "object", "properties": {"payment_token": {"type": "string"},
                                               "sku": {"type": "string"}}}
    r = _c("submit_order", {"readOnlyHint": False}, schema=schema)
    assert "money_move" in r.irreversible


def test_money_signal_is_the_whole_token_detector_not_a_substring():
    assert money_signal("summarize_payload", {}) is False      # "pay" inside a word
    assert money_signal("lookup", {"properties": {"max_price": {}}}) is False
    assert money_signal("move", {"properties": {"funds": {}, "transfer_to": {}}}) is True


def test_money_card_phrase_needs_no_amount_to_be_money():
    assert _c("transfer_funds").irreversible == ("money_move",)


# ── the operator's declaration ───────────────────────────────────────────────

def test_operator_declaration_raises():
    r = _c("lookup_status", {"readOnlyHint": True}, operator=["money_move"])
    assert r.irreversible == ("money_move",) and r.basis == "operator"


@pytest.mark.parametrize("marker,expect", [("payment", "money_move"),
                                           ("funds_move", "money_move"),
                                           ("irreversible", "irreversible"),
                                           ("IRREVERSIBLE ", "irreversible")])
def test_operator_r1_markers_are_kept_never_coerced_to_unknown(marker, expect):
    assert expect in _c("lookup_status", operator=[marker]).irreversible


def test_operator_declaration_may_lower_server_hints():
    r = _c("reset_ledger", {"destructiveHint": True}, operator=["net_mutate"])
    assert r.irreversible == ()
    assert r.effect_tags == ("net_mutate",)
    assert r.tier == "A"


def test_operator_allow_band_class_is_the_ungated_tier_only_when_trusted():
    assert _c("lookup", operator=["net_read"]).tier == "R"
    assert _c("lookup", operator=["net_read"], trusted=False).tier == "A"


def test_operator_cannot_lower_a_money_name_without_a_local_only_class():
    """The native rule: the name verb map still escalates a non-local declaration."""
    assert "money_move" in _c("transfer_funds", operator=["net_read"]).irreversible
    assert _c("transfer_funds", operator=["local_read"]).irreversible == ()


def test_operator_declaration_lives_in_the_connections_file_and_survives_writes(tmp_path):
    """The declaration round-trips through ``connections/mcp.json`` and survives every
    other write to that file (discovery pins, enablement, servers); enabling a tool
    with whatever a server sent never writes it."""
    from types import SimpleNamespace

    from systemu.runtime.mcp import connections as conn
    v = SimpleNamespace(root=str(tmp_path))
    conn.set_operator_effect_classes(v, " srv/ ", "lookup", ["money_move", 3, " "])
    assert conn.get_operator_effect_classes(v, "srv", "lookup") == ("money_move",)
    conn.add_server(v, "srv")
    conn.set_tool_hash(v, "srv", "lookup", "h")
    conn.set_tool_enabled(v, "srv", "lookup", True, description="d",
                          schema={"operator_effect_classes": ["no_effect"]},
                          annotations={"operator_effect_classes": ["no_effect"]})
    assert conn.get_operator_effect_classes(v, "srv", "lookup") == ("money_move",)
    assert conn.get_operator_effect_classes(v, "srv", "other") is None
    conn.set_operator_effect_classes(v, "srv", "lookup", [])
    assert conn.get_operator_effect_classes(v, "srv", "lookup") is None
    assert conn.get_state(v)["operator_effect_classes"] == {}


def test_a_malformed_declaration_table_reads_as_none_and_keeps_the_rest(tmp_path):
    import json
    from types import SimpleNamespace

    from systemu.runtime.mcp import connections as conn
    v = SimpleNamespace(root=str(tmp_path))
    conn.add_server(v, "srv")
    path = tmp_path / "connections" / "mcp.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["operator_effect_classes"] = ["not", "a", "dict"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert conn.get_operator_effect_classes(v, "srv", "x") is None
    assert conn.get_state(v)["servers"] == ["srv"]


@pytest.mark.parametrize("raw", [None, [], (), "money_move", {"a": 1}, 7])
def test_no_usable_declaration_is_no_declaration(raw):
    assert operator_declared_classes(raw) is None


def test_non_string_declaration_entries_are_unknown():
    declared, _irr = operator_declared_classes([3, None, "net_read"])
    assert declared == {"unknown", "net_read"}


# ── fail-closed ──────────────────────────────────────────────────────────────

def test_a_classification_that_raises_is_the_unknown_band(monkeypatch):
    import systemu.runtime.mcp.effect_class as ec

    def _boom(**_kw):
        raise RuntimeError("scorer down")
    monkeypatch.setattr(ec, "_classify", _boom)
    r = ec.classify_mcp_call(tool="lookup_status",
                             annotations={"readOnlyHint": True},
                             classification_trusted=True)
    assert r.tier == "D" and r.effect_tags == ("unknown",)


def test_the_r1_card_shape_from_the_classification():
    """The descriptor the gate posts for an irreversible class: Deny / Approve once,
    high risk, the phrase, and the R1 stamp in its decision context."""
    from systemu.interface.command.gate import GateDescriptor
    r = _c("transfer_funds", params={"to_account": "a", "amount": 75})
    g = GateDescriptor.from_mcp_call(server="srv", tool="transfer_funds",
                                     params={"to_account": "a", "amount": 75},
                                     destructive=True, effect_tags=r.effect_tags,
                                     irreversible_effects=r.irreversible,
                                     confirm_phrase="75")
    assert g.options == ["Deny", "Approve once"] and g.risk == "high"
    assert g.confirm_phrase == "75"
    ctx = g.to_decision_context(gate_type="mcp_call")
    assert ctx["requires_typed_confirm"] is True
    assert ctx["irreversible_effects"] == ["money_move"]
    assert g.what_approve_does.isascii()


def test_a_stale_mcp_card_rendered_from_context_drops_every_standing_option():
    """A card whose context carries the R1 stamp but whose stored options still list
    the standing choices (an older build) renders without them."""
    from systemu.interface.command.gate import GateDescriptor
    ctx = {"kind": "gate", "gate_type": "mcp_call", "effect_tags": ["money_move"],
           "tool_name": "transfer_funds"}
    g = GateDescriptor.from_decision_context(
        ctx, title="MCP tool call: transfer_funds",
        options=["Deny", "Approve once", "Trust this tool for the session",
                 "Always allow"])
    assert g.options == ["Deny", "Approve once"]
    assert g.irreversible_effects == ["money_move"]


# ── the operator's ASSIGNMENT from an MCP DENY card (IMPL-2 parity) ──────────

def test_an_unclassified_destructive_hint_is_the_deny_floor():
    r = _c("reset_ledger", {"destructiveHint": True})
    assert r.verdict == "deny" and r.tier == "D"


def test_an_assignment_lifts_the_refusal_but_keeps_every_other_signal():
    r = classify_mcp_call(tool="reset_ledger", annotations={"destructiveHint": True},
                          operator_assigned="local_write",
                          classification_trusted=True)
    assert r.verdict == "require_approval"
    assert r.irreversible == ("irreversible",)          # the server's raise stands
    assert "local_write" in r.effect_tags and "unknown" not in r.effect_tags


@pytest.mark.parametrize("assigned", ["local_read", "net_read", "local_write"])
def test_an_assignment_is_never_frictionless(assigned):
    r = classify_mcp_call(tool="lookup", annotations={"readOnlyHint": True},
                          operator_assigned=assigned, classification_trusted=True)
    assert r.verdict == "require_approval" and r.tier != "R"


@pytest.mark.parametrize("assigned", [None, "", "  ", 7, ["local_read"]])
def test_no_usable_assignment_is_no_assignment(assigned):
    r = classify_mcp_call(tool="reset_ledger", annotations={"destructiveHint": True},
                          operator_assigned=assigned, classification_trusted=True)
    assert r.verdict == "deny"


def test_an_assignment_and_a_declaration_share_one_entry(tmp_path):
    from types import SimpleNamespace

    from systemu.runtime.mcp import connections as conn
    v = SimpleNamespace(root=str(tmp_path))
    conn.set_operator_effect_classes(v, "srv", "t", ["money_move"])
    conn.set_operator_effect_classes(v, "srv", "t", assigned="local_write",
                                     decision_id="dec_1")
    assert conn.get_operator_effect_classes(v, "srv", "t") == ("money_move",)
    assert conn.get_operator_assigned_class(v, "srv", "t") == "local_write"
    conn.set_operator_effect_classes(v, "srv", "t", [])          # declaration only
    assert conn.get_operator_effect_classes(v, "srv", "t") is None
    assert conn.get_operator_assigned_class(v, "srv", "t") == "local_write"
    conn.set_operator_effect_classes(v, "srv", "t", assigned=None)
    assert conn.get_state(v)["operator_effect_classes"] == {}


def test_the_mcp_deny_card_is_a_refusal_shape():
    from systemu.interface.command.gate import RECLASSIFY_OPTION, GateDescriptor
    g = GateDescriptor.from_mcp_call(server="srv", tool="reset_ledger",
                                     params={"ledger_id": "L1"},
                                     effect_tags=("irreversible", "unknown"),
                                     irreversible_effects=("irreversible",),
                                     confirm_phrase="x", verdict="deny",
                                     reason="unclassifiable \u2014 refusing")
    assert g.options == ["Deny", RECLASSIFY_OPTION] and g.safe_default == "Deny"
    assert g.risk == "high"
    assert g.irreversible_effects == [] and g.confirm_phrase == ""
    assert g.what_approve_does.isascii() and g.inspect.isascii()
    assert g.what_approve_does.startswith("Nothing")


# ── residual (a): the credit-net obligation reads the classification ────────

@pytest.mark.parametrize("tool,ann,mutation", [
    ("remove_user", {"readOnlyHint": True}, True),          # a delete claiming read-only
    ("transfer_funds", {"readOnlyHint": True}, True),       # money claiming read-only
    ("lookup_status", {"readOnlyHint": True}, False),       # honest read-only
    ("lookup_status", {"read_only_hint": True}, False),     # mcp 2.x spelling
    ("lookup_status", {"readOnlyHint": "true"}, True),      # unparseable -> UNKNOWN
    ("mystery", {}, True),                                  # no hint -> UNKNOWN
])
def test_a_registered_mcp_tool_keeps_its_obligation_unless_truly_read_only(
        tmp_path, tool, ann, mutation):
    """``is_action_tool`` (the per-actuation verification obligation) comes from the
    classifier, raise-only: a destructive or money tool claiming readOnlyHint keeps
    the obligation, and the credit seam still resolves it as a known mutation."""
    from types import SimpleNamespace

    from systemu.runtime.mcp.sdk import registry_bridge as rb
    from systemu.runtime.shadow_runtime import _known_mutation_mcp_entry
    from systemu.runtime.tool_registry_v2 import registry
    v = SimpleNamespace(root=str(tmp_path))
    server = "http://p21-oblig"
    rb.register_server_tools(v, server, [{"name": tool, "description": "d",
                                          "parameters_schema": {},
                                          "annotations": ann}])
    try:
        name = rb.namespaced_name(server, tool)
        assert registry.get(name).is_action_tool is mutation
        entry = _known_mutation_mcp_entry({"tool_name": name})
        assert (entry is not None) is mutation
    finally:
        rb.unregister_server_tools(server)


def test_an_operator_declaration_decides_the_obligation_too(tmp_path):
    from types import SimpleNamespace

    from systemu.runtime.mcp import connections as conn
    from systemu.runtime.mcp.sdk import registry_bridge as rb
    v = SimpleNamespace(root=str(tmp_path))
    conn.set_operator_effect_classes(v, "http://p21-oblig", "lookup_status",
                                     ["money_move"])
    assert rb.is_mutation(v, "http://p21-oblig", "lookup_status",
                          {"readOnlyHint": True}, {}) is True
