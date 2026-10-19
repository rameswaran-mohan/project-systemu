"""IMPL-1 honesty pin: the tool gate card may claim a host-class binding ONLY
because -- and only while -- the signature it describes actually carries one.

THE DEFECT THIS FILE WAS BORN TO PIN (v0.10.27). ``command_approvals
.tool_signature`` has always accepted a ``host_class`` and discriminated on it,
so the card's copy -- "'Always allow' remembers this exact tool body + effect
set + host class" -- read as a true statement about the machinery. It was not
true of the SYSTEM: every production caller passed ``host_class=""``, so a
host-parameterized tool blessed while it talked to one host stayed blessed
against EVERY host. The clause was removed and this file pinned its absence,
conditional on a WITNESS that the live gate bound an empty host class.

THE CONSCIOUS FLIP (GATE-6, capability-diff Slice 2). The host class is now
real: ``command_approvals.tool_host_class`` derives it deterministically from the
tool's DECLARED spec -- ``none`` (no network effect; written into the signature
as the empty string every pre-host signature carried), ``undeclared`` (a network
effect and no declared hosts), or the sorted unique declared hostnames -- and
``ToolSandbox._maybe_gate_tool`` binds it. The witness went red exactly as its
docstring said it would, and was flipped, not weakened:

  * ``test_production_gate_binds_the_derived_host_class`` -- the WITNESS, now
    asserting the live gate's stamped signature is the one keyed on the DERIVED
    class (and, for a networked tool, NOT the old empty-class one);
  * ``test_card_claims_the_host_class_it_binds`` -- the PIN, which re-derives
    that witness first and only then requires the card to claim the binding and
    show the ``hosts:`` line.

If a future change stops binding the derived class, the witness goes red first
and the card claim must come out again. Do NOT relax the witness to keep the
pin green: the pin means nothing without it.

Mirrors tests/test_s1b_tool_gate.py for how the live gate is reached, so the
two files cannot disagree about what "the real gate" is.
"""
import asyncio
import hashlib

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature

TOOL_NAME = "send_status_email"
EFFECT_TAGS = ["send_message"]          # REQUIRE_APPROVAL band -> a card with
                                        # "Always allow" on it (not the DENY card,
                                        # which offers no standing allow at all).
BODY = "def run():\n    return {'success': True}\n"

# The derivation table, written out by hand rather than computed with the
# function under test: (tool name, declared effect tags, declared hosts, the
# host class the card shows, the value the SIGNATURE is keyed on).
CASES = [
    # a network effect and no hosts -> "undeclared"
    (TOOL_NAME, ["send_message"], None, "undeclared", "undeclared"),
    # a network effect and hosts -> the sorted, unique, lower-cased hostnames
    ("post_status_update", ["net_mutate"], ["Mail.Example.com", "api.example.com",
                                            "api.example.com"],
     "api.example.com,mail.example.com", "api.example.com,mail.example.com"),
    # no network effect -> "none", which signs as the pre-host empty string
    ("archive_old_reports", ["local_delete"], None, "none", ""),
]

# The STANDING-ALLOW card's cases. R1 (ruled 2026-10-19): ``local_delete`` is
# irreversible -- the shipped file_delete unlinks permanently and nothing signals a
# recycle path -- so its card offers no "Always allow" at all and cannot carry the
# claim this test pins. The no-network row is re-pointed to ``clipboard_write``: an
# approval-band, non-network, NON-irreversible class. The test asserts both
# properties itself, so the example can never silently become irreversible again.
# (GATE-6, authorised for exactly this case; the binding test above keeps CASES.)
CLAIM_CASES = [
    row if row[0] != "archive_old_reports"
    else ("archive_old_reports", ["clipboard_write"], None, "none", "")
    for row in CASES
]


def _drive_real_gate(tmp_path, monkeypatch, *, name=TOOL_NAME,
                     effect_tags=tuple(EFFECT_TAGS), hosts=None):
    """Run the REAL ToolSandbox action gate on an effectful forged tool and
    return the posted card, the stamped context_extras, and the body hash of the
    implementation the gate actually read."""
    captured = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, context_extras=None, **kw):
            captured["descriptor"] = descriptor
            captured["gate_type"] = gate_type
            captured["context_extras"] = context_extras or {}
            return "dec_impl1_hostclass"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    from systemu.runtime.tool_sandbox import ToolSandbox
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=object(), command_approvals=store)

    # A real impl file on disk (the gate hashes the BYTES), placed where
    # execute_tool resolves a path relative to vault_root.parent.
    impl_dir = tmp_path.parent / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_file = impl_dir / f"{name}.py"
    impl_file.write_text(BODY, encoding="utf-8")
    rel = impl_file.relative_to(tmp_path.parent)

    tool = Tool(
        id=f"tool_{name}",
        name=name,
        description="test tool for the IMPL-1 host-class claim",
        tool_type=ToolType.PYTHON_FUNCTION,
        implementation_path=str(rel),
        effect_tags=list(effect_tags),
        hosts=list(hosts or []),
        version=1,
    )

    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))

    assert "descriptor" in captured, "the gate posted no card, so there is nothing to pin"
    assert captured["gate_type"] == "tool"
    captured["body_hash"] = hashlib.sha1(impl_file.read_bytes()).hexdigest()
    return captured


def _witnessed_derived_host_sig(captured, *, name, effect_tags, signed_class):
    """THE WITNESS, as a value. Returns the signature the gate stamped, having
    established it is the one keyed on the DERIVED host class. Raises the
    assertion (never a bare False) so a caller cannot read a failure as a pass."""
    sig = captured["context_extras"].get("tool_signature")
    # DEC-36: pin the concrete type in this frame before operating on it, so a
    # None / stub / mock cannot make the comparisons below vacuously agreeable.
    assert type(sig) is str and sig, f"gate stamped no tool_signature: {sig!r}"

    derived = tool_signature(name, captured["body_hash"], set(effect_tags),
                             host_class=signed_class)
    assert sig == derived, (
        "the live gate no longer binds the DERIVED host class. If the host class "
        "was deliberately dropped from the signature, the card's host-class claim "
        "must come out again and test_card_claims_the_host_class_it_binds must be "
        "inverted. Do not weaken this assertion.")
    if signed_class:
        # A networked tool: the binding is real, so the pre-host-check (empty
        # class) signature must be a DIFFERENT key -- that is what makes a change
        # of declared hosts, and an approval minted before host checks, re-gate.
        pre_host = tool_signature(name, captured["body_hash"], set(effect_tags),
                                  host_class="")
        assert sig != pre_host, "a networked tool signed with an empty host class"
    return sig


@pytest.mark.parametrize("name,effect_tags,hosts,shown_class,signed_class", CASES)
def test_production_gate_binds_the_derived_host_class(
        tmp_path, monkeypatch, name, effect_tags, hosts, shown_class, signed_class):
    """WITNESS. The signature the operator's "Always allow" is keyed on is
    computed with the host class derived from the tool's declared spec -- so the
    blessing discriminates on it, and the card may say so."""
    captured = _drive_real_gate(tmp_path, monkeypatch, name=name,
                                effect_tags=effect_tags, hosts=hosts)
    _witnessed_derived_host_sig(captured, name=name, effect_tags=effect_tags,
                                signed_class=signed_class)
    # The parts stamped beside the signature name the class the operator sees.
    assert captured["context_extras"]["tool_parts"]["host_class"] == shown_class


@pytest.mark.parametrize("name,effect_tags,hosts,shown_class,signed_class",
                         CLAIM_CASES)
def test_card_claims_the_host_class_it_binds(
        tmp_path, monkeypatch, name, effect_tags, hosts, shown_class, signed_class):
    """THE PIN. While the witness above holds, the standing-allow card tells the
    operator what the blessing binds -- body, effect set AND host class -- and
    shows the class it is binding on a `hosts:` line."""
    # R1: a standing-allow card exists only for an approval-band effect that is
    # NOT irreversible. Assert both of the example itself, so a later
    # reclassification of its tag fails HERE, by name, not as a missing option.
    from systemu.runtime.action_governance import (ActionContext, Verdict,
                                                   evaluate_action,
                                                   irreversible_effects)
    assert irreversible_effects(list(effect_tags)) == (), effect_tags
    assert evaluate_action(ActionContext(tool=name, effect_tags=set(effect_tags)))[0] \
        is Verdict.REQUIRE_APPROVAL, effect_tags

    captured = _drive_real_gate(tmp_path, monkeypatch, name=name,
                                effect_tags=effect_tags, hosts=hosts)
    _witnessed_derived_host_sig(captured, name=name, effect_tags=effect_tags,
                                signed_class=signed_class)   # the conditional

    card = captured["descriptor"]
    what = card.what_approve_does
    assert type(what) is str and what, f"card has no what_approve_does: {what!r}"

    # The card really is the standing-allow one. Without this the pin could pass
    # vacuously on a DENY card, which offers no standing allow to describe.
    assert "Always allow" in card.options, card.options

    assert "tool body + effect set + host class" in what, what
    assert "changing the hosts it may reach re-gates it" in what, what
    assert f"\nhosts: {shown_class}" in card.inspect, card.inspect
    # ASCII: this is the line an operator quotes back, on any console codepage.
    "\n".join([card.title, card.inspect, what]).encode("ascii")
