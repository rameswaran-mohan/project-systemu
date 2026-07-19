"""The QUICK LANE's action-gate wiring, and the IMPL-5 taint residual it bounds.

WHY THIS FILE EXISTS. ``quick_task`` has been the DEFAULT lane since v0.9.18 and it
never calls ``requirement_binder`` — so the IMPL-5 taint gate (a ``content_derived``
value never silently binds; it forces a one-click operator confirm) does not run there
at all. That residual is already recorded in ``requirement_binder`` beside the
prompt-channel clamp.

What was NOT recorded is the control that BOUNDS it. The quick lane is not ungoverned:
``_execute_tool`` threads ``tool=`` into ``ToolSandbox.execute_tool``, which runs
``_maybe_gate_tool`` — the effect-class action gate. That gate confirm-cards every
``action_governance._APPROVAL_TAGS`` effect (net_mutate / send_message / money_move /
oauth_call / local_delete / shell_exec) plus the UNKNOWN empty-tags floor, and its card
carries an ``args_preview`` of the ACTUAL parameter values. So a tainted value can never
reach a DANGEROUS effect unattended in this lane; the residual is confined to the ALLOW
band (net_read / local_read), where the unattended action is a READ.

That whole bound rests on ONE keyword argument, and it had no dedicated pin. Deleting
``tool=tool`` from ``_execute_tool`` makes the default lane execute money_move /
send_message / oauth_call / net_mutate / local_delete / shell_exec / UNKNOWN tools
completely unattended — measured, not assumed — and every one of the ~132 quick-lane
tests still passed. The single test that failed
(``test_impl2_resolved_dedup_scope``'s stale-resolved-row reproduction) catches it only
INCIDENTALLY: it drives ``_execute_tool`` as a vehicle for an unrelated IMPL-2
invariant, so a legitimate retarget of that test would silently remove the only guard on
the default lane's action gate.

These tests close that, in BOTH directions — the dangerous bands card, AND the read band
still resolves silently, so a future "gate everything" change that destroys the lane's
reason to exist also fails here.

The IMPL-5 read-band residual itself is pinned as a CHARACTERIZATION test, not an
aspiration: it asserts today's real behaviour so that closing it is a deliberate,
visible edit rather than silent drift.

NOTE: this module is behavioural throughout and deliberately reads no module source, so
it stays in the edit-safe (non ``source_sensitive``) gate tier where the mutation pin
above is actually run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType, UserProfile
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

# The tainted value, carried inside a content_derived profile fact.
TAINTED = "acct-99887-xray"

_BODY = ("TOOL_META = {'name': 'do_thing'}\n"
         "def run(**kwargs):\n"
         "    return {'success': True, 'data': {'v': kwargs.get('account_id')},"
         " 'error': None}\n")


def _mk_vault(root: Path, *, tainted_fact: bool = True) -> Vault:
    for sub in ["tools/implementations", "elder", "notifications"]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "tools" / "index.json").write_text("[]", encoding="utf-8")
    vault = Vault(str(root))
    vault.save_user_profile(UserProfile(
        name="Op", location_text="Indiranagar, Bangalore",
        timezone="Asia/Kolkata", default_output_dir=str(root / "output")))
    if tainted_fact:
        # written EXACTLY as fact_extractor.extract_from_chat writes it: the LLM
        # picks the tags, and `office_context` is what user_context reads back.
        vault.append_user_fact(
            fact=f"User's billing account is {TAINTED}",
            source="auto_extract", tags=["office_context"],
            source_ref="chat:2026-07-19T10:00:00", confidence=0.95,
            origin_class="content_derived")
    return vault


def _mk_tool(vault: Vault, effect_tags) -> Tool:
    impl = Path(vault.root) / "tools" / "implementations" / "do_thing.py"
    impl.write_text(_BODY, encoding="utf-8")
    tool = Tool(
        id=generate_id("tool"), name="do_thing", description="t",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True, implementation_path=str(impl), forged_by_systemu=False,
        parameter_names=["account_id"],
        parameters_schema={"type": "object",
                           "properties": {"account_id": {"type": "string"}},
                           "required": ["account_id"]},
        effect_tags=list(effect_tags))
    vault.save_tool(tool)
    return tool


def _run(vault: Vault, monkeypatch, *, value: str = TAINTED):
    """Run one quick-lane iteration that calls ``do_thing`` with ``value``.

    Returns ``(executed_params, result)``. ``executed_params`` is recorded only AFTER
    the real sandbox returns, so it is non-empty ONLY when the real gates let the tool
    body actually run.
    """
    from systemu.pipelines import quick_task as qt
    from systemu.runtime.tool_sandbox import ToolSandbox

    # An unresolved gate card would otherwise block-poll for 300s; None selects the
    # fail-closed denial branch, which is the outcome under test.
    monkeypatch.setattr(qt, "_poll_command_choice",
                        lambda v, k, timeout=None: None)

    executed = []

    class _Recording:
        def __init__(self, inner):
            self._inner, self._vault = inner, vault

        def __getattr__(self, n):
            return getattr(self._inner, n)

        async def execute_tool(self, impl_path, params, **kw):
            out = await self._inner.execute_tool(impl_path, params, **kw)
            executed.append(dict(params))          # only on real execution
            return out

    def _llm(*, system, user, config):
        if json.loads(user)["iteration"] == 1:
            return {"action": "TOOL_CALL", "tool": "do_thing",
                    "params": {"account_id": value}, "reasoning": "r"}
        return {"action": "ANSWER", "answer_md": "done", "completed": True}

    result = qt.run_quick_task(
        "do it", None, vault, llm_json=_llm,
        sandbox=_Recording(ToolSandbox(str(vault.root), vault=vault, config=None)),
        max_iters=3)
    return executed, result


# ── direction 1: the dangerous bands MUST card in the default lane ───────────
@pytest.mark.parametrize("tags", [
    [],                       # UNKNOWN-until-classified floor (DEC-24)
    ["net_mutate"], ["send_message"], ["money_move"],
    ["oauth_call"], ["local_delete"], ["shell_exec"],
])
def test_approval_band_never_runs_unattended_in_quick_lane(tmp_path, monkeypatch, tags):
    """THE MUTATION PIN. Every ``_APPROVAL_TAGS`` effect plus the UNKNOWN floor must
    reach a confirm card in the DEFAULT lane, never the tool body.

    Dropping ``tool=`` from ``quick_task._execute_tool`` disarms all of these at once.
    """
    vault = _mk_vault(tmp_path)
    _mk_tool(vault, tags)
    executed, _ = _run(vault, monkeypatch)
    assert executed == [], (
        f"effect_tags={tags!r} executed UNATTENDED in the quick lane — the sandbox "
        f"action gate is not wired (is `tool=` still threaded in _execute_tool?)")


# ── direction 2: the read band MUST still resolve silently ───────────────────
@pytest.mark.parametrize("tags", [["net_read"], ["local_read"]])
def test_read_band_still_resolves_silently(tmp_path, monkeypatch, tags):
    """The quick lane exists to be FAST. An ALLOW-band read must run with no card — a
    "gate everything" change looks safe while destroying the lane's purpose.
    """
    vault = _mk_vault(tmp_path)
    _mk_tool(vault, tags)
    executed, result = _run(vault, monkeypatch)
    assert executed == [{"account_id": TAINTED}], (
        f"effect_tags={tags!r} was carded — the quick lane's read band must stay "
        f"frictionless")
    assert result.status == "success"


# ── the IMPL-5 residual this bound leaves open (characterization) ────────────
def test_quick_lane_does_not_invoke_the_requirement_binder(tmp_path, monkeypatch):
    """CHARACTERIZATION. The quick lane never calls the binder, so no IMPL-5 taint gate
    runs there. Behavioural rather than source-scanning: the binder's aggregate entry
    point is replaced with a recorder and must never fire.
    """
    import systemu.runtime.requirement_binder as rb
    calls = []
    monkeypatch.setattr(rb, "build_requirement_report", lambda *a, **k: calls.append(1))
    vault = _mk_vault(tmp_path)
    _mk_tool(vault, ["net_read"])
    _run(vault, monkeypatch)
    assert calls == [], "the quick lane now calls the binder — update this residual"


def test_tainted_fact_confirm_gates_on_the_full_lane(tmp_path):
    """The other half of the asymmetry: the SAME value the quick lane acts on
    unattended is forced into the ask_bundle by ``_needs_ask`` on the full lane.
    """
    from systemu.runtime.requirement_binder import (_needs_ask,
                                                    build_requirement_report)
    from systemu.runtime.user_profile import get_facts

    vault = _mk_vault(tmp_path)
    cap = _mk_tool(vault, ["net_read"])
    situation = {"profile": {"name": "Op",
                             "user_facts": [f.model_dump()
                                            for f in get_facts(vault)]}}

    class _Obj:
        id = 1
        description = "look up the billing account"
        reference_text = "billing account"
        success_criteria = []
        requires_external_verification = False

    class _Ctx:
        files_produced = []

    report = build_requirement_report([_Obj()], cap, situation, _Ctx(),
                                      provided_params={"account_id": TAINTED},
                                      vault=vault)
    ask = list(getattr(report, "ask_bundle", []) or [])
    assert ask, "the full lane must confirm-gate the tainted value"
    assert any(_needs_ask(r) and r.value_origin == "content_derived" for r in ask)


def test_read_band_taint_residual_is_real(tmp_path, monkeypatch):
    """CHARACTERIZATION of the OPEN residual: on the read band the quick lane acts on
    the tainted value with NO confirm. Paired with the full-lane test above this is the
    executed asymmetry. Closing the residual should FAIL here — deliberately, so the
    change is visible.
    """
    vault = _mk_vault(tmp_path)
    _mk_tool(vault, ["net_read"])
    executed, _ = _run(vault, monkeypatch)
    assert executed == [{"account_id": TAINTED}]


def test_tainted_value_is_injected_into_the_quick_lane_prompt(tmp_path):
    """The carrier: ``user_context.profile_context_block`` renders a content_derived
    user_fact verbatim into the quick lane's system prompt, with no origin filter.
    """
    from systemu.runtime.user_context import profile_context_block
    vault = _mk_vault(tmp_path)
    assert TAINTED in profile_context_block(vault)
    clean = _mk_vault(tmp_path / "clean", tainted_fact=False)
    assert TAINTED not in profile_context_block(clean)
