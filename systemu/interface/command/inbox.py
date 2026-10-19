"""InboxQueue — the one queue (spec §4.3 / D3). A thin facade over the shipped
OperatorDecisionQueue that speaks GateDescriptor. No new store: every gate is an
OperatorDecision in the existing vault `decisions` collection, carrying the
descriptor in context (kind="gate"). The harness path already lands here via
harness_review.surface_harness_request."""
from __future__ import annotations

import logging
from typing import List, Tuple

from systemu.interface.command.gate import GateDescriptor
from systemu.interface.command.result import CommandResult, CommandStatus
# IMPL-4. Safe at module scope: ``first_gate_review`` has no top-level ``systemu``
# imports (it reaches back into this module lazily, inside post_bulk_review_card).
from systemu.runtime.first_gate_review import BULK_GATE_TYPE
# D-2 (e2e v0.10.31). The resume rail's OWN naming of the card the operator
# clicked. Imported rather than re-derived so the toast and the rail's event
# cannot drift into naming two different cards. Safe at module scope for the
# same reason as the import above: ``resume_on_decision`` has no top-level
# ``systemu`` imports at all. Deliberately NOT lazy inside the resolve branch —
# a broken import must fail at import time, in every process, and not halfway
# through a click that has already recorded the operator's decision.
from systemu.runtime.resume_on_decision import _gate_card_label
# R1: the rail's own refusal + re-open, imported for the same reason -- the
# executor's sentence and the rail's action must be one decision.
from systemu.runtime.resume_on_decision import (
    reopen_unconfirmed_gate as _reopen_unconfirmed_gate,
    typed_confirm_refusal_for as _typed_confirm_refusal_for)

logger = logging.getLogger(__name__)

_APPROVE_LABELS = {"approve", "approve & apply", "approve & install", "forge",
                   "enable & run"}

# CALLER-READS-CHOICE gates: resolving ANY option unblocks the waiting run,
# because the parked caller re-reads the answer via get_resolved_choice and
# `resume_on_decision` dispatches a resume. Nothing here should re-execute, so
# these are NOT gated on _APPROVE_LABELS — every resolution is a valid answer.
#
# F34: "command" and "tool" belong here and were missing. Their own option
# labels are Deny / Approve once / Always allow, none of which appear in
# _APPROVE_LABELS, so EVERY resolution — including the affirmative ones — fell
# through to the generic early-exit and reported
#     Gate tool not approved (Always allow).
# The standing allow was in fact recorded and the tool ran on the next attempt,
# so the only feedback the operator received about a permission they had just
# granted was false. DEC-34: a false assertion about what just happened is a
# defect in its own right. Observed live while driving the forge end to end.
#
# resume_on_decision.py already treats exactly these two gate_types as
# resumable (is_cmd_gate / is_tool_gate), which is the same contract stated
# from the other side.
_RENDER_ONLY_GATES = {"operator", "command", "tool"}

# ── D-2 (e2e v0.10.31): the summary says what the decision DID ────────────────
#
# WITNESSED: a **Deny** on a tool-run gate produced
#     "Operator decision recorded (Deny); run unblocked."
# in a GREEN toast, while the resume rail — reading the same decision — finalized
# the activity FAILED and wrote "Denied: <card> - the task was stopped and will
# not run." Two surfaces, one event, and the louder one was false. DEC-34: a
# false assertion about what just happened is a defect in its own right.
#
# The mint below is keyed on the SAME normalized ``decision.choice`` the rail
# reads (``(choice or "").strip().lower()``, "" meaning deny — see
# ``resume_on_decision._dispatch_resume``), so there is exactly ONE derivation of
# the choice on this path and the two surfaces cannot disagree. The card name
# comes from the rail's own ``_gate_card_label``.
_GATE_DENY_CHOICES = frozenset({"deny", ""})
_GATE_APPROVE_ONCE = "approve once"
_GATE_ALWAYS_ALLOW = "always allow"

# ── What an "Always allow" on THIS card actually remembers ────────────────────
#
# The tail used to read "remembering this exact tool body and effect set" for
# EVERY render-only gate. That is the TOOL card's own copy (gate.py); the COMMAND
# card says "'Always allow' remembers this exact command", and an operator card
# has neither semantics. Telling the operator of a command gate that the system
# just memorised a tool body and effect set is a false assertion at the boundary
# — DEC-34, the same class of defect as the "run unblocked" green toast this
# sentence was rewritten to fix.
#
# So the clause is minted per gate type, from ONE mapping, and the default is
# SILENCE: no clause unless a standing approval is provably written on a path
# this sentence's callers travel. What that means per gate type, traced through
# the rail and witnessed in tests/test_e2e31_d2_gate_decision_summary.py:
#
#   tool     — ``resume_on_decision._record_gate_approval`` calls
#              ``CommandApprovalStore.approve(ctx["tool_signature"])``, a STANDING
#              allow-list entry keyed on ``tool_signature(name, body_hash,
#              effect_tags, host_class)``. "This exact tool body and effect set"
#              is that key in English. It is written ONLY for a card carrying a
#              stamped ``verdict`` that is not DENY and is not a ``reclassified``
#              follow-up — otherwise the recorder falls back to a SINGLE-USE
#              resume bridge, or (on the DENY band) writes nothing at all. The
#              conditions below are that contract, read off the recorder.
#   command  — the recorder's else-branch ALWAYS mints only a single-use bridge.
#              The standing command allow exists but is minted exclusively by
#              ``command_gate_handler``, reached only through
#              ``decision_dispatcher.dispatch`` — which exactly one surface calls
#              (interface/pages/insights.py), and that surface never calls
#              ``resolve_gate``. So no reader of this sentence has a standing
#              command approval on record; claiming one is also precisely what
#              ``test_v0932_command_gate`` fences.
#   operator — ``_dispatch_resume`` reaches the recorder only for gate_type in
#              {command, tool}, and no dispatcher handler is registered for the
#              ``operator`` namespace. Nothing persists.
_STANDING_APPROVAL_SCOPE = {"tool": "this exact tool body and effect set"}


def standing_approval_scope(gate_type: str, ctx) -> str:
    """What a standing "Always allow" on this card covers, or "" for nothing.

    The ONE source of the remembering clause. A branch that spelled its own tail
    would be a second derivation of the persistence fact, and a second
    derivation is exactly how the tool card's copy ended up on command cards.

    Returns "" — no claim at all — whenever the recorder would not write a
    standing entry. Fail-closed by construction: a clause is minted only for the
    shape whose persistence is witnessed, so a card the rail handles some new
    way gets silence rather than an inherited promise.
    """
    scope = _STANDING_APPROVAL_SCOPE.get(gate_type, "")
    if not scope:
        return ""
    # The recorder keys the standing entry on the signature the gate stamped at
    # park time; with no signature it returns without writing.
    if not ctx.get("tool_signature"):
        return ""
    # ``Verdict`` is a str-Enum. Normalise via ``.value`` FIRST — ``str()`` on the
    # member yields "Verdict.DENY", which would not match and would let a
    # DENY-band card claim a standing allow that was never recorded. This mirrors
    # ``_record_gate_approval`` byte for byte, deliberately.
    raw = ctx.get("verdict")
    verdict = str(getattr(raw, "value", raw) or "").strip().lower()
    if not verdict or verdict == "deny":
        # An ABSENT verdict fails closed to a single-use bridge at the recorder;
        # a DENY band records nothing whatsoever. Neither is a memory.
        return ""
    if ctx.get("reclassified"):
        # IMPL-2: a follow-up card posted under an operator reclassification may
        # never mint a standing allow — only a scoped one-shot.
        return ""
    return scope


def _ascii(text: str) -> str:
    """DEC-32c: a verdict-carrying line is ASCII. This one crosses a browser
    toast, ``CommandResult.to_rich`` at a Windows console, and the JSON render."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def gate_decision_notice(decision, choice: str) -> Tuple[str, str]:
    """Pure: the ``(summary, notice_kind)`` for a resolved RENDER-ONLY gate.

    ``choice`` is the caller's already-normalized ``decision.choice`` — passed in
    rather than re-derived here, because a second derivation is exactly how the
    sentence and the action drift apart. ``notice_kind`` is minted here and
    carried on the result so no rendering surface has to guess a colour for a
    verdict it did not make.

    Exported (not underscored) so the honest-reporting rule is unit-testable
    without a NiceGUI runtime, like ``_inbox_card_model`` next door.

    THE SENTENCE HAS TWO HALVES AND NEEDS BOTH:
    ``Recorded (<Choice>): <title> - <outcome>.`` It echoes the RECORDED CHOICE,
    spelled the way the button was ("Deny", not "Denied"; "Always allow", not
    "Approved and remembered"), and then says what that choice DID. F34 fences
    the first half — an operator who has just clicked one of three buttons must
    be able to read back WHICH ONE was recorded, and a paraphrase of a click is
    indistinguishable from a paraphrase of a different click. D-2 fences the
    second half — "recorded" alone is what let a Deny be reported as "run
    unblocked" in a green toast. Dropping either half to satisfy the other
    restores one of the two defects, so both fences are stated together in
    ``tests/test_e2e31_d2_gate_decision_summary.py``.

    THE ECHO IS PARENTHESISED BECAUSE THAT IS THE ONE SHAPE BOTH SHIPPED FENCES
    ACCEPT. ``test_v0932_command_gate::
    test_resolve_gate_does_not_persist_command_always_allow`` requires that a
    command gate's summary contain no "always allow" once ``(always allow)`` is
    stripped: the rail persists no standing command approval, so any prose
    repetition of the button's name reads as a claim that it applied one. F34
    requires the label to appear at all. The parenthesised echo satisfies both,
    and nothing else does.

    The echoed label for a KNOWN outcome is the canonical spelling for that
    branch, not a raw echo of the stored string: "  DENY  " and a missing choice
    are the same recorded decision as "Deny" (the rail normalizes exactly so),
    and the operator is shown the one label the branch means. Only the
    render-only fallback — a card with its own bespoke options — echoes the
    label as the decision object carries it, because there is no canonical
    spelling to prefer.

    The Always-allow tail is asked of ``standing_approval_scope`` rather than
    spelled here, so this function claims only what the rail actually persists
    for THIS gate type — see that helper for the per-type trace.
    """
    ctx = decision.context or {}
    gate_type = ctx.get("gate_type", "")
    title = _gate_card_label(decision, ctx, gate_type)
    if choice in _GATE_DENY_CHOICES:
        return (_ascii(f"Recorded (Deny): {title} - the task was stopped and "
                       f"will not run."),
                "negative")
    if choice == _GATE_APPROVE_ONCE:
        return _ascii(f"Recorded (Approve once): {title} - resuming now."), "positive"
    if choice == _GATE_ALWAYS_ALLOW:
        scope = standing_approval_scope(gate_type, ctx)
        remembers = f" and remembering {scope}" if scope else ""
        return (_ascii(f"Recorded (Always allow): {title} - resuming now"
                       f"{remembers}."),
                "positive")
    return _ascii(f"Recorded ({decision.choice}): {title}."), "info"


def _handle_forge_rejection(decision, vault) -> CommandResult:
    """v0.9.49: the operator declined a ``forge:<tool_id>`` gate. Flag the tool
    ``forge_rejected`` (so it's recognized as permanently unavailable) and finalize
    every PARTIAL activity that required it, instead of leaving them parked forever.
    Best-effort; never raises."""
    from systemu.core.models import ActivityStatus
    from systemu.runtime.activity_completion import finalize_unsatisfiable_activity

    _, _, tool_id = (decision.dedup_key or "").partition(":")
    if not tool_id:
        return CommandResult(status=CommandStatus.NOOP,
                             summary="Forge declined (no tool id in gate).")
    name = tool_id
    try:
        tool = vault.get_tool(tool_id)
        name = getattr(tool, "name", tool_id) or tool_id
        tool.forge_rejected = True
        vault.save_tool(tool)
    except Exception:
        logger.debug("[Inbox] forge-reject: could not flag tool %s", tool_id, exc_info=True)

    finalized = 0
    try:
        for header in vault.list_activities(status=ActivityStatus.PARTIAL):
            act_id = header.get("id")
            if act_id and tool_id in (header.get("required_tool_ids") or []):
                if finalize_unsatisfiable_activity(
                        vault, act_id,
                        context=f"Tool '{name}' was declined at forge review."):
                    finalized += 1
    except Exception:
        logger.debug("[Inbox] forge-reject: finalize sweep failed for %s", tool_id, exc_info=True)

    if finalized:
        return CommandResult(
            status=CommandStatus.OK,
            summary=(f"Declined forging '{name}'; finalized {finalized} parked "
                     f"task(s) that required it."))
    return CommandResult(status=CommandStatus.NOOP,
                         summary=f"Declined forging '{name}'.")


def _handle_bulk_first_gate_review(decision) -> CommandResult:
    """IMPL-4: apply a resolved bulk first-gate review.

    The batch Always-allow records a STANDING allow only for a tool that BOTH gates
    (REQUIRE_APPROVAL band) and carries a batch-approvable effect class
    (``effect_tags.BATCH_APPROVABLE``). Every entry is re-scored inside
    ``apply_bulk_decision`` from its raw signals — the verdict stamped in the decision
    context is untrusted by the time it comes back out of the store, and a card posted by
    an older build may still list a shell tool as eligible — so neither a DENY-band nor a
    high-authority tool can be swept in, whatever the stored row says. Best-effort; never
    raises."""
    from systemu.runtime.first_gate_review import (apply_bulk_decision,
                                                   entries_from_context)
    ctx = decision.context or {}
    entries = entries_from_context(ctx)
    try:
        from systemu.runtime.command_approvals import init_default_store
        from pathlib import Path as _P
        store = init_default_store(_P("data"))
    except Exception:
        logger.debug("[Inbox] bulk first-gate review: no approval store", exc_info=True)
        store = None

    written = apply_bulk_decision(entries, choice=decision.choice, store=store)
    if not written:
        return CommandResult(
            status=CommandStatus.NOOP,
            summary=(f"First-gate review recorded ({decision.choice}); no tools were "
                     f"batch-approved."))
    excluded = len(entries) - len(written)
    return CommandResult(
        status=CommandStatus.OK,
        summary=(f"Remembered {len(written)} tool(s) from the first-gate review; "
                 f"{excluded} still gated (a high-authority or unclassified effect "
                 f"cannot be batch-approved)."))


def resolve_gate(decision, *, vault) -> CommandResult:
    """Execute the action a resolved gate authorizes (Approve EXECUTES).

    Dispatches by gate_type; Reject/Deny/Dismiss are no-ops that still record
    the operator's choice. The scroll slice calls the SAME executor the CLI
    `scrolls approve`, the dashboard, and the scheduler use, so Approve genuinely
    runs approval + activity extraction (spec §4.3)."""
    ctx = decision.context or {}
    gate_type = ctx.get("gate_type", "")
    choice = (decision.choice or "").strip().lower()

    # ── Render-only gates: any resolution unblocks the waiting caller ─────────
    # The contract for an operator gate is "resolve unblocks the waiting run":
    # notify_user / request_choice posted the decision and the parked caller
    # re-reads the chosen value via OperatorDecisionQueue.get_resolved_choice.
    # We must NOT re-execute anything here — just acknowledge the resolution.
    if gate_type in _RENDER_ONLY_GATES:
        # R1 (spec section 10): a money / irreversible card resolved WITHOUT its
        # typed phrase -- by any surface that called ``queue.resolve`` directly (the
        # CLI, a stale card's "Always allow") -- takes no effect. The resume rail
        # refuses it too; this is the executor's half, so the sentence the caller
        # prints is true: nothing ran, and the card is back for the typed confirm.
        refusal = _typed_confirm_refusal_for(decision)
        if refusal:
            reopened = False
            if vault is not None:
                reopened = _reopen_unconfirmed_gate(decision, vault, refusal)
            title = _gate_card_label(decision, ctx, gate_type)
            return CommandResult(
                status=CommandStatus.NOOP,
                summary=_ascii(f"Not approved: {title} - {refusal} Nothing ran"
                               + ("; the card is back in your Inbox." if reopened
                                  else ".")),
                data={"notice_kind": "warning", "choice": decision.choice or "",
                      "typed_confirm_refused": True},
            )
        # D-2: one sentence per OUTCOME, minted from the same normalized choice
        # the resume rail acts on. The status stays OK for every choice — the
        # decision WAS recorded, and recording a denial is a success; what the
        # denial did rides ``notice_kind``, not an error code.
        summary, kind = gate_decision_notice(decision, choice)
        return CommandResult(
            status=CommandStatus.OK,
            summary=summary,
            data={"notice_kind": kind, "choice": decision.choice or ""},
        )

    # v0.9.49: a DECLINED forge gate must finalize the tasks parked on that tool —
    # not leave them hanging waiting_on_tools forever (the rejected-tool RCA).
    # Detect it BEFORE the generic non-approve early-exit below.
    if gate_type == "forge" and choice not in _APPROVE_LABELS:
        return _handle_forge_rejection(decision, vault)

    # IMPL-4: the one-time bulk first-gate review card owns its OWN option labels
    # ("Leave gated" / "Review individually" / the batch allow), none of which are in
    # _APPROVE_LABELS — so it is dispatched BEFORE the generic early-exit below, which
    # would otherwise NOOP every resolution and leave the card inert. The executor
    # matches its affirmative label exactly and re-scores every entry, so a non-approve
    # choice (and any label drift) records nothing.
    if gate_type == BULK_GATE_TYPE:
        return _handle_bulk_first_gate_review(decision)

    if choice not in _APPROVE_LABELS:
        return CommandResult(
            status=CommandStatus.NOOP,
            summary=f"Gate {gate_type} not approved ({decision.choice}).",
        )

    if gate_type == "scroll":
        _, _, scroll_id = (decision.dedup_key or "").partition(":")
        from systemu.pipelines.scroll_refiner import approve_pending_scroll
        approve_pending_scroll(scroll_id, vault)
        return CommandResult(
            status=CommandStatus.OK,
            summary=f"Approved scroll {scroll_id}; extraction started.",
        )

    if gate_type == "dep":
        # dedup: dep:<package>
        _, _, package = (decision.dedup_key or "").partition(":")
        from systemu.runtime.dep_approvals import approve_and_install
        # The requesting tool id round-trips through the decision context
        # (GateDescriptor.tool_id, set by from_dep). It targets the post-install
        # dry-run at the real requesting tool. Fall back to the legacy
        # first_seen_tool_id key for any pre-existing rows.
        tool_id = ctx.get("tool_id") or ctx.get("first_seen_tool_id", "") or ""
        approve_and_install(tool_id=tool_id, package=package, source="inbox")
        return CommandResult(
            status=CommandStatus.OK,
            summary=f"Approved dependency {package}; pip install + dry-run started.",
        )

    if gate_type == "tools_blocked":
        # W1.2: a task parked by the Stage-3.5 readiness gate. Approve runs
        # the canonical Gate-3 verb (tools_enable) per blocking tool — the
        # Gate-3.5 rule (dry-run must have passed) stays enforced by the verb,
        # so never-dry-run tools are REPORTED, not silently bypassed. Once
        # enabled, the heal sweep / recovery Pass 2 re-runs the parked task.
        from systemu.interface.command.verbs import tools_enable
        tool_ids = (ctx.get("tool_ids") or [])
        # v0.9.51: the operator is explicitly retrying, so give each blocking tool a
        # FRESH dry-run under current code FIRST — a tool cached `failed` under older
        # code (or before a fix) may now pass, in which case tools_enable below
        # actually succeeds instead of reporting it stuck. force=True bypasses the
        # once-per-session bound since this is a deliberate operator action.
        _act_for_reval = ctx.get("activity_id")
        if _act_for_reval:
            try:
                from systemu.scheduler.tool_reconciler import revalidate_blocking_failed_tools
                from sharing_on.config import Config as _RevalCfg
                revalidate_blocking_failed_tools(vault, _RevalCfg.from_env(), _act_for_reval, force=True)
            except Exception:
                logger.debug("[Inbox] tools_blocked: pre-enable re-validate failed", exc_info=True)
        enabled, skipped, failed = [], [], []
        for tid in tool_ids:
            res = tools_enable(tid, vault=vault)
            label = (res.data or {}).get("tool_id", tid) if res.data else tid
            if res.status == CommandStatus.OK:
                enabled.append(label)
            elif res.status == CommandStatus.NOOP:
                skipped.append(label)
            else:
                failed.append(f"{label}: {res.summary}")

        # v0.9.49 (broadens v0.9.48 Phase 4.1): never silently stuck. If nothing
        # was newly enabled and the parked activity awaits a tool that can never
        # become available — dry-run FAILED, the forge was DECLINED, or the tool
        # was never forged — finalize it cleanly instead of leaving it parked
        # `waiting_on_tools` forever. The shared finalizer's ANY-unavailable rule
        # covers the repro (one deployed tool + one declined tool) and is
        # idempotent vs the F2 forge-reject event and the F4 reaper.
        activity_id = ctx.get("activity_id")
        if activity_id and not enabled:
            from systemu.runtime.activity_completion import finalize_unsatisfiable_activity
            from systemu.interface.notifications import log_event
            reason = finalize_unsatisfiable_activity(
                vault, activity_id, context="Enable & run could not proceed.")
            if reason:
                log_event(
                    "ERROR", "activity",
                    f"Activity {activity_id} finalized — {reason}",
                    {"activity_id": activity_id})
                return CommandResult(status=CommandStatus.ERROR, summary=reason)

        # v0.9.43: actually FIRE the heal sweep. The gate's contract is "the heal
        # sweep re-runs the parked task once tools are ready," but only the Tools
        # page ever called it — the Inbox "Enable & run" path enabled the tool and
        # then stopped, leaving the parked activity stuck (forge demo hang). Trigger
        # it here for every tool the gate covers; heal is idempotent (it only
        # re-dispatches an activity once ALL its required tools are ready). It is
        # blocking (decide_shadow makes LLM calls) → run in a daemon thread so the
        # resolving Inbox/CLI caller returns immediately.
        if enabled or skipped:
            try:
                import threading

                from sharing_on.config import Config
                from systemu.pipelines.tool_service import heal_activities_for_tool
                _cfg = Config.from_env()
                for tid in tool_ids:
                    threading.Thread(
                        target=heal_activities_for_tool,
                        args=(tid, _cfg, vault),
                        daemon=True,
                    ).start()
            except Exception:
                logger.exception(
                    "[Inbox] tools_blocked: failed to start heal sweep for %s",
                    tool_ids,
                )
        parts = []
        if enabled:
            parts.append(f"enabled {len(enabled)} tool(s)")
        if skipped:
            parts.append(f"{len(skipped)} already enabled")
        if failed:
            parts.append(f"{len(failed)} blocked — " + " | ".join(failed))
        summary = ("Readiness gate: " + "; ".join(parts or ["no tools to enable"])
                   + ". The heal sweep re-runs the parked task once tools are ready.")
        return CommandResult(
            status=CommandStatus.ERROR if (failed and not enabled) else CommandStatus.OK,
            summary=summary,
        )

    if gate_type == "forge":
        # dedup: forge:<tool_id>. Replicates _handle_resolved_forge_tool: look
        # up the proposed tool and re-run forge_tool_from_spec with the
        # unedited spec. Two-stage (spec approved now; code generation runs).
        _, _, tool_id = (decision.dedup_key or "").partition(":")
        from sharing_on.config import Config
        from systemu.pipelines.tool_forge import forge_tool_from_spec
        config = Config.from_env()
        tool = vault.get_tool(tool_id)
        forge_tool_from_spec(tool_id, tool.model_dump_json(), config, vault)
        return CommandResult(
            status=CommandStatus.OK,
            summary=(f"Spec approved for tool {tool_id}; code generation "
                     f"queued (two-stage forge)."),
        )

    if gate_type == "evolution":
        # dedup: evolution:<evolution_id>
        _, _, evolution_id = (decision.dedup_key or "").partition(":")
        from sharing_on.config import Config
        from systemu.pipelines.evolution_engine import apply_evolution
        # Surface the silent-no-op-for-non-UPGRADE bug: apply_evolution marks
        # unimplemented types APPLIED and returns True silently. Pre-check the
        # evolution type here so an unimplemented apply reports ERROR instead.
        # The one implemented path is upgrade-on-shadow (checked below via the
        # inverse condition); any other type is deferred to Phase S3.
        try:
            evolution = vault.get_evolution(evolution_id)
            evo_type = getattr(getattr(evolution, "evolution_type", None),
                               "value", str(getattr(evolution, "evolution_type", "")))
            target_type = getattr(evolution, "target_entity_type", "")
        except Exception:
            evo_type, target_type = "", ""
        # The one implemented path is UPGRADE on a shadow (see
        # evolution_engine.apply_evolution). Anything else is deferred to
        # Phase S3 and must NOT be silently swallowed.
        if evo_type and not (evo_type == "upgrade" and target_type == "shadow"):
            return CommandResult(
                status=CommandStatus.ERROR,
                summary=(f"Evolution type {evo_type!r} not yet implemented "
                         f"(deferred to Phase S3); not applied."),
            )
        config = Config.from_env()
        applied = apply_evolution(evolution_id, config, vault)
        if applied:
            return CommandResult(
                status=CommandStatus.OK,
                summary=f"Applied evolution {evolution_id}.",
            )
        return CommandResult(
            status=CommandStatus.ERROR,
            summary=f"apply_evolution returned False for {evolution_id}.",
        )

    if gate_type == "harness":
        # The real harness grant/deny executor is
        # ``Supervisor.resume_after_grant(*, execution_id, activity_id,
        # shadow_id, grant_payload, ...)`` (systemu/runtime/supervisor.py).
        # It requires a LIVE daemon Supervisor singleton plus resume
        # coordinates (activity_id / shadow_id) that the harness decision
        # context does NOT carry — only execution_id / request_id / kind.
        # resolve_gate's (decision, *, vault) signature cannot reach that
        # path cleanly, so we SURFACE the un-wired state (never silent) and
        # defer the real re-dispatch wiring to a follow-up task rather than
        # fabricating a Supervisor here. The operator's choice IS recorded
        # on the decision; the daemon-side resume path can consume it.
        return CommandResult(
            status=CommandStatus.QUEUED,
            summary=("Harness decision recorded ({choice}); daemon-side "
                     "resume_after_grant re-dispatch is not wired into "
                     "resolve_gate yet (needs the live Supervisor + resume "
                     "coords). Deferred to Task 18.").format(
                         choice=decision.choice),
        )

    if gate_type == "recovery":
        # dedup: recovery:<scope_kind>:<scope_id>:<kind>. Reconstruct the
        # minimal RecoveryAction the shared apply path (verbs.doctor_apply →
        # recover._handle_action) needs and run it.
        parts = (decision.dedup_key or "").split(":")
        if len(parts) >= 4:
            _, scope_kind, scope_id, action_kind = parts[0], parts[1], parts[2], parts[3]
            from systemu.recovery.engine import RecoveryAction
            from systemu.interface.command.verbs import doctor_apply
            action = RecoveryAction(
                scope_kind=scope_kind,
                scope_id=scope_id,
                kind=action_kind,
                reason=ctx.get("inspect", "") or "",
                fix_url="",
                fix_command=ctx.get("what_approve_does", "") or None,
                severity="blocker",
            )
            return doctor_apply([action], vault=vault)
        return CommandResult(
            status=CommandStatus.NOOP,
            summary=f"Malformed recovery dedup_key {decision.dedup_key!r}.",
        )

    return CommandResult(
        status=CommandStatus.NOOP,
        summary=f"No executor wired for gate_type {gate_type!r}.",
    )


def resolve_pending_gate(dec_id: str, *, choice: str, vault,
                         typed_confirm=None) -> CommandResult:
    """Resolve a PENDING gate from the Inbox card, then run its executor.

    R1 (spec section 10): the server-side typed-confirm check runs BEFORE anything
    is recorded. A money / irreversible card resolved with anything but "Approve
    once" plus the exact phrase records NOTHING -- the decision stays pending, no
    event fires, the rail never sees it -- and the result says why. When the phrase
    is right, the operator's typed text is persisted on the decision in the SAME
    save that resolves it (``resolve_with_context_patch``), so the rail and the
    gate re-check the very value the operator typed rather than trusting that a
    check happened somewhere upstream.

    Every other card resolves exactly as before (``queue.resolve`` ->
    :func:`resolve_gate`).
    """
    from systemu.runtime.command_approvals import (CONFIRM_TYPED_KEY,
                                                   TYPED_CONFIRM_REFUSED_KEY,
                                                   irreversible_card_effects,
                                                   typed_confirm_refusal)
    queue = InboxQueue(vault)._queue
    decision = vault.get_decision(dec_id)
    ctx = getattr(decision, "context", None) or {}
    if irreversible_card_effects(ctx):
        refusal = typed_confirm_refusal(ctx, choice=choice, typed=typed_confirm)
        if refusal:
            title = _gate_card_label(decision, ctx, ctx.get("gate_type", ""))
            return CommandResult(
                status=CommandStatus.NOOP,
                summary=_ascii(f"Not approved: {title} - {refusal} Nothing ran; "
                               f"the card is still waiting for you."),
                data={"notice_kind": "warning", "choice": choice or "",
                      "typed_confirm_refused": True},
            )
        patch = {TYPED_CONFIRM_REFUSED_KEY: ""}
        if type(typed_confirm) is str and choice.strip().lower() != "deny":
            patch[CONFIRM_TYPED_KEY] = typed_confirm.strip()
        resolved = queue.resolve_with_context_patch(dec_id, choice=choice,
                                                    context_patch=patch)
        return resolve_gate(resolved, vault=vault)
    resolved = queue.resolve(dec_id, choice=choice)
    return resolve_gate(resolved, vault=vault)


class _SyntheticApproved:
    """A lightweight stand-in for a resolved OperatorDecision, used to drive
    resolve_gate for an auto-granted (Bypass) gate WITHOUT posting a row.

    Carries exactly the three fields resolve_gate reads — context, dedup_key,
    choice — so the SAME executor that runs for an operator-approved gate runs
    for an auto-granted one (no second code path)."""

    __slots__ = ("context", "dedup_key", "choice")

    def __init__(self, context, dedup_key, choice):
        self.context = context
        self.dedup_key = dedup_key
        self.choice = choice


def _synthetic_approved(descriptor: GateDescriptor, gate_type: str) -> _SyntheticApproved:
    """Build the synthetic approved decision for an auto-granted gate.

    The approve choice is descriptor.options[-1] (the affirmative option, e.g.
    "Approve" / "Forge" / "Approve & Install"); resolve_gate's _APPROVE_LABELS
    check matches it and executes the authorized action."""
    return _SyntheticApproved(
        context=descriptor.to_decision_context(gate_type=gate_type),
        dedup_key=descriptor.dedup,
        choice=descriptor.options[-1] if descriptor.options else "",
    )


class InboxQueue:
    def __init__(self, vault):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        self._vault = vault
        self._queue = OperatorDecisionQueue(vault)

    def enqueue(self, descriptor: GateDescriptor, *, gate_type: str, body: str = "",
                policy=None, capability: str = "", vault=None, context_extras=None):
        """Post a gate for operator review, OR enforce the gate-mode dial.

        With ``policy is None`` this behaves exactly as before: it posts the
        gate and returns the decision id.

        With a ``policy`` (a GateModePolicy), it consults the dial first:
          * ``"allow"`` — auto-grant: run resolve_gate on a synthetic approved
            decision and return its CommandResult; DON'T post.
          * ``"deny"``  — record an auditable resolved-denied row (post then
            resolve with the safe-default) and DON'T execute; return the id.
          * ``"ask"``   — fall through to today's post() and return the id.

        The floor (D5) is enforced inside ``policy.decide`` — floor gate types /
        capabilities resolve to ``"ask"`` even under Bypass, so they post.

        ``context_extras`` is an optional dict of gate-type-specific fields to
        carry alongside the descriptor's serialized context. ``to_decision_context``
        only emits the fixed descriptor fields; a producer that needs to round-trip
        extra keys for its (future) executor — e.g. harness surfacing must preserve
        execution_id / request_id / harness_kind / spec for the deferred
        grant-resume — passes them here. The canonical gate keys (``kind="gate"`` /
        ``gate_type``) always win, so extras can never shadow the Inbox marker that
        ``list_descriptors`` filters on.
        """
        ctx = descriptor.to_decision_context(gate_type=gate_type)
        if context_extras:
            # Extras first, then the descriptor context — so kind/gate_type and
            # the serialized descriptor fields always take precedence and the
            # Inbox marker (kind="gate") can never be clobbered by a producer.
            ctx = {**dict(context_extras), **ctx}

        if policy is not None:
            verdict = policy.decide(
                risk=descriptor.risk, gate_type=gate_type, capability=capability)
            if verdict == "allow":
                # Auto-grant: execute the authorized action WITHOUT posting.
                # W10.2: but NEVER without a trace — the deny path always
                # recorded an audit row while allow recorded NOTHING, leaving
                # policy auto-grants invisible to the audit trail. Save a
                # BORN-RESOLVED decision row (resolved_by=auto_policy): it
                # shows in /inbox History and exports with the ledger, but
                # was never pending — so it cannot ping the needs-you badge
                # or push "Needs you" to the operator's phone. Best-effort:
                # an audit-write failure must not block the grant (it is
                # logged loud instead).
                try:
                    self._save_auto_allow_audit_row(
                        descriptor, gate_type, ctx,
                        vault=vault or self._vault)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "[inbox] could not record auto-allow audit row for %s",
                        descriptor.dedup, exc_info=True)
                synthetic = _synthetic_approved(descriptor, gate_type)
                return resolve_gate(synthetic, vault=vault or self._vault)
            if verdict == "deny":
                # Audit, don't execute: post then immediately resolve with the
                # safe-default (the deny/reject/dismiss option) so the denial is
                # an inspectable, resolved row — never silent.
                dec_id = self._queue.post(
                    title=descriptor.title,
                    body=body or descriptor.inspect,
                    options=descriptor.options,
                    context=ctx,
                    dedup_key=descriptor.dedup,
                )
                safe = descriptor.safe_default or (
                    descriptor.options[0] if descriptor.options else "")
                try:
                    self._queue.resolve(dec_id, choice=safe)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        "[inbox] could not record denied audit row for %s", dec_id)
                return dec_id
            # verdict == "ask": fall through to post().

        return self._queue.post(
            title=descriptor.title,
            body=body or descriptor.inspect,
            options=descriptor.options,
            context=ctx,
            dedup_key=descriptor.dedup,
        )

    def _save_auto_allow_audit_row(self, descriptor: GateDescriptor,
                                   gate_type: str, ctx: dict, *, vault) -> None:
        """W10.2: persist a born-resolved decision row for a policy
        auto-grant. Constructed directly (NOT posted): posting would fire
        operator_decision_posted → needs-you badge + phone push for
        something that needed no attention."""
        import uuid
        from datetime import datetime, timezone
        from systemu.approval.decision_queue import OperatorDecision

        now = datetime.now(tz=timezone.utc)
        options = list(descriptor.options or [])
        affirmative = options[-1] if options else "Approve"
        vault.save_decision(OperatorDecision(
            id=f"dec_{uuid.uuid4().hex[:8]}",
            title=descriptor.title,
            body=(descriptor.inspect or "")[:500],
            options=options or [affirmative],
            context={**ctx, "resolved_by": "auto_policy"},
            dedup_key=descriptor.dedup,
            status="resolved",
            choice=affirmative,
            created_at=now,
            resolved_at=now,
        ))
        # S1b (PLAN-11): this row is born-resolved and bypasses post()/resolve()
        # entirely, so without this it would be invisible to the approval-
        # fatigue counters (an auto-policy "Always allow" grant). Count it as
        # a resolved gate card. Best-effort: never break the auto-grant audit.
        try:
            from pathlib import Path

            from systemu.runtime.metrics_store import MetricsStore
            MetricsStore(Path(vault.root) / "metrics").record_resolution(
                0.0, ts=now.timestamp(), choice=affirmative)
        except Exception:
            logger.debug(
                "[inbox] metrics record_resolution (auto-allow) failed", exc_info=True)

    def list_descriptors(self) -> List[Tuple[str, GateDescriptor]]:
        out: List[Tuple[str, GateDescriptor]] = []
        for d in self._queue.list_pending():
            ctx = getattr(d, "context", None) or {}
            if ctx.get("kind") != "gate":
                continue
            out.append((d.id, GateDescriptor.from_decision_context(
                ctx, title=d.title, options=d.options, dedup=d.dedup_key)))
        return out
