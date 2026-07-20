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
