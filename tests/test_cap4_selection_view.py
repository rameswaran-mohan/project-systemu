"""R-CAP1 · CAP-4 two-stage selection view + registered ``find_tools`` + CAP-3
trust ordering -- the witnesses.

Ruled R9 (binding):
  (a) AC8 is SET identity + determinism: with <= K tools enabled every enabled
      tool is a full record, none demoted; order = the CAP-4 key; shuffled
      storage order -> identical bytes.
  (b) never-subtract: ``find_tools`` searches the COMPLETE store and every hit
      carries its state; dispatch eligibility is UNCHANGED -- calling a returned
      disabled tool is refused.
Plus AC1 (60+ tools: <= K full + index; a task whose right tool is OUTSIDE the
top-K succeeds via ``find_tools``, driven by a scripted LLM), DEC-27 (an
unreadable roster is a rendered, distinct state), DEC-34 (verified usage has no
production writer, so nothing ranks by it) and DEC-43 (one mint, every builder).
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.runtime import capability_view as cv
from systemu.vault.vault import Vault

K = cv.K_DEFAULT


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

def _vault(root: Path) -> Vault:
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "elder"]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (root / idx / "index.json").write_text("[]", encoding="utf-8")
    (root / "global_memory.jsonl").write_text("", encoding="utf-8")
    return Vault(str(root))


_ECHO_BODY = (
    "TOOL_META = {'name': 'echo_tool'}\n"
    "def run(**kwargs):\n"
    "    return {'success': True, 'data': {'echo': kwargs.get('x')}, 'error': None}\n"
)


def _add(vault: Vault, name: str, *, tid: str = "", enabled: bool = True,
         status: ToolStatus = ToolStatus.DEPLOYED, forged: bool = False,
         dry_run_status: str = "not_run", body: str = _ECHO_BODY,
         description: str = "", extra: dict | None = None) -> Tool:
    """A tool with a real implementation file. Effect tags are derived the way the
    forge pipeline derives them (``classify_source``) so the sandbox gate sees a
    realistically classified tool."""
    from systemu.runtime.effect_tags import classify_source
    impl = Path(vault.root) / "tools" / "implementations" / f"{name}.py"
    impl.write_text(body, encoding="utf-8")
    data = dict(
        id=tid or f"tool_{name}", name=name,
        description=description or f"test tool {name}",
        tool_type=ToolType.PYTHON_FUNCTION, status=status, enabled=enabled,
        implementation_path=str(impl), forged_by_systemu=forged,
        parameter_names=["x"],
        parameters_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        effect_tags=sorted(t.value for t in classify_source(body)),
        dry_run_status=dry_run_status,
    )
    data.update(extra or {})
    tool = Tool.model_validate(data)
    vault.save_tool(tool)
    return tool


def _fake_llm(script):
    calls = {"payloads": [], "i": 0}

    def llm_json(*, system, user, config=None):
        calls["payloads"].append(user)
        step = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return step(json.loads(user)) if callable(step) else step

    llm_json.calls = calls
    return llm_json


def _names(records):
    return [r["name"] for r in records]


def _store_names(full_records):
    return {n for n in _names(full_records) if n not in cv.PINNED_AFFORDANCES}


# --------------------------------------------------------------------------- #
# R9(a) -- the floor: <= K enabled => every enabled tool in full, CAP-4 order
# --------------------------------------------------------------------------- #

def test_r9a_at_or_below_k_every_enabled_tool_is_a_full_record(tmp_path):
    from systemu.pipelines.quick_task import run_quick_task
    v = _vault(tmp_path / "v")
    enabled = {f"tool_{i:02d}_alpha" for i in range(K)}
    for n in sorted(enabled):
        _add(v, n)
    _add(v, "never_enabled_beta", enabled=False)        # not part of the enabled set
    llm = _fake_llm([{"action": "ANSWER", "answer_md": "ok"}])
    run_quick_task("alpha things", None, v, llm_json=llm)
    payload = json.loads(llm.calls["payloads"][0])
    # set identity: the full-record set IS the enabled set (+ the lookup affordance)
    assert _store_names(payload["tools"]) == enabled
    assert cv.FIND_TOOLS_NAME in _names(payload["tools"])
    # none demoted: no index at all
    assert cv.INDEX_KEY not in payload and cv.INDEX_NOTE_KEY not in payload
    # order = the CAP-4 key (tool_id terminal tiebreak on an all-equal query)
    store_order = [n for n in _names(payload["tools"]) if n not in cv.PINNED_AFFORDANCES]
    assert store_order == sorted(enabled, key=lambda n: f"tool_{n}")


def _quick_payload(vault, prompt):
    from systemu.pipelines.quick_task import run_quick_task
    llm = _fake_llm([{"action": "ANSWER", "answer_md": "ok"}])
    run_quick_task(prompt, None, vault, llm_json=llm)
    payload = json.loads(llm.calls["payloads"][0])
    return json.dumps({k: payload.get(k) for k in ("tools", cv.INDEX_KEY)},
                      sort_keys=False)


@pytest.mark.parametrize("n_tools", [K - 3, K + 9])
def test_r9a_shuffled_storage_order_gives_identical_bytes(tmp_path, n_tools):
    names = [f"widget_{i:02d}_{'x' * (i % 3)}" for i in range(n_tools)]
    rng = random.Random(1234)
    orders = [list(names), list(reversed(names))]
    shuffled = list(names)
    rng.shuffle(shuffled)
    orders.append(shuffled)
    rendered = []
    for i, order in enumerate(orders):
        v = _vault(tmp_path / f"v{i}")
        for n in order:
            _add(v, n)
        rendered.append(_quick_payload(v, "read the widget list"))
    assert rendered[0] == rendered[1] == rendered[2]


def test_r9a_pure_view_is_storage_order_independent():
    recs = [{"name": f"tool_{c}", "description": "same"} for c in "qwertyuiopasdfghjk"]
    traits = {r["name"]: cv.ToolTraits(tool_id=f"id_{r['name']}") for r in recs}
    outs = set()
    for seed in range(5):
        shuffled = list(recs)
        random.Random(seed).shuffle(shuffled)
        view = cv.build_view(shuffled, "anything", catalog=cv.NO_ROSTER_READ, traits=traits)
        outs.add(json.dumps(view.payload_fields("tools", lookup=True)))
    assert len(outs) == 1


# --------------------------------------------------------------------------- #
# AC1 -- 60+ enabled tools: <= K full + index; the right tool OUTSIDE top-K is
# reached through find_tools (scripted LLM) and the task succeeds
# --------------------------------------------------------------------------- #

def test_ac1_sixty_plus_tools_render_k_full_plus_one_line_index(tmp_path):
    v = _vault(tmp_path / "v")
    names = {f"filler_{i:02d}_tool" for i in range(63)} | {"count_stock_items"}
    for n in names:
        _add(v, n, tid=("tool_zzz_target" if n == "count_stock_items" else f"tool_a_{n}"))
    from systemu.pipelines.quick_task import run_quick_task
    llm = _fake_llm([{"action": "ANSWER", "answer_md": "ok"}])
    run_quick_task("tally the widget inventory", None, v, llm_json=llm)
    payload = json.loads(llm.calls["payloads"][0])
    full = _store_names(payload["tools"])
    assert len(full) == K
    index_names = {line.split(" (")[0] for line in payload[cv.INDEX_KEY]}
    assert len(index_names) == len(names) - K
    assert full | index_names == names and not (full & index_names)   # never-subtract
    assert "count_stock_items" in index_names                       # outside top-K
    assert "find_tools" in payload[cv.INDEX_NOTE_KEY]


def test_ac1_task_whose_tool_is_outside_top_k_succeeds_via_find_tools(tmp_path):
    from systemu.pipelines.quick_task import run_quick_task
    v = _vault(tmp_path / "v")
    for i in range(63):
        _add(v, f"filler_{i:02d}_tool", tid=f"tool_a_{i:02d}")
    _add(v, "count_stock_items", tid="tool_zzz_target",
         description="count how many items of stock exist")
    seen = {}

    def turn1(payload):
        seen["top"] = _names(payload["tools"])
        seen["index"] = payload.get(cv.INDEX_KEY, [])
        return {"action": "TOOL_CALL", "tool": "find_tools",
                "params": {"query": "count stock items"}, "reasoning": "look it up"}

    def turn2(payload):
        lookups = [h for h in payload["history"] if h.get("role") == "tool_lookup"]
        seen["lookup"] = lookups[-1]["result"]
        return {"action": "TOOL_CALL", "tool": "count_stock_items",
                "params": {"x": "widgets"}, "reasoning": "found it"}

    llm = _fake_llm([turn1, turn2, {"action": "ANSWER", "answer_md": "counted"}])
    res = run_quick_task("tally the widget inventory", None, v, llm_json=llm)

    assert "count_stock_items" not in seen["top"]                       # outside top-K
    assert any(line.startswith("count_stock_items") for line in seen["index"])
    assert "count_stock_items | state: enabled | callable here: yes" in seen["lookup"]
    assert '"x"' in seen["lookup"]                                       # full record
    assert res.status == "success", res.error
    assert res.tool_calls == 1                       # find_tools is not a tool call
    assert "widgets" in llm.calls["payloads"][2]     # the real tool really ran


# --------------------------------------------------------------------------- #
# R9(b) -- never-subtract lookup over the COMPLETE store, with state
# --------------------------------------------------------------------------- #

def _state_vault(root: Path) -> Vault:
    from systemu.runtime.mcp import connections as cx
    v = _vault(root)
    _add(v, "alpha_enabled")
    _add(v, "beta_disabled", enabled=False)
    _add(v, "gamma_broken", enabled=False, dry_run_status="failed")
    _add(v, "delta_proposed", enabled=False, status=ToolStatus.PROPOSED)
    _add(v, "epsilon_forged_enabled", status=ToolStatus.FORGED)
    _add(v, "zeta_quarantined", extra={
        "requires_credentials": [{"key": "PYTHONPATH", "label": "path"}]})
    cx.set_tool_enabled(v, "http://stub", "eta_connector", True,
                        description="a connector tool", schema={})
    return v


_EXPECTED_STATES = {
    "alpha_enabled": cv.STATE_ENABLED,
    "beta_disabled": cv.STATE_DISABLED,
    "gamma_broken": cv.STATE_BROKEN,
    "delta_proposed": cv.STATE_NOT_READY,
    "epsilon_forged_enabled": cv.STATE_NOT_READY,
    "zeta_quarantined": cv.STATE_QUARANTINED,
    "eta_connector": cv.STATE_ENABLED,
}


def test_r9b_every_stored_tool_is_returned_with_its_correct_state(tmp_path):
    v = _state_vault(tmp_path / "v")
    assert v.get_tool("tool_zeta_quarantined").credential_refusals   # precondition
    for name, state in _EXPECTED_STATES.items():
        res = cv.lookup(v, name, limit=3, lane_extras=())
        hit = res.hits[0]
        assert hit.name == name, (name, [h.name for h in res.hits])
        assert hit.state == state, (name, hit.state, hit.reason)
        assert hit.reason
        if state != cv.STATE_ENABLED:
            assert "do not forge" in hit.next_step.lower()
            assert "operator" in hit.next_step.lower()


def test_r9b_lookup_never_filters_by_state(tmp_path):
    v = _state_vault(tmp_path / "v")
    res = cv.lookup(v, "zzz unrelated words", limit=cv.FIND_TOOLS_MAX_LIMIT, lane_extras=())
    assert {h.name for h in res.hits} == set(_EXPECTED_STATES)
    assert res.total_in_store == len(_EXPECTED_STATES)


def test_r9b_quarantined_enable_path_stays_operator_review(tmp_path):
    v = _state_vault(tmp_path / "v")
    hit = cv.lookup(v, "zeta_quarantined", limit=1, lane_extras=()).hits[0]
    assert hit.state == cv.STATE_QUARANTINED
    assert "review" in hit.next_step and "Tools page" in hit.next_step
    assert hit.parameters is None                  # not handed out as callable


def test_r9b_quick_lane_refuses_a_returned_disabled_tool(tmp_path):
    from systemu.pipelines.quick_task import run_quick_task
    v = _state_vault(tmp_path / "v")
    seen = {}

    def turn1(payload):
        assert "beta_disabled" not in json.dumps(payload["tools"])     # never shown
        return {"action": "TOOL_CALL", "tool": "find_tools",
                "params": {"query": "beta_disabled"}}

    def turn2(payload):
        seen["lookup"] = [h for h in payload["history"]
                          if h.get("role") == "tool_lookup"][-1]["result"]
        return {"action": "TOOL_CALL", "tool": "beta_disabled", "params": {"x": "1"}}

    def turn3(payload):
        seen["refusal"] = payload["history"][-1]
        return {"action": "ANSWER", "answer_md": "asked", "completed": False}

    llm = _fake_llm([turn1, turn2, turn3])
    res = run_quick_task("use beta", None, v, llm_json=llm)
    assert "beta_disabled | state: disabled | callable here: no" in seen["lookup"]
    assert res.tool_calls == 0, "a disabled hit must never execute"
    err = seen["refusal"]["error"]
    assert "unknown or disabled" in err
    assert "state: disabled" in err and "do NOT forge" in err


def test_r9b_runtime_lane_lookup_reports_lane_callability_and_refuses(tmp_path):
    import asyncio
    from systemu.runtime.shadow_runtime import ShadowRuntime

    class _Ctx:
        def __init__(self):
            self.observations, self.tool_calls = [], []

        def add_observation(self, obs, ab):
            self.observations.append(obs)

        def add_tool_call(self, d, ab):
            self.tool_calls.append(d)

    v = _state_vault(tmp_path / "v")
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.config, rt.vault, rt._subagent_depth = None, v, 0
    rt._same_tool_fail_streak = {}
    loaded = [v.get_tool("tool_alpha_enabled")]
    ctx = _Ctx()
    res = asyncio.run(rt._handle_tool_call(
        {"tool_name": "find_tools", "parameters": {"query": "alpha_enabled"}},
        loaded, ctx, current_ab=0, dry_run=False))
    assert res.success and "callable here: yes" in res.parsed["matches"]
    res2 = asyncio.run(rt._handle_tool_call(
        {"tool_name": "find_tools", "parameters": {"query": "beta_disabled"}},
        loaded, ctx, current_ab=0, dry_run=False))
    assert "beta_disabled | state: disabled | callable here: no" in res2.parsed["matches"]
    # dispatch unchanged: the disabled hit is refused, nothing executes
    ctx2 = _Ctx()
    out = asyncio.run(rt._handle_tool_call(
        {"tool_name": "beta_disabled", "parameters": {"x": "1"}},
        loaded, ctx2, current_ab=0, dry_run=False))
    assert out is None
    assert "not found in available tools" in ctx2.observations[-1]["error"]


def test_find_tools_in_a_run_never_costs_an_operator_ask(tmp_path):
    """A missing query is answered by the lookup itself -- never routed through the
    missing-required seam (which would raise an operator INPUT request)."""
    import asyncio
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.config, rt.vault, rt._subagent_depth = None, _vault(tmp_path / "v"), 0
    rt._same_tool_fail_streak = {}

    class _Ctx:
        def add_observation(self, o, ab): pass
        def add_tool_call(self, d, ab): pass

    res = asyncio.run(rt._handle_tool_call(
        {"tool_name": "find_tools", "parameters": {}}, [], _Ctx(),
        current_ab=0, dry_run=False))
    assert res.success is False
    assert res.parsed.get("error_type") == "find_tools_needs_query"
    assert not res.parsed.get("__needs_input__")


def test_find_tools_is_a_registered_v2_tool_on_the_runtime_surface(tmp_path, monkeypatch):
    from systemu.runtime.shadow_runtime import _build_llm_tool_catalog
    from systemu.runtime.tool_registry_v2 import registry
    from systemu.runtime.tools import capability_lookup_tools as clt
    registry.discover_modules("systemu.runtime.tools")
    entry = registry.get("find_tools")
    assert entry is not None and entry.is_action_tool is False
    assert "query" not in (entry.schema.get("required") or [])
    assert "find_tools" in {e["name"] for e in _build_llm_tool_catalog(vault=None)}
    v = _state_vault(tmp_path / "v")
    monkeypatch.setattr(clt, "_open_vault", lambda: v)
    out = clt.find_tools_handler(query="alpha_enabled", _root="x")
    assert out["success"] and "alpha_enabled | state: enabled" in out["matches"]


# --------------------------------------------------------------------------- #
# runtime decision loop: the view reaches the executor prompt end-to-end
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_runtime_decision_prompt_carries_the_view_and_find_tools_answers(tmp_path):
    from unittest.mock import patch
    from sharing_on.config import Config
    from systemu.core.models import Activity, Objective, Scroll, Shadow, ShadowStatus
    from systemu.runtime.shadow_runtime import ShadowRuntime

    v = _vault(tmp_path / "v")
    cfg = Config()
    cfg.vault_dir = str(tmp_path / "v")
    cfg.output_dir = str(tmp_path / "out")
    shadow = Shadow(id="sh1", name="T", description="d", system_prompt="p",
                    status=ShadowStatus.AWAKENED)
    v.save_shadow(shadow)
    ids = []
    for i in range(K + 4):
        t = _add(v, f"runner_{i:02d}_job", tid=f"tool_r_{i:02d}")
        ids.append(t.id)
    scroll = Scroll(id="sc1", name="S", source_session_id="x",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="do it", success_criteria="done")])
    v.save_scroll(scroll)
    activity = Activity(id="a1", name="A", scroll_id=scroll.id, required_tool_ids=ids,
                        required_skill_ids=[], assigned_shadow_id=shadow.id)
    v.save_activity(activity)

    payloads = []
    script = [{"action": "TOOL_CALL", "tool_name": "find_tools",
               "parameters": {"query": "runner_15_job"}, "reasoning": "look"},
              {"action": "FAIL", "reason": "done testing"}]

    def fake_llm(**kw):
        if kw.get("stage") != "executor":
            return {}
        payloads.append(json.loads(kw["user"]))
        return script[min(len(payloads) - 1, len(script) - 1)]

    # Every OTHER model call (episodic summary, verifiers) fails fast, offline.
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=fake_llm), \
         patch("systemu.core.llm_router.llm_call", side_effect=RuntimeError("offline")), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        await ShadowRuntime(cfg, v).execute(shadow, activity)

    first = payloads[0]
    shown = _names(first["available_tools"])
    ranked = [n for n in shown if n not in cv.PINNED_AFFORDANCES]
    assert len(ranked) == K                        # top-K full
    assert "find_tools" in shown                   # the pinned lookup affordance
    assert first[cv.INDEX_KEY]                     # the one-line index rides along
    every = set(ranked) | {l.split(" (")[0] for l in first[cv.INDEX_KEY]}
    assert {f"runner_{i:02d}_job" for i in range(K + 4)} <= every    # never-subtract
    second = json.dumps(payloads[1]["history"])
    assert "runner_15_job | state: enabled | callable here: yes" in second


# --------------------------------------------------------------------------- #
# CAP-3 -- effectful-slot eligibility gate (verified usage absent -> origin trust)
# --------------------------------------------------------------------------- #

def _rows(n_builtin=K + 2):
    recs = [{"name": f"builtin_{i:02d}_helper"} for i in range(n_builtin)]
    traits = {r["name"]: cv.ToolTraits(tool_id=f"b{i:02d}", origin="builtin")
              for i, r in enumerate(recs)}
    return recs, traits


@pytest.mark.parametrize("origin", ["mcp:https://evil", "forged"])
def test_cap3_stuffed_untrusted_name_cannot_enter_an_effectful_top_k(origin):
    recs, traits = _rows()
    recs.append({"name": "send_email_now_send_email"})
    traits["send_email_now_send_email"] = cv.ToolTraits(tool_id="x", origin=origin)
    view = cv.build_view(recs, "send an email to bob", catalog=cv.NO_ROSTER_READ,
                         traits=traits)
    assert "send_email_now_send_email" not in _names(view.full_records())
    assert any(l.startswith("send_email_now_send_email") for l in view.index_lines())


def test_cap3_independent_verified_signal_admits_it():
    recs, traits = _rows()
    recs.append({"name": "send_email"})
    traits["send_email"] = cv.ToolTraits(tool_id="x", origin="mcp:https://gh",
                                         verified_done=1)
    view = cv.build_view(recs, "send an email to bob", catalog=cv.NO_ROSTER_READ,
                         traits=traits)
    assert _names(view.full_records())[0] == "send_email"


def test_cap3_gate_is_for_effectful_slots_only():
    recs, traits = _rows()
    recs.append({"name": "read_inbox"})
    traits["read_inbox"] = cv.ToolTraits(tool_id="x", origin="mcp:https://gh")
    view = cv.build_view(recs, "read my inbox", catalog=cv.NO_ROSTER_READ, traits=traits)
    assert _names(view.full_records())[0] == "read_inbox"


def test_cap3_an_ambiguous_noun_verb_errs_effectful():
    # "email" is also a verb ("to email"): the profile reads it as effectful, which
    # only ever TIGHTENS the gate -- the safe direction.
    assert cv.profile_query("read my email").effectful is True
    assert cv.profile_query("read my inbox").effectful is False


def test_cap3_description_is_never_a_ranking_signal():
    recs = [{"name": "zzz_unrelated", "description": "send email send email send email"},
            {"name": "send_email", "description": ""}]
    traits = {"zzz_unrelated": cv.ToolTraits(tool_id="a", origin="builtin"),
              "send_email": cv.ToolTraits(tool_id="b", origin="builtin")}
    view = cv.build_view(recs, "send email", catalog=cv.NO_ROSTER_READ, traits=traits, k=1)
    assert _names(view.full_records()) == ["send_email"]


def test_dec34_no_production_code_writes_verified_usage():
    """DEC-34: the view may not claim ranking by verified usage unless the input is
    the independent verified-done record -- which does not exist. So no production
    code may set ``verified_done``/``verified_recency`` on a ToolTraits."""
    import ast
    root = Path(__file__).resolve().parent.parent / "systemu"
    offenders = []
    for p in root.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id",
                                                      getattr(node.func, "attr", "")) == "ToolTraits":
                for kw in node.keywords:
                    if kw.arg in ("verified_done", "verified_recency"):
                        offenders.append(f"{p}:{node.lineno}")
    assert offenders == []
    assert cv.VERIFIED_USAGE_SOURCE == "absent"


# --------------------------------------------------------------------------- #
# DEC-27 -- an unreadable roster is a witnessed, RENDERED state
# --------------------------------------------------------------------------- #

def test_dec27_unreadable_roster_is_rendered_not_an_empty_list(tmp_path):
    from systemu.pipelines.quick_task import _enabled_tool_records, run_quick_task
    v = _vault(tmp_path / "v")
    _add(v, "alpha_enabled")
    (Path(v.root) / "tools" / "index.json").write_text("{ not json", encoding="utf-8")
    assert _enabled_tool_records(v) == []                  # same records as before ...
    read = cv.read_tool_headers(v)
    assert read.state == cv.CATALOG_DEGRADED and "parse" in read.reason
    llm = _fake_llm([{"action": "ANSWER", "answer_md": "ok"}])
    run_quick_task("anything", None, v, llm_json=llm)
    payload = json.loads(llm.calls["payloads"][0])
    status = payload[cv.STATUS_KEY]                        # ... but never silent
    assert status.startswith("DEGRADED") and "NOT evidence" in status
    found = cv.find_tools_tool_result(v, {"query": "alpha"}, lane_extras=())
    assert found["catalog_complete"] is False and "WARNING" in found["matches"]


def test_dec27_a_clean_read_renders_no_status(tmp_path):
    v = _vault(tmp_path / "v")
    _add(v, "alpha_enabled")
    assert cv.read_tool_headers(v).state == cv.CATALOG_OK
    view = cv.build_view([{"name": "alpha_enabled"}], "x",
                         catalog=cv.read_tool_headers(v))
    assert cv.STATUS_KEY not in view.extra_fields(lookup=True)


# --------------------------------------------------------------------------- #
# DEC-43 -- one mint: every single-shot builder renders the view's own value
# --------------------------------------------------------------------------- #

@pytest.fixture
def view_spy(monkeypatch):
    made = []
    real = cv.build_view

    def spy(*a, **k):
        out = real(*a, **k)
        made.append(out)
        return out

    monkeypatch.setattr(cv, "build_view", spy)
    return made


def _many_tools(vault, n=K + 5):
    for i in range(n):
        _add(vault, f"many_{i:02d}_tool")


def _assert_payload_is_view(payload, key, view):
    assert payload[key] == json.loads(json.dumps(view.full_records(), default=str))
    assert payload[cv.INDEX_KEY] == view.index_lines()
    assert len(payload[key]) == K


def test_dec43_validator_renders_the_view(tmp_path, monkeypatch, view_spy):
    import systemu.core.llm_router as router
    from systemu.core.models import Objective, Scroll
    from systemu.pipelines import scroll_validator as sv
    v = _vault(tmp_path / "v")
    _many_tools(v)
    seen = {}
    monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
    monkeypatch.setattr(router, "llm_call_json",
                        lambda **kw: seen.setdefault("user", kw["user"]) and {"satisfiable": True})
    scroll = Scroll(id="s", name="S", source_session_id="x", raw_instructions_path="",
                    narrative_md="", objectives=[Objective(id=1, goal="g", success_criteria="c")])
    sv.validate_scroll(scroll, config=object(), vault=v)
    _assert_payload_is_view(json.loads(seen["user"]), "tools_available", view_spy[-1])


def test_dec43_refinery_renders_the_view(tmp_path, monkeypatch, view_spy):
    from types import SimpleNamespace
    from systemu.core.models import Objective, Scroll
    from systemu.pipelines import refinery
    v = _vault(tmp_path / "v")
    _many_tools(v)
    seen = {}

    def boom(**kw):
        seen["user"] = kw["user"]
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(refinery, "llm_call_json", boom)
    scroll = Scroll(id="s", name="S", source_session_id="x", raw_instructions_path="",
                    narrative_md="", objectives=[Objective(id=1, goal="g", success_criteria="c")])
    ctx = SimpleNamespace(execution_id="exec_cap4_spy", get_full_history=lambda: [])
    refinery.process_execution_result(SimpleNamespace(id="sh_cap4_spy"), scroll,
                                      {"status": "failure"}, ctx, object(), v)
    _assert_payload_is_view(json.loads(seen["user"]), "available_tools", view_spy[-1])


def test_dec43_recalibrator_renders_the_view(tmp_path, monkeypatch, view_spy):
    from types import SimpleNamespace
    import systemu.core.llm_router as router
    from systemu.pipelines import skill_recalibrator as sr
    v = _vault(tmp_path / "v")
    _many_tools(v)
    seen = {}

    def boom(**kw):
        seen["user"] = kw["user"]
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(router, "llm_call_json", boom)
    skill = SimpleNamespace(id="sk", name="many things", description="d",
                            target_outcomes=[], produces=[], required_tool_names=[],
                            instructions_md="", skill_version=1, evolution_history=[])
    sr.recalibrate_skill(skill, failure_context={}, config=object(), vault=v,
                         mode="bump_skill")
    _assert_payload_is_view(json.loads(seen["user"]), "current_tool_catalog",
                            view_spy[-1])


def test_dec43_extractor_renders_the_view(tmp_path, monkeypatch, view_spy):
    from systemu.core.models import Objective, Scroll
    from systemu.pipelines import activity_extractor as ae
    v = _vault(tmp_path / "v")
    _many_tools(v)
    seen = {}

    def boom(**kw):
        seen["user"] = kw["user"]
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(ae, "llm_call_json", boom)
    scroll = Scroll(id="s", name="S", source_session_id="x", raw_instructions_path="",
                    narrative_md="", intent="many tools",
                    objectives=[Objective(id=1, goal="g", success_criteria="c")])
    v.save_scroll(scroll)
    ae.extract_and_process(scroll, object(), v, skip_shadow_decision=True)
    _assert_payload_is_view(json.loads(seen["user"]), "existing_tools", view_spy[-1])


def test_dec43_lint_is_clean_on_the_tree_and_catches_a_bypass():
    from tools import lint_tool_view as lint
    assert lint.main() == 0
    assert lint.mint_breaks() == []
    bad = lint.find_violations("def build():\n    return {'tools': index}\n", "x.py")
    assert [(f.scope, f.key) for f in bad] == [("build", "tools")]
    ok = lint.find_violations(
        "def build(view):\n    return {'tools': view.full_records(), "
        "**view.payload_fields('tools', lookup=True)}\n", "x.py")
    assert ok == []
    appended = lint.find_violations(
        "def build(p, e):\n    p['available_tools'].append(e)\n", "x.py")
    assert [f.what for f in appended] == ["in-place .append()"]
    kw = lint.find_violations("def b(t):\n    f(available_tools=t)\n", "x.py")
    assert [f.key for f in kw] == ["available_tools"]
