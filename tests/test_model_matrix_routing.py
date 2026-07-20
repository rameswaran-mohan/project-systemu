"""DEC-12 MODEL-MATRIX — the ROUTING pins.

`test_ra10_model_matrix.py` asserts the config knobs exist and parse. That is
not the same as asserting they route anything, and for `binder_tier` /
`parser_tier` it was previously the whole story while both knobs had zero
consumers.

These tests drive real calls through `llm_router` with a fake transport and
assert **which model id reached the wire**. Each knob is mutated to a different
value and the selected model must change — a test that only asserts the setting
was read is the bug, not the fix.

No provider key is ever constructed, logged, or asserted here; the fake
transport is installed at `_get_client`, above the key-resolution layer.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from sharing_on.config import Config
from sharing_on import model_matrix as mm
from systemu.core import llm_router


# Sentinel model ids — distinct per tier so "which model was selected" is a
# question with an unambiguous answer.
TIER1_MODEL = "sentinel/tier1-deep"
TIER2_MODEL = "sentinel/tier2-structured"
TIER3_MODEL = "sentinel/tier3-fast"

MODEL_BY_TIER = {1: TIER1_MODEL, 2: TIER2_MODEL, 3: TIER3_MODEL}


def _cfg(**overrides):
    """A Config with the three tiers pinned to distinguishable sentinels."""
    cfg = Config()
    cfg.tier1_model = TIER1_MODEL
    cfg.tier2_model = TIER2_MODEL
    cfg.tier3_model = TIER3_MODEL
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------
# fake transport
# --------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.reasoning_details = []


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeUsage:
    prompt_tokens = 1
    completion_tokens = 1


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    async def create(self, **kwargs):
        self._sink.append(kwargs["model"])
        return _FakeResponse('{"ok": true, "items": [], "records": []}')


class _FakeChat:
    def __init__(self, sink):
        self.completions = _FakeCompletions(sink)


class _FakeClient:
    def __init__(self, sink):
        self.chat = _FakeChat(sink)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Install the fake transport; return the list of model ids that reached it."""
    sink: list = []
    monkeypatch.setattr(llm_router, "_get_client", lambda config, tier=0: _FakeClient(sink))
    # Force the OpenAI-shape path — the sentinel ids must not be probed for a
    # native provider (that would import providers and consult key presence).
    monkeypatch.setattr(llm_router, "_uses_native_path", lambda tier, config: False)
    return sink


def _selected_model(cfg, wire, **call_kwargs) -> str:
    """Drive one real llm_call and return the model id that reached the wire."""
    result = asyncio.run(llm_router.llm_call(config=cfg, system="s", user="u", **call_kwargs))
    assert len(wire) == 1, f"expected exactly one call, got {len(wire)}"
    assert result["model"] == wire[0]
    return wire[0]


# --------------------------------------------------------------------------
# the matrix itself
# --------------------------------------------------------------------------

def test_every_registered_stage_resolves_to_a_valid_tier():
    """MASTER-SPEC 15.4: 'a test asserts each registered stage resolves'."""
    cfg = _cfg()
    assert mm.registered_stages(), "matrix must not be empty"
    for stage in mm.registered_stages():
        assert mm.resolve_stage_tier(stage, cfg) in (1, 2, 3)


def test_registered_stage_defaults_match_the_documented_table():
    cfg = _cfg()
    expected = {
        "planner": 1, "refiner": 1,
        "binder_assist": 1,
        "consult_parse": 3, "desk_extraction": 3, "brief_phrasing": 3,
        "router_suggestion": 3, "slot_canonicalization": 3,
        "verification": 3,
    }
    assert set(expected) == set(mm.registered_stages())
    for stage, tier in expected.items():
        assert mm.resolve_stage_tier(stage, cfg) == tier, stage


def test_wired_set_is_exactly_what_the_doc_claims():
    """docs/MODEL-MATRIX.md's last column is checkable, not assertable."""
    assert set(mm.wired_stages()) == {
        "planner", "binder_assist", "consult_parse", "desk_extraction",
        "refiner",
    }


def test_blank_knob_falls_back_to_the_class_default_not_tier_two():
    """The bare idiom yields 2 for "" — parser must still land on 3."""
    assert mm.resolve_stage_tier("desk_extraction", _cfg(parser_tier="")) == 3
    assert mm.resolve_stage_tier("planner", _cfg(planner_tier="")) == 1


def test_whitespace_only_knob_falls_back_to_the_class_default():
    """A knob set to whitespace (e.g. SYSTEMU_PLANNER_TIER="  ") is blank.

    This is the one place the matrix is NOT a behaviour-identical copy of the
    old inline idiom it replaced: that idiom's `or "tier1"` guard only caught
    falsy values, so "  " fell through to the else-branch and silently routed
    the planner to tier 2 — a different model than either the operator or the
    default intended. Found by a surviving mutation, pinned here.
    """
    assert mm.resolve_stage_tier("planner", _cfg(planner_tier="   ")) == 1
    assert mm.resolve_stage_tier("desk_extraction", _cfg(parser_tier="\t")) == 3
    assert mm.resolve_stage_tier("binder_assist", _cfg(binder_tier=" ")) == 1


def test_whitespace_knob_at_the_planner_call_site(wire):
    """The same, driven through the real call site and asserted at the wire."""
    from systemu.runtime import open_world_planner as owp
    cfg = _cfg(planner_tier="   ")
    assert owp._resolve_planner_tier(cfg) == 1
    assert _selected_model(cfg, wire, stage="planner") == TIER1_MODEL


def test_missing_knob_attribute_falls_back_to_the_class_default():
    class _Bare:
        pass
    assert mm.resolve_stage_tier("desk_extraction", _Bare()) == 3
    assert mm.resolve_stage_tier("binder_assist", _Bare()) == 1


def test_unregistered_stage_raises_rather_than_defaulting():
    with pytest.raises(ValueError) as exc:
        mm.resolve_stage_tier("planer", _cfg())          # typo, on purpose
    assert "planer" in str(exc.value)
    assert "registered stages" in str(exc.value)


def test_int_valued_verifier_tier_resolves():
    """verifier_tier is an int (=3), unlike the three string knobs."""
    assert mm.resolve_stage_tier("verification", _cfg(verifier_tier=1)) == 1
    assert mm.resolve_stage_tier("verification", _cfg(verifier_tier=3)) == 3


# --------------------------------------------------------------------------
# THE POINT: a knob change selects a different MODEL
# --------------------------------------------------------------------------

@pytest.mark.parametrize("knob,stage,label,expected_model", [
    # parser_tier — previously ZERO consumers
    ("parser_tier",  "desk_extraction", "tier3", TIER3_MODEL),
    ("parser_tier",  "desk_extraction", "tier1", TIER1_MODEL),
    ("parser_tier",  "desk_extraction", "tier2", TIER2_MODEL),
    ("parser_tier",  "consult_parse",   "tier1", TIER1_MODEL),
    # binder_tier — previously ZERO consumers
    ("binder_tier",  "binder_assist",   "tier1", TIER1_MODEL),
    ("binder_tier",  "binder_assist",   "tier3", TIER3_MODEL),
    ("binder_tier",  "binder_assist",   "tier2", TIER2_MODEL),
    # planner_tier — had one consumer, now goes through the matrix
    ("planner_tier", "planner",         "tier1", TIER1_MODEL),
    ("planner_tier", "planner",         "tier3", TIER3_MODEL),
])
def test_tier_knob_selects_the_model_for_its_stage(wire, knob, stage, label, expected_model):
    cfg = _cfg(**{knob: label})
    assert _selected_model(cfg, wire, stage=stage) == expected_model


def test_the_same_stage_selects_different_models_under_different_knobs(monkeypatch):
    """The differential, stated directly: one stage, two knob values, two models."""
    seen = []
    for label in ("tier3", "tier1"):
        sink: list = []
        monkeypatch.setattr(llm_router, "_get_client", lambda config, tier=0, _s=sink: _FakeClient(_s))
        monkeypatch.setattr(llm_router, "_uses_native_path", lambda tier, config: False)
        asyncio.run(llm_router.llm_call(
            config=_cfg(parser_tier=label), system="s", user="u", stage="desk_extraction"))
        seen.append(sink[0])
    assert seen == [TIER3_MODEL, TIER1_MODEL]
    assert seen[0] != seen[1], "knob change did not change the selected model"


def test_binder_and_parser_knobs_are_independent(wire, monkeypatch):
    """A binder knob must not move a parser stage, and vice versa."""
    cfg = _cfg(binder_tier="tier3", parser_tier="tier1")
    assert _selected_model(cfg, wire, stage="binder_assist") == TIER3_MODEL
    wire.clear()
    assert _selected_model(cfg, wire, stage="desk_extraction") == TIER1_MODEL


# --------------------------------------------------------------------------
# router contract
# --------------------------------------------------------------------------

def test_explicit_tier_still_routes_unchanged(wire):
    """The pre-matrix path is untouched — the majority of the build uses it."""
    assert _selected_model(_cfg(), wire, tier=2) == TIER2_MODEL


def test_stage_overrides_a_disagreeing_explicit_tier(wire, caplog):
    """Stage is the intent; a literal tier beside it is the stale hard-coding."""
    import logging
    with caplog.at_level(logging.INFO, logger="systemu.core.llm_router"):
        model = _selected_model(_cfg(parser_tier="tier3"), wire,
                                tier=1, stage="desk_extraction")
    assert model == TIER3_MODEL
    msgs = [r.getMessage() for r in caplog.records]
    assert any("MODEL-MATRIX" in m and "overridden" in m for m in msgs), \
        f"the override must be logged, not silent; saw {msgs}"


def test_neither_tier_nor_stage_raises():
    with pytest.raises(ValueError) as exc:
        asyncio.run(llm_router.llm_call(config=_cfg(), system="s", user="u"))
    assert "tier" in str(exc.value) and "stage" in str(exc.value)


def test_unregistered_stage_raises_through_the_router(wire):
    with pytest.raises(ValueError):
        asyncio.run(llm_router.llm_call(
            config=_cfg(), system="s", user="u", stage="not_a_stage"))
    assert wire == [], "a bad stage must never reach the provider"


def test_sync_and_async_json_wrappers_carry_the_stage(wire, monkeypatch):
    cfg = _cfg(parser_tier="tier1")
    asyncio.run(llm_router.async_llm_call_json(
        config=cfg, system="s", user="u", stage="desk_extraction"))
    assert wire == [TIER1_MODEL]
    wire.clear()
    llm_router.llm_call_json(config=cfg, system="s", user="u", stage="consult_parse")
    assert wire == [TIER1_MODEL]


def test_json_wrapper_rejects_an_unregistered_stage(wire):
    with pytest.raises(ValueError):
        llm_router.llm_call_json(config=_cfg(), system="s", user="u", stage="nope")
    assert wire == []


# --------------------------------------------------------------------------
# the wired call sites, driven for real
# --------------------------------------------------------------------------

#: Long enough to clear extractor._MIN_INPUT_CHARS (50) AFTER html stripping —
#: a shorter fixture short-circuits before the LLM call and the routing
#: assertion would silently pass over a call that never happened.
_PAGE_TEXT = (
    "<html><body><p>Quarterly results for the northern region are listed "
    "below, one row per branch office.</p></body></html>"
)


def test_desk_extraction_call_site_honours_parser_tier(wire):
    """extractor.extract_records -> the wire, with the knob deciding the model."""
    from systemu.runtime import extractor
    out = extractor.extract_records(
        _PAGE_TEXT,
        {"type": "object", "properties": {"a": {"type": "string"}}},
        config=_cfg(parser_tier="tier1"),
    )
    assert out["success"] is True
    assert wire == [TIER1_MODEL], "parser_tier did not reach the extractor's call"


def test_desk_extraction_default_is_still_tier3(wire):
    """Behaviour preservation: the default knob keeps the shipped tier-3 model."""
    from systemu.runtime import extractor
    extractor.extract_records(
        _PAGE_TEXT,
        {"type": "object", "properties": {"a": {"type": "string"}}},
        config=_cfg(),
    )
    assert wire == [TIER3_MODEL]


def test_consult_parse_call_site_honours_parser_tier(wire):
    """table_consult with its REAL default_llm_fn (not a stub)."""
    from systemu.runtime import table_consult as tc
    tc.parse_area_answers(
        "services", {"items": "Gmail, Notion"},
        llm_fn=tc.default_llm_fn(), config=_cfg(parser_tier="tier2"))
    assert wire == [TIER2_MODEL]


def _bootstrap_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions", "elder"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root=tmp_path)


def test_binder_assist_call_site_honours_binder_tier(wire, tmp_path):
    """fact_extractor.extract_from_chat -> the wire.

    This is the ONLY binder-class LLM call site in the build, so without this
    pin a revert of that one `stage=` back to `tier=1` would make `binder_tier`
    inert again with every other test still green.
    """
    from systemu.pipelines import fact_extractor
    vault = _bootstrap_vault(tmp_path)
    entry = {"ts": "2026-07-20T00:00:00", "prompt": "I live in Chennai",
             "status": "completed"}
    fact_extractor.extract_from_chat(entry, vault, _cfg(binder_tier="tier2"))
    assert wire == [TIER2_MODEL], "binder_tier did not reach the fact extractor"


def test_binder_assist_default_is_still_tier1(wire, tmp_path):
    from systemu.pipelines import fact_extractor
    vault = _bootstrap_vault(tmp_path)
    entry = {"ts": "2026-07-20T00:00:00", "prompt": "I live in Chennai",
             "status": "completed"}
    fact_extractor.extract_from_chat(entry, vault, _cfg())
    assert wire == [TIER1_MODEL]


def test_planner_resolves_through_the_matrix():
    """open_world_planner._resolve_planner_tier now delegates to the matrix."""
    from systemu.runtime import open_world_planner as owp
    assert owp._resolve_planner_tier(_cfg(planner_tier="tier3")) == 3
    assert owp._resolve_planner_tier(_cfg(planner_tier="tier1")) == 1
    assert owp._resolve_planner_tier(_cfg()) == 1


# --------------------------------------------------------------------------
# the `refiner` call sites, driven for real
# --------------------------------------------------------------------------
#
# scroll_refiner.py has FIVE llm_call_json call sites, all previously hard-coded
# `tier=1`. All five now tag stage="refiner". Only THREE are on a live
# production path; the other two live in `_refine_with_gui_guard`, which nothing
# in production calls (refine_scroll re-implements that guard inline). The tests
# below drive the three live ones through the real router and assert the model
# id that reached the wire. See docs/MODEL-MATRIX.md.

#: A draft the real pipeline accepts, with a clean (non-GUI-codified) objective.
_CLEAN_DRAFT = {
    "title": "Document weather",
    "intent": "Document weather",
    "expected_outcome": "a weather doc exists",
    "narrative_md": "x",
    "objectives": [
        {"id": 1, "goal": "fetch weather data",
         "success_criteria": "data received", "output_type": "data"},
    ],
    "constraints": {}, "observed_preferences": {}, "tags": ["weather"],
    "self_check_passed": True, "self_check_notes": "",
}

#: Same, but the objective trips detect_gui_codification ("screenshot"), which
#: is what forces refine_scroll's inline GUI-rewrite retry — the SECOND live
#: call site. Without a matching goal that site never runs and a routing
#: assertion over it would pass vacuously.
_GUI_DRAFT = {
    **_CLEAN_DRAFT,
    "objectives": [
        {"id": 1, "goal": "take a screenshot of the dashboard",
         "success_criteria": "image saved", "output_type": "side_effect"},
    ],
}


class _ScriptedCompletions:
    """`_FakeCompletions` with a scriptable body per successive call."""

    def __init__(self, sink, bodies):
        self._sink = sink
        self._bodies = bodies

    async def create(self, **kwargs):
        self._sink.append(kwargs["model"])
        return _FakeResponse(self._bodies[min(len(self._sink) - 1,
                                              len(self._bodies) - 1)])


class _ScriptedChat:
    def __init__(self, sink, bodies):
        self.completions = _ScriptedCompletions(sink, bodies)


class _ScriptedClient:
    def __init__(self, sink, bodies):
        self.chat = _ScriptedChat(sink, bodies)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def scripted_wire(monkeypatch):
    """Fake transport whose response body the test can script.

    Yields ``(sink, set_bodies)``: `sink` is the list of model ids that reached
    the wire, in order; `set_bodies` replaces the scripted responses.
    """
    sink: list = []
    bodies: list = [json.dumps(_CLEAN_DRAFT)]
    monkeypatch.setattr(llm_router, "_get_client",
                        lambda config, tier=0: _ScriptedClient(sink, bodies))
    monkeypatch.setattr(llm_router, "_uses_native_path", lambda tier, config: False)
    # The scroll validator makes its OWN untagged tier=1 call and is env-gated;
    # clear the env so these tests see only what the config asks for.
    monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)

    def set_bodies(*payloads):
        bodies[:] = [p if isinstance(p, str) else json.dumps(p) for p in payloads]

    return sink, set_bodies


def _refiner_cfg(**overrides):
    """A `_cfg` with the scroll validator OFF.

    The validator issues its own untagged `tier=1` call from inside
    `refine_scroll`; switching it off keeps `sink` to just the refiner's calls.
    `test_planner_tier_moves_the_refiner_but_not_the_validator` turns it back on
    on purpose.
    """
    overrides.setdefault("scroll_validator", False)
    overrides.setdefault("intelligent_supervisor_enabled", False)
    return _cfg(**overrides)


def _refiner_vault(tmp_path):
    vault = _bootstrap_vault(tmp_path)
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "chat_history.jsonl").write_text("", encoding="utf-8")
    return vault


def _capture_session(tmp_path):
    """A capture session dir refine_scroll will accept."""
    sess = tmp_path / "captures" / "s1"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "instructions.md").write_text(
        "## Intent\n\n- **Intent:** document weather\n\n# Body\n", encoding="utf-8")
    (sess / "session.json").write_text(
        json.dumps({"name": "weather session", "session_id": "sess_matrix"}),
        encoding="utf-8")
    return sess


@pytest.mark.parametrize("label,expected_model", [
    ("tier1", TIER1_MODEL),
    ("tier2", TIER2_MODEL),
    ("tier3", TIER3_MODEL),
])
def test_refine_scroll_call_site_honours_planner_tier(
        scripted_wire, tmp_path, label, expected_model):
    """refine_scroll -> the wire, with `planner_tier` deciding the model.

    Three knob values, three different models: this is the assertion the matrix
    exists to make true. Before the stage tag, all three landed on tier 1.
    """
    sink, _ = scripted_wire
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll = scroll_refiner.refine_scroll(
        _capture_session(tmp_path), _refiner_cfg(planner_tier=label), vault)

    assert scroll.objectives, "pipeline did not consume a draft — call never ran"
    assert sink, "no LLM call reached the wire; the routing assertion is vacuous"
    assert sink[0] == expected_model, f"planner_tier={label!r} did not route: {sink}"


def test_refine_scroll_default_is_still_tier1(scripted_wire, tmp_path):
    """Behaviour preservation: the shipped default keeps the tier-1 model.

    The five call sites hard-coded `tier=1`. `planner_tier` defaults to "tier1",
    so tagging them must be a routing-plumbing change, not a behaviour change.
    """
    sink, _ = scripted_wire
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll_refiner.refine_scroll(
        _capture_session(tmp_path), _refiner_cfg(), vault)
    assert sink and sink[0] == TIER1_MODEL, sink


@pytest.mark.parametrize("label,expected_model", [
    ("tier1", TIER1_MODEL),
    ("tier2", TIER2_MODEL),
    ("tier3", TIER3_MODEL),
])
def test_refine_from_text_call_site_honours_planner_tier(
        scripted_wire, tmp_path, label, expected_model):
    """The chat path (`elder_intake`) — the third live refiner call site."""
    sink, _ = scripted_wire
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll = scroll_refiner.refine_from_text(
        "document the weather", vault, _refiner_cfg(planner_tier=label))

    assert scroll.objectives, "pipeline did not consume a draft — call never ran"
    assert sink, "no LLM call reached the wire; the routing assertion is vacuous"
    assert sink[0] == expected_model, f"planner_tier={label!r} did not route: {sink}"


def test_refine_from_text_default_is_still_tier1(scripted_wire, tmp_path):
    sink, _ = scripted_wire
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll_refiner.refine_from_text(
        "document the weather", vault, _refiner_cfg())
    assert sink and sink[0] == TIER1_MODEL, sink


def test_refiner_gui_rewrite_retry_runs_on_the_knobs_model(scripted_wire, tmp_path):
    """The SECOND live site: refine_scroll's inline GUI-rewrite retry.

    The first draft is GUI-codified, which forces the rewrite call. Both calls
    must land on the knob's model — a retry that silently reverted to a literal
    tier 1 would be exactly the drift the stage tag removes.
    """
    sink, set_bodies = scripted_wire
    set_bodies(_GUI_DRAFT, _CLEAN_DRAFT)
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll_refiner.refine_scroll(
        _capture_session(tmp_path), _refiner_cfg(planner_tier="tier3"), vault)

    assert len(sink) >= 2, f"the GUI-rewrite retry never fired: {sink}"
    assert sink[:2] == [TIER3_MODEL, TIER3_MODEL], sink


def test_refiner_selfcheck_retry_runs_on_the_knobs_model(scripted_wire, tmp_path):
    """The self-check auto-retry re-enters `_call_refine` — same call site."""
    sink, set_bodies = scripted_wire
    failing = {**_CLEAN_DRAFT, "self_check_passed": False,
               "self_check_notes": "objective 1 does not serve the intent"}
    set_bodies(failing, _CLEAN_DRAFT)
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll_refiner.refine_scroll(
        _capture_session(tmp_path), _refiner_cfg(planner_tier="tier2"), vault)

    assert len(sink) >= 2, f"the self-check retry never fired: {sink}"
    assert sink[:2] == [TIER2_MODEL, TIER2_MODEL], sink


def test_planner_tier_moves_the_refiner_but_not_the_untagged_validator(
        scripted_wire, tmp_path):
    """The knob is TARGETED, not global.

    `refine_scroll` also invokes `scroll_validator`, which still hard-codes
    `tier=1` and is one of the ~36 deliberately untouched call sites. Moving
    `planner_tier` must move the refiner and leave the validator where it is —
    otherwise the knob is a global tier override wearing a stage's name.
    """
    sink, _ = scripted_wire
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll_refiner.refine_scroll(
        _capture_session(tmp_path),
        _refiner_cfg(planner_tier="tier3", scroll_validator=True),
        vault)

    assert len(sink) >= 2, f"the validator call never fired: {sink}"
    assert sink[0] == TIER3_MODEL, f"refiner did not move: {sink}"
    assert sink[1] == TIER1_MODEL, f"untagged validator moved with it: {sink}"


def test_whitespace_planner_tier_at_the_refiner_call_site(scripted_wire, tmp_path):
    """A whitespace-only knob must fall back to tier 1, not tier 2.

    Same live-bug class as `test_whitespace_only_knob_falls_back_to_the_class_default`,
    asserted at the wire through a real call site: the `or "tier1"` idiom only
    caught FALSY values, so "  " was truthy, fell through, and routed to tier 2.
    """
    sink, _ = scripted_wire
    from systemu.pipelines import scroll_refiner
    vault = _refiner_vault(tmp_path / "vault")
    scroll_refiner.refine_from_text(
        "document the weather", vault, _refiner_cfg(planner_tier="   "))
    assert sink and sink[0] == TIER1_MODEL, sink


def test_the_gui_guard_helpers_two_call_sites_route_on_the_knob(
        scripted_wire, tmp_path):
    """`_refine_with_gui_guard` holds the other two of the five call sites.

    Nothing in production calls it (pinned separately, twice, below), but both
    of its calls are tagged for consistency, so pin that they actually route.
    The `wired=True` claim in the matrix rests on the three LIVE sites above,
    not on this one.

    Reads no source, so it stays inside the EDIT-SAFE gate — without it, a
    revert of either of these two `stage=` tags would survive
    `pytest -m "not source_sensitive"`.
    """
    sink, set_bodies = scripted_wire
    set_bodies(_GUI_DRAFT, _CLEAN_DRAFT)
    import systemu.pipelines.scroll_refiner as sr
    sr._refine_with_gui_guard(
        payload={"x": 1}, prompt_text="p",
        config=_refiner_cfg(planner_tier="tier2"))
    assert sink[:2] == [TIER2_MODEL, TIER2_MODEL], sink


def test_production_paths_never_invoke_the_gui_guard_helper(
        scripted_wire, tmp_path, monkeypatch):
    """The dead-helper claim, pinned by BEHAVIOUR — and edit-safe.

    `refine_scroll` re-implements this guard inline, so neither live entry point
    may reach the helper. A source-text version of this claim lives in
    `test_no_call_to_the_gui_guard_helper_anywhere_in_the_module`; that one has
    to be `source_sensitive` (it reads the file under test, so an unrelated
    mid-edit comment naming the helper reds it). This one cannot be fooled by a
    comment, and it checks what the doc actually asserts: that the LIVE paths
    do not call it.

    Weaker than the source pin in one direction — it only covers the branches it
    drives — which is why both exist.
    """
    sink, set_bodies = scripted_wire
    import systemu.pipelines.scroll_refiner as sr

    seen: list = []
    real = sr._refine_with_gui_guard
    monkeypatch.setattr(sr, "_refine_with_gui_guard",
                        lambda **kw: (seen.append("called"), real(**kw))[1])

    # The spy must be demonstrably able to fire, or `seen == []` below is
    # vacuous — a monkeypatch that silently failed to bind would "pass".
    set_bodies(_CLEAN_DRAFT)
    sr._refine_with_gui_guard(payload={"x": 1}, prompt_text="p",
                              config=_refiner_cfg())
    assert seen == ["called"], "spy never fired — the assertion below proves nothing"
    seen.clear()

    # Both live entry points, including the GUI-codified branch — the closest
    # analogue of what the helper does, and where a re-wiring would land.
    set_bodies(_GUI_DRAFT, _CLEAN_DRAFT)
    sr.refine_scroll(_capture_session(tmp_path), _refiner_cfg(),
                     _refiner_vault(tmp_path / "v1"))
    set_bodies(_CLEAN_DRAFT)
    sr.refine_from_text("document the weather",
                        _refiner_vault(tmp_path / "v2"), _refiner_cfg())
    assert seen == [], f"a live path invoked the supposedly-dead helper: {seen}"


@pytest.mark.source_sensitive
def test_no_call_to_the_gui_guard_helper_anywhere_in_the_module():
    """The same claim over the WHOLE module, including undriven branches.

    Explicitly `source_sensitive`: it reads `scroll_refiner.py` off disk, so a
    subagent mid-edit reds it spuriously. GATE-TIER/DEC-14's auto-tagger only
    detects the `inspect.getsource` idiom, and this reads the file directly —
    so the marker has to be manual or this silently breaks the EDIT-SAFE gate's
    "safe to run concurrently with source edits" promise.
    """
    from pathlib import Path
    import systemu.pipelines.scroll_refiner as sr
    src = Path(sr.__file__).read_text(encoding="utf-8")
    # The ONLY occurrence of the name followed by "(" must be its own def —
    # i.e. the module never calls it. A bare `== 0` would be wrong here (the
    # def line itself matches), and `not in` would be vacuously false.
    assert "def _refine_with_gui_guard(" in src, "helper vanished — retarget this pin"
    assert src.count("_refine_with_gui_guard(") == 1, (
        "production now calls _refine_with_gui_guard — update the matrix note "
        "and docs/MODEL-MATRIX.md, which both state that it does not"
    )


# --------------------------------------------------------------------------
# locality is declarative — pin that it does NOT route
# --------------------------------------------------------------------------

def test_locality_is_declared_per_stage():
    assert mm.locality_of_stage("desk_extraction") == "local_capable"
    assert mm.locality_of_stage("planner") == "cloud_default"
    assert mm.locality_of_stage("binder_assist") == "cloud_default"
    assert mm.locality_of_stage("verification") == "n/a"
    assert set(mm.stages_by_locality("local_capable")) == {
        "consult_parse", "desk_extraction", "brief_phrasing",
        "router_suggestion", "slot_canonicalization",
    }


def test_locality_does_not_influence_model_selection(wire):
    """A local_capable stage still routes to the configured CLOUD model.

    This pins the deferral: if someone later makes locality route, this test
    fails and forces the PCM spec-pass conversation MASTER-SPEC 15.4 requires.
    """
    cfg = _cfg(parser_tier="tier1")
    assert mm.locality_of_stage("desk_extraction") == "local_capable"
    assert _selected_model(cfg, wire, stage="desk_extraction") == TIER1_MODEL


def test_stage_locality_and_model_locality_are_different_functions():
    from sharing_on.model_presets import locality_of
    assert locality_of("ollama/llama3") == "local_capable"          # a MODEL id
    assert mm.locality_of_stage("consult_parse") == "local_capable"  # a STAGE
    with pytest.raises(ValueError):
        mm.locality_of_stage("ollama/llama3")   # not a stage name


# --------------------------------------------------------------------------
# the citations that were dangling
# --------------------------------------------------------------------------

def test_the_cited_doc_exists():
    """sharing_on/config.py, model_presets.py, test_ra10_model_matrix.py and
    release-notes/v0.9.60.md all cite docs/MODEL-MATRIX.md."""
    from pathlib import Path
    doc = Path(__file__).resolve().parents[1] / "docs" / "MODEL-MATRIX.md"
    assert doc.is_file(), f"dangling citation: {doc} does not exist"
    text = doc.read_text(encoding="utf-8")
    for stage in mm.registered_stages():
        assert f"`{stage}`" in text, f"{stage} is registered but undocumented"


def test_the_doc_does_not_leak_a_key():
    from pathlib import Path
    doc = Path(__file__).resolve().parents[1] / "docs" / "MODEL-MATRIX.md"
    text = doc.read_text(encoding="utf-8").lower()
    for needle in ("sk-or-", "sk-ant-", "api_key=", "apikey="):
        assert needle not in text, f"doc contains a key-shaped example: {needle}"
