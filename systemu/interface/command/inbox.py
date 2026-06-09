"""InboxQueue — the one queue (spec §4.3 / D3). A thin facade over the shipped
OperatorDecisionQueue that speaks GateDescriptor. No new store: every gate is an
OperatorDecision in the existing vault `decisions` collection, carrying the
descriptor in context (kind="gate"). The harness path already lands here via
harness_review.surface_harness_request."""
from __future__ import annotations

from typing import List, Tuple

from systemu.interface.command.gate import GateDescriptor
from systemu.interface.command.result import CommandResult, CommandStatus

_APPROVE_LABELS = {"approve", "approve & apply", "approve & install", "forge"}

# Operator gates are render-only: resolving ANY option unblocks the waiting
# run (the caller re-reads the choice via get_resolved_choice). They are
# therefore NOT gated on _APPROVE_LABELS — every resolution is a valid answer.
_RENDER_ONLY_GATES = {"operator"}


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
        return CommandResult(
            status=CommandStatus.OK,
            summary=f"Operator decision recorded ({decision.choice}); run unblocked.",
        )

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

    def list_descriptors(self) -> List[Tuple[str, GateDescriptor]]:
        out: List[Tuple[str, GateDescriptor]] = []
        for d in self._queue.list_pending():
            ctx = getattr(d, "context", None) or {}
            if ctx.get("kind") != "gate":
                continue
            out.append((d.id, GateDescriptor.from_decision_context(
                ctx, title=d.title, options=d.options, dedup=d.dedup_key)))
        return out
