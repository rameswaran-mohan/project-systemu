"""G-LEARN slice 3 (§5.9) — the PROMOTION slice: behavioural + security pins.

This module deliberately contains NO ``inspect.getsource`` call: ``tests/conftest.py``
auto-tags a WHOLE MODULE ``source_sensitive`` on that substring, which would drop every
pin here (the anti-laundering ones included) out of the edit-safe gate
(``pytest -m "not source_sensitive"``, GATE-TIER / DEC-14). The source-level pins live
in ``tests/test_glearn_s3_promotion_source.py``.

THE THREAT MODEL. S3 is the hop that would INTRODUCE the laundering bug: it is the
first writer on the promotion path, and every default along that path points at
laundering — ``add_fact(origin_class=...)`` defaults to ABSENT, and ABSENT
grandfathers to ``operator`` in ``requirement_binder._fact_origin``. So a promoter
that forgets one kwarg turns a page-derived value into a silently-bound trusted one.
The pins below are written against that specific failure.

FIXTURE REALISM. Every snapshot used here is produced by RUNNING the real producers
(``requirement_binder.build_requirement_report`` → ``replay_metrics.requirement_snapshot``),
never hand-built. ``_assert_realistic`` holds that line and is itself pinned.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from systemu.core.models import UserProfile
from systemu.runtime import ask_promotion as ap
from systemu.runtime import replay_metrics as rm
from systemu.runtime import requirement_binder as rb
from systemu.runtime import situational_inventory as si
from systemu.runtime import table_reconciler as tr
from systemu.runtime import table_store as ts
from systemu.runtime import user_profile as up


# ── the vault + run-context doubles (the shapes production actually passes) ───
class _Vault:
    """The vault surface the promotion path touches: ``.root`` (user_facts.jsonl,
    the table sidecars, the HMAC key derivation) and ``list_tools`` (the projector)."""

    def __init__(self, root):
        self.root = str(root)

    def list_tools(self, status=None):
        return []


class _Ctx:
    """The run context. Carries an EMPTY ``files_produced``.

    It used to carry a produced file, because ``_bind_run_context`` (source #2) was
    this module's ``content_derived`` channel on non-path leaves. That source is now
    PATH-ONLY (``requirement_binder._PATH_ONLY_SOURCES``): ungated it bound the first
    produced file into every leaf at 0.5, which measured 100% wrong on the harvested
    corpus and — because source #2 is ordered BEFORE the profile — masked exactly the
    silent operator-profile bind this slice exists to deliver. See ``_real_snaps`` for
    the channel that replaced it."""

    def __init__(self):
        self.files_produced = []


class _Obj:
    id = 1
    goal = "send the report"
    success_criteria = ""


#: The EXACT key set ``replay_metrics.requirement_snapshot`` emits. Pinned by
#: ``test_fixture_realism_guard_tracks_the_live_producer`` against a real run, so a
#: producer shape change breaks this module loudly instead of silently making every
#: pin here test a shape production never emits.
SNAPSHOT_KEYS = frozenset({
    "schema_path", "class", "state", "source", "value_origin", "confidence",
    "candidate_ref",
    # R-A16 F2: the CANONICAL-form twin of ``candidate_ref``. Introduced for the
    # observability comparison in ``record_ask_avoidable``; the promoter now reads it
    # too, as the THIRD witness in the origin decision (see PIN 2b). It is consulted
    # only where the answer matched no exact digest and the field was not explicitly
    # picked — the branch that previously fell through to ``operator`` and laundered a
    # reshaped confirm onto the trusted axis. The two ref shapes stay disjoint by
    # length, so ``_is_value_ref`` still rejects a canonical digest outright and
    # neither field can ever stand in for the other.
    "candidate_canon_ref",
})


def _assert_realistic(snaps):
    """Reject any snapshot shape the real producer never emits (the F1 meta-lesson:
    two defects shipped because tests used shapes production never produces)."""
    assert snaps, "empty snapshot list — the producer did not run, so the pin is vacuous"
    for s in snaps:
        assert isinstance(s, dict), f"not a snapshot dict: {s!r}"
        assert set(s) == SNAPSHOT_KEYS, (
            f"synthetic snapshot shape {sorted(s)} — the real "
            f"requirement_snapshot emits exactly {sorted(SNAPSHOT_KEYS)}")
        cand = s["candidate_ref"]
        assert cand is None or rm._is_value_ref(cand), (
            f"candidate_ref {cand!r} is not the keyed shape value_ref emits")
    return snaps


#: The ``UserFact.source`` of the fixture SEED fact (see ``_real_snaps``). This is a
#: REAL production source string — ``fact_extractor.extract_from_chat`` is its sole
#: writer — chosen because ``requirement_binder._fact_origin`` carves it out
#: DETERMINISTICALLY to ``content_derived``. Nothing here hand-stamps a taint.
_SEED_SOURCE = "auto_extract"


def _seed_text(value) -> str:
    """The seed fact's SENTENCE. Deliberately contains the VALUE and no leaf-key token.

    ``_bind_profile`` matches a user_fact by ``tags`` OR by ``key in fact_text`` and
    returns the FIRST match, while ``get_facts`` is newest-LAST. A seed naming the leaf
    would therefore SHADOW the promotion under test at every ``_rebind`` — the
    promoted fact would never be reached and the re-bind pins would assert against the
    fixture instead of against the slice. Untagged + leaf-free keeps the seed inert to
    source #4 while still populating the ``content_derived`` corpus source #0 reads."""
    return f"observed {value} in fetched content"


def _promoted(vault, **kw):
    """The facts THIS SLICE wrote, i.e. excluding the fixture seed.

    ``_real_snaps`` writes one ``auto_extract`` seed fact per call to manufacture the
    binder's ``content_derived`` candidate, so a bare ``get_facts`` now counts fixture
    material alongside promotions. Filtering on the promoter's OWN source keeps every
    accounting pin measuring exactly what it measured before the re-channel — this is
    fixture bookkeeping, not a weakened assertion."""
    return [f for f in up.get_facts(vault, **kw) if f.source == ap.PROMOTION_SOURCE]


def _inventory_snaps(vault, leaf, value):
    """The SECOND real ``content_derived`` channel: a scanned inventory SERVICE entry.

    ``_bind_inventory_entry`` (source #3) binds an account/identity/service-named leaf
    from a live-token service entry and derives the taint with ``_entry_origin``, which
    clamps a scanned entry to ``content_derived`` — with a resolved value, so a real
    digest. Used where the seed fact of the source-#0 channel would itself perturb what
    the pin measures: this path writes NOTHING to the vault, so ``get_facts`` pins keep
    their exact original form."""
    rep = rb.build_requirement_report(
        [_Obj()], {leaf: {"type": "string"}},
        {"services": [{"name": "crm", "account": value, "has_live_token": True}]},
        _Ctx(), vault=vault)
    return [s for r in rep.ask_bundle if (s := rm.requirement_snapshot(r))]


def _real_snaps(vault, schema, *, situation=None, candidate_value=None):
    """Run the REAL binder + REAL snapshot producer. Never hand-builds a snapshot.

    THE CANDIDATE CHANNEL. ``candidate_value`` makes the binder hold ``value`` as a
    ``content_derived`` candidate for every leaf of ``schema``. It does that through
    source #0's content-seeded provided-params clamp
    (``requirement_binder._provided_value_is_content_seeded``): a ``content_derived``
    user_fact whose sentence CONTAINS the value, plus the same value arriving in
    ``provided_params``, is read as "the model lifted this out of tainted content we
    put in front of it" and clamps to ``content_derived`` at confidence 1.0 — with a
    resolved value, hence a real ``candidate_ref``.

    WHY NOT ``ctx.files_produced`` (source #2), which this module used to use. That
    source is now PATH-ONLY. It was also never production-realistic for these leaves:
    the only production writers of ``files_produced`` go through
    ``collect_artifact_paths``, which emits resolved ABSOLUTE paths of files that exist
    on disk — so ``["acme-crm"]`` and bare relative paths were shapes production can
    never emit, binding leaves like ``recipient`` and ``service`` with a file path.

    THIS channel is production-realistic on both halves: ``build_requirement_report``
    is called with ``provided_params=decision.get("parameters")`` at the real tool-call
    seam (``shadow_runtime``), and ``auto_extract`` facts are what ``fact_extractor``
    really writes."""
    if candidate_value is not None:
        # The seed must be VISIBLE to the binder, and it is read off
        # ``situation["profile"]`` — not off the vault. A caller supplying its own
        # situation would therefore seed a fact the clamp never sees, and the candidate
        # would quietly bind ``systemu_authored`` instead of ``content_derived``: every
        # pin here would still "pass" while testing nothing. Refuse loudly instead.
        assert situation is None, (
            "pass either `situation` or `candidate_value`, not both — a caller-supplied "
            "situation hides the seed fact from the content_derived clamp and silently "
            "degrades every pin that depends on it")
        up.add_fact(vault, _seed_text(candidate_value), source=_SEED_SOURCE)
        situation = {"profile": si.build_profile(vault)}
        provided = {leaf: candidate_value for leaf in schema}
    else:
        provided = None
    rep = rb.build_requirement_report(
        [_Obj()], schema, situation or {}, _Ctx(), vault=vault,
        provided_params=provided)
    out = []
    for r in rep.ask_bundle:
        snap = rm.requirement_snapshot(r)
        if snap:
            out.append(snap)
    return out


def _dctx(snaps, request_id="hreq_abc12345"):
    """The decision-context shape ``_build_bundled_scope_card`` really stamps."""
    return {"request_id": request_id, "spec": {"requirement_snapshot": list(snaps)}}


def _rebind(vault, schema, *, situation=None):
    """Re-bind the same goal against the promoted profile. Returns
    ``(requirement, in_ask_bundle)`` for the single leaf."""
    if situation is None:
        situation = {"profile": si.build_profile(vault)}
    rep = rb.build_requirement_report([_Obj()], schema, situation, _Ctx(), vault=vault)
    reqs = [r for rs in rep.per_objective.values() for r in rs]
    assert len(reqs) == 1, f"expected one leaf, got {[r.schema_path for r in reqs]}"
    asked = {r.schema_path for r in rep.ask_bundle}
    return reqs[0], (reqs[0].schema_path in asked)


SCHEMA = {"recipient": {"type": "string"}}


# ══ PIN 1 — promote→rebind, BOTH directions (the point of the slice) ═════════
def test_content_derived_answer_promotes_tainted_and_rebinds_confirm_gated(tmp_path):
    """(a) The operator CONFIRMS the binder's content_derived candidate. The promoted
    profile entry must carry the value's ORIGINAL origin, so the next identical goal
    re-binds ``content_derived`` and stays in the ask_bundle (one-click confirm) —
    never a silent trusted bind. This is IMPL-5 "taint travels" across the promotion."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    # positive control: the binder really did hold a content_derived candidate
    assert snaps[0]["value_origin"] == "content_derived"
    assert snaps[0]["candidate_ref"] is not None

    # the operator confirms exactly what the binder held
    n = ap.promote_answered_asks(v, _dctx(snaps), {"recipient": "out/draft.md"})
    assert n == 1, "the confirmed answer was not promoted"

    facts = _promoted(v)
    assert len(facts) == 1
    assert facts[0].origin_class == "content_derived", (
        "LAUNDERING: a content_derived value was promoted as trusted")
    assert facts[0].source == ap.PROMOTION_SOURCE
    # The TAG is the join key `_bind_profile` matches on, and it is the mechanism this
    # slice relies on. The fact SENTENCE happens to contain the leaf too, so a text
    # match currently masks a missing tag — mutation testing caught that the tag was
    # therefore unpinned. Assert it structurally.
    assert facts[0].tags == ["recipient"], (
        "the promoted fact must carry the leaf as its tag — the text-match fallback "
        "is incidental, not the contract")
    assert facts[0].source_ref == "recipient", "dedupe key (schema_path) not recorded"

    req, asked = _rebind(v, SCHEMA)
    assert req.value_origin == "content_derived", (
        "LAUNDERING at re-bind: the promoted taint did not survive the profile hop")
    assert asked, ("a content_derived bind must stay in the ask_bundle "
                   "(one-click confirm), never silent-bind")


def test_freshly_typed_operator_answer_promotes_trusted_and_rebinds_SILENTLY(tmp_path):
    """(b) THE INVERSE — and the reason this pin exists. Without it, a
    "clamp everything to content_derived" promoter looks perfectly safe while
    destroying the entire payoff of the slice: the operator would be re-asked forever
    for a value THEY typed. A freshly-typed answer is operator-origin and MUST
    silent-bind on the next run."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    assert snaps[0]["value_origin"] == "content_derived"

    # the operator OVERRIDES the candidate with their own typed value
    n = ap.promote_answered_asks(v, _dctx(snaps), {"recipient": "ops@acme.com"})
    assert n == 1

    facts = _promoted(v)
    assert facts[0].origin_class == "operator", (
        "OVER-TAINT: the operator's own typed value was recorded as untrusted — "
        "the next run would re-ask for something they just typed")

    req, asked = _rebind(v, SCHEMA)
    assert req.value_origin == "operator"
    assert req.state == "have"
    assert not asked, (
        "the payoff is gone: an operator-typed promoted value must bind SILENTLY "
        "on the next identical goal (ask → resolve)")


# ══ PIN 2 — fail-closed on a missing digest ══════════════════════════════════
def test_missing_candidate_digest_promotes_NOTHING(tmp_path):
    """No candidate digest ⇒ NO promotion at all. Stamping ``operator`` here IS the
    laundering bug (the binder may well have held a page-derived value); stamping
    ``content_derived`` over-taints the operator's own typing. The only correct
    answer is to promote nothing."""
    v = _Vault(tmp_path)
    # A bind whose source binds an IDENTIFIER, not an extractable value: a user_fact
    # binds as ``profile_fact:<id>`` with resolved_value=None ⇒ candidate_ref is None.
    # It must be content_derived, or `_needs_ask` lets it silent-bind and it never
    # reaches an ask at all (the operator-origin variant is the INVERSE pin above).
    up.add_fact(v, "recipient is ops@acme.com", source="operator_chat",
                tags=["recipient"], origin_class="content_derived")
    snaps = _assert_realistic(
        _real_snaps(v, SCHEMA, situation={"profile": si.build_profile(v)}))
    # positive control: this really is the no-digest path, and it really did surface
    assert snaps[0]["candidate_ref"] is None, "not exercising the no-digest path"
    assert snaps[0]["source"] == "operator_profile", "not the profile_fact bind"

    before = len(up.get_facts(v))
    n = ap.promote_answered_asks(v, _dctx(snaps), {"recipient": "ops@acme.com"})
    assert n == 0, "fail-closed violated: promoted without a candidate digest"
    assert len(up.get_facts(v)) == before, "a fact was written on the fail-closed path"


def test_unkeyable_vault_promotes_NOTHING(tmp_path, monkeypatch):
    """The other half of fail-closed: the ANSWER cannot be digested (no per-vault key
    ⇒ ``value_ref`` returns None). With no comparable ref there is no origin decision
    to make, so nothing may be promoted.

    Drives the REAL fail-closed path (``_ref_key`` raises on an unusable per-vault
    secret) rather than stubbing ``value_ref`` itself, and asserts the negative control
    is genuine before relying on it."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    assert snaps[0]["candidate_ref"] is not None

    import systemu.runtime.dashboard_auth as da
    monkeypatch.setattr(da, "session_secret", lambda root: "")   # too short ⇒ raises
    rm._REF_KEY_CACHE.pop(str(v.root), None)                     # defeat the per-root cache

    # POSITIVE CONTROL: the vault really is unkeyable now (a vacuous control here
    # would let this pin pass against a promoter that ignores the failure entirely).
    assert rm.value_ref("anything", v) is None, "the negative control is not real"

    assert ap.promote_answered_asks(v, _dctx(snaps),
                                    {"recipient": "out/draft.md"}) == 0
    assert not _promoted(v), "promoted despite an undigestable answer"


def test_most_tainted_candidate_wins_a_multi_match(tmp_path):
    """When several candidates for one ``schema_path`` match the answer with DIFFERING
    origins, the promotion takes the MOST-TAINTED. Deliberately NOT the
    highest-confidence collapse ``replay_metrics`` uses for its own metric: confidence
    is the wrong axis here and is itself a laundering vector (a high-confidence
    ``content_derived`` candidate would win and be stamped trusted)."""
    v = _Vault(tmp_path)
    ref = rm.value_ref("out/draft.md", v)
    assert ref is not None
    # a real two-candidate group: same path + same digest, different origins. Built by
    # running the producer twice under different situations, so both entries are real.
    a = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))[0]
    b = dict(a)
    b["value_origin"] = "operator"
    group = [b, a]                      # trusted first — order must not decide
    assert a["value_origin"] == "content_derived"

    n = ap.promote_answered_asks(v, _dctx(group), {"recipient": "out/draft.md"})
    assert n == 1
    assert _promoted(v)[0].origin_class == "content_derived", (
        "a multi-candidate match must resolve to the MOST-TAINTED origin")


def test_a_POISONED_value_origin_in_the_stamp_fails_UNTRUSTED(tmp_path):
    """The card spec is persisted in PLAINTEXT and re-read here, so ``value_origin``
    arrives as untrusted input — a hand-edited or tampered stamp can carry any string.
    An unknown origin must fail UNTRUSTED (``content_derived``), never to a trusted
    axis, or editing one JSON field turns a page-derived value into a silent bind.

    This snapshot is deliberately NOT ``_assert_realistic``-clean on ``value_origin``:
    modelling TAMPERING is the point. Its key SET is still the producer's, so it is a
    real persisted shape carrying a hostile value — not an invented shape."""
    v = _Vault(tmp_path)
    real = _real_snaps(v, SCHEMA, candidate_value="out/draft.md")[0]
    assert set(real) == SNAPSHOT_KEYS                      # still the producer's shape

    for poison in ("trusted", "OPERATOR", "", None, "operator ", 7, ["operator"]):
        vv = _Vault(tmp_path / f"p{abs(hash(str(poison)))}")
        snap = {**_real_snaps(vv, SCHEMA, candidate_value="out/draft.md")[0],
                "value_origin": poison}
        n = ap.promote_answered_asks(vv, _dctx([snap]), {"recipient": "out/draft.md"})
        assert n == 1, f"the poisoned-origin case did not run for {poison!r}"
        got = _promoted(vv)[0].origin_class
        assert got == "content_derived", (
            f"a poisoned value_origin {poison!r} was promoted as {got!r} — an unknown "
            f"origin must fail UNTRUSTED")


# ══ PIN 2b — the RESHAPED confirm (R-A16 F2's canonical witness, applied here) ══
#
#  THE DEFECT. The origin decision had exactly two witnesses: exact digest equality,
#  and R-B4/F3's explicit pick marker. An operator who CONFIRMS the binder's
#  content_derived candidate through a form that merely reshapes it — a widget adding
#  quotes, a trailing period, a URL-encoded separator — matches neither, so the answer
#  read as "the operator TYPED something new" and promoted as ``operator``: the TRUSTED
#  axis. The file already named this "a laundering path for a scraped value"; what it
#  lacked was the witness to close it.
#
#  WHY IT MATTERS MORE HERE THAN IN THE SIBLING METRIC. In ``replay_metrics`` the same
#  miscomparison costs a mislabelled row. Here it costs the CONFIRM GATE: a promoted
#  ``operator`` fact binds at confidence 1.0 ≥ T_high, so every later run silent-binds
#  a value that was scraped out of fetched content — measured below, both directions.
#
#  Each reshape is chosen to be PLATFORM-INDEPENDENT: ``normalize_value`` normcases a
#  path on Windows, so a case/separator reshape would already fold at the EXACT digest
#  there and these pins would pass on Windows against an unfixed promoter.
_RESHAPED_CONFIRMS = [
    ('"out/draft.md"', "surrounding double quotes (widget/JSON round-trip)"),
    ("out/draft.md.", "a trailing period"),
    ("out/draft.md,", "a trailing comma"),
    ("out%2Fdraft.md", "a URL-encoded separator"),
]

#: The line the canonical witness must not cross. Each is a GENUINELY different value
#: reachable by a substring-style fold; promoting any of them as ``content_derived``
#: would re-ask the operator forever for something they really did type — destroying
#: the payoff PIN 1(b) exists to protect.
_STILL_OVERRIDES = [
    ("out/other.md", "a sibling file"),
    ("out", "a PREFIX of the candidate"),
    ("draft.md", "a SUFFIX of the candidate"),
    ("out/draft", "the extension dropped"),
]


@pytest.mark.parametrize("answer,label", _RESHAPED_CONFIRMS,
                         ids=[l for _, l in _RESHAPED_CONFIRMS])
def test_a_RESHAPED_confirm_is_not_laundered_into_a_trusted_silent_bind(
        tmp_path, answer, label):
    """THE FIX. The operator confirmed the binder's own content_derived candidate; only
    its FORM differs. It must keep the value's taint, and must still be confirm-gated
    on the next run."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    # positive controls: the binder really held a TAINTED candidate, it really stamped
    # the canonical twin, and the answer really is a form-only reshape of it.
    assert snaps[0]["value_origin"] == "content_derived"
    assert snaps[0]["candidate_canon_ref"] is not None, (
        "the binder did not stamp the canonical twin — this pin would be vacuous")
    assert rm.canonical_compare_form(answer) == rm.canonical_compare_form("out/draft.md")

    assert ap.promote_answered_asks(v, _dctx(snaps), {"recipient": answer}) == 1
    assert _promoted(v)[0].origin_class == "content_derived", (
        f"LAUNDERING via {label}: a scraped value the operator merely RESHAPED was "
        f"promoted as operator-trusted")

    req, asked = _rebind(v, SCHEMA)
    assert req.value_origin == "content_derived"
    assert asked, (
        f"LAUNDERING at re-bind via {label}: the confirm gate is gone — every later "
        f"run now silent-binds a value that came out of fetched content")


@pytest.mark.parametrize("answer,label", _STILL_OVERRIDES,
                         ids=[l for _, l in _STILL_OVERRIDES])
def test_a_genuinely_different_answer_still_promotes_TRUSTED(tmp_path, answer, label):
    """The negative half, and the more important one. Over-tainting is not "the safe
    direction" here: it re-asks the operator forever for what they typed, which is
    precisely the payoff PIN 1(b) protects. The canonical witness must stay a TOTAL
    rewrite compared with ``==``, never a containment test."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    assert ap.promote_answered_asks(v, _dctx(snaps), {"recipient": answer}) == 1
    assert _promoted(v)[0].origin_class == "operator", (
        f"OVER-TAINT via {label}: a value the operator genuinely typed was recorded "
        f"untrusted — the canonical witness has collapsed into a substring match")
    _req, asked = _rebind(v, SCHEMA)
    assert not asked, f"the payoff is gone for {label}: it no longer silent-binds"


def test_a_canonical_ref_signed_by_ANOTHER_vault_key_is_not_a_witness(tmp_path):
    """Key-rotation / cross-vault OUTCOME, mirroring the one the exact ref already has.

    A canonical digest signed under a different key is incomparable, not evidence, and
    must never produce a match.

    WHAT THIS DOES AND DOES NOT HOLD. It pins the OUTCOME, not the key-id guard inside
    ``_comparable_canon_ref``: deleting that guard leaves this green, because a foreign
    key yields a different MAC and the exact ``==`` fails anyway. Measured — the guard
    is redundant for the match decision and is kept as defence-in-depth, matching the
    structure ``record_ask_avoidable`` uses on the same field. Do not read this test as
    protection for the guard itself."""
    v = _Vault(tmp_path)
    other = _Vault(tmp_path / "other_vault")
    snaps = _real_snaps(v, SCHEMA, candidate_value="out/draft.md")
    foreign = rm.canonical_value_ref("out/draft.md", other)
    assert foreign is not None
    assert rm._ref_key_id(foreign) != rm._ref_key_id(rm.value_ref("x", v)), (
        "the two vaults derived the same key — this pin would be vacuous")
    snap = {**snaps[0], "candidate_canon_ref": foreign}
    assert set(snap) == SNAPSHOT_KEYS                    # still the producer's shape

    assert ap.promote_answered_asks(v, _dctx([snap]), {"recipient": '"out/draft.md"'}) == 1
    assert _promoted(v)[0].origin_class == "operator", (
        "a foreign-keyed canonical digest was accepted as a witness")


@pytest.mark.parametrize("bad", [
    "out/draft.md",                                  # a RAW value, not a digest
    "hmac256:" + "0" * 8 + ":" + "0" * 16,           # the EXACT-ref shape, not canonical
    "hmac256c:" + "0" * 8 + ":" + "0" * 15,          # right prefix, wrong length
    "hmac256c:zzzzzzzz:" + "0" * 16,                 # non-hex key id
    "", None, 7, ["hmac256c"], {"a": 1},
])
def test_a_MALFORMED_canonical_ref_is_not_a_witness(tmp_path, bad):
    """``candidate_canon_ref`` rides a persisted card spec across a suspend, so it is
    attacker-shaped input exactly as ``candidate_ref`` is. Only the emitted shape may
    ever act as a witness; anything else is dropped, never guessed at.

    The EXACT-ref shape is in this list deliberately: the two schemes are disjoint by
    length, and accepting one where the other belongs is how a shape-confusion bug
    would launder a value.

    SAME CAVEAT as the cross-key pin above — this holds the OUTCOME, not the shape
    guard. Deleting ``_is_canonical_ref`` from ``_comparable_canon_ref`` leaves this
    green: none of these shapes can equal a well-formed ``ans_canon`` under an exact
    comparison, so the guard cannot change the result. It is belt-and-braces, kept for
    symmetry with the sibling recorder and against a future non-exact comparison."""
    v = _Vault(tmp_path / f"v{abs(hash(str(bad)))}")
    snaps = _real_snaps(v, SCHEMA, candidate_value="out/draft.md")
    snap = {**snaps[0], "candidate_canon_ref": bad}

    assert ap.promote_answered_asks(v, _dctx([snap]),
                                    {"recipient": '"out/draft.md"'}) == 1
    assert _promoted(v)[0].origin_class == "operator", (
        f"a malformed canonical ref {bad!r} was accepted as a witness")


def test_an_ABSENT_canonical_ref_still_promotes_and_does_not_fail_closed(tmp_path):
    """Legacy compatibility. Cards stamped before the canonical twin existed carry no
    ``candidate_canon_ref``; they must keep working exactly as they did (exact-digest
    witness only), not start refusing to promote."""
    v = _Vault(tmp_path)
    snaps = _real_snaps(v, SCHEMA, candidate_value="out/draft.md")
    legacy = {**snaps[0], "candidate_canon_ref": None}

    # the EXACT witness still fires on an exact confirm
    assert ap.promote_answered_asks(v, _dctx([legacy]),
                                    {"recipient": "out/draft.md"}) == 1
    assert _promoted(v)[0].origin_class == "content_derived", (
        "a legacy card without the canonical twin stopped honouring the exact witness")


def test_the_explicit_pick_still_widens_to_EVERY_candidate(tmp_path):
    """R-B4/F3's marker is the BROADER witness — it says a suggestion was taken but not
    which one, so it resolves to ``_most_tainted`` over every comparable candidate. The
    canonical witness names ONE candidate, so it must not narrow the picked case and
    quietly make a picked answer LESS tainted than it was before this change.

    THE TWO CANDIDATES MUST DIFFER IN BOTH VALUE AND ORIGIN, and that is what makes this
    pin real rather than decorative. A first attempt gave both snapshots the same
    digests and differed only in ``value_origin``: the canonical witness then matched
    BOTH candidates, ``_most_tainted`` collapsed them to the same answer either way, and
    disabling the pick branch entirely still passed. Here the reshaped answer confirms
    ONLY the ``operator`` candidate, so pick-first (⇒ ``content_derived``, the taint of
    the OTHER candidate) and canonical-first (⇒ ``operator``) give different results and
    the ordering is actually observable."""
    v = _Vault(tmp_path)
    picked_val, other_val = "out/other.md", "out/draft.md"
    tainted = _assert_realistic(
        _real_snaps(v, SCHEMA, candidate_value=other_val))[0]
    assert tainted["value_origin"] == "content_derived"
    # a second REAL candidate for the same path: genuinely computed digests under this
    # same vault key, carrying the TRUSTED origin
    trusted = {**tainted,
               "candidate_ref": rm.value_ref(picked_val, v),
               "candidate_canon_ref": rm.canonical_value_ref(picked_val, v),
               "value_origin": "operator"}
    assert trusted["candidate_ref"] != tainted["candidate_ref"]
    group = _assert_realistic([tainted, trusted])

    # the answer is a reshaped confirm of the TRUSTED candidate only
    n = ap.promote_answered_asks(v, _dctx(group), {"recipient": f'"{picked_val}"'},
                                 picked=["recipient"])
    assert n == 1
    assert _promoted(v)[0].origin_class == "content_derived", (
        "a PICKED answer resolved to less taint than _most_tainted over every "
        "candidate — the canonical witness narrowed the pick, which is a REGRESSION "
        "in the trusting direction on a security decision")


def test_without_a_pick_the_canonical_witness_credits_the_matched_candidate(tmp_path):
    """The control for the pin above: the SAME two-candidate group and the SAME answer,
    differing only in that no pick marker arrives. Now the canonical witness is the only
    inferred witness and it credits the candidate it actually matched.

    Without this control the pin above would also pass against a promoter that ignored
    the canonical witness completely and simply always widened."""
    v = _Vault(tmp_path)
    picked_val, other_val = "out/other.md", "out/draft.md"
    tainted = _real_snaps(v, SCHEMA, candidate_value=other_val)[0]
    trusted = {**tainted,
               "candidate_ref": rm.value_ref(picked_val, v),
               "candidate_canon_ref": rm.canonical_value_ref(picked_val, v),
               "value_origin": "operator"}

    n = ap.promote_answered_asks(v, _dctx([tainted, trusted]),
                                 {"recipient": f'"{picked_val}"'})
    assert n == 1
    assert _promoted(v)[0].origin_class == "operator", (
        "the canonical witness credited the wrong candidate — it must name the one it "
        "matched, not collapse over all of them the way an explicit pick does")


def test_the_secret_and_spine_guards_FAIL_CLOSED_when_their_import_breaks(tmp_path,
                                                                          monkeypatch):
    """Both guards resolve their rule from another module at call time and both answer
    "refuse" if that import fails. Nothing else exercises that branch, so without this
    pin either could be flipped to fail-OPEN and every other test would stay green."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))

    monkeypatch.delattr(rm, "_is_secret_path")
    assert ap._is_secret("recipient", "input") is True, (
        "the secret guard failed OPEN when its import broke")

    monkeypatch.delattr(rb, "_PROFILE_SPINE")
    assert ap._collides_with_profile_spine("recipient") is True, (
        "the spine guard failed OPEN when its import broke")

    # ...and the third guard, which resolves the shipped value-level detector the same
    # way. Failing OPEN here would egress a credential into a system prompt.
    import systemu.messaging.gateway as _gw
    monkeypatch.delattr(_gw, "mask_outbound")
    assert ap._value_is_secret("anything at all") is True, (
        "the value-secret guard failed OPEN when its import broke")

    assert ap.promote_answered_asks(v, _dctx(snaps),
                                    {"recipient": "out/draft.md"}) == 0
    assert not _promoted(v)


def test_an_empty_leaf_is_refused(tmp_path):
    """A schema_path that yields no leaf token has no usable tag, so the promoted fact
    could never be re-bound — and an empty key matches the spine loop's substring test
    against every field. Refused."""
    assert ap._collides_with_profile_spine("") is True


# ══ PIN 3 — the projector tick ═══════════════════════════════════════════════
def test_learned_card_survives_a_reconcile_tick(tmp_path):
    """An item written straight to ``items.json`` is GONE after one ``reconcile_once``
    (the reconciler is that file's sole writer and re-projects from scratch). So a
    learned card needs its OWN sidecar merged inside ``project()`` — this pin fails if
    a future refactor drops the merge."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    assert ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"}) == 1

    learned = ts.load_learned_items(v)
    assert len(learned) == 1, "no learned TableItem was materialized"
    assert learned[0].provenance == "learned"
    assert learned[0].origin_class == "content_derived"
    assert learned[0].status == "suggested", (
        "a §5.9 proposal enters the tray as `suggested`; it is never auto-confirmed")

    tr.reconcile_once(v)
    keys = {ts.ref_key(i.kind, i.ref) for i in ts.load_items(v)}
    # keyed on the LEAF, never on the answer value (F2 part 2 / F5): the card is
    # "what the profile learned for this slot", so a changed answer heals it in place
    # instead of minting a second card.
    assert ts.ref_key("service", {"server": "service"}) in keys, (
        "the learned card did not survive the projector tick")


def test_tombstoned_ref_is_never_re_promoted(tmp_path):
    """The operator removed it. A learned card must never resurrect it — that is the
    "no re-add flapping" rule, and the reason removal writes a durable tombstone."""
    v = _Vault(tmp_path)
    key = ts.ref_key("service", {"server": "service"})       # leaf-keyed (F2/F5)
    ts.add_tombstone(v, key)

    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"})

    assert not ts.load_learned_items(v), "a tombstoned ref was re-promoted as a card"
    tr.reconcile_once(v)
    assert key not in {ts.ref_key(i.kind, i.ref) for i in ts.load_items(v)}


def test_removing_a_learned_card_survives_the_next_tick(tmp_path):
    """The READ-side half of the tombstone rule, and the realistic ordering: the card
    is learned FIRST, the operator removes it AFTERWARDS. The sidecar row still exists
    at that point, so only the merge-side skip in ``project()`` can stop the next tick
    from re-adding it. Without this, ``add_learned_item``'s write-side refusal is the
    only defence and a removed card comes straight back."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    assert ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"}) == 1
    key = ts.ref_key("service", {"server": "service"})       # leaf-keyed (F2/F5)
    tr.reconcile_once(v)
    assert key in {ts.ref_key(i.kind, i.ref) for i in ts.load_items(v)}   # control

    ts.add_tombstone(v, key)             # the operator removes it
    assert ts.load_learned_items(v), "precondition: the sidecar row still exists"

    tr.reconcile_once(v)
    assert key not in {ts.ref_key(i.kind, i.ref) for i in ts.load_items(v)}, (
        "a removed learned card came back on the next reconcile tick (re-add flapping)")


def test_learned_card_never_overrides_an_operator_declaration(tmp_path):
    """``load_operator_items`` force-stamps ``operator``/``operator_added``. A learned
    card at the same ref_key must not shadow or duplicate the operator's own
    declaration — the operator loop runs first and wins."""
    v = _Vault(tmp_path)
    # named for the LEAF, so it lands on the same ref_key the learned card now takes —
    # which is the collision this pin exists to exercise.
    ts.add_operator_item(v, ts.make_operator_item("service", "service"))
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"})

    items = tr.project(v)
    match = [i for i in items if ts.ref_key(i.kind, i.ref)
             == ts.ref_key("service", {"server": "service"})]
    assert len(match) == 1, "duplicate card: the learned merge did not defer"
    assert match[0].provenance == "operator_added"
    assert match[0].origin_class == "operator"


def test_learned_card_defers_to_an_operator_item_whose_id_is_STALE(tmp_path):
    """Why the learned merge checks ``operator_keys`` and not only ``setdefault``.

    ``setdefault`` dedupes on ``id``; the tombstone/ownership rules are keyed on
    ``ref_key``. Those normally agree (``make_operator_item`` derives the id from the
    ref_key) — so mutating either guard away alone leaves every other pin green. They
    STOP agreeing for a hand-edited or legacy ``operator_items.json`` row whose stored
    id no longer matches its ref: then ``setdefault`` sees two different ids and emits
    a DUPLICATE card for one thing the operator already declared. This is the case that
    makes the ref_key-based guard independently killable."""
    v = _Vault(tmp_path)
    stale = ts.make_operator_item("service", "service")   # same ref_key as the card
    stale.id = "ti_stale_legacy_id"                    # id no longer derives from ref
    ts.save_operator_items(v, [stale])

    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"})

    key = ts.ref_key("service", {"server": "service"})
    cards = [i for i in tr.project(v) if ts.ref_key(i.kind, i.ref) == key]
    assert len(cards) == 1, (
        f"duplicate card for {key}: the learned merge deduped on id alone, so a "
        f"stale operator id produced a second card for the same declared thing")
    assert cards[0].provenance == "operator_added"


def test_learned_items_loader_is_defensive_and_force_stamps(tmp_path):
    """Mirror-image of ``load_operator_items``: absent/corrupt ⇒ [], malformed entry
    skipped, ``provenance`` forced to ``learned`` — but ``origin_class`` PRESERVED
    (forcing it, as the operator loader does, would destroy the taint this slice
    exists to carry). A non-canonical stamp fails UNTRUSTED."""
    v = _Vault(tmp_path)
    assert ts.load_learned_items(v) == []

    p = Path(v.root) / "table" / "learned_items.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert ts.load_learned_items(v) == []

    good = ts.make_learned_item("service", "acme", origin_class="content_derived")
    payload = [
        {"junk": True},                                   # malformed → skipped
        {**good.model_dump(mode="json"),
         "provenance": "operator_added",                  # a lie → force-stamped
         "origin_class": "operator"},                     # a lie → but see below
    ]
    p.write_text(json.dumps(payload), encoding="utf-8")
    out = ts.load_learned_items(v)
    assert len(out) == 1
    assert out[0].provenance == "learned", "provenance must be force-stamped"

    payload[1]["origin_class"] = "not_a_real_origin"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert ts.load_learned_items(v)[0].origin_class == "content_derived", (
        "a non-canonical origin must fail UNTRUSTED, not pass through")


# ══ PIN 4 — the exclusions ═══════════════════════════════════════════════════
@pytest.mark.parametrize("leaf", sorted(rb._PROFILE_SPINE | {"output_dir"}))
def test_spine_colliding_leaf_is_never_promoted(tmp_path, leaf):
    """``UserProfile`` has ``extra="forbid"`` and NO ``origin_class`` field, so the
    spine structurally cannot carry taint — and ``_bind_profile`` hard-codes
    ``operator`` for every spine hit. Worse, the spine loop runs BEFORE the
    user_facts loop: a spine value SHADOWS a promoted fact for the same leaf, so a
    content_derived promotion there launders 100% of the time even though the fact
    itself is stamped correctly. Hard-exclude the whole spine."""
    v = _Vault(tmp_path)
    up.save_profile(v, UserProfile(name="R", location_text="Chennai",
                                   timezone="Asia/Kolkata", default_output_dir="D:/out"))
    schema = {leaf: {"type": "string"}}
    snaps = _real_snaps(v, schema, candidate_value="D:/tainted")
    if not snaps:                       # the spine already bound it silently — fine
        return
    _assert_realistic(snaps)
    before = len(_promoted(v))
    ap.promote_answered_asks(v, _dctx(snaps), {leaf: "D:/tainted"})
    assert len(_promoted(v)) == before, (
        f"promoted into a spine-colliding leaf {leaf!r} — the spine bind shadows the "
        f"fact and hard-codes `operator`, laundering the value")


def test_secret_and_credential_asks_are_never_promoted(tmp_path):
    """Reuses the codebase's canonical secret marker rather than inventing a second
    mechanism. Belt-and-braces: ``requirement_snapshot`` already refuses these, so the
    promoter should never SEE one — this pin proves it also refuses one directly."""
    v = _Vault(tmp_path)
    # NOTE `credential_ref`: it never even reaches the card logic — "credential" is a
    # secret NAME token, so the fence refuses it one layer earlier. That is why it is
    # absent from the card-kind parametrization below.
    for path, klass in (("auth/api_key", "input"), ("token", "decision"),
                        ("password", "input"), ("credential_ref", "decision"),
                        ("anything", "credential")):
        assert rm.requirement_snapshot(
            {"kind": klass, "schema_path": path, "state": "resolvable",
             "source": "schema", "confidence": 0.5}) is None, (
            f"{path}/{klass} produced a snapshot — the secret fence moved")

    # and a snapshot that somehow carried a secret path is refused at promote time
    real = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))[0]
    smuggled = {**real, "schema_path": "auth/api_key"}
    assert ap.promote_answered_asks(v, _dctx([smuggled]),
                                    {"auth/api_key": "out/draft.md"}) == 0
    assert not _promoted(v), "a secret-mode field was promoted into the profile"


@pytest.mark.parametrize("leaf,expect_card", [
    ("service", True), ("mcp_server", True), ("device", True),
    ("tool", False),             # B7: ref_key prefers tool_id; a name-keyed learned
                                 # card never meets the operator's tool_id tombstone
    ("data_root", False),        # normcased path key the answer text won't reproduce
    ("approval_preference", False),  # §5.10.b#5: posture can NEVER arrive as learned.
                                 # NOTE: this leaf DOES match the `preference` kind on
                                 # its own tokens — so the posture denylist is what
                                 # withholds the card, not a failure to map.
    ("recipient", False),        # unmapped ⇒ fact-only, no card
])
def test_learned_card_kinds_are_restricted(tmp_path, leaf, expect_card):
    """A learned card is emitted ONLY for kinds whose ref_key is single-field and keyed
    the SAME way the projector keys it. ``tool`` is the proven trap: the operator's
    removal tombstones ``tool:<tool_id>`` while an answer-derived card knows only the
    name → ``tool:<name>``; the keys never meet, so a deleted tool is re-suggested
    forever. The FACT still promotes — only the card is withheld."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {leaf: {"type": "string"}}, candidate_value="acme-thing"))
    ap.promote_answered_asks(v, _dctx(snaps), {leaf: "acme-thing"})
    assert bool(ts.load_learned_items(v)) is expect_card
    assert _promoted(v), "the FACT must promote regardless of the card decision"


# ══ dedupe, supersede, caps ══════════════════════════════════════════════════
def test_dedupe_is_keyed_on_schema_path_and_supersedes_on_change(tmp_path):
    """``ask_id`` cannot be the dedupe key: mid-loop it is ``"hreq_"+uuid4()`` (zero
    cross-run protection), and the pre-loop one hashes rationale prose (a reworded
    prompt re-promotes). Dedupe on ``schema_path``; a CHANGED answer supersedes the
    prior promotion rather than accumulating a contradictory second fact."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))

    assert ap.promote_answered_asks(v, _dctx(snaps, "hreq_aaaa1111"),
                                    {"recipient": "ops@acme.com"}) == 1
    # SAME answer, a DIFFERENT ask_id → no second row
    assert ap.promote_answered_asks(v, _dctx(snaps, "hreq_bbbb2222"),
                                    {"recipient": "ops@acme.com"}) == 0
    assert len(_promoted(v)) == 1, "re-promoted the same value under a new ask_id"

    # a CHANGED answer supersedes the prior promotion
    assert ap.promote_answered_asks(v, _dctx(snaps, "hreq_cccc3333"),
                                    {"recipient": "sre@acme.com"}) == 1
    live = _promoted(v)
    assert len(live) == 1, "the superseded fact is still live"
    assert "sre@acme.com" in live[0].fact
    allf = _promoted(v, include_superseded=True)
    assert len(allf) == 2 and any(f.superseded_by for f in allf), (
        "the prior promotion must be marked superseded, not deleted (audit trail)")


def _many(v, n):
    """``n`` real, distinct, promotable snapshots + their answers."""
    schema = {f"field_{i}": {"type": "string"} for i in range(n)}
    snaps = _assert_realistic(_real_snaps(v, schema, candidate_value="out/draft.md"))
    assert len(snaps) == n, "the producer did not surface every leaf"
    return snaps, {s["schema_path"]: f"value-{i}" for i, s in enumerate(snaps)}


def test_the_batch_cap_is_enforced_and_what_it_drops_is_logged(tmp_path, caplog,
                                                               monkeypatch):
    """Bounded + auditable (§5.9): one answered card cannot flood the profile, and what
    was withheld is LOGGED rather than silently dropped.

    Asserts a FIXED expected count against a patched cap. The first version of this pin
    computed its input size *from* ``MAX_PROMOTIONS_PER_BATCH`` and asserted
    ``n <= MAX_PROMOTIONS_PER_BATCH`` — self-referential, so raising the constant to
    10000 kept it green. Mutation testing caught that; do not reintroduce it."""
    v = _Vault(tmp_path)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_BATCH", 3)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_CLASS", 99)
    snaps, answers = _many(v, 7)

    with caplog.at_level("INFO"):
        n = ap.promote_answered_asks(v, _dctx(snaps), answers)
    assert n == 3, f"the batch cap was not enforced (promoted {n} of 7)"
    assert len(_promoted(v)) == 3
    assert any("cap" in r.message.lower() for r in caplog.records), (
        "a capped promotion must be logged, not silently dropped")


def test_the_per_class_cap_is_enforced(tmp_path, monkeypatch):
    """The per-class bound is separate from the batch bound (§5.9 "capped per class")."""
    v = _Vault(tmp_path)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_BATCH", 99)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_CLASS", 2)
    snaps, answers = _many(v, 7)
    # positive control: these really are all ONE class, or the cap under test never binds
    assert len({s["class"] for s in snaps}) == 1

    assert ap.promote_answered_asks(v, _dctx(snaps), answers) == 2
    assert len(_promoted(v)) == 2


def test_the_shipped_caps_are_actually_small(tmp_path):
    """The enforcement pins above patch the caps, so they cannot see a SHIPPED value
    raised to something meaningless. This pins the shipped defaults themselves —
    "bounded" is a property of the value, not only of the code that reads it."""
    assert 1 <= ap.MAX_PROMOTIONS_PER_CLASS <= 20
    assert ap.MAX_PROMOTIONS_PER_CLASS <= ap.MAX_PROMOTIONS_PER_BATCH <= 50


def test_suggested_is_never_auto_confirmed(tmp_path):
    """§5.9/§5.10: a learned proposal enters the tray. Nothing on this path may
    promote it to a configured/ready state."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"})
    assert all(i.status == "suggested" for i in ts.load_learned_items(v))


# ══ B6 — the grandfather must not cover the promoter ═════════════════════════
def test_a_dropped_origin_stamp_over_asks_instead_of_laundering(tmp_path):
    """THE DEFENCE-IN-DEPTH PIN. The single most likely way this slice ships broken is
    a promoter that forgets the ``origin_class=`` kwarg — and ABSENT grandfathers to
    ``operator``, i.e. silent trusted bind. Adding ``ask_promotion`` to the
    ``_fact_origin`` carve-out means a dropped stamp reads ``content_derived``: the
    failure mode becomes an extra confirm (safe) instead of laundering (unsafe)."""
    v = _Vault(tmp_path)
    up.add_fact(v, "recipient is ops@acme.com", source=ap.PROMOTION_SOURCE,
                tags=["recipient"])                       # NOTE: no origin_class
    req, asked = _rebind(v, SCHEMA)
    assert req.value_origin == "content_derived", (
        "an unstamped ask_promotion fact grandfathered to `operator` — a dropped "
        "kwarg now launders silently")
    assert asked

    # and the carve-out must not leak to other sources (the grandfather still applies)
    v2 = _Vault(tmp_path / "b")
    up.add_fact(v2, "recipient is ops@acme.com", source="operator_chat",
                tags=["recipient"])
    req2, asked2 = _rebind(v2, SCHEMA)
    assert req2.value_origin == "operator" and not asked2, (
        "the carve-out widened beyond ask_promotion and broke the legacy grandfather")


# ══ robustness ═══════════════════════════════════════════════════════════════
def test_promoter_never_raises_on_junk(tmp_path):
    """Same contract as the sibling recorder: this runs inside a daemon reconciler
    tick after a real resume has already been dispatched. It must never take the tick
    down, and never half-write."""
    v = _Vault(tmp_path)
    for dctx, answers in (
        (None, None), ({}, {}), ({"spec": "junk"}, "junk"),
        ({"spec": {"requirement_snapshot": "nope"}}, {"a": "b"}),
        ({"spec": {"requirement_snapshot": [None, 7, {"no_path": 1}]}}, {"a": "b"}),
        (_dctx([]), {"a": "b"}),
    ):
        assert ap.promote_answered_asks(v, dctx, answers) == 0
    assert ap.promote_answered_asks(None, None, None) == 0
    assert not up.get_facts(v)


def test_unanswered_and_blank_slots_promote_nothing_AND_are_not_logged_as_refusals(
        tmp_path, caplog):
    """The signal is ANSWER-linked: an untouched form slot is not a promotion.

    It is also not a REFUSAL. The cap/refusal log is an audit signal about values the
    promoter declined to trust; letting every skipped blank slot land in it would
    drown that signal on any partially-filled card. (Blank answers also fail closed at
    the digest step, so the early skip is what keeps them OUT of the log — that
    distinction is the only observable difference, and it is what this pins.)"""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    with caplog.at_level("INFO"):
        assert ap.promote_answered_asks(v, _dctx(snaps), {"recipient": ""}) == 0
        assert ap.promote_answered_asks(v, _dctx(snaps), {"recipient": None}) == 0
        assert ap.promote_answered_asks(v, _dctx(snaps), {"other/path": "x"}) == 0
    assert not _promoted(v)
    assert not [r for r in caplog.records if "capped/refused" in r.getMessage()], (
        "an unanswered/blank slot was logged as a refusal — that is a non-event, and "
        "logging it drowns the real refusals in the audit signal")


# ══ the realism guard's own teeth ════════════════════════════════════════════
def test_fixture_realism_guard_tracks_the_live_producer(tmp_path):
    """``SNAPSHOT_KEYS`` is only worth having if it agrees with the real producer, and
    ``_assert_realistic`` is only worth having if it REJECTS a synthetic shape. Both
    halves are pinned here — this is the guard that would have caught the two defects
    that shipped today from fixtures production never emits."""
    v = _Vault(tmp_path)
    live = _real_snaps(v, SCHEMA, candidate_value="out/draft.md")
    assert live and set(live[0]) == SNAPSHOT_KEYS, (
        f"requirement_snapshot now emits {sorted(set(live[0]))}; update SNAPSHOT_KEYS "
        f"and re-check every pin in this module against the new shape")

    for synthetic in (
        {"schema_path": "recipient", "candidate_ref": "sha256:deadbeef"},   # wrong keys
        {**live[0], "candidate_ref": "sha256:deadbeef"},                    # wrong ref shape
        {**live[0], "extra": 1},                                            # extra key
    ):
        with pytest.raises(AssertionError):
            _assert_realistic([synthetic])
    with pytest.raises(AssertionError):
        _assert_realistic([])


# ══ F1 — an INCOMPARABLE candidate_ref must promote NOTHING ══════════════════
#: The seven shapes a ``candidate_ref`` can arrive in that are present-but-not-
#: comparable to the answer's own digest. Each one used to fall past the
#: ``if not cands`` fail-closed branch and land on ``_OPERATOR`` — the TRUSTED axis —
#: so a page-derived value the operator merely confirmed was promoted as if typed.
def _flip_last_hex(r):
    return r[:-1] + ("a" if r[-1] != "a" else "b")


def _swap_key_id(r):
    scheme, key_id, mac = r.split(":")
    return "%s:%s:%s" % (scheme, "0" * len(key_id), mac)


def _strip_key_segment(r):
    parts = r.split(":")
    return parts[0] + ":" + parts[2]


INCOMPARABLE_REFS = [
    ("truncated", lambda r: r[:-4]),
    ("wrapped_in_a_list", lambda r: [r]),
    ("wrapped_in_a_dict", lambda r: {"ref": r}),
    ("raw_value_smuggled", lambda r: "out/draft.md"),
    ("key_segment_stripped", _strip_key_segment),
    ("key_id_rotated", _swap_key_id),
]


@pytest.mark.parametrize("label,mutate", INCOMPARABLE_REFS,
                         ids=[lbl for lbl, _ in INCOMPARABLE_REFS])
def test_an_incomparable_candidate_ref_promotes_NOTHING(tmp_path, label, mutate):
    """``candidate_ref`` rides a card spec across a suspend in PLAINTEXT, so it is
    attacker-shaped input by the time it is read back here — the sibling reader of the
    SAME field (``replay_metrics.record_ask_avoidable``) says so in as many words and
    drops anything that is not a same-key ``value_ref``.

    The promoter must do the same. A present-but-incomparable candidate never reaches
    the ``if not cands`` fail-closed branch, so without the shape+key guards it falls
    through to ``operator``: one flipped hex character silently upgrades a
    ``content_derived`` value to the trusted axis, and it then SILENT-BINDS forever."""
    v = _Vault(tmp_path / label)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    # POSITIVE CONTROL: the un-mutated path really is tainted and really has a digest
    assert snaps[0]["value_origin"] == "content_derived"
    assert snaps[0]["candidate_ref"] is not None

    poisoned = {**snaps[0], "candidate_ref": mutate(snaps[0]["candidate_ref"])}
    n = ap.promote_answered_asks(v, _dctx([poisoned]), {"recipient": "out/draft.md"})
    assert n == 0, (
        "an incomparable candidate_ref (%s) was promoted — it must fail closed" % label)
    assert not _promoted(v), (
        "LAUNDERING: %s produced a fact; an unusable digest cannot be evidence that "
        "the operator TYPED the answer, so `operator` must never be inferred" % label)


def test_a_WELL_FORMED_same_key_NON_MATCH_is_an_override_by_construction(tmp_path):
    """THE DOCUMENTED RESIDUAL, pinned so nobody "fixes" it into a regression.

    A candidate whose MAC is altered but whose SHAPE and KEY-ID are intact (e.g. one
    flipped hex character) survives both guards, fails the equality test, and promotes
    ``operator``. That is not a hole the guards can close: a well-formed same-key
    digest that does not equal the answer is *exactly* the signal "the operator typed
    something the binder did not hold", which is the slice's entire payoff (see
    ``test_freshly_typed_operator_answer_promotes_trusted_and_rebinds_SILENTLY``).
    Nothing distinguishes it from a tampered MAC, because the promoter holds digests
    and never values.

    It is also not an escalation. Producing it requires WRITE access to the persisted
    card spec in the vault — and anyone with that already has write access to
    ``user_facts.jsonl`` itself, where they could simply author a fact stamped
    ``operator`` and skip this path entirely. The guards exist for the shapes that
    arise WITHOUT vault write access: a non-conforming producer, and a vault-key
    rotation, which needs no attacker at all.

    BOTH digests are flipped, and that is the point of the fixture rather than an
    incidental detail. The binder stamps the exact ref and its canonical twin from the
    SAME resolved value, so flipping only one of them describes no reachable state: it
    asserts "this answer both is and is not the binder's value". Since the canonical
    witness landed, that inconsistent pair resolves — correctly — to ``content_derived``
    (see the companion pin below), so a one-digest fixture would silently stop
    exercising this residual. The residual itself is unchanged; the tamper surface it
    needs is strictly larger, because BOTH digests must now be corrupted to force it."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    flipped = {**snaps[0],
               "candidate_ref": _flip_last_hex(snaps[0]["candidate_ref"]),
               "candidate_canon_ref": _flip_last_hex(snaps[0]["candidate_canon_ref"])}
    # the mutation really did leave comparable refs — that is the whole point
    assert rm._is_value_ref(flipped["candidate_ref"])
    assert rm._is_canonical_ref(flipped["candidate_canon_ref"])
    assert (rm._ref_key_id(flipped["candidate_ref"])
            == rm._ref_key_id(rm.value_ref("out/draft.md", v)))

    assert ap.promote_answered_asks(v, _dctx([flipped]),
                                    {"recipient": "out/draft.md"}) == 1
    assert _promoted(v)[0].origin_class == "operator", (
        "a comparable, non-matching candidate must read as an override — clamping it "
        "to content_derived would re-ask the operator forever for values they typed")


def test_flipping_ONLY_the_exact_mac_is_recovered_by_the_canonical_witness(tmp_path):
    """The companion to the residual above, and the reason its fixture had to change.

    Corrupting one of the two digests no longer forces ``operator``: the surviving
    witness still recognises the operator's answer as the binder's own value, so the
    taint travels. Pinned in BOTH directions so neither witness can be quietly dropped
    while the other masks it."""
    checks = (("candidate_ref", "candidate_canon_ref", rm._is_canonical_ref),
              ("candidate_canon_ref", "candidate_ref", rm._is_value_ref))
    for field, other, other_is_wellformed in checks:
        vv = _Vault(tmp_path / f"only_{field}")
        snap = _assert_realistic(
            _real_snaps(vv, SCHEMA, candidate_value="out/draft.md"))[0]
        # positive control: BOTH digests were stamped, so corrupting one really does
        # leave exactly one intact witness rather than testing an absent field.
        assert snap[field] is not None and snap[other] is not None
        snap = {**snap, field: _flip_last_hex(snap[field])}
        assert other_is_wellformed(snap[other]), "the surviving twin was not left intact"

        assert ap.promote_answered_asks(vv, _dctx([snap]),
                                        {"recipient": "out/draft.md"}) == 1
        assert _promoted(vv)[0].origin_class == "content_derived", (
            f"corrupting {field} alone forced the TRUSTED axis — the surviving "
            f"{other} witness should still have recognised the confirm")


def test_the_incomparable_ref_control_promotes_when_UNMUTATED(tmp_path):
    """The positive control for the parametrization above: the very same snapshot,
    answer and vault DO promote when ``candidate_ref`` is left alone. Without this the
    seven pins above would all pass against a promoter that promotes nothing at all."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    assert ap.promote_answered_asks(v, _dctx(snaps), {"recipient": "out/draft.md"}) == 1
    assert _promoted(v)[0].origin_class == "content_derived"


def test_a_vault_key_ROTATION_promotes_nothing_rather_than_trusted(tmp_path):
    """THE CASE THAT FIRES WITHOUT AN ATTACKER, and the reason this is a ship blocker.

    ``value_ref`` is keyed per-vault. If the per-vault secret changes between the ask
    and the answer (a rotated/rebuilt vault key), every in-flight ``candidate_ref`` is
    signed under the OLD key and is simply not comparable to the new answer digest.
    Nothing is malformed and nothing was tampered with — yet EVERY promotion on that
    card silently became ``operator``, laundering every content_derived value the
    operator merely confirmed.

    Modelled the honest way: two real vaults with genuinely different derived keys."""
    a = _Vault(tmp_path / "before_rotation")
    b = _Vault(tmp_path / "after_rotation")
    snaps = _assert_realistic(_real_snaps(a, SCHEMA, candidate_value="out/draft.md"))

    # POSITIVE CONTROL: both refs are WELL-FORMED, and differ only by the signing key.
    ref_old = snaps[0]["candidate_ref"]
    ref_new = rm.value_ref("out/draft.md", b)
    assert rm._is_value_ref(ref_old) and rm._is_value_ref(ref_new)
    assert rm._ref_key_id(ref_old) != rm._ref_key_id(ref_new), (
        "the two vaults derived the SAME key — the rotation is not modelled")

    n = ap.promote_answered_asks(b, _dctx(snaps), {"recipient": "out/draft.md"})
    assert n == 0, "a key rotation promoted anyway"
    assert not _promoted(b), (
        "a digest signed under a DIFFERENT vault key was treated as proof the operator "
        "typed the answer — a routine key rotation launders the whole card")


# ══ F2 — a secret VALUE under a non-secret LEAF ══════════════════════════════
#: The fence in place before this fix inspected field NAMES only, so a secret parked
#: in the ANSWER sailed through and was persisted to user_facts.jsonl + the learned
#: sidecar + items.json — and then EGRESSED verbatim: `_build_user_context_block`
#: puts the 5 most-recent facts in the system prompt and `scroll_refiner` ships the
#: recent 20 to a tier-1 LLM call, on unrelated future runs.
SECRET_VALUES = [
    "postgres://admin:S3cr3tP4ssw0rd@db.internal:5432/prod",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "sk-abcdefghijklmnopqrstuvwx",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "deploy.sh --token s0mesecretvalue123456",
    "deploy.sh --password hunter2hunter2hunter2",
    "a3f5c9d2e1b7a3f5c9d2e1b7a3f5c9d2e1b7a3f5",
]


@pytest.mark.parametrize("secret", SECRET_VALUES)
def test_a_secret_VALUE_under_a_NON_secret_leaf_is_never_promoted(tmp_path, secret):
    """The leaf is ``service_endpoint`` — not a secret NAME by any rule, so the
    name-only fence passes it. The VALUE is a credential. It must not be written to
    the profile, because the profile is read verbatim into a system prompt and into a
    tier-1 LLM payload on later, unrelated runs."""
    v = _Vault(tmp_path / str(abs(hash(secret))))
    # the INVENTORY channel deliberately: the source-#0 channel seeds a user_fact
    # CONTAINING the candidate, which would put the secret into user_facts.jsonl as
    # fixture material and make the pin below assert against its own setup.
    snaps = _assert_realistic(_inventory_snaps(v, "service_endpoint", secret))
    # POSITIVE CONTROL: the name-only fence really does PASS this leaf, so this pin is
    # exercising the value-level path it exists for and not the old name fence.
    assert ap._is_secret("service_endpoint", snaps[0]["class"]) is False, (
        "the leaf NAME is already secret by the name fence — this pin is vacuous")

    n = ap.promote_answered_asks(v, _dctx(snaps), {"service_endpoint": secret})
    assert n == 0, "a secret-shaped value was promoted: %r" % secret
    assert not up.get_facts(v), "a secret value reached user_facts.jsonl"
    assert not ts.load_learned_items(v), "a secret value reached the learned sidecar"
    tr.reconcile_once(v)
    blob = json.dumps([i.model_dump(mode="json") for i in ts.load_items(v)])
    assert secret not in blob, "a secret value survived into items.json"


def test_the_secret_VALUE_fence_leaves_ORDINARY_answers_alone(tmp_path):
    """The negative control the parametrization above needs: the value fence must not
    be a blanket refusal. Ordinary answers — paths, emails, service names, URLs,
    timezones — still promote, or the fix silently disables the whole slice."""
    for i, benign in enumerate(["out/draft.md", "ops@acme.com", "acme-crm",
                                "https://api.acme.com/v1/reports", "Asia/Kolkata"]):
        v = _Vault(tmp_path / ("benign%d" % i))
        # same channel as the secret parametrization above — a control that took a
        # different route through the binder would not be controlling for anything.
        snaps = _assert_realistic(_inventory_snaps(v, "service_endpoint", benign))
        assert ap.promote_answered_asks(
            v, _dctx(snaps), {"service_endpoint": benign}) == 1, (
            "the value fence refused an ordinary answer %r" % benign)


def test_the_learned_card_never_carries_the_raw_ANSWER_in_its_name(tmp_path):
    """§5.10 cards render ``name``. Putting the raw answer there published every
    promoted value on the /table surface — and keyed the card on the VALUE, which is
    what made F5's duplicate cards possible. The card names the LEAF; the value rides
    a non-rendered field."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    assert ap.promote_answered_asks(v, _dctx(snaps), {"service": "acme-crm"}) == 1

    card = ts.load_learned_items(v)[0]
    assert card.name == "service", (
        "the card is named %r — it must name the LEAF, not the answer" % card.name)
    assert "acme-crm" not in card.name and "acme-crm" not in card.detail
    assert "acme-crm" not in ts.ref_key(card.kind, card.ref), (
        "the card is still KEYED on the answer value — three answers to one leaf "
        "would produce three cards (F5)")
    assert "acme-crm" in json.dumps(card.usage), (
        "the promoted value must still be carried, in the non-rendered field")


# ══ F5 — a learned card must supersede when its fact does ════════════════════
def test_three_answers_to_ONE_path_leave_ONE_learned_card_holding_the_LATEST(tmp_path):
    """One ``schema_path`` supersedes to exactly ONE live fact, so it must leave
    exactly ONE card. Before the fix each answer minted a value-keyed card and all
    three were projected forever, removable only one-by-one."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    for i, answer in enumerate(["acme-crm", "acme-two", "acme-three"]):
        ap.promote_answered_asks(v, _dctx(snaps, "hreq_%d" % i), {"service": answer})

    assert len(_promoted(v)) == 1, "the fact side did not supersede (control)"
    cards = ts.load_learned_items(v)
    assert len(cards) == 1, (
        "%d learned cards for one schema_path — a superseded fact must not leave its "
        "card behind" % len(cards))
    assert "acme-three" in json.dumps(cards[0].usage), (
        "the surviving card still carries a SUPERSEDED value")
    tr.reconcile_once(v)
    projected = [i for i in ts.load_items(v) if i.provenance == "learned"]
    assert len(projected) == 1


def test_superseding_a_card_does_not_resurrect_an_operator_REMOVAL(tmp_path):
    """The supersede path drops the prior card and re-adds a fresh one. That must not
    become a back door around a tombstone: if the operator removed the card, a changed
    answer must not bring it back (the "no re-add flapping" rule)."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(
        _real_snaps(v, {"service": {"type": "string"}}, candidate_value="acme-crm"))
    assert ap.promote_answered_asks(v, _dctx(snaps, "h1"), {"service": "acme-crm"}) == 1
    key = ts.ref_key("service", {"server": "service"})
    assert ts.load_learned_items(v), "control: the card exists"

    ts.add_tombstone(v, key)                       # the operator removes it
    ap.promote_answered_asks(v, _dctx(snaps, "h2"), {"service": "acme-two"})
    assert not ts.load_learned_items(v), (
        "a changed answer resurrected a card the operator had removed")


# ══ F4 — the bound must be per-TICK, not per-CALL ════════════════════════════
def test_the_per_TICK_budget_bounds_promotions_ACROSS_cards(tmp_path, monkeypatch,
                                                            caplog):
    """``promoted``/``per_class`` are call-local, and the reconciler loops over every
    resolved decision in the tick — so N answered cards multiplied the bound by N
    (measured: 5 cards ⇒ 20 promotions against a batch cap of 8). §5.9 says bounded.

    A budget SHARED across the tick is what makes the bound real."""
    v = _Vault(tmp_path)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_TICK", 5)
    budget = ap.PromotionBudget()
    total = 0
    with caplog.at_level("INFO"):
        for card in range(4):
            schema = {("c%d_f%d" % (card, i)): {"type": "string"} for i in range(3)}
            snaps = _assert_realistic(_real_snaps(v, schema,
                                                  candidate_value="out/draft.md"))
            answers = {s["schema_path"]: "v%d-%d" % (card, i)
                       for i, s in enumerate(snaps)}
            total += ap.promote_answered_asks(v, _dctx(snaps, "hreq_%d" % card), answers,
                                              budget=budget)
    assert total == 5, "the per-tick budget was not enforced (promoted %d of 12)" % total
    assert len(_promoted(v)) == 5
    assert any("tick budget" in r.getMessage().lower() for r in caplog.records), (
        "a promotion dropped by the tick budget must be logged, not silently dropped")


def test_WITHOUT_a_shared_budget_each_call_still_gets_its_own_bound(tmp_path,
                                                                    monkeypatch):
    """The budget is optional (a direct caller need not build one), so the per-call
    caps must still bind on their own — otherwise dropping the budget argument at the
    one production call site would remove ALL bounding, not just the cross-card half."""
    v = _Vault(tmp_path)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_BATCH", 2)
    monkeypatch.setattr(ap, "MAX_PROMOTIONS_PER_CLASS", 99)
    snaps, answers = _many(v, 5)
    assert ap.promote_answered_asks(v, _dctx(snaps), answers) == 2


def test_the_shipped_tick_budget_is_actually_small():
    """As with the batch/class caps: "bounded" is a property of the VALUE, not only of
    the code that reads it."""
    assert ap.MAX_PROMOTIONS_PER_BATCH <= ap.MAX_PROMOTIONS_PER_TICK <= 64


# ══ F8 — a failed supersede must not pin a stale answer ═════════════════════
def test_ALL_live_promotions_for_a_path_are_superseded_not_just_one(tmp_path,
                                                                    monkeypatch):
    """``get_facts`` is newest-LAST while ``_bind_profile`` returns the FIRST match, so
    a stale live row BINDS and the newer one is never consulted. If the supersede write
    fails once (a Windows lock, or two ticks racing the ``harness_grant_dispatched``
    check-then-stamp window) the stale value is pinned SILENTLY and never self-heals.

    Superseding EVERY live promotion for the path — not just the one ``prior`` id —
    makes the next promotion repair the damage instead of compounding it."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    assert ap.promote_answered_asks(v, _dctx(snaps, "h1"),
                                    {"recipient": "old@a.com"}) == 1

    # the supersede write FAILS — the real failure this pin is about
    import systemu.runtime.user_profile as _up
    monkeypatch.setattr(_up, "forget_fact", lambda *a, **k: False)
    assert ap.promote_answered_asks(v, _dctx(snaps, "h2"),
                                    {"recipient": "mid@a.com"}) == 1
    live = _promoted(v)
    assert len(live) == 2, "control: the failed supersede really did leave two live rows"
    assert "old@a.com" in live[0].fact, (
        "control: the STALE row is the one _bind_profile would bind (first match)")

    monkeypatch.undo()                              # the lock clears
    assert ap.promote_answered_asks(v, _dctx(snaps, "h3"),
                                    {"recipient": "new@a.com"}) == 1
    live = _promoted(v)
    assert len(live) == 1 and "new@a.com" in live[0].fact, (
        "the stale rows were not healed — still live: %s" % [f.fact for f in live])


def test_an_UNCHANGED_answer_also_heals_stale_duplicate_rows(tmp_path, monkeypatch):
    """The other half of F8. The dedupe branch returns early on an identical answer, so
    if the healing lived only on the write path a duplicate left by a failed supersede
    would survive every subsequent tick that re-confirmed the same value — which is the
    common case, since the operator keeps answering the same thing."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    ap.promote_answered_asks(v, _dctx(snaps, "h1"), {"recipient": "old@a.com"})
    import systemu.runtime.user_profile as _up
    monkeypatch.setattr(_up, "forget_fact", lambda *a, **k: False)
    ap.promote_answered_asks(v, _dctx(snaps, "h2"), {"recipient": "same@a.com"})
    assert len(_promoted(v)) == 2                # control

    monkeypatch.undo()
    ap.promote_answered_asks(v, _dctx(snaps, "h3"), {"recipient": "same@a.com"})
    live = _promoted(v)
    assert len(live) == 1 and "same@a.com" in live[0].fact, (
        "re-confirming the same value did not heal the duplicate: %s"
        % [f.fact for f in live])


def _stack_up_live_rows(v, snaps, values, monkeypatch):
    """Drive the degraded state F8 is about: several LIVE promotions for one path,
    left behind by supersede writes that failed. Returns nothing; the caller asserts."""
    import systemu.runtime.user_profile as _up
    ap.promote_answered_asks(v, _dctx(snaps, "h0"), {"recipient": values[0]})
    monkeypatch.setattr(_up, "forget_fact", lambda *a, **k: False)
    for i, val in enumerate(values[1:], start=1):
        ap.promote_answered_asks(v, _dctx(snaps, "h%d" % i), {"recipient": val})
    monkeypatch.undo()
    assert len(_promoted(v)) == len(values), "control: the degraded state was not built"


def test_one_RAISING_supersede_does_not_abandon_the_remaining_rows(tmp_path,
                                                                   monkeypatch):
    """``forget_fact`` rewrites the JSONL in place, so on Windows it can RAISE (a
    file lock) rather than merely return False — and it raises per ROW. If the healing
    loop aborts on the first raise, every later row stays live and the oldest of them
    keeps binding. Each row must be attempted independently.

    The other F8 pins stub ``forget_fact`` to return False, which never enters the
    except branch — so this failure mode was invisible to them (it survived mutation)."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    _stack_up_live_rows(v, snaps, ["a@x.com", "b@x.com", "c@x.com"], monkeypatch)

    import systemu.runtime.user_profile as _up
    doomed = _promoted(v)[0].id                 # the OLDEST — attempted first
    real_forget = _up.forget_fact

    def flaky(vault, fact_id, *, reason="forgotten"):
        if fact_id == doomed:
            raise OSError("the file is locked by another process")
        return real_forget(vault, fact_id, reason=reason)

    monkeypatch.setattr(_up, "forget_fact", flaky)
    assert ap.promote_answered_asks(v, _dctx(snaps, "h9"),
                                    {"recipient": "d@x.com"}) == 1

    live = _promoted(v)
    assert {f.id for f in live} == {doomed, live[-1].id}, (
        "the healing loop abandoned the rows after the one that raised — still live: "
        "%s" % [f.fact for f in live])
    assert len(live) == 2 and "d@x.com" in live[-1].fact


def test_the_dedupe_compares_the_NEWEST_live_promotion_not_the_oldest(tmp_path,
                                                                      monkeypatch):
    """Which row the dedupe compares against only becomes observable once duplicates
    exist — the healthy case has exactly one live row, so both readings agree and the
    choice survived mutation.

    In the degraded case they diverge sharply. With two live rows (old, mid) and the
    operator re-answering the OLD value: comparing against the NEWEST correctly sees a
    change, writes the answered value and retires both stale rows. Comparing against
    the OLDEST sees a match, takes the no-op path, and leaves MID live — a value the
    operator did not give, now pinned as the profile's answer."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    _stack_up_live_rows(v, snaps, ["old@x.com", "mid@x.com"], monkeypatch)

    n = ap.promote_answered_asks(v, _dctx(snaps, "h9"), {"recipient": "old@x.com"})
    assert n == 1, "re-answering a value that is live but STALE must promote it afresh"
    live = _promoted(v)
    assert len(live) == 1 and "old@x.com" in live[0].fact, (
        "the operator's answer is not what is live — got %s"
        % [f.fact for f in live])


# ══ F3 (cheap half) — path-shaped answers compare NORMCASED ═════════════════
def test_normalize_value_normcases_path_shaped_values_only():
    """``out/draft.md`` and ``out\\draft.md`` are the SAME file on Windows, but compared
    raw they digest differently — so confirming the binder's own candidate read as an
    override and promoted ``operator``. The failure direction is toward TRUST, and
    path-shaped values are the dominant ``content_derived`` source.

    ``os.path.normcase`` is the right primitive precisely because it encodes the
    PLATFORM's rule (fold case + separators on Windows, identity on POSIX, where
    ``a\\b`` is a legitimate filename and folding it would conflate two real files)."""
    for p in ("out/draft.md", "OUT/DRAFT.MD", "C:/Users/x/Report.DOCX", "a/b\\c"):
        assert rm.normalize_value(p) == os.path.normcase(p), (
            "%r was not normcased before digesting" % p)
    # NOT path-shaped ⇒ untouched (folding an email/service name is not ours to do)
    for s in ("ops@Acme.com", "Acme-CRM", "MixedCase"):
        assert rm.normalize_value(s) == s, "%r was normcased but is not a path" % s
    # a URL is not a filesystem path — its path segment is case-SENSITIVE
    assert rm.normalize_value("https://API.acme.com/V1") == "https://API.acme.com/V1"
    # the pre-existing contract still holds
    assert rm.normalize_value(True) == "true" and rm.normalize_value(" x ") == "x"


@pytest.mark.skipif(os.path.normcase("A/B") == "A/B",
                    reason="platform does not fold path case/separators (POSIX)")
def test_a_separator_variant_answer_still_reads_as_a_CONFIRM(tmp_path):
    """The end-to-end consequence: the operator confirms the binder's candidate, typing
    the SAME file with a backslash. That is a confirm, and must promote the candidate's
    ``content_derived`` origin — not ``operator``."""
    v = _Vault(tmp_path)
    snaps = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))
    assert snaps[0]["value_origin"] == "content_derived"
    assert ap.promote_answered_asks(v, _dctx(snaps),
                                    {"recipient": "out\\draft.md"}) == 1
    assert _promoted(v)[0].origin_class == "content_derived", (
        "a separator variant of the SAME file read as a fresh operator override and "
        "was promoted TRUSTED")


# ══ F11 — the learned-item origin clamp (the mutation that survived) ════════
def test_make_learned_item_clamps_a_NON_canonical_origin_to_content_derived():
    """``TableItem.origin_class`` is a plain ``str`` defaulting to ``operator`` — the
    model does NOT validate it (unlike ``UserFact.origin_class``, which is a closed
    vocabulary that fails loud, and is why ``_promote_fact`` deliberately carries no
    clamp of its own). So this clamp on a PUBLIC constructor is a real write-side
    guard, and the only reason it survived mutation is that its one caller happens to
    pass a canonical value. Pinned directly."""
    for bogus in ("trusted", "OPERATOR", "", "operator ", "not_a_real_origin"):
        it = ts.make_learned_item("service", "svc", origin_class=bogus)
        assert it.origin_class == "content_derived", (
            "a non-canonical origin %r was kept as %r — flipping this clamp to a "
            "trusted default would launder every learned card" % (bogus, it.origin_class))
    for ok in ("operator", "systemu_authored", "content_derived"):
        assert ts.make_learned_item("service", "svc", origin_class=ok).origin_class == ok


# ══ refusals must be VISIBLE ════════════════════════════════════════════════
def test_secret_and_spine_refusals_are_LOGGED_not_silently_dropped(tmp_path, caplog):
    """§5.9 "bounded + auditable": what was withheld is LOGGED. Both of these refusals
    used to log at DEBUG and never entered ``capped``, so at the daemon's default level
    they were invisible — the module's own observability claim was false."""
    v = _Vault(tmp_path)
    real = _assert_realistic(_real_snaps(v, SCHEMA, candidate_value="out/draft.md"))[0]

    with caplog.at_level("INFO"):
        assert ap.promote_answered_asks(
            v, _dctx([{**real, "schema_path": "auth/api_key"}]),
            {"auth/api_key": "x"}) == 0
    assert any("capped/refused" in r.getMessage() for r in caplog.records), (
        "a SECRET refusal was not surfaced at INFO")

    caplog.clear()
    with caplog.at_level("INFO"):
        assert ap.promote_answered_asks(
            v, _dctx([{**real, "schema_path": "timezone"}]),
            {"timezone": "Asia/Kolkata"}) == 0
    assert any("capped/refused" in r.getMessage() for r in caplog.records), (
        "a SPINE refusal was not surfaced at INFO")
