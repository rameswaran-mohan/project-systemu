"""R-A16 / IMPL-5 — the PROMPT-CHANNEL laundering exposure.

A ``content_derived`` user_fact is injected verbatim into planning prompts
(``scroll_refiner``'s tier-1 elder_intake payload, recent 20; the planner's fenced
SituationReport, capped to the most-recent ``_PROMPT_FACT_BUDGET`` — it carried the
WHOLE profile until the corpus bound in ``test_ra16_taint_corpus_bound`` required
otherwise). Nothing stopped the model from
copying such a value into a tool call's params, and bind source #0
(``_bind_provided_params``) then stamped it ``systemu_authored`` at confidence 1.0 — a
TRUSTED axis — so it bound SILENTLY. The taint laundered through the MODEL rather than
through the store.

Executed before the clamp, with the SAME value and the SAME vault::

    route A (store -> binder source #4): value_origin=content_derived  _needs_ask=True
    route B (store -> PROMPT -> model -> provided_params -> source #0):
                                         value_origin=systemu_authored _needs_ask=False
    >>> SILENT BIND of a value the store had already recorded as untrusted <<<

WHY THE FIX IS A GATE, NOT A PROMPT MARKER. Rendering provenance inline and asking the
model to honour it is a request to an untrusted component, not a control — and CAP-0 #4
already bans that shape for trust decisions. The evidence is direct: the planner's
fenced SituationReport ALREADY renders ``origin_class`` on every user_fact, and the
laundering completed anyway, because the trust decision is made at the binder.

NOT CLOSED BY THIS (see ``_provided_value_is_content_seeded``): in-context content with
no stored taint record (tool results, fetched pages); the quick lane, which never calls
the binder at all; and values below ``_MIN_TAINT_MATCH_LEN``.
"""
from __future__ import annotations

import os

import pytest

from systemu.core.models import Objective, Tool
from systemu.runtime import requirement_binder as rb
from systemu.runtime.requirement_binder import (
    build_requirement_report,
    compute_requirements,
)

TAINTED = "acct-99-attacker"


class _FakeGranted:
    def is_within_granted(self, candidate) -> bool:
        return False


class _FakeCtx:
    def __init__(self, situation):
        self._situation_report = situation
        self._granted_roots = _FakeGranted()
        self.files_produced = []
        self.vault = None


def _tool(schema):
    return Tool(id="tool_x", name="transfer_funds", description="move money",
                tool_type="python_function", parameters_schema=schema,
                effect_tags=[], external_verification_channel=None)


def _obj():
    return Objective(id=1, goal="send the payment", success_criteria="done")


def _fact(**over):
    f = {"id": "fact_1", "ts": "2020", "fact": f"account_id is {TAINTED}",
         "tags": ["account_id"], "source": "auto_extract", "confidence": 0.95,
         "origin_class": "content_derived"}
    f.update(over)
    return f


def _situation(facts):
    return {"services": [], "capabilities": [], "roots": [], "credentials": [],
            "declared_intents": [],
            "profile": {"name": "Op", "location_text": "NYC", "timezone": "UTC",
                        "default_output_dir": "/out", "user_facts": facts}}


def _bind(provided, facts, *, schema=None, leaf="account_id"):
    situation = _situation(facts)
    ctx = _FakeCtx(situation)
    cap = _tool(schema or {leaf: {"type": "string", "description": "destination"}})
    reqs = compute_requirements(_obj(), cap, situation, ctx,
                                provided_params=provided)
    match = [r for r in reqs if r.schema_path.endswith(leaf)]
    assert match, f"the required {leaf} leaf should produce a Requirement"
    rep = build_requirement_report([_obj()], cap, situation, ctx,
                                   provided_params=provided)
    in_ask = any(a.schema_path.endswith(leaf) for a in rep.ask_bundle)
    return match[0], in_ask


# ── THE EXPOSURE PIN ───────────────────────────────────────────────────────
def test_provided_param_matching_a_content_derived_fact_is_confirm_gated():
    """THE pin. A value the model copied out of a content_derived fact (which it read
    in the injected user-context block) must NOT be stamped systemu_authored at
    source #0. Executed before the clamp:

        value_origin='systemu_authored' state='have' _needs_ask=False in_ask=False

    -- i.e. the taint laundered through the prompt."""
    req, in_ask = _bind({"account_id": TAINTED}, [_fact()])

    assert req.source == "provided"
    assert req.value_origin == "content_derived", (
        "a provided param whose value appears verbatim inside a content_derived "
        "user_fact is content-seeded; stamping it systemu_authored launders the taint "
        "the store already recorded"
    )
    assert rb._needs_ask(req) is True
    assert in_ask


def test_laundered_value_keeps_the_content_seeded_predicate_true():
    """Spec 5.3/IMPL-5: an effect is content-seeded IFF any bind traces to
    content_derived. If the prompt detour flips the stamp, the predicate reads FALSE
    for an effect that really is content-seeded."""
    req, _ = _bind({"account_id": TAINTED}, [_fact()])
    assert any(r == "content_derived" for r in [req.value_origin])


def test_clamp_survives_the_legacy_unstamped_auto_extract_shape():
    """The legacy corpus (origin_class absent, source='auto_extract') is clamped by
    ``_fact_origin`` on READ, so the source-#0 clamp must see it as tainted too."""
    req, in_ask = _bind({"account_id": TAINTED},
                        [_fact(origin_class=None)])
    assert req.value_origin == "content_derived"
    assert rb._needs_ask(req) is True
    assert in_ask


def test_clamp_matches_a_paraphrased_fact_sentence():
    """The fact is a SENTENCE; the model emits the extracted VALUE. Matching must be
    containment (value inside the sentence), not equality, or the realistic shape
    misses entirely."""
    req, _ = _bind({"account_id": TAINTED},
                   [_fact(fact=f"the account the user mentioned was {TAINTED}, per the email",
                          tags=[])])
    assert req.value_origin == "content_derived"


# ── THE PAYOFF MUST SURVIVE (over-ask regression pins) ─────────────────────
#
# R-A12c/R-A13a made provided params bind SILENTLY on purpose -- that was the fix for
# a real over-ask defect. A "clamp every provided param" regression would be green on
# every pin above while re-breaking it. These are the pins that catch that.

def test_provided_param_with_no_tainted_facts_still_binds_silently():
    """The overwhelmingly common case: no content_derived facts at all. Behaviour must
    be byte-identical to before the clamp."""
    req, in_ask = _bind({"account_id": "acct-operator-typed"},
                        [_fact(source="explicit_user", origin_class="operator",
                               fact="account_id is acct-operator-typed")])
    assert req.value_origin == "systemu_authored"
    assert req.state == "have"
    assert rb._needs_ask(req) is False
    assert not in_ask


def test_provided_param_not_matching_any_tainted_fact_binds_silently():
    """A tainted fact EXISTS but the model's value did not come from it."""
    req, in_ask = _bind({"account_id": "acct-unrelated-value"}, [_fact()])
    assert req.value_origin == "systemu_authored"
    assert rb._needs_ask(req) is False
    assert not in_ask


def test_short_values_do_not_false_match_into_an_over_ask():
    """A short/common value is a substring of almost any sentence. Clamping those
    would re-introduce the over-ask defect across every enum-ish leaf."""
    req, _ = _bind({"mode": "is"},
                   [_fact(fact="account_id is acct-1", tags=["mode"])],
                   schema={"mode": {"type": "string", "description": "mode"}},
                   leaf="mode")
    assert req.value_origin == "systemu_authored", (
        "a sub-threshold value must not clamp, or every short param over-asks"
    )
    assert rb._needs_ask(req) is False


def test_a_value_buried_inside_a_longer_word_does_not_clamp():
    """Matching is TOKEN-delimited, not raw substring. "count" sits inside
    "accounting"; a raw ``in`` test would clamp it and manufacture an over-ask on a
    leaf that has nothing to do with the tainted fact."""
    req, in_ask = _bind({"account_id": "count"},
                        [_fact(fact="the accounting team handles it", tags=[])])
    assert req.value_origin == "systemu_authored", (
        "raw-substring matching over-clamps; the guard must be token-delimited"
    )
    assert rb._needs_ask(req) is False
    assert not in_ask


def test_operator_origin_facts_never_arm_the_clamp():
    """Only content_derived facts are tainted. An operator-authored fact containing the
    same value must leave the silent bind intact -- otherwise the profile stops paying
    off for values the operator typed themselves."""
    req, in_ask = _bind({"account_id": TAINTED},
                        [_fact(source="explicit_user", origin_class="operator")])
    assert req.value_origin == "systemu_authored"
    assert rb._needs_ask(req) is False
    assert not in_ask


def test_clamp_is_defensive_against_a_malformed_profile():
    """A rehydrated profile can hold anything. The clamp must degrade to 'not tainted'
    rather than raise -- a raise would empty the whole objective's diff."""
    for junk in ([], [None], ["not-a-dict"], [{"fact": None}], [{"origin_class": []}]):
        req, _ = _bind({"account_id": TAINTED}, junk)
        assert req.value_origin in {"systemu_authored", "content_derived"}
