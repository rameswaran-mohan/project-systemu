"""v0.8.22.1 (R5): resume a chat task when its stuck-loop decision is resolved.

Two trigger paths feed the same dispatch:

  (1) EventBus subscriber (fast path) — registered in the daemon via
      :func:`register`. Fires synchronously on the in-process
      ``operator_decision_resolved`` event. Used when the resolution
      happens inside the daemon (dashboard click, etc.).

  (2) Daemon-side reconciler poll (cross-process safety net) — see
      :func:`systemu.scheduler.jobs.reconcile_resolved_stuck_decisions`.
      EventBus is process-local (``systemu/interface/event_bus.py``),
      so the CLI command ``sharing_on decisions resolve`` lives in a
      separate process and its publish never reaches the daemon
      subscriber. The reconciler walks the persisted decisions index
      and re-dispatches any resolved structured_question decision that
      hasn't been dispatched yet.

Both paths funnel into :func:`_dispatch_resume`, which:
  * stashes the operator's answer into the parked run's execution
    snapshot (``__STUCK_ANSWER__::obj_<id>::<choice>`` sticky note,
    consumed by ``shadow_runtime._apply_stuck_answer`` on resume),
  * re-submits the activity with ``resume_from_execution_id`` so the
    runtime applies the answer at resume-start,
  * and stamps ``decision.context["resume_dispatched"] = True`` on
    the persisted decision so we never double-dispatch (across
    restarts, across both paths).

The in-memory ``_handled`` set is the EventBus fast-path dedup; the
persisted flag is the cross-restart / cross-path source of truth.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Idempotency: decisions we've already re-dispatched in THIS process
# (EventBus may replay; in-memory only).  The persisted flag
# decision.context["resume_dispatched"] is the cross-process /
# cross-restart truth — checked in _dispatch_resume.
_handled: set = set()


def _dispatch_resume(decision, *, vault, supervisor,
                     data_dir: Optional[Path] = None) -> bool:
    """Given a resolved stuck `structured_question` decision, stash the
    operator's answer into the parked run's snapshot and re-submit the
    activity with `resume_from_execution_id`.

    Idempotent: skips if ``decision.context["resume_dispatched"]`` is
    already True, AND stamps it True on success so subsequent calls
    (from the poll, from an EventBus replay, from a daemon restart)
    are no-ops.

    Returns True if a dispatch was performed, False if skipped (already
    dispatched, missing coords, wrong kind, etc.).  Best-effort: any
    exception while stashing the snapshot is logged but does not block
    the re-submit.  Failures of the re-submit itself propagate to the
    caller (the EventBus adapter swallows them; the reconciler logs
    and continues).
    """
    dctx = decision.context or {}
    kind = dctx.get("kind")
    # v0.9.52: a parked COMMAND gate (kind="gate", gate_type="command") is now
    # resumable too — previously only structured_question questions resumed, so a
    # chat task that parked on a run_command approval hung forever on resolution.
    # S1b (Task 4): a gate_type="tool" action gate resumes the SAME way (its
    # activity/shadow also derive from the snapshot; its Deny/Approve branches are
    # gate_type-discriminated below so the command path stays byte-for-byte).
    gate_type = dctx.get("gate_type")
    is_cmd_gate = (kind == "gate" and gate_type == "command")
    is_tool_gate = (kind == "gate" and gate_type == "tool")
    is_gate = is_cmd_gate or is_tool_gate
    # R-A13 Stage-3a: an operator-attest card (gate_type="operator") resumes too. Its
    # `kind` is overwritten to "gate" by gate.to_decision_context, so key off the
    # sibling kind_marker (NOT `kind`). It stashes an __OPERATOR_ATTEST__ sticky the
    # resume-start applier peels; activity/shadow derive from the snapshot like a gate.
    is_attest = (dctx.get("kind_marker") == "operator_attest")
    if kind != "structured_question" and not is_gate and not is_attest:
        return False
    if not dctx.get("chat_submission_id"):
        return False
    if dctx.get("resume_dispatched"):
        return False
    execution_id = dctx.get("execution_id")
    if not execution_id:
        logger.info("[ResumeOnDecision] decision %s has no execution_id — skipping", decision.id)
        return False
    choice = (decision.choice or "").strip().lower()

    # v0.10.21: a tool/command gate that parked on the run's FIRST tool call now
    # STAMPS activity_id/shadow_id into the context (chat_submission_ctx carriers), so
    # prefer those. A structured_question carries them directly. Only fall back to the
    # snapshot for an OLDER gate decision (or a mid-run park) that lacks context coords.
    activity_id = dctx.get("activity_id")
    shadow_id = dctx.get("shadow_id")
    snap = None
    snapshot_refused = False
    try:
        from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
        from systemu.runtime.snapshot_migrations import SnapshotRefused
        try:
            snap = read_snapshot(execution_id, data_dir=data_dir)
        except SnapshotRefused as _refused:
            # DEC-9: schema newer than this build supports. Don't proceed as if
            # there were simply no snapshot — log loudly. Any resume this path
            # dispatches re-reads the snapshot in shadow_runtime.execute (the single
            # fresh-vs-resume chokepoint), which refuses it there, so the run fails
            # honestly rather than starting fresh.
            logger.error("[ResumeOnDecision] snapshot refused for %s: %s",
                         execution_id, _refused)
            snap = None
            snapshot_refused = True
    except Exception:
        snap = None
    if snap is not None:
        activity_id = activity_id or getattr(snap, "activity_id", None)
        shadow_id = shadow_id or getattr(snap, "shadow_id", None)
    if not (activity_id and shadow_id):
        # v0.10.21: a GATE with no resume coords (an iteration-1 park whose decision
        # predates the context-coord stamp, or any coords-less gate) can't be RESUMED,
        # but we can still record the operator's approval so a manual re-run succeeds —
        # and stamp resume_dispatched so the reconciler stops re-logging this every
        # poll. A structured_question / attest genuinely needs the snapshot coords, so
        # those keep skipping.
        #
        # DEC-9 GUARD: a REFUSED snapshot (newer schema — not merely absent) means the
        # parked run may have done effectful work this build can't read. Do NOT record
        # the approval or stamp dispatched in that case — fall through to the honest
        # skip so nothing masks the refusal (mirrors the pre-v0.10.21 behaviour).
        if is_gate and not snapshot_refused:
            # Record ONLY a STANDING allow here ("Always allow" on a tool gate) — the
            # choice meant to carry forward, idempotent and keyed to persist across runs.
            # Deliberately DO NOT persist a SINGLE-USE bridge ("Approve once", or any
            # command gate) in the coords-less path: with no run to resume, the one-shot
            # would sit unconsumed and could later be spent by an UNRELATED call to the
            # same (params-independent) tool signature. A manual re-run simply re-asks —
            # the correct "approve once" semantics for a parked instance that no longer
            # exists. (The normal coords-present path re-submits immediately, consuming
            # the bridge within seconds, so it has no such window.)
            # A DENY-band gate records NOTHING here. Routing it into the recorder would
            # fall through to the single-use bridge — creating exactly the dangling,
            # params-independent one-shot this branch refuses to create, and creating it
            # only for the most dangerous band. A DENY simply re-asks on re-run.
            # IMPL-2: a RECLASSIFY records nothing here either, for the same reason —
            # a reclassification is a params-INDEPENDENT single-use record, so with no
            # run to consume it within seconds it would sit in the store until some
            # LATER, unrelated call to this tool signature spent it. That call would be
            # re-arbitrated on a class the operator assigned to a different set of
            # parameters. A manual re-run simply re-asks, which is the correct
            # semantics for a parked instance that no longer exists.
            _v = str(dctx.get("verdict") or "").strip().lower()
            _is_reclass = is_tool_gate and choice.startswith("reclassify")
            _standing = (is_tool_gate and choice == "always allow" and _v != "deny")
            # The reclassify exclusion is UNREACHABLE by construction today, and is kept
            # deliberately: a reclassify is never "always allow", and only ever arrives
            # from a DENY card, so either condition above already refuses it. No test
            # can drive this term (mutation-checked — the coords-less test passes
            # without it), so it is documentation, not a pinned guard: it exists so that
            # loosening the standing-allow rule later cannot silently start minting
            # dangling reclassification records here.
            if _standing and not _is_reclass:
                _record_gate_approval(dctx, is_tool_gate=True, choice=choice)
            _stamp_dispatched(decision, vault)
            _handled.add(decision.id)
            logger.info(
                "[ResumeOnDecision] %s gate resolved (%s) for %s but no resume coords "
                "— %s; task must be re-run (not auto-resumed)",
                gate_type, choice or "deny", decision.id,
                ("recorded standing allow" if (is_tool_gate and choice == "always allow")
                 else "no standing approval recorded"),
            )
            return True
        logger.info(
            "[ResumeOnDecision] decision %s missing resume coords — skipping",
            decision.id,
        )
        return False

    # IMPL-2: "Reclassify effect…" is neither an approval nor a denial. It assigns an
    # effect class the gate re-arbitrates on, so it takes its own branch below — and
    # is EXCLUDED from the ordinary non-deny path, which would mint an approval bridge.
    is_reclassify = is_tool_gate and choice.startswith("reclassify")

    if is_gate:
        if choice in ("deny", ""):
            # Operator denied a REQUIRED command/tool → the task can't proceed; mark
            # it FAILED rather than re-submitting (which would re-ask the same action
            # → loop). Idempotent: only flips a non-terminal activity. gate_type-
            # agnostic — a tool gate finalizes identically to a command gate.
            try:
                from systemu.core.models import ActivityStatus
                act = vault.get_activity(activity_id)
                if getattr(act, "status", None) not in (
                        ActivityStatus.COMPLETED, ActivityStatus.FAILED):
                    act.status = ActivityStatus.FAILED
                    vault.save_activity(act)
            except Exception:
                logger.debug("[ResumeOnDecision] gate-deny finalize failed", exc_info=True)
            # IMPL-2: a DENIED follow-up card must leave NO dangling reclassification.
            # The record is single-use but not self-expiring, so without this the class
            # the operator just refused to approve would still be sitting in the store,
            # ready to re-arbitrate the NEXT call to this signature — one the operator
            # never saw. Unconditional (not just on a reclassified card): a plain deny
            # of a signature that happens to carry a stale record clears it too.
            _clear_reclassification(dctx)
            _stamp_dispatched(decision, vault)
            logger.info("[ResumeOnDecision] %s gate DENIED for activity %s — finalized",
                        gate_type, activity_id)
            return True
        if is_reclassify:
            # Record the single-use class assignment and fall through to the resume
            # submit. Deliberately NO _record_gate_approval: a reclassify must never
            # mint an approval bridge of any kind. A standing allow would cover the
            # params-independent signature forever; a single-use bridge would let the
            # re-run BYPASS the gate, so the operator would have assigned a class and
            # run the call without ever seeing what that class scores to. The remedy's
            # whole point is that the re-run posts an HONEST card on the new
            # classification — the operator approves THAT, or denies it.
            _record_reclassification(dctx)
        else:
            # S1b (Task 4) / v0.9.52: record the operator's approval to the
            # CommandApprovalStore. Tool gate: "Always allow" → STANDING allow-list,
            # anything else non-deny → SINGLE-USE resume bridge. Command gate: always a
            # SINGLE-USE bridge (byte-for-byte the v0.9.52 behaviour). Extracted into
            # _record_gate_approval so the missing-coords rescue above records the SAME
            # approval — the ONLY place a tool-gate "Always allow" becomes a standing
            # entry.
            _record_gate_approval(dctx, is_tool_gate=is_tool_gate, choice=choice)
    elif is_attest:
        # R-A13 Stage-3a: stash the operator's attest CHOICE + the enqueue-time effect
        # classification (a JSON payload the resume-start applier peels) so the applier
        # can run verify(operator_attest) with the KNOWN non-money effect_class — a
        # requires_external objective with no known effect tag is money-move via the
        # fail-closed fallback and could otherwise never credit. Never a money-move at
        # enqueue (the enqueue gate excludes it); the applier + verify re-gate anyway.
        import json as _json
        objective_id = dctx.get("objective_id")
        try:
            if snap is not None:
                _payload = _json.dumps({
                    "choice": decision.choice,
                    "effect_class": dctx.get("effect_class"),
                    "is_money_move": bool(dctx.get("is_money_move")),
                })
                snap.sticky_notes.append(
                    f"__OPERATOR_ATTEST__::obj_{objective_id}::{_payload}"
                )
                write_snapshot(snap, data_dir=data_dir)
        except Exception:
            logger.debug("[ResumeOnDecision] could not stash attest answer in snapshot",
                         exc_info=True)
    else:
        # structured_question: stash the operator's answer into the snapshot so the
        # runtime applies it deterministically on resume.
        objective_id = dctx.get("objective_id")
        try:
            if snap is not None:
                snap.sticky_notes.append(
                    f"__STUCK_ANSWER__::obj_{objective_id}::{decision.choice}"
                )
                write_snapshot(snap, data_dir=data_dir)
        except Exception:
            logger.debug("[ResumeOnDecision] could not stash answer in snapshot", exc_info=True)

    _stamp_dispatched(decision, vault)
    _handled.add(decision.id)
    supervisor.submit(
        activity_id, shadow_id,
        priority=1, reason="chat", origin="chat",
        resume_from_execution_id=execution_id,
        chat_submission_id=dctx.get("chat_submission_id"),
        consult_affinity_log=False,
    )
    logger.info(
        "[ResumeOnDecision] re-dispatched activity %s (resume %s) after decision %s",
        activity_id, execution_id, decision.id,
    )
    return True


def reclassification_can_be_recorded(dctx) -> bool:
    """Will ``_dispatch_resume`` be able to reach ``_record_reclassification`` for a
    decision carrying this context?

    The Inbox panel asks BEFORE claiming the remedy worked. ``_dispatch_resume`` returns
    False for a decision with no ``chat_submission_id`` — the resume machinery is
    single-lane — so outside the chat lane a reclassify records NOTHING, and the panel
    nonetheless notified "Reclassified as <class>. The task will re-check this call…" in
    green. Nothing had been written; re-running did not help, because there was no
    record to apply. The single-lane limitation is pre-existing and out of scope; the
    affirmative claim about it was the defect.

    This mirrors the early-return ladder in ``_dispatch_resume`` and is deliberately
    NARROW: it reports only the STATICALLY decidable refusals. A True answer is "nothing
    known will stop this", not a guarantee — the coords-less rescue can still decline to
    record (it refuses a reclassify by design, since a dangling params-independent
    one-shot could later be spent by an unrelated call), and that depends on a snapshot
    this function cannot see. False, however, is certain: the dispatcher will not record.
    """
    d = dctx or {}
    if d.get("kind") != "gate" or d.get("gate_type") != "tool":
        return False        # reclassification is a TOOL-gate remedy
    if not d.get("chat_submission_id"):
        return False        # THE reported case: the dispatcher returns False here
    if d.get("resume_dispatched"):
        return False        # already handled; this decision will not be processed again
    if not d.get("execution_id"):
        return False        # no run to resume, and the reclassify branch is never reached
    return True


def _reclassify_store():
    """The CommandApprovalStore the reclassification records live in, or None.
    Best-effort; never raises (mirrors _record_gate_approval)."""
    try:
        from pathlib import Path as _P
        from systemu.runtime.command_approvals import init_default_store
        return init_default_store(_P("data"))
    except Exception:
        logger.debug("[ResumeOnDecision] could not open the approval store",
                     exc_info=True)
        return None


def _record_reclassification(dctx) -> Optional[str]:
    """IMPL-2: persist the operator's SINGLE-USE effect-class assignment for a tool
    signature. Returns the recorded class, or None when nothing was recorded.

    Four preconditions, each fail-closed (recording nothing means the resumed run
    re-DENYs and posts a fresh card — never that something runs):

    * a tool signature, READ BACK from the context (stamped by the gate at park
      time), never recomputed here;
    * the TYPED CONFIRMATION gesture. ``inbox_page`` stamps ``typed_confirmed`` when
      the operator transcribes the class they are asserting, but until this check it
      was written and never read: the validation lived only in the UI, while
      ``resolve_with_context_patch`` is a public API with no notion of caller. The
      gesture the whole remedy is predicated on has to be enforced where the record
      is made, not where the button is drawn;
    * an ARGS FINGERPRINT of the call the operator was looking at. The signature is
      params-INDEPENDENT and the DENY verdict is params-DEPENDENT, so an unscoped
      record would re-arbitrate any call on the tool body;
    * a class that validates through ``effect_tags.coerce``. An unrecognised value
      classifies NOTHING, so storing it would hold a "reclassification" that strips
      the UNKNOWN conjunct and puts nothing in its place.

    Recording also CLEARS any outstanding one-shot resume bridge on the signature.
    The gate's arbitration is about to change; no bridge minted before that change
    can be a decision about the call the change produces, so leaving one standing
    would let it be cashed by the lifted verdict. Belt and braces with the DENY-band
    rule in ``_record_gate_approval`` (which is why no such bridge should exist) and
    the bridge SCOPE check in ``consume_resume_approved``.

    Best-effort; never raises."""
    try:
        sig = dctx.get("tool_signature")
        if not sig:
            logger.info("[ResumeOnDecision] reclassify with no tool_signature "
                        "— recording nothing")
            return None
        if not dctx.get("typed_confirmed"):
            logger.warning(
                "[ResumeOnDecision] reclassify for %s arrived without the typed "
                "confirmation — recording nothing; the run will re-gate. (A "
                "reclassification is only meaningful when the operator transcribed "
                "the class they are asserting.)", sig)
            return None
        fingerprint = str(dctx.get("args_fingerprint") or "").strip()
        if not fingerprint:
            logger.warning(
                "[ResumeOnDecision] reclassify for %s carried no args fingerprint "
                "— recording nothing. An unscoped record would re-arbitrate every "
                "call to this tool body, not the one the operator classified.", sig)
            return None
        from systemu.runtime.effect_tags import EffectTag, coerce
        cls = coerce(dctx.get("assigned_class"))
        if cls == EffectTag.UNKNOWN.value:
            logger.warning(
                "[ResumeOnDecision] reclassify for %s carried no usable effect class "
                "(%r) — recording nothing; the run will re-gate",
                sig, dctx.get("assigned_class"))
            return None
        store = _reclassify_store()
        if store is None:
            return None
        if store.clear_resume_approved(sig):
            logger.warning(
                "[ResumeOnDecision] cleared a stale resume bridge on %s before "
                "recording a reclassification — it was minted under a different "
                "arbitration and must not be redeemable by the lifted verdict", sig)
        return (cls if store.mark_reclassified(sig, cls,
                                               args_fingerprint=fingerprint)
                else None)
    except Exception:
        logger.debug("[ResumeOnDecision] could not record reclassification",
                     exc_info=True)
        return None


def _clear_reclassification(dctx) -> None:
    """Drop any pending reclassification for this gate's tool signature (the operator
    denied the follow-up card). Best-effort; never raises."""
    try:
        sig = dctx.get("tool_signature")
        if not sig:
            return
        store = _reclassify_store()
        if store is not None:
            store.clear_reclassified(sig)
    except Exception:
        logger.debug("[ResumeOnDecision] could not clear reclassification",
                     exc_info=True)


def _record_gate_approval(dctx, *, is_tool_gate: bool, choice: str) -> None:
    """Persist a resolved tool/command gate approval to the CommandApprovalStore.

    Tool gate: ``"always allow"`` → STANDING allow-list (``store.approve``); any other
    non-deny choice → SINGLE-USE resume bridge (``store.mark_resume_approved``). Command
    gate: always a SINGLE-USE bridge (byte-for-byte the v0.9.52 behaviour). The tool
    signature is READ BACK from the context (stamped by the gate at park time), never
    recomputed here (no tool object is in scope).

    Decoupled from the resume DISPATCH so the approval is recorded even when the parked
    run has no resume coords — without it a re-run would re-hit the same gate. The
    caller guarantees ``choice`` is non-deny. Best-effort; never raises."""
    try:
        from pathlib import Path as _P
        from systemu.runtime.command_approvals import init_default_store
        store = init_default_store(_P("data"))
        if store is None:
            return
        if is_tool_gate:
            sig = dctx.get("tool_signature")
            if not sig:
                return
            # IMPL-1: an "Always allow" can NEVER cover the DENY band. The gate stamps the
            # verdict at park time; without this check a standing approval for a
            # DENY-band tool would be consulted by ``tool_sandbox`` BEFORE any band check
            # on the next call and run it UNGATED — a persistent bypass of the
            # unknown-∩-high-severity floor. A DENY is downgraded to single-use, so the
            # gate re-fires every time and the floor keeps being enforced.
            # Normalise via ``.value`` first: ``Verdict`` is a str-Enum, and ``str()`` on
            # the member yields "Verdict.DENY", which would NOT match and would silently
            # re-open the hole for any caller that stamps the enum rather than its value.
            raw = dctx.get("verdict")
            verdict = str(getattr(raw, "value", raw) or "").strip().lower()
            # IMPL-2 ROOT-CAUSE FIX (adversarial review, CRITICAL): a DENY-band gate
            # records NOTHING here — not a standing allow, and not a single-use bridge.
            #
            # "Approve once" on a DENY card is a no-op AT THE GATE: every bypass in
            # ``tool_sandbox._maybe_gate_tool`` sits under ``if verdict != Verdict.DENY``.
            # But this recorder was not band-aware — it fell through to the else-branch
            # and minted ``resume_pending[sig]`` anyway. That was inert only for as long
            # as DENY skipped every bypass. IMPL-2 lifts those same params to
            # REQUIRE_APPROVAL, at which point the stale bridge becomes REDEEMABLE — and
            # the bridge is the one bypass deliberately left live under a pending
            # reclassification. The result was: DENY card → "Approve once" (a natural
            # first move, documented as doing nothing) → reclassify → the re-run cashes
            # the bridge from step two, spends the reclassification, and executes the
            # destructive call with NO card ever shown for the assigned classification.
            #
            # The coords-less rescue above already carries exactly this rule; this is it
            # on the ordinary path. It also stops littering the store with one-shots that
            # are, by contract, unusable.
            #
            # Normalise via ``.value`` first: ``Verdict`` is a str-Enum, and ``str()`` on
            # the member yields "Verdict.DENY", which would NOT match and would silently
            # re-open the hole for any caller that stamps the enum rather than its value.
            if verdict == "deny":
                logger.info(
                    "[ResumeOnDecision] DENY-band tool gate %s resolved %r — recording "
                    "NOTHING (no stored approval may satisfy the DENY band; the remedy "
                    "is to reclassify the effect)", sig, choice)
                return
            # ABSENCE of the verdict fails CLOSED (single-use), matching
            # ``decision_bridge.classify_resolution``, which floors on a missing verdict
            # for the same key. A gate parked before the verdict was carried is rare; the
            # cost of closing it is one extra ask on a legacy card, and the cost of
            # leaving it open is a standing allow on an unknown band.
            # IMPL-2 (defence in depth): a card posted under an operator
            # RECLASSIFICATION may never mint a STANDING allow. The classification is
            # single-use and params-scoped by intent, so a standing allow granted on it
            # would outlive the one call it was reasoned about and cover the
            # params-independent signature forever. The follow-up card does not OFFER
            # "Always allow" — this is the recorder half of that pair, so no other
            # surface can supply the choice and get a standing entry.
            reclassified = bool(dctx.get("reclassified"))
            # IMPL-2 SCOPE (adversarial review, defence in depth #3): stamp WHICH card
            # minted this bridge. A bridge is the operator's decision about one specific
            # card; without the stamp, any unconsumed bridge on the (params-independent)
            # signature satisfies any call — which is what made the attack above
            # redeemable in the first place. The gate honours a bridge only when its
            # scope equals the class the current call is being scored under.
            scope = None
            if reclassified:
                scope = str(dctx.get("assigned_class") or "").strip()
                if not scope:
                    # No coherent scope to mint under, and an UNSCOPED bridge on a
                    # reclassified card is precisely what the gate refuses to honour.
                    # Record nothing rather than a one-shot that can never be spent.
                    logger.warning(
                        "[ResumeOnDecision] reclassified card for %s carried no "
                        "assigned_class — recording nothing; the run will re-gate", sig)
                    return
            if choice == "always allow" and verdict and not reclassified:
                store.approve(sig)
            else:
                store.mark_resume_approved(sig, for_reclassification=scope)
        else:
            from systemu.runtime.command_approvals import command_signature
            sig = command_signature(dctx.get("command") or "", cwd=dctx.get("cwd") or "")
            store.mark_resume_approved(sig)
    except Exception:
        logger.debug("[ResumeOnDecision] could not record gate approval", exc_info=True)


def _stamp_dispatched(decision, vault) -> None:
    """Stamp the persisted ``resume_dispatched`` marker so a concurrent poll/event
    can't double-dispatch (across restarts, across both trigger paths)."""
    try:
        decision.context["resume_dispatched"] = True
        vault.save_decision(decision)
    except Exception:
        logger.debug(
            "[ResumeOnDecision] could not stamp resume_dispatched on %s",
            decision.id, exc_info=True,
        )


def handle_decision_resolved(event: Dict[str, Any], *, vault, supervisor,
                             data_dir: Optional[Path] = None) -> None:
    """EventBus adapter: process one ``operator_decision_resolved`` event.

    Fetches the decision and delegates to :func:`_dispatch_resume`.
    Best-effort; never raises (EventBus subscribers must not crash the
    publisher).
    """
    try:
        if event.get("category") != "operator_decision_resolved":
            return
        ctx = event.get("context") or {}
        decision_id = ctx.get("decision_id")
        if not decision_id or decision_id in _handled:
            return
        try:
            dec = vault.get_decision(decision_id)
        except Exception:
            return
        _dispatch_resume(dec, vault=vault, supervisor=supervisor, data_dir=data_dir)
    except Exception:
        logger.debug("[ResumeOnDecision] handler error", exc_info=True)


def register(vault, supervisor, data_dir: Optional[Path] = None):
    """Subscribe the handler to the EventBus. Returns the unsubscribe callable."""
    from systemu.interface.event_bus import EventBus

    def _cb(ev):
        handle_decision_resolved(ev, vault=vault, supervisor=supervisor, data_dir=data_dir)

    unsub = EventBus.get().subscribe(_cb, replay=False)
    logger.info("[ResumeOnDecision] registered EventBus subscriber for stuck-decision resume")
    return unsub
