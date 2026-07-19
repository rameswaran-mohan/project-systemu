"""R-A16 — the AUTO-EXTRACT silent-bind exposure (IMPL-5 taint carriage).

``fact_extractor.extract_from_chat`` persists durable ``user_facts`` that a tier-1 LLM
extracted from ``chat_entry["prompt"]``. Slice 1 allowlisted that caller as an
"operator surface" on the reasoning that its sole input is the operator's own typed
chat prompt. That reasoning does not hold:

  * ``chat_entry["prompt"]`` is operator-DELIVERED, not operator-AUTHORED. An operator
    who pastes an email, a log, or a scraped page into chat authored none of it.
  * The EXTRACTOR — not the operator — chooses which sentences become durable facts,
    and the operator never reviews the output before it lands in the profile.
  * ``prompts/extract_user_facts.md`` asks for confidence >= 0.9, above ``T_HIGH``
    (0.80), and ``_bind_profile`` matches on tag OR raw fact TEXT — so an LLM
    paraphrase binds with no matching tag and no operator confirm.

Executed before this change::

    persisted: source='auto_extract', confidence=0.95, origin_class=None
    value_origin=operator, state=have, _needs_ask=False, in ask_bundle=False
    >>> SILENT BIND of an LLM paraphrase of possibly-pasted content <<<

Two deterministic pieces close it, and this file pins BOTH:

  1. WRITER STAMP — ``extract_from_chat`` passes ``origin_class="content_derived"``.
  2. READER CLAMP — ``requirement_binder._fact_origin`` treats an absent stamp on a
     ``source="auto_extract"`` fact as ``content_derived``. This is what closes the
     LEGACY corpus already sitting in operator vaults, with no migration. It is
     deterministic, not a heuristic: ``UserFact.source`` is a REQUIRED field (no
     default) and ``fact_extractor`` is the SOLE writer of that source string.

THE PAYOFF MUST SURVIVE. The grandfather (absent ⇒ ``operator``) stays for genuinely
operator-authored sources — ``onboarding`` (welcome + tour) and ``explicit_user``
(``user remember``) — which must still bind SILENTLY. A "clamp everything" regression
would be green on the exposure pins alone; ``test_operator_authored_sources_*`` below
is the pin that catches it.

NOT CLOSED BY THIS: user facts flow into LLM prompts regardless of taint
(``shadow_runtime._build_profile_block``, ``scroll_refiner``). If the LLM copies a
value from a tainted fact into tool params it is stamped trusted at a DIFFERENT bind
source. That is a separate, known residual whose defense is the S4 net.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from systemu.core.models import Objective, Tool
from systemu.runtime import requirement_binder as rb
from systemu.runtime.requirement_binder import (
    build_requirement_report,
    compute_requirements,
)


# ── fixtures (mirror tests/test_ra16_slice1_taint_carriage.py) ──────────────
class _FakeGrantedRoots:
    def __init__(self, roots):
        self._roots = [os.path.normcase(os.path.abspath(r)) for r in roots]

    def is_within_granted(self, candidate: str) -> bool:
        c = os.path.normcase(os.path.abspath(str(candidate or "")))
        return any(c == r or c.startswith(r + os.sep) for r in self._roots)


class _FakeCtx:
    def __init__(self, *, situation=None, granted_roots=None):
        self._situation_report = situation
        self._granted_roots = granted_roots
        self.files_produced = []
        self.vault = None


def _tool(name, schema):
    return Tool(id="tool_" + name, name=name, description="test tool",
                tool_type="python_function", parameters_schema=schema,
                effect_tags=[], external_verification_channel=None)


def _obj(oid=1, goal="do the thing"):
    return Objective(id=oid, goal=goal, success_criteria="it is done")


def _situation(**over):
    base = {"services": [], "capabilities": [], "roots": [], "credentials": [],
            "profile": {}, "declared_intents": []}
    base.update(over)
    return base


def _profile_with_fact(**fact_over):
    """A profile whose single user_fact matches an ``account_id`` leaf by TAG."""
    fact = {"id": "fact_1", "ts": "2020", "fact": "account_id is acct-42",
            "tags": ["account_id"], "source": "operator", "confidence": 1.0}
    fact.update(fact_over)
    return {"name": "Op", "location_text": "NYC", "timezone": "UTC",
            "default_output_dir": "/out", "user_facts": [fact]}


def _bind_account_id(profile):
    """Bind an ``account_id`` leaf against ``profile``; return (requirement, report)."""
    situation = _situation(profile=profile)
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("api_call", {"account_id": {"type": "string",
                                            "description": "the account id"}})
    reqs = compute_requirements(_obj(), cap, situation, ctx)
    matches = [r for r in reqs if r.schema_path.endswith("account_id")]
    assert matches, "the required account_id leaf should produce a Requirement"
    report = build_requirement_report([_obj()], cap, situation, ctx)
    return matches[0], report


def _in_ask_bundle(report):
    return any(a.schema_path.endswith("account_id") for a in report.ask_bundle)


# ── PIN 1: the LEGACY corpus — an UNSTAMPED auto_extract fact ───────────────
#
# This is the pin that matters most operationally: every auto_extract fact already
# persisted in every operator vault carries origin_class=None. The reader-side clamp
# is what closes them, with no migration.

def test_legacy_unstamped_auto_extract_fact_is_confirm_gated_never_silent():
    """THE legacy pin. ``source="auto_extract"`` + ABSENT stamp — exactly the shape of
    every fact already on disk — must NOT inherit the ``operator`` grandfather."""
    req, report = _bind_account_id(
        _profile_with_fact(source="auto_extract", origin_class=None))

    assert req.source == "operator_profile"
    assert req.value_origin == "content_derived", (
        "an unstamped auto_extract fact is an LLM extraction from operator-DELIVERED "
        "text; grandfathering it to `operator` is the silent-bind exposure"
    )
    assert rb._needs_ask(req) is True, "an auto_extract bind can never be silent"
    assert _in_ask_bundle(report), (
        "the clamped profile bind must reach the operator's one-click confirm bundle"
    )


def test_legacy_unstamped_auto_extract_is_clamped_at_full_confidence():
    """Confidence must not rescue it. The extraction prompt asks for >= 0.9 and
    ``UserFact.confidence`` defaults to 1.0 — both well above ``T_HIGH`` (0.80), which
    is precisely why this bound SILENTLY before."""
    req, report = _bind_account_id(
        _profile_with_fact(source="auto_extract", origin_class=None, confidence=1.0))

    assert req.value_origin == "content_derived"
    assert rb._needs_ask(req) is True
    assert _in_ask_bundle(report)


def test_legacy_auto_extract_clamped_on_the_TEXT_match_path_too():
    """``_bind_profile`` matches on tag OR raw fact text (``kl in fact_txt``). An LLM
    PARAPHRASE therefore binds with NO matching tag at all — the likeliest real shape
    of an extracted fact. Pin the clamp on that path, not just the tidy tag path."""
    req, report = _bind_account_id(_profile_with_fact(
        fact="the account_id the user mentioned is acct-42",
        tags=[],                      # no tag hit — text match only
        source="auto_extract", origin_class=None))

    assert req.value_origin == "content_derived", (
        "the text-match path must clamp too, or a tagless paraphrase still launders"
    )
    assert rb._needs_ask(req) is True
    assert _in_ask_bundle(report)


# ── PIN 2: the FRESH writer stamp ──────────────────────────────────────────

def test_freshly_stamped_auto_extract_fact_is_confirm_gated_never_silent():
    """A fact written by the fixed extractor carries the stamp explicitly. It must bind
    identically to the clamped legacy one — the two pieces agree."""
    req, report = _bind_account_id(_profile_with_fact(
        source="auto_extract", origin_class="content_derived"))

    assert req.value_origin == "content_derived"
    assert rb._needs_ask(req) is True
    assert _in_ask_bundle(report)


def test_fact_extractor_stamps_content_derived_on_every_persisted_fact(
        tmp_path, monkeypatch):
    """WRITER pin, asserted against PERSISTED JSON (what run 2 reads back off disk),
    not the returned object — a stamp that reaches the model but not the writer is
    still a silent bind next run."""
    from sharing_on.config import Config
    from systemu.pipelines import fact_extractor as fe
    from systemu.vault.vault import Vault

    def fake_llm(*, tier, system, user, config, temperature=0.2, max_tokens=2000, **kw):
        return {"facts": [
            {"fact": "account_id is acct-42", "tags": ["account_id"],
             "confidence": 0.95},
            {"fact": "User lives in Bangalore", "tags": ["location"],
             "confidence": 0.9},
        ]}

    monkeypatch.setattr(fe, "llm_call_json", fake_llm)
    vault = Vault(str(tmp_path / "vault"))
    cfg = Config()
    cfg.openrouter_api_key = "sk-fake-for-test"

    n = fe.extract_from_chat(
        {"ts": "2026-07-18T00:00:00",
         "prompt": "here is an email I pasted: your account_id is acct-42",
         "status": "completed"},
        vault, cfg)
    assert n == 2, "both candidate facts should persist"

    path = pathlib.Path(vault.root) / "user_facts.jsonl"
    rows = [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 2
    for row in rows:
        assert row.get("source") == "auto_extract"
        assert row.get("origin_class") == "content_derived", (
            "extract_from_chat must stamp the taint on the way to user_facts.jsonl; "
            f"persisted={row!r}"
        )


def test_extracted_fact_round_trips_from_the_vault_to_a_gated_bind(
        tmp_path, monkeypatch):
    """END-TO-END, no hand-built fact dict: extract → persist → read back → bind. This
    is the pin that would have caught the exposure as originally executed."""
    from sharing_on.config import Config
    from systemu.pipelines import fact_extractor as fe
    from systemu.vault.vault import Vault

    def fake_llm(*, tier, system, user, config, temperature=0.2, max_tokens=2000, **kw):
        return {"facts": [{"fact": "account_id is acct-42",
                           "tags": ["account_id"], "confidence": 0.95}]}

    monkeypatch.setattr(fe, "llm_call_json", fake_llm)
    vault = Vault(str(tmp_path / "vault"))
    cfg = Config()
    cfg.openrouter_api_key = "sk-fake-for-test"
    fe.extract_from_chat(
        {"ts": "2026-07-18T00:00:00", "prompt": "pasted text mentioning acct-42",
         "status": "completed"}, vault, cfg)

    facts = vault.load_user_facts()
    assert len(facts) == 1
    profile = {"name": "Op", "location_text": "NYC", "timezone": "UTC",
               "default_output_dir": "/out",
               "user_facts": [f.model_dump() for f in facts]}

    req, report = _bind_account_id(profile)
    assert req.value_origin == "content_derived"
    assert rb._needs_ask(req) is True
    assert _in_ask_bundle(report)


# ── PIN 3: THE PAYOFF SURVIVES — operator-authored sources still silent-bind ─
#
# Without this pin a "clamp every unstamped fact" regression is GREEN on every pin
# above while destroying the feature: the whole point of the profile is that an
# operator-authored default resolves WITHOUT asking. The grandfather must survive
# for genuine operator surfaces.

_OPERATOR_AUTHORED_SOURCES = [
    "onboarding",      # welcome.save_onboarding / mark_skipped / tour
    "explicit_user",   # cli_commands.user_remember — operator types it verbatim
]


@pytest.mark.parametrize("source", _OPERATOR_AUTHORED_SOURCES)
def test_operator_authored_sources_still_bind_silently(source):
    """The payoff. An UNSTAMPED operator-surface fact keeps the grandfather: origin
    ``operator``, ``_needs_ask`` False, NOT in the ask_bundle.

    If this goes red, the clamp was written too broadly (source-blind) — it now asks
    the operator to re-confirm facts they typed themselves, which is the feature
    regression the narrow, source-specific clamp exists to avoid."""
    req, report = _bind_account_id(
        _profile_with_fact(source=source, origin_class=None))

    assert req.value_origin == "operator", (
        f"source={source!r} is operator-AUTHORED; clamping it destroys the profile "
        f"payoff (the operator gets re-asked for what they typed)"
    )
    assert req.state == "have"
    assert rb._needs_ask(req) is False, "an operator-authored fact must bind SILENTLY"
    assert not _in_ask_bundle(report), (
        "an operator-authored fact must NOT be pushed into the confirm bundle"
    )


def test_profile_spine_fields_still_bind_silently_as_operator():
    """The 4-field UserProfile spine is operator-typed in the onboarding wizard and
    carries no fact ``source`` at all. It must be untouched by a source-keyed clamp."""
    situation = _situation(profile={"name": "Op", "location_text": "NYC",
                                    "timezone": "UTC", "default_output_dir": "/out"})
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("writer", {"output_dir": {"type": "string",
                                          "description": "where to write"}})
    reqs = compute_requirements(_obj(), cap, situation, ctx)
    matches = [r for r in reqs if r.schema_path.endswith("output_dir")]
    assert matches, "the output_dir leaf should produce a Requirement"

    assert matches[0].value_origin == "operator"
    assert rb._needs_ask(matches[0]) is False


def test_an_unknown_source_keeps_the_grandfather():
    """SCOPE pin. The clamp is keyed to the ONE source string whose sole writer is the
    extractor. An unrelated/unknown source keeps the documented absent ⇒ operator
    grandfather, so this change stays the narrow fix it claims to be rather than a
    silent re-classification of every writer in the tree."""
    req, _report = _bind_account_id(
        _profile_with_fact(source="some_future_operator_surface", origin_class=None))

    assert req.value_origin == "operator"
    assert rb._needs_ask(req) is False


def test_explicit_stamp_still_wins_over_the_source_keyed_clamp():
    """The clamp fires only on an ABSENT stamp. A PRESENT stamp remains authoritative
    (and non-canonical values still fail untrusted via ``_coerce_origin``) — the clamp
    must not shadow a deliberate stamp from a future writer."""
    req, _ = _bind_account_id(_profile_with_fact(
        source="onboarding", origin_class="content_derived"))
    assert req.value_origin == "content_derived", "a present stamp is authoritative"

    req2, _ = _bind_account_id(_profile_with_fact(
        source="auto_extract", origin_class="operator"))
    assert req2.value_origin == "operator", (
        "an explicitly-stamped auto_extract fact reads its stamp — the clamp is a "
        "backstop for ABSENT stamps, not an override"
    )
