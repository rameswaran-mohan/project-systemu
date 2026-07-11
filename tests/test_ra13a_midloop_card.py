"""R-A13a Stage 1 — the mid-loop binder at the TOOL_CALL seam, the bundled scope card on
the WORKING harness_request rail, and the schema_path-aware nested bind-back.

Every test drives the REAL execute() loop with a REAL Tool carrying a REAL nested
parameters_schema — never a synthetic dict / schema-less tool (the blind spot that hid
all 5 R-A12c defects). surface_ask_bundle_requirement (the reverted push rail) must NEVER
be called."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch

from systemu.runtime.shadow_runtime import _apply_nested_answers


# ── unit: the nested bind-back setter (pure) ─────────────────────────────────
def test_apply_nested_answers_sets_nested_by_path():
    tgt = {"recipient": "a@b", "message": {"body": "hi"}}
    _apply_nested_answers(tgt, {"message/subject": "yo"})
    assert tgt == {"recipient": "a@b", "message": {"body": "hi", "subject": "yo"}}


def test_apply_nested_answers_flat_key_unchanged_behavior():
    tgt = {"repo": "x"}
    _apply_nested_answers(tgt, {"branch": "main"})     # no '/' ⇒ flat, as before
    assert tgt == {"repo": "x", "branch": "main"}


def test_apply_nested_answers_creates_intermediate_dicts():
    tgt = {}
    _apply_nested_answers(tgt, {"a/b/c": 1})
    assert tgt == {"a": {"b": {"c": 1}}}


def test_apply_nested_answers_array_segment_falls_back_flat():
    tgt = {}
    _apply_nested_answers(tgt, {"items/[]/id": "x"})   # '[]' not resolvable ⇒ never dropped
    assert tgt == {"items/[]/id": "x"}


# ── integration harness (real Vault + real execute loop) ─────────────────────
_NESTED = {
    "type": "object",
    "properties": {
        "recipient": {"type": "string"},
        "message": {"type": "object",
                    "properties": {"body": {"type": "string"}, "subject": {"type": "string"}},
                    "required": ["body", "subject"]}},
    "required": ["recipient", "message"]}


def _tool(name, schema, *, effect_tags=None):
    from systemu.core.models import Tool, ToolStatus, ToolType
    return Tool(id="tool_" + name, name=name, description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED, enabled=True,
                implementation_path="vault/tools/implementations/%s.py" % name,
                parameters_schema=schema, effect_tags=list(effect_tags or []))


def _build_entities(tmp_path, tool):
    from systemu.vault.vault import Vault
    from systemu.core.models import (Activity, Shadow, ShadowStatus, Scroll, Objective)
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    vault = Vault(str(tmp_path))
    shadow = Shadow(id="shadow_a13a", name="s", description="t", system_prompt="t",
                    status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_a13a", name="s", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="send the message",
                                          success_criteria="sent")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_a13a", name="a", scroll_id=scroll.id,
                        required_tool_ids=[tool.id], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


def _redirect_snapshot_io(monkeypatch, data_dir):
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))


def _run(tmp_path, monkeypatch, *, tool, decisions, handle, with_queue=False):
    """Drive execute() with a scripted llm_call_json emitting `decisions` in order. Returns
    (runtime, result, surface_spy). `handle` replaces _handle_tool_call. `with_queue` wires
    a real operator decision queue so a mid-loop ask posts + parks."""
    from sharing_on.config import Config
    import systemu.runtime.shadow_runtime as _sr
    vault, shadow, activity = _build_entities(tmp_path, tool)
    cfg = Config(); cfg.vault_dir = str(tmp_path); cfg.output_dir = str(tmp_path / "out")
    _redirect_snapshot_io(monkeypatch, tmp_path / "snap")
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "off")

    runtime = _sr.ShadowRuntime(cfg, vault)
    monkeypatch.setattr(runtime, "_handle_tool_call", handle)
    if with_queue:
        from systemu.interface import notifications
        monkeypatch.setattr(notifications, "_vault", vault, raising=False)
        # Force a fresh lazy queue build against THIS vault + auto-restore after (matches
        # the reverted reference harness; prevents cross-test queue contamination).
        monkeypatch.setattr(notifications, "_decision_queue_instance", None, raising=False)

    surface_spy = []
    import systemu.runtime.elicitation as _elic
    if hasattr(_elic, "surface_ask_bundle_requirement"):
        monkeypatch.setattr(_elic, "surface_ask_bundle_requirement",
                            lambda *a, **k: surface_spy.append((a, k)), raising=False)

    _it = iter(decisions)
    def _fake_llm(*a, **k):
        try:
            return next(_it)
        except StopIteration:
            return {"action": "COMPLETE", "summary": "done"}
    monkeypatch.setattr(_sr, "llm_call_json", _fake_llm, raising=False)

    # Hermetic: a fresh loop per test — never rely on a pre-existing current loop
    # (Python 3.12's get_event_loop() RAISES when a prior test [e.g. asyncio.run]
    # left none current — a full-suite test-isolation failure).
    result = asyncio.run(runtime.execute(shadow, activity, dry_run=False))
    return runtime, result, surface_spy, vault


# ── LIVE-CONSUMER AC (the anti-dormancy tripwire — MUST pass or Stage 1 STOPS) ─
def test_live_consumer_nested_gap_surfaces_only_subject(tmp_path, monkeypatch):
    """LLM provides {recipient, message:{body}} for a tool whose message object requires
    [body, subject]. missing_required (top-level) flags nothing (recipient + message
    present). The bundled card must surface EXACTLY 'message/subject' — no false
    'message/body' gap. The card rides the WORKING rail; the reverted push rail is NEVER
    called."""
    tool = _tool("send", _NESTED, effect_tags=["net_mutate"])
    captured = {}
    with patch("systemu.interface.harness_review.surface_harness_request") as _shr:
        def _spy(_req, _verdict, **kw):
            captured["schema"] = _req.spec.get("requested_schema")
            captured["pending"] = _req.spec.get("pending_tool")
            return "did_1"
        _shr.side_effect = _spy
        _, result, surface_spy, _v = _run(
            tmp_path, monkeypatch, tool=tool, with_queue=True,
            decisions=[{"action": "TOOL_CALL", "tool_name": "send",
                        "completes_objective": 1,
                        "parameters": {"recipient": "a@b", "message": {"body": "hi"}}}],
            handle=lambda *a, **k: (_ for _ in ()).throw(AssertionError("tool ran despite gap")))
    props = (captured.get("schema") or {}).get("properties") or {}
    assert list(props.keys()) == ["message/subject"]          # ONLY the nested gap
    assert "message/body" not in props                        # no false over-ask
    assert captured["pending"]["tool_name"] == "send"
    assert not surface_spy                                     # push rail NEVER used
    assert str(result.get("status", "")).startswith("suspended")


# ── AC6: a fully-provided nested call ⇒ no card, tool runs, byte-identical ─────
def test_ac6_fully_provided_no_card_tool_runs(tmp_path, monkeypatch):
    tool = _tool("send", _NESTED, effect_tags=["net_mutate"])
    ran = {"n": 0}
    async def _handle(decision, *a, **k):
        from systemu.runtime.tool_sandbox import ToolResult
        ran["n"] += 1
        return ToolResult(success=True, parsed={"ok": True})
    with patch("systemu.interface.harness_review.surface_harness_request") as _shr:
        _, result, surface_spy, _v = _run(
            tmp_path, monkeypatch, tool=tool, with_queue=True,
            decisions=[{"action": "TOOL_CALL", "tool_name": "send",
                        "completes_objective": 1,
                        "parameters": {"recipient": "a@b",
                                       "message": {"body": "hi", "subject": "yo"}}}],
            handle=_handle)
        assert _shr.call_count == 0        # no card surfaced
    assert ran["n"] == 1                   # the tool actually ran
    assert not surface_spy


# ── stamp OFF: a net_mutate objective is never binder-stamped external ────────
def test_stamp_off_objective_not_flagged(tmp_path, monkeypatch):
    tool = _tool("send", _NESTED, effect_tags=["net_mutate"])
    async def _handle(decision, *a, **k):
        from systemu.runtime.tool_sandbox import ToolResult
        return ToolResult(success=True, parsed={"ok": True})
    with patch("systemu.interface.harness_review.surface_harness_request"):
        runtime, result, _spy, vault = _run(
            tmp_path, monkeypatch, tool=tool, with_queue=True,
            decisions=[{"action": "TOOL_CALL", "tool_name": "send",
                        "completes_objective": 1,
                        "parameters": {"recipient": "a@b",
                                       "message": {"body": "hi", "subject": "yo"}}}],
            handle=_handle)
    sc = vault.get_scroll("scroll_a13a")
    assert sc.objectives[0].requires_external_verification is False


def test_working_rail_roundtrip_nested_bindback(tmp_path, monkeypatch):
    """The bundled card suspends via harness_request (snapshot written), and on resume the
    operator's 'message/subject' answer is set at its NESTED position in
    pending_tool.parameters and re-dispatched — the tool then runs with the full message."""
    tool = _tool("send", _NESTED, effect_tags=["net_mutate"])
    seen = {}
    def _handle(decision, *a, **k):
        from systemu.runtime.tool_sandbox import ToolResult
        seen["params"] = decision.get("parameters")
        return ToolResult(success=True, parsed={"ok": True})

    # Build a grant payload as jobs.py would (kind=input + schema-path-keyed answers).
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._resume_redispatch = None

    async def _drive():
        captured = {}
        async def _redispatch(dec):
            captured["dec"] = dec
        rt._resume_redispatch = _redispatch

        class _Ctx:
            def add_observation(self, *a, **k): pass
        payload = {
            "kind": "input",
            "pending_tool": {"tool_name": "send",
                             "parameters": {"recipient": "a@b", "message": {"body": "hi"}}},
            "param_answers": {"message/subject": "yo"},
        }
        await rt._apply_harness_grant_async(
            payload, context=_Ctx(), tools=[tool], tool_index={}, current_ab=None,
            iter_budget=5)
        return captured

    captured = asyncio.run(_drive())   # hermetic fresh loop (see _run above)
    assert captured["dec"]["parameters"] == {
        "recipient": "a@b", "message": {"body": "hi", "subject": "yo"}}


def test_array_element_gap_skipped_from_card():
    """Adversarial-review LOW fix: an array-element gap (schema_path with '[]') is NOT
    surfaced in the bundled card — _apply_nested_answers can't bind it back yet, so an ask
    the operator's answer can't satisfy must never be posed. A satisfiable sibling gap is
    still surfaced; the array param falls to the top-level missing_required backstop."""
    from systemu.core.models import Requirement
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    ask = [
        Requirement(kind="input", schema_path="items/[]/id", state="missing", source="schema"),
        Requirement(kind="input", schema_path="recipient", state="missing", source="schema"),
    ]
    req = rt._build_bundled_scope_card("send", ask, {"items": [{}]}, "")
    props = (req.spec.get("requested_schema") or {}).get("properties") or {}
    assert "recipient" in props                 # the satisfiable gap IS surfaced
    assert "items/[]/id" not in props           # the array-element gap is skipped

    # a bundle of ONLY array-element gaps yields no card at all (nothing satisfiable)
    only_arr = [Requirement(kind="input", schema_path="items/[]/id",
                            state="missing", source="schema")]
    assert rt._build_bundled_scope_card("send", only_arr, {"items": [{}]}, "") is None
