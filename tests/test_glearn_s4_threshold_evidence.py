# tests/test_glearn_s4_threshold_evidence.py
"""R-A16 G-LEARN slice 4 (§5.9) — why the per-class THRESHOLD delta was NOT built.

§5.9's Update clause asks for two things. The synonym half was built (see
``test_glearn_s4_learned_synonyms``). The threshold half — "lower that class's
confirm threshold toward the answered value" — was deliberately NOT built, and this
module is the executable evidence for that decision, so it is auditable rather than a
silent omission.

THE FINDING, established by RUNNING the producers rather than reading the spec.
A learned threshold delta can only change an outcome for a bind that is
CONFIDENCE-gated: not ``content_derived`` (a content_derived bind is surfaced by
``requirement_binder._needs_ask`` at ANY confidence — the load-bearing IMPL-5
invariant, which no threshold may override) and below the static ``T_HIGH``.

Every production ``UserFact`` writer emits either confidence 1.0 or
``content_derived``:

  ==================  ==========================  ==============  ==================
  writer              source                      confidence      origin_class
  ==================  ==========================  ==============  ==================
  cli user_remember   ``explicit_user``           1.0 (default)   absent -> operator
  welcome / tour      ``onboarding``              1.0 (default)   absent -> operator
  ``ask_promotion``   ``ask_promotion``           1.0 (literal)   answer's origin
  ``fact_extractor``  ``auto_extract``            LLM, sub-1.0    ``content_derived``
  ==================  ==========================  ==============  ==================

and the fixed-confidence inventory binds are either already at/above the line
(credentials @0.85, operator) or clamped (services/capabilities -> content_derived).
So the set of binds a lowered threshold could newly silent-bind is EMPTY.

WHY THAT MEANS "DO NOT BUILD", not "build it anyway for completeness":
  * it would add vault-backed state to a hot, must-never-raise bind path for zero
    present benefit;
  * an invisible tuned threshold is a debugging trap, and one that never fires is a
    trap with no upside;
  * it would entrench the FALSE model that asks are confidence-gated when they are
    taint-gated — the exact misreading a stale ``requirement_binder`` docstring has
    already caused once (see that module's "Do not 'fix' this" note).

WHAT WAS BUILT INSTEAD: ``replay_metrics._threshold_sensitive_counts`` — the trigger
evidence such a delta would need, surfaced in the shipped §5.9 report and CLI. If it
is ever non-zero on real runs, that is the signal the delta has become worth
building. These tests pin that it is not vacuous: it DOES count a confidence-gated
confirm when one exists.

NOTE: this module must not read source via ``inspect`` — ``conftest`` auto-tags a
whole module ``source_sensitive`` on the substring that call would introduce, which
would drop these pins out of the edit-safe tier. The substring is deliberately not
spelled out here: writing it even inside a comment is enough to trip the tagger (it
matches module TEXT, not code). ``inspect.signature`` is fine and is used below.
"""
from __future__ import annotations

import inspect
import json

import pytest

from systemu.runtime import replay_metrics as rm
from systemu.runtime import requirement_binder as rb


@pytest.fixture()
def vault(tmp_path):
    from systemu.vault.vault import Vault
    return Vault(str(tmp_path / "vault"))


# ── PART 1: the writer census, established by RUNNING each writer ────────────
def test_add_fact_default_confidence_is_1_0():
    """The three operator surfaces (cli ``user remember``, welcome, tour) pass no
    ``confidence=``, so they take this default — at/above ``T_HIGH`` already."""
    from systemu.runtime.user_profile import add_fact
    default = inspect.signature(add_fact).parameters["confidence"].default
    assert default == 1.0
    assert default >= rb.T_HIGH


def test_ask_promotion_writes_confidence_1_0(vault):
    """Driven through the REAL promoter, not an assumption about it."""
    from systemu.runtime.ask_promotion import _promote_fact
    uf = _promote_fact(vault, schema_path="repo", leaf="default_repo",
                       answer="acme/prod", origin_class="operator")
    assert uf.confidence == 1.0
    assert uf.confidence >= rb.T_HIGH


def test_fact_extractor_is_the_only_sub_1_0_writer_and_it_stamps_content_derived(
        vault, monkeypatch):
    """The one writer that CAN emit a sub-``T_HIGH`` confidence hard-stamps the
    untrusted axis, so ``_needs_ask`` surfaces it whatever the threshold is."""
    from systemu.pipelines import fact_extractor as fe
    monkeypatch.setattr(fe, "load_prompt", lambda *a, **k: "x", raising=False)
    monkeypatch.setattr(fe, "llm_call_json", lambda *a, **k: {
        "facts": [{"fact": "repo is acme/prod", "tags": ["default_repo"],
                   "confidence": 0.42}]}, raising=False)

    n = fe.extract_from_chat({"prompt": "my repo is acme/prod", "ts": "t1"},
                             vault, type("C", (), {})())
    assert n == 1
    facts = vault.get_user_facts() if hasattr(vault, "get_user_facts") else None
    if facts is None:
        from systemu.runtime.user_profile import get_facts
        facts = get_facts(vault)
    got = [f for f in facts if f.source == "auto_extract"]
    assert got, "the extractor persisted nothing"
    assert got[0].confidence == 0.42 < rb.T_HIGH
    assert got[0].origin_class == "content_derived", \
        "the only sub-T_HIGH writer must stay on the untrusted axis"


def test_legacy_auto_extract_rows_without_a_stamp_still_clamp():
    """The reader-side clamp closes the pre-slice-1 corpus with no migration, so even
    a legacy sub-``T_HIGH`` ``auto_extract`` row is taint-gated, not threshold-gated."""
    assert rb._fact_origin({"source": "auto_extract", "origin_class": None}) \
        == "content_derived"


@pytest.mark.parametrize("source", ["explicit_user", "onboarding"])
def test_operator_surfaces_keep_binding_silently(source):
    """The counterpart: operator-authored facts must NOT be clamped, or the profile
    stops paying off. They bind at confidence 1.0, above any threshold."""
    assert rb._fact_origin({"source": source, "origin_class": None}) == "operator"


def test_no_producer_reachable_bind_is_confidence_gated(vault):
    """THE HEADLINE PIN, driven through the REAL binder across every bind source.

    A bind is threshold-movable only when it is below ``T_HIGH`` AND not
    ``content_derived``. Using the confidence/origin pairs the writers above actually
    emit, that set is EMPTY — which is why the delta was not built.
    """
    from systemu.core.models import Objective

    cap = type("Cap", (), {"name": "w", "effect_tags": [], "parameters_schema": {
        "type": "object", "required": ["account_id", "default_repo", "api_key"],
        "properties": {"account_id": {"type": "string"},
                       "default_repo": {"type": "string"},
                       "api_key": {"type": "string"}}}})()

    class _Ctx:
        files_produced = []

    # every (confidence, source, stamp) combination the real writers can persist
    situations = [
        {"profile": {"user_facts": [{"id": "f", "fact": "repo is acme/prod",
                                     "tags": ["default_repo"], "confidence": conf,
                                     "source": src, "origin_class": stamp}]}}
        for conf, src, stamp in (
            (1.0, "explicit_user", None), (1.0, "onboarding", None),
            (1.0, "ask_promotion", "operator"),
            (1.0, "ask_promotion", "content_derived"),
            (0.42, "auto_extract", "content_derived"),
            (0.42, "auto_extract", None),          # legacy, reader-clamped
        )
    ]
    situations += [
        {"credentials": ["api_key"]},
        {"services": [{"name": "gh", "account": "acme", "has_live_token": True,
                       "origin_class": "operator"}]},
        {"capabilities": [{"name": "default_repo", "curated": True,
                           "origin_class": "operator"}]},
    ]

    movable = []
    for sit in situations:
        obj = Objective(id=1, goal="update the repo", success_criteria="done")
        for r in rb.compute_requirements(obj, cap, sit, _Ctx()):
            if (rb._needs_ask(r) and r.state != "missing"
                    and r.value_origin != "content_derived"
                    and r.confidence < rb.T_HIGH):
                movable.append((r.schema_path, r.source, r.confidence, r.value_origin))

    assert movable == [], (
        "a producer-reachable CONFIDENCE-gated bind now exists — the §5.9 threshold "
        f"delta may have become worth building: {movable}")


# ── PART 2: the counter that WOULD justify the delta is not vacuous ──────────
def _rec(resolution, origin, score, cls="input", path="p", matched=...):
    """One corpus row.

    ``matched`` defaults to what ``record_ask_avoidable`` really writes for each
    resolution, but is overridable: the corpus is a PLAINTEXT, hand-editable audit
    file, so an INCONSISTENT row (a non-confirmed resolution that still names a
    matched candidate) is a shape this reader must survive without counting.
    """
    if matched is ...:
        matched = "ref1" if resolution == "resolvable_confirmed" else None
    return {"ask_id": "a", "class": cls, "schema_path": path, "state": "resolvable",
            "source": "situation", "resolution": resolution,
            "matched_candidate": matched,
            "candidates": [{"ref": "ref1", "score": score, "value_origin": origin}]}


def test_counter_counts_a_genuinely_confidence_gated_confirm():
    """Not vacuous: a definitive confirm on a TRUSTED axis below T_HIGH DOES count."""
    out = rm._threshold_sensitive_counts([_rec("resolvable_confirmed", "operator", 0.5)])
    assert out["eligible_total"] == 1
    assert out["by_class"]["input"] == 1


def test_counter_ignores_content_derived_however_low_the_score():
    """The security-relevant rule: a taint-gated ask can never drive a threshold, so
    it must never even be counted as evidence for one."""
    out = rm._threshold_sensitive_counts(
        [_rec("resolvable_confirmed", "content_derived", 0.1)])
    assert out["eligible_total"] == 0


def test_counter_ignores_scores_already_at_or_above_t_high():
    out = rm._threshold_sensitive_counts(
        [_rec("resolvable_confirmed", "operator", rb.T_HIGH)])
    assert out["eligible_total"] == 0


@pytest.mark.parametrize("resolution", ["missing_answered", "resolvable_overridden"])
def test_only_the_definitive_sub_case_may_count(resolution):
    """``missing_answered`` is DIRECTIONAL only and ``resolvable_overridden`` means the
    ask was NECESSARY. Neither may be evidence for moving a confirm line — the
    directional-signal rule. (``missing_answered`` MAY drive a synonym, which only
    widens candidate scoring and is not a security decision.)"""
    out = rm._threshold_sensitive_counts([_rec(resolution, "operator", 0.5)])
    assert out["eligible_total"] == 0


@pytest.mark.parametrize("resolution", ["missing_answered", "resolvable_overridden",
                                        "", "totally-made-up"])
def test_resolution_gate_holds_even_when_a_row_names_a_matched_candidate(resolution):
    """The resolution gate must be load-bearing ON ITS OWN, not merely shadowed by the
    ``matched_candidate`` guard.

    The corpus is a plaintext audit file. A hand-edited or future-shaped row could
    carry a non-confirmed resolution AND a matched candidate; only the DEFINITIVE
    sub-case may ever be evidence for moving a confirm line, so it must still not
    count. (A surviving mutation of exactly this guard is what prompted this pin.)
    """
    out = rm._threshold_sensitive_counts(
        [_rec(resolution, "operator", 0.5, matched="ref1")])
    assert out["eligible_total"] == 0, \
        "a non-definitive resolution was counted as threshold evidence"


def test_counter_uses_the_static_t_high_not_a_tuned_one():
    """An eligibility test that referenced a lowered threshold would shrink its own
    input set and oscillate. Pin that it reports the static constant."""
    assert rm._threshold_sensitive_counts([])["t_high"] == rb.T_HIGH


@pytest.mark.parametrize("recs", [
    None, [], "not-a-list", [None], [{}], [{"resolution": "resolvable_confirmed"}],
    [{"resolution": "resolvable_confirmed", "matched_candidate": "x",
      "candidates": "nope"}],
    [{"resolution": "resolvable_confirmed", "matched_candidate": "ref1",
      "candidates": [{"ref": "ref1", "score": "not-a-number",
                      "value_origin": "operator"}]}],
])
def test_counter_never_raises_on_malformed_corpus(recs):
    out = rm._threshold_sensitive_counts(recs)
    assert isinstance(out, dict) and out["eligible_total"] == 0


# ── PART 3: the decision is VISIBLE in the shipped report + CLI ──────────────
def test_report_surfaces_the_threshold_evidence(vault):
    rep = rm.avoidable_ask_report(vault)
    ts = (rep.get("answer_linked") or {}).get("threshold_sensitive")
    assert isinstance(ts, dict)
    assert ts["eligible_total"] == 0
    assert ts["t_high"] == rb.T_HIGH


def test_formatter_explains_the_zero(vault):
    """A bare 0 is a debugging trap of its own — the report must say WHY it is 0."""
    text = "\n".join(rm.format_avoidable_ask(rm.avoidable_ask_report(vault)))
    assert "confidence-gated" in text.lower()
    assert "taint" in text.lower(), "the report must name taint as the real gate"


def test_formatter_reports_a_nonzero_count_without_the_explainer():
    lines = rm._format_threshold_sensitive(
        {"eligible_total": 3, "t_high": 0.80, "by_class": {"input": 3}})
    text = "\n".join(lines)
    assert "3" in text
    assert "dead machinery" not in text, \
        "the 'no evidence' explainer must not print once evidence exists"


def test_cli_debug_avoidable_ask_renders(vault, monkeypatch, tmp_path):
    """End-to-end through the shipped CLI surface, so the new blocks cannot be
    unreachable in the command the operator actually runs."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands as cc

    monkeypatch.setattr(cc, "_get_vault_and_config", lambda ctx: (None, vault))
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output
    assert "learned synonyms" in res.output.lower()
    assert "confidence-gated" in res.output.lower()
