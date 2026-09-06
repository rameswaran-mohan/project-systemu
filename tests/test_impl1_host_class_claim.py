"""IMPL-1 honesty pin: the tool gate card may not promise a host-class binding
that the signature it is describing does not actually carry.

THE DEFECT THIS PINS. ``command_approvals.tool_signature`` really does accept a
``host_class`` and really does discriminate on it (tests/test_tool_signature.py
::test_host_class_regate_and_default), so the card's old copy -- "'Always
allow' remembers this exact tool body + effect set + host class." -- read as a
true statement about the machinery. It was not a true statement about THIS
SYSTEM. No host resolver exists yet, so every production caller passes
``host_class=""`` unconditionally: ``tool_sandbox._maybe_gate_tool`` hardcodes
it empty behind a comment saying the host signal is DEFERRED, and
``first_gate_review`` mirrors that. A host-parameterized tool blessed while it
was talking to one host therefore stays blessed against EVERY host -- and the
consent surface told the operator the opposite.

HOW TO FLIP THIS PIN CONSCIOUSLY. The two tests below are a conditional, not a
ban on the words. ``test_production_gate_binds_an_empty_host_class`` is the
WITNESS: it drives the real ToolSandbox gate and shows that the signature the
operator is being asked to bless is the ``host_class=""`` one, and that a
non-empty host class would have moved it. ``test_card_does_not_claim_a_host_
class`` is the PIN, and it re-derives that witness before it asserts anything
about the copy -- so the pin is only ever in force WHILE production binds an
empty host class.

When a future build lands a real host resolver and ``_maybe_gate_tool`` starts
passing a resolved ``host_class``, the WITNESS goes red FIRST and names exactly
what changed. That is the moment -- and the only moment -- to put the
host-class clause back into the card copy and invert this pin. Do NOT relax the
witness to keep the pin green: the pin means nothing without it.

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

# Any host that is not "". Used ONLY to show the emptiness the witness asserts is
# load-bearing: were a host class ever bound, the signature would be a different one.
OTHER_HOST = "tools.example.com"


def _drive_real_gate(tmp_path, monkeypatch):
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
    impl_file = impl_dir / f"{TOOL_NAME}.py"
    impl_file.write_text(BODY, encoding="utf-8")
    rel = impl_file.relative_to(tmp_path.parent)

    tool = Tool(
        id=f"tool_{TOOL_NAME}",
        name=TOOL_NAME,
        description="test tool for the IMPL-1 host-class claim",
        tool_type=ToolType.PYTHON_FUNCTION,
        implementation_path=str(rel),
        effect_tags=list(EFFECT_TAGS),
        version=1,
    )

    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))

    assert "descriptor" in captured, "the gate posted no card, so there is nothing to pin"
    assert captured["gate_type"] == "tool"
    captured["body_hash"] = hashlib.sha1(impl_file.read_bytes()).hexdigest()
    return captured


def _witnessed_empty_host_sig(captured):
    """THE WITNESS, as a value. Returns the signature the gate stamped, having
    established it is the ``host_class=""`` one. Raises the assertion (never a
    bare False) so a caller cannot read a failure as a pass."""
    sig = captured["context_extras"].get("tool_signature")
    # DEC-36: pin the concrete type in this frame before operating on it, so a
    # None / stub / mock cannot make the comparisons below vacuously agreeable.
    assert type(sig) is str and sig, f"gate stamped no tool_signature: {sig!r}"

    empty_host = tool_signature(TOOL_NAME, captured["body_hash"],
                                set(EFFECT_TAGS), host_class="")
    assert sig == empty_host, (
        "the live gate no longer binds an EMPTY host class. If a host resolver "
        "has landed, this is the conscious-flip moment: re-add the host-class "
        "clause to GateDescriptor.from_tool's copy and invert the pin in "
        "test_card_does_not_claim_a_host_class. Do not weaken this assertion.")
    return sig


def test_production_gate_binds_an_empty_host_class(tmp_path, monkeypatch):
    """WITNESS. The signature the operator's "Always allow" is keyed on is
    computed with ``host_class=""`` -- so the blessing does not discriminate on
    host, and a card that says it does is making a promise the store cannot
    keep."""
    captured = _drive_real_gate(tmp_path, monkeypatch)
    sig = _witnessed_empty_host_sig(captured)

    # The emptiness is a real choice, not a no-op: host_class IS load-bearing in
    # the signature, which is exactly why claiming it while passing "" was a lie
    # rather than a harmless simplification.
    other_host = tool_signature(TOOL_NAME, captured["body_hash"],
                                set(EFFECT_TAGS), host_class=OTHER_HOST)
    assert sig != other_host, (
        "tool_signature stopped discriminating on host_class; "
        "tests/test_tool_signature.py::test_host_class_regate_and_default "
        "is the primary pin for that property")


def test_card_does_not_claim_a_host_class(tmp_path, monkeypatch):
    """THE PIN. While the witness above holds, no part of the rendered gate card
    may use the phrase "host class" -- the claim and the code cannot drift apart
    again without a test going red."""
    captured = _drive_real_gate(tmp_path, monkeypatch)
    _witnessed_empty_host_sig(captured)      # the conditional this pin rides on

    card = captured["descriptor"]
    what = card.what_approve_does
    assert type(what) is str and what, f"card has no what_approve_does: {what!r}"

    # The card really is the standing-allow one. Without this the pin could pass
    # vacuously on a DENY card, whose copy never mentioned a host class anyway.
    assert "Always allow" in card.options, card.options

    rendered = "\n".join([card.title, card.inspect, what]).lower()
    assert "host class" not in rendered, (
        "the gate card claims a host-class binding, but the signature it "
        "describes was computed with host_class='' (see the witness above): "
        f"{what!r}")
    assert "host_class" not in rendered, rendered

    # The pin must not be satisfiable by deleting the sentence. The card still
    # has to tell the operator what the blessing DOES bind.
    assert "tool body" in what.lower(), what
    assert "effect set" in what.lower(), what
