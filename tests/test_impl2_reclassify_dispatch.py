"""IMPL-2 Step 2 — the resume dispatcher's reclassify branch.

When the operator resolves a DENY card with "Reclassify effect…", the dispatcher must
do exactly two things and no more: RECORD the single-use effect-class assignment, and
resume the parked run so the gate re-arbitrates.

The negative is the load-bearing half. A reclassify must NEVER mint an approval bridge
of any kind:

  * a STANDING allow would cover the params-independent signature forever;
  * a SINGLE-USE bridge would let the re-run bypass the gate silently — the operator
    would have assigned a class and, without ever seeing what that class scores to,
    have run the call. The whole point of IMPL-2 is that the remedy produces an HONEST
    approval card on the new classification, not a shortcut past one.
"""
from __future__ import annotations

from types import SimpleNamespace

from systemu.core.models import ActivityStatus
from systemu.runtime.command_approvals import tool_signature

SIG = tool_signature("consolidate_records", "bodyhash123", [], host_class="")

# The gate stamps the CALL's args fingerprint at park time; the record is scoped to
# it so an assignment made for one call cannot be spent by a different one on the
# same (params-independent) signature.
FP = "f" * 40


def _peek(store, sig=SIG, fp=FP):
    return store.peek_reclassified(sig, args_fingerprint=fp)


class _Dec:
    def __init__(self, ctx, choice):
        self.id, self.context, self.choice = "dec_reclass_x", ctx, choice


class _Snap:
    activity_id, shadow_id = "act_1", "shadow_1"


class _Vault:
    def __init__(self):
        self.activity_status = None

    def save_decision(self, d):
        pass

    def get_activity(self, aid):
        return SimpleNamespace(status=ActivityStatus.PARTIAL)

    def save_activity(self, a):
        self.activity_status = a.status


class _Sup:
    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append((activity_id, shadow_id, kw.get("resume_from_execution_id")))


def _bind_store(monkeypatch, tmp_path, *, with_snapshot=True):
    """Point the resume path's default store at a REAL store in tmp (no mocks)."""
    from systemu.runtime import execution_snapshot as es
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    if with_snapshot:
        monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: _Snap())
    else:
        monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: None)
    store = ca.CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    return store


def _reclass_dec(choice="Reclassify effect…", *, assigned_class="local_write",
                 sig=SIG, verdict="deny", **extra):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_signature": sig,
           "tool_name": "consolidate_records", "verdict": verdict,
           "execution_id": "exec_A", "chat_submission_id": "sub_1",
           # what the Inbox panel stamps: the typed gesture + the call the operator
           # was actually looking at.
           "typed_confirmed": True, "args_fingerprint": FP}
    if assigned_class is not None:
        ctx["assigned_class"] = assigned_class
    ctx.update(extra)
    return _Dec(ctx, choice)


# ── the reclassify branch ────────────────────────────────────────────────────

def test_reclassify_records_the_class_and_resumes(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_reclass_dec(), vault=v, supervisor=sup,
                              data_dir=str(tmp_path))
    assert ok is True
    assert _peek(store) == "local_write"
    assert sup.submits == [("act_1", "shadow_1", "exec_A")]   # the run re-arbitrates
    assert v.activity_status is None                          # NOT failed


def test_reclassify_never_mints_an_approval_bridge(monkeypatch, tmp_path):
    """THE PIN. Neither a standing allow nor a single-use bridge — the re-run must
    reach the gate and post an honest card on the new classification."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    rod._dispatch_resume(_reclass_dec(), vault=_Vault(), supervisor=_Sup(),
                         data_dir=str(tmp_path))
    assert store.is_approved(SIG) is False
    assert store.consume_resume_approved(SIG) is False


def test_reclassify_does_not_call_the_approval_recorder_at_all(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    _bind_store(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(rod, "_record_gate_approval",
                        lambda *a, **k: calls.append(k.get("choice")))
    rod._dispatch_resume(_reclass_dec(), vault=_Vault(), supervisor=_Sup(),
                         data_dir=str(tmp_path))
    assert calls == []


def test_reclassify_label_is_matched_case_and_suffix_insensitively(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    for label in ("Reclassify effect…", "reclassify effect", "Reclassify"):
        store = _bind_store(monkeypatch, tmp_path)
        rod._dispatch_resume(_reclass_dec(label), vault=_Vault(), supervisor=_Sup(),
                             data_dir=str(tmp_path))
        assert _peek(store) == "local_write", label
        assert store.consume_resume_approved(SIG) is False, label


def test_a_garbage_assigned_class_records_nothing_but_still_dispatches(monkeypatch, tmp_path):
    """An unrecognised class classifies nothing. Record nothing — the re-run then
    re-DENYs and posts a fresh card, which is the fail-closed outcome. But the
    decision is still stamped dispatched so the reconciler stops re-logging it every
    poll (the v0.10.21 stuck-run lesson)."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_reclass_dec(assigned_class="made_up_class"),
                              vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is True
    assert _peek(store) is None
    assert store.is_approved(SIG) is False
    assert store.consume_resume_approved(SIG) is False


def test_a_missing_assigned_class_records_nothing(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    ok = rod._dispatch_resume(_reclass_dec(assigned_class=None),
                              vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path))
    assert ok is True
    assert _peek(store) is None


def test_an_unknown_assigned_class_records_nothing(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    rod._dispatch_resume(_reclass_dec(assigned_class="unknown"),
                         vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path))
    assert _peek(store) is None


def test_a_reclassify_on_a_COMMAND_gate_records_nothing(monkeypatch, tmp_path):
    """Reclassification is a TOOL-gate remedy (the effect vocabulary is the tool's).
    A command gate carrying the label must not be routed into it — and, since it is
    not a reclassify for this gate type, it takes the ordinary non-deny path."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    dec = _reclass_dec()
    dec.context["gate_type"] = "command"
    dec.context["command"] = "rm -rf /tmp/x"
    rod._dispatch_resume(dec, vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path))
    assert _peek(store) is None


# ── the deny branch clears a dangling record ─────────────────────────────────

def test_denying_the_follow_up_card_clears_the_reclassification(monkeypatch, tmp_path):
    """Otherwise the assignment sits in the store and the NEXT call to this tool —
    which the operator never saw — would be re-arbitrated on it."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    store.mark_reclassified(SIG, "local_write", args_fingerprint=FP)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(
        _reclass_dec("Deny", verdict="require_approval", reclassified=True),
        vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is True
    assert _peek(store) is None
    assert sup.submits == []
    assert v.activity_status == ActivityStatus.FAILED


def test_denying_a_plain_tool_gate_still_works(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    _bind_store(monkeypatch, tmp_path)
    v, sup = _Vault(), _Sup()
    ok = rod._dispatch_resume(_reclass_dec("Deny", assigned_class=None),
                              vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is True and sup.submits == []
    assert v.activity_status == ActivityStatus.FAILED


# ── the coords-less rescue records NOTHING for a reclassify ──────────────────

def test_a_coordsless_reclassify_records_nothing(monkeypatch, tmp_path):
    """With no run to resume, a recorded reclassification would be a dangling,
    params-independent one-shot that a LATER unrelated call to the same tool could
    spend. Mirrors the existing "Approve once" / DENY-band rule in that rescue.

    HONEST NOTE on what this pins. The outcome is OVER-DETERMINED: the rescue records
    only a STANDING allow on a non-DENY gate, and a reclassify is neither "always
    allow" nor non-DENY (it only ever arrives from a DENY card). So this test passes
    even with the explicit reclassify exclusion in that branch removed — it was
    checked by mutation and it does NOT pin that term. It pins the OUTCOME, which is
    the thing that must never regress. The named exclusion in the source is
    unreachable-by-construction documentation guarding a future loosening of the
    standing-allow rule; no test can drive it, and it is labelled as such there rather
    than given a test that would pass for the wrong reason.
    """
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path, with_snapshot=False)
    v, sup = _Vault(), _Sup()
    dec = _reclass_dec()                      # no activity_id/shadow_id, no snapshot
    ok = rod._dispatch_resume(dec, vault=v, supervisor=sup, data_dir=str(tmp_path))
    assert ok is True                          # stamped dispatched (stops re-logging)
    assert sup.submits == []                   # nothing to resume
    assert _peek(store) is None
    assert store.is_approved(SIG) is False
    assert store.consume_resume_approved(SIG) is False


def test_a_coordsless_always_allow_still_records_the_standing_allow(monkeypatch, tmp_path):
    # the surrounding rescue behaviour must be untouched by the reclassify carve-out
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path, with_snapshot=False)
    rod._dispatch_resume(
        _reclass_dec("Always allow", assigned_class=None, verdict="require_approval"),
        vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path))
    assert store.is_approved(SIG) is True


# ── _record_gate_approval: a reclassified card can never mint a standing allow ─

class _RecStore:
    def __init__(self):
        self.standing, self.single_use = [], []

    def approve(self, sig):
        self.standing.append(sig)

    def mark_resume_approved(self, sig, *, for_reclassification=None):
        self.single_use.append((sig, for_reclassification))


def _record(monkeypatch, dctx, choice):
    from systemu.runtime import resume_on_decision as rod
    import systemu.runtime.command_approvals as _ca
    store = _RecStore()
    monkeypatch.setattr(_ca, "init_default_store", lambda _p: store, raising=False)
    rod._record_gate_approval(dctx, is_tool_gate=True, choice=choice)
    return store


# ── THE ROOT CAUSE: a DENY-band gate records NOTHING ─────────────────────────

def test_a_deny_band_gate_records_no_approval_of_any_kind(monkeypatch):
    """THE REGRESSION PIN (adversarial review, CRITICAL).

    "Approve once" on a DENY card is a no-op AT THE GATE — every bypass in
    ``_maybe_gate_tool`` sits under ``if verdict != Verdict.DENY``. But the recorder
    was not band-aware: it fell to its else-branch and minted ``resume_pending[sig]``
    anyway. Harmless while DENY skipped every bypass; the moment IMPL-2 lifts those
    same params to REQUIRE_APPROVAL that bridge becomes redeemable — and the bridge is
    the one bypass deliberately left live under a pending reclassification.

    The coords-less rescue above already carries this rule ("A DENY-band gate records
    NOTHING here"). This is the same rule on the ordinary path.
    """
    for choice in ("approve once", "always allow", "approve"):
        store = _record(monkeypatch, {"tool_signature": "sig-1", "verdict": "deny"},
                        choice)
        assert store.standing == [], choice
        assert store.single_use == [], choice


def test_a_deny_verdict_stamped_as_the_enum_member_also_records_nothing(monkeypatch):
    """``Verdict`` is a str-Enum: ``str(member)`` is "Verdict.DENY", which would not
    match a lowercase compare. The recorder normalises via ``.value`` — pinned, because
    a caller stamping the member rather than its value would otherwise re-open this."""
    from systemu.runtime.action_governance import Verdict
    store = _record(monkeypatch, {"tool_signature": "sig-1", "verdict": Verdict.DENY},
                    "approve once")
    assert store.standing == [] and store.single_use == []


def test_a_missing_verdict_still_records_a_single_use_bridge(monkeypatch):
    """The DENY rule must not swallow the legacy-card case. An ABSENT verdict already
    fails closed to single-use (never standing); that behaviour is unchanged."""
    store = _record(monkeypatch, {"tool_signature": "sig-1"}, "always allow")
    assert store.standing == []
    assert store.single_use == [("sig-1", None)]


# ── FIX 3: the bridge carries the classification of the card that minted it ──

def test_an_approve_once_on_a_reclassified_card_scopes_the_bridge(monkeypatch):
    store = _record(monkeypatch, {"tool_signature": "sig-1",
                                  "verdict": "require_approval",
                                  "reclassified": True,
                                  "assigned_class": "local_write"}, "approve once")
    assert store.single_use == [("sig-1", "local_write")]


def test_an_ordinary_approve_once_mints_an_unscoped_bridge(monkeypatch):
    store = _record(monkeypatch, {"tool_signature": "sig-1",
                                  "verdict": "require_approval"}, "approve once")
    assert store.single_use == [("sig-1", None)]


def test_a_reclassified_card_with_no_assigned_class_records_nothing(monkeypatch):
    """There is no coherent scope to mint the bridge under, and an UNSCOPED bridge on
    a reclassified card is exactly the thing fix 3 refuses to honour. Record nothing
    rather than litter the store with a one-shot that can never be spent."""
    for bad in ({}, {"assigned_class": ""}, {"assigned_class": "   "}):
        store = _record(monkeypatch, {"tool_signature": "sig-1",
                                      "verdict": "require_approval",
                                      "reclassified": True, **bad}, "approve once")
        assert store.single_use == [] and store.standing == []


# ── FIX 2: recording an assignment clears any bridge standing on the signature ─

def test_recording_a_reclassification_clears_a_pending_bridge(monkeypatch, tmp_path):
    """Defence in depth behind the DENY-band rule: an unconsumed bridge from ANY
    source (an "Approve once" whose run crashed before re-entry, say) must not be
    sitting there when the verdict lifts."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    store.mark_resume_approved(SIG)
    assert store.consume_resume_approved(SIG) is True     # fixture precondition
    store.mark_resume_approved(SIG)

    rod._dispatch_resume(_reclass_dec(), vault=_Vault(), supervisor=_Sup(),
                         data_dir=str(tmp_path))
    assert _peek(store) == "local_write"
    assert store.consume_resume_approved(SIG) is False, (
        "the bridge must be gone before the verdict lifts")


# ── MED: the typed confirmation is enforced where the record is made ─────────

def test_a_reclassify_without_typed_confirmation_records_nothing(monkeypatch, tmp_path):
    """``resolve_with_context_patch`` is public and has no notion of caller — the UI
    is not a control. Nothing outside ``inbox_page`` had ever READ ``typed_confirmed``,
    so the gesture the whole remedy is predicated on was unenforced."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    dec = _reclass_dec()
    dec.context.pop("typed_confirmed")
    ok = rod._dispatch_resume(dec, vault=_Vault(), supervisor=_Sup(),
                              data_dir=str(tmp_path))
    assert ok is True                       # still stamped dispatched
    assert _peek(store) is None


def test_a_reclassify_with_a_falsy_typed_confirm_records_nothing(monkeypatch, tmp_path):
    from systemu.runtime import resume_on_decision as rod
    for falsy in (False, "", 0, None):
        store = _bind_store(monkeypatch, tmp_path)
        rod._dispatch_resume(_reclass_dec(typed_confirmed=falsy),
                             vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path))
        assert _peek(store) is None, falsy


def test_a_reclassify_with_no_args_fingerprint_records_nothing(monkeypatch, tmp_path):
    """HONEST NOTE: over-determined, same as its gate-file twin. ``mark_reclassified``
    refuses an unfingerprinted write on its own, so this passes with the dispatcher's
    guard removed (mutation-checked). It pins the OUTCOME; the store-layer guard is
    pinned by ``test_marking_without_a_fingerprint_records_nothing``."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    dec = _reclass_dec()
    dec.context.pop("args_fingerprint")
    rod._dispatch_resume(dec, vault=_Vault(), supervisor=_Sup(), data_dir=str(tmp_path))
    assert _peek(store) is None
    assert store.peek_reclassified(SIG, args_fingerprint="") is None


def test_always_allow_on_a_reclassified_card_degrades_to_single_use(monkeypatch):
    """Defence in depth: the follow-up card does not OFFER "Always allow", but a
    one-shot operator classification must not be able to mint a standing allow even
    if some other surface ever supplied the choice.

    FIXTURE NOTE: ``assigned_class`` is now part of this context because production
    always stamps it alongside ``reclassified`` (``tool_sandbox`` sets both from the
    same ``pending_class``). Omitting it made this a card shape the gate cannot
    produce; the missing-class case has its own test below.
    """
    store = _record(monkeypatch, {"tool_signature": "sig-1",
                                  "verdict": "require_approval",
                                  "reclassified": True,
                                  "assigned_class": "local_write"}, "always allow")
    assert store.standing == []
    assert store.single_use == [("sig-1", "local_write")]


def test_always_allow_without_the_reclassified_marker_is_still_standing(monkeypatch):
    store = _record(monkeypatch, {"tool_signature": "sig-1",
                                  "verdict": "require_approval"}, "always allow")
    assert store.standing == ["sig-1"] and store.single_use == []


# ── MED: the Inbox must not claim success when nothing was recorded ──────────
#
# ``_dispatch_resume`` returns False for a decision with no ``chat_submission_id``:
# the reclassify branch is never reached, no store record is written, and re-running
# does not help because there is no record to apply. The Inbox panel nonetheless
# notified "Reclassified as <class>. …The task will re-check this call…" in green.
# The single-lane limitation is pre-existing; the affirmative claim about it was not.

def test_the_dispatcher_records_nothing_without_a_chat_submission(monkeypatch, tmp_path):
    """GROUND TRUTH for the predicate below: this is what actually happens today."""
    from systemu.runtime import resume_on_decision as rod
    store = _bind_store(monkeypatch, tmp_path)
    dec = _reclass_dec()
    dec.context.pop("chat_submission_id")
    assert rod._dispatch_resume(dec, vault=_Vault(), supervisor=_Sup(),
                                data_dir=str(tmp_path)) is False
    assert _peek(store) is None, "nothing recorded — the operator's click did nothing"


def test_the_predicate_mirrors_the_dispatchers_early_returns():
    """The UI asks this before claiming success. It must track the dispatcher's own
    ladder, so the two cannot drift into disagreeing about what will happen."""
    from systemu.runtime.resume_on_decision import reclassification_can_be_recorded
    full = {"kind": "gate", "gate_type": "tool", "chat_submission_id": "sub_1",
            "execution_id": "exec_A"}
    assert reclassification_can_be_recorded(full) is True

    # each early return, one at a time
    assert reclassification_can_be_recorded({**full, "chat_submission_id": ""}) is False
    assert reclassification_can_be_recorded({**full, "execution_id": None}) is False
    assert reclassification_can_be_recorded({**full, "gate_type": "command"}) is False
    assert reclassification_can_be_recorded({**full, "kind": "structured_question"}) is False
    assert reclassification_can_be_recorded({**full, "resume_dispatched": True}) is False
    assert reclassification_can_be_recorded({}) is False
    assert reclassification_can_be_recorded(None) is False


def test_the_inbox_notice_does_not_claim_success_when_nothing_will_be_recorded():
    from systemu.interface.pages.inbox_page import _reclassify_outcome_notice
    msg, kind = _reclassify_outcome_notice(
        {"kind": "gate", "gate_type": "tool"}, "local_write")
    assert kind != "positive", "a no-op must not be reported as a success"
    low = msg.lower()
    assert "local_write" in msg
    assert "nothing" in low and "record" in low, msg
    # it must not repeat the promise the code cannot keep
    assert "will re-check" not in low, msg


def test_the_inbox_notice_still_reports_the_working_path_positively():
    from systemu.interface.pages.inbox_page import _reclassify_outcome_notice
    msg, kind = _reclassify_outcome_notice(
        {"kind": "gate", "gate_type": "tool", "chat_submission_id": "sub_1",
         "execution_id": "exec_A"}, "local_write")
    assert kind == "positive"
    assert "local_write" in msg
    assert "nothing has run" in msg.lower()


def test_approve_once_on_a_reclassified_card_is_a_single_use_bridge(monkeypatch):
    # this bridge is the follow-up card's own "Approve once" — it MUST be recorded,
    # it is the one thing that lets the re-run actually execute the call. It is minted
    # SCOPED to the classification of the card that offered it.
    store = _record(monkeypatch, {"tool_signature": "sig-1",
                                  "verdict": "require_approval",
                                  "reclassified": True,
                                  "assigned_class": "local_write"}, "approve once")
    assert store.single_use == [("sig-1", "local_write")] and store.standing == []
