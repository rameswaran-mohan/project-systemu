"""IMPL-2 Step 1 — the SINGLE-USE reclassification record in CommandApprovalStore.

A DENY is the gate's refusal band. IMPL-2 makes it operator-REMEDIABLE: the operator
assigns the real effect class under typed confirmation, and the gate re-arbitrates on
it. This store slice is where that assignment lives between the operator's click and
the re-run that consumes it.

Two properties are load-bearing and pinned here:

  * SINGLE-USE. A reclassification is spent by the first consume, so it re-arbitrates
    exactly ONE call. The tool signature is params-INDEPENDENT while the DENY verdict
    is params-DEPENDENT — a standing reclassification would therefore silently cover
    every future destructive call to the same tool body.
  * A GARBAGE CLASS RECORDS NOTHING. ``coerce`` maps an unrecognised value to
    ``unknown``, and ``unknown`` is exactly the conjunct the DENY band keys on — so
    storing it would either be inert or, worse, read back as a "classification" that
    strips UNKNOWN and puts nothing in its place.
"""
from __future__ import annotations

import json

from systemu.runtime.command_approvals import CommandApprovalStore


def _store(tmp_path):
    return CommandApprovalStore(tmp_path / "command_approvals.json")


# A stand-in args fingerprint. Production always supplies one (the gate hashes the
# call's parameters), because the record is keyed on the params-INDEPENDENT tool
# signature while the DENY verdict is params-DEPENDENT — without it, a record made
# for one call applies to every call on that signature.
FP = "a" * 40
FP_OTHER = "b" * 40


def _mark(s, sig, cls, fp=FP):
    return s.mark_reclassified(sig, cls, args_fingerprint=fp)


def _peek(s, sig, fp=FP):
    return s.peek_reclassified(sig, args_fingerprint=fp)


def _consume(s, sig, fp=FP):
    return s.consume_reclassified(sig, args_fingerprint=fp)


def _now():
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _entry(cls, *, age=0, fp=FP):
    """A hand-written store row, as the operator-inspectable file would hold it."""
    from datetime import datetime, timedelta, timezone
    ts = datetime.now(tz=timezone.utc) - timedelta(seconds=age)
    return {"effect_class": cls, "recorded_at": ts.isoformat(timespec="seconds"),
            "args_fingerprint": fp}


# ── round-trip ───────────────────────────────────────────────────────────────

def test_mark_then_peek_round_trips(tmp_path):
    s = _store(tmp_path)
    assert _peek(s, "sig-1") is None
    assert _mark(s, "sig-1", "local_write") is True
    assert _peek(s, "sig-1") == "local_write"
    # peek is NON-consuming — the gate peeks on every call while the record stands
    assert _peek(s, "sig-1") == "local_write"


def test_record_persists_with_a_timestamp(tmp_path):
    s = _store(tmp_path)
    _mark(s, "sig-1", "net_mutate")
    raw = json.loads((tmp_path / "command_approvals.json").read_text(encoding="utf-8"))
    entry = raw["reclassified"]["sig-1"]
    assert entry["effect_class"] == "net_mutate"
    assert entry["recorded_at"]          # an operator-auditable when


def test_a_second_store_over_the_same_file_sees_the_record(tmp_path):
    # The dashboard/CLI write out-of-process from the daemon that reads (the same
    # freshness contract is_approved keeps).
    _mark(_store(tmp_path), "sig-1", "local_delete")
    assert _peek(_store(tmp_path), "sig-1") == "local_delete"


def test_class_is_normalised_through_coerce(tmp_path):
    s = _store(tmp_path)
    assert _mark(s, "sig-1", "  LOCAL_WRITE  ") is True
    assert _peek(s, "sig-1") == "local_write"


def test_marking_again_replaces_the_class(tmp_path):
    s = _store(tmp_path)
    _mark(s, "sig-1", "local_write")
    _mark(s, "sig-1", "net_mutate")
    assert _peek(s, "sig-1") == "net_mutate"


# ── single-use consume ───────────────────────────────────────────────────────

def test_consume_returns_the_class_exactly_once(tmp_path):
    s = _store(tmp_path)
    _mark(s, "sig-1", "local_write")
    assert _consume(s, "sig-1") == "local_write"
    assert _consume(s, "sig-1") is None
    assert _peek(s, "sig-1") is None


def test_consume_on_an_absent_signature_is_none(tmp_path):
    assert _consume(_store(tmp_path), "nope") is None


def test_consume_only_spends_the_named_signature(tmp_path):
    s = _store(tmp_path)
    _mark(s, "sig-1", "local_write")
    _mark(s, "sig-2", "net_mutate")
    assert _consume(s, "sig-1") == "local_write"
    assert _peek(s, "sig-2") == "net_mutate"


# ── clear ────────────────────────────────────────────────────────────────────

def test_clear_removes_a_pending_record(tmp_path):
    s = _store(tmp_path)
    _mark(s, "sig-1", "local_write")
    assert s.clear_reclassified("sig-1") is True
    assert _peek(s, "sig-1") is None


def test_clear_on_an_absent_signature_is_false(tmp_path):
    assert _store(tmp_path).clear_reclassified("nope") is False


# ── a garbage class records NOTHING ──────────────────────────────────────────

def test_an_unrecognised_class_records_nothing(tmp_path):
    s = _store(tmp_path)
    assert _mark(s, "sig-1", "totally_made_up") is False
    assert _peek(s, "sig-1") is None


def test_the_literal_unknown_class_records_nothing(tmp_path):
    # "unknown" IS the conjunct the DENY band keys on. Recording it would be a
    # reclassification that classifies nothing.
    s = _store(tmp_path)
    assert _mark(s, "sig-1", "unknown") is False
    assert _peek(s, "sig-1") is None


def test_empty_and_none_classes_record_nothing(tmp_path):
    s = _store(tmp_path)
    for bad in ("", "   ", None):
        assert _mark(s, "sig-1", bad) is False
    assert _peek(s, "sig-1") is None


def test_a_refused_write_does_not_disturb_an_existing_record(tmp_path):
    s = _store(tmp_path)
    _mark(s, "sig-1", "local_write")
    assert _mark(s, "sig-1", "garbage") is False
    assert _peek(s, "sig-1") == "local_write"


def test_a_hand_edited_garbage_class_is_refused_on_READ(tmp_path):
    """The store file is documented as operator-inspectable/hand-editable, so the
    write-side validation is not the only door. A class that does not coerce to a
    REAL tag must read back as no reclassification at all — otherwise a hand-edit
    could inject a value that strips UNKNOWN and puts nothing in its place."""
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1, "approved": {}, "pending": {}, "session_trusted": {},
        "reclassified": {
            "sig-1": _entry("unknown"),
            "sig-2": _entry("made_up"),
            "sig-3": _entry("local_write"),
        },
    }), encoding="utf-8")
    s = CommandApprovalStore(path)
    assert _peek(s, "sig-1") is None
    assert _peek(s, "sig-2") is None
    assert _peek(s, "sig-3") == "local_write"
    assert _consume(s, "sig-2") is None


def test_a_malformed_entry_is_refused(tmp_path):
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1, "reclassified": {"sig-1": "not-a-dict"},
    }), encoding="utf-8")
    assert _peek(CommandApprovalStore(path), "sig-1") is None


# ── the record is scoped to the CALL, not just the tool signature ────────────

def test_a_record_does_not_apply_to_a_different_args_fingerprint(tmp_path):
    """THE SUBSTITUTION PIN. The signature is params-INDEPENDENT (name + body hash +
    effect tags + host class); the DENY verdict is params-DEPENDENT. Without this the
    operator's assignment for one call silently re-arbitrates every other destructive
    call to the same tool body."""
    s = _store(tmp_path)
    _mark(s, "sig-1", "local_write", fp=FP)
    assert _peek(s, "sig-1", fp=FP_OTHER) is None
    assert _consume(s, "sig-1", fp=FP_OTHER) is None
    # …and the refusal did not spend the record the operator actually made
    assert _peek(s, "sig-1", fp=FP) == "local_write"


def test_a_record_with_no_fingerprint_is_never_applicable(tmp_path):
    """Fail closed in BOTH directions: an unfingerprinted record (a hand edit, or a
    legacy row) matches nothing, and an unfingerprinted read matches nothing. There is
    no "both absent, therefore equal" case that could be reached from production."""
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1,
        "reclassified": {"sig-1": {"effect_class": "local_write",
                                   "recorded_at": _now()}},
    }), encoding="utf-8")
    s = CommandApprovalStore(path)
    assert _peek(s, "sig-1", fp=FP) is None
    assert s.peek_reclassified("sig-1", args_fingerprint="") is None
    assert s.peek_reclassified("sig-1") is None


def test_marking_without_a_fingerprint_records_nothing(tmp_path):
    """A record that can never be applied is a lie in an operator-inspectable store.
    Refuse the write instead."""
    s = _store(tmp_path)
    assert s.mark_reclassified("sig-1", "local_write", args_fingerprint="") is False
    assert s.mark_reclassified("sig-1", "local_write") is False
    assert _peek(s, "sig-1") is None


# ── TTL: an abandoned assignment must not live forever ───────────────────────

def test_a_record_older_than_the_ttl_reads_as_absent(tmp_path):
    """An abandoned follow-up card used to leave an assignment that lived forever and
    was spent by whatever call reached that signature next."""
    from systemu.runtime.command_approvals import RECLASSIFY_TTL_SECONDS
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1,
        "reclassified": {"sig-1": _entry("local_write",
                                         age=RECLASSIFY_TTL_SECONDS + 60)},
    }), encoding="utf-8")
    s = CommandApprovalStore(path)
    assert _peek(s, "sig-1") is None
    assert _consume(s, "sig-1") is None


def test_a_record_inside_the_ttl_still_applies(tmp_path):
    from systemu.runtime.command_approvals import RECLASSIFY_TTL_SECONDS
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1,
        "reclassified": {"sig-1": _entry("local_write",
                                         age=RECLASSIFY_TTL_SECONDS - 60)},
    }), encoding="utf-8")
    assert _peek(CommandApprovalStore(path), "sig-1") == "local_write"


def test_an_expired_record_is_purged_on_consume(tmp_path):
    from systemu.runtime.command_approvals import RECLASSIFY_TTL_SECONDS
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1,
        "reclassified": {"sig-1": _entry("local_write",
                                         age=RECLASSIFY_TTL_SECONDS * 2)},
    }), encoding="utf-8")
    s = CommandApprovalStore(path)
    assert _consume(s, "sig-1") is None
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "sig-1" not in (raw.get("reclassified") or {})


def test_an_unparseable_timestamp_reads_as_expired(tmp_path):
    """Fail closed: if the store cannot tell how old an assignment is, it does not get
    to act on it."""
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({
        "version": 1,
        "reclassified": {"sig-1": {"effect_class": "local_write",
                                   "recorded_at": "not-a-timestamp",
                                   "args_fingerprint": FP}},
    }), encoding="utf-8")
    assert _peek(CommandApprovalStore(path), "sig-1") is None


def test_the_ttl_is_short(tmp_path):
    from systemu.runtime.command_approvals import RECLASSIFY_TTL_SECONDS
    assert 60 <= RECLASSIFY_TTL_SECONDS <= 60 * 60, (
        "minutes, not hours or days — it is the window in which an abandoned "
        "assignment can be spent by a call the operator never saw")


# ── it must not disturb the neighbouring approval records ────────────────────

def test_reclassification_is_independent_of_approvals(tmp_path):
    s = _store(tmp_path)
    s.approve("sig-1", command="c")
    s.mark_resume_approved("sig-1")
    _mark(s, "sig-1", "local_write")
    assert s.is_approved("sig-1") is True
    assert _peek(s, "sig-1") == "local_write"
    assert _consume(s, "sig-1") == "local_write"
    # spending the reclassification left both approval records untouched
    assert s.is_approved("sig-1") is True
    assert s.consume_resume_approved("sig-1") is True


def test_an_empty_signature_records_nothing(tmp_path):
    s = _store(tmp_path)
    assert _mark(s, "", "local_write") is False
    assert _peek(s, "") is None
