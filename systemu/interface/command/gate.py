"""One gate descriptor (spec §4.3) carried by the shared layer.

Seeds subsumed (NOT redesigned):
  * ``harness_review.surface_harness_request`` context — the operator card built
    for an ESCALATED HarnessRequest.  Its options list (``_HARNESS_OPTIONS``),
    safe-default ordering ("Deny" at index 0), dedup-key format
    (``harness:<execution_id>:<request_id>``), and field shapes
    (request.request_id / request.kind.value / request.rationale;
    verdict.risk_band.value) are mirrored here verbatim.
  * the recovery ``RecoveryAction`` (systemu/recovery/engine.py) — a frozen
    dataclass with fields scope_kind / scope_id / kind / reason / fix_url /
    fix_command / severity, where severity ∈ {blocker, warning, info}.

Phase 3 builds the Inbox UI on this; Phase 2 just defines + proves it.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

# Mirrors recovery.engine.Severity = Literal["blocker", "warning", "info"].
_SEVERITY_TO_RISK = {"blocker": "high", "warning": "medium", "info": "low"}

# Mirrors harness_review._HARNESS_OPTIONS verbatim (index 0 is the safe default).
_HARNESS_OPTIONS: List[str] = ["Deny", "Approve", "Edit spec"]


class GateDescriptor(BaseModel):
    title:             str
    risk:              str = "low"
    inspect:           str = ""
    options:           List[str] = Field(default_factory=list)
    safe_default:      str = ""
    what_approve_does: str = ""
    dedup:             str = ""
    # The id of the entity the gate's executor must target (e.g. the requesting
    # tool for a dep install). Carried through the decision context so the
    # resolve branch can authorize the action against the real entity — without
    # it, dep approve_and_install always got tool_id="" (see from_dep).
    tool_id:           str = ""
    model_config = {"extra": "forbid"}

    def to_decision_context(self, *, gate_type: str) -> dict:
        """Serialize this descriptor into an OperatorDecision.context payload.
        kind="gate" marks queue rows owned by the Inbox facade so legacy
        decision rows (kind="harness_review"/...) stay distinguishable."""
        return {
            "kind":              "gate",
            "gate_type":         gate_type,
            "risk":              self.risk,
            "inspect":           self.inspect,
            "safe_default":      self.safe_default,
            "what_approve_does": self.what_approve_does,
            "tool_id":           self.tool_id,
        }

    @classmethod
    def from_decision_context(cls, ctx: dict, *, title: str = "",
                              options=None, dedup: str = "") -> "GateDescriptor":
        """Reconstruct a descriptor from a stored context (+ the decision's
        title/options/dedup_key, which live on the OperatorDecision itself)."""
        return cls(
            title=title or ctx.get("title", ""),
            risk=ctx.get("risk", "low"),
            inspect=ctx.get("inspect", ""),
            options=list(options if options is not None else ctx.get("options", [])),
            safe_default=ctx.get("safe_default", ""),
            what_approve_does=ctx.get("what_approve_does", ""),
            dedup=dedup or ctx.get("dedup", ""),
            # Back-compat: legacy dep rows stored the id as first_seen_tool_id.
            tool_id=ctx.get("tool_id") or ctx.get("first_seen_tool_id", "") or "",
        )

    @classmethod
    def from_scroll(cls, scroll, *, summary: str = "") -> "GateDescriptor":
        """Build a GateDescriptor for a PENDING_APPROVAL scroll.
        Subsumes the legacy scroll_approval Notification: same safe-default
        ordering ["Reject","Approve"], scroll-scoped dedup."""
        name = getattr(scroll, "name", "") or getattr(scroll, "id", "?")
        options = ["Reject", "Approve"]
        return cls(
            title=f"Approve scroll: {name}",
            risk="medium",
            inspect=summary,
            options=options,
            safe_default=options[0],
            what_approve_does=("Runs skill/tool extraction and creates the "
                               "activity from this scroll."),
            dedup=f"scroll:{getattr(scroll, 'id', '')}",
        )

    @classmethod
    def from_harness(cls, request, verdict, *, execution_id: str) -> "GateDescriptor":
        """Build a GateDescriptor from a HarnessRequest + Verdict.

        Subsumes ``harness_review.surface_harness_request``: same options,
        same safe-default ("Deny"), same dedup-key format, same field access.
        """
        kind_val = getattr(request.kind, "value", str(request.kind))
        risk = getattr(verdict.risk_band, "value", str(verdict.risk_band))
        req_id = getattr(request, "request_id", "") or ""
        options = list(_HARNESS_OPTIONS)
        return cls(
            title=f"Harness request: {kind_val} [{req_id}]",
            risk=risk,
            inspect=getattr(request, "rationale", "") or "",
            options=options,
            safe_default=options[0],
            what_approve_does=f"Grants the {kind_val} capability the agent requested.",
            dedup=f"harness:{execution_id}:{req_id}",
        )

    @classmethod
    def from_dep(cls, entry) -> "GateDescriptor":
        """Build a GateDescriptor from a pending dependency entry.

        ``entry`` mirrors a ``DepApprovalStore.list_pending()`` row:
        ``{"package", "first_seen_tool", "first_seen_tool_id",
        "request_count", ...}``.  Installing arbitrary packages on the floor
        is a high-risk action, so safe-default is "Dismiss".
        """
        package = entry.get("package", "?")
        tool = entry.get("first_seen_tool") or entry.get("first_seen_tool_id") or "?"
        count = entry.get("request_count", 1)
        options = ["Dismiss", "Approve & Install"]
        return cls(
            title=f"Install dependency: {package}",
            risk="high",
            inspect=f"Requested by {tool} x{count}",
            options=options,
            safe_default=options[0],
            what_approve_does=(f"pip install {package} and re-run the "
                               f"requesting tool's dry-run."),
            dedup=f"dep:{package}",
            # Carry the requesting tool id so the resolve branch can target the
            # real tool for the post-install dry-run (not best-effort "").
            tool_id=entry.get("first_seen_tool_id", "") or "",
        )

    @classmethod
    def from_command(cls, *, tool_name: str, command: str, cwd: str = "",
                     reason: str = "") -> "GateDescriptor":
        """Build a GateDescriptor for a destructive shell command (v0.9.32, D-2).

        Running an arbitrary shell command is the highest-risk action, so
        safe_default is "Deny" (index 0 — fail-closed). dedup is keyed on the
        EXACT normalized signature so "Always allow" / re-attempts collapse to
        one decision row. Options carry the three-way choice the command-gate
        handler routes on (Deny / Approve once / Always allow)."""
        from systemu.runtime.command_approvals import command_signature
        sig = command_signature(command, cwd=cwd)
        options = ["Deny", "Approve once", "Always allow"]
        inspect = f"$ {command}"
        if cwd:
            inspect += f"\n(cwd: {cwd})"
        if reason:
            inspect += f"\n\nReason: {reason}"
        return cls(
            title=f"Run command: {tool_name}",
            risk="high",
            inspect=inspect,
            options=options,
            safe_default=options[0],
            what_approve_does=(f"Runs `{command}`"
                               + (f" in {cwd}" if cwd else "")
                               + ". 'Always allow' remembers this exact command."),
            dedup=f"command:{sig}",
        )

    @classmethod
    def from_forge(cls, tool) -> "GateDescriptor":
        """Build a GateDescriptor from a PROPOSED tool record.

        ``tool`` may be EITHER the proposed-tool dict (``{"id", "name",
        "description", "status": "proposed"}``) surfaced by the Pending Tools
        card, OR a ``Tool`` model (the activity-extractor proposed-tool seam
        enqueues from the live model).  Forging runs LLM code generation, so
        this is a high-risk gate; safe-default is "Skip".  "Forge" is one of the
        Inbox Approve-labels — resolving it kicks off the two-stage spec→code
        path.
        """
        # Dual access: dict (legacy card) OR Tool model (routing seam).
        def _get(key: str, default=""):
            if isinstance(tool, dict):
                return tool.get(key, default)
            return getattr(tool, key, default)

        name = _get("name") or _get("id", "?")
        tool_id = _get("id", "")
        options = ["Skip", "Forge"]
        return cls(
            title=f"Forge tool: {name}",
            risk="high",
            inspect=(_get("description") or "").strip(),
            options=options,
            safe_default=options[0],
            what_approve_does=("Generates + reviews the tool code, then "
                               "enables it."),
            dedup=f"forge:{tool_id}",
        )

    @classmethod
    def from_blocked_tools(cls, activity, tools) -> "GateDescriptor":
        """Build a GateDescriptor for a task parked on not-ready tools (W1.2).

        The Stage-3.5 readiness gate parks a task when its required tools are
        not deployed+enabled — on a fresh install that's EVERY first task, and
        until now the park was a log line + a 'waiting_on_tools' chat status
        with no operator path forward.  This gate names the blockers and
        "Enable & run" executes Gate-3 enable on each (the heal sweep then
        re-runs the parked task automatically).

        Enabling LLM-forged tools is a security gate (Gate 3), so producers
        MUST enqueue with ``policy=None`` — never auto-executed, even under
        Bypass mode.
        """
        act_id = getattr(activity, "id", "") or ""
        act_name = getattr(activity, "name", "") or act_id
        lines = []
        for t in tools or []:
            status = getattr(getattr(t, "status", None), "value",
                             str(getattr(t, "status", "?")))
            dry = getattr(t, "dry_run_status", "not_run")
            enabled = "enabled" if getattr(t, "enabled", False) else "disabled"
            lines.append(f"{getattr(t, 'name', '?')} — {status}, {enabled}, dry-run: {dry}")
        options = ["Dismiss", "Enable & run"]
        return cls(
            title=f"Task blocked — {len(tools or [])} tool(s) not ready",
            risk="medium",
            inspect=f"Task: {act_name}\n" + "\n".join(lines),
            options=options,
            safe_default=options[0],
            what_approve_does=(
                "Runs Gate-3 enable on each blocking tool (tools that haven't "
                "passed a dry-run are reported and stay disabled). Once "
                "enabled, the heal sweep re-runs the parked task automatically."
            ),
            dedup=f"tools_blocked:{act_id}",
        )

    @classmethod
    def from_evolution(cls, proposal) -> "GateDescriptor":
        """Build a GateDescriptor from an Evolution proposal.

        Risk is mapped from the proposal's soft ``priority`` band
        (high/medium/low → high/medium/low; default medium).  ``what_approve_does``
        names the concrete change (the proposal description).
        """
        evo_type = getattr(getattr(proposal, "evolution_type", None), "value",
                            str(getattr(proposal, "evolution_type", "")))
        priority = getattr(proposal, "priority", "medium") or "medium"
        risk = {"high": "high", "medium": "medium", "low": "low"}.get(priority, "medium")
        evo_id = getattr(proposal, "id", "")
        target_type = getattr(proposal, "target_entity_type", "")
        description = getattr(proposal, "description", "") or ""
        options = ["Dismiss", "Approve & Apply"]
        return cls(
            title=f"Evolution: {evo_type} {target_type}".strip(),
            risk=risk,
            inspect=getattr(proposal, "rationale", "") or description,
            options=options,
            safe_default=options[0],
            what_approve_does=(f"Applies the {evo_type} proposal: {description}"
                               if description
                               else f"Applies the {evo_type} evolution."),
            dedup=f"evolution:{evo_id}",
        )

    @classmethod
    def from_operator(cls, *, title: str, body: str = "", options,
                      dedup: str, risk: str = "low") -> "GateDescriptor":
        """Generic render-only adapter for an operator decision.

        Used to render a free-form operator decision (notify_user /
        request_choice posts) as a gate card.  Resolving it does NOT
        re-execute anything — the waiting caller re-reads the choice via
        ``get_resolved_choice`` — so safe_default is simply ``options[0]``.
        """
        opts = list(options or [])
        return cls(
            title=title,
            risk=risk,
            inspect=body,
            options=opts,
            safe_default=opts[0] if opts else "",
            what_approve_does="",
            dedup=dedup,
        )

    @classmethod
    def from_recovery_action(cls, action) -> "GateDescriptor":
        """Build a GateDescriptor from a recovery RecoveryAction.

        Subsumes ``recovery.engine.RecoveryAction``: same scope/kind/reason
        fields, severity→risk mapping (blocker/warning/info → high/medium/low),
        and a recovery-scoped dedup key.
        """
        options = ["Dismiss", "Approve & Apply"]
        return cls(
            title=f"{action.kind} on {action.scope_kind} {action.scope_id}",
            risk=_SEVERITY_TO_RISK.get(action.severity, "low"),
            inspect=action.reason,
            options=options,
            safe_default=options[0],
            what_approve_does=(action.fix_command
                               or f"Applies the {action.kind} recovery action."),
            dedup=f"recovery:{action.scope_kind}:{action.scope_id}:{action.kind}",
        )
