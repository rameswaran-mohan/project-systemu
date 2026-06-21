"""Governor — always-on PULL authority for the Reverse-Harness (Phase 1.3).

The Governor is the single arbitration + materialisation authority for every
``HarnessRequest`` the executing agent emits via ``REQUEST_HARNESS``.

Responsibilities
----------------
arbitrate(request, context) → HarnessVerdict
    Delegates to the pure, deterministic ``harness_arbiter.arbitrate()``.
    For Phase 1 the arbiter's ESCALATE verdict is kept as-is when the arbiter
    flags ``needs_llm_judgment=True`` (LLM judgment is deferred to Phase 4).
    Every (request, verdict) pair is appended to the harness ledger.

materialise(request, verdict, *, vault, config, execution_id) → dict
    Acts **only** on ``verdict.decision == GRANT``.
    Phase 1 implements the TOOL provisioner (forge spine) only.  Later phases
    add SKILL / ACCESS / COMPUTE / SUBAGENT and the MCP runtime-connect
    provisioner (P3, ``_provision_mcp`` — connect+discover → enable → lease).
    Remaining kinds (INPUT, etc.) return a stub "not-implemented" dict and are
    never raised as exceptions.

Harness ledger
    Append-only JSONL at ``<vault_root>/harness_ledger/<execution_id>.jsonl``.
    Each line records the request, the verdict, the materialisation outcome,
    and any lease info.  Use ``ledger_path(execution_id)`` to locate it.

Leases
    In-process dict keyed by ``lease_id``.  Each lease carries
    ``{request_id, kind, execution_id, granted_at, revoked}``.
    Call ``revoke_leases(execution_id)`` at the execution's terminal state.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from systemu.core.models import (
    HarnessDecision,
    HarnessKind,
    HarnessRequest,
    HarnessVerdict,
)
from systemu.runtime import harness_arbiter
from systemu.runtime.harness_judge import judge_harness_request
from systemu.runtime.harness_policy import HarnessPolicy
from systemu.pipelines.tool_forge import forge_proposed_tools
from systemu.runtime.auto_skill_extractor import persist_skill_candidate

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mint_lease_id() -> str:
    return "lease_" + uuid.uuid4().hex[:10]


# ─────────────────────────────────────────────────────────────────────────────
#  Governor
# ─────────────────────────────────────────────────────────────────────────────

class Governor:
    """Always-on PULL authority that arbitrates and materialises HarnessRequests.

    Parameters
    ----------
    config:
        Runtime config object, dict, or None.  Forwarded to
        ``HarnessPolicy.from_config()`` — accepts whatever form the caller has.
    """

    def __init__(self, config=None) -> None:
        self.policy: HarnessPolicy = HarnessPolicy.from_config(config)
        # Keep the raw config so the LLM judge (Phase 4.1) can acquire the
        # LLM client the same way goal_verifier does.
        self.config = config
        # In-process lease registry.  key = lease_id, value = lease dict.
        self._leases: Dict[str, Dict[str, Any]] = {}
        self._lease_lock = threading.RLock()
        # v0.10.0 — the vault the active execution loop publishes so that
        # ``revoke_leases()`` (which carries no vault param) can emit lease-revoke
        # ledger events.  None until the loop sets it; when None the revoke-event
        # ledger write is a best-effort no-op (never a fallback path).
        self._active_ledger_vault = None

    # ── Public API ────────────────────────────────────────────────────────────

    def arbitrate(
        self,
        request: HarnessRequest,
        context: dict | None = None,
    ) -> HarnessVerdict:
        """Arbitrate a HarnessRequest and return a HarnessVerdict.

        Delegates to the pure ``harness_arbiter.arbitrate()``.  When the
        arbiter sets ``needs_llm_judgment=True`` (a genuinely-ambiguous
        MEDIUM-risk case) AND ``policy.llm_judge_enabled`` is True, the
        request is handed to the conservative LLM judge
        (``harness_judge.judge_harness_request``):
          * judge GRANT (confident) → GRANT verdict with a freshly minted
            lease_id + the judge's rationale;
          * judge DENY              → DENY verdict;
          * judge ESCALATE / any error / low confidence → ESCALATE kept.
        When ``policy.llm_judge_enabled`` is False the arbiter's ESCALATE
        verdict is kept unchanged (legacy behaviour).

        Every (request, verdict) is recorded to the harness ledger via the
        vault if ``_active_ledger_vault`` is set, or deferred until the
        next ``materialise()`` / explicit ``_ledger_append()`` call that
        supplies a vault.  When no vault is available the entry is queued
        in-process and written on the next opportunity.

        Notes
        -----
        The ledger write in ``arbitrate()`` uses vault=None (no ledger path
        available yet — vault is optional at arbitrate time).  Callers that
        want a ledger record at arbitrate time should call
        ``_ledger_append(entry, vault, execution_id)`` manually, or use
        ``materialise()`` which always appends.
        """
        arb_result = harness_arbiter.arbitrate(request, self.policy, context)
        verdict: HarnessVerdict = arb_result["verdict"]

        if arb_result.get("needs_llm_judgment"):
            if self.policy.llm_judge_enabled:
                verdict = self._apply_llm_judgment(request, arb_result, context)
            else:
                logger.debug(
                    "[Governor] request %s flagged needs_llm_judgment but "
                    "llm_judge_enabled=False — keeping ESCALATE verdict",
                    request.request_id,
                )

        logger.info(
            "[Governor] arbitrated request=%s kind=%s decision=%s band=%s",
            request.request_id,
            request.kind.value,
            verdict.decision.value,
            verdict.risk_band.value,
        )

        return verdict

    def _apply_llm_judgment(
        self,
        request: HarnessRequest,
        arb_result: Dict[str, Any],
        context: dict | None,
    ) -> HarnessVerdict:
        """Resolve an ambiguous MEDIUM-risk request via the LLM judge.

        Translates the judge's result into a HarnessVerdict:
          * GRANT (confident) → GRANT with a freshly minted lease_id + the
            judge's rationale (tagged ``judged_by=llm``);
          * DENY              → DENY verdict carrying the judge's rationale;
          * ESCALATE / error  → the arbiter's original ESCALATE verdict, with
            the judge's rationale appended so the operator sees why.

        Never raises — the judge itself is fail-safe (returns ESCALATE on any
        error), and we keep the arbiter's ESCALATE verdict as the floor.
        """
        original: HarnessVerdict = arb_result["verdict"]
        judged = judge_harness_request(
            request=request,
            arb_result=arb_result,
            policy=self.policy,
            context=context,
            config=self.config,
        )
        decision: HarnessDecision = judged["decision"]
        rationale: str = judged.get("rationale", "")

        logger.info(
            "[Governor] LLM judge resolved request=%s → decision=%s confidence=%.2f",
            request.request_id,
            decision.value,
            float(judged.get("confidence", 0.0)),
        )

        if decision == HarnessDecision.GRANT:
            lease_id = _mint_lease_id()
            return HarnessVerdict(
                request_id=request.request_id,
                decision=HarnessDecision.GRANT,
                risk_band=original.risk_band,
                rationale=f"[judged_by=llm] {rationale}".strip(),
                lease_id=lease_id,
                alternatives=original.alternatives,
                decided_by="llm",
            )

        if decision == HarnessDecision.DENY:
            return HarnessVerdict(
                request_id=request.request_id,
                decision=HarnessDecision.DENY,
                risk_band=original.risk_band,
                rationale=f"[judged_by=llm] {rationale}".strip(),
                lease_id=None,
                alternatives=original.alternatives,
                decided_by="llm",
            )

        # ESCALATE (or any non-grant/deny) — keep the arbiter's ESCALATE
        # verdict but record the judge's reasoning for the operator.  The judge
        # still *decided* this (it was asked and resolved to ESCALATE), so the
        # provenance is "llm".
        appended = original.rationale
        if rationale:
            appended = f"{original.rationale} [judged_by=llm: {rationale}]".strip()
        return HarnessVerdict(
            request_id=original.request_id,
            decision=original.decision,
            risk_band=original.risk_band,
            rationale=appended,
            lease_id=original.lease_id,
            alternatives=original.alternatives,
            decided_by="llm",
        )

    def materialise(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """Materialise a GRANTed HarnessRequest into a real capability.

        Only acts on ``verdict.decision == GRANT``.  Non-GRANT decisions are
        always a no-op (return ``{"materialised": False, ...}``).

        Phase 1 implements the TOOL provisioner only.  All other kinds return
        a stub "not implemented in Phase 1" dict.

        Never raises — forge failures are caught and returned as
        ``{"materialised": False, "reason": "forge failed: ..."}``.

        The outcome (request, verdict, materialisation, lease) is appended to
        the harness ledger for ``execution_id``.

        Parameters
        ----------
        request:   The original HarnessRequest.
        verdict:   The HarnessVerdict from ``arbitrate()``.
        vault:     A Vault instance (used for ledger writes and tool forge).
        config:    Runtime config (forwarded to tool_forge).
        execution_id:
            Scopes the ledger entry and any lease created.

        Returns
        -------
        dict with at least ``{"materialised": bool}``.  On success also carries
        ``{"lease_id": str, "tool": str}``.  On failure also carries
        ``{"reason": str}``.
        """
        # ── Non-GRANT: no-op ─────────────────────────────────────────────────
        if verdict.decision != HarnessDecision.GRANT:
            outcome = {
                "materialised": False,
                "reason": f"verdict is {verdict.decision.value} — not materialising",
            }
            self._ledger_append(
                self._ledger_entry(request, verdict, outcome, execution_id),
                vault=vault,
                execution_id=execution_id,
            )
            return outcome

        # ── GRANT: dispatch by kind ───────────────────────────────────────────
        outcome = self._dispatch(request, verdict, vault=vault, config=config, execution_id=execution_id)

        self._ledger_append(
            self._ledger_entry(request, verdict, outcome, execution_id),
            vault=vault,
            execution_id=execution_id,
        )

        # A GRANT that minted a capability lease gets a dedicated lease-mint
        # event so the ledger carries the full lease lifecycle (mint → revoke).
        # Best-effort: never raises (the _ledger_append guard already swallows).
        if isinstance(outcome, dict) and outcome.get("lease_id"):
            self._ledger_append(
                {
                    "ts": _utcnow_iso(),
                    "execution_id": execution_id,
                    "event_type": "lease-mint",
                    "lease_id": outcome["lease_id"],
                    "kind": request.kind.value,
                },
                vault=vault,
                execution_id=execution_id,
            )

        return outcome

    def revoke_leases(self, execution_id: str) -> int:
        """Mark all leases belonging to ``execution_id`` as revoked.

        Called by the execution engine at the terminal state (COMPLETE / FAIL).
        Returns the number of leases revoked. MCP leases ALSO unregister their
        live namespaced tools from the v2 registry (dangling-capability guard) —
        done AFTER releasing the lock, never raises.
        """
        count = 0
        revoked_lease_ids: list[str] = []
        mcp_servers_to_unregister: list[str] = []          # P3: collect under lock
        with self._lease_lock:
            for lease in self._leases.values():
                if lease["execution_id"] == execution_id and not lease["revoked"]:
                    lease["revoked"] = True
                    lease["revoked_at"] = _utcnow_iso()
                    revoked_lease_ids.append(lease["lease_id"])
                    count += 1
                    _server = lease.get("mcp_server_id")
                    if lease.get("kind") == "mcp" and _server:
                        mcp_servers_to_unregister.append(_server)
        if count:
            logger.info(
                "[Governor] revoked %d lease(s) for execution_id=%s",
                count,
                execution_id,
            )

        # Emit a lease-revoke ledger event for each revoked lease.  ``revoke_leases``
        # carries no vault param, so we use the vault the active execution loop
        # published on ``_active_ledger_vault``.  When it is None this is a
        # best-effort no-op — never raise, never write to a fallback path.
        vault = self._active_ledger_vault
        if vault is not None:
            for lease_id in revoked_lease_ids:
                self._ledger_append(
                    {
                        "ts": _utcnow_iso(),
                        "execution_id": execution_id,
                        "event_type": "lease-revoke",
                        "lease_id": lease_id,
                    },
                    vault=vault,
                    execution_id=execution_id,
                )

        # P3: unregister MCP namespaced tools AFTER the lock (registry mutation).
        for _server in mcp_servers_to_unregister:
            try:
                from systemu.runtime.mcp.sdk.registry_bridge import (
                    unregister_server_tools,
                )
                unregister_server_tools(_server)
            except Exception:
                logger.debug(
                    "[Governor] mcp unregister_server_tools failed for %s",
                    _server, exc_info=True,
                )

        return count

    def ledger_path(self, execution_id: str, vault=None) -> Path:
        """Return the JSONL ledger path for ``execution_id``.

        When ``vault`` is supplied the path is rooted under the vault's root
        directory.  Falls back to ``data/systemu/vault`` when vault is None.
        """
        root = self._vault_root(vault)
        return root / "harness_ledger" / f"{execution_id}.jsonl"

    # ── Kind dispatchers ──────────────────────────────────────────────────────

    def _dispatch(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """Route a GRANTed request to its kind-specific provisioner."""
        if request.kind == HarnessKind.TOOL:
            return self._provision_tool(request, verdict, vault=vault, config=config, execution_id=execution_id)
        if request.kind == HarnessKind.SKILL:
            return self._provision_skill(request, verdict, vault=vault, config=config, execution_id=execution_id)
        if request.kind == HarnessKind.ACCESS:
            return self._provision_access(request, verdict, vault=vault, config=config, execution_id=execution_id)
        if request.kind == HarnessKind.COMPUTE:
            return self._provision_compute(request, verdict, vault=vault, config=config, execution_id=execution_id)
        if request.kind == HarnessKind.SUBAGENT:
            return self._provision_subagent(request, verdict, vault=vault, config=config, execution_id=execution_id)
        if request.kind == HarnessKind.MCP:
            return self._provision_mcp(request, verdict, vault=vault, config=config, execution_id=execution_id)
        # Remaining kinds (INPUT, etc.) — not materialised
        return {
            "materialised": False,
            "reason": f"provisioner not implemented (kind={request.kind.value})",
        }

    def _provision_tool(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """TOOL provisioner — calls the tool_forge spine to forge/register the tool.

        Reuses ``forge_proposed_tools`` from the existing tool_forge pipeline.
        The request spec mirrors a forge spec:
          ``{name, description, tool_type, parameters_schema, return_schema,
             implementation_notes, dependencies}``

        A stub Activity and stub Scroll are synthesised so ``forge_proposed_tools``
        has enough context to run.  This is consistent with how ``forge_tool_by_name``
        and ``forge_proposed_tools_from_specs`` operate when they lack a real scroll.

        A capability lease is created and registered in the in-process lease dict.
        The lease_id is taken from the verdict if already set, or minted here.
        """
        from systemu.core.models import (
            Activity,
            ActivityStatus,
            Scroll,
            Tool,
            ToolStatus,
            ToolType,
        )
        from systemu.core.utils import generate_id

        spec = request.spec or {}
        tool_name = spec.get("name", "")

        try:
            # Build a minimal Tool record with PROPOSED status so forge_proposed_tools
            # can pick it up and generate code.
            raw_type = spec.get("tool_type", "python_function")
            try:
                tool_type = ToolType(raw_type)
            except ValueError:
                tool_type = ToolType.PYTHON_FUNCTION

            tool = Tool(
                id=generate_id("tool"),
                name=tool_name,
                description=spec.get("description", f"Tool requested at runtime: {tool_name}"),
                tool_type=tool_type,
                parameters_schema=spec.get("parameters_schema", {}),
                return_schema=spec.get("return_schema", {}),
                implementation_notes=spec.get("implementation_notes", ""),
                dependencies=spec.get("dependencies", []),
                status=ToolStatus.PROPOSED,
                forged_by_systemu=True,
                forged_by_execution_id=execution_id,
            )
            vault.save_tool(tool)

            # Build a stub scroll for context
            stub_scroll = Scroll(
                id="stub",
                name=tool_name,
                source_session_id=execution_id,
                raw_instructions_path="",
                narrative_md=spec.get("implementation_notes", request.rationale or tool.description),
            )

            # Build a stub activity linking the tool
            activity = Activity(
                id=generate_id("activity"),
                name=f"governor-harness-{tool_name}",
                scroll_id=stub_scroll.id,
                required_tool_ids=[tool.id],
                status=ActivityStatus.UNASSIGNED,
            )

            # Forge — this calls _generate_and_save_code internally
            forged = forge_proposed_tools(activity, config, vault)

        except Exception as exc:
            logger.error(
                "[Governor] forge failed for tool '%s': %s",
                tool_name,
                exc,
                exc_info=True,
            )
            return {
                "materialised": False,
                "reason": f"forge failed: {exc}",
            }

        if not forged:
            return {
                "materialised": False,
                "reason": f"forge returned no tools for '{tool_name}'",
            }

        forged_tool = forged[0]

        # Mint or reuse the lease_id from the verdict
        lease_id: str = verdict.lease_id or _mint_lease_id()
        self._register_lease(lease_id, request, execution_id)

        logger.info(
            "[Governor] materialised TOOL '%s' (id=%s) lease_id=%s execution_id=%s",
            forged_tool.name,
            forged_tool.id,
            lease_id,
            execution_id,
        )

        return {
            "materialised": True,
            "lease_id": lease_id,
            "tool": forged_tool.name,
            "tool_id": forged_tool.id,
        }

    def _provision_skill(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """SKILL provisioner — author a SKILL.md from request.spec and persist it.

        Calls ``auto_skill_extractor.persist_skill_candidate`` with the spec as
        the candidate.  Resolves skills_dir from config.skills_user_dir or a
        vault-local fallback (``<vault_root>/skills``).

        Returns
        -------
        {"materialised": True, "skill": <path>, "lease_id": ...}
        or {"materialised": False, "reason": ...}
        """
        spec = request.spec or {}
        try:
            # Resolve skills directory
            skills_dir: Optional[str] = None
            if config is not None:
                skills_dir = getattr(config, "skills_user_dir", None)
            if not skills_dir:
                vault_root = self._vault_root(vault)
                skills_dir = str(vault_root / "skills")

            # Build candidate dict from the request spec
            candidate: Dict[str, Any] = {
                "name": spec.get("name", ""),
                "description": spec.get("description", ""),
                "procedure": spec.get("procedure", []),
                "pitfalls": spec.get("pitfalls", []),
                "confidence": spec.get("confidence", 1.0),
            }

            skill_path = persist_skill_candidate(candidate, skills_dir=skills_dir)
            if not skill_path:
                return {
                    "materialised": False,
                    "reason": "persist_skill_candidate returned None (invalid candidate)",
                }

        except Exception as exc:
            logger.error(
                "[Governor] skill persist failed for '%s': %s",
                spec.get("name", "<unknown>"),
                exc,
                exc_info=True,
            )
            return {
                "materialised": False,
                "reason": f"skill persist failed: {exc}",
            }

        lease_id: str = verdict.lease_id or _mint_lease_id()
        self._register_lease(lease_id, request, execution_id)

        logger.info(
            "[Governor] materialised SKILL '%s' → %s  lease_id=%s execution_id=%s",
            spec.get("name", ""),
            skill_path,
            lease_id,
            execution_id,
        )

        return {
            "materialised": True,
            "skill": skill_path,
            "lease_id": lease_id,
        }

    def _provision_access(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """ACCESS provisioner — issue a scoped capability lease for a resource.

        Single-owner posture (by design): record the advisory lease and return
        the access spec only.  Does NOT open any network or filesystem resource
        and does NOT emit a sandbox-policy patch — there is no sandbox boundary
        to apply one to, and nothing in the loop consumed the old ``apply`` patch
        (Bug 5 / D.2: dead plumbing removed).

        Returns
        -------
        {"materialised": True, "lease_id": ..., "access": <spec>}
        or {"materialised": False, "reason": ...}
        """
        spec = request.spec or {}
        try:
            # Single-owner backend: nothing to materialise beyond the advisory
            # lease record. No sandbox-policy patch is built (Bug 5 / D.2).
            pass
        except Exception as exc:
            logger.error(
                "[Governor] access provision failed: %s", exc, exc_info=True,
            )
            return {
                "materialised": False,
                "reason": f"access provision failed: {exc}",
            }

        lease_id: str = verdict.lease_id or _mint_lease_id()
        self._register_lease(lease_id, request, execution_id)

        logger.info(
            "[Governor] recorded advisory ACCESS lease_id=%s execution_id=%s",
            lease_id,
            execution_id,
        )

        return {
            "materialised": True,
            "lease_id": lease_id,
            "access": dict(spec),
        }

    def _provision_compute(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """COMPUTE provisioner — return a budget grant the loop applies.

        Clamps requested values against ``HarnessPolicy.max_compute_ceiling``
        (for budget_fraction) and sensible per-field caps.

        Returns
        -------
        {"materialised": True, "compute_grant": {"extra_iterations": N, "extra_think": M}, "lease_id": ...}
        or {"materialised": False, "reason": ...}
        """
        spec = request.spec or {}
        try:
            ceiling: float = self.policy.max_compute_ceiling

            # extra_iterations: integer, capped at ceiling * 100 (reasonable upper bound)
            raw_iterations = spec.get("extra_iterations", 0)
            try:
                raw_iterations = int(raw_iterations)
            except (TypeError, ValueError):
                raw_iterations = 0
            max_iterations = max(1, int(ceiling * 100))
            extra_iterations = max(0, min(raw_iterations, max_iterations))

            # extra_think: integer tokens, capped at ceiling * 32000
            raw_think = spec.get("extra_think", 0)
            try:
                raw_think = int(raw_think)
            except (TypeError, ValueError):
                raw_think = 0
            max_think = max(0, int(ceiling * 32_000))
            extra_think = max(0, min(raw_think, max_think))

            compute_grant = {
                "extra_iterations": extra_iterations,
                "extra_think": extra_think,
            }

        except Exception as exc:
            logger.error(
                "[Governor] compute provision failed: %s", exc, exc_info=True,
            )
            return {
                "materialised": False,
                "reason": f"compute provision failed: {exc}",
            }

        lease_id: str = verdict.lease_id or _mint_lease_id()
        self._register_lease(lease_id, request, execution_id)

        logger.info(
            "[Governor] materialised COMPUTE grant=%s lease_id=%s execution_id=%s",
            compute_grant,
            lease_id,
            execution_id,
        )

        return {
            "materialised": True,
            "compute_grant": compute_grant,
            "lease_id": lease_id,
        }

    def _provision_subagent(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """SUBAGENT provisioner — return a spawn directive the loop executes.

        Does NOT spawn the subagent here (materialise is sync + pure-ish).
        Returns a directive dict the async loop hands to ``spawn_subagent``.
        Parameter names match ``delegate.spawn_subagent`` so the loop can
        call it directly:
          spawn_subagent(task=..., config=..., parent_depth=..., max_turns=...)

        Depth and budget fraction are clamped to policy limits.

        Returns
        -------
        {"materialised": True, "subagent": {"task": ..., "depth_cap": ...,
         "budget_fraction": ...}, "lease_id": ...}
        or {"materialised": False, "reason": ...}
        """
        spec = request.spec or {}
        try:
            max_depth: int = self.policy.max_subagent_depth
            max_budget: float = self.policy.max_subagent_budget_fraction

            task = spec.get("task", request.rationale or "")

            # depth_cap: requested depth capped at policy max
            raw_depth = spec.get("depth", 1)
            try:
                raw_depth = int(raw_depth)
            except (TypeError, ValueError):
                raw_depth = 1
            depth_cap = max(1, min(raw_depth, max_depth))

            # budget_fraction: requested fraction capped at policy max
            raw_budget = spec.get("budget_fraction", max_budget)
            try:
                raw_budget = float(raw_budget)
            except (TypeError, ValueError):
                raw_budget = max_budget
            budget_fraction = max(0.0, min(raw_budget, max_budget))

            if not task:
                return {
                    "materialised": False,
                    "reason": "subagent request missing 'task' in spec",
                }

        except Exception as exc:
            logger.error(
                "[Governor] subagent provision failed: %s", exc, exc_info=True,
            )
            return {
                "materialised": False,
                "reason": f"subagent provision failed: {exc}",
            }

        lease_id: str = verdict.lease_id or _mint_lease_id()
        self._register_lease(lease_id, request, execution_id)

        logger.info(
            "[Governor] materialised SUBAGENT task=%r depth_cap=%d budget=%.2f "
            "lease_id=%s execution_id=%s",
            task[:80],
            depth_cap,
            budget_fraction,
            lease_id,
            execution_id,
        )

        return {
            "materialised": True,
            "subagent": {
                "task": task,
                "depth_cap": depth_cap,
                "budget_fraction": budget_fraction,
            },
            "lease_id": lease_id,
        }

    def _provision_mcp(
        self,
        request: HarnessRequest,
        verdict: HarnessVerdict,
        *,
        vault,
        config,
        execution_id: str,
    ) -> dict:
        """MCP provisioner (P3) — connect+discover (one seam) → hash-pin +
        persist + enable → mint lease. Forge-style: NEVER raises; every failure
        is a ``{"materialised": False, "reason": ...}`` dict.

        Reuses the PINNED P2 layer (lazy-imported — no import-time connect,
        v0.9.33 lesson): the SINGLE seam
        ``ConnectionManager().connect_and_discover_sync(server_id, spec)``
        (connect + DNS/SSRF/TLS precheck + discover in one call),
        ``schema_map.tool_def_hash`` (the ONLY def-hash) +
        ``connections.set_tool_hash`` (rug-pull pin), and
        ``connections.set_tool_enabled`` / ``set_server_meta``. Honours
        ``spec["tool_filter"]`` (per-tool opt-in) and the P2 exposure budget
        (enforced inside ``connections.set_tool_enabled``).

        Returns
        -------
        success   {"materialised": True, "lease_id": ...,
                   "mcp": {"server_id","label","transport",
                           "tools": [FULL dicts {name,description,
                                     parameters_schema,annotations}]}}
        oauth     {"materialised": False, "reason": "oauth_pending",
                   "authorize_url": ...}   (P4 wires the URL-mode handoff)
        failure   {"materialised": False, "reason": ...}
        """
        spec = request.spec or {}
        server_id = str(spec.get("server_id", "") or "")
        transport = str(spec.get("transport", "") or "").lower()
        label = str(spec.get("label", "") or server_id)
        tool_filter = spec.get("tool_filter")
        try:
            import os
            from systemu.runtime.mcp.sdk.manager import ConnectionManager
            from systemu.runtime.mcp.sdk.schema_map import (
                tool_def_hash, sanitize_description,
            )
            from systemu.runtime.mcp import connections as _connections

            # Build the connect spec from the request (the seam's contract:
            # {transport, command, args, env, url}). env_keys names the parent
            # env vars the child may inherit — forwarded as `env` per P2.
            connect_spec = {
                "transport": transport,
                "command": spec.get("command"),
                "args": spec.get("args"),
                # v0.9.37 (review HIGH): resolve approved env-var NAMES to their
                # current values for the stdio child. Passing the name LIST as the
                # env dict left credentialed stdio MCP servers unable to connect.
                "env": {k: os.environ[k] for k in (spec.get("env_keys") or [])
                        if k in os.environ},
                "url": spec.get("url"),
            }

            # ── The ONE pinned seam: connect + DNS/SSRF/TLS precheck + discover.
            # Thread the operator allowlist + TLS policy (review HIGH/MEDIUM): the
            # runtime-connect path now actually enforces mcp_require_tls and honours
            # allowed_mcp_hosts (an allow-listed private host can connect; a
            # plaintext remote host is rejected).
            cm = ConnectionManager()
            result = cm.connect_and_discover_sync(
                server_id, connect_spec,
                allowed_hosts=set(getattr(self.policy, "allowed_mcp_hosts", None) or set()),
                require_tls=bool(getattr(self.policy, "mcp_require_tls", True)),
            ) or {}

            # OAuth handoff — surfaced honestly; P4 owns the URL-mode resolve.
            if result.get("oauth_required"):
                return {
                    "materialised": False,
                    "reason": "oauth_pending",
                    "authorize_url": result.get("authorize_url"),
                }
            if not result.get("connected"):
                # DNS-resolution SSRF returns error="ssrf_blocked" here (P2).
                return {
                    "materialised": False,
                    "reason": f"mcp connect failed: {result.get('error') or 'unknown'}",
                }

            # `tools` are normalised dicts {name, description,
            # parameters_schema, annotations}. Hash-pin (rug-pull defence) +
            # enable per filter/budget; carry the FULL dicts through (B5) so
            # register_server_tools downstream gets schema + action tier.
            discovered = result.get("tools") or []
            wanted = set(tool_filter) if tool_filter else None
            granted_tools: list = []
            for t in discovered:
                name = str(t.get("name") or "")
                if not name:
                    continue
                if wanted is not None and name not in wanted:
                    continue
                # v0.9.37 (review BLOCKER+HIGH): sanitize the server-supplied
                # description to the SAME canonical form the use-time rug-pull
                # re-check (mcp_list_tools) uses — otherwise the pin-hash (raw)
                # never matches the re-check (sanitized) and EVERY connected tool
                # auto-disables on its first call. Also keeps unsanitized/poisoned
                # descriptions out of the catalog (tool-poisoning defence).
                desc = sanitize_description(str(t.get("description") or ""))
                pschema = dict(t.get("parameters_schema") or {})
                annotations = dict(t.get("annotations") or {})
                # Pin the canonical def-hash so P2's use-time re-hash detects
                # drift (rug-pull → fail-closed disable + re-prompt).
                h = tool_def_hash(name=name, description=desc, input_schema=pschema)
                _connections.set_tool_hash(vault, server_id, name, h)
                _connections.set_tool_enabled(
                    vault, server_id, name, True,
                    description=desc, schema=pschema, annotations=annotations,
                )
                granted_tools.append({
                    "name": name,
                    "description": desc,
                    "parameters_schema": pschema,
                    "annotations": annotations,
                })

            # Persist server transport + label + connected flag (re-attach LOW).
            _connections.set_server_meta(
                vault, server_id, label=label, transport=transport, connected=True,
            )

        except Exception as exc:
            logger.error(
                "[Governor] mcp provision failed for '%s': %s",
                server_id, exc, exc_info=True,
            )
            return {
                "materialised": False,
                "reason": f"mcp provision failed: {exc}",
            }

        lease_id: str = verdict.lease_id or _mint_lease_id()
        self._register_lease(lease_id, request, execution_id)

        logger.info(
            "[Governor] materialised MCP server '%s' (%s) tools=%s lease_id=%s exec=%s",
            server_id, transport, [t["name"] for t in granted_tools],
            lease_id, execution_id,
        )

        return {
            "materialised": True,
            "lease_id": lease_id,
            "mcp": {
                "server_id": server_id,
                "label": label,
                "transport": transport,
                # B5: FULL tool dicts (not bare names) so the grant-apply path
                # can call register_server_tools(vault, server, tool_dicts).
                "tools": granted_tools,
            },
        }

    # ── Lease registry ────────────────────────────────────────────────────────

    def _register_lease(
        self,
        lease_id: str,
        request: HarnessRequest,
        execution_id: str,
    ) -> Dict[str, Any]:
        lease = {
            "lease_id": lease_id,
            "request_id": request.request_id,
            "kind": request.kind.value,
            "execution_id": execution_id,
            "granted_at": _utcnow_iso(),
            "revoked": False,
            "revoked_at": None,
            # P3: MCP leases carry the server_id so revoke can unregister the
            # live namespaced tools; all other kinds leave it None.
            "mcp_server_id": (
                str((request.spec or {}).get("server_id", "") or "")
                if request.kind == HarnessKind.MCP else None
            ),
        }
        with self._lease_lock:
            self._leases[lease_id] = lease
        return lease

    def get_lease(self, lease_id: str) -> Optional[Dict[str, Any]]:
        """Return the lease dict for ``lease_id``, or None if unknown."""
        return self._leases.get(lease_id)

    def list_leases(self, execution_id: str | None = None) -> list:
        """Return all leases, optionally filtered by execution_id."""
        with self._lease_lock:
            leases = list(self._leases.values())
        if execution_id is not None:
            leases = [l for l in leases if l["execution_id"] == execution_id]
        return leases

    # ── Ledger ────────────────────────────────────────────────────────────────

    @staticmethod
    def _vault_root(vault) -> Path:
        """Extract the vault root Path from a Vault instance or fall back."""
        if vault is not None:
            root_attr = getattr(vault, "root", None)
            if root_attr:
                return Path(root_attr)
        return Path("data") / "systemu" / "vault"

    def ledger_path(self, execution_id: str, vault=None) -> Path:  # noqa: F811
        root = self._vault_root(vault)
        return root / "harness_ledger" / f"{execution_id}.jsonl"

    @staticmethod
    def _ledger_entry(
        request: HarnessRequest,
        verdict: HarnessVerdict,
        outcome: dict,
        execution_id: str,
    ) -> dict:
        return {
            "ts": _utcnow_iso(),
            "execution_id": execution_id,
            "request": {
                "request_id": request.request_id,
                "kind": request.kind.value,
                "spec": request.spec,
                "rationale": request.rationale,
                "urgency": request.urgency,
                "blocking": request.blocking,
                # v0.10.0 pull-decision instrumentation — carried so reconciliation
                # + the CGB extractor can classify the pull-decision failure mode.
                "attempts_before": getattr(request, "attempts_before_request", 0),
                "confidence": getattr(request, "confidence", None),
            },
            "verdict": {
                "decision": verdict.decision.value,
                "risk_band": verdict.risk_band.value,
                "rationale": verdict.rationale,
                "lease_id": verdict.lease_id,
                "decided_by": verdict.decided_by,
            },
            "outcome": outcome,
        }

    def _ledger_append(
        self,
        entry: dict,
        vault,
        execution_id: str,
    ) -> None:
        """Append ``entry`` to the JSONL ledger for ``execution_id``.

        Uses the vault root to determine the ledger directory.  Creates the
        directory and file if they don't exist.  Failures are logged but never
        raised — a ledger write failure must never abort the calling operation.
        """
        try:
            ledger = self.ledger_path(execution_id, vault)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.error(
                "[Governor] ledger write failed for execution_id=%s: %s",
                execution_id,
                exc,
            )

    # ── Terminal-pass request-outcome reconciliation (v0.10.0 pull-decision) ────

    @staticmethod
    def reconcile_outcomes(ledger_rows, used_tool_names, run_success: bool = True) -> list:
        """Pure: derive per-request ``request-outcome`` events from a run's ledger.

        Skips non-request event rows (lease-mint / lease-revoke / request-outcome).
        For each arbitrated request:
          * GRANT     → ``granted_used`` if its materialised tool was actually called
                        in the run, else ``granted_unused``;
          * DENY      → ``denied_fallback_ok`` / ``denied_fallback_failed`` by run success;
          * ESCALATE  → ``escalate_unresolved`` (autonomous run, no operator decision here).
        """
        used = set(used_tool_names or ())
        events: list = []
        for row in (ledger_rows or ()):
            if not isinstance(row, dict) or row.get("event_type"):
                continue
            req = row.get("request") or {}
            verd = row.get("verdict") or {}
            rid = req.get("request_id")
            decision = str(verd.get("decision") or "").lower()
            if not rid or not decision:
                continue
            if decision == "grant":
                tool = (row.get("outcome") or {}).get("tool")
                used_after = bool(tool and tool in used)
                outcome = "granted_used" if used_after else "granted_unused"
            elif decision == "deny":
                used_after = None
                outcome = "denied_fallback_ok" if run_success else "denied_fallback_failed"
            else:
                used_after = None
                outcome = "escalate_unresolved"
            # v0.10.0 Task 1.7 — pull-failure taxonomy (premature / wasted / unused).
            try:
                from systemu.runtime.failure_classifier import classify_pull_failure
                category = classify_pull_failure(
                    attempts_before=int(req.get("attempts_before", 0) or 0),
                    decision=decision,
                    fallback_ok=(run_success if decision == "deny" else None),
                    used_after_grant=used_after,
                )
            except Exception:
                category = "unknown"
            events.append({
                "ts": _utcnow_iso(),
                "execution_id": row.get("execution_id"),
                "event_type": "request-outcome",
                "request_id": rid,
                "outcome": outcome,
                "pull_failure_category": category,
            })
        return events

    def _read_ledger_rows(self, execution_id: str, vault) -> list:
        rows: list = []
        try:
            p = self.ledger_path(execution_id, vault)
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
        return rows

    def write_outcome_reconciliation(
        self, execution_id: str, used_tool_names, *, run_success: bool = True, vault=None,
    ) -> int:
        """Terminal-pass: read this run's ledger and append a ``request-outcome``
        event per arbitrated request. Best-effort; never raises. Returns the count."""
        v = vault if vault is not None else getattr(self, "_active_ledger_vault", None)
        try:
            rows = self._read_ledger_rows(execution_id, v)
            events = self.reconcile_outcomes(rows, used_tool_names, run_success)
            for ev in events:
                self._ledger_append(ev, v, execution_id)
            return len(events)
        except Exception:
            logger.debug("[Governor] outcome reconciliation failed", exc_info=True)
            return 0
