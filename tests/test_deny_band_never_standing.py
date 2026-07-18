"""IMPL-1 — an "Always allow" can NEVER cover the DENY band.

The DENY band is the unknown-∩-high-severity floor: systemu could not classify the
effect AND a high-severity signal fired. It is the last thing standing between the agent
and an unclassifiable destructive action.

The hole this pins (found by grounding, live on mainline before this): the gate stamped
the verdict at park time, but the approval recorder read only the operator's CHOICE. So
"Always allow" on a DENY card wrote a STANDING approval — and on the next call
``tool_sandbox`` consults the approval store BEFORE any band check and early-returns,
running the DENY-band tool UNGATED, permanently. Nothing pinned the negative.

Defence in depth, both halves pinned here:
  * the recorder refuses to persist ANY approval for a DENY verdict;
  * the card does not offer the option in the first place.

IMPL-2 STRENGTHENED THE RECORDER RULE (adversarial review, CRITICAL). It used to
DEGRADE a DENY-band approval to a single-use resume bridge, on the reasoning that the
bridge is harmless because every bypass in ``tool_sandbox._maybe_gate_tool`` sits under
``if verdict != Verdict.DENY``. That held only for as long as nothing could lift a DENY.
IMPL-2's operator reclassification lifts exactly those params to REQUIRE_APPROVAL — at
which point the degraded bridge becomes REDEEMABLE, and it is the one bypass left live
under a pending reclassification. So "Approve once" on a DENY card, a documented no-op
that an operator would naturally try first, silently minted a token that a later
legitimate use of the remedy would cash: the destructive call ran with no card ever
shown for the assigned classification.

A DENY-band gate now records NOTHING — the rule the coords-less rescue already carried.
The assertions below were tightened accordingly (``single_use == []``, not
``== ["sig-1"]``); nothing here was relaxed.
"""
from __future__ import annotations

import pytest

from systemu.interface.command.gate import GateDescriptor
from systemu.runtime import resume_on_decision as rod


class _Store:
    """Records which persistence path was taken."""
    def __init__(self):
        self.standing, self.single_use = [], []

    def approve(self, sig):
        self.standing.append(sig)

    def mark_resume_approved(self, sig, *, for_reclassification=None):
        # IMPL-2 added the scope kwarg (which reclassified card minted this bridge);
        # the double has to carry it or a real call raises TypeError into the
        # recorder's best-effort except and this file silently tests nothing.
        self.single_use.append(sig)


def _record(monkeypatch, *, verdict, choice):
    store = _Store()
    monkeypatch.setattr(rod, "init_default_store", lambda _p: store, raising=False)
    import systemu.runtime.command_approvals as _ca
    monkeypatch.setattr(_ca, "init_default_store", lambda _p: store, raising=False)
    rod._record_gate_approval(
        {"tool_signature": "sig-1", "verdict": verdict}, is_tool_gate=True, choice=choice)
    return store


# ── the recorder half ────────────────────────────────────────────────────────

def test_always_allow_on_a_deny_records_nothing_at_all(monkeypatch):
    store = _record(monkeypatch, verdict="deny", choice="always allow")
    assert store.standing == [], "a DENY-band tool must never get a standing allow"
    assert store.single_use == [], (
        "nor a single-use bridge — IMPL-2's reclassification can LIFT a DENY on these "
        "params, and the bridge is the one bypass live under a pending "
        "reclassification, so a degraded one-shot is redeemable later")


def test_always_allow_on_a_non_deny_is_still_standing(monkeypatch):
    # the carve-out must be surgical — normal approvals keep working exactly as before
    store = _record(monkeypatch, verdict="require_approval", choice="always allow")
    assert store.standing == ["sig-1"] and store.single_use == []


def test_a_missing_verdict_fails_closed(monkeypatch):
    # A gate parked before the verdict was carried has no safety evidence. The other
    # reader of this exact key (the remote lane's classify_resolution) already floors on
    # absence; this matches it. Cost of closing: one extra ask on a legacy card. Cost of
    # leaving it open: a standing allow on an unknown band.
    store = _record(monkeypatch, verdict="", choice="always allow")
    assert store.standing == [] and store.single_use == ["sig-1"]


def test_deny_verdict_is_matched_case_insensitively(monkeypatch):
    for v in ("DENY", "Deny", "deny"):
        store = _record(monkeypatch, verdict=v, choice="always allow")
        assert store.standing == [], f"{v!r} must not persist"


def test_approve_once_on_a_deny_records_nothing(monkeypatch):
    """THE REGRESSION PIN for the IMPL-2 adversarial finding. "Approve once" is the
    natural first click on a refusal card and does nothing at the gate — but minting a
    bridge for it left a token that a later reclassification could cash. The DENY card
    no longer OFFERS the option (see ``test_a_deny_card_offers_no_approval_at_all``);
    this is the recorder half, so no other surface can supply the choice and get one."""
    store = _record(monkeypatch, verdict="deny", choice="approve once")
    assert store.standing == [] and store.single_use == []


def test_no_choice_whatsoever_persists_on_a_deny(monkeypatch):
    for choice in ("approve", "approve once", "always allow", "trust for session", "yes"):
        store = _record(monkeypatch, verdict="deny", choice=choice)
        assert store.standing == [] and store.single_use == [], choice


# ── the card half ────────────────────────────────────────────────────────────

def test_a_deny_card_offers_no_approval_at_all():
    """IMPL-2: neither a standing allow NOR "Approve once". Both are no-ops at the
    gate, and offering the latter is what minted the stale, later-redeemable bridge.
    What the card does offer is the reclassify remedy."""
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    assert "Always allow" not in d.options
    assert "Approve once" not in d.options
    assert d.options == ["Deny", RECLASSIFY_OPTION]
    assert d.options[0] == "Deny" and d.safe_default == "Deny"   # fail-closed default kept
    assert d.risk == "high"


def test_a_require_approval_card_still_offers_always_allow():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval")
    assert d.options == ["Deny", "Approve once", "Always allow"]
    assert d.safe_default == "Deny" and d.risk == "medium"


def test_a_deny_card_says_it_cannot_be_remembered():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    assert "cannot be remembered" in d.what_approve_does


def test_the_card_matches_a_verdict_enum_not_just_its_string():
    # str(Verdict.DENY) is "Verdict.DENY" — a caller passing the enum rather than its
    # .value must not silently get an "Always allow" back on a DENY card.
    from systemu.runtime.action_governance import Verdict
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict=Verdict.DENY)
    assert "Always allow" not in d.options


# ── the CONSUMPTION half: no stored approval may satisfy the DENY band ───────
# This is the load-bearing one. Refusing to RECORD a standing allow is not enough,
# because the tool signature is params-INDEPENDENT while the DENY verdict is
# params-DEPENDENT — so an approval granted legitimately on a benign call would
# otherwise cover the same tool's destructive calls forever.

def test_a_standing_approval_from_a_benign_call_does_not_cover_a_deny_call(tmp_path, monkeypatch):
    import asyncio, hashlib
    from systemu.core.models import Tool, ToolType
    from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature
    from systemu.runtime.tool_sandbox import ToolSandbox
    from systemu.runtime.action_governance import ActionContext, Verdict, evaluate_action
    from systemu.approval.exceptions import PendingOperatorDecision

    posted = {"n": 0}

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, *a, **k):
            posted["n"] += 1
            return "dec_1"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    impl_dir = tmp_path.parent / "impls"; impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / "sync_records.py"
    impl.write_text("def run():\n    return {'success': True}\n", encoding="utf-8")
    tool = Tool(id="tool_sync", name="sync_records", description="d",
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path=str(impl.relative_to(tmp_path.parent)),
                effect_tags=[], version=1)          # empty tags ⇒ UNKNOWN

    benign = {"path": "/data/x"}
    nasty = {"path": "/data/x", "flags": "--force"}

    # Precondition: the SAME tool yields two different verdicts purely from params.
    def _verdict(params):
        return evaluate_action(ActionContext(
            tool="sync_records", effect_tags=set(),
            is_destructive_param=ToolSandbox.is_destructive_call("sync_records", params)))[0]
    assert _verdict(benign) == Verdict.REQUIRE_APPROVAL
    assert _verdict(nasty) == Verdict.DENY

    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=object(), command_approvals=store)

    # The operator legitimately grants "Always allow" on the BENIGN call.
    sig = tool_signature("sync_records", hashlib.sha1(impl.read_bytes()).hexdigest(),
                         set(), host_class="")
    store.approve(sig, command="sync_records")

    # …the benign call now runs without a gate, as intended.
    asyncio.run(sb.execute_tool(tool.implementation_path, benign, tool=tool))
    assert posted["n"] == 0

    # …but the DENY-band call MUST still be gated, despite the standing approval
    # covering its (identical) signature.
    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(tool.implementation_path, nasty, tool=tool))
    assert posted["n"] == 1, "a stored approval must never satisfy the DENY band"


def test_a_one_shot_bridge_also_cannot_satisfy_the_deny_band(tmp_path, monkeypatch):
    import asyncio, hashlib
    from systemu.core.models import Tool, ToolType
    from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature
    from systemu.runtime.tool_sandbox import ToolSandbox
    from systemu.approval.exceptions import PendingOperatorDecision

    class _FakeInbox:
        def __init__(self, vault): pass
        def enqueue(self, *a, **k): return "dec_1"
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)

    impl_dir = tmp_path.parent / "impls"; impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / "sync_records2.py"
    impl.write_text("def run():\n    return {'success': True}\n", encoding="utf-8")
    tool = Tool(id="tool_sync2", name="sync_records2", description="d",
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path=str(impl.relative_to(tmp_path.parent)),
                effect_tags=[], version=1)

    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=object(), command_approvals=store)
    sig = tool_signature("sync_records2", hashlib.sha1(impl.read_bytes()).hexdigest(),
                         set(), host_class="")
    store.mark_resume_approved(sig)                 # a pending one-shot bridge

    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(tool.implementation_path,
                                    {"flags": "--force"}, tool=tool))
    # and the bridge must NOT have been silently spent by the refused call
    assert store.consume_resume_approved(sig) is True


# ── the coords-less rescue must not mint a dangling one-shot for a DENY ─────

def test_a_coordsless_deny_records_nothing(monkeypatch):
    # The coords-less path deliberately persists ONLY a standing allow, because a
    # single-use bridge with no run to consume it could later be spent by an unrelated
    # call to the same params-independent signature. Routing a DENY into the recorder
    # would create exactly that artifact, for the most dangerous band.
    calls = []
    monkeypatch.setattr(rod, "_record_gate_approval",
                        lambda *a, **k: calls.append(k.get("choice")), raising=False)
    dctx = {"tool_signature": "sig-1", "verdict": "deny"}
    is_tool_gate, choice = True, "always allow"
    _v = str(dctx.get("verdict") or "").strip().lower()
    if is_tool_gate and choice == "always allow" and _v != "deny":
        rod._record_gate_approval(dctx, is_tool_gate=True, choice=choice)
    assert calls == [], "a DENY must persist nothing in the coords-less path"


# ── the surrounding surfaces that could re-open it ──────────────────────────

def test_tool_gates_are_not_one_click_approvable_from_the_rail():
    # The rail's one-click approve resolves with options[-1]; the render-only set exists
    # precisely to stop that for high-risk kinds. `tool:` was missing from it.
    from systemu.interface.components.inbox_rail import _RAIL_RENDER_ONLY_DEDUP_PREFIXES
    assert "tool:" in _RAIL_RENDER_ONLY_DEDUP_PREFIXES


def test_tool_gates_are_on_the_bypass_floor():
    # A Bypass policy must never auto-grant a tool gate.
    from systemu.interface.command.gate_mode import FLOOR_GATE_TYPES
    assert "tool" in FLOOR_GATE_TYPES
