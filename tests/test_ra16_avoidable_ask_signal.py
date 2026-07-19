"""R-A16 / G-LEARN slice-2 — the ANSWER-LINKED avoidable-ask signal (SPEC §5.9).

§5.9: *"When the operator answers an ``input``/``decision``/``capability`` ask with
something the inventory/discovery/resolver COULD have produced, record an
``AskWasAvoidable`` event with the class + near-miss score."*

Distinct from the shipped R-A13.5 ``record_ask`` no-attempt PROXY (which records no
answer, no candidates, no requirement identity, and lives on the harness else-branch
that excludes INPUT while including credential — the inverse of §5.9's class list).

The pins here are, in order of severity:
  1. **SECRET EXCLUSION** — a credential / secret-mode ask must produce NO record, and
     no recorded field may ever contain the answer text. This file is a PLAINTEXT
     append-only audit artefact; a leak here is a shipped data leak.
  2. the two deterministic sub-cases (resolvable-confirmed = DEFINITIVE avoidable;
     missing-answered = candidate only), and
  3. observability-only discipline (never raises, append-only, no run effect).

**FIXTURE REALISM (the lesson of the F1 defect).** The first cut of this slice
classified by comparing a digest of ``bound_value_ref`` against a digest of the
answer, and EVERY fixture here handed it a value-shaped ``bound_value_ref`` (the bare
string ``out/r.md``, answered ``out/r.md``) — a shape **no binder
emits**. Real binders emit a NAMESPACED HANDLE (``file:...``, ``profile:...``,
``run_context:...``), so the comparison could never match: the definitive sub-case was
structurally unreachable and every bound ask recorded ``resolvable_overridden`` — the
exact inverse of the truth. The classifier now compares a KEYED digest of the bind's
RESOLVED VALUE (``Requirement.bound_value_digest``, stamped by the binder), and
:func:`test_no_fixture_uses_a_value_shaped_bound_value_ref` fails if any fixture in
this module ever regresses to a ref shape no binder produces.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from systemu.core.models import Requirement
from systemu.runtime import replay_metrics as rm


# ── harness ───────────────────────────────────────────────────────────────────

class _Vault:
    def __init__(self, root: Path):
        self.root = str(root)

    def list_tools(self, status=None):
        return []


#: The namespaces a REAL binder emits into ``Requirement.bound_value_ref`` — derived
#: from ``requirement_binder``'s return sites (``provided:`` / ``file:`` /
#: ``run_context:`` / ``credential:`` / ``service:`` / ``inventory:`` / ``profile:`` /
#: ``profile_fact:`` / the ``schema_*:`` family). A ``bound_value_ref`` outside this
#: set is a SYNTHETIC shape and makes any pin written against it worthless.
#: ``tests/test_ra16_join_placement.py`` pins this list against the binder source, so
#: a NEW bind source cannot silently fall outside it.
BINDER_REF_PREFIXES = (
    "provided:", "file:", "run_context:", "credential:", "service:",
    "inventory:", "profile:", "profile_fact:",
    "schema_default:", "schema_const:", "schema_enum0:", "schema_enum:",
    "schema_value:",
)


def _lines(tmp_path) -> list:
    p = Path(tmp_path) / "audit" / "ask_avoidable.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _raw(tmp_path) -> str:
    p = Path(tmp_path) / "audit" / "ask_avoidable.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _req(**kw):
    base = dict(kind="input", schema_path="report/output_path", state="missing",
                source="schema", value_origin="operator", bound_value_ref=None,
                bound_value_digest=None, confidence=0.0, rationale="where to write")
    base.update(kw)
    return Requirement(**base)


def _bound(vault, value, *, ref=None, **kw):
    """A REALISTICALLY bound requirement: a NAMESPACED binder handle plus the keyed
    digest of the bind's RESOLVED VALUE — exactly the pair ``_emit_requirement``
    now stamps. ``value`` is what the operator would have to type to confirm."""
    kw.setdefault("state", "resolvable")
    return _req(bound_value_ref=(ref or f"run_context:{value}"),
                bound_value_digest=rm.value_ref(value, vault), **kw)


def _record(v, req, answer, ask_id="hreq_test"):
    rm.record_ask_avoidable(v, ask_id=ask_id,
                            snapshot=rm.requirement_snapshot(req), answer=answer)


# ═══════════════════════════════════════════════════════════════════════════════
#  1. SECRET EXCLUSION — the highest-severity pin in the slice
# ═══════════════════════════════════════════════════════════════════════════════

SECRET = "sk-live-51H9xQq-NEVER-IN-AN-AUDIT-FILE"


def test_credential_ask_writes_nothing_and_never_leaks_the_answer(tmp_path):
    """§5.9 excludes ``credential``. End-to-end: a credential requirement answered
    with a real-looking secret produces NO record at all, and the secret text appears
    NOWHERE in the corpus file. (Mutating the exclusion away must fail THIS test.)"""
    v = _Vault(tmp_path)
    _record(v, _req(kind="credential", schema_path="auth/api_key",
                    state="missing", source="schema"), SECRET)
    assert _lines(tmp_path) == []
    assert SECRET not in _raw(tmp_path)


def test_a_secret_bearing_bind_produces_no_row_even_carrying_a_value_digest(tmp_path):
    """The F1 fix adds a SECOND value-derived field (``bound_value_digest``). Every
    existing guard must apply to it too: a credential bind that somehow carries a
    digest of its resolved value still snapshots to ``None`` and still records
    NOTHING — the new field can never become a secret-exclusion bypass."""
    v = _Vault(tmp_path)
    for req in (
        _req(kind="credential", schema_path="auth/api_key", state="resolvable",
             bound_value_ref="credential:openai", bound_value_digest=rm.value_ref(SECRET, v),
             confidence=0.85),
        # mis-KINDED secret: an allowed class but a secret-mode name.
        _req(kind="input", schema_path="creds/password", state="resolvable",
             bound_value_ref="profile:password", bound_value_digest=rm.value_ref(SECRET, v),
             confidence=0.9),
    ):
        assert rm.requirement_snapshot(req) is None
        _record(v, req, SECRET)
    assert _lines(tmp_path) == []
    assert SECRET not in _raw(tmp_path)


def test_credential_requirement_yields_no_snapshot(tmp_path):
    """Guard 1 (producer): the snapshot builder refuses a credential requirement, so
    a secret-bearing requirement can never even be STAMPED into a card spec."""
    assert rm.requirement_snapshot(_req(kind="credential", schema_path="auth/token")) is None


def test_class_allowlist_is_strict_independently_of_the_secret_guard(tmp_path):
    """The §5.9 class list is a STRICT allowlist, and this pins it INDEPENDENTLY.

    (Mutation finding: a ``credential`` kind also trips ``_is_secret_path`` — it stamps
    ``format="password"`` exactly as ``requirement_to_field`` does — so the credential
    tests alone cannot tell the two guards apart. This path is not secret-shaped, so
    ONLY the class guard can reject it.)"""
    assert rm.requirement_snapshot(
        {"kind": "future_unmodelled_class", "schema_path": "plan/step",
         "state": "missing", "confidence": 0.9}) is None
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(
        v, ask_id="a",
        snapshot={"schema_path": "plan/step", "class": "future_unmodelled_class",
                  "state": "missing", "source": "schema", "value_origin": "operator",
                  "confidence": 0.0, "candidate_ref": None},
        answer="anything")
    assert _lines(tmp_path) == []


def test_recorder_refuses_a_secret_class_snapshot(tmp_path):
    """Guard 2 (recorder, defence-in-depth): even handed a hand-built snapshot whose
    class is outside §5.9's list, the recorder writes nothing."""
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(
        v, ask_id="a",
        snapshot={"schema_path": "auth/api_key", "class": "credential",
                  "state": "missing", "source": "schema", "value_origin": "operator",
                  "confidence": 0.0, "candidate_ref": None},
        answer=SECRET)
    assert _lines(tmp_path) == []
    assert SECRET not in _raw(tmp_path)


def test_secret_named_input_field_is_excluded_by_is_secret_field(tmp_path):
    """Defence-in-depth: a MIS-KINDED secret (kind='input' but a secret-mode name)
    is excluded by the codebase's canonical secret marker (elicitation.is_secret_field
    — the same one that routes fields URL-mode), not by a bespoke rule."""
    v = _Vault(tmp_path)
    for path in ("auth/api_key", "creds/password", "billing/cvv", "x/client_secret"):
        assert rm.requirement_snapshot(_req(kind="input", schema_path=path)) is None
        _record(v, _req(kind="input", schema_path=path), SECRET)
    assert _lines(tmp_path) == []
    assert SECRET not in _raw(tmp_path)


def test_no_recorded_field_ever_contains_a_raw_value(tmp_path):
    """For an ALLOWED class the record still carries refs only — never the operator's
    answer text and never the binder's raw bound value."""
    v = _Vault(tmp_path)
    answer = "/home/op/Q3-forecast-CONFIDENTIAL.xlsx"
    _record(v, _bound(v, answer, ref=f"file:{answer}", confidence=0.61), answer)
    blob = _raw(tmp_path)
    assert blob.strip(), "an allowed-class ask must record something"
    assert answer not in blob
    rec = _lines(tmp_path)[0]
    assert (rec["answer_ref"] or "").startswith("hmac256:")


_BAD_CANDIDATE_REFS = (
    "/home/op/CONFIDENTIAL.xlsx", "hmac256:nothex!!!!!!!!!", "hmac256:abc",
    "plain-value", 12345,
    # the LEGACY unkeyed sha256 shape must not be accepted either
    "sha256:0123456789abcdef",
)


def test_recorder_drops_a_candidate_ref_that_is_not_a_digest(tmp_path):
    """Guard 3: ``candidate_ref`` rides a card spec across a suspend, so from this
    writer's view it is untrusted input. Only the exact ``value_ref`` digest shape is
    accepted — a raw value there is DROPPED, never written. The row degrades to
    missing-answered (under-counting avoidable asks — the safe direction).

    The row COUNT is asserted deliberately: dropping the candidate must be a decision,
    not an exception swallowed by the observability-only ``except`` (which would also
    leave no raw value on disk, and would let a broken guard look healthy)."""
    v = _Vault(tmp_path)
    for bad in _BAD_CANDIDATE_REFS:
        rm.record_ask_avoidable(
            v, ask_id="a",
            snapshot={"schema_path": "report/out", "class": "input", "state": "resolvable",
                      "source": "situation", "value_origin": "operator",
                      "confidence": 0.8, "candidate_ref": bad},
            answer="/home/op/CONFIDENTIAL.xlsx")
    blob = _raw(tmp_path)
    assert "/home/op/CONFIDENTIAL.xlsx" not in blob
    assert "plain-value" not in blob
    recs = _lines(tmp_path)
    assert len(recs) == len(_BAD_CANDIDATE_REFS), "a dropped candidate still records a row"
    for rec in recs:
        assert rec["resolution"] == "missing_answered"
        assert rec["candidates"] == []


def test_a_crafted_ref_carrying_the_right_key_id_is_still_rejected(tmp_path):
    """Guard 3 held in place ON ITS OWN. The key-id comparison downstream rejects most
    malformed refs as a side effect, which would let guard 3 be deleted with nothing
    observable changing. This input is shaped to slip PAST the key-id check — it embeds
    this vault's real key id — so only the shape check can stop the raw path from being
    written into a plaintext audit file."""
    v = _Vault(tmp_path)
    key_id = rm.value_ref("probe", v).split(":")[1]
    leak = f"LEAK:{key_id}:/home/op/CONFIDENTIAL.xlsx"
    rm.record_ask_avoidable(
        v, ask_id="a",
        snapshot={"schema_path": "report/out", "class": "input", "state": "resolvable",
                  "source": "situation", "value_origin": "operator",
                  "confidence": 0.8, "candidate_ref": leak},
        answer="/home/op/CONFIDENTIAL.xlsx")
    blob = _raw(tmp_path)
    assert "/home/op/CONFIDENTIAL.xlsx" not in blob
    assert "LEAK" not in blob
    recs = _lines(tmp_path)
    assert len(recs) == 1 and recs[0]["candidates"] == []


def test_only_the_exact_keyed_ref_shape_is_accepted(tmp_path):
    """The shape predicate itself, pinned directly — it is consulted from more than one
    place (guard 3 and the key-id read), so mutating either call site alone leaves the
    other standing. This pin makes the PREDICATE load-bearing on its own."""
    v = _Vault(tmp_path)
    assert rm._is_value_ref(rm.value_ref("anything", v))
    for bad in _BAD_CANDIDATE_REFS + (None, "", "hmac256:0123456789abcdef", b"x"):
        assert not rm._is_value_ref(bad), bad


def test_recorder_refuses_an_allowed_class_snapshot_with_a_secret_mode_path(tmp_path):
    """Guard 2's SECRET half, isolated from its class half. A hand-built snapshot whose
    class IS allowed but whose schema_path reads secret-mode must still write nothing —
    the producer-side guard cannot be relied on for a snapshot that crossed a suspend."""
    v = _Vault(tmp_path)
    for path in ("auth/api_key", "creds/password", "x/client_secret"):
        rm.record_ask_avoidable(
            v, ask_id="a",
            snapshot={"schema_path": path, "class": "input", "state": "resolvable",
                      "source": "situation", "value_origin": "operator",
                      "confidence": 0.8, "candidate_ref": rm.value_ref(SECRET, v)},
            answer=SECRET)
    assert _lines(tmp_path) == []
    assert SECRET not in _raw(tmp_path)


def test_stamped_card_snapshot_carries_no_raw_bound_value(tmp_path):
    """The ask-time snapshot is what gets stamped into a card spec (plaintext, persisted
    in the decision queue) — it must carry a digest, never the bound value AND never the
    binder's handle (a ``file:`` handle embeds a real filesystem path)."""
    v = _Vault(tmp_path)
    snap = rm.requirement_snapshot(
        _bound(v, "/secret/path.txt", ref="file:/secret/path.txt", confidence=0.5))
    assert "/secret/path.txt" not in json.dumps(snap)
    assert (snap["candidate_ref"] or "").startswith("hmac256:")


# ═══════════════════════════════════════════════════════════════════════════════
#  1b. F4 — the ref function is KEYED (an unsalted digest is reversible)
# ═══════════════════════════════════════════════════════════════════════════════

def test_answer_ref_is_not_an_unsalted_digest_of_the_answer(tmp_path):
    """F4: ``sha256(value)[:16]`` is brute-forceable for a low-entropy answer (a 6-digit
    code falls in under a second), and ``login/verification_code`` is NOT caught by the
    secret-name tokens. The ref is an HMAC under a PER-VAULT key, so the corpus cannot
    be reversed by anyone who does not also hold the vault's secret."""
    import hashlib
    v = _Vault(tmp_path)
    code = "482913"
    _record(v, _req(kind="input", schema_path="login/verification_code"), code)
    rec = _lines(tmp_path)[0]
    naive = "sha256:" + hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    assert rec["answer_ref"] != naive
    assert hashlib.sha256(code.encode("utf-8")).hexdigest()[:16] not in _raw(tmp_path)
    assert rec["answer_ref"].startswith("hmac256:")


def test_the_same_value_refs_differently_under_different_vault_keys(tmp_path):
    """The key is PER-VAULT: the same answer in two vaults produces two different refs
    (so a shared corpus cannot be correlated against a rainbow table built elsewhere)."""
    a, b = _Vault(tmp_path / "a"), _Vault(tmp_path / "b")
    assert rm.value_ref("hello", a) != rm.value_ref("hello", b)
    assert rm.value_ref("hello", a) == rm.value_ref("hello", a)   # stable within a vault


def test_a_candidate_signed_by_another_vault_key_is_dropped_not_overridden(tmp_path):
    """A snapshot rides a card spec across a suspend; if the vault key changed (or the
    spec came from a different vault) the two digests are simply INCOMPARABLE. Dropping
    the candidate degrades to missing-answered (candidate-only). It must NEVER be
    reported as ``resolvable_overridden`` — that would assert the binder was WRONG on
    the strength of a comparison that was never valid."""
    a, b = _Vault(tmp_path / "a"), _Vault(tmp_path / "b")
    snap = rm.requirement_snapshot(_bound(a, "out/report.md", confidence=0.7))
    rm.record_ask_avoidable(b, ask_id="x", snapshot=snap, answer="out/report.md")
    rec = _lines(tmp_path / "b")[0]
    assert rec["resolution"] == "missing_answered"
    assert rec["candidates"] == []


def test_no_row_is_written_when_no_vault_key_can_be_derived(tmp_path, monkeypatch):
    """Fail-closed: without a key there is no non-reversible ref, and the recorder must
    NOT fall back to writing an unkeyed digest (or the raw answer). No key ⇒ no row."""
    v = _Vault(tmp_path)
    monkeypatch.setattr(rm, "_ref_key",
                        lambda vault: (_ for _ in ()).throw(RuntimeError("no key")))
    _record(v, _req(), "some-answer")
    assert _lines(tmp_path) == []
    assert "some-answer" not in _raw(tmp_path)


def test_value_normalisation_is_symmetric_across_both_sides(tmp_path):
    """The binder digests a typed schema value; the operator answers a form STRING. The
    same normalisation must run on both sides or a genuine confirm reads as an
    override. (Pinned for the two shapes that actually differ: surrounding whitespace
    and a bool rendered ``True`` by Python but ``true`` by a form.)"""
    v = _Vault(tmp_path)
    assert rm.value_ref(True, v) == rm.value_ref("true", v) == rm.value_ref(" TRUE ", v)
    assert rm.value_ref("out/r.md", v) == rm.value_ref("  out/r.md  ", v)
    assert rm.value_ref(5, v) == rm.value_ref("5", v)
    assert rm.value_ref("a", v) != rm.value_ref("b", v)


# ═══════════════════════════════════════════════════════════════════════════════
#  2. The two deterministic sub-cases
# ═══════════════════════════════════════════════════════════════════════════════

def test_resolvable_confirmed_is_definitive_avoidable(tmp_path):
    """Sub-case A — the binder HAD the value and asked only because of T_high /
    content_derived taint; the operator confirmed it unchanged. Avoidable BY
    CONSTRUCTION; near-miss = the bind confidence. No replay needed."""
    v = _Vault(tmp_path)
    _record(v, _bound(v, "out/report.md", confidence=0.62,
                      value_origin="content_derived"), "out/report.md")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed"
    assert rec["class"] == "input"
    assert rec["schema_path"] == "report/output_path"
    assert rec["near_miss_score"] == pytest.approx(0.62)
    assert rec["matched_candidate"] == rec["candidates"][0]["ref"]
    assert rec["candidates"][0]["value_origin"] == "content_derived"
    assert rec["candidates"][0]["score"] == pytest.approx(0.62)
    assert rec["ask_id"] == "hreq_test"


@pytest.mark.parametrize("ref,value", [
    ("file:C:/work/draft.md", "C:/work/draft.md"),
    ("provided:report/output_path", "out/generated.md"),
    ("run_context:out/prior.md", "out/prior.md"),
    ("profile:default_output_dir", "D:/operator/out"),
    ("inventory:capabilities", "pdf_writer"),
    ("service:github#acme-bot", "acme-bot"),
    ("schema_default:output_path", "out/default.md"),
])
def test_definitive_confirmed_fires_for_every_real_binder_handle(tmp_path, ref, value):
    """THE F1 REGRESSION PIN. The handle is OPAQUE and never equals the value, so the
    old ``digest(bound_value_ref) == digest(answer)`` test could not fire for ANY real
    binder output — ``resolvable_confirmed`` was structurally unreachable and every one
    of these recorded ``resolvable_overridden`` (the inverse of the truth). Classified
    off the RESOLVED-VALUE digest, each one is correctly definitive-avoidable."""
    v = _Vault(tmp_path)
    _record(v, _bound(v, value, ref=ref, confidence=0.7), value)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed", rec
    assert rec["matched_candidate"] == rec["candidates"][0]["ref"]


def test_a_bound_requirement_without_a_value_digest_is_candidate_only(tmp_path):
    """A handle with NO value digest (a legacy persisted Requirement, a bind source that
    holds an identifier rather than an extractable value) is not comparable. It records
    ``missing_answered`` — candidate-only, the documented safe direction — and NEVER
    ``resolvable_overridden``, which would claim the binder's value was wrong when the
    binder never offered one."""
    v = _Vault(tmp_path)
    _record(v, _req(state="resolvable", bound_value_ref="profile_fact:f_17",
                    confidence=0.6), "acme/prod")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "missing_answered"
    assert rec["candidates"] == []


def test_resolvable_overridden_is_not_avoidable(tmp_path):
    """The binder had a candidate but the operator answered something ELSE — the ask
    was NECESSARY (the candidate was wrong). Must NOT count as avoidable."""
    v = _Vault(tmp_path)
    _record(v, _bound(v, "out/wrong.md", confidence=0.55), "out/right.md")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_overridden"
    assert rec["matched_candidate"] is None
    assert rec["candidates"][0]["ref"] != rec["answer_ref"]


def test_missing_answered_records_answer_ref_and_no_candidates(tmp_path):
    """Sub-case B — nothing was bound; record the answer ref + the (empty) ask-time
    candidate set. NOT definitive: the definitive verdict needs the resolver-replay."""
    v = _Vault(tmp_path)
    _record(v, _req(state="missing", bound_value_ref=None, confidence=0.0), "acme-prod")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "missing_answered"
    assert rec["candidates"] == []
    assert rec["matched_candidate"] is None
    assert rec["near_miss_score"] == 0.0
    assert (rec["answer_ref"] or "").startswith("hmac256:")


def test_decision_and_capability_classes_are_in_scope(tmp_path):
    v = _Vault(tmp_path)
    _record(v, _req(kind="decision", schema_path="plan/strategy"), "batch")
    _record(v, _req(kind="capability", schema_path="tool/pdf_writer"), "pandoc")
    assert [r["class"] for r in _lines(tmp_path)] == ["decision", "capability"]


def test_empty_or_absent_answer_records_nothing(tmp_path):
    """The signal is ANSWER-linked: a decline / cancel / empty answer is not an answer."""
    v = _Vault(tmp_path)
    _record(v, _req(), None)
    _record(v, _req(), "")
    _record(v, _req(), "   ")
    assert _lines(tmp_path) == []


# ═══════════════════════════════════════════════════════════════════════════════
#  2b. F2 — one ANSWER is one OBSERVATION (never N rows)
# ═══════════════════════════════════════════════════════════════════════════════

def test_same_path_candidates_collapse_to_one_row(tmp_path):
    """F2. ``build_requirement_report`` deliberately keeps same-path/different-value
    asks DISTINCT (``bound_value_ref`` is in its dedupe key), but
    ``elicitation_schema_from_fields`` collapses same-NAMED fields into ONE form
    property. So the operator sees ONE slot while the card carries N snapshots — and
    the join wrote N rows off ONE answer, halving ``definitive_rate`` and inventing an
    override observation the operator never made. One answered path ⇒ ONE row."""
    v = _Vault(tmp_path)
    snaps = [
        rm.requirement_snapshot(_bound(v, "out/a.md", ref="file:out/a.md", confidence=0.5)),
        rm.requirement_snapshot(_bound(v, "out/b.md", ref="file:out/b.md", confidence=0.8)),
    ]
    rm.record_ask_avoidable(v, ask_id="h1", snapshot=snaps, answer="out/b.md")
    recs = _lines(tmp_path)
    assert len(recs) == 1
    # classified confirmed because SOME candidate for the path matched
    assert recs[0]["resolution"] == "resolvable_confirmed"
    assert recs[0]["near_miss_score"] == pytest.approx(0.8)
    assert len(recs[0]["candidates"]) == 2
    assert rm.answer_linked_ask_report(v)["definitive_rate"] == pytest.approx(1.0)


def test_same_path_candidates_none_matching_is_one_override_row(tmp_path):
    v = _Vault(tmp_path)
    snaps = [rm.requirement_snapshot(_bound(v, "out/a.md", ref="file:out/a.md", confidence=0.5)),
             rm.requirement_snapshot(_bound(v, "out/b.md", ref="file:out/b.md", confidence=0.8))]
    rm.record_ask_avoidable(v, ask_id="h1", snapshot=snaps, answer="out/c.md")
    recs = _lines(tmp_path)
    assert len(recs) == 1 and recs[0]["resolution"] == "resolvable_overridden"


def test_a_grouped_element_for_another_path_is_never_counted(tmp_path):
    """The group is keyed by ``schema_path``; the recorder must re-check each element
    rather than trust the caller's grouping. A stray element for a DIFFERENT path (or a
    different class) contributes no candidate — otherwise a mis-grouped stamp could
    credit one requirement's confirm to another, or route a candidate around the
    class guard by hiding behind an allowed head."""
    v = _Vault(tmp_path)
    good = rm.requirement_snapshot(_bound(v, "out/a.md", ref="file:out/a.md",
                                          confidence=0.4))
    stray_path = dict(good, schema_path="other/path",
                      candidate_ref=rm.value_ref("out/z.md", v), confidence=0.9)
    stray_class = dict(good, **{"class": "credential",
                                "candidate_ref": rm.value_ref("out/z.md", v),
                                "confidence": 0.9})
    rm.record_ask_avoidable(v, ask_id="h1", snapshot=[good, stray_path, stray_class],
                            answer="out/z.md")
    rec = _lines(tmp_path)[0]
    assert rec["schema_path"] == "report/output_path"
    assert len(rec["candidates"]) == 1
    assert rec["resolution"] == "resolvable_overridden"
    assert rec["near_miss_score"] == pytest.approx(0.4)


def test_a_grouped_snapshot_still_enforces_the_secret_and_class_guards(tmp_path):
    """The grouped form must not become a guard bypass: a credential element inside a
    list is refused exactly as a lone credential snapshot is."""
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(
        v, ask_id="h1",
        snapshot=[{"schema_path": "auth/api_key", "class": "credential",
                   "state": "missing", "source": "schema", "value_origin": None,
                   "confidence": 0.0, "candidate_ref": None}],
        answer=SECRET)
    assert _lines(tmp_path) == []
    assert SECRET not in _raw(tmp_path)


def test_grant_reconciler_writes_one_row_per_answered_path(tmp_path):
    """F2 at the live join: the bundled card's ``_req_snaps`` can carry two entries for
    one form slot; ``param_answers`` comes back with ONE coerced answer for it."""
    from systemu.scheduler.jobs import record_bundled_ask_outcomes
    v = _Vault(tmp_path)
    dctx = {"request_id": "hreq_dup", "spec": {"requirement_snapshot": [
        rm.requirement_snapshot(_bound(v, "out/a.md", ref="file:out/a.md", confidence=0.5)),
        rm.requirement_snapshot(_bound(v, "out/b.md", ref="file:out/b.md", confidence=0.8)),
    ]}}
    record_bundled_ask_outcomes(v, dctx, {"report/output_path": "out/b.md"})
    recs = _lines(tmp_path)
    assert len(recs) == 1
    assert recs[0]["resolution"] == "resolvable_confirmed"
    rep = rm.answer_linked_ask_report(v)
    assert rep["total"] == 1 and rep["necessary_overridden"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Observability-only discipline (matches record_ask's contract)
# ═══════════════════════════════════════════════════════════════════════════════

def test_recorder_never_raises_on_a_broken_vault():
    class _Bad:
        @property
        def root(self):
            raise RuntimeError("vault exploded")
    rm.record_ask_avoidable(_Bad(), ask_id="a",
                            snapshot=rm.requirement_snapshot(_req()), answer="x")


def test_recorder_never_raises_on_garbage_input(tmp_path):
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(v, ask_id=None, snapshot=None, answer="x")
    rm.record_ask_avoidable(v, ask_id="a", snapshot="not-a-dict", answer="x")
    rm.record_ask_avoidable(v, ask_id="a", snapshot={}, answer="x")
    rm.record_ask_avoidable(v, ask_id="a", snapshot=[], answer="x")
    rm.record_ask_avoidable(v, ask_id="a", snapshot=[None, "junk"], answer="x")
    assert _lines(tmp_path) == []


def test_requirement_snapshot_never_raises_on_garbage():
    assert rm.requirement_snapshot(None) is None
    assert rm.requirement_snapshot(object()) is None
    assert rm.requirement_snapshot({"kind": "input"}) is None      # no schema_path


def test_corpus_is_append_only_and_lf_terminated(tmp_path):
    """Append-only, and one LF per row. The line writer bypasses Python's text layer
    (``os.write`` on a raw fd), so on Windows it MUST pass ``O_BINARY`` — otherwise the
    CRT translates every ``\\n`` into ``\\r\\n`` and the corpus stops matching the
    UTF-8/LF shape every other JSONL artefact in the tree uses."""
    v = _Vault(tmp_path)
    for i in range(3):
        _record(v, _req(schema_path=f"a/p{i}"), f"v{i}")
    assert len(_lines(tmp_path)) == 3
    blob = (Path(tmp_path) / "audit" / "ask_avoidable.jsonl").read_bytes()
    assert b"\r\n" not in blob
    assert blob.count(b"\n") == 3


def test_concurrent_appends_never_lose_a_row(tmp_path):
    """F3. The corpus has TWO real concurrent writers (the pre-loop elicitation rail on
    a shadow exec thread, the harness-grant reconciler on the daemon thread), and its
    CONC-MAP row claims whole-line safety. A BUFFERED text-mode append is not atomic
    across handles: measured 8x150 concurrent writes landed ~1155/1200 rows — no torn
    lines and no exception, just silent loss (which the blanket ``except`` does not even
    see). One ``os.write`` to an O_APPEND fd is atomic; nothing may be lost."""
    v = _Vault(tmp_path)
    threads, per_thread = 8, 150
    barrier = threading.Barrier(threads)

    def _writer(t):
        barrier.wait()
        for i in range(per_thread):
            rm.record_ask_avoidable(
                v, ask_id=f"t{t}-{i}",
                snapshot={"schema_path": f"a/p{t}", "class": "input",
                          "state": "missing", "source": "schema",
                          "value_origin": None, "confidence": 0.0,
                          "candidate_ref": None},
                answer=f"v{t}-{i}")

    ts = [threading.Thread(target=_writer, args=(t,)) for t in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    recs = _lines(tmp_path)
    assert len(recs) == threads * per_thread, (
        f"lost {threads * per_thread - len(recs)} row(s) to concurrent appends")
    assert len({r["ask_id"] for r in recs}) == threads * per_thread


def test_concurrent_appends_never_lose_a_row_without_os_file_locking(tmp_path, monkeypatch):
    """The OS file lock is BEST-EFFORT (``fcntl``/``msvcrt`` may be unavailable, and a
    network filesystem may not honour it). The in-process lock is the guaranteed floor
    and must hold on its own — otherwise removing it looks free until the day OS
    locking silently no-ops."""
    monkeypatch.setattr(rm, "_lock_whole_file", lambda fd: False)
    v = _Vault(tmp_path)
    threads, per_thread = 8, 100
    barrier = threading.Barrier(threads)

    def _writer(t):
        barrier.wait()
        for i in range(per_thread):
            rm.record_ask_avoidable(
                v, ask_id=f"t{t}-{i}",
                snapshot={"schema_path": f"a/p{t}", "class": "input",
                          "state": "missing", "source": "schema",
                          "value_origin": None, "confidence": 0.0,
                          "candidate_ref": None},
                answer=f"v{t}-{i}")

    ts = [threading.Thread(target=_writer, args=(t,)) for t in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(_lines(tmp_path)) == threads * per_thread


def test_loader_skips_malformed_lines(tmp_path):
    d = Path(tmp_path) / "audit"
    d.mkdir(parents=True)
    (d / "ask_avoidable.jsonl").write_text(
        '{"class":"input","resolution":"missing_answered"}\nnot json {\n\n[1,2]\n',
        encoding="utf-8")
    out = rm.load_avoidable_ask_corpus(_Vault(tmp_path))
    assert len(out) == 1 and out[0]["class"] == "input"


def test_absent_corpus_is_a_clean_zero(tmp_path):
    rep = rm.answer_linked_ask_report(_Vault(tmp_path))
    assert rep["total"] == 0 and rep["definitive_avoidable"] == 0
    assert rep["definitive_rate"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#  4. The report — the two sub-cases stay LABELLED APART (definitive vs directional)
# ═══════════════════════════════════════════════════════════════════════════════

def test_report_separates_definitive_from_candidate_subcases(tmp_path):
    v = _Vault(tmp_path)
    # 2 definitive-avoidable, 1 necessary override, 1 missing-answered
    _record(v, _bound(v, "v1", schema_path="a/x", confidence=0.7), "v1")
    _record(v, _bound(v, "v2", schema_path="a/y", confidence=0.6), "v2")
    _record(v, _bound(v, "v3", schema_path="a/z", confidence=0.5), "OTHER")
    _record(v, _req(schema_path="a/w", state="missing"), "v4")
    rep = rm.answer_linked_ask_report(v)
    assert rep["total"] == 4
    assert rep["definitive_avoidable"] == 2
    assert rep["definitive_rate"] == pytest.approx(0.5)
    assert rep["necessary_overridden"] == 1
    assert rep["missing_answered"] == 1
    # the missing-answered bucket must NOT be blurred into the definitive number
    assert rep["definitive_avoidable"] != rep["total"] - rep["necessary_overridden"]


def test_report_breaks_down_by_class(tmp_path):
    v = _Vault(tmp_path)
    _record(v, _bound(v, "v", kind="input", schema_path="a/x", confidence=0.8), "v")
    _record(v, _req(kind="decision", schema_path="a/y", state="missing"), "z")
    rep = rm.answer_linked_ask_report(v)
    assert rep["by_class"]["input"]["definitive_avoidable"] == 1
    assert rep["by_class"]["decision"]["definitive_avoidable"] == 0
    assert rep["by_class"]["decision"]["total"] == 1


def test_ask_to_resolve_conversion_trend(tmp_path):
    """§5.9's ask→resolve conversion: the SAME (class, schema_path) first asked with
    NOTHING bound, later asked with the value already bound ⇒ the world grew and the
    ask is converting to a RESOLVE. Deterministic over the corpus, never an LLM."""
    v = _Vault(tmp_path)
    # group 1: missing → later resolvable  ⇒ CONVERTED
    _record(v, _req(schema_path="a/converted", state="missing"), "val")
    _record(v, _bound(v, "val", schema_path="a/converted", confidence=0.9), "val")
    # group 2: missing → missing           ⇒ eligible, NOT converted
    _record(v, _req(schema_path="a/stuck", state="missing"), "s")
    _record(v, _req(schema_path="a/stuck", state="missing"), "s")
    # group 3: single ask                  ⇒ not eligible (no repeat to judge)
    _record(v, _req(schema_path="a/once", state="missing"), "o")
    conv = rm.answer_linked_ask_report(v)["conversion"]
    assert conv["eligible"] == 2
    assert conv["converted"] == 1
    assert conv["rate"] == pytest.approx(0.5)
    assert conv["repeat_asks"] == 2      # one re-ask in each of the two repeat groups


def test_format_labels_definitive_and_directional_differently(tmp_path):
    v = _Vault(tmp_path)
    _record(v, _bound(v, "v", schema_path="a/x", confidence=0.7), "v")
    _record(v, _req(schema_path="a/y", state="missing"), "q")
    text = "\n".join(rm.format_avoidable_ask(rm.avoidable_ask_report(v)))
    low = text.lower()
    assert "definitive" in low
    assert "directional" in low          # the legacy no-attempt proxy stays labelled so
    assert "conversion" in low or "ask->resolve" in text
    # the two must not be presented as one blended number
    assert "resolvable-confirmed" in low or "resolvable_confirmed" in low
    # ASCII only — cp1252 is the stock Windows console encoding
    text.encode("cp1252")


def test_avoidable_ask_report_embeds_the_answer_linked_block(tmp_path):
    v = _Vault(tmp_path)
    _record(v, _bound(v, "v", schema_path="a/x", confidence=0.7), "v")
    rep = rm.avoidable_ask_report(v)
    assert rep["answer_linked"]["definitive_avoidable"] == 1
    # the legacy DIRECTIONAL proxy is untouched by the new corpus
    assert rep["total_asks"] == 0 and rep["no_attempt_count"] == 0


def test_new_corpus_does_not_perturb_the_legacy_dec7_proxy(tmp_path):
    """The answer-linked events live in their OWN file precisely so they cannot be
    counted by the shipped DEC-7 no-attempt proxy (whose absent-field defaults would
    have silently scored every one of them as an avoidable candidate)."""
    v = _Vault(tmp_path)
    rm.record_ask(v, kind="tool", attempts_before=2, tool_attempts=1)
    for i in range(5):
        _record(v, _req(kind="decision", schema_path=f"a/p{i}", state="missing"), "x")
    rep = rm.avoidable_ask_report(v)
    assert rep["total_asks"] == 1          # only the legacy corpus row
    assert rep["no_attempt_count"] == 0
    assert rep["answer_linked"]["total"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Wiring — the two live answer chokepoints
# ═══════════════════════════════════════════════════════════════════════════════

def test_bundled_scope_card_stamps_a_secret_free_requirement_snapshot(tmp_path):
    """The mid-loop bundled card (R-A13a) destroys its Requirement objects at suspend —
    ``ShadowRuntime._build_bundled_scope_card`` builds them and only ``spec`` survives
    to answer time. So the snapshot MUST be stamped at card-build time. Credential
    requirements are excluded from the stamp (they also go URL-mode, never into
    requested_schema)."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    v = _Vault(tmp_path)
    rt = ShadowRuntime.__new__(ShadowRuntime)
    bundle = [
        _bound(v, "/tmp/out.md", ref="file:/tmp/out.md", kind="input",
               schema_path="report/output_path", confidence=0.58),
        _req(kind="credential", schema_path="auth/api_key", state="missing"),
    ]
    card = ShadowRuntime._build_bundled_scope_card(
        rt, "write_report", bundle, {"a": 1}, "why")
    snaps = card.spec.get("requirement_snapshot")
    assert isinstance(snaps, list) and len(snaps) == 1
    assert snaps[0]["schema_path"] == "report/output_path"
    assert snaps[0]["class"] == "input"
    assert "/tmp/out.md" not in json.dumps(card.spec)
    assert "auth/api_key" not in json.dumps(snaps)


def test_elicitation_chokepoint_records_on_accept(tmp_path, monkeypatch):
    """The pre-loop B10 rail is the one place where the full Requirement AND the accept
    envelope are in the same frame — record there."""
    from systemu.runtime import elicitation as el
    v = _Vault(tmp_path)
    monkeypatch.setattr(el, "resolve_structured_input",
                        lambda **kw: {"action": "accept",
                                      "content": {"output_path": "out/r.md"}})
    el.surface_ask_bundle_requirement(
        _bound(v, "out/r.md", ref="file:out/r.md", confidence=0.44),
        vault=v, config=None)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed"
    assert rec["near_miss_score"] == pytest.approx(0.44)


@pytest.mark.parametrize("envelope", [
    {"action": "decline", "content": {}},
    {"action": "cancel", "content": {}},
    # A decline/cancel that still carries content (a half-filled form; an MCP
    # elicitation callback is free to return this). ONLY the action check can reject
    # it — the empty-answer guard does not fire. Mutation finding: without this case
    # the ``action == "accept"`` check was unkillable.
    {"action": "decline", "content": {"output_path": "half-typed"}},
    {"action": "cancel", "content": {"output_path": "half-typed"}},
])
def test_elicitation_chokepoint_records_nothing_unless_accepted(
        tmp_path, monkeypatch, envelope):
    """The signal is ANSWER-linked: only an ``accept`` is an answer."""
    from systemu.runtime import elicitation as el
    v = _Vault(tmp_path)
    monkeypatch.setattr(el, "resolve_structured_input", lambda **kw: envelope)
    el.surface_ask_bundle_requirement(_req(), vault=v, config=None)
    assert _lines(tmp_path) == []


def test_elicitation_chokepoint_never_breaks_the_ask(tmp_path, monkeypatch):
    """Observability-only: a recorder explosion must not change what the rail returns."""
    from systemu.runtime import elicitation as el
    v = _Vault(tmp_path)
    monkeypatch.setattr(el, "resolve_structured_input",
                        lambda **kw: {"action": "accept", "content": {"output_path": "x"}})
    monkeypatch.setattr(rm, "record_ask_avoidable",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = el.surface_ask_bundle_requirement(_req(), vault=v, config=None)
    assert out["action"] == "accept"


def test_grant_reconciler_joins_the_stamped_snapshot_to_the_answer(tmp_path):
    """The answer-time join: at the harness-grant reconciler both the stamped snapshot
    (dctx['spec']) and the coerced operator answers are in scope, keyed by the SAME
    full schema_path. One AskWasAvoidable per answered requirement."""
    from systemu.scheduler.jobs import record_bundled_ask_outcomes
    v = _Vault(tmp_path)
    dctx = {
        "request_id": "hreq_abc123",
        "spec": {"requirement_snapshot": [
            {"schema_path": "report/output_path", "class": "input", "state": "resolvable",
             "source": "situation", "value_origin": "operator", "confidence": 0.66,
             "candidate_ref": rm.value_ref("out/r.md", v)},
            {"schema_path": "plan/mode", "class": "decision", "state": "missing",
             "source": "schema", "value_origin": None, "confidence": 0.0,
             "candidate_ref": None},
        ]},
    }
    record_bundled_ask_outcomes(
        v, dctx, {"report/output_path": "out/r.md", "plan/mode": "batch"})
    recs = _lines(tmp_path)
    assert len(recs) == 2
    by_path = {r["schema_path"]: r for r in recs}
    assert by_path["report/output_path"]["resolution"] == "resolvable_confirmed"
    assert by_path["report/output_path"]["ask_id"] == "hreq_abc123"
    assert by_path["report/output_path"]["near_miss_score"] == pytest.approx(0.66)
    assert by_path["plan/mode"]["resolution"] == "missing_answered"
    assert "out/r.md" not in _raw(tmp_path)


def test_grant_reconciler_join_is_a_noop_without_a_stamp(tmp_path):
    """Cards from the other INPUT rails (missing_required, B9 fold) carry no stamp —
    the join must be a strict no-op for them, never a guess."""
    from systemu.scheduler.jobs import record_bundled_ask_outcomes
    v = _Vault(tmp_path)
    record_bundled_ask_outcomes(v, {"request_id": "x", "spec": {}}, {"a": "b"})
    record_bundled_ask_outcomes(v, {}, {"a": "b"})
    assert _lines(tmp_path) == []


# NOTE: the exactly-once PLACEMENT pin for this join (it must sit after the
# ``harness_grant_dispatched`` stamp) lives in
# ``tests/test_ra16_join_placement.py`` — it reads source via inspect.getsource,
# and conftest auto-tags a WHOLE MODULE ``source_sensitive`` on that, which would
# drop every pin in THIS file (the secret-exclusion ones included) out of the
# edit-safe gate. The BINDER_REF_PREFIXES completeness pin lives there too.


def test_grant_reconciler_join_never_raises(tmp_path):
    from systemu.scheduler.jobs import record_bundled_ask_outcomes
    record_bundled_ask_outcomes(None, None, None)
    record_bundled_ask_outcomes(_Vault(tmp_path), {"spec": "junk"}, "junk")


# ═══════════════════════════════════════════════════════════════════════════════
#  6. END-TO-END over REAL binder output — the pin that would have caught F1
# ═══════════════════════════════════════════════════════════════════════════════

class _Cap:
    name = "write_report"

    def __init__(self, schema):
        self.parameters_schema = schema
        self.effect_tags = ["local_write"]


class _Obj:
    id = 1
    goal = "write the report"
    success_criteria = "file exists"
    requires_external_verification = False


class _Ctx:
    def __init__(self, vault, produced):
        self.vault = vault
        self.files_produced = list(produced)


def test_real_binder_output_classifies_as_definitive_avoidable(tmp_path):
    """END-TO-END over what the BINDER ACTUALLY EMITS — no hand-written
    ``bound_value_ref``. This is the pin the original slice lacked: every fixture there
    used a value-shaped ref that no bind source produces, so the whole definitive
    sub-case was dead on arrival and no test could see it.

    Here the run-context source binds a prior objective's produced file, the operator
    confirms that exact path, and the event must read ``resolvable_confirmed``."""
    from systemu.runtime.requirement_binder import build_requirement_report

    v = _Vault(tmp_path)
    ctx = _Ctx(v, ["out/prior_report.md"])
    cap = _Cap({"out_path": {"type": "string", "description": "where to write"}})
    report = build_requirement_report([_Obj()], cap, {}, ctx)

    ask = list(report.ask_bundle)
    assert len(ask) == 1, ask
    req = ask[0]
    # the REAL shape: a namespaced handle (never the bare value) + a keyed value digest
    assert req.bound_value_ref == "run_context:out/prior_report.md"
    assert req.bound_value_ref != "out/prior_report.md"
    assert req.bound_value_digest == rm.value_ref("out/prior_report.md", v)

    _record(v, req, "out/prior_report.md")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed", rec
    assert rm.answer_linked_ask_report(v)["definitive_rate"] == pytest.approx(1.0)
    assert "out/prior_report.md" not in _raw(tmp_path)


def test_real_binder_output_records_an_override_when_the_operator_changes_it(tmp_path):
    from systemu.runtime.requirement_binder import build_requirement_report
    v = _Vault(tmp_path)
    ctx = _Ctx(v, ["out/prior_report.md"])
    cap = _Cap({"out_path": {"type": "string"}})
    req = build_requirement_report([_Obj()], cap, {}, ctx).ask_bundle[0]
    _record(v, req, "out/something_else.md")
    assert _lines(tmp_path)[0]["resolution"] == "resolvable_overridden"


def test_the_binder_value_digest_gate_refuses_credentials_and_secret_paths(tmp_path):
    """The two producer-side digest guards, pinned DIRECTLY.

    End-to-end the credential source also returns no resolved value, so the KIND guard
    is masked by that and survives deletion invisibly. Driving the gate itself keeps
    both guards independently load-bearing — they are the reason no secret-derived
    datum is ever even COMPUTED, let alone carried across the suspend."""
    from systemu.runtime import requirement_binder as rb
    bc = rb._BindCtx(situation={}, ctx=_Ctx(_Vault(tmp_path), []), granted=None)
    # a non-secret leaf DOES get a digest — without this the pin could not fail
    assert rb._value_digest(bc, kind="input", schema_path="report/out", value="x")
    # (a) the KIND guard
    assert rb._value_digest(bc, kind="credential", schema_path="report/out",
                            value="x") is None
    # (b) the secret-NAME guard, via the canonical is_secret_field marker
    for path in ("auth/api_key", "creds/password", "x/client_secret", "billing/cvv"):
        assert rb._value_digest(bc, kind="input", schema_path=path, value="x") is None


def test_the_binder_never_stamps_a_value_digest_on_a_credential_kind_bind(tmp_path):
    """The secret-exclusion boundary sits at the PRODUCER too, not only at the recorder.
    The KIND half: a leaf bound from the credentials inventory is ``kind="credential"``
    and must carry no value digest at all."""
    from systemu.runtime.requirement_binder import compute_requirements
    v = _Vault(tmp_path)
    ctx = _Ctx(v, [])                      # no run-context bind: force the cred source
    cap = _Cap({"openai_account": {"type": "string"}})
    reqs = compute_requirements(_Obj(), cap, {"credentials": ["openai"]}, ctx)
    assert [r.kind for r in reqs] == ["credential"], [(r.kind, r.bound_value_ref)
                                                      for r in reqs]
    assert reqs[0].bound_value_ref == "credential:openai"
    assert reqs[0].bound_value_digest is None
    assert rm.requirement_snapshot(reqs[0]) is None


def test_the_binder_never_stamps_a_value_digest_on_a_secret_named_leaf(tmp_path):
    """The NAME half, independent of kind: a leaf bound from an ordinary (non-secret)
    source whose schema_path nonetheless reads secret-mode gets no digest either —
    using the canonical ``is_secret_field`` marker, not a bespoke rule."""
    from systemu.runtime.requirement_binder import compute_requirements
    v = _Vault(tmp_path)
    ctx = _Ctx(v, ["some/produced.txt"])
    cap = _Cap({"password": {"type": "string"}, "client_secret": {"type": "string"}})
    reqs = compute_requirements(_Obj(), cap, {}, ctx)
    assert reqs, "expected the binder to emit requirements"
    for r in reqs:
        assert r.kind != "credential", "this pin must exercise the NAME guard, not kind"
        assert r.bound_value_ref, "expected a real bind (else the pin proves nothing)"
        assert r.bound_value_digest is None, (r.schema_path, r.bound_value_ref)
        assert rm.requirement_snapshot(r) is None


def test_the_realism_predicate_rejects_a_value_shaped_ref():
    """The realism scan below can only be as good as its predicate, and a scan that
    finds nothing to reject looks identical to a scan that cannot reject anything.
    Pin BOTH directions on the shapes that actually matter here."""
    for synthetic in ("out/r.md", "/tmp/out.md", "C:/work/draft.md", "acme/prod", ""):
        assert not synthetic.startswith(BINDER_REF_PREFIXES), synthetic
    for real in ("file:C:/work/draft.md", "profile:email", "run_context:out/p.md",
                 "provided:report/out", "credential:openai", "schema_default:k"):
        assert real.startswith(BINDER_REF_PREFIXES), real


def test_no_fixture_uses_a_value_shaped_bound_value_ref():
    """THE FIXTURE-REALISM PIN (the meta-lesson of F1).

    Every ``bound_value_ref`` literal in this module must carry a namespace a REAL
    bind source emits. The original slice's fixtures all used a bare value
    (``"out/r.md"``), which no binder produces — so the pins agreed with each other
    and disagreed with production, and a structurally-unreachable code path read as
    fully covered. If a future fixture regresses to a synthetic shape, this fails."""
    text = Path(__file__).read_text(encoding="utf-8")
    found = re.findall(r"bound_value_ref=[\"']([^\"']*)[\"']", text)
    assert found, "the realism scan matched nothing — the pin has gone blind"
    bad = [r for r in found if not r.startswith(BINDER_REF_PREFIXES)]
    assert not bad, (
        f"fixture(s) use a ref shape NO binder emits: {sorted(set(bad))}. Real bind "
        f"sources emit a namespaced handle ({', '.join(BINDER_REF_PREFIXES)}); a "
        f"value-shaped ref makes the pin agree with itself and not with production."
    )
