"""DEC-12 / DEC-20a: every matrix row is well-formed and routes, including
when only one model is configured.

``test_model_matrix_routing.py`` pins today's rows by name. The checks here are
written over ``registered_stages()`` instead, so a row added later is covered
the moment it exists, with nobody remembering to extend a list.

Routing is asserted at the wire: a fake transport records the model id each
call actually dispatched with. No provider key is constructed or asserted.
"""
from __future__ import annotations

import asyncio

import pytest

from sharing_on import model_matrix as mm
from sharing_on.config import Config
from systemu.core import llm_router

TIER1_MODEL = "sentinel/tier1-deep"
TIER2_MODEL = "sentinel/tier2-structured"
TIER3_MODEL = "sentinel/tier3-fast"
MODEL_BY_TIER = {1: TIER1_MODEL, 2: TIER2_MODEL, 3: TIER3_MODEL}
ONLY_MODEL = "sentinel/the-only-model"

_CLASS_KNOB = {"planner": "planner_tier", "binder": "binder_tier",
               "parser": "parser_tier", "verifier": "verifier_tier",
               "reasoning": "reasoning_tier", "judge": "judge_tier",
               "executor": "executor_tier", "forge": "forge_tier"}

#: The default tier of each class, stated here independently of the module.
#: DEC-12: planner/refiner/binder-assist = strongest; parsers/summarizers =
#: cheap; verification advisory on the cheap tier. The other four classes
#: default to the tier their call sites used before they were tagged.
_CLASS_DEFAULT = {"planner": 1, "binder": 1, "reasoning": 1, "judge": 1,
                  "executor": 2, "forge": 2, "parser": 3, "verifier": 3}


def _cfg(**overrides):
    cfg = Config()
    cfg.tier1_model = TIER1_MODEL
    cfg.tier2_model = TIER2_MODEL
    cfg.tier3_model = TIER3_MODEL
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------
# fake transport: records (tier the client was built for, model on the wire)
# --------------------------------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.content = content
        self.reasoning_details = []


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 1
    completion_tokens = 1


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _Client:
    def __init__(self, sink, tier):
        self._sink = sink
        self._tier = tier
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self._sink.append((self._tier, kwargs["model"]))
        return _Resp('{"ok": true}')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Sink(list):
    """(client tier, model) per dispatched call; ``native_tiers`` records the
    tier each native-path decision was asked about."""
    native_tiers: list


@pytest.fixture
def wire(monkeypatch):
    sink = _Sink()
    native_tiers: list = []
    monkeypatch.setattr(llm_router, "_get_client",
                        lambda config, tier=0: _Client(sink, tier))

    def _no_native(tier, config):
        native_tiers.append(tier)
        return False

    monkeypatch.setattr(llm_router, "_uses_native_path", _no_native)
    sink.native_tiers = native_tiers
    return sink


def _models(sink):
    return [m for _, m in sink]


def _call(cfg, **kw):
    return asyncio.run(llm_router.llm_call(config=cfg, system="s", user="u", **kw))


# --------------------------------------------------------------------------
# every row is well-formed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stage", mm.registered_stages())
def test_every_row_carries_a_tier_class_and_a_locality(stage):
    row = mm.require_stage(stage)
    assert row.stage == stage
    assert row.tier_class in _CLASS_KNOB, row
    if row.tier_class == "verifier":
        # DEC-20a (i): verification is deterministic; locality n/a by construction.
        assert row.locality == "n/a", row
    else:
        assert row.locality in {"cloud_required", "cloud_default", "local_capable"}, row
    if row.tier_class == "parser":
        # DEC-20a (ii): parse-class stages are the ones a local backend serves first.
        assert row.locality == "local_capable", row
    if row.tier_class in {"planner", "binder"}:
        # DEC-20a (iii): planner/binder stay cloud_default until a local model
        # passes that stage's fixture set.
        assert row.locality == "cloud_default", row
    if row.wired:
        assert row.call_site.endswith(".py"), f"wired row names no call site: {row}"


def test_strongest_and_cheap_classes_default_per_dec12():
    """planner/refiner/binder-assist = strongest (tier 1); parse-class and the
    advisory verifier = cheap (tier 3); the rest per ``_CLASS_DEFAULT``."""
    for stage in mm.registered_stages():
        cls = mm.require_stage(stage).tier_class
        assert mm.default_tier_for(stage) == _CLASS_DEFAULT[cls], (stage, cls)


def test_config_ships_each_class_knob_at_its_class_default():
    """Every class knob exists on ``Config`` (and in ``from_env``) with a
    default that resolves to the class default, so no new class changed
    routing out of the box."""
    import os
    for cfg in (Config(), Config.from_env()):
        for cls, knob in _CLASS_KNOB.items():
            env = f"SYSTEMU_{knob.upper()}"
            if env in os.environ:
                continue  # an operator override in this environment
            raw = getattr(cfg, knob)
            label = str(raw).strip()
            got = 1 if "1" in label else (3 if "3" in label else 2)
            assert got == _CLASS_DEFAULT[cls], (knob, raw)


@pytest.mark.parametrize("stage", mm.registered_stages())
def test_every_registered_stage_resolves_per_the_matrix_at_the_wire(wire, stage):
    """Default knob -> the class default's model; each knob value -> its model."""
    _call(_cfg(), stage=stage)
    assert _models(wire) == [MODEL_BY_TIER[mm.default_tier_for(stage)]], stage
    knob = _CLASS_KNOB[mm.require_stage(stage).tier_class]
    for tier, model in MODEL_BY_TIER.items():
        wire.clear()
        _call(_cfg(**{knob: f"tier{tier}"}), stage=stage)
        assert _models(wire) == [model], (stage, knob, tier)


def test_a_summarizer_now_runs_on_the_cheap_tier_and_parser_tier_moves_it(wire):
    """The one behaviour change of tagging every call (DEC-12: summarizers =
    cheap tier). Per-run memory consolidation used a literal tier 1; through
    the ``summarize`` row it now reaches the tier-3 model, and the operator's
    ``parser_tier`` knob moves it."""
    from systemu.runtime import memory_consolidator as mc
    history = [{"role": "user", "content": "I prefer short answers"}]
    mc.consolidate_run(chat_history=history,
                       config=_cfg(memory_consolidation_enabled=True))
    assert _models(wire) == [TIER3_MODEL]
    wire.clear()
    mc.consolidate_run(chat_history=history,
                       config=_cfg(memory_consolidation_enabled=True,
                                   parser_tier="tier1"))
    assert _models(wire) == [TIER1_MODEL]


# --------------------------------------------------------------------------
# single-model configuration
# --------------------------------------------------------------------------

def test_one_model_on_every_tier_serves_every_stage(wire):
    cfg = _cfg(tier1_model=ONLY_MODEL, tier2_model=ONLY_MODEL, tier3_model=ONLY_MODEL)
    for stage in mm.registered_stages():
        _call(cfg, stage=stage)
    assert set(_models(wire)) == {ONLY_MODEL}
    assert len(wire) == len(mm.registered_stages())


@pytest.mark.parametrize("only_tier", [1, 2, 3])
def test_with_one_tier_configured_strongest_and_cheap_both_resolve_to_it(wire, only_tier):
    """The other two tiers are blank. Every stage, the strongest-class ones and
    the cheap ones alike, must dispatch on the one configured model rather than
    send an empty model id (or crash)."""
    blanks = {f"tier{t}_model": "" for t in (1, 2, 3) if t != only_tier}
    cfg = _cfg(**{f"tier{only_tier}_model": ONLY_MODEL}, **blanks)
    for stage in mm.registered_stages():
        result = _call(cfg, stage=stage)
        assert result["model"] == ONLY_MODEL, stage
        assert result["tier"] == only_tier, stage
    assert set(_models(wire)) == {ONLY_MODEL}
    # The client was built for the configured tier, so its provider override
    # is the one that travels with the model.
    assert {t for t, _ in wire} == {only_tier}
    assert set(wire.native_tiers) == {only_tier}


def test_a_legacy_tier_call_also_borrows(wire):
    cfg = _cfg(tier2_model="", tier3_model="")
    _call(cfg, tier=3)
    assert wire == [(1, TIER1_MODEL)]


def test_the_json_wrapper_borrows_too(wire):
    cfg = _cfg(tier1_model="", tier2_model="")
    out = llm_router.llm_call_json(config=cfg, system="s", user="u", stage="planner")
    assert out == {"ok": True}
    assert wire == [(3, TIER3_MODEL)]


def test_whitespace_or_missing_model_counts_as_blank(wire):
    cfg = _cfg(tier3_model="   ", tier2_model=None)
    _call(cfg, stage="desk_extraction")
    assert wire == [(1, TIER1_MODEL)]


def test_borrow_order_is_nearest_then_stronger():
    f = llm_router._configured_tier
    assert f(2, _cfg(tier2_model="")) == 1                      # tie -> stronger
    assert f(3, _cfg(tier3_model="")) == 2                      # nearest
    assert f(3, _cfg(tier3_model="", tier2_model="")) == 1
    assert f(1, _cfg(tier1_model="")) == 2                      # nearest
    assert f(1, _cfg(tier1_model="", tier2_model="")) == 3


def test_a_configured_tier_is_never_rerouted():
    cfg = _cfg()
    assert [llm_router._configured_tier(t, cfg) for t in (1, 2, 3)] == [1, 2, 3]

    class _Opaque:
        """A model value the router cannot read as blank stays put (DEC-36)."""

    odd = _cfg(tier3_model=_Opaque())
    assert llm_router._configured_tier(3, odd) == 3


def test_no_model_anywhere_leaves_the_tier_unchanged():
    cfg = _cfg(tier1_model="", tier2_model="", tier3_model="")
    assert [llm_router._configured_tier(t, cfg) for t in (1, 2, 3)] == [1, 2, 3]
