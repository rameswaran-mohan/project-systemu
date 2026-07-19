# tests/test_glearn_s4_learned_synonyms.py
"""R-A16 G-LEARN slice 4 (§5.9) — the LEARNED synonym overlay.

§5.9 asks the learning loop to "extend the synonym map (``reference_synonyms.py``)".
``reference_synonyms`` is a PURE static dict by design (no I/O, no state), so the
learned half lives in ``reference_synonyms_learned`` as a capped, deduped,
vault-backed overlay consulted through a MERGED accessor.

WHY THIS PAYS OFF (grounded, and the reason this half was built while the
threshold half was NOT — see ``test_glearn_s4_threshold_evidence``):
``reference_resolver._score`` has a RELEVANCE GATE — ``if not matched and not
ext_match: return 0.0``. So a synonym-ext hint can be the ONLY thing that
qualifies a candidate at all. A learned token therefore converts a
``missing`` verdict (a BLANK "type the path" ask) into ``resolvable`` (a
PRE-FILLED one-click confirm) — exactly the missing-answered → resolvable
conversion §5.9's own metric reports.

SAFETY: this can NEVER weaken the silent-bind invariant. ``_bind_filehandle``
clamps a resolved file to ``content_derived`` regardless of score, and
``_needs_ask`` surfaces a content_derived bind at ANY confidence. So the overlay
only ever upgrades a blank ask to a pre-filled confirm; it can never make an
untrusted file value bind silently. ``test_overlay_cannot_weaken_taint`` pins that.

NOTE: this module must not read source via ``inspect`` — ``conftest`` auto-tags a
whole module ``source_sensitive`` on the substring that call would introduce, which
would drop these behavioural pins out of the edit-safe tier. The substring is
deliberately not spelled out here: writing it even inside a comment is enough to trip
the tagger (it matches module TEXT, not code), which is exactly how these pins were
silently deselected once already.
"""
from __future__ import annotations

import json
import logging
import os
import time

import pytest

from systemu.runtime import reference_resolver as rr
from systemu.runtime import reference_synonyms as rs
from systemu.runtime import reference_synonyms_learned as rsl


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def vault(tmp_path):
    from systemu.vault.vault import Vault
    return Vault(str(tmp_path / "vault"))


class _AllGranted:
    def is_within_granted(self, p):
        return True


def _salient_situation(path, name, ext, mtime=None):
    """A situation dict shaped like ``build_roots`` emits.

    The SHAPE is pinned against the real producer in
    ``test_fixture_shape_matches_the_real_producer`` — that test RUNS
    ``build_roots`` and fails if this fixture drifts from what production emits.
    """
    return {"roots": [{"salient": [{"path": str(path), "name": name, "ext": ext,
                                    "size": 10,
                                    "mtime": mtime if mtime is not None else time.time()}]}]}


# ── PIN 0: FIXTURE REALISM — derive the shape by RUNNING the producer ────────
def test_fixture_shape_matches_the_real_producer(tmp_path):
    """The salient-handle fixture above must use the keys ``build_roots`` REALLY emits.

    Three defects shipped in this programme from fixtures using shapes production
    never produces. This pin runs the actual producer and compares, so a
    ``FileHandleLite`` field rename breaks the fixture LOUDLY instead of leaving
    these tests passing against an invented shape.
    """
    from systemu.runtime.situational_inventory import build_roots

    root = tmp_path / "root"
    root.mkdir()
    (root / "burndown.xlsx").write_text("x", encoding="utf-8")

    class _Roots:
        def list_roots(self):
            return [str(root)]

        def is_within_granted(self, p):
            return True

    surveys = build_roots(_Roots())
    assert surveys, "producer emitted no survey — cannot ground the fixture"
    real = surveys[0].salient[0].model_dump()

    fixture = _salient_situation("p", "burndown.xlsx", ".xlsx")["roots"][0]["salient"][0]
    missing = set(fixture) - set(real)
    assert not missing, f"fixture uses key(s) the producer never emits: {missing}"

    # and the two fields the resolver actually scores on must agree in SHAPE
    assert real["name"] == "burndown.xlsx"
    assert real["ext"] == ".xlsx"
    assert isinstance(real["mtime"], float)


def test_producer_emits_ext_with_original_case_so_learning_must_lowercase(tmp_path):
    """``build_roots`` uses ``os.path.splitext``, which PRESERVES case; the resolver
    lowercases at compare time (``ext = str(fh.get("ext")).lower()``).

    So the learned overlay must store lowercase extensions or a learned ``.XLSX``
    could never match. Pinned against the real producer, not an assumption.
    """
    from systemu.runtime.situational_inventory import build_roots

    root = tmp_path / "root"
    root.mkdir()
    (root / "REPORT.XLSX").write_text("x", encoding="utf-8")

    class _Roots:
        def list_roots(self):
            return [str(root)]

        def is_within_granted(self, p):
            return True

    real = build_roots(_Roots())[0].salient[0]
    assert real.ext == ".XLSX", "producer no longer preserves ext case — revisit lowercasing"


def test_learned_tokens_are_lookupable_by_the_resolvers_own_tokenizer(vault):
    """A learned KEY the resolver's tokenizer can never produce is dead data.

    The resolver looks synonyms up per token of ``_tokens(text) | _tokens(key)``.
    So every token the learner emits for a leaf MUST be in
    ``reference_resolver._tokens(leaf)`` — pinned against the resolver's OWN
    tokenizer so a split-regex change breaks this loudly.
    """
    for leaf in ("burndown_path", "sprint-tracker_file", "invoiceDoc", "q3_ledger"):
        learned = rsl._learnable_tokens(leaf)
        producible = rr._tokens(leaf)
        assert learned <= producible, (
            f"learner emitted token(s) the resolver can never look up for {leaf!r}: "
            f"{learned - producible}")


# ── PIN 1: PURITY — the static module stays a readable constant ──────────────
def test_static_module_stays_pure_after_learning(vault):
    before = dict(rs._SYNONYM_EXTS)
    assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                 answer="C:/w/a.xlsx") is True
    assert rs._SYNONYM_EXTS == before, "learning mutated the pure static map"
    assert rs.synonym_exts("tracker") == frozenset(), \
        "the static accessor must not see learned data"


def test_merged_accessor_without_a_vault_equals_the_static_map():
    for tok in ("deck", "resume", "sheet", "nope", "", None):
        assert rsl.merged_synonym_exts(tok, None) == rs.synonym_exts(tok)


def test_merged_accessor_unions_and_never_shrinks_the_static_map(vault):
    # 'report' is STATIC -> {.docx, .pdf}. Learning must never subtract.
    rsl._save_learned(vault, {"report": [".xlsx"]})
    merged = rsl.merged_synonym_exts("report", vault)
    assert rs.synonym_exts("report") <= merged
    assert ".xlsx" in merged


# ── PIN 2: THE PAYOFF — missing -> resolvable, driven through the REAL resolver ──
def test_learned_synonym_converts_missing_to_resolvable(vault, tmp_path):
    f = tmp_path / "burndown.xlsx"
    f.write_text("x", encoding="utf-8")
    sit = _salient_situation(f, "burndown.xlsx", ".xlsx")

    # 'tracker' is not in the static map and does not match the filename stem
    v0 = rr.resolve_reference("update my sprint tracker", situation=sit,
                              granted=_AllGranted(), key="tracker_path", vault=vault)
    assert v0.state == "missing", f"precondition failed: {v0.why}"

    assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                 answer=str(f)) is True

    v1 = rr.resolve_reference("update my sprint tracker", situation=sit,
                              granted=_AllGranted(), key="tracker_path", vault=vault)
    assert v1.state == "resolvable"
    assert v1.referent == str(f)


def test_batch_accessor_agrees_with_the_single_token_accessor(vault):
    rsl._save_learned(vault, {"tracker": [".xlsx"], "ledger": [".csv"]})
    toks = ["tracker", "ledger", "deck", "unknown", ""]
    expected = frozenset()
    for t in toks:
        expected |= rsl.merged_synonym_exts(t, vault)
    assert rsl.merged_exts_for_tokens(toks, vault) == expected
    assert ".xlsx" in expected and ".csv" in expected and ".pptx" in expected


def test_overlay_is_read_once_per_resolve_not_once_per_token(vault, tmp_path, monkeypatch):
    """The resolver runs per path LEAF; a per-TOKEN read would multiply file I/O on a
    hot bind path. Pin the batch read so that regression cannot return silently."""
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")
    sit = _salient_situation(f, "a.xlsx", ".xlsx")
    rsl._save_learned(vault, {"tracker": [".xlsx"]})

    reads = {"n": 0}
    real = rsl.load_learned

    def _counting(v):
        reads["n"] += 1
        return real(v)

    monkeypatch.setattr(rsl, "load_learned", _counting)
    # a reference with MANY tokens — a per-token implementation would read once each
    rr.resolve_reference("update my quarterly sprint tracker ledger summary",
                         situation=sit, granted=_AllGranted(),
                         key="tracker_path", vault=vault)
    assert reads["n"] <= 1, f"overlay read {reads['n']}x for one resolve"


def test_resolver_without_a_vault_is_unchanged(vault, tmp_path):
    """Default-off: a caller that threads no vault sees byte-identical behaviour."""
    f = tmp_path / "burndown.xlsx"
    f.write_text("x", encoding="utf-8")
    sit = _salient_situation(f, "burndown.xlsx", ".xlsx")
    rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                          answer=str(f))
    v = rr.resolve_reference("update my sprint tracker", situation=sit,
                             granted=_AllGranted(), key="tracker_path")
    assert v.state == "missing"


def test_overlay_cannot_weaken_taint(vault, tmp_path):
    """A learned synonym upgrades a BLANK ask to a PRE-FILLED confirm — never to a
    silent bind. The resolved file stays ``content_derived`` and still needs an ask."""
    from systemu.runtime import requirement_binder as rb
    from systemu.core.models import Objective

    f = tmp_path / "burndown.xlsx"
    f.write_text("x", encoding="utf-8")
    sit = _salient_situation(f, "burndown.xlsx", ".xlsx")
    rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                          answer=str(f))

    cap = type("Cap", (), {"name": "w", "effect_tags": [], "parameters_schema": {
        "type": "object", "required": ["tracker_path"],
        "properties": {"tracker_path": {"type": "string"}}}})()

    class _Ctx:
        files_produced = []
        _granted_roots = _AllGranted()

    reqs = rb.compute_requirements(Objective(id=1, goal="update my sprint tracker",
                                             success_criteria="done"),
                                   cap, sit, _Ctx(), vault=vault)
    bound = [r for r in reqs if r.schema_path == "tracker_path"]
    assert bound, "leaf disappeared"
    r = bound[0]
    assert r.value_origin == "content_derived", "taint clamp lost"
    assert rb._needs_ask(r) is True, "a learned synonym must never enable a silent bind"


# ── PIN 3: the LEARNING RULE is deterministic + narrow ───────────────────────
@pytest.mark.parametrize("schema_path,klass,answer,why", [
    ("output_path", "input", "C:/w/a.xlsx", "generic token -> would flood candidates"),
    ("input_file", "input", "C:/w/a.xlsx", "generic token"),
    ("report_path", "input", "C:/w/a.docx", "'report' is already in the static map"),
    ("tracker_path", "decision", "C:/w/a.xlsx", "not a file-reference (input) ask"),
    ("tracker_path", "input", "no-extension-here", "answer carries no extension"),
    ("tracker_path", "input", "", "empty answer"),
    ("tracker_path", "input", None, "no answer"),
    ("", "input", "C:/w/a.xlsx", "no schema path"),
])
def test_refusals(vault, schema_path, klass, answer, why):
    assert rsl.learn_from_answer(vault, schema_path=schema_path, klass=klass,
                                 answer=answer) is False, why
    assert rsl.load_learned(vault) == {}


def test_extension_is_normalised_to_lowercase(vault):
    rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                          answer="C:/w/SPRINT.XLSX")
    assert rsl.load_learned(vault)["tracker"] == frozenset({".xlsx"})


def test_token_matching_the_answer_stem_is_not_learned(vault):
    """If the token already matches the filename, the resolver's NAME overlap already
    fires — learning buys nothing and would spend the cap."""
    assert rsl.learn_from_answer(vault, schema_path="sprint_burndown_path",
                                 klass="input", answer="C:/w/burndown.xlsx") is True
    learned = rsl.load_learned(vault)
    # 'burndown' matched the stem (the resolver already scores it); 'sprint' did not
    assert "burndown" not in learned
    assert learned["sprint"] == frozenset({".xlsx"})


def test_dedupe_same_pair_twice_writes_once(vault):
    assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                 answer="C:/w/a.xlsx") is True
    assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                 answer="C:/w/b.xlsx") is False
    assert rsl.load_learned(vault) == {"tracker": frozenset({".xlsx"})}


# ── PIN 4: BOUNDS — capped, and what is withheld is LOGGED ───────────────────
def test_token_cap_is_enforced_and_refusal_is_logged(vault, caplog):
    data = {f"tok{i}": [".xlsx"] for i in range(rsl.MAX_TOKENS)}
    rsl._save_learned(vault, data)
    with caplog.at_level(logging.INFO, logger="systemu.runtime.reference_synonyms_learned"):
        assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                     answer="C:/w/a.xlsx") is False
    assert len(rsl.load_learned(vault)) == rsl.MAX_TOKENS
    assert "cap" in caplog.text.lower(), "a withheld learn must be LOGGED, not dropped"


def test_ext_per_token_cap_is_enforced(vault):
    rsl._save_learned(vault, {"tracker": [f".e{i}" for i in range(rsl.MAX_EXTS_PER_TOKEN)]})
    assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                 answer="C:/w/a.xlsx") is False
    assert len(rsl.load_learned(vault)["tracker"]) == rsl.MAX_EXTS_PER_TOKEN


def test_refusals_are_logged_not_silently_dropped(vault, caplog):
    with caplog.at_level(logging.INFO, logger="systemu.runtime.reference_synonyms_learned"):
        rsl.learn_from_answer(vault, schema_path="api_key_path", klass="input",
                              answer="C:/w/a.xlsx")
    assert caplog.text.strip(), "a refusal must leave an audit line"


# ── PIN 5: SECRETS — reuse the shipped value-level detector, never a third one ──
def test_secret_looking_answer_is_refused(vault, caplog):
    """S3 found a secret can hide under a NON-secret leaf name, so the value itself
    must go through ``ask_promotion._value_is_secret`` (which reuses
    ``messaging.gateway.mask_outbound`` as the detector). No third mechanism."""
    with caplog.at_level(logging.INFO, logger="systemu.runtime.reference_synonyms_learned"):
        assert rsl.learn_from_answer(
            vault, schema_path="endpoint_path", klass="input",
            answer="postgres://admin:hunter2@db/prod.sqlite") is False
    assert rsl.load_learned(vault) == {}
    assert "credential" in caplog.text.lower() or "secret" in caplog.text.lower()


def test_secret_named_leaf_is_refused(vault):
    """The NAME-level fence too — symmetric with S3's ``_is_secret``."""
    assert rsl.learn_from_answer(vault, schema_path="api_token", klass="input",
                                 answer="C:/w/a.xlsx") is False
    assert rsl.load_learned(vault) == {}


def test_it_reuses_the_shipped_detector(vault, monkeypatch):
    """Pin the REUSE, not a re-implementation: neutering the shipped detector must
    change this module's verdict, proving it is the same mechanism."""
    import systemu.messaging.gateway as gw
    seen = {}

    def _fake_mask(s, *a, **k):
        seen["called"] = True
        return s + "-MASKED"          # always "contains a secret"

    monkeypatch.setattr(gw, "mask_outbound", _fake_mask)
    assert rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                                 answer="C:/w/a.xlsx") is False
    assert seen.get("called"), "the shipped secret detector was not consulted"


# ── PIN 6: NEVER RAISES — this is an observability/tuning path ───────────────
def test_broken_vault_never_raises(caplog):
    class _Bad:
        @property
        def root(self):
            raise RuntimeError("boom")

    assert rsl.learn_from_answer(_Bad(), schema_path="tracker_path", klass="input",
                                 answer="C:/w/a.xlsx") is False
    assert rsl.load_learned(_Bad()) == {}
    assert rsl.merged_synonym_exts("deck", _Bad()) == rs.synonym_exts("deck")


def test_corrupt_store_degrades_to_empty(vault):
    p = rsl._learned_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert rsl.load_learned(vault) == {}
    assert rsl.merged_synonym_exts("deck", vault) == rs.synonym_exts("deck")


@pytest.mark.parametrize("payload", [
    '["not", "a", "dict"]', '{"tok": "not-a-list"}', '{"tok": [123]}',
    '{"": [".xlsx"]}', '{"tok": [".xlsx", null]}', 'null', '42',
    # STRING values that are not extensions. This store is plain JSON in the vault
    # and may be hand-edited, so an `isinstance(str)` filter alone is NOT enough —
    # the SHAPE must be re-validated on read.
    '{"tok": ["notanext"]}', '{"tok": ["../../etc/passwd"]}',
    '{"tok": [".way_too_long_to_be_an_extension"]}', '{"tok": ["xlsx"]}',
    '{"tok": [".x/y"]}', '{"tok": [""]}',
])
def test_malformed_entries_are_skipped_not_fatal(vault, payload):
    p = rsl._learned_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload, encoding="utf-8")
    got = rsl.load_learned(vault)
    assert isinstance(got, dict)
    for k, v in got.items():
        assert isinstance(k, str) and k
        assert isinstance(v, frozenset)
        for e in v:
            # the real contract, not just "starts with a dot"
            assert isinstance(e, str) and rsl._EXT_RE.match(e), \
                f"loaded a value that is not a valid extension: {e!r}"


def test_a_hand_edited_store_cannot_inject_a_bogus_extension(vault, tmp_path):
    """End-to-end consequence of the above: garbage on disk must not reach the
    resolver's ``want_exts`` and start qualifying candidates."""
    p = rsl._learned_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"tracker": ["notanext", "../x", ".xlsx"]}', encoding="utf-8")
    assert rsl.merged_synonym_exts("tracker", vault) == frozenset({".xlsx"})


def test_a_resolver_failure_in_the_overlay_never_widens_file_access(vault, tmp_path):
    """Fail-safe direction: if the overlay blows up, the resolver must degrade to
    ``missing`` (a wider ask), never to a bogus referent."""
    f = tmp_path / "a.xlsx"
    f.write_text("x", encoding="utf-8")
    sit = _salient_situation(f, "a.xlsx", ".xlsx")

    class _Boom:
        @property
        def root(self):
            raise RuntimeError("boom")

    v = rr.resolve_reference("update my sprint tracker", situation=sit,
                             granted=_AllGranted(), key="tracker_path", vault=_Boom())
    assert v.state == "missing"


# ── PIN 6b: THE WIRING IS LIVE (this slice must not ship inert) ──────────────
def _real_card_spec(schema_path, state="missing", confidence=0.0):
    """A card spec built by the REAL producer, ``_build_bundled_scope_card``.

    Derived by RUNNING it, not hand-written: this codebase has already shipped a
    §5.9 field inert because the answer-side read a shape the producer never emits
    (``ctx.vault``), and three more defects this programme came from invented
    fixtures. If the producer's spec/snapshot shape changes, this breaks loudly.
    """
    from systemu.core.models import Requirement
    from systemu.runtime.shadow_runtime import ShadowRuntime

    req = Requirement(kind="input", schema_path=schema_path, state=state,
                      source="schema", value_origin=None, bound_value_ref=None,
                      confidence=confidence, rationale="no source bound it")
    card = ShadowRuntime._build_bundled_scope_card(
        object.__new__(ShadowRuntime), "writer", [req], {}, reasoning="r")
    return card.spec


def test_the_producer_still_emits_the_snapshot_shape_the_learner_reads():
    """Pin the PRODUCER's contract, so a rename cannot leave the learner reading a
    key that no longer exists (the failure mode that shipped ``bound_value_digest``
    inert)."""
    spec = _real_card_spec("tracker_path")
    snaps = spec.get("requirement_snapshot")
    assert isinstance(snaps, list) and snaps, "producer emitted no snapshot"
    snap = snaps[0]
    # exactly the two keys the learning trigger in `jobs.py` reads off the snapshot
    assert snap["schema_path"] == "tracker_path"
    assert snap["class"] == "input"


def test_answer_time_join_actually_learns(vault):
    """END-TO-END through the SHIPPED trigger: ``jobs.record_bundled_ask_outcomes``.

    Without this pin the overlay could be perfectly correct and never invoked in
    production — the exact way a §5.9 field has shipped inert here before.
    """
    from systemu.scheduler.jobs import record_bundled_ask_outcomes

    dctx = {"request_id": "ask-1", "spec": _real_card_spec("tracker_path")}
    record_bundled_ask_outcomes(vault, dctx, {"tracker_path": "C:/w/sprint.xlsx"})
    assert rsl.load_learned(vault) == {"tracker": frozenset({".xlsx"})}


def test_answer_time_join_still_records_the_slice_2_signal(vault):
    """The learning trigger must not disturb the sibling recorder it sits beside."""
    from systemu.runtime.replay_metrics import load_avoidable_ask_corpus
    from systemu.scheduler.jobs import record_bundled_ask_outcomes

    dctx = {"request_id": "ask-1", "spec": _real_card_spec("tracker_path")}
    record_bundled_ask_outcomes(vault, dctx, {"tracker_path": "C:/w/sprint.xlsx"})
    assert len(load_avoidable_ask_corpus(vault)) == 1


def _real_card_spec_multi(*schema_paths):
    """A MULTI-slot card spec from the real producer — the shape a bundled scope card
    genuinely produces (it bundles every requirement of one tool-call)."""
    from systemu.core.models import Requirement
    from systemu.runtime.shadow_runtime import ShadowRuntime

    reqs = [Requirement(kind="input", schema_path=sp, state="missing", source="schema",
                        value_origin=None, bound_value_ref=None, confidence=0.0,
                        rationale="no source bound it") for sp in schema_paths]
    return ShadowRuntime._build_bundled_scope_card(
        object.__new__(ShadowRuntime), "writer", reqs, {}, reasoning="r").spec


def test_a_learner_failure_cannot_break_the_slice_2_recorder(vault, monkeypatch):
    """The tuning half must never take the MEASUREMENT half down with it.

    Uses a MULTI-slot card on purpose: with a single slot this passes even when the
    learner is guarded only by the function-level ``except``, because there are no
    remaining paths to lose. With several slots, an unguarded learner failure on the
    first path abandons the loop and costs every later path its slice-2 row.
    """
    from systemu.runtime.replay_metrics import load_avoidable_ask_corpus
    from systemu.scheduler.jobs import record_bundled_ask_outcomes

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rsl, "learn_from_answer", _boom)
    paths = ["tracker_path", "ledger_path", "roster_path"]
    dctx = {"request_id": "ask-1", "spec": _real_card_spec_multi(*paths)}
    record_bundled_ask_outcomes(
        vault, dctx, {p: f"C:/w/{p}.xlsx" for p in paths})
    assert len(load_avoidable_ask_corpus(vault)) == len(paths), \
        "a learner failure cost a later path its slice-2 observation"


def test_all_slots_of_a_multi_slot_card_are_learned_from(vault):
    from systemu.scheduler.jobs import record_bundled_ask_outcomes

    paths = ["tracker_path", "ledger_path"]
    dctx = {"request_id": "ask-1", "spec": _real_card_spec_multi(*paths)}
    record_bundled_ask_outcomes(vault, dctx, {"tracker_path": "C:/w/a.xlsx",
                                              "ledger_path": "C:/w/b.csv"})
    assert rsl.load_learned(vault) == {"tracker": frozenset({".xlsx"}),
                                       "ledger": frozenset({".csv"})}


def test_a_secret_answer_is_not_learned_through_the_live_trigger(vault):
    """The secret fence must hold on the PRODUCTION path, not only on direct calls."""
    from systemu.scheduler.jobs import record_bundled_ask_outcomes

    dctx = {"request_id": "ask-1", "spec": _real_card_spec("endpoint_path")}
    record_bundled_ask_outcomes(
        vault, dctx, {"endpoint_path": "postgres://admin:hunter2@db/prod.sqlite"})
    assert rsl.load_learned(vault) == {}


# ── PIN 7: the overlay is VISIBLE (an invisible learned map is a debug trap) ──
def test_learned_overlay_is_reported(vault):
    from systemu.runtime.replay_metrics import avoidable_ask_report, format_avoidable_ask
    rsl.learn_from_answer(vault, schema_path="tracker_path", klass="input",
                          answer="C:/w/a.xlsx")
    rep = avoidable_ask_report(vault)
    learned = ((rep.get("answer_linked") or {}).get("learned_synonyms")) or {}
    assert learned.get("tokens") == 1
    text = "\n".join(format_avoidable_ask(rep))
    assert "tracker" in text, "the learned token must be visible in the report"
