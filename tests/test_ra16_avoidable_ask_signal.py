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
                bound_value_digest=None, bound_value_canon_digest=None,
                confidence=0.0, rationale="where to write")
    base.update(kw)
    return Requirement(**base)


def _bound(vault, value, *, ref=None, **kw):
    """A REALISTICALLY bound requirement: a NAMESPACED binder handle plus BOTH keyed
    digests of the bind's RESOLVED VALUE — exactly the triple ``_emit_requirement``
    now stamps. ``value`` is what the operator would have to type to confirm.

    The canonical twin is stamped here for the same fixture-realism reason the exact
    digest is: a real bind emits both, so a helper that emitted only one would leave
    the form-insensitive comparison structurally unreachable in every test that uses
    it — the identical failure mode as F1's value-shaped ``bound_value_ref``."""
    kw.setdefault("state", "resolvable")
    return _req(bound_value_ref=(ref or f"run_context:{value}"),
                bound_value_digest=rm.value_ref(value, vault),
                bound_value_canon_digest=rm.canonical_value_ref(value, vault), **kw)


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
    """The producer-side digest guards, pinned DIRECTLY — the OUTCOME, not independence.

    Read this before trusting it: it does NOT establish that both guards are
    independently load-bearing. `_is_secret_path` stamps `format="password"` for any
    `kind == "credential"` regardless of schema_path, so assertion (a) below passes via
    the NAME guard alone; deleting the KIND guard leaves every test green. The KIND
    guard is a redundant short-circuit under every current path, not dead code — it
    executes and returns first — and the credential bind source additionally returns no
    resolved value at all, so end-to-end there are THREE protections and this test can
    kill none of them individually.

    What it does pin: for a credential kind and for a secret-NAMED leaf, no digest is
    ever computed — so no secret-derived datum is carried across the suspend. That
    guarantee is what matters; which guard delivers it is not pinned here.

    (An earlier version of this docstring claimed the opposite. It was false, and in
    this codebase a docstring asserting a property its test does not establish has
    already propagated into a wrong fix.)"""
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
    ctx = _Ctx(v, [])
    cap = _Cap({"password": {"type": "string"}, "client_secret": {"type": "string"}})
    # The positive control — "a real bind whose digest is absent" — comes from source #0.
    # It used to come from ``files_produced``, but source #2 is now PATH-ONLY and these
    # are not path leaves; that channel also bound a FILE PATH into ``password``, which
    # is the exact wrong pre-fill the gate exists to stop.
    reqs = compute_requirements(_Obj(), cap, {}, ctx,
                                provided_params={"password": "hunter2xyz",
                                                 "client_secret": "sekrit-abc"})
    assert reqs, "expected the binder to emit requirements"
    for r in reqs:
        assert r.kind != "credential", "this pin must exercise the NAME guard, not kind"
        assert r.bound_value_ref, "expected a real bind (else the pin proves nothing)"
        assert r.bound_value_digest is None, (r.schema_path, r.bound_value_ref)
        assert rm.requirement_snapshot(r) is None


def _real_execution_context(produced):
    """A REAL ``ExecutionContext``, built with the exact keyword set production uses at
    ``shadow_runtime.py:4862`` — not a stand-in. ``_Ctx`` above carries a ``.vault`` the
    real object does not have, which is precisely the shape difference this section
    exists to pin."""
    from systemu.runtime.context_builder import ExecutionContext
    ctx = ExecutionContext(
        execution_id="exec-ra16",
        system_prompt="sp",
        scroll_json=[],
        tool_index=[],
        skill_index=[],
        recalled_memory="",
        use_objectives=True,
        scroll_intent="write the report",
    )
    ctx.files_produced = list(produced)
    return ctx


def test_a_real_execution_context_has_no_vault_so_it_must_be_threaded(tmp_path):
    """THE CTX-SHAPE FIXTURE-REALISM PIN — the same defect class as F1, one layer up.

    F1's lesson was applied to the ``bound_value_ref`` SHAPE but not to the ``ctx``
    shape. ``_Ctx`` above carries a ``.vault``; the real ``ExecutionContext`` does not
    and never did, so ``_value_digest``'s ``getattr(ctx, "vault", None)`` lookup
    returned ``None`` on EVERY production bind and the digest was never once stamped in
    a real run — while every test here passed, because they all handed the binder a
    fixture that happened to have the attribute.

    Two assertions, and BOTH are load-bearing:
      (a) a real ``ExecutionContext`` has no ``vault``. This fails the day someone
          "fixes" the threading by hanging a live vault handle on the context instead —
          which is exactly the wrong repair (the context is serialized and snapshotted).
      (b) with the vault threaded EXPLICITLY, the digest is populated."""
    from systemu.runtime.requirement_binder import build_requirement_report

    v = _Vault(tmp_path)
    ctx = _real_execution_context(["out/prior_report.md"])

    # (a) the shape fact the whole commit rests on
    assert not hasattr(ctx, "vault"), (
        "a real ExecutionContext must NOT carry a vault — it is serialized and "
        "snapshotted, and attaching a live handle invites a snapshot-shape "
        "regression. Thread the vault through build_requirement_report instead.")

    cap = _Cap({"out_path": {"type": "string", "description": "where to write"}})

    # without the thread there is no key ⇒ no digest (the production behaviour that
    # made resolvable_confirmed structurally unreachable)
    unthreaded = build_requirement_report([_Obj()], cap, {}, ctx).ask_bundle[0]
    assert unthreaded.bound_value_ref == "run_context:out/prior_report.md", (
        "the binder must genuinely BIND here, else this pin proves nothing")
    assert unthreaded.bound_value_digest is None

    # (b) threaded explicitly ⇒ the digest IS stamped
    req = build_requirement_report([_Obj()], cap, {}, ctx, vault=v).ask_bundle[0]
    assert req.bound_value_ref == "run_context:out/prior_report.md"
    assert req.bound_value_digest == rm.value_ref("out/prior_report.md", v)


def test_compute_requirements_threads_the_vault_too(tmp_path):
    """The per-objective core is a public §5.3 entry in its own right; the kwarg must
    reach ``_BindCtx`` by that path as well, not only through the aggregator."""
    from systemu.runtime.requirement_binder import compute_requirements

    v = _Vault(tmp_path)
    ctx = _real_execution_context(["out/prior_report.md"])
    cap = _Cap({"out_path": {"type": "string"}})
    reqs = compute_requirements(_Obj(), cap, {}, ctx, vault=v)
    assert reqs and reqs[0].bound_value_digest == rm.value_ref("out/prior_report.md", v)


def test_a_confirm_over_a_real_context_is_definitive_end_to_end(tmp_path):
    """END-TO-END over the REAL producer chain with the REAL context object:
    ExecutionContext → binder → ``requirement_snapshot`` → ``record_ask_avoidable``.

    ``requirement_snapshot()`` is called for real rather than hand-building a snapshot
    dict — a hand-built snapshot silently supplies the very ``candidate_ref`` whose
    absence IS the defect, which produces a false pass."""
    from systemu.runtime.requirement_binder import build_requirement_report

    v = _Vault(tmp_path)
    ctx = _real_execution_context(["out/prior_report.md"])
    cap = _Cap({"out_path": {"type": "string"}})
    req = build_requirement_report([_Obj()], cap, {}, ctx, vault=v).ask_bundle[0]

    snap = rm.requirement_snapshot(req)          # the REAL producer, never a literal
    assert snap and snap["candidate_ref"], "no candidate_ref ⇒ the join cannot fire"

    rm.record_ask_avoidable(v, ask_id="hreq_real", snapshot=snap,
                            answer="out/prior_report.md")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed", rec
    assert rec["matched_candidate"] == snap["candidate_ref"]
    assert "out/prior_report.md" not in _raw(tmp_path)


def test_an_override_over_a_real_context_is_recorded_as_necessary(tmp_path):
    """The other half: the binder held a value and the operator answered something
    else ⇒ ``resolvable_overridden`` (the ask was NECESSARY). Pre-fix BOTH this and the
    confirm above degraded to ``missing_answered`` — "the binder had nothing" — which
    was false; it had a value."""
    from systemu.runtime.requirement_binder import build_requirement_report

    v = _Vault(tmp_path)
    ctx = _real_execution_context(["out/prior_report.md"])
    cap = _Cap({"out_path": {"type": "string"}})
    req = build_requirement_report([_Obj()], cap, {}, ctx, vault=v).ask_bundle[0]

    rm.record_ask_avoidable(v, ask_id="hreq_real", snapshot=rm.requirement_snapshot(req),
                            answer="out/something_else.md")
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_overridden", rec
    assert rec["matched_candidate"] is None


def test_a_credential_leaf_stamps_no_digest_even_with_the_vault_threaded(tmp_path):
    """SECRET REGRESSION. Threading a real vault makes the digest computable for the
    first time in production, so the producer-side secret boundary is now genuinely
    load-bearing rather than masked by an always-``None`` vault. A credential-kind bind
    must still stamp NO digest and still produce NO row."""
    from systemu.runtime.requirement_binder import compute_requirements

    v = _Vault(tmp_path)
    ctx = _real_execution_context([])            # no run-context bind: force the cred source
    cap = _Cap({"openai_account": {"type": "string"}})
    reqs = compute_requirements(_Obj(), cap, {"credentials": ["openai"]}, ctx, vault=v)

    assert [r.kind for r in reqs] == ["credential"], [(r.kind, r.bound_value_ref)
                                                      for r in reqs]
    assert reqs[0].bound_value_ref == "credential:openai"
    assert reqs[0].bound_value_digest is None
    assert rm.requirement_snapshot(reqs[0]) is None

    rm.record_ask_avoidable(v, ask_id="hreq_cred",
                            snapshot=rm.requirement_snapshot(reqs[0]), answer="openai")
    assert _lines(tmp_path) == [], "a credential ask must never reach the corpus"


def test_a_secret_named_leaf_stamps_no_digest_even_with_the_vault_threaded(tmp_path):
    """The NAME half of the boundary, likewise re-pinned under a threaded vault."""
    from systemu.runtime.requirement_binder import compute_requirements

    v = _Vault(tmp_path)
    ctx = _real_execution_context([])
    cap = _Cap({"password": {"type": "string"}, "client_secret": {"type": "string"}})
    # source #0 supplies the positive-control bind (see the sibling pin above): source
    # #2 is PATH-ONLY now and would not fire on these leaves.
    reqs = compute_requirements(_Obj(), cap, {}, ctx, vault=v,
                                provided_params={"password": "hunter2xyz",
                                                 "client_secret": "sekrit-abc"})
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


# ═══════════════════════════════════════════════════════════════════════════════
#  F2 — the CANONICAL-FORM comparison, the version stamp, and the explicit pick
#
#  THE DEFECT. `record_ask_avoidable` classified by exact equality over minimally-
#  normalised keyed digests. A confirmed answer differing only in FORM — a separator
#  swapped, a quote pair a widget added, a trailing period, a URL-encoded space, case
#  on a path — compared unequal and scored `resolvable_overridden`: "the ask was
#  NECESSARY, the binder was wrong". The exact inverse of what happened, and the same
#  family as F1 (which compared against a namespaced HANDLE and likewise reported the
#  inverse).
#
#  WHY IT IS URGENT RATHER THAN MERELY WRONG. The corpus is APPEND-ONLY and its refs
#  are NON-REVERSIBLE, so a row can never be re-scored: every mis-labelled row is
#  permanent. The error is DIRECTIONAL — the definitive count reads low and the
#  "necessary" count high — in a metric that feeds a decision about how often systemu
#  asks for what it already knew, and it is blind in exactly the direction an
#  over-clamping taint fix would show up. It also suppresses G-LEARN S4's
#  `threshold_sensitive` counters, which only count `resolvable_confirmed` rows.
# ═══════════════════════════════════════════════════════════════════════════════

#: Reshapes of ONE bound path that must all read as a CONFIRM. Every one is a real
#: widget/round-trip artefact, not a hypothetical.
_RESHAPES = [
    ("out/prior_report.md", "exact - v1 behaviour, must not regress"),
    ("out\\prior_report.md", "separator swapped"),
    ("OUT/PRIOR_REPORT.MD", "case folded on a path"),
    ('"out/prior_report.md"', "surrounding double quotes"),
    ("'out/prior_report.md'", "surrounding single quotes"),
    ("out/prior_report.md.", "trailing period"),
    ("out/prior_report.md,", "trailing comma"),
    ("out%2Fprior_report.md", "URL-encoded separator"),
    ("out//prior_report.md", "duplicated separator"),
    ("  out/prior_report.md  ", "surrounding whitespace"),
]

#: Answers that are GENUINELY different values. These pin the line the canonical form
#: must never cross: a prefix, a suffix, a sibling and an extension-stripped path are
#: all reachable by a substring-style fold, and every one of them would be a FALSE
#: confirmation — inflating exactly the number this fix exists to make trustworthy.
_NOT_CONFIRMS = [
    ("out/other_report.md", "a different file in the same directory"),
    ("out", "a PREFIX of the candidate"),
    ("prior_report.md", "a SUFFIX of the candidate"),
    ("out/prior_report", "the extension dropped"),
    ("outprior_report.md", "the separator DELETED, not normalised"),
    ("out/prior_report.md.bak", "a longer path that CONTAINS the candidate"),
]


def _bound_via_real_binder(v, produced="out/prior_report.md"):
    """The REAL producer chain — ExecutionContext → binder → Requirement.

    Hand-built snapshot dicts have produced false results in this module repeatedly
    (F1's whole lesson), so the candidate digests here are the ones a real bind
    stamps, canonical twin included."""
    from systemu.runtime.requirement_binder import build_requirement_report
    ctx = _real_execution_context([produced])
    cap = _Cap({"out_path": {"type": "string", "description": "where to write"}})
    req = build_requirement_report([_Obj()], cap, {}, ctx, vault=v).ask_bundle[0]
    assert req.bound_value_ref == f"run_context:{produced}", req.bound_value_ref
    return req


@pytest.mark.parametrize("answer,label", _RESHAPES, ids=[l for _, l in _RESHAPES])
def test_a_reshaped_answer_is_a_CONFIRM_not_an_override(tmp_path, answer, label):
    """THE FIX. Each of these is the operator confirming the binder's own value, and
    each used to score `resolvable_overridden` — "the ask was necessary"."""
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), answer)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed", (
        f"{label}: a confirm differing only in FORM was scored "
        f"{rec['resolution']!r} — the inverse of what happened")
    assert rec["matched_candidate"], label


@pytest.mark.parametrize("answer,label", _NOT_CONFIRMS,
                         ids=[l for _, l in _NOT_CONFIRMS])
def test_a_genuinely_different_answer_is_still_an_override(tmp_path, answer, label):
    """THE LINE THE FOLD MUST NOT CROSS — the negative half, and the more important
    one. Canonicalising into a substring/prefix match would manufacture FALSE
    confirmations, which is strictly worse than the under-count being fixed."""
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), answer)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_overridden", (
        f"{label}: a genuinely different value was scored a CONFIRM — the canonical "
        f"form has collapsed into a substring match and is inventing avoidable asks")
    assert rec["matched_candidate"] is None, label


def test_the_comparison_is_exact_over_canonical_forms_not_a_containment_test():
    """Directly on the canonicaliser: it is a TOTAL rewrite compared with ``==``.

    Pinned at this level too because the record-level pins above could be satisfied by
    a fold that happens to separate these particular fixtures while still being
    substring-ish for other inputs."""
    c = rm.canonical_compare_form
    assert c("out/report.md") == c("OUT\\Report.MD.")          # form only
    # structure is never deleted, and containment is never equality
    for other in ("out", "report.md", "outreport.md", "out/report",
                  "out/report.md.bak", "a/out/report.md"):
        assert c("out/report.md") != c(other), other
    # interior punctuation is preserved — only TRAILING is stripped
    assert c("a.b.c") != c("abc")
    # a UNC root is meaningful and must not fold into an absolute path
    assert c("//srv/share/f.md") != c("/srv/share/f.md")
    # an all-punctuation answer keeps its form rather than collapsing to ""
    assert c("...") != ""


def test_the_canonical_form_folds_separators_independently_of_the_platform():
    """A PLATFORM-INDEPENDENT pin, and it exists because a mutation survived without it.

    Deleting the separator fold from the canonical form changed NOTHING in the
    record-level reshape pins — on Windows ``normalize_value`` already normcases
    separators, so the exact digest matches first and the canonical pass is never
    reached. On POSIX ``normalize_value`` deliberately does nothing (``a\\b`` is a
    legal filename there), so the canonical fold is the ONLY thing folding
    ``out/r.md`` against ``out\\r.md`` — and a Windows-only suite could not see it
    being removed. Asserted directly on the canonicaliser, where the platform cannot
    mask it."""
    c = rm.canonical_compare_form
    assert c("out\\r.md") == c("out/r.md") == "out/r.md"
    assert c("C:\\work\\sprint.xlsx") == c("C:/work/sprint.xlsx")
    # and the fold must NORMALISE separators, never delete them
    assert c("out\\r.md") != c("outr.md")


def test_requirement_snapshot_refuses_a_raw_value_in_the_canonical_field():
    """GUARD 1's twin, pinned where it actually bites — also added because a mutation
    survived without it.

    The snapshot is stamped into a card spec that is PERSISTED IN PLAINTEXT, so this
    guard runs one layer earlier than the recorder's: a Requirement carrying a raw
    value in ``bound_value_canon_digest`` would write that value into the card spec
    before the recorder ever saw it. The recorder-level guard cannot cover this path,
    which is why removing this one changed no test until now."""
    r = _req(bound_value_ref="run_context:out/r.md",
             bound_value_canon_digest="out/r.md")     # a RAW value, not a digest
    snap = rm.requirement_snapshot(r)
    assert snap["candidate_canon_ref"] is None, (
        "a raw value survived into the snapshot and would be persisted in the "
        "plaintext card spec")
    # not vacuous: a genuine canonical digest IS carried through
    good = _req(bound_value_ref="run_context:out/r.md",
                bound_value_canon_digest="hmac256c:" + "0" * 8 + ":" + "0" * 16)
    assert rm.requirement_snapshot(good)["candidate_canon_ref"] is not None


def test_the_canonical_fold_does_not_eat_a_lone_or_mismatched_quote():
    """Only MATCHED pairs are wrappers. A lone quote is part of the value."""
    c = rm.canonical_compare_form
    assert c('"out/r.md') != c("out/r.md")
    assert c("out/r.md'") != c("out/r.md")


def test_a_non_escape_percent_survives_url_decoding():
    """`unquote` must not mangle a value that merely CONTAINS a percent sign."""
    c = rm.canonical_compare_form
    assert c("100% cotton") == "100% cotton"
    assert c("%APPDATA%/systemu") == "%appdata%/systemu"
    assert c("50%") == "50%"


# ── the version stamp ────────────────────────────────────────────────────────

def test_every_row_carries_the_scoring_rule_that_produced_it(tmp_path):
    """The corpus is append-only with non-reversible digests, so a row can NEVER be
    re-scored. A corpus that cannot say which rule produced which row cannot be
    trusted later — the v1 rows' inflated `resolvable_overridden` count has to stay
    visible rather than being averaged into the v2 rows."""
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), "out/prior_report.md")
    rec = _lines(tmp_path)[0]
    assert rec["scoring_version"] == rm.ASK_SCORING_VERSION
    assert rm.ASK_SCORING_VERSION >= 2, (
        "the canonical-comparison rule must be a NEW version — reusing v1 makes the "
        "two populations inseparable in an append-only file")


def test_the_report_separates_the_two_scoring_populations(tmp_path):
    """A mixed corpus must stay interpretable: old rows report as v1 even though they
    predate the stamp entirely."""
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), "out/prior_report.md")
    # a legacy row, exactly as v1 wrote it — no scoring_version key at all
    path = Path(tmp_path) / "audit" / "ask_avoidable.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ask_id": "legacy", "class": "input",
                             "schema_path": "report/output_path",
                             "resolution": "resolvable_overridden",
                             "candidates": [], "matched_candidate": None}) + "\n")
    rep = rm.answer_linked_ask_report(v)
    assert rep["scoring_versions"] == {"2": 1, "1": 1}, rep["scoring_versions"]
    assert rep["scoring_version"] == rm.ASK_SCORING_VERSION


def test_the_row_records_WHICH_witness_confirmed_it(tmp_path):
    """`match_basis` makes the confirm auditable: an exact match, a form-only match
    and an explicit pick are different strengths of evidence."""
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), "out/prior_report.md")
    assert _lines(tmp_path)[0]["match_basis"] == "digest"

    sub = Path(tmp_path) / "b"
    v2 = _Vault(sub)
    _record(v2, _bound_via_real_binder(v2), '"out/prior_report.md".')
    rec = [json.loads(x) for x in
           (sub / "audit" / "ask_avoidable.jsonl")
           .read_text(encoding="utf-8").splitlines() if x.strip()][0]
    assert rec["match_basis"] == "canonical", rec
    assert rec["resolution"] == "resolvable_confirmed"


def test_an_override_records_no_match_basis(tmp_path):
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), "out/something_else.md")
    assert _lines(tmp_path)[0]["match_basis"] is None


# ── the explicit R-B4/F3 pick marker ─────────────────────────────────────────

def test_an_explicit_pick_outranks_a_digest_mismatch(tmp_path):
    """R-B4 shipped a marker recording what the UI KNOWS at the moment of the click.
    It is ground truth about this very question, so it outranks any inference."""
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(v, ask_id="a",
                            snapshot=rm.requirement_snapshot(_bound_via_real_binder(v)),
                            answer="a value that digests differently", picked=True)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed", rec
    assert rec["match_basis"] == "picked"
    assert rec["matched_candidate"], "a pick must credit a real candidate"


def test_a_pick_cannot_invent_a_candidate_that_never_existed(tmp_path):
    """The marker is attacker-shaped (it rides a persisted decision across a suspend).
    With NO comparable candidate there was nothing to pick, so the row must stay
    `missing_answered` — a forged pick can never fabricate a confirm out of nothing."""
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(v, ask_id="a", snapshot=rm.requirement_snapshot(_req()),
                            answer="anything", picked=True)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "missing_answered", rec
    assert rec["matched_candidate"] is None


@pytest.mark.parametrize("bogus", ["yes", "true", 1, {"a": 1}, [], (), "", 0, None])
def test_only_a_real_pick_assertion_counts(tmp_path, bogus):
    """A truthy-looking non-assertion must not be read as a pick."""
    v = _Vault(tmp_path)
    rm.record_ask_avoidable(v, ask_id="a",
                            snapshot=rm.requirement_snapshot(_bound_via_real_binder(v)),
                            answer="a value that digests differently", picked=bogus)
    assert _lines(tmp_path)[0]["resolution"] == "resolvable_overridden", bogus


def test_the_pick_marker_is_stripped_before_this_call_site_and_is_restored(monkeypatch):
    """WHERE THE STRIP HAPPENS, pinned rather than assumed.

    `param_answers_from_choice` drops PICK_MARKER_KEY unconditionally (correctly — it
    must never become a tool argument), so `resolve_structured_input`'s coerced content
    cannot carry it. The rail therefore surfaces it as a SIBLING of `content`."""
    from systemu.runtime import elicitation as el

    schema = {"type": "object", "properties": {"output_path": {"type": "string"}}}
    coerced = el.param_answers_from_choice(
        schema, {"output_path": "out/r.md", el.PICK_MARKER_KEY: ["output_path"]})
    assert el.PICK_MARKER_KEY not in coerced, (
        "the strip site moved — this pin no longer describes the code")

    # `notifications` is imported INSIDE resolve_structured_input, so the patch has to
    # land on the source module rather than on an attribute of `el`.
    from systemu.interface import notifications
    monkeypatch.setattr(notifications, "request_choice",
                        lambda *a, **k: {"output_path": "out/r.md",
                                         el.PICK_MARKER_KEY: ["output_path"]})
    env = el.resolve_structured_input(message="m", requested_schema=schema)
    assert env["action"] == "accept"
    assert env["picked"] == ["output_path"], "the rail must restore the marker"
    assert el.PICK_MARKER_KEY not in env["content"], (
        "the marker must never be re-introduced INTO the tool parameters")


def test_the_restored_marker_can_only_name_a_field_that_came_back(monkeypatch):
    """Intersected with the coerced keys, so a forged marker cannot name a field the
    operator never answered."""
    from systemu.runtime import elicitation as el
    schema = {"type": "object", "properties": {"output_path": {"type": "string"}}}
    # `notifications` is imported INSIDE resolve_structured_input, so the patch has to
    # land on the source module rather than on an attribute of `el`.
    from systemu.interface import notifications
    monkeypatch.setattr(notifications, "request_choice",
                        lambda *a, **k: {"output_path": "out/r.md",
                                         el.PICK_MARKER_KEY: ["not_a_field", 7, None]})
    env = el.resolve_structured_input(message="m", requested_schema=schema)
    assert "picked" not in env


def test_the_elicitation_rail_threads_the_pick_into_the_record(tmp_path, monkeypatch):
    """END-TO-END on the pre-loop rail: an explicit pick reaches the corpus."""
    from systemu.runtime import elicitation as el
    v = _Vault(tmp_path)
    monkeypatch.setattr(el, "resolve_structured_input",
                        lambda **kw: {"action": "accept",
                                      "content": {"output_path": "typed-something-else"},
                                      "picked": ["output_path"]})
    el.surface_ask_bundle_requirement(
        _bound(v, "out/r.md", ref="file:out/r.md", confidence=0.44),
        vault=v, config=None)
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed"
    assert rec["match_basis"] == "picked"


def test_the_grant_reconciler_threads_the_pick_per_schema_path(tmp_path):
    """The reconciler holds the marker as a list of FIELD NAMES and must resolve it
    per path — a pick on one slot must not confirm a different slot."""
    from systemu.scheduler.jobs import record_bundled_ask_outcomes
    v = _Vault(tmp_path)
    snap_a = rm.requirement_snapshot(_bound(v, "out/a.md", ref="file:out/a.md",
                                            schema_path="a", confidence=0.5))
    snap_b = rm.requirement_snapshot(_bound(v, "out/b.md", ref="file:out/b.md",
                                            schema_path="b", confidence=0.5))
    dctx = {"request_id": "r1", "spec": {"requirement_snapshot": [snap_a, snap_b]}}
    record_bundled_ask_outcomes(v, dctx,
                                {"a": "totally different", "b": "totally different"},
                                picked=["a"])
    rows = {r["schema_path"]: r for r in _lines(tmp_path)}
    assert rows["a"]["resolution"] == "resolvable_confirmed"
    assert rows["a"]["match_basis"] == "picked"
    assert rows["b"]["resolution"] == "resolvable_overridden", (
        "a pick on slot 'a' must not confirm slot 'b'")


# ── containment: this fix must not reach the SECURITY decision ───────────────

def test_a_canonical_ref_can_never_pass_the_promoters_candidate_guard(tmp_path):
    """THE CONTAINMENT PIN. `ask_promotion` compares `value_ref` digests to decide a
    promoted fact's taint ORIGIN — a security decision this observability fix must not
    move. The canonical ref is a DIFFERENT shape (and a different length), so the
    promoter's guard 3 rejects it on sight and cannot be fed one even by a hand-built
    snapshot."""
    v = _Vault(tmp_path)
    canon = rm.canonical_value_ref("out/r.md", v)
    exact = rm.value_ref("out/r.md", v)
    assert canon and exact and canon != exact
    assert not rm._is_value_ref(canon), (
        "a canonical digest passed the promoter's value-ref guard — the observability "
        "fold can now reach the taint decision")
    assert not rm._is_canonical_ref(exact)
    assert len(canon) != len(exact), "the two shapes must not even be confusable"


def test_the_exact_digest_rule_is_unchanged(tmp_path):
    """`normalize_value` / `value_ref` are shared with the promoter and with every
    already-stamped on-disk digest. Widening them would move the security decision AND
    make every in-flight candidate incomparable, so this fix must leave them alone."""
    v = _Vault(tmp_path)
    assert rm.value_ref('"out/r.md"', v) != rm.value_ref("out/r.md", v), (
        "value_ref has been widened — the canonical fold belongs in its OWN digest")
    assert rm.value_ref("out/r.md.", v) != rm.value_ref("out/r.md", v)
    assert rm.normalize_value("Acme Corp") == "Acme Corp", (
        "normalize_value must not casefold — ask_promotion compares with it")


# ── the no-secrets invariant, re-verified against the new surface ────────────

def test_the_canonical_layer_records_nothing_for_a_credential_ask(tmp_path):
    """The highest-severity pin, re-run against the F2 surface: the canonical form is
    a LOWER-entropy transform of the answer, so if it ever escaped the secret fences it
    would be a worse leak than the raw digest. A credential ask still records NOTHING."""
    v = _Vault(tmp_path)
    _record(v, _req(kind="credential", schema_path="auth/api_key"), SECRET)
    assert _lines(tmp_path) == []
    _record(v, _req(kind="input", schema_path="service/client_secret"), SECRET)
    assert _lines(tmp_path) == []
    raw = _raw(tmp_path)
    assert SECRET not in raw
    assert rm.canonical_compare_form(SECRET) not in raw


def test_a_secret_shaped_answer_to_an_ordinary_ask_leaks_neither_form(tmp_path):
    """An ordinary ask DOES record (correctly), but neither the value nor its
    canonical form may appear — only keyed, non-reversible digests."""
    v = _Vault(tmp_path)
    _record(v, _bound_via_real_binder(v), SECRET)
    raw = _raw(tmp_path)
    assert raw.strip(), "a non-secret ask must still record"
    assert SECRET not in raw
    assert rm.canonical_compare_form(SECRET) not in raw


def test_the_binder_never_stamps_a_canonical_digest_for_a_secret(tmp_path):
    """BOTH digests pass through the SAME credential/secret refusals — pinned, so a
    future guard cannot protect one and silently miss the other."""
    from systemu.runtime import requirement_binder as rb

    class _BC:
        def __init__(self, vault):
            self.vault = vault

    bc = _BC(_Vault(tmp_path))
    for kind, path in (("credential", "auth/api_key"),
                       ("input", "service/client_secret"),
                       ("input", "login/password")):
        assert rb._value_digest(bc, kind=kind, schema_path=path, value=SECRET,
                                canonical=True) is None, (kind, path)
        assert rb._value_digest(bc, kind=kind, schema_path=path,
                                value=SECRET) is None, (kind, path)
    # not vacuous — an ordinary leaf DOES stamp both
    assert rb._value_digest(bc, kind="input", schema_path="report/output_path",
                            value="out/r.md", canonical=True)


def test_a_hand_built_snapshot_cannot_smuggle_a_raw_value_through_the_canonical_field(
        tmp_path):
    """Guard 3's twin. `candidate_canon_ref` rides the same card spec across the same
    suspend, so it gets the same shape check — a raw value in that field is DROPPED,
    degrading the row to candidate-only rather than writing plaintext."""
    v = _Vault(tmp_path)
    snap = rm.requirement_snapshot(_bound_via_real_binder(v))
    snap["candidate_canon_ref"] = "out/prior_report.md"        # a raw value
    rm.record_ask_avoidable(v, ask_id="a", snapshot=snap, answer="out/prior_report.md.")
    raw = _raw(tmp_path)
    assert "out/prior_report.md" not in raw, "a raw value reached a plaintext audit file"
    rec = _lines(tmp_path)[0]
    assert all("canon_ref" not in c or rm._is_canonical_ref(c["canon_ref"])
               for c in rec["candidates"])


def test_a_canonical_digest_under_a_different_vault_key_is_dropped_not_miscompared(
        tmp_path):
    """A key rotation makes every in-flight candidate incomparable at once. That must
    degrade the row, never be reported as "the operator overrode the binder"."""
    v = _Vault(tmp_path)
    other = _Vault(Path(tmp_path) / "other")
    snap = rm.requirement_snapshot(_bound_via_real_binder(v))
    snap["candidate_canon_ref"] = rm.canonical_value_ref("out/prior_report.md", other)
    assert rm._is_canonical_ref(snap["candidate_canon_ref"])
    rm.record_ask_avoidable(v, ask_id="a", snapshot=snap, answer='"out/prior_report.md"')
    rec = _lines(tmp_path)[0]
    assert all("canon_ref" not in c for c in rec["candidates"]), (
        "a foreign-keyed canonical digest was kept and compared")


# ── the S4 unblock ───────────────────────────────────────────────────────────

def test_a_reshaped_confirm_now_reaches_the_S4_threshold_counters(tmp_path):
    """`_threshold_sensitive_counts` only counts `resolvable_confirmed` rows, so every
    form-only confirm mis-scored as an override was invisible to S4's trigger evidence.
    A confidence-gated confirm answered in a reshaped form must now count."""
    v = _Vault(tmp_path)
    req = _bound(v, "out/r.md", ref="file:out/r.md", confidence=0.5,
                 value_origin="operator")
    _record(v, req, '"out/r.md".')
    rec = _lines(tmp_path)[0]
    assert rec["resolution"] == "resolvable_confirmed"
    counts = rm.answer_linked_ask_report(v)["threshold_sensitive"]
    assert counts["eligible_total"] == 1, (
        "a reshaped confirm is still dark to S4 — its trigger evidence stays "
        "suppressed by the comparison bug")


# ── the stale-digest invariant extends to the twin ───────────────────────────

def test_the_runtime_fold_clears_the_canonical_digest_with_the_handle(tmp_path):
    """The re-ask flip clears `bound_value_ref` + `bound_value_digest` because a stale
    digest would be compared against a LATER answer. The canonical twin is the same
    value under a deliberately easier-to-match rule, so leaving it behind is strictly
    more dangerous."""
    from systemu.runtime import runtime_fold

    v = _Vault(tmp_path)
    req = _bound(v, "out/r.md", ref="file:out/r.md", schema_path="report/output_path",
                 source="runtime_error", state="have")
    req = req.model_copy(update={
        "bound_value_canon_digest": rm.canonical_value_ref("out/r.md", v)})
    assert req.bound_value_canon_digest

    class _O:
        id = 1

        def __init__(self):
            self.requirements = [req]

        def model_copy(self, update):
            o = _O()
            o.requirements = update["requirements"]
            return o

    out = runtime_fold._reask_satisfied_precede(
        [_O()], precede_id=1, kind="input", schema_path="report/output_path")
    flipped = out[0].requirements[0]
    assert flipped.bound_value_digest is None
    assert flipped.bound_value_canon_digest is None, (
        "a stale canonical digest survived the re-ask flip and can confirm a later, "
        "unrelated answer")
