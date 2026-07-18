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
_HARNESS_OPTIONS: List[str] = ["Deny", "Approve"]

# IMPL-2: the exact option label a DENY tool card offers as the remedy, AND the exact
# choice string the Inbox panel resolves with. ``decision_queue.resolve`` validates
# choice-in-options, so a one-character drift between the two would raise instead of
# resolving — hence ONE constant, imported by both surfaces. The trailing character is
# a real ellipsis (U+2026), not three periods.
RECLASSIFY_OPTION = "Reclassify effect…"


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
    # v0.9.35 (P1): MCP form-mode schema for an INPUT/elicitation gate. Empty
    # for every non-elicitation gate (back-compat). Carried so the operator
    # card can render a multi-field form and the reconciler can type-coerce.
    requested_schema:  dict = Field(default_factory=dict)
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
        v0.9.35: an INPUT request may carry a ``requested_schema`` in its spec
        (elicitation form mode) — surfaced so the card renders a multi-field form.
        """
        kind_val = getattr(request.kind, "value", str(request.kind))
        risk = getattr(verdict.risk_band, "value", str(verdict.risk_band))
        req_id = getattr(request, "request_id", "") or ""
        options = list(_HARNESS_OPTIONS)
        _spec = getattr(request, "spec", {}) or {}
        _req_schema = _spec.get("requested_schema") or {}
        # v0.9.45: a free-text ASK_OPERATOR (kind=input, no schema) gets a
        # synthesized one-field schema so the card renders an answer BOX instead
        # of generic capability buttons — the operator can type the value.
        if kind_val == "input" and not _req_schema:
            from systemu.runtime.elicitation import free_text_input_schema
            _req_schema = free_text_input_schema(_spec.get("question") or "")
        return cls(
            title=f"Harness request: {kind_val} [{req_id}]",
            risk=risk,
            inspect=getattr(request, "rationale", "") or "",
            options=options,
            safe_default=options[0],
            what_approve_does=f"Grants the {kind_val} capability the agent requested.",
            dedup=f"harness:{execution_id}:{req_id}",
            requested_schema=_req_schema if isinstance(_req_schema, dict) else {},
        )

    @classmethod
    def from_oauth_url(cls, *, server_id, authorize_url, execution_id):
        """A URL-mode OAuth handoff card (P4). Safe-default Deny; dedup per
        (execution_id, server_id) so repeated surfacing returns the same card.
        Uses ONLY the fixed GateDescriptor fields (extra=forbid) — gate_type is an
        InboxQueue.enqueue() arg, NOT a descriptor field. The operator clicks
        `inspect` (the authorize URL) to complete consent out-of-band."""
        return cls(
            title=f"Authorize MCP server: {server_id}",
            risk="high",
            inspect=authorize_url,
            options=["Deny", "Approve"],
            safe_default="Deny",
            what_approve_does="Authorizes the MCP server out-of-band.",
            dedup=f"mcp_oauth:{execution_id}:{server_id}",
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
    def from_tool(cls, *, tool_name: str, sig: str, verdict: str = "require_approval",
                  reason: str = "", effect_tags=None,
                  reclassified: bool = False,
                  assigned_class: str = "",
                  args_preview=None) -> "GateDescriptor":
        """Build a GateDescriptor for a gated forged/registry tool call (S1b, THE
        CRUX). Mirrors ``from_command``: safe_default "Deny" (index 0, fail-closed),
        dedup keyed on the EXACT tool signature (``tool:<sig>``) so re-attempts and
        "Always allow" collapse to one row. The signature ``sig`` is
        ``command_approvals.tool_signature(...)`` computed at the gate; it is stamped
        into the decision's ``context_extras`` by the caller (Task 4 reads it on
        resume — never recomputed there).

        ``verdict`` is the ``Verdict`` value (str) from ``evaluate_action``. A DENY posts
        the same card shape (safe_default Deny) but WITHOUT the standing "Always allow"
        option: IMPL-1 says an always-allow can never cover the DENY band, so offering it
        would either be a persistent bypass of the unknown-∩-high-severity floor or (once
        the recorder refuses it) a silent no-op. ``_record_gate_approval`` enforces the
        same rule independently — this is the UX half of a defence-in-depth pair.

        IMPL-2: a DENY card additionally offers ``RECLASSIFY_OPTION`` — the ONLY exit
        from the refusal band that is not "fail the task". It runs nothing by itself: the
        operator assigns the real effect class under typed confirmation, and the gate
        re-arbitrates and posts a FRESH card on that classification.

        ``reclassified`` marks that follow-up card. Its option set is exactly
        ["Deny", "Approve once"] — no standing allow may be minted under a ONE-SHOT
        classification (it would outlive the single call it was reasoned about, on a
        params-independent signature), and no second reclassify, because the operator
        has already classified this call. Risk stays "high": an effect that was refused
        once does not become routine because it now has a label.
        """
        # normalise via .value: Verdict is a str-Enum and str(member) is
        # "Verdict.DENY", which would not match and would re-offer the option.
        is_deny = str(getattr(verdict, "value", verdict) or "").strip().lower() == "deny"
        if reclassified:
            options = ["Deny", "Approve once"]
        elif is_deny:
            # NO "Approve once" (adversarial review, CRITICAL). It does NOTHING at the
            # gate by contract — every bypass in ``tool_sandbox._maybe_gate_tool`` sits
            # under ``if verdict != Verdict.DENY``. Offering an option that cannot act
            # is bad enough on a safety surface; worse, clicking it drove the recorder
            # to mint a single-use resume bridge, which became REDEEMABLE the moment a
            # reclassification lifted the verdict on those same params. The remedy is
            # the reclassify option; the approval belongs on the follow-up card, where
            # the operator can see what they are approving.
            options = ["Deny", RECLASSIFY_OPTION]
        else:
            options = ["Deny", "Approve once", "Always allow"]
        tags = ", ".join(sorted(str(t) for t in (effect_tags or []))) or "unclassified"
        inspect = f"tool: {tool_name}\neffects: {tags}\nverdict: {verdict}"
        if args_preview:
            # WHICH call this is. The tool signature is params-INDEPENDENT, so without
            # the arguments two entirely different calls render an identical card — and
            # a reclassification the operator made for one of them would look like it
            # was made for the other. Already bounded + secret-masked by the caller.
            _args = ", ".join(f"{k}={v!r}" for k, v in sorted(args_preview.items()))
            inspect += f"\nargs: {_args}"
        if reclassified:
            inspect += f"\noperator-reclassified as {assigned_class or 'unspecified'}"
        if reason:
            inspect += f"\n\nReason: {reason}"
        risk = "high" if (is_deny or reclassified) else "medium"
        if reclassified:
            what = (
                f"Runs the {tool_name!r} tool once, scored as the effect class you "
                f"assigned ({assigned_class or 'unspecified'}). That classification is "
                "single-use and cannot be remembered — the next identical call is "
                "gated again from scratch.")
        elif is_deny:
            what = (
                f"Runs the {tool_name!r} tool ({tags}). This effect could not be "
                "classified and carries a high-severity signal, so it cannot be "
                "remembered — you will be asked again every time. "
                f"'{RECLASSIFY_OPTION}' never runs anything by itself: you assign the "
                "real effect class under typed confirmation, and a fresh approval card "
                "is posted on that classification.")
        else:
            what = (
                f"Runs the {tool_name!r} tool ({tags}). 'Always allow' remembers "
                "this exact tool body + effect set + host class.")
        return cls(
            title=f"Run tool: {tool_name}",
            risk=risk,
            inspect=inspect,
            options=options,
            safe_default=options[0],
            what_approve_does=what,
            dedup=f"tool:{sig}",
        )

    @classmethod
    def from_mcp_call(cls, *, server: str, tool: str, params: dict,
                      destructive: bool = True) -> "GateDescriptor":
        """Build a GateDescriptor for an MCP tool call (v0.9.34 P0, §3.3 L3).

        Risk-tiered + scoped-trust. The card offers the four choices the
        action gate routes on: Deny / Approve once / Trust this tool for the
        session / Always allow. safe_default is "Deny" (index 0 — fail-closed).
        ``destructive`` raises the risk band to high and labels the card; the
        dedup key is mcp:<server>:<tool> so the dispatcher namespace 'mcp'
        routes the resolution and re-attempts collapse to one row."""
        from systemu.runtime.command_approvals import mcp_signature  # noqa: F401
        options = ["Deny", "Approve once",
                   "Trust this tool for the session", "Always allow"]
        try:
            import json as _json
            args_preview = _json.dumps(params or {}, default=str)[:300]
        except Exception:
            args_preview = str(params)[:300]
        inspect = f"server: {server}\ntool: {tool}\nargs: {args_preview}"
        verb = "a DESTRUCTIVE/irreversible" if destructive else "an"
        return cls(
            title=f"MCP tool call: {tool}",
            risk="high" if destructive else "medium",
            inspect=inspect,
            options=options,
            safe_default=options[0],
            what_approve_does=(
                f"Calls {verb} MCP tool {tool!r} on {server}. "
                "'Trust this tool for the session' suppresses re-prompts for "
                "this exact (server, tool) until the run ends; 'Always allow' "
                "remembers it across runs."),
            dedup=f"mcp:{server}:{tool}",
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
