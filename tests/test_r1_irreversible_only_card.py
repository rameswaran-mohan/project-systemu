"""R1 -- an IRREVERSIBLE-only card (no money) behaves exactly like a money card.

``local_delete`` is classified irreversible (``action_governance.IRREVERSIBLE_EFFECTS``):
the vocabulary carries no recycle-path signal, and the shipped ``file_delete`` body
calls ``Path.unlink()`` -- a permanent delete. So a delete card is HIGH risk, offers
Deny / "Approve once" only, never a standing approval, confirms by the TOOL NAME
(the amount rule is for money), and is refused remotely.

THE WITNESSED GAP: before the classification, a ``local_delete`` tool whose name
and arguments carry no destructive hint (``tidy_folder``) posted a medium-risk card
offering "Always allow", resumed on a bare click, and was ONE-TAP approvable from
the phone (resolution_class "remotely_resolvable").

Real objects throughout, as in ``test_r1_money_gate_typed_confirm``.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from systemu.core.models import Activity, ActivityStatus, Tool, ToolType
from systemu.runtime.command_approvals import CommandApprovalStore

TOOL_NAME = "tidy_folder"
ACT_ID = "act_r1_delete"
SHADOW_ID = "sh_r1_delete"
EXEC_ID = "exec_r1_delete"
PARAMS = {"folder": "reports", "amount": 3}      # an "amount" must NOT become the phrase


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    root = tmp_path / "vault"
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"):
        (root / sub).mkdir(parents=True)
        (root / sub / "index.json").write_text("[]", encoding="utf-8")
    v = Vault(str(root))
    v.save_activity(Activity(id=ACT_ID, name="tidy up", scroll_id="scr_1",
                             required_tool_ids=[f"tool_{TOOL_NAME}"],
                             status=ActivityStatus.ASSIGNED))
    return v


class _Dispatcher:
    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append(activity_id)
        return "sub"


class _Backend:
    def __init__(self):
        self.calls = []

    async def execute(self, impl_path, params_json, timeout=None, extra_packages=None):
        from systemu.runtime.tool_sandbox import ToolResult
        self.calls.append(json.loads(params_json))
        return ToolResult(success=True, parsed={"success": True})


@pytest.fixture
def rail(monkeypatch, tmp_path, vault):
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    from systemu.runtime.tool_sandbox import ToolSandbox

    rod._handled.clear()
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    dispatcher, backend = _Dispatcher(), _Backend()
    unsub = rod.register(vault, dispatcher, data_dir=str(tmp_path / "data"))
    sb = ToolSandbox(str(vault.root), vault=vault, command_approvals=store)
    sb._backend = backend
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir()
    impl = impl_dir / f"{TOOL_NAME}.py"
    impl.write_text("def run(**kw):\n    return {'success': True}\n", encoding="utf-8")
    tool = Tool(id=f"tool_{TOOL_NAME}", name=TOOL_NAME, description="tidy a folder",
                tool_type=ToolType.PYTHON_FUNCTION, implementation_path=str(impl),
                effect_tags=["local_delete"], version=1)
    try:
        yield {"store": store, "dispatcher": dispatcher, "backend": backend,
               "sb": sb, "tool": tool, "impl": impl}
    finally:
        unsub()
        rod._handled.clear()


def _park(rail):
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import chat_submission_ctx as ctx
    pairs = ((ctx.set_activity_id, ACT_ID), (ctx.set_shadow_id, SHADOW_ID),
             (ctx.set_execution_id, EXEC_ID), (ctx.set_chat_submission_id, None))
    tokens = [(setter, setter(value)) for setter, value in pairs]
    try:
        with pytest.raises(PendingOperatorDecision) as excinfo:
            asyncio.run(rail["sb"].execute_tool(str(rail["impl"]), dict(PARAMS),
                                                tool=rail["tool"]))
    finally:
        for setter, token in tokens:
            setter(None, reset_token=token)
    return excinfo.value


def test_an_irreversible_only_card_is_deny_and_approve_once_at_high_risk(rail, vault):
    pend = _park(rail)
    dec = vault.get_decision(pend.decision_id)
    assert dec.options == ["Deny", "Approve once"]
    ctx = dec.context
    assert ctx["risk"] == "high"
    assert ctx["destructive"] is False, "must not be carded by some other axis"
    assert ctx["irreversible_effects"] == ["local_delete"]
    assert ctx["requires_typed_confirm"] is True
    assert ctx["confirm_phrase"] == TOOL_NAME          # not the "amount" argument
    what = ctx["what_approve_does"]
    assert "cannot be undone" in what and "cannot be remembered" in what
    assert "moves money" not in what
    what.encode("ascii")


def test_a_bare_click_resolves_nothing_and_the_phrase_resumes_once(rail, vault):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import resolve_pending_gate

    pend = _park(rail)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]

    OperatorDecisionQueue(vault).resolve(pend.decision_id, choice="Approve once")
    assert vault.get_decision(pend.decision_id).status == "pending"   # re-opened
    assert rail["dispatcher"].submits == []

    res = resolve_pending_gate(pend.decision_id, choice="Approve once", vault=vault,
                               typed_confirm="tidy_folder ")
    assert res.status.value == "ok", res.summary
    assert rail["dispatcher"].submits == [ACT_ID]
    assert rail["store"].is_approved(sig) is False

    out = asyncio.run(rail["sb"].execute_tool(str(rail["impl"]), dict(PARAMS),
                                              tool=rail["tool"]))
    assert out.success is True and rail["backend"].calls == [PARAMS]


def test_the_store_refuses_a_standing_approval_for_it(rail, vault):
    pend = _park(rail)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]
    assert rail["store"].approve(sig) is False
    assert rail["store"].is_approved(sig) is False


def test_telegram_refuses_the_irreversible_only_class_too(rail, vault, monkeypatch):
    """THE PHONE HALF of the gap: a benign-named local_delete card used to be
    stamped remotely_resolvable. ``decision_bridge`` is unchanged -- the card now
    carries ``requires_typed_confirm``, which its floor already honours."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.messaging.decision_bridge import decision_tag, resolve_from_channel

    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "4242")
    pend = _park(rail)
    assert vault.get_decision(pend.decision_id).context["resolution_class"] == "floor"
    for key in ("a1", "a2"):
        outcome, _ = resolve_from_channel(decision_tag(pend.decision_id), key,
                                          sender_id="4242", channel="telegram",
                                          queue=OperatorDecisionQueue(vault))
        assert outcome == "REFUSED_TYPED_CONFIRM"
    assert vault.get_decision(pend.decision_id).status == "pending"


def test_bulk_first_gate_review_excludes_it():
    from systemu.runtime.first_gate_review import build_entry, partition_entries
    entry = build_entry(tool_id="t1", name=TOOL_NAME, effect_tags=["local_delete"],
                        signature="sig::delete")
    part = partition_entries([entry])
    assert part.eligible == () and len(part.excluded) == 1


def test_the_floor_set_and_the_irreversible_set_agree():
    """Every class the phone floors as money/irreversible is irreversible here, so
    the two surfaces can never disagree about a card."""
    from systemu.messaging.decision_bridge import _FLOOR_EFFECT_TAGS
    from systemu.runtime.action_governance import IRREVERSIBLE_EFFECTS
    assert set(_FLOOR_EFFECT_TAGS) <= set(IRREVERSIBLE_EFFECTS)
