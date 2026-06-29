"""ShadowRuntime — the Claude Code-inspired agentic execution loop.

Orchestrates a Shadow running through its assigned Scroll:

  1. Boot with lightweight skeleton context (Progressive Loading)
  2. Enter the ReAct loop (Reason → Act → Observe → repeat)
  3. At each ActionBlock boundary → trigger Tier 3 snapshot compaction
  4. Safety gate → destructive calls require user approval
  5. Exit on COMPLETE / FAIL / max_iterations

Architecture:
  - LLM calls: Tier 2 (structured reasoning / execution decisions)
  - Snapshot compaction: Tier 3 (fast summarisation)
  - Tool execution: ToolSandbox (subprocess isolation)
  - Context management: ExecutionContext
"""

from __future__ import annotations

import datetime as _datetime_module
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import Activity, Shadow, Skill, Tool, ToolStatus
from systemu.core.utils import load_prompt, utcnow
from systemu.interface.notifications import confirm, notify_user, log_event
from systemu.runtime.context_builder import ExecutionContext
from systemu.runtime.tool_sandbox import ToolResult, ToolSandbox
from systemu.vault.vault import Vault

# v0.6.1-c: Stage 3.5 cure path — decay + recalibrate skill loaded during
# failed executions.  These imports are top-level (not lazy) so tests can
# monkey-patch them at module level.
from systemu.pipelines.skill_recalibrator import (
    apply_recalibration,
    decay_effectiveness,
    is_low_risk_skill_recalibration,
    recalibrate_skill,
)

# ─────────────────────────────────────────────────────────────────────────
#  v0.9.1 (Layer 4) — Durable-outcome verifier hook surface
# ─────────────────────────────────────────────────────────────────────────
from dataclasses import dataclass, field as _dataclass_field

from systemu.runtime import objective_verifier, state_delta
from systemu.runtime.loop_guard import LoopGuard

# P4 / H9 — sampling gate rail. Imported at module scope (NOT lazily inside the
# helper) so the gate rail + descriptor are module attributes the production
# wiring resolves and tests can monkeypatch.
from systemu.interface.command.inbox import InboxQueue
from systemu.interface.command.gate import GateDescriptor


@dataclass
class ObjectiveState:
    """Per-objective verifier bookkeeping carried across iterations."""
    rejection_count: int = 0
    calls_this_turn: int = 0
    baseline: Optional[object] = None  # state_delta._Baseline


@dataclass
class CompletionOutcome:
    """Result of one process_completion_claim call."""
    credited: bool
    state: ObjectiveState
    feedback_message: Optional[str] = None
    escalate_stuck: bool = False
    bypassed_verifier: bool = False


# ─────────────────────────────────────────────────────────────────────────
#  P4 / H9 — MCP sampling on the REAL gate rail + BYPASS floor
#
#  The pure routing core (sdk/sampling.route_sampling_request) is policy-free so
#  it stays reusable (web_act). PRODUCTION sampling rides the SAME
#  InboxQueue.enqueue(..., gate_type="sampling", policy=…) rail every other
#  operator gate uses — `sampling` is on the BYPASS floor (so BYPASS still asks),
#  the production on_gate defaults to ASK (never silent allow), a deny is
#  fail-closed (the model is never invoked), any "Trust for session" grant is
#  scoped per (server_id, session_id), and every call writes a per-call ledger
#  entry that carries NO prompt text / no secret.
# ─────────────────────────────────────────────────────────────────────────


def _has_session_sampling_trust(vault, server_id: str, session_id: str) -> bool:
    """True iff a prior 'Trust for session' grant covers sampling for this
    (server_id, session_id). Mirrors the MCP action-gate session-trust check
    (command_approvals). Fail-closed: ANY failure to resolve the store ⇒ no
    trust ⇒ the gate is posted."""
    try:
        from systemu.runtime.command_approvals import (
            get_default_store, mcp_session_key)
        store = get_default_store()
        if store is None:
            return False
        skey = mcp_session_key(f"sampling:{server_id}", "createMessage", session_id)
        return bool(store.is_session_trusted(skey))
    except Exception:
        logger.debug("[Sampling] session-trust check failed; will gate "
                     "(fail-closed)", exc_info=True)
        return False


def _resolve_sampling_gate(decision_id, *, vault=None, server_id="",
                           session_id="", dedup="") -> bool:
    """Resolve a posted 'sampling' gate decision to an approve/deny outcome.

    Mirrors how the MCP action gate consumes a resolved decision
    (OperatorDecisionQueue.consume_resolved_choice keyed on the gate dedup — the
    operator chooses Deny / Approve once / Trust for session). Returns True iff
    approved. A 'Trust for session' choice records a per-(server,session) trust
    grant via the SAME command_approvals store the MCP gate uses, so subsequent
    calls in the same run skip the prompt. Fail-closed: any error ⇒ deny."""
    key = dedup or f"sampling:{session_id}:{server_id}"
    try:
        from systemu.approval.decision_queue import OperatorDecisionQueue
        choice = OperatorDecisionQueue(vault).consume_resolved_choice(key)
    except Exception:
        logger.debug("[Sampling] gate resolution failed; deny (fail-closed)",
                     exc_info=True)
        return False
    norm = str(choice or "").strip().lower()
    if norm in {"deny", "reject", "skip", ""}:
        return False
    if norm in {"trust for session", "trust this server for the session",
                "trust", "always allow"}:
        try:
            from systemu.runtime.command_approvals import (
                get_default_store, mcp_session_key)
            store = get_default_store()
            if store is not None:
                skey = mcp_session_key(f"sampling:{server_id}", "createMessage",
                                       session_id)
                store.trust_session(skey, server=f"sampling:{server_id}",
                                    tool="createMessage", session_id=session_id)
        except Exception:
            logger.debug("[Sampling] could not persist session trust",
                         exc_info=True)
    return True


def _build_sampling_on_gate(*, server_id, session_id, vault, policy, ledger):
    """Return the operator-gate hook passed into route_sampling_request in
    PRODUCTION. It posts a deniable 'sampling' gate through the REAL Inbox rail
    (gate_type='sampling', which is on the BYPASS floor), defaults to ASK (never
    silent allow), scopes any 'Trust for session' grant to (server_id,
    session_id), and writes a per-call ledger entry. Returns True iff approved."""

    def _on_gate(summary):  # summary is route_sampling_request's redacted dict
        # 1) check a per-server/session standing trust grant (set by a prior
        #    'Trust for session' resolution) BEFORE posting again.
        if _has_session_sampling_trust(vault, server_id, session_id):
            allowed = True
        else:
            dedup = f"sampling:{session_id}:{server_id}"
            descriptor = GateDescriptor(
                title=f"MCP server wants an LLM completion: {server_id}",
                risk="medium",
                inspect=(f"server={server_id} session={session_id} "
                         f"messages={summary.get('message_count')} "
                         f"max_tokens={summary.get('max_tokens')} "
                         f"tier={summary.get('tier')}"),
                options=["Deny", "Approve once", "Trust for session"],
                safe_default="Deny",
                what_approve_does=("Routes ONE sampling/createMessage through "
                                   "systemu's own model. No api key reaches the server."),
                dedup=dedup,
            )
            decision_id = InboxQueue(vault).enqueue(
                descriptor,
                gate_type="sampling",      # ON THE FLOOR — BYPASS still asks
                body="",                   # NO prompt text on the card (redacted)
                policy=policy,             # consults the dial; floor forces 'ask'
                context_extras={"server_id": server_id, "session_id": session_id,
                                "kind": "gate"},
            )
            allowed = _resolve_sampling_gate(decision_id, vault=vault,
                                             server_id=server_id,
                                             session_id=session_id, dedup=dedup)
        # 2) per-call ledger entry — auditable, secret-free.
        ledger.append({
            "server_id": server_id, "session_id": session_id,
            "allowed": bool(allowed),
            "message_count": summary.get("message_count"),
            "max_tokens": summary.get("max_tokens"),
            "tier": summary.get("tier"),
        })
        return bool(allowed)

    return _on_gate


def build_sampling_callback(manager, *, server_id, session_id, vault, config,
                            policy, tier=2, ledger=None):
    """Inject the gate-backed sampling callback into the manager's
    set_sampling_callback slot (the slot exists from P2, left None). Uses
    transports.make_sampling_callback for the SDK<->dict adapter, but supplies a
    gate-backed on_gate so production NEVER routes with on_gate=None."""
    from systemu.runtime.mcp.sdk import transports
    ledger = ledger if ledger is not None else []
    on_gate = _build_sampling_on_gate(server_id=server_id, session_id=session_id,
                                      vault=vault, policy=policy, ledger=ledger)
    cb = transports.make_sampling_callback(config=config, tier=tier, on_gate=on_gate)
    manager.set_sampling_callback(cb)
    return cb


def process_completion_claim(
    *,
    objective,
    vault,
    config,
    execution_id: str,
    default_output_dir: str,
    chat_result: Optional[str],
    state: ObjectiveState,
    fresh_work_since_last_call: bool = True,
    user_id: Optional[str] = None,
    extensions: Optional[dict] = None,
) -> CompletionOutcome:
    """Judge one completion claim. Returns the credit decision + updated state.

    - If state.calls_this_turn >= config.verifier_per_turn_cap AND no fresh
      effectful work landed → bypass verifier (claim cannot be re-judged this
      turn; runtime should keep iterating). Returns ``bypassed_verifier=True``.
    - Otherwise calls the verifier and credits/rejects accordingly.
    - On reject: increments rejection_count, returns feedback_message, and if
      rejection_count >= config.verifier_rejection_budget sets escalate_stuck.
    """
    cap = int(getattr(config, "verifier_per_turn_cap", 2))
    if state.calls_this_turn >= cap and not fresh_work_since_last_call:
        return CompletionOutcome(
            credited=False, state=state, bypassed_verifier=True,
            feedback_message=(
                "Verifier per-turn cap reached without fresh effectful work. "
                "Produce new durable evidence (write the file, send the action) "
                "before claiming completion again."
            ),
        )

    # Build the state delta against this objective's baseline.
    baseline = state.baseline or state_delta.capture_baseline(
        vault=vault, execution_id=execution_id,
        objective_id=objective.id, default_output_dir=default_output_dir,
    )
    delta = state_delta.compute_delta(
        baseline=baseline, vault=vault, default_output_dir=default_output_dir,
        chat_result=chat_result, config=config,
        execution_id=execution_id, user_id=user_id,
        extensions=extensions or {},
    )

    verdict = objective_verifier.run(objective=objective, delta=delta, config=config)
    state.calls_this_turn += 1

    if verdict["verified"]:
        # Reset rejection counter on success.
        state.rejection_count = 0
        return CompletionOutcome(credited=True, state=state)

    state.rejection_count += 1
    feedback = (
        f"Objective {objective.id} claim REJECTED. Verifier said: "
        f"{verdict['reason']}. Produce the declared evidence before claiming "
        f"completion again."
    )
    budget = int(getattr(config, "verifier_rejection_budget", 3))
    escalate = state.rejection_count >= budget
    return CompletionOutcome(
        credited=False, state=state,
        feedback_message=feedback, escalate_stuck=escalate,
    )


def recredit_on_resume(
    *,
    objective,
    vault,
    config,
    execution_id: str,
    default_output_dir: str,
    chat_result: Optional[str] = None,
    user_id: Optional[str] = None,
) -> CompletionOutcome:
    """Resume hook: judge an uncredited objective against current durable state.

    Baseline is the unix epoch — we want EVERYTHING currently present to count
    as evidence. If the verifier passes, the objective is re-credited without
    re-running its tool path.
    """
    baseline = state_delta._Baseline(iteration_start_ts="1970-01-01T00:00:00Z")
    delta = state_delta.compute_delta(
        baseline=baseline, vault=vault, default_output_dir=default_output_dir,
        chat_result=chat_result, config=config,
        execution_id=execution_id, user_id=user_id,
    )
    verdict = objective_verifier.run(objective=objective, delta=delta, config=config)
    if verdict["verified"]:
        return CompletionOutcome(credited=True, state=ObjectiveState())
    return CompletionOutcome(
        credited=False, state=ObjectiveState(rejection_count=0),
        feedback_message=verdict["reason"],
    )


# ─────────────────────────────────────────────────────────────────────────


def _resolve_verifier_output_dir(config, user_profile) -> str:
    """v0.9.1.1 hotfix: precedence for the verifier's default_output_dir.

    1. user_profile.default_output_dir (if non-empty)  — user's explicit choice
    2. config.output_dir (if non-empty)                — env-var SYSTEMU_OUTPUT_DIR
    3. {vault_dir}/outputs                             — last-resort default

    Without this, a user who runs `sharing_on user init` with a non-default
    output dir but no SYSTEMU_OUTPUT_DIR env var would see the verifier check
    ~/Documents while the LLM wrote files to the profile path → false rejection.
    """
    if user_profile is not None:
        prof_dir = getattr(user_profile, "default_output_dir", None) or ""
        if prof_dir:
            return prof_dir
    cfg_dir = getattr(config, "output_dir", None) or ""
    if cfg_dir:
        return cfg_dir
    vault_dir = getattr(config, "vault_dir", ".")
    return str(Path(vault_dir) / "outputs")


def _intent_engine_enabled(config) -> bool:
    """v0.9.7: master flag for the intent-driven engine behaviours.

    Phase 4.4 (graduated): the intent engine is now **default ON**. When on,
    COMPLETE is accepted on GOAL-level verification even if some refiner-baked
    per-objective criteria weren't individually credited; REQUEST_HARNESS /
    ASK_OPERATOR provisioning, adherence resolution, and the LLM judge are
    active. Set ``SYSTEMU_INTENT_ENGINE=false`` (or ``config.intent_engine_enabled
    = False``) to fall back to the legacy per-objective engine.
    """
    if hasattr(config, "intent_engine_enabled"):
        return bool(config.intent_engine_enabled)
    return os.getenv("SYSTEMU_INTENT_ENGINE", "true").lower() == "true"


def _next_harness_request_no(prev) -> int:
    """v0.9.33 Bug 2: monotonic per-execution harness-request counter.

    Coerces any prior value (None / garbage / negative) to a safe floor so a
    corrupted resume count can never crash the loop. The first request is #1.
    """
    try:
        n = int(prev)
    except (TypeError, ValueError):
        n = 0
    return n + 1 if n >= 0 else 1


# v0.9.33 Bug 3: the v2 (code-registered) delegation tools that, post Section A,
# became dispatchable through the loop. A CHILD runtime (depth>=1) must never be
# able to recurse through these — that is a SECOND delegation path alongside the
# native REQUEST_HARNESS kind=subagent fleet, and its handler ignores the threaded
# child config (it reads Config.from_env()). We refuse them for children here.
_V2_DELEGATION_TOOL_NAMES = frozenset({
    "spawn_subagent", "delegate", "mixture_of_agents",
})


def _harness_arbitration_context(pre_inc_count: int, subagent_depth: int) -> dict:
    """v0.9.33 Bug 2/3: build the arbitration ``context`` the loop threads into
    ``Governor.arbitrate``.

    ``pre_inc_count`` is the per-run harness-request counter value BEFORE this
    request was counted — so the arbiter's cap (count == max → cap) fires at
    exactly ``max_requests_per_run`` requests, not one early. ``subagent_depth``
    is this runtime's actual nesting (0 for a parent) so the SUBAGENT depth guard
    sees real nesting rather than trusting model-claimed ``spec.depth``.
    """
    return {
        "requests_this_run": int(pre_inc_count),
        "subagent_depth": int(subagent_depth),
    }


def _runtime_depth_from_config(config) -> int:
    """v0.9.33 Bug 3: read a runtime's subagent nesting depth off its config.

    A parent runtime's config has no ``_subagent_depth`` → 0. SubagentFleet
    stamps a child config with an incremented depth (see
    ``SubagentFleet._build_child_config``) so the arbiter's depth guard
    (``harness_arbiter._arbitrate_subagent``) sees REAL nesting. Pure and
    crash-proof: any missing / garbage value floors to 0.
    """
    try:
        return int(getattr(config, "_subagent_depth", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _intent_goal_success(*, vault, config, user_profile, scroll, execution_id,
                         summary=None) -> bool:
    """v0.9.7: goal-level acceptance from CURRENT durable evidence.

    Uses an epoch baseline so EVERYTHING present counts (sidesteps per-objective
    state-delta baseline/timing fragility). Returns True iff the goal verifier
    judges the goal met. Never raises.
    """
    try:
        from systemu.runtime import goal_verifier as _gv
        _gbaseline = state_delta._Baseline(iteration_start_ts="1970-01-01T00:00:00Z")
        _gdelta = state_delta.compute_delta(
            baseline=_gbaseline, vault=vault,
            default_output_dir=_resolve_verifier_output_dir(config, user_profile),
            chat_result=summary, config=config, execution_id=execution_id,
        )
        _gres = _gv.verify_goal(
            goal=(getattr(scroll, "raw_request", None) or getattr(scroll, "intent", "") or ""),
            delta=_gdelta, config=config, chat_result=summary,
        )
        ok = bool(_gres.get("verified"))
        logger.info(
            "[Runtime] intent-engine goal-verify: %s — %s",
            "PASS" if ok else "no-pass", str(_gres.get("reason", ""))[:160],
        )
        return ok
    except Exception:
        logger.debug("[Runtime] goal-level check errored", exc_info=True)
        return False


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.3 (Layer 3) — Tool registry v2 startup discovery + whitelist resolver
# ─────────────────────────────────────────────────────────────────────────

_V2_DISCOVERED: bool = False


def _discover_v2_tools() -> None:
    """Populate the v2 tool registry singleton by AST-scanning the
    ``systemu.runtime.tools`` package for modules that call
    ``registry.register(...)`` at top level.

    Idempotent — only runs once per process. ShadowRuntime calls this at
    init so v2 tools are available without each tool module needing to be
    imported explicitly by name.
    """
    global _V2_DISCOVERED
    if _V2_DISCOVERED:
        return
    try:
        from systemu.runtime.tool_registry_v2 import registry as _v2_registry
        _v2_registry.discover_modules("systemu.runtime.tools")
        _V2_DISCOVERED = True
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "[Runtime] v2 tool discovery failed: %s", exc,
        )


def _resolve_tool_whitelist(context: str) -> set:
    """Resolve the set of tool names allowed in ``context``.

    Wraps the registry's whitelist_for_context() with a safe fallback:
    unknown contexts return an empty set rather than raising, so the
    runtime can ask about novel contexts without crashing.

    Known contexts:
      - "main"          → every registered tool
      - "verifier_fork" → read-only subset (vault.get_audit_log, file.read, ...)
      - "curator"       → skill/memory lifecycle subset
      - "fact_extractor" → write_user_fact only
      - "delegate_child" → empty (runtime composes parent_whitelist - {delegate})
    """
    from systemu.runtime.tool_registry_v2 import registry as _v2_registry
    try:
        return _v2_registry.whitelist_for_context(context)
    except ValueError:
        import logging
        logging.getLogger(__name__).debug(
            "[Runtime] unknown whitelist context %r — returning empty set",
            context,
        )
        return set()


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.36 P2 — MCP tool-exposure budget (context-rot control)
# ─────────────────────────────────────────────────────────────────────────

_MCP_SEARCH_AFFORDANCE = {
    "name": "mcp_search_tools",
    "description": (
        "Search the tools available on connected MCP servers by keyword and "
        "expose a specific one for use. Use this when the MCP tool you need is "
        "not already listed (the per-run exposure budget hides the rest)."
    ),
    "parameters_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Keywords to match against tool names/descriptions."},
        },
        "required": ["query"],
    },
    "toolset": "mcp",
    "is_action_tool": False,
}


# Meta-tools that are MCP-toolset but are NOT per-server tools: they must always
# be exposed and NEVER counted against the budget (contract: "Exposure budget
# excludes mcp_call_tool AND mcp_search_tools from the budget count").
_MCP_BUDGET_EXEMPT = {"mcp_call_tool", "mcp_search_tools"}


def _apply_mcp_exposure_budget(catalog: List[Dict[str, Any]],
                               *, max_exposed: int) -> List[Dict[str, Any]]:
    """Cap per-server MCP-toolset entries at ``max_exposed`` per run. Non-MCP
    tools pass through untouched. The MCP meta-tools ``mcp_call_tool`` and
    ``mcp_search_tools`` are EXEMPT — always passed through and NEVER counted
    against the budget. When the remaining (countable) MCP tools exceed the
    budget, keep a round-robin slice across servers (families stay represented)
    and advertise a single ``mcp_search_tools`` affordance so the rest are
    reachable on demand.
    """
    non_mcp = [e for e in catalog if e.get("toolset") != "mcp"]
    # Exempt meta-tools (excluded from the count, but preserved in output);
    # de-dup any pre-existing mcp_search_tools so we re-add exactly one below.
    exempt = [e for e in catalog if e.get("toolset") == "mcp"
              and e.get("name") in _MCP_BUDGET_EXEMPT
              and e.get("name") != "mcp_search_tools"]
    mcp = [e for e in catalog if e.get("toolset") == "mcp"
           and e.get("name") not in _MCP_BUDGET_EXEMPT]
    if len(mcp) <= max_exposed:
        return non_mcp + exempt + mcp

    # Group by server (prefix mcp__<server>__) and round-robin to the budget.
    from collections import OrderedDict
    by_server: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for e in mcp:
        name = e.get("name", "")
        server = name.split("__")[1] if name.startswith("mcp__") and "__" in name[5:] else ""
        by_server.setdefault(server, []).append(e)

    kept: List[Dict[str, Any]] = []
    queues = list(by_server.values())
    idx = 0
    while len(kept) < max_exposed and any(queues):
        q = queues[idx % len(queues)]
        if q:
            kept.append(q.pop(0))
        idx += 1
        # Drop emptied queues so round-robin doesn't spin.
        queues = [qq for qq in queues if qq]
        if not queues:
            break
    # exempt meta-tools + the kept slice + exactly one search affordance.
    return non_mcp + exempt + kept + [dict(_MCP_SEARCH_AFFORDANCE)]


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.5 T0 — LLM-visible tool catalog builder (v1 + v2 unified)
# ─────────────────────────────────────────────────────────────────────────


def _build_llm_tool_catalog(vault=None, config=None) -> List[Dict[str, Any]]:
    """Build the LLM-visible tool catalog, combining v1 vault tools and v2
    code-registered tools.

    v2 tools whose check_fn returns False are EXCLUDED (so the LLM doesn't
    waste turns calling unavailable tools). v1 vault tools are filtered
    by the existing enabled/dry_run gates (passed in already-filtered via
    the ``vault`` arg — caller responsibility).

    For the test path (vault=None), only v2 tools are returned.

    Each entry has at minimum: name, description, parameters_schema.
    v1 entries also carry id and parameter_names (preserved for backward
    compat with the existing prompt template).
    """
    catalog: List[Dict[str, Any]] = []

    # Ensure v2 tool modules are imported before listing.
    _discover_v2_tools()

    # Resolve config for check_fn availability checks.
    _cfg = config
    if _cfg is None:
        try:
            from sharing_on.config import Config as _Config
            _cfg = _Config.from_env()
        except Exception:
            _cfg = None

    # ── v2 tools ──────────────────────────────────────────────────────────
    from systemu.runtime.tool_registry_v2 import registry as _v2

    for entry in _v2.list():
        # check_fn gating: exclude when unavailable.
        if entry.check_fn is not None:
            if not _v2.available(entry.name, _cfg):
                continue
        catalog.append({
            "name": entry.name,
            "description": entry.description or f"v2 tool: {entry.name}",
            "parameters_schema": dict(entry.schema or {}),
            "toolset": entry.toolset,
            "is_action_tool": entry.is_action_tool,
        })

    # ── v1 vault tools (preserve existing shape) ─────────────────────────
    if vault is not None:
        try:
            from systemu.core.models import ToolStatus as _ToolStatus
            v1_tools = (
                vault.list_tools(status=_ToolStatus.DEPLOYED)
                if hasattr(vault, "list_tools")
                else []
            )
        except Exception:
            v1_tools = []

        _existing_names = {e["name"] for e in catalog}
        for t in (v1_tools or []):
            if t.name in _existing_names:
                continue  # v2 wins on conflict (code-registered tools are
                           # intentional replacements of vault auto-forged stubs)
            catalog.append({
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "parameter_names": list(getattr(t, "parameter_names", []) or []),
                # v0.9.7: surface the tool's REAL parameter schema so the LLM
                # emits correctly-shaped {param: value} args instead of guessing.
                # The old hardcoded {} left the model blind to v1 params → it
                # emitted bare-string args (e.g. "http://…/json/") and crashed.
                "parameters_schema": dict(getattr(t, "parameters_schema", {}) or {}),
            })

    # v0.9.36 P2: cap MCP tool exposure (context-rot control); overflow is
    # reachable via the lazy mcp_search_tools affordance.
    _max = int(getattr(_cfg, "mcp_max_exposed_tools", 15)) if _cfg is not None else 15
    return _apply_mcp_exposure_budget(catalog, max_exposed=_max)


# ─────────────────────────────────────────────────────────────────────────
#  v0.9.2 (Layer 2) — Episodic memory capture hook
# ─────────────────────────────────────────────────────────────────────────


def _trigger_episodic_capture(
    *,
    vault,
    config,
    session_id: str,
    intent: str,
    chat_result: Optional[str],
    files_produced: list,
    status: str,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    raw_chat_id: Optional[str] = None,
) -> None:
    """v0.9.2 hook: summarize+persist the finished run.

    Gated by config.summarize_after_run. Best-effort — failures degrade silently
    so a flaky LLM never blocks the user's task from completing.
    """
    if vault is None or config is None:
        return  # nothing to capture against (e.g. __new__-constructed ShadowRuntime)
    if not getattr(config, "summarize_after_run", True):
        return
    try:
        from systemu.runtime import episodic_memory
        episodic_memory.capture(
            vault=vault,
            session_id=session_id,
            intent=intent,
            chat_result=chat_result,
            files_produced=files_produced,
            status=status,
            config=config,
            execution_id=execution_id,
            user_id=user_id,
            raw_chat_id=raw_chat_id,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "[Runtime] episodic capture failed for session %s: %s",
            session_id, exc,
        )


# ─────────────────────────────────────────────────────────────────────────

# Deferred refinery dispatch
def _dispatch_refinery(shadow, scroll, result_dict, context, config, vault):
    try:
        from systemu.pipelines.refinery import process_execution_result
        process_execution_result(shadow, scroll, result_dict, context, config, vault)
    except Exception as exc:
        logger.error("[Runtime] Failed to dispatch to Refinery: %s", exc)

logger = logging.getLogger(__name__)


def _observe_best_effort(label: str, fn):
    """Run a best-effort side-effect, returning its result.

    On failure the exception is logged (WARNING + traceback) instead of being
    swallowed silently — the step is still non-fatal (returns None), but a
    failed telemetry/refinery dispatch no longer disappears without a trace.
    """
    try:
        return fn()
    except Exception:  # noqa: BLE001 — deliberate best-effort boundary
        logger.warning("[Runtime] best-effort step failed: %s", label, exc_info=True)
        return None


MAX_ITERATIONS       = 30     # Hard ceiling on agentic loop iterations
SNAPSHOT_INTERVAL    = 5      # Compact after every N completed ActionBlocks

# Fix 2: a circuit-breaker trip on one of these (transient) reasons is NOT
# structural — a retry could still succeed, so it must not poison the
# structural-failure flag that tells the supervisor to skip the retry storm.
_TRANSIENT_FAIL_HINTS = ("timeout", "timed out", "504", "503", "429", "rate limit",
                         "temporar", "connection", "reset by peer", "econnreset",
                         "unavailable", "try again")


def _is_transient_reason(reason: str) -> bool:
    r = (reason or "").lower()
    return any(h in r for h in _TRANSIENT_FAIL_HINTS)

# v0.8.17: fail-fast constants + helper for consecutive-degraded-search detection.
_SEARCH_TOOLS = {"web_search"}
_MAX_CONSEC_DEGRADED_SEARCH = 3


def _is_degraded_search_result(tool_name, parsed) -> bool:
    """True iff a search tool returned a degraded result with no usable data
    (the whole provider chain failed/empty). NOT keyed on len(results)==0 —
    engines fuzzy-match and rarely return truly zero; degraded is the real
    'search is down' signal (v0.8.17 POC finding)."""
    if tool_name not in _SEARCH_TOOLS or not isinstance(parsed, dict):
        return False
    return bool(parsed.get("degraded")) and not parsed.get("results")


# v0.8.13 (RC#3): single source of truth for "can this tool be used in a normal
# (non-dry-run) run?" — shared by ShadowRuntime._load_tools and the direct_task
# readiness gate so the loader and the gate cannot drift.
_RUNTIME_READY_STATUSES = frozenset({ToolStatus.DEPLOYED, ToolStatus.TESTED, ToolStatus.UPGRADED})


def tool_is_runtime_ready(status) -> bool:
    """True iff a tool with this status can be used in a normal (non-dry-run) run.

    Single source of truth shared by ShadowRuntime._load_tools and the
    direct_task readiness gate so they cannot drift."""
    return status in _RUNTIME_READY_STATUSES


def _gen_execution_id() -> str:
    return f"exec_{secrets.token_hex(4)}"


# ── v0.9.8 KEYSTONE: tool-success auto-audit helpers ─────────────────────────
_AUDIT_PARAM_VALUE_CAP = 200  # max chars per stringified param value in the audit row


def _truncate_audit_params(params: Any) -> Dict[str, Any]:
    """Return a shallow, length-capped copy of ``params`` safe for the audit log.

    Each value is stringified and clipped to ``_AUDIT_PARAM_VALUE_CAP`` chars so a
    huge ``content=`` blob can't bloat the audit JSONL (and the verifier prompt).
    Never raises — returns ``{}`` for non-dict / unserialisable input.
    """
    out: Dict[str, Any] = {}
    if not isinstance(params, dict):
        return out
    for k, v in params.items():
        try:
            sv = v if isinstance(v, (int, float, bool)) or v is None else str(v)
            if isinstance(sv, str) and len(sv) > _AUDIT_PARAM_VALUE_CAP:
                sv = sv[:_AUDIT_PARAM_VALUE_CAP] + "…[truncated]"
            out[str(k)] = sv
        except Exception:
            continue
    return out


def _build_tool_audit_entry(
    *,
    execution_id: str,
    objective_id: Any,
    tool_name: str,
    params: Any,
) -> Dict[str, Any]:
    """Build the compact audit row written for every successful tool call.

    Matches the shape ``vault.append_action_audit`` documents (vault.py:1116):
    keys ``ts`` (ISO), ``execution_id``, ``objective_id``, ``action``, ``params``
    (truncated dict), ``success`` (True), ``error`` (None). The ``ts`` format
    (``...Z``) matches state_delta's baseline ``iteration_start_ts`` so the
    verifier's ``query_action_audit(since_ts=...)`` filter surfaces the row.
    """
    try:
        oid = int(objective_id)
    except (TypeError, ValueError):
        oid = 0
    return {
        "ts": utcnow().isoformat() + "Z",
        "execution_id": execution_id,
        "objective_id": oid,
        "action": tool_name or "?",
        "params": _truncate_audit_params(params),
        "success": True,
        "error": None,
    }


def _current_objective_id_for_audit(objectives, completed) -> int:
    """Best-effort current-objective id for an audit row when the decision did
    not declare ``completes_objective``: the first not-yet-completed objective
    whose dependencies are all satisfied, else 0. Never raises."""
    try:
        if not objectives:
            return 0
        done = set(completed or [])
        for o in objectives:
            if o.id in done:
                continue
            if all(dep in done for dep in (getattr(o, "depends_on", None) or [])):
                return int(o.id)
        return 0
    except Exception:
        return 0


# v0.9.8 (B2): read-only research tools that, when called repeatedly with nothing
# produced, signal a "research forever, never write" loop.
_RESEARCH_TOOLS_B2 = ("web_search", "web_read", "web_extract", "fetch_json")
_PRODUCE_TOKENS_B2 = ("file_write", "write_file", "save")


def _research_loop_steer(*, tool_name, success, consec_reads, steers_used,
                         threshold, cap):
    """Pure B2 bookkeeping for the research-loop convergence steer.

    Returns ``(consec_reads, steers_used, steer_or_None)``:
      * a successful PRODUCE call (file_write/…) resets ``consec_reads`` to 0;
      * a successful read-only RESEARCH call increments it;
      * when ``consec_reads >= threshold`` and ``steers_used < cap``, emit a
        forceful "stop searching, write now" steer, reset the counter, and bump
        ``steers_used``.
    Independent of objective-credit (which audit evidence keeps resetting), so it
    catches the loop the stall path misses. Never raises.
    """
    tn = (tool_name or "").lower()
    if success:
        if any(t in tn for t in _PRODUCE_TOKENS_B2):
            consec_reads = 0
        elif tn in _RESEARCH_TOOLS_B2:
            consec_reads += 1
    steer = None
    if consec_reads >= threshold and steers_used < cap:
        steers_used += 1
        consec_reads = 0
        steer = (
            "## Convergence steer\n"
            f"You have made {threshold}+ research/search calls in a row without "
            "producing a deliverable. You very likely already have enough to "
            "answer. STOP searching now: synthesize your best answer from what you "
            "have gathered and call the file-write tool to SAVE it to the requested "
            "output file THIS turn (or give your final answer if no file was "
            "requested). Only search again if you are missing one specific, named "
            "fact you cannot answer without."
        )
    return consec_reads, steers_used, steer


def _objective_items(objectives, completed) -> list:
    """v0.8.19 (R2): derive per-objective status for the live checklist.
    done = in completed; in_progress = deps satisfied but not done; else pending."""
    items = []
    for o in objectives:
        if o.id in completed:
            st = "done"
        elif all(d in completed for d in (o.depends_on or [])):
            st = "in_progress"
        else:
            st = "pending"
        items.append({"id": o.id, "goal": o.goal, "status": st})
    return items


def _objective_state_event(objectives, completed, execution_id, *, stamp) -> dict:
    """v0.8.19 (R2): build an objective_state EventBus event (stamp = origin wrapper)."""
    return stamp({
        "ts": utcnow().isoformat() + "Z",
        "level": "INFO",
        "category": "objective_state",
        "message": f"objectives {len(completed)}/{len(objectives)}",
        "context": {"execution_id": execution_id,
                    "items": _objective_items(objectives, completed)},
    })


def _stuck_thresholds() -> tuple[int, int, bool]:
    """v0.8.21: per-call read of stuck-guard env vars (live-editable via Settings)."""
    no_progress = int(os.environ.get("SYSTEMU_STUCK_NO_PROGRESS", "5") or "5")
    tool_fails  = int(os.environ.get("SYSTEMU_STUCK_TOOL_FAILS", "3") or "3")
    guard_on    = (os.environ.get("SYSTEMU_STUCK_GUARD", "on") or "on").lower() != "off"
    return (no_progress, tool_fails, guard_on)


_NO_PROGRESS_TAG = "__NO_PROGRESS_CARRY__::"


def _encode_no_progress_note(iters_since_credit: int) -> str:
    """Fix #5: sticky-note carrying the no-progress counter across a resume so the
    resumed run doesn't restart its 'iterations since objective credit' at 0 and
    re-do the same futile work from scratch."""
    return f"{_NO_PROGRESS_TAG}{int(iters_since_credit)}"


def _decode_no_progress_note(sticky_notes) -> int:
    for n in (sticky_notes or []):
        if isinstance(n, str) and n.startswith(_NO_PROGRESS_TAG):
            try:
                return int(n[len(_NO_PROGRESS_TAG):])
            except (TypeError, ValueError):
                return 0
    return 0


def _should_force_finalize_stuck(*, coach_steers_used: int, max_steers: int,
                                 stuck_round: int, finalize_after_rounds: int) -> bool:
    """Fix #2/#4: once the auto-coach budget is spent AND the SAME objective has
    been stuck for >= finalize_after_rounds rounds, stop coaching/re-parking and
    force a terminal failure. finalize_after_rounds<=0 disables (back-compat)."""
    if finalize_after_rounds <= 0:
        return False
    return coach_steers_used >= max(0, max_steers) and stuck_round >= finalize_after_rounds


def _build_user_context_block(vault) -> str:
    """v0.9.0 (Layer 1): compact one-block summary of the user profile + up to
    5 most-recent facts. Returns "" when no profile is set.

    The block is <= ~10 lines so it fits comfortably in a system prompt without
    risk to the token budget. Layer 2 (episodic memory) will expand this.
    """
    try:
        prof = vault.get_user_profile()
        if prof is None:
            return ""
        lines = [
            "## What you know about the user",
            f"- name: {prof.name}",
            f"- location: {prof.location_text}",
            f"- timezone: {prof.timezone}",
            f"- default_output_dir: {prof.default_output_dir}",
        ]
        facts = vault.load_user_facts(recent=5, include_superseded=False)
        if facts:
            lines.append("- facts (most recent):")
            for f in facts[-5:]:
                conf = f"{f.confidence:.2f}"
                lines.append(f"  - ({conf}) {f.fact}")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_boot_memory(shadow: Any, vault: Any) -> str:
    """Build the boot-time memory block for a shadow.

    Injects full GLOBAL_MEMORY (always) + a one-line SHADOW_MEMORY header
    (lazy — shadow calls LOAD_RESOURCE memory:self to read the full file).
    """
    parts: List[str] = []

    try:
        global_md = vault.load_global_memory()
        if global_md.strip():
            parts.append(
                f"## Global Memory (cross-shadow personalisation)\n\n{global_md.rstrip()}"
            )
    except Exception as exc:
        logger.warning("[Runtime] Could not load global memory (non-fatal): %s", exc)

    try:
        shadow_md, _ = vault.load_shadow_memory(shadow.id)
        entry_count = shadow_md.count("\n- ") if shadow_md else 0
        if entry_count > 0:
            parts.append(
                f"## Specialist Memory — {entry_count} entries available. "
                f"Use `LOAD_RESOURCE resource_type=\"memory\" resource_id=\"self\"` "
                f"to consult your specialist memory if you encounter unfamiliar territory."
            )
    except Exception as exc:
        logger.warning("[Runtime] Could not load shadow memory header (non-fatal): %s", exc)

    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
#  ShadowRuntime
# ─────────────────────────────────────────────────────────────────────────────

def _apply_terminate_directive(
    directive,
    *,
    context,
    shadow,
    scroll,
    execution_id: str,
    vault=None,
    origin: str = "system",
) -> None:
    """Handle a TERMINATE directive from the Intelligent Supervisor (v0.4.1-b).

    Publishes an operator approval card to the chat feed (via the v0.3.6
    redirect-card pattern) so the operator can choose one of three actions:
    Retry-with-different-shadow / Discard / Inspect.  Also records an
    entry in the affinity log so future shadow-assignment decisions can
    exclude the shadow that just gave up.

    Note: TERMINATE itself is **advisory** at this seam — the existing
    outer loop continues to process the shadow's natural FAIL/COMPLETE
    decision.  The supervisor's role is to surface the situation; the
    operator decides the recovery action via the approval card.
    """
    try:
        from systemu.runtime.affinity_log import compute_intent_hash, get_affinity_log
        intent_hash = compute_intent_hash(
            intent=getattr(scroll, "intent", ""),
            objectives=getattr(scroll, "objectives", None),
        )
        if shadow is not None:
            get_affinity_log().record_termination(
                intent_hash=intent_hash,
                shadow_id=getattr(shadow, "id", "unknown"),
                scroll_id=getattr(scroll, "id", None),
                execution_id=execution_id,
                reason="supervisor_terminate",
            )
    except Exception:
        logger.debug("[Runtime] affinity log record skipped", exc_info=True)

    # Sticky note + reflection block so the LLM still in the loop sees the
    # supervisor's verdict and can wind down cleanly with FAIL.
    context.add_sticky_note(
        f"Supervisor TERMINATEd execution: {directive.rationale[:200]}"
    )
    context.queue_reflection_block(
        "The Intelligent Supervisor has decided this execution should "
        "TERMINATE.  Wind down with a FAIL action and a short reason "
        "referring to the supervisor's diagnosis above.  Operator is "
        "being notified separately and will choose the recovery action."
    )

    # Resolve scroll/shadow names for the operator-facing card; the execution
    # id has no name so it stays as a short companion.
    from systemu.interface.name_resolver import resolve_name, short_id
    _scroll_name = (
        resolve_name(getattr(scroll, "id", ""), vault) if vault is not None and getattr(scroll, "id", "")
        else getattr(scroll, "name", "") or "this scroll"
    )
    _shadow_name = (
        resolve_name(getattr(shadow, "id", ""), vault) if vault is not None and getattr(shadow, "id", "")
        else getattr(shadow, "name", "") or "the shadow"
    )

    # Operator approval card via the v0.3.6 supervisor-flash bus.
    try:
        from datetime import datetime as _dt, timezone as _tz
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()
        bus.publish({
            "ts":       _dt.now(tz=_tz.utc).isoformat(timespec="seconds"),
            "level":    "WARNING",
            "category": "approval",
            "origin":   origin,   # v0.8.16: trigger origin threaded from execute()
            "message":  f"🛑 Supervisor TERMINATEd: {_scroll_name} · {short_id(execution_id)}",
            "context": {
                "approval_message": (
                    f"The Intelligent Supervisor has decided {_shadow_name}'s run of "
                    f"“{_scroll_name}” cannot succeed and should terminate "
                    f"(execution {short_id(execution_id)}). "
                    f"Reason: {directive.rationale or 'see audit log'}.\n\n"
                    "Choose a recovery action on the workflow detail page."
                ),
                "options":       [],
                "redirect_to":   f"/workflow/{execution_id}",
                "dedup_key":     f"supervisor-terminate:{execution_id}",
                "execution_id":  execution_id,
                "shadow_id":     getattr(shadow, "id", None),
                "scroll_id":     getattr(scroll, "id", None),
                "supervisor_rationale": directive.rationale or "",
                "actions":       ["retry_with_different_shadow", "discard", "inspect"],
            },
        })
    except Exception:
        logger.debug("[Runtime] TERMINATE approval card publish skipped", exc_info=True)


def _auto_approve_recalibration(
    *, result, vault, shadow, scroll, execution_id: str,
) -> None:
    """v0.5.1-c — bypass the operator card, enable + resume immediately.

    Used only when ``is_low_risk_recalibration()`` returned True AND the
    config flag is on.  Mirrors the operator's "Enable & Resume" click on
    the Tools page card, but happens automatically.
    """
    new_tool_id = result.new_tool_id or result.original_tool_id
    # v0.9.48 Phase 3: route through the gated enable mechanism instead of
    # laundering a failed dry-run to "skipped" + flipping .enabled directly. A
    # tool whose dry_run_status isn't passed/skipped is refused, so we log and
    # return WITHOUT resuming the activity.
    try:
        from systemu.pipelines import tool_service
        if not tool_service.enable_tool(new_tool_id, vault):
            status = getattr(vault.get_tool(new_tool_id), "dry_run_status", "not_run")
            logger.warning(
                "[Runtime] auto-approve: enable refused for %s "
                "(dry_run_status=%s) — not resuming",
                new_tool_id, status,
            )
            return
    except Exception:
        logger.exception("[Runtime] auto-approve: tool enable failed")
        return

    try:
        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.get()
        sub_id = sup.resume_after_recalibration(
            execution_id=execution_id,
            original_tool_id=result.original_tool_id,
            new_tool_id=new_tool_id,
            mode=result.mode,
            original_shadow_id=shadow.id,
            scroll_id=getattr(scroll, "id", None) if scroll is not None else None,
        )
        logger.info(
            "[Runtime] auto-approved recalibration → resumed activity (sub=%s)",
            sub_id,
        )
    except Exception:
        logger.exception("[Runtime] auto-approve: resume failed")


def _apply_recalibrate_tool_directive(
    directive,
    *,
    context,
    shadow,
    scroll,
    execution_id: str,
    config,
    vault,
    consec_tool_fails: Dict[str, int],
    origin: str = "system",
) -> None:
    """v0.5.0-d: Handle a RECALIBRATE_TOOL supervisor directive.

    1. Identify the failing tool from ``consec_tool_fails`` (highest-fail-count tool).
    2. Run the Tier-1 inadequacy diagnosis (cached per tool × execution).
    3. If verdict says inadequate, run the recalibration pipeline (bump or fork).
    4. Publish operator approval card via v0.3.6 supervisor-flash bus.
    5. Pin a sticky note + reflection block so the LLM winds down with FAIL.

    Never raises; never directly modifies vault state other than via the
    recalibrator (which writes the new/updated tool + dry-run evidence).
    """
    if not consec_tool_fails or vault is None or shadow is None:
        logger.debug("[Runtime] RECALIBRATE_TOOL skipped — missing context")
        return

    # Pick the tool with the most consecutive fails as the candidate.
    tool_name, _fail_count = max(consec_tool_fails.items(), key=lambda kv: kv[1])
    try:
        tool = vault.find_tool_by_name(tool_name)
    except Exception:
        tool = None
    if tool is None:
        logger.debug("[Runtime] RECALIBRATE_TOOL: tool %s not in vault", tool_name)
        return

    # Pull recent failure observations from context for the diagnosis prompt.
    recent_fails: List[Dict[str, Any]] = []
    try:
        for ev in (context._history or [])[-20:]:
            if ev.event_type == "observation":
                c = ev.content
                if isinstance(c, dict) and c.get("success") is False:
                    recent_fails.append(c)
                    if len(recent_fails) >= 3:
                        break
    except Exception:
        pass

    try:
        from systemu.pipelines.tool_inadequacy_diagnosis import diagnose_tool_inadequacy
        diagnosis = diagnose_tool_inadequacy(
            tool=tool, shadow=shadow,
            config=config, vault=vault,
            execution_id=execution_id,
            failing_objective=(directive.rationale or "")[:300],
            recent_failures=recent_fails,
            scroll_intent=getattr(scroll, "intent", "") if scroll is not None else "",
        )
    except Exception:
        logger.exception("[Runtime] RECALIBRATE_TOOL diagnosis crashed")
        return

    if not diagnosis.is_inadequate:
        logger.info(
            "[Runtime] RECALIBRATE_TOOL: diagnosis says tool not inadequate (rationale=%s) — no recalibration",
            diagnosis.rationale[:120],
        )
        context.add_sticky_note(
            f"Supervisor considered recalibrating {tool.name} but diagnosis "
            f"declined: {diagnosis.rationale[:160]}"
        )
        return

    try:
        from systemu.pipelines.tool_recalibrator import (
            is_low_risk_recalibration, publish_recalibration_card, recalibrate_tool,
        )
        result = recalibrate_tool(
            tool=tool, shadow=shadow, diagnosis=diagnosis,
            failure_context=(directive.rationale or "")[:400],
            config=config, vault=vault, execution_id=execution_id,
        )

        # v0.5.1-c: auto-approve low-risk recalibrations when config allows.
        # Bypasses the operator card entirely — enables tool + resumes the
        # activity directly.  Default config has this OFF; opt-in via
        # SYSTEMU_AUTO_APPROVE_LOW_RISK_RECAL=true.
        auto_approved = False
        if (
            getattr(config, "auto_approve_low_risk_recalibrations", False)
            and result.success
        ):
            eligible, reason = is_low_risk_recalibration(
                result=result, tool=tool, diagnosis=diagnosis,
            )
            if eligible:
                logger.info(
                    "[Runtime] auto-approving low-risk recalibration: %s", reason,
                )
                _auto_approve_recalibration(
                    result=result, vault=vault,
                    shadow=shadow, scroll=scroll, execution_id=execution_id,
                )
                auto_approved = True
            else:
                logger.debug(
                    "[Runtime] auto-approve declined: %s — surfacing card", reason,
                )

        if not auto_approved:
            publish_recalibration_card(
                result=result, shadow_id=shadow.id,
                execution_id=execution_id,
                scroll_id=getattr(scroll, "id", None) if scroll is not None else None,
                origin=origin,
            )
    except Exception:
        logger.exception("[Runtime] recalibration pipeline crashed")
        return

    # v0.5.1-e: persist execution snapshot for true resume after the
    # operator approves the recalibrated tool.  When skipped (auto-
    # approve path or write failure), the resume falls back to v0.5.0's
    # fresh-restart-with-sticky behaviour.
    try:
        from systemu.runtime.execution_snapshot import (
            ExecutionSnapshot, capture_from_context, write_snapshot,
        )
        # The shadow_runtime caller stashed iteration / current_ab / completed
        # into context attributes so we can pull them here without threading
        # them through every directive helper.
        snapshot = capture_from_context(
            execution_id=execution_id,
            shadow_id=shadow.id,
            scroll_id=getattr(scroll, "id", "") if scroll is not None else "",
            iteration=int(getattr(context, "_resume_iteration", 0)),
            current_action_block=int(getattr(context, "_resume_current_ab", 1)),
            completed_objectives=getattr(context, "_resume_completed_objectives", set()),
            context=context,
            original_tool_id=tool.id,
            recalibration_dedup_key=(
                f"tool-recalibrate:{tool.id}:{execution_id}"
            ),
            # v0.9.33 Bug 2/3: the loop stashes the cap count + depth on context
            # (no loop-local in this helper's scope) so a recalibration-resume
            # keeps counting toward the cap instead of silently resetting it.
            requests_this_run=int(getattr(context, "_resume_requests_this_run", 0) or 0),
            subagent_depth=int(getattr(context, "_resume_subagent_depth", 0) or 0),
        )
        write_snapshot(snapshot)
    except Exception:
        logger.debug("[Runtime] snapshot capture skipped", exc_info=True)

    # Sticky note + reflection so the LLM winds down with FAIL while the
    # operator decides on the approval card.
    context.add_sticky_note(
        f"Supervisor triggered RECALIBRATE_TOOL for {tool.name} → "
        f"{result.mode}{' (fallback)' if result.forced_fallback else ''}. "
        f"Awaiting operator approval on /tools."
    )
    context.queue_reflection_block(
        "The Intelligent Supervisor has initiated tool recalibration because "
        f"{tool.name} appears structurally inadequate.  A new {result.mode} "
        f"has been forged and dry-run "
        f"{result.dry_run_status}.  Wind down with a FAIL action; the operator "
        "will resume this activity once the new tool is approved."
    )


def _maybe_decay_loaded_skills(
    context,
    *,
    vault,
    status: str,
) -> None:
    """v0.6.1-c: per-iteration hook — decay effectiveness on loaded skills
    when the current execution observed a failure / partial.

    Idempotent per (execution × skill) via ``context._decayed_skills_this_exec``.
    Crossing ``RECAL_THRESHOLD`` queues a RECALIBRATE_SKILL directive on
    ``context.pending_directives`` (consumed by ``_apply_supervisor_directives``).
    """
    loaded = getattr(context, "_loaded_skill_ids", None)
    if not loaded:
        return

    decayed = getattr(context, "_decayed_skills_this_exec", None)
    if decayed is None:
        decayed = set()
        context._decayed_skills_this_exec = decayed

    pending = getattr(context, "pending_directives", None)
    if pending is None:
        pending = []
        context.pending_directives = pending

    for skill_id in list(loaded):
        if skill_id in decayed:
            continue
        try:
            skill = vault.get_skill(skill_id)
        except Exception:
            continue
        decayed.add(skill_id)
        crossed = decay_effectiveness(skill, status=status, vault=vault)
        if crossed:
            from types import SimpleNamespace
            pending.append(SimpleNamespace(
                action="RECALIBRATE_SKILL",
                skill_id=skill_id,
            ))
            logger.info(
                "[Runtime] decay crossed threshold for skill %s — "
                "RECALIBRATE_SKILL queued",
                skill_id,
            )


def _apply_recalibrate_skill_directive(
    directive,
    *,
    context,
    vault,
    config,
    execution_id: str,
    origin: str = "system",
) -> None:
    """v0.6.1-c: dispatch RECALIBRATE_SKILL — re-author the failing skill's
    ``instructions_md`` and either auto-apply (low-risk + opt-in env knob)
    or surface a flash card on /skills.

    Mirrors ``_apply_recalibrate_tool_directive`` so the operator UX is
    consistent between the tool and skill recal flows.
    """
    skill_id = getattr(directive, "skill_id", None)
    if not skill_id:
        logger.debug("[Runtime] RECALIBRATE_SKILL missing skill_id — skipping")
        return

    try:
        skill = vault.get_skill(skill_id)
    except Exception:
        logger.debug("[Runtime] RECALIBRATE_SKILL: skill %s not in vault", skill_id)
        return

    failure_context = {
        "execution_id": execution_id,
        "status":       "failure",
        "summary":      "Effectiveness score decayed below threshold",
        "recent_failure_observations": [],
        "objective_in_flight": "",
    }

    try:
        result = recalibrate_skill(
            skill, failure_context=failure_context,
            config=config, vault=vault, mode="bump_skill",
        )
    except Exception:
        logger.exception("[Runtime] RECALIBRATE_SKILL recalibrator crashed")
        return

    if not result.success:
        logger.warning(
            "[Runtime] RECALIBRATE_SKILL did not succeed: %s", result.error,
        )
        return

    # Auto-approve gate — env knob + all conservative criteria must pass.
    auto = bool(getattr(config, "auto_approve_low_risk_skill_recalibrations", False))
    eligible, reason = is_low_risk_skill_recalibration(result, skill)
    if auto and eligible:
        try:
            apply_recalibration(
                skill, result, vault=vault,
                reason=f"auto-approved low-risk (exec={execution_id})",
            )
            logger.info(
                "[Runtime] auto-approved RECALIBRATE_SKILL for %s — applied",
                skill_id,
            )
            return
        except Exception:
            logger.exception(
                "[Runtime] auto-apply RECALIBRATE_SKILL failed — falling back to operator card",
            )

    # Operator approval path — flash a card on /skills.
    try:
        from datetime import datetime as _dt, timezone as _tz
        from systemu.interface.event_bus import EventBus
        EventBus.get().publish({
            "ts": _dt.now(tz=_tz.utc).isoformat(timespec="seconds"),
            "level": "WARNING",
            "category": "approval",
            "origin": origin,   # v0.8.16: trigger origin threaded from execute()
            "message": f"Skill '{skill.name}' needs recalibration",
            "context": {
                "approval_message": (
                    f"Auto-approve declined: {reason}\n\n"
                    f"Proposed new instructions_md:\n\n"
                    f"{result.new_instructions_md[:600]}..."
                ),
                "options": [],
                "redirect_to": "/skills",
                "dedup_key":   f"skill-recalibrate:{skill_id}:{execution_id}",
                "skill_id":    skill_id,
            },
        })
    except Exception:
        logger.debug(
            "[Runtime] could not flash RECALIBRATE_SKILL card", exc_info=True,
        )


def _apply_supervisor_directives(directives, *, context, config, shadow=None, scroll=None, execution_id: str = "", vault=None, consec_tool_fails=None, origin: str = "system") -> None:
    """Apply directives from the Intelligent Supervisor between iterations.

    Each directive is one of the bounded vocabulary actions defined in
    ``systemu/runtime/execution_mind.ACTION_VOCABULARY``.  The actions
    that mutate the shadow's prompt or state are applied directly here;
    DO_NOTHING / ESCALATE / TERMINATE / SWAP_SHADOW return-only signals
    are logged but currently still flow through the standard outcome
    path — v0.4.0-d focuses on the in-shadow effects (NUDGE, REFLECT,
    ROLLBACK, SET_THINK_BUDGET).  Future phases route the operator-
    facing actions through the v0.3.6 approval-flash bus.
    """
    for d in directives:
        try:
            if d.action == "NUDGE" and d.hint:
                context.queue_reflection_block(f"Supervisor nudge: {d.hint}")
                context.add_sticky_note(f"Supervisor nudge: {d.hint[:120]}")
            elif d.action == "INJECT_REFLECTION":
                # Mind's rationale carries the structured reflection text.
                context.queue_reflection_block(
                    f"Supervisor reflection: {d.rationale or 'reassess strategy.'}"
                )
            elif d.action == "FORCE_REFLECT":
                context.queue_reflection_block(
                    "Supervisor requires you to emit a REFLECT decision next, "
                    "naming the strategy you intend to follow. "
                    f"Supervisor reasoning: {d.rationale[:200]}"
                )
            elif d.action == "ROLLBACK":
                target = context.rollback_to_last_snapshot()
                if target is not None:
                    context.queue_reflection_block(
                        "Supervisor rolled back the context to the last snapshot. "
                        "Sticky notes preserved — choose a different approach."
                    )
            elif d.action == "SET_THINK_BUDGET" and d.think_budget_delta:
                # Bump the in-memory ceiling for this run only.
                new_val = (getattr(config, "max_consecutive_think", 5) or 5) + int(d.think_budget_delta)
                try:
                    config.max_consecutive_think = max(1, min(new_val, 30))
                except Exception:
                    pass  # frozen dataclass; supervisor must use a mutable config
            elif d.action == "TERMINATE":
                # v0.4.1-b: TERMINATE now produces an operator-facing approval
                # card + records to the affinity log so future assignment
                # decisions can exclude the shadow that just gave up.
                _apply_terminate_directive(
                    d, context=context, shadow=shadow, scroll=scroll,
                    execution_id=execution_id, vault=vault, origin=origin,
                )
            elif d.action == "RECALIBRATE_TOOL":
                # v0.5.0-d: tool inadequacy → diagnose → bump / fork → operator card.
                # The dispatcher infers the failing tool from the rolling
                # ``_consec_tool_fails`` map (most-recently-failing tool wins).
                _apply_recalibrate_tool_directive(
                    d, context=context, shadow=shadow, scroll=scroll,
                    execution_id=execution_id, config=config, vault=vault,
                    consec_tool_fails=consec_tool_fails or {}, origin=origin,
                )
            elif d.action == "RECALIBRATE_SKILL":
                # v0.6.1-c: skill inadequacy → re-author instructions_md →
                # operator card (or auto-apply when low-risk + env knob set).
                _apply_recalibrate_skill_directive(
                    d, context=context, vault=vault, config=config,
                    execution_id=execution_id, origin=origin,
                )
            elif d.action in ("DO_NOTHING", "SWAP_SHADOW", "ESCALATE"):
                # No-op in-shadow; operator-facing — handled at the
                # supervisor / orchestration layer or future phases.
                pass
        except Exception:
            logger.debug("[Runtime] directive application failed for %s", d.action, exc_info=True)


def _build_reflection_block(
    *,
    tool_name: str,
    category: str,
    keyword,
    consec: int,
    strategies: list,
    force_reflect: bool,
) -> str:
    """Compose the v0.4.0-b in-loop reflection block.

    Compact intentionally — token budget is precious.  References the
    classifier's category, the consecutive-failure count, and the
    recommended strategy enumeration so the LLM can either pick one or
    issue a REFLECT decision (which is mandatory once consec ≥ 3).
    """
    strategy_lines = "\n".join(f"  - {s}" for s in strategies)
    kw = f" (keyword: {keyword})" if keyword else ""
    body = (
        f"The tool **{tool_name}** has failed **{consec}** time(s) "
        f"this run.  Failure category: **{category}**{kw}.\n\n"
        f"Recommended strategies:\n{strategy_lines}\n\n"
        "If a strategy is clearly best, take it directly via the "
        "appropriate action.  Otherwise, emit a single REFLECT decision "
        "that names the strategy you intend to follow next."
    )
    if force_reflect:
        body += (
            "\n\n**Required**: your NEXT decision MUST be `REFLECT` "
            "(this tool has failed ≥3 times — surface your strategy choice "
            "explicitly before any further tool call).  After REFLECT, "
            "proceed with the chosen strategy."
        )
    return body


def _record_terminal_telemetry(
    *,
    shadow,
    execution_id: str,
    scroll,
    status: str,
    iteration: int,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """v0.4.0-0 — best-effort telemetry write at execution terminal state.

    Skipped for ``status="success"`` (we only want failure-mode data).
    Failures inside this function are swallowed inside the telemetry module
    so the shadow's exit path is never affected.

    v0.4.3-a: Also records the outcome to ShadowMetrics keyed by
    (shadow_id, intent_hash) — this runs for ALL statuses including
    success.  The metrics feed the supervisor's affinity-routing
    alternative-selection so shadows with proven track records on
    similar work get preferred.
    """
    try:
        from systemu.runtime.failure_telemetry import record_execution_terminal
        record_execution_terminal(
            shadow_id=(shadow.id if shadow is not None else None),
            execution_id=execution_id,
            activity_id=None,                # not directly available at this seam
            scroll_id=(scroll.id if scroll is not None else None),
            status=status,
            iterations=iteration,
            extra=extra,
        )
    except Exception:
        logger.debug("[Runtime] terminal telemetry skipped", exc_info=True)

    # v0.4.3-a: separate metric update path — runs for every status.
    _record_shadow_metric(shadow=shadow, scroll=scroll, status=status)


def _record_shadow_metric(*, shadow, scroll, status: str) -> None:
    """Update ShadowMetrics for this terminal state.

    Computes the intent_hash from the scroll and records the outcome.
    Skipped silently when the shadow / scroll / status can't be resolved.
    """
    if shadow is None or scroll is None:
        return
    try:
        from systemu.runtime.affinity_log import compute_intent_hash
        from systemu.runtime.shadow_metrics import get_shadow_metrics
        intent_hash = compute_intent_hash(
            intent=getattr(scroll, "intent", "") or "",
            objectives=getattr(scroll, "objectives", None),
        )
        get_shadow_metrics().record(
            shadow_id=getattr(shadow, "id", ""),
            intent_hash=intent_hash,
            status=status,
        )
    except Exception:
        logger.debug("[Runtime] shadow_metrics record skipped", exc_info=True)


def _build_user_payload(
    *,
    shadow_name: str,
    output_dir: str,
    current_date: str,
    current_datetime_utc: str,
    use_objectives: bool,
    intent,
    scroll_json,
    completed_objectives,
    pending_objectives,
    current_ab: int,
    available_tools,
    history,
    last_snapshot,
    iteration: int,
    iter_budget: int,
) -> dict:
    """Build the BASE per-iteration user payload sent to the Tier-2 decision LLM.

    v0.9.33 (Bug 4-C): surfaces the iteration budget — ``iteration``,
    ``iter_budget``, ``iterations_remaining`` — so the agent can make
    budget-aware decisions (wind down / consolidate when the budget is nearly
    spent) instead of looping blindly to the ceiling.  ``iter_budget`` is the
    LIVE budget at the call site (a COMPUTE harness grant can extend it
    mid-run), so ``iterations_remaining`` always reflects the real headroom.

    An *escalating* ``low_budget_notice`` is added when the remaining budget is
    low AND there is still pending work — it re-fires (with the live remaining
    count) on each low-budget iteration rather than once, so the reminder does
    not get buried by intervening history.

    Pure function: no I/O, no mutation of inputs, and the returned ``dict`` is a
    fresh object whose ``available_tools`` is a NEW list — the loop mutates the
    result in place (v2-tool augmentation, one-shot operator_hint, loop-guard
    notice/force-finalize), so it must not alias the caller's inputs.
    """
    iterations_remaining = iter_budget - iteration

    payload: dict = {
        "shadow_name":          shadow_name,
        "output_dir":           output_dir,
        "current_date":         current_date,
        "current_datetime_utc": current_datetime_utc,
        # v0.9.33-C: iteration budget surfaced to the agent.
        "iteration":            iteration,
        "iter_budget":          iter_budget,
        "iterations_remaining": iterations_remaining,
    }

    if use_objectives:
        payload.update({
            "intent":               intent,
            "objectives":           scroll_json,
            "completed_objectives": list(completed_objectives),
            "pending_objectives":   pending_objectives,
        })
    else:
        payload.update({
            "current_action_block": current_ab,
            "pending_action_blocks": [
                ab for ab in scroll_json
                if ab.get("step_number", 0) >= current_ab
            ],
        })

    # New list (not the caller's) so the loop's in-place .append() is safe.
    payload["available_tools"] = list(available_tools)
    payload["history"]         = history
    payload["last_snapshot"]   = last_snapshot

    # v0.9.33-C: escalating low-budget nudge — only when the budget is nearly
    # spent AND there is still pending work.  Cheap, additive, and only fires in
    # the narrow window so it does not pollute every payload.
    _LOW_BUDGET_THRESHOLD = 3
    _has_pending = bool(pending_objectives) if use_objectives else True
    if 0 < iterations_remaining <= _LOW_BUDGET_THRESHOLD and _has_pending:
        payload["low_budget_notice"] = (
            f"Only {iterations_remaining} iteration(s) remain before the budget "
            f"({iter_budget}) is exhausted and the run is force-finalized. "
            "Prioritize the most load-bearing remaining objective, consolidate, "
            "and prepare to COMPLETE — do not start new exploratory work."
        )

    return payload


def _build_history_slice(context, max_events: int = 30) -> list:
    """Return the LAST N tool-call/observation/thought events (in chronological
    order) as a compact list for LLM context.

    v0.9.7 fix (round-about-loop root cause): collect from the NEWEST event
    backward, then restore chronological order. The previous implementation
    iterated the recent window oldest-first and ``break``-ed after N, so on longer
    runs it returned the OLDEST N events and silently dropped the most recent
    ones — the model could not see what it had just done and re-proposed it.
    """
    recent = []
    for event in reversed(context._history):   # newest-first so we never drop recent events
        if event.event_type == "tool_call":
            recent.append({
                "role": "tool_call",
                "tool": event.content.get("tool_name"),
                "params": event.content.get("parameters"),
                "completes_objective": event.content.get("completes_objective"),
            })
        elif event.event_type == "observation":
            c = event.content
            # Truncate large data fields so they don't bloat the prompt
            if isinstance(c, dict):
                preview = {}
                for k, v in c.items():
                    preview[k] = str(v)[:400] if isinstance(v, (dict, list)) and len(str(v)) > 400 else v
                c = preview
            recent.append({"role": "tool_result", "result": c})
        elif event.event_type == "thought":
            recent.append({"role": "thought", "thought": event.content.get("thought", "")[:300]})
        if len(recent) >= max_events:
            break
    recent.reverse()   # restore chronological (oldest→newest) order for the prompt
    return recent


def _coerce_scalar_parameter(value, tool_name: str, tools) -> dict:
    """v0.9.7: coerce a non-dict tool-call ``parameters`` into a kwargs dict.

    Some LLMs emit a bare scalar (e.g. ``"http://ip-api.com/json/"``) instead of
    ``{param: value}`` for a single-argument tool. If the named tool declares
    exactly one parameter, wrap the scalar as ``{that_param: value}``; otherwise
    return ``{}`` (the tool's required-arg guard will then surface a clear error
    rather than the runtime crashing on ``parameters.keys()``).
    """
    names: list = []
    for t in (tools or []):
        if getattr(t, "name", None) == tool_name:
            names = list(getattr(t, "parameter_names", []) or [])
            if not names:
                from systemu.core.schema_utils import schema_param_names
                schema = getattr(t, "parameters_schema", {}) or {}
                if isinstance(schema, dict):
                    names = schema_param_names(schema)
            break
    if len(names) == 1:
        return {names[0]: value}
    return {}


def _legacy_autodeny_applies(tool_name: str) -> bool:
    """v0.9.32 (D.5): the pre-gate headless auto-deny path applies only to
    NON-shell destructive tools. Shell tools (run_command / run_cli_command)
    are gated at the ToolSandbox chokepoint (posts a command gate + raises
    PendingOperatorDecision, which the workflow lane parks/resumes), so the
    legacy confirm()/auto-deny must NOT pre-empt them."""
    from systemu.runtime.tool_sandbox import _SHELL_TOOL_NAMES
    return tool_name not in _SHELL_TOOL_NAMES


import re as _re

_PKG_TOKEN_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


def _install_hint(missing_packages: list) -> str:
    """Return an actionable install hint, never the misleading 'pip install unknown'.

    If the first entry looks like a real package token, suggest pip; otherwise
    (it's a human fallback phrase, or the list is empty) point at the manifest.
    """
    pkg = (missing_packages or [None])[0]
    if pkg and _PKG_TOKEN_RE.match(pkg):
        return f"pip install {pkg}"
    return "see the tool's manifest"


def _resolve_missing_packages(result_missing, declared) -> list:
    """Pick the most honest missing-package list for operator messaging.

    Priority: the tool result's own missing_packages → the tool's declared
    manifest dependencies → a clear human phrase. NEVER returns ['unknown'].
    """
    if result_missing:
        return list(result_missing)
    if declared:
        return list(declared)
    return ["a required package (see tool manifest)"]


def _dep_failure_messages(
    *,
    tool_name:        str,
    error_type:       str,
    missing_packages: list,
    hint:             str,
    pip_tail:         Optional[str] = None,
) -> tuple[str, str, str]:
    """Build (LLM-facing, operator-facing, op-log-level) messages for a dep-failure result.

    Centralised so the four error_types stay consistent in tone and
    actionability.  The LLM message always tells the Shadow "do not retry,
    FAIL the objective" — the variability is in *why* and in what the
    operator should do.
    """
    pkgs = ", ".join(missing_packages) if missing_packages else "a required package (see tool manifest)"
    if error_type == "missing_dependency":
        return (
            f"Tool '{tool_name}' cannot run: Python package '{pkgs}' is not installed "
            f"and the tool manifest does not declare it. This tool is permanently "
            f"unavailable for this execution. Do not call it again. Issue a FAIL "
            f"action for any objective that requires it.",
            f"Tool '{tool_name}' failed — undeclared missing package '{pkgs}'. "
            f"Add it to the tool's manifest and install with: {hint}",
            "WARNING",
        )
    if error_type == "dependency_install_pending_approval":
        return (
            f"Tool '{tool_name}' cannot run: it requires Python package(s) '{pkgs}' "
            f"which need operator approval before installing. This tool is "
            f"permanently unavailable for this execution. Do not call it again. "
            f"Issue a FAIL action for any objective that requires it.",
            f"Tool '{tool_name}' is awaiting operator approval to install: {pkgs}. "
            f"{hint}",
            "WARNING",
        )
    if error_type == "dependency_install_blocked":
        return (
            f"Tool '{tool_name}' cannot run: dependency installation is disabled in "
            f"this environment, and required package(s) '{pkgs}' are not pre-installed. "
            f"This tool is permanently unavailable for this execution. Do not call "
            f"it again. Issue a FAIL action for any objective that requires it.",
            f"Tool '{tool_name}' blocked: install mode is OFF and package(s) '{pkgs}' "
            f"are missing. Bake them into the base image or enable "
            f"SYSTEMU_TOOL_DEP_INSTALL_MODE=prompt.",
            "ERROR",
        )
    # dependency_install_failed
    extra = f" (pip stderr tail: {pip_tail[:200]})" if pip_tail else ""
    return (
        f"Tool '{tool_name}' cannot run: automatic install of '{pkgs}' failed. "
        f"This tool is permanently unavailable for this execution. Do not call it "
        f"again. Issue a FAIL action for any objective that requires it.",
        f"Tool '{tool_name}' install failed for {pkgs}{extra}. Investigate network / "
        f"environment and retry. {hint}",
        "ERROR",
    )


class ShadowRuntime:
    """Runs a Shadow through an Activity's Scroll using the agentic loop.

    Args:
        config:       Config carrying OpenRouter key + tier model names.
        vault:        Vault instance for entity lookups and persistence.
        executions_dir: Path where execution snapshots are persisted.
    """

    @staticmethod
    def _init_subagent_depth(config) -> int:
        """v0.9.33 Bug 3: this runtime's nesting depth from its config (0 for a
        parent). Thin wrapper over the pure module helper so tests can stamp
        depth via ``ShadowRuntime.__new__`` without standing up the sandbox."""
        return _runtime_depth_from_config(config)

    def __init__(
        self,
        config: Config,
        vault:  Vault,
        executions_dir: Optional[Path] = None,
        audit_namespace: Optional[Path] = None,
    ):
        self.config        = config
        self.vault         = vault
        # v0.10.0 Item 1: when set (by SubagentFleet for a child), action-audit
        # writes route to this per-child namespace instead of the shared global
        # audit log. There is NO corruption risk under the asyncio-gather fleet
        # model (synchronous vault writes can't interleave across coroutines); this
        # provides semantic isolation so child audits stay cleanly separable.
        self._audit_namespace = audit_namespace
        # v0.9.33 Bug 3: nesting depth of THIS runtime. The parent runs at 0;
        # SubagentFleet stamps the child config with an incremented depth so the
        # depth guard (harness_arbiter._arbitrate_subagent) and the v2 delegation
        # refusal (in _handle_tool_call) both see real nesting.
        self._subagent_depth = self._init_subagent_depth(config)
        # v0.9.1.1 fix: load user_profile once at init so _resolve_verifier_output_dir
        # can actually prefer user_profile.default_output_dir over config.output_dir.
        try:
            self.user_profile = vault.get_user_profile() if vault is not None else None
        except Exception:
            self.user_profile = None
        _vault_root = Path(config.vault_dir).resolve()
        # Pick the backend from config (resolved at Config.from_env() time
        # from SYSTEMU_TOOL_BACKEND; defaults to "local").
        _backend_is_docker = (config.tool_backend == "docker")

        # Resolve dependency-installer policy once per Shadow runtime so the
        # sandbox + registry agree on InstallMode/approvals.  In docker
        # backends the registry isn't attached and the installer is dormant;
        # we still resolve the mode so a future docker-side hook can read it.
        from systemu.runtime.dependency_installer import resolve_install_mode
        from systemu.runtime.dep_approvals import init_default_store
        install_mode = resolve_install_mode(
            config_mode=getattr(config, "tool_dep_install_mode", None),
            systemu_mode=getattr(config, "systemu_mode", None),
        )
        # Approval store lives alongside other runtime state (data/).  When
        # the operator hasn't run anything yet the file is created lazily on
        # first approve/record_pending call.
        approvals = init_default_store(Path("data"))

        self.sandbox       = ToolSandbox(
            vault_root=_vault_root,
            backend=config.tool_backend,
            default_timeout=(
                config.docker_tool_timeout if _backend_is_docker else 30
            ),
            install_mode=install_mode,
            approvals=approvals,
            vault=vault,         # v0.9.1: T8 must-wire — enables _after_successful_call audit hook
            config=config,       # v0.9.1: T8 must-wire — enables max_result_size_chars truncation
        )
        # Attach ToolRegistry for the direct-call fast path (avoids subprocess overhead
        # and fixes path-resolution issues with relative vault_dir configurations).
        # The fast path is only safe for the in-process local backend; Docker / SSH /
        # WSL backends always go through the sandbox protocol.
        if not _backend_is_docker:
            from systemu.runtime.tool_registry import ToolRegistry
            _impl_dir = _vault_root / "tools" / "implementations"
            self.sandbox.attach_registry(
                ToolRegistry(
                    _impl_dir, vault,
                    install_mode=install_mode,
                    approvals=approvals,
                )
            )
        self.executions_dir = (
            executions_dir or Path(config.vault_dir) / "executions"
        )
        # Tools that returned a dep-related error during this execution.
        # Mapped to the list of packages that blocked them so we can clear the
        # suppression precisely when an approval lands (v0.3.6 no-restart fix).
        # Older code paths that used this as a set still see truthy membership
        # via ``tool_name in self._dep_failed_tools``.
        self._dep_failed_tools: dict[str, list[str]] = {}
        # v0.4.0-b: per-tool consecutive-failure counter for in-loop reflection.
        # Reset whenever the same tool succeeds.
        self._consec_tool_fails: dict[str, int] = {}
        # v0.8.16: canonical trigger origin for every event this runtime
        # publishes.  Defaults to "manual"; `execute()` resets it from the
        # passed `origin` (or the activity's stamped origin) at the top of a run.
        self._origin: str = "manual"
        # v0.8.17: consecutive degraded web-search counter; reset per run in execute().
        self._consec_degraded_search: int = 0
        # v0.8.21: stuck-loop guard counters; reset at top of execute() like _consec_degraded_search.
        self._iters_since_obj_credit: int = 0
        self._same_tool_fail_streak: dict[str, int] = {}
        self._stuck_round_for_obj: dict[int, int] = {}
        # W6.3: EVERY tool called since the last objective credit — not just
        # failing ones. The stuck ask reported "Tools tried: (none)" while
        # fetch_json had been called repeatedly, because lying-success calls
        # reset the failure streak and vanished from the report.
        self._tools_since_credit: set[str] = set()
        # W12 (F9): tool → objective id claimed on a FAILED call; a later
        # success of the same tool nudges the model to re-claim. Reset per run.
        self._failed_objective_claims: dict[str, int] = {}
        self._operator_hint: "str | None" = None
        # v0.9.8 Phase 2: autonomous-coach self-steer counter; reset per run in execute().
        self._coach_steers_used: int = 0
        # v0.9.1 (Layer 4): per-objective verifier state + fresh-work flag.
        # Reset per run in execute(); mutated during completes_objective path.
        self._objective_states: dict[int, ObjectiveState] = {}
        self._fresh_work_since_last_verifier_call: bool = False
        # Directory is created lazily when the vault's prune_old_executions needs it;
        # we no longer eagerly create it since snapshot/SKILL.md disk writes are removed.

        # v0.9.3: discover code-registered tools at runtime startup so the
        # main loop + verifier fork can use them.
        _discover_v2_tools()

    # ─────────────────────────────────────────────────────────────────────────

    def _stamp(self, event: dict) -> dict:
        """v0.8.16: stamp the canonical trigger origin onto an event payload.

        ``setdefault`` so an event that already carries an explicit ``origin``
        is never clobbered.  Used to wrap every EventBus publish so the
        origin-partitioned live panes can filter on ``event["origin"]``.
        """
        event.setdefault("origin", getattr(self, "_origin", "manual"))
        return event

    # ─────────────────────────────────────────────────────────────────────────

    def _iteration_event(
        self,
        *,
        iteration,
        decision,
        tool_result=None,
        execution_id=None,
        llm_ref=None,
    ) -> dict:
        """v0.8.16: build a bounded per-iteration event with expandable details.

        The ``details`` dict carries the reasoning + tool I/O the live panes
        render on expand.  ``tool_result`` is truncated (≤4000 chars) and the
        raw LLM completion is NOT inlined — only referenced by ``llm_ref``
        ({exec_id, call_index}) for lazy load from the per-execution transcript.
        """
        d = decision or {}
        action = d.get("action", "?")
        message = f"iter={iteration} {action}"
        if action == "TOOL_CALL":
            message += f" {d.get('tool_name', '')}"
        return self._stamp({
            "ts":       utcnow().isoformat() + "Z",
            "level":    "INFO",
            "category": "runtime",
            "message":  message,
            "context":  {"execution_id": execution_id},
            "details": {
                "reasoning":   d.get("reasoning") or d.get("thought"),
                "action":      action,
                "tool_name":   d.get("tool_name"),
                "tool_params": d.get("parameters"),
                "tool_result": (str(tool_result)[:4000] if tool_result is not None else None),
                "llm_ref":     llm_ref,
            },
        })

    # ─────────────────────────────────────────────────────────────────────────

    def _gate3_check(self, tool) -> "dict | None":
        """Return structured error dict if the tool can't be invoked, else None.

        v0.6.9: messages now point operators to the dashboard recovery URL
        instead of the misleading "Re-forge with feedback" instruction —
        most blockers are dep approval / dry-run re-runs, not re-forges.
        """
        from systemu.recovery.links import recover_url
        if not getattr(tool, "enabled", False):
            return {
                "reason": "GATE_3_DISABLED",
                "action_url": recover_url("tool", tool.id),
                "message": (
                    f"Tool {tool.name} is disabled. "
                    f"Apply the fix at {recover_url('tool', tool.id)}"
                ),
            }
        if getattr(tool, "dry_run_status", None) == "failed":
            ev = getattr(tool, "dry_run_evidence", None) or {}
            classified = ev.get("classified_reason", "DRY_RUN_FAILED_BUG")
            return {
                "reason": classified,
                "missing_package": ev.get("missing_package"),
                "action_url": recover_url("tool", tool.id),
                "message": (
                    f"Tool {tool.name} dry-run failed ({classified}). "
                    f"Apply the fix at {recover_url('tool', tool.id)}"
                ),
            }
        return None

    CIRCUIT_BREAKER_FAILURES = 3  # v0.6.9: bail after N consecutive same-tool same-reason failures

    def _record_tool_failure(self, tool_name: str, reason: str) -> bool:
        """v0.6.9 iteration-loop circuit breaker.

        Append a failure to the consecutive-failures window. Returns True
        when the circuit is now tripped (caller should bail the iteration
        loop with a useful summary that points to the recovery URL).

        The window resets on any change in (tool_name, reason): a different
        tool or a different failure class indicates the LLM is exploring,
        not stuck in a retry loop.
        """
        if not hasattr(self, "_consecutive_failures"):
            self._consecutive_failures = []
        key = (tool_name, reason)
        if self._consecutive_failures and self._consecutive_failures[-1] != key:
            self._consecutive_failures = []
        self._consecutive_failures.append(key)
        tripped = len(self._consecutive_failures) >= self.CIRCUIT_BREAKER_FAILURES
        if tripped and not _is_transient_reason(reason):
            # Fix 2: a tool that structurally/persistently fails (non-transient)
            # won't be fixed by re-running — record it so the terminal flags the
            # run structural and the supervisor skips the retry storm.
            if not hasattr(self, "_structural_tool_failures"):
                self._structural_tool_failures = set()
            self._structural_tool_failures.add(tool_name)
        return tripped

    def _structural_failure(self) -> bool:
        """True iff a tool structurally/persistently failed (non-transient
        circuit trip) — re-running won't help. The terminal stamps the result
        with this so the supervisor skips the retry storm."""
        return bool(getattr(self, "_structural_tool_failures", None))

    # ─────────────────────────────────────────────────────────────────────────
    # v0.8.21 — stuck-loop guard helpers (pure; wired into execute() in T6).
    # ─────────────────────────────────────────────────────────────────────────

    def _update_stuck_counters(self, *, action: str, tool_name: "str | None",
                                 tool_success: "bool | None", credited_obj_id: "int | None") -> None:
        """v0.8.21: per-iteration counter update.
        Progress (objective credited) resets BOTH counters.
        TOOL_CALL failure increments same_tool_fail_streak.
        Any iteration without a credit increments iters_since_obj_credit."""
        if credited_obj_id is not None:
            self._iters_since_obj_credit = 0
            self._same_tool_fail_streak.clear()
            self._tools_since_credit.clear()
            return
        self._iters_since_obj_credit += 1
        if action == "TOOL_CALL" and tool_name:
            # W6.3: record the attempt regardless of reported success, so the
            # stuck ask's "Tools tried" is truthful even for calls that
            # "succeeded" without producing progress.
            self._tools_since_credit.add(tool_name)
            if tool_success:
                self._same_tool_fail_streak[tool_name] = 0
            else:
                self._same_tool_fail_streak[tool_name] = \
                    self._same_tool_fail_streak.get(tool_name, 0) + 1

    def _tools_tried_since_credit(self) -> "list[str]":
        """W6.3: every tool attempted since the last objective credit, for the
        stuck ask's "Tools tried" line — union of the all-attempts set and any
        active failure streaks (belt-and-braces for resumed runs)."""
        attempted = set(getattr(self, "_tools_since_credit", set()) or set())
        attempted |= {k for k, v in self._same_tool_fail_streak.items() if v > 0}
        return sorted(attempted)

    def _stuck_trigger(self) -> "tuple[bool, str]":
        """v0.8.21: hybrid trigger — no-progress OR same-tool-failure streak."""
        no_progress, tool_fails, guard_on = _stuck_thresholds()
        if not guard_on:
            return (False, "")
        if self._iters_since_obj_credit >= no_progress:
            return (True, f"no objective credit for {self._iters_since_obj_credit} iterations")
        worst = max(self._same_tool_fail_streak.items(),
                    key=lambda kv: kv[1], default=(None, 0))
        if worst[1] >= tool_fails:
            return (True, f"tool '{worst[0]}' failed {worst[1]} consecutive times")
        return (False, "")

    def _ask_stuck_or_degrade(self, *, execution_id, current_objective,
                                 tools_tried, reason: str,
                                 scroll_id: str = "", activity_id: str = "",
                                 shadow_id: str = ""):
        """v0.8.21: post stuck-loop decision via v0.8.19 R3 request_choice.
        Returns the answer dict on resume, None when no queue (headless),
        raises PendingChoiceRequest while awaiting operator.
        v0.8.22.1 (R2): dedup_key is execution-INDEPENDENT (keyed by scroll +
        objective + round) so a resumed run reaches the same decision. (R4):
        the decision context carries the resume coordinates."""
        round_n = self._stuck_round_for_obj.get(current_objective.id, 0) + 1
        self._stuck_round_for_obj[current_objective.id] = round_n
        dedup = f"stuck:{scroll_id or execution_id}:obj_{current_objective.id}:r{round_n}"
        goal_short = (getattr(current_objective, "goal", "") or "")[:120]
        tried = ", ".join(sorted(set(tools_tried or [])))
        qs = [{
          "id": "action",
          "prompt": (f"Stuck on Objective {current_objective.id}: '{goal_short}'.  "
                     f"{reason}. Tools tried: {tried or '(none)'}."),
          "multi": False,
          "options": [
            {"label": "Provide hint",   "desc": "free-text suggestion folded into next iteration"},
            {"label": "Accept partial", "desc": "finalize with completed objectives; mark this as N/A"},
            {"label": "Cancel run",     "desc": "stop the run cleanly"},
          ],
          "allow_free_text": True,
        }]
        from systemu.interface.notifications import request_choice
        return request_choice(qs, dedup_key=dedup, extra_context={
            "execution_id": execution_id,
            "activity_id":  activity_id,
            "scroll_id":    scroll_id,
            "shadow_id":    shadow_id,
            "objective_id": current_objective.id,
            "stuck_round":  round_n,
        })

    def _finalize_stuck(self, *, context, status: str, reason: str,
                          stuck_on: int, completed, iteration: int,
                          tool_calls_made: int, scroll, shadow,
                          execution_id: str, exec_start: float,
                          total_objectives: int):
        """v0.8.21: terminal finalize for stuck-loop. Mirrors the MaxIterations path
        (build_result + telemetry + refinery + shadow-log) so downstream consumers
        treat 'partial' / 'cancelled' here identically."""
        _observe_best_effort(
            "stuck-loop shadow-log append",
            lambda: self._append_to_shadow_log(
                shadow, execution_id, status, f"Stuck-loop: {reason}",
                iteration_count=iteration, tool_calls_made=tool_calls_made,
                objectives_completed=len(completed or []),
                objectives_total=total_objectives,
                duration_seconds=(__import__("time").time() - exec_start),
            ),
        )
        # v0.8.22.1 (Fix 3): a deliberate operator cancel is not a system "stuck"
        # failure — don't mislabel it or stamp it with the StuckLoopDetected error.
        if status == "cancelled":
            _summary = f"Run cancelled by operator (was working on objective {stuck_on})."
            _err = None
        else:
            _summary = f"Stuck on objective {stuck_on}: {reason}"
            _err = "StuckLoopDetected"
        res = context.build_result(
            status=status,
            final_summary=_summary,
            error=_err,
        )
        _observe_best_effort(
            "stuck-loop terminal telemetry",
            lambda: _record_terminal_telemetry(
                shadow=shadow, execution_id=execution_id, scroll=scroll,
                status=status, iteration=iteration,
                extra={"reason": "StuckLoopDetected",
                       "stuck_on_objective": stuck_on},
            ),
        )
        _observe_best_effort(
            "stuck-loop refinery dispatch",
            lambda: _dispatch_refinery(
                shadow, scroll, res, context, self.config, self.vault),
        )
        # v0.9.2: episodic capture — best-effort, never raises
        _trigger_episodic_capture(
            vault=getattr(self, 'vault', None),
            config=getattr(self, 'config', None),
            session_id=execution_id,
            intent=getattr(scroll, "intent", ""),
            chat_result=None,
            files_produced=[],
            status=status,
            execution_id=execution_id,
        )
        return res

    def _apply_stuck_answer(self, stuck_obj, ans: dict, *, finalize):
        """v0.8.22.1 (R6): map a resolved stuck answer to an action.
        Returns ("continue", None) to keep looping (hint applied), or
        ("finalize", <result>) when the answer is partial/cancel.
        `finalize` is a callable(**kwargs) -> result (the caller binds the
        _finalize_stuck context/scroll/shadow/etc.)."""
        action_choice = (ans or {}).get("action") or ""
        _canonical = {"Provide hint", "Accept partial", "Cancel run"}
        if action_choice in _canonical:
            hint_text = ""
        else:
            hint_text = action_choice.strip()
            action_choice = "Provide hint" if hint_text else action_choice
        if action_choice == "Provide hint" and hint_text:
            self._operator_hint = (
                f"## Operator hint (use to retry Objective {stuck_obj.id})\n{hint_text}"
            )
            self._iters_since_obj_credit = 0
            self._same_tool_fail_streak.clear()
            self._tools_since_credit.clear()
            return ("continue", None)
        if action_choice == "Accept partial":
            return ("finalize", finalize(status="partial"))
        if action_choice == "Cancel run":
            return ("finalize", finalize(status="cancelled"))
        # ambiguous → treat as partial
        return ("finalize", finalize(status="partial"))

    def _apply_materialised_grant(
        self,
        mat: Dict[str, Any],
        *,
        context,
        tools,
        tool_index,
        current_ab,
        iter_budget: int,
    ) -> int:
        """Apply a Governor ``materialise()`` outcome into THIS run.

        Shared between the autonomous Governor GRANT path and the (deferred)
        harness grant-resume replay so the two are byte-identical — resume
        *applies* the operator's authoritative verdict, it never re-arbitrates.

        Branches on the materialise dict's discriminating key:
          * TOOL (``mat["tool"]``) — resolve → deploy inline (dry-run →
            DEPLOYED+enabled) → append to the live ``tools`` / ``tool_index`` so
            it is callable in THIS run; observation.
          * COMPUTE (``mat["compute_grant"]``) — extend ``iter_budget`` by the
            granted ``extra_iterations`` (clamped 0..100); observation.
          * SKILL / ACCESS / SUBAGENT — observation only (parity with the
            autonomous path, which narrates these today).
          * MCP (``mat["mcp"]``) — register the discovered tools into the LIVE
            v2 registry via ``registry_bridge.register_server_tools`` (namespaced
            ``mcp__server__tool``); observation lists the now-callable names
            derived from that call's RETURN. Does NOT touch v1 ``tools`` /
            ``tool_index`` (the v2 catalog picks the registered tools up).
          * failure (``mat["materialised"]`` falsy) — harness_grant_failed
            observation carrying ``mat["reason"]`` and the request's fallback
            (the caller stamps ``mat["fallback"]`` before calling).

        Returns the possibly-updated ``iter_budget``.
        """
        if mat.get("materialised"):
            # v0.9.7 Phase 3: the Governor materialises one of
            # several harness KINDs; apply each into THIS run.
            if mat.get("tool") is not None:
                # ── TOOL: resolve → deploy inline → offer back ──
                _tref = mat.get("tool")
                _nt = None
                for _resolve in (
                    lambda: self.vault.get_tool(_tref),
                    lambda: self.vault.find_tool_by_name(_tref),
                ):
                    try:
                        _nt = _resolve()
                        if _nt is not None:
                            break
                    except Exception:
                        _nt = None
                # v0.9.7 Phase 2: deploy the freshly-forged tool
                # synchronously (dry-run → DEPLOYED + enabled) so it
                # is callable in THIS run, not just a future one.
                _dryrun_reason = None
                if _nt is not None and not getattr(_nt, "enabled", False):
                    try:
                        from systemu.pipelines.tool_deploy import deploy_forged_tool
                        _dep = deploy_forged_tool(_nt.id, self.vault, self.config)
                        if _dep.get("deployed"):
                            try:
                                _nt = self.vault.get_tool(_nt.id)
                            except Exception:
                                pass
                        else:
                            # v0.9.34.3: surface the dry-run failure so the agent
                            # can repair its tool on a re-request. It was discarded
                            # before, so the agent re-forged the same broken schema
                            # and failed instead of fixing it.
                            _dryrun_reason = _dep.get("reason")
                    except Exception as _exc:
                        _dryrun_reason = f"deploy raised: {_exc}"
                        logger.debug("[Runtime] forge-deploy failed", exc_info=True)
                if _nt is not None and getattr(_nt, "enabled", False):
                    tools.append(_nt)
                    tool_index.append({
                        "id": _nt.id, "name": _nt.name,
                        "description": _nt.description,
                        "parameter_names": list(getattr(_nt, "parameter_names", []) or []),
                        "parameters_schema": dict(getattr(_nt, "parameters_schema", {}) or {}),
                    })
                    context.add_observation({
                        "type": "harness_granted",
                        "message": f"Capability provisioned and ready: '{_nt.name}'. You may call it now.",
                        "tool": _nt.name,
                    }, current_ab)
                else:
                    context.add_observation({
                        "type": "harness_granted_pending",
                        "message": (
                            f"Capability '{getattr(_nt, 'name', _tref)}' was forged but FAILED its "
                            "automatic dry-run, so it is not callable this run. "
                            + (f"Dry-run error: {_dryrun_reason}. " if _dryrun_reason else "")
                            + "If you request this capability again, FIX the cause first — most often the "
                            "implementation's parameters must match the declared parameters_schema (same "
                            "names; the schema must not require a parameter the function does not accept). "
                            "Otherwise use an existing tool or FAIL."
                        ),
                    }, current_ab)
            elif mat.get("compute_grant"):
                # ── COMPUTE: extend THIS run's iteration budget ──
                _cg = mat.get("compute_grant") or {}
                _extra_it = max(0, min(int(_cg.get("extra_iterations", 0) or 0), 100))
                if _extra_it:
                    iter_budget += _extra_it
                context.add_observation({
                    "type": "harness_granted",
                    "message": (
                        f"Compute granted: +{_extra_it} iteration(s) "
                        f"(budget now {iter_budget}). Continue toward the goal."
                    ),
                }, current_ab)
            elif mat.get("skill"):
                # ── SKILL: procedure authored to the vault ──
                context.add_observation({
                    "type": "harness_granted",
                    "message": (
                        f"Skill provisioned: {mat.get('skill')}. Its procedure is now "
                        "available — follow it to complete the task."
                    ),
                }, current_ab)
            elif mat.get("access"):
                # ── ACCESS: a scoped capability lease was recorded ──
                # Single-owner backend (by design): the lease is ADVISORY — no
                # sandbox boundary is enforced locally. Tell the agent the truth
                # so it does not believe a non-existent boundary authorizes the
                # op; it proceeds with its EXISTING tools (Bug 5 / D.1).
                context.add_observation({
                    "type": "harness_granted",
                    "message": (
                        f"Access lease recorded (advisory on the local single-owner "
                        f"backend — no sandbox boundary is enforced): {mat.get('access')}. "
                        "Proceed using your existing tools."
                    ),
                }, current_ab)
            elif mat.get("subagent"):
                # ── SUBAGENT: delegation capability granted ──
                # v0.9.38 Bug 13: TERMINAL framing (mirrors the native fleet
                # branch). The old "decompose and proceed" wording invited the
                # agent to keep issuing kind=subagent requests, which on the
                # escalate→suspend→approve→resume path looped until the request
                # cap / resume budget ran out and the run ended parked
                # (suspended_harness_escalation), never finalizing or
                # reconciling. Tell it to PROCEED and COMPLETE, not re-request.
                _sa = mat.get("subagent") or {}
                context.add_observation({
                    "type": "harness_granted",
                    "message": (
                        "Sub-agent delegation granted for: "
                        f"{str(_sa.get('task', ''))[:160]}. Proceed with the work and "
                        "COMPLETE the objective now — do NOT request more sub-agents; "
                        "request another only for a distinct, named sub-task you "
                        "genuinely cannot do yourself."
                    ),
                    "fleet": {"terminal": True},
                }, current_ab)
            elif mat.get("mcp"):
                # ── MCP: register discovered tools into the LIVE v2 registry ──
                # (namespaced mcp__server__tool via the P2 registry_bridge);
                # _build_llm_tool_catalog picks them up automatically — so we do
                # NOT touch the v1 `tools`/`tool_index` lists here. The
                # observation lists the now-callable names so the agent uses them.
                _mcp = mat.get("mcp") or {}
                _server = str(_mcp.get("server_id") or "")
                # B5: tools are FULL dicts {name, description, parameters_schema,
                # annotations}. Pass them POSITIONALLY (vault FIRST) and derive
                # the callable names from register_server_tools' RETURN value —
                # never reconstruct `mcp__server__tool` ourselves (the slug may
                # differ; the budget may register fewer than discovered).
                _tool_dicts = list(_mcp.get("tools") or [])
                _registered: list = []
                try:
                    from systemu.runtime.mcp.sdk.registry_bridge import (
                        register_server_tools,
                    )
                    _registered = register_server_tools(
                        self.vault, _server, _tool_dicts,
                    ) or []
                except Exception:
                    logger.debug("[Runtime] mcp register_server_tools failed",
                                 exc_info=True)
                # v0.9.36 Bug 9: remember the server we registered so the terminal
                # finalize can tear it down even when the lease-keyed revoke can't
                # reach it (a resumed run's lease lives in the dead pre-suspend
                # Governor). Robust to a __new__-built test runtime (no __init__).
                if _server:
                    try:
                        _reg_set = getattr(self, "_mcp_servers_registered_this_run", None)
                        if _reg_set is None:
                            _reg_set = set()
                            self._mcp_servers_registered_this_run = _reg_set
                        _reg_set.add(_server)
                    except Exception:
                        pass
                # Fall back to the namespaced_name of each discovered tool only
                # if the bridge returned nothing (e.g. a stubbed test) — still
                # the authoritative builder, not a hand-built string.
                _callable = list(_registered)
                if not _callable and _tool_dicts:
                    try:
                        from systemu.runtime.mcp.sdk.registry_bridge import (
                            namespaced_name,
                        )
                        _callable = [namespaced_name(_server, str(t.get("name") or ""))
                                     for t in _tool_dicts if t.get("name")]
                    except Exception:
                        _callable = []
                context.add_observation({
                    "type": "harness_granted",
                    "message": (
                        f"MCP server '{_mcp.get('label') or _server}' connected. "
                        f"Callable tools: {', '.join(_callable) or '(none)'}. "
                        "Call them by their namespaced names now."
                    ),
                    "mcp_server": _server,
                    "mcp_tools": _callable,
                }, current_ab)
            else:
                context.add_observation({
                    "type": "harness_granted",
                    "message": "Capability provisioned. Proceed toward the goal.",
                }, current_ab)
        else:
            _fallback = mat.get("fallback") or ""
            context.add_observation({
                "type": "harness_grant_failed",
                "message": f"Provisioning failed: {mat.get('reason')}. {_fallback or 'Try an alternative or FAIL.'}",
            }, current_ab)
        return iter_budget

    def _apply_harness_grant(
        self,
        payload: Dict[str, Any],
        *,
        context,
        tools,
        tool_index,
        current_ab,
        iter_budget: int,
    ) -> int:
        """Replay an operator-resolved harness grant into THIS (resumed) run.

        ``payload`` is the ``grant_payload`` the daemon harness-grant reconciler
        built once on Approve/Deny (Task 5) and ``resume_after_grant`` stamped onto
        the snapshot as a ``__HARNESS_GRANT__`` note (peeled at resume-start). The
        operator's verdict is AUTHORITATIVE — this method APPLIES it; it never
        re-arbitrates and never re-calls the Governor.

        Routing:
          * DENY  (``payload["denied"]``) → a ``harness_grant_failed``-style
            observation carrying the original request ``fallback`` (peeled from the
            ``__HARNESS_PENDING__`` note); the run proceeds with its fallback.
          * INPUT (kind == "input" / an ``operator_answer`` present) → inject the
            answer as an observation; no helper call (INPUT is not a capability).
          * else → reconstruct a per-kind *materialise dict* from ``payload`` and
            route through the SHARED ``_apply_materialised_grant`` so resume is
            byte-identical to an autonomous GRANT (TOOL deploy+register, COMPUTE
            budget bump, SKILL/ACCESS/SUBAGENT observation, MCP register the
            discovered server tools into the live registry — empty ``mcp`` block
            replays the oauth_pending/non-materialised handoff honestly).

        Returns the possibly-updated ``iter_budget``.
        """
        payload = payload or {}
        _kind = str(payload.get("kind", "") or "").lower()
        _fallback = payload.get("fallback", "") or ""

        # ── DENY: proceed with the agent's fallback (no re-escalate) ──────────
        if payload.get("denied"):
            context.add_observation({
                "type": "harness_grant_failed",
                "message": (
                    "Operator denied the capability request: "
                    f"{payload.get('rationale') or 'no reason given'}. "
                    f"{_fallback or 'Proceed with an alternative approach or FAIL.'}"
                ),
            }, current_ab)
            return iter_budget

        # ── INPUT / ASK_OPERATOR: inject the operator's answer ────────────────
        if _kind == "input" or payload.get("operator_answer") is not None:
            _ans = payload.get("operator_answer", "")
            context.add_observation({
                "type": "harness_granted",
                "message": (
                    "Operator provided the requested input: "
                    f"{_ans}. Use it to continue toward the goal."
                ),
            }, current_ab)
            return iter_budget

        # ── Capability kinds: reconstruct a materialise dict + reuse the helper ─
        mat: Dict[str, Any] = {"materialised": True, "fallback": _fallback}
        if _kind == "tool" or payload.get("granted_tool") or payload.get("tool_id"):
            # _apply_materialised_grant resolves the tool ref via vault.get_tool /
            # find_tool_by_name — prefer the id, fall back to the name.
            mat["tool"] = payload.get("tool_id") or payload.get("granted_tool")
            mat["tool_id"] = payload.get("tool_id")
            mat["lease_id"] = payload.get("lease_id")
        elif _kind == "compute" or payload.get("compute_grant"):
            mat["compute_grant"] = payload.get("compute_grant") or {}
            mat["lease_id"] = payload.get("lease_id")
        elif _kind == "skill" or payload.get("skill"):
            mat["skill"] = payload.get("skill")
            mat["lease_id"] = payload.get("lease_id")
        elif _kind == "access" or payload.get("access"):
            mat["access"] = payload.get("access")
            # No apply patch — advisory lease only (Bug 5 / D.2).
        elif _kind == "subagent" or payload.get("subagent"):
            mat["subagent"] = payload.get("subagent")
        elif _kind == "mcp" or payload.get("mcp"):
            mat["mcp"] = payload.get("mcp")
            mat["lease_id"] = payload.get("lease_id")
            if not mat["mcp"]:
                # oauth_pending / non-materialised handoff replayed honestly
                mat["materialised"] = False
                mat["reason"] = payload.get("reason") or "mcp not materialised"
        else:
            # Unknown/empty grant — narrate generically (helper's no-key branch).
            pass

        return self._apply_materialised_grant(
            mat, context=context, tools=tools, tool_index=tool_index,
            current_ab=current_ab, iter_budget=iter_budget,
        )

    def _resolve_scroll_parameters(self, scroll):
        """v0.9.35 (Phase 3): build the INPUT elicitation request for a
        BROAD-generalized scroll's captured ``parameters``.

        Returns ``None`` when ``scroll.parameters`` is empty (standard/narrow
        scroll => strict no-op, byte-identical execution path). Otherwise returns
        a ``kind=INPUT`` HarnessRequest whose ``requested_schema`` has every slot
        in ``required[]`` and ABSENT from any provided values, with the captured
        value as the editable ``default`` (pinned KEY CONSTRAINT). The
        ``param_substitution`` marker tells the resume path to slot-substitute
        (NOT re-dispatch a tool)."""
        params = list(getattr(scroll, "parameters", None) or [])
        if not params:
            return None
        from systemu.core.models import HarnessRequest, HarnessKind
        from systemu.runtime.param_resolution import slot_schema_from_parameters
        schema = slot_schema_from_parameters(params)
        if not schema.get("properties"):
            return None
        names = ", ".join(p.name for p in params)
        return HarnessRequest(
            kind=HarnessKind.INPUT,
            spec={
                "question": (
                    "This task was recorded with adjustable details. "
                    "Confirm or edit the values below before it runs."
                ),
                "requested_schema": schema,
                # No pending_tool — there is no tool to re-dispatch; the answers
                # are substituted into the objectives/intent/constraints the
                # agent sees.
                "param_substitution": True,
            },
            rationale=f"Confirm recorded parameter(s): {names}.",
            fallback="",
            blocking=True,
        )

    def _stash_scroll_parameters(self, scroll) -> None:
        """Cache a scroll's parameters + constraints so a post-suspend RESUME can
        substitute without reloading the scroll. No-op-safe for paramless scrolls."""
        self._scroll_parameters = list(getattr(scroll, "parameters", None) or [])
        self._scroll_constraints = dict(getattr(scroll, "constraints", None) or {})

    async def _apply_harness_grant_async(
        self,
        payload: Dict[str, Any],
        *,
        context,
        tools,
        tool_index,
        current_ab,
        iter_budget: int,
    ) -> int:
        """Async resume-apply that adds the v0.9.35 (P1) INPUT param-answer
        re-dispatch on top of the sync :meth:`_apply_harness_grant`.

        For an INPUT payload carrying ``param_answers`` + ``pending_tool``:
          * empty ``param_answers`` (Decline / non-coercible) ⇒ a
            ``harness_grant_failed`` observation; the tool is NOT re-dispatched
            (never fabricate a value);
          * otherwise merge ``param_answers`` into ``pending_tool.parameters``
            and RE-DISPATCH the original call through the injected
            ``self._resume_redispatch`` closure (which calls _handle_tool_call →
            re-validates; a still-missing field re-asks; the gate runs once).

        All non-INPUT-param payloads (DENY, plain operator_answer, capability
        kinds) defer to the sync helper byte-for-byte.
        """
        payload = payload or {}
        _kind = str(payload.get("kind", "") or "").lower()
        if _kind == "input" and payload.get("param_substitution"):
            # v0.9.35 (P3): the operator confirmed/edited the recorded scroll
            # parameters. Substitute the chosen values into the live objectives/
            # intent/constraints the agent sees — NO tool re-dispatch.
            from systemu.runtime.param_resolution import substitute_parameters
            _answers = payload.get("param_answers") or {}
            _params = list(getattr(self, "_scroll_parameters", None) or [])
            new_json, new_intent, new_constraints, resolved = substitute_parameters(
                _params, _answers,
                scroll_json=getattr(context, "scroll_json", []) or [],
                intent=getattr(context, "scroll_intent", "") or "",
                constraints=getattr(self, "_scroll_constraints", {}) or {},
            )
            context.scroll_json = new_json
            context.scroll_intent = new_intent
            self._scroll_constraints = new_constraints
            context.add_observation({
                "type": "parameters_resolved",
                "message": (
                    "Operator-confirmed task parameters applied: "
                    f"{resolved}. Use these values."
                ),
                "resolved": resolved,
            }, current_ab)
            return iter_budget
        if (_kind == "input"
                and payload.get("pending_tool")
                and "param_answers" in payload):
            _pending = payload.get("pending_tool") or {}
            _answers = payload.get("param_answers") or {}
            if not _answers:
                context.add_observation({
                    "type": "harness_grant_failed",
                    "message": (
                        "Operator declined to supply the missing parameter(s). "
                        "Use an alternative tool or FAIL — do not fabricate values."
                    ),
                }, current_ab)
                return iter_budget
            _merged = dict(_pending.get("parameters") or {})
            _merged.update(_answers)
            _decision = {
                "tool_name": _pending.get("tool_name", ""),
                "parameters": _merged,
            }
            _redispatch = getattr(self, "_resume_redispatch", None)
            if _redispatch is None:
                # No live re-dispatch closure (legacy caller) — hand the values
                # back so the agent re-issues the call itself.
                context.add_observation({
                    "type": "harness_granted",
                    "message": (
                        "Operator supplied the missing parameter(s): "
                        f"{_answers}. Re-issue the tool call with them."
                    ),
                }, current_ab)
                return iter_budget
            try:
                await _redispatch(_decision)
            except Exception:
                logger.debug("[Runtime] INPUT re-dispatch failed", exc_info=True)
                context.add_observation({
                    "type": "harness_grant_failed",
                    "message": ("Re-dispatch of the completed tool call failed; "
                                "retry it yourself or FAIL."),
                }, current_ab)
            return iter_budget
        # Everything else: identical to the sync path.
        return self._apply_harness_grant(
            payload, context=context, tools=tools, tool_index=tool_index,
            current_ab=current_ab, iter_budget=iter_budget,
        )

    def _build_memory_context_for_prompt(self) -> str:
        """LLM-facing memory view (consolidated, not the raw execution_log).
        v0.6.9: also includes refined lessons from memory_buffer, filtered
        for resolved causes.
        v0.7-g: buffer comes via the configurable memory backend (defaults to
        filesystem, lifts the existing vault layout — operators can switch to
        Mem0 via SYSTEMU_MEMORY_BACKEND=mem0)."""
        from systemu.runtime.memory_consolidator import MemoryConsolidator
        log = self.shadow.execution_log or []
        try:
            from systemu.runtime.memory_backends import get_backend
            backend = get_backend(getattr(self, "config", None))
            buffer_entries = backend.load_buffer(self.shadow.id)
        except Exception:
            # Fall back to the legacy vault path if backend init fails
            try:
                buffer_entries = self.vault.load_shadow_memory(self.shadow.id)[1]
            except Exception:
                buffer_entries = []
        return MemoryConsolidator().consolidate_with_buffer(
            execution_log=log, buffer_entries=buffer_entries or [], vault=self.vault,
        )

    # ─────────────────────────────────────────────────────────────────────────

    async def execute(
        self,
        shadow:   Shadow,
        activity: Activity,
        *,
        dry_run: bool = False,
        cancel_event: Optional[threading.Event] = None,
        resume_from_execution_id: Optional[str] = None,
        root_execution_id: Optional[str] = None,
        origin: Optional[str] = None,
        chat_submission_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a Shadow through its assigned Activity.

        Args:
            shadow:        The Shadow persona to execute under.
            activity:      The Activity to execute (carries scroll_id + tool_ids).
            dry_run:       If True, no tools are actually invoked — prints plan only.
            cancel_event:  Optional threading.Event set by the Supervisor watchdog to
                           request clean cancellation.  Checked at the top of each
                           loop iteration — shadow exits with status="cancelled".
            origin:        v0.8.16 — canonical trigger origin threaded from the
                           Supervisor queue payload.  Falls back to the activity's
                           own ``origin`` field, then "manual".  Stamped onto every
                           event this run publishes so the origin-partitioned live
                           panes can filter on it.

        Returns:
            Execution result dict with status, summary, snapshots_taken, etc.
        """
        # v0.8.16: resolve + remember the trigger origin for the whole run so
        # `_stamp` can tag every published event.
        self._origin = origin or getattr(activity, "origin", None) or "manual"
        # v0.8.17: reset per-run consecutive-degraded-search counter.
        self._consec_degraded_search = 0
        # v0.8.21: reset stuck-guard counters per run (declared in __init__).
        self._iters_since_obj_credit = 0
        self._same_tool_fail_streak.clear()
        self._tools_since_credit.clear()
        self._stuck_round_for_obj.clear()
        self._operator_hint = None
        # W12 (audit F9): objective claims consumed by FAILED tool calls —
        # when the same tool later succeeds WITHOUT re-claiming, the model is
        # nudged to re-state the claim (the A2 run finished its deliverable
        # at iter=12 but never re-claimed; the watchdog cancelled a finished
        # run and the retry re-did paid work).
        self._failed_objective_claims = {}
        # v0.9.8 Phase 2: reset the autonomous-coach self-steer counter per run.
        self._coach_steers_used = 0
        # v0.9.8 (B2): consecutive read-only research tool calls (web_search/
        # web_read/web_extract/fetch_json) with NO deliverable written. Independent
        # of objective-credit (which audit evidence keeps resetting), so it catches
        # the "research forever, never write" loop that loops to MAX_ITERATIONS.
        self._consec_research_reads = 0
        self._research_loop_steers_used = 0
        self._resume_stuck_answer = None  # v0.8.22.1 (R6): (obj_id, answer) lifted from snapshot
        self._resume_harness_grant = None  # v0.9.7 grant-resume: payload lifted from snapshot
        # v0.9.1 (Layer 4): reset verifier bookkeeping per run.
        self._objective_states.clear()
        self._fresh_work_since_last_verifier_call = False
        # v0.9.0 (Layer 1): one-block user context computed once per run.
        # Prompt assembly can read self._user_context_block; Layer 2 will
        # extend this with episodic memory.
        self._user_context_block = _build_user_context_block(self.vault)
        # v0.8.22 (C): carry chat_submission_id for the run so the R3 producers
        # can thread it into OperatorDecision.context, enabling the chat UI to
        # surface decisions inline.
        from systemu.runtime.chat_submission_ctx import set_chat_submission_id
        self._chat_submission_id = chat_submission_id
        self._chat_submission_token = set_chat_submission_id(chat_submission_id)
        try:
            execution_id = _gen_execution_id()
            # v0.9.34 P0 (H3): scope MCP "Trust for session" to THIS run so a
            # trust grant cannot leak across runs (mcp_session_key bakes the id
            # into its hash). Resolved from the run id here — NOT from any
            # LLM-supplied tool kwarg.
            from systemu.runtime.mcp_run_ctx import set_mcp_session_id
            self._mcp_session_token = set_mcp_session_id(execution_id)
            # v0.9.52: carry the run's execution_id so a command gate posted mid-run
            # can stamp it into the decision context → the parked gate is resumable.
            from systemu.runtime.chat_submission_ctx import set_execution_id
            self._execution_id_token = set_execution_id(execution_id)
            exec_start   = __import__("time").time()
            tool_call_count = 0
            # v0.9.33 Bug 2/3: per-execution harness-request counter. Threaded
            # into Governor.arbitrate so the per-run cap (max_requests_per_run)
            # actually fires. Restored from a resume snapshot below if present.
            harness_requests_this_run = 0
            # v0.9.39 Bug 15: the RUN-TREE id. Fresh top-level run → self is the
            # root; a sub-agent child → inherited via the explicit param; a resume
            # → refined from the snapshot below. The cap + outcome reconciliation
            # key off this so they span the whole tree, not one execution.
            root_eid = root_execution_id or execution_id
            logger.info(
                "[Runtime] Starting execution %s — shadow='%s' activity='%s'",
                execution_id, shadow.name, activity.name,
            )

            # ── Load entities from vault ──────────────────────────────────────────
            scroll = self.vault.get_scroll(activity.scroll_id)
            # v0.9.35 (P3): cache recorded scroll PARAMETERS + constraints so a
            # post-suspend RESUME can substitute without reloading the scroll.
            # No-op-safe for standard/narrow scrolls (empty parameters).
            self._stash_scroll_parameters(scroll)
            tools  = self._load_tools(activity.required_tool_ids, dry_run=dry_run)
            skills = self._load_skills(activity.required_skill_ids)

            # ── Determine execution mode: objectives (new) or action_blocks (legacy) ─
            use_objectives = bool(scroll.objectives)
            objectives     = scroll.objectives if use_objectives else []
            scroll_json    = [obj.model_dump(mode="json") for obj in objectives] if use_objectives \
                             else [ab.model_dump(mode="json") for ab in scroll.action_blocks]

            if use_objectives and not objectives:
                return {"status": "failure", "error": "Scroll has no objectives", "execution_id": execution_id}
            if not use_objectives and not scroll.action_blocks:
                return {"status": "failure", "error": "Scroll has no ActionBlocks", "execution_id": execution_id}

            if not tools and not dry_run:
                return {"status": "failure", "error": "No deployed tools available for this Shadow", "execution_id": execution_id}

            if not tools and dry_run:
                logger.warning("[Runtime] Dry-run with 0 tools — executing as THINK-only planning mode")

            # ── Build skeleton indexes (Progressive Disclosure) ─
            # Include parameter_names so the LLM knows which kwargs each tool
            # expects WITHOUT needing a LOAD_RESOURCE round-trip.  Without this
            # the LLM has to guess, leading to tool_call(args={}) and the tool's
            # required-arg guard rejecting every call.
            tool_index = [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "parameter_names": list(getattr(t, "parameter_names", []) or []),
                    # v0.9.7: surface the REAL parameter schema (was hardcoded {},
                    # which left the executor LLM blind to v1 params → bare-string
                    # args → AttributeError on parameters.keys()).
                    "parameters_schema": dict(getattr(t, "parameters_schema", {}) or {}),
                }
                for t in tools
            ]
            # v0.9.5 T0: augment v1 tool_index with v2-registered tools so the
            # LLM can actually call them (file_tools, skill_tools, capability_tools,
            # etc.). Without this, all L3/L5/L6 LLM tools are dead code.
            _existing_names = {t["name"] for t in tool_index}
            _v2_entries = _build_llm_tool_catalog(
                vault=None,  # v2 portion only — v1 already in tool_index above
                config=getattr(self, "config", None),
            )
            for _entry in _v2_entries:
                if _entry["name"] not in _existing_names:
                    tool_index.append(_entry)
                    _existing_names.add(_entry["name"])
            skill_index = [
                {"id": s.id, "name": s.name, "category": s.category, "description": s.description}
                for s in skills
            ]

            # ── Boot-time memory injection ────────────────────────────────────────
            # Global memory: always full (personalisation applies to every task).
            # Shadow memory: header-only at boot; shadow calls LOAD_RESOURCE on demand.
            recalled_memory = _build_boot_memory(shadow, self.vault)

            # ── Initialise context ────────────────────────────────────────────────
            context = ExecutionContext(
                execution_id=execution_id,
                system_prompt=shadow.system_prompt,
                scroll_json=scroll_json,
                tool_index=tool_index,
                skill_index=skill_index,
                recalled_memory=recalled_memory,
                use_objectives=use_objectives,
                scroll_intent=scroll.intent,
            )

            step_prompt = load_prompt("execute_step.md")

            # Objective tracking (intent-driven mode)
            completed_objectives: set[int] = set()
            total_objectives = len(objectives)

            # v0.8.19 (R2): publish the initial objective_state so the live pane
            # can render the full checklist at boot.  Best-effort — EventBus is
            # optional and a publish failure must never break execution.
            if use_objectives:
                try:
                    from systemu.interface.event_bus import EventBus
                    EventBus.get().publish(_objective_state_event(
                        objectives, completed_objectives, execution_id, stamp=self._stamp))
                except Exception:
                    pass  # EventBus is optional — never break execution

            # Legacy ActionBlock tracking
            current_ab   = 1

            # ── v0.5.1-e: resume from prior-execution snapshot ─────────────────
            # When the supervisor's RECALIBRATE_TOOL → operator-approval flow
            # triggers re-queue with resume_from_execution_id, load the snapshot
            # and pre-populate sticky notes + completed_objectives so the new
            # run picks up where the prior one left off.  Snapshot is consumed
            # (deleted) after read so a subsequent restart starts clean.
            if resume_from_execution_id:
                try:
                    from systemu.runtime.execution_snapshot import (
                        apply_to_context, delete_snapshot, read_snapshot,
                    )
                    snap = read_snapshot(resume_from_execution_id)
                    if snap is not None:
                        apply_to_context(snap, context=context)
                        if use_objectives and snap.completed_objective_ids:
                            # Restore completed objective ids — the runtime won't
                            # ask the LLM to redo them.
                            completed_objectives.update(snap.completed_objective_ids)
                            # v0.9.1 (Layer 4): re-credit any objectives NOT in the snapshot
                            # that already have durable evidence on disk.  This covers the
                            # case where the shadow completed work but the snapshot was taken
                            # before the verifier ran (e.g., mid-run restart).
                            try:
                                _uncredited = [
                                    o for o in objectives
                                    if o.id not in completed_objectives
                                ]
                                for _uc_obj in _uncredited:
                                    _rc = recredit_on_resume(
                                        objective=_uc_obj,
                                        vault=self.vault,
                                        config=self.config,
                                        execution_id=resume_from_execution_id or execution_id,
                                        default_output_dir=_resolve_verifier_output_dir(
                                            self.config, getattr(self, "user_profile", None)
                                        ),
                                    )
                                    if _rc.credited:
                                        completed_objectives.add(_uc_obj.id)
                                        logger.info(
                                            "[Runtime] recredit_on_resume: obj=%d re-credited "
                                            "from existing durable evidence.",
                                            _uc_obj.id,
                                        )
                            except Exception:
                                logger.debug(
                                    "[Runtime] recredit_on_resume hook crashed — skipping",
                                    exc_info=True,
                                )
                        if not use_objectives and snap.current_action_block:
                            current_ab = max(current_ab, int(snap.current_action_block))
                        # v0.8.22.1 (R6): if the operator answered a stuck decision,
                        # lift it (and the round counters) from the snapshot stickies
                        # so we can apply it at resume-start instead of re-triggering.
                        try:
                            import json as _json
                            _ans_note = next((n for n in snap.sticky_notes
                                              if n.startswith("__STUCK_ANSWER__::")), None)
                            _rounds_note = next((n for n in snap.sticky_notes
                                                 if n.startswith("__STUCK_ROUNDS__::")), None)
                            if _rounds_note:
                                self._stuck_round_for_obj = {
                                    int(k): v for k, v in
                                    _json.loads(_rounds_note.split("::", 1)[1]).items()
                                }
                            # Fix #5: restore the no-progress pressure so a resumed run
                            # trips the stuck/force-finalize path instead of re-searching
                            # from a clean slate.
                            try:
                                self._iters_since_obj_credit = _decode_no_progress_note(snap.sticky_notes)
                            except Exception:
                                pass
                            if _ans_note:
                                _parts = _ans_note.split("::", 2)  # __STUCK_ANSWER__::obj_<id>::<choice_json>
                                _obj_id = int(_parts[1].replace("obj_", ""))
                                try:
                                    _ans = _json.loads(_parts[2])
                                except Exception:
                                    _ans = {"action": _parts[2]}
                                self._resume_stuck_answer = (_obj_id, _ans)
                        except Exception:
                            logger.debug("[Runtime] resume stuck-answer parse failed", exc_info=True)
                        # v0.9.7 grant-resume: peel a __HARNESS_GRANT__ note (the
                        # operator's resolved grant, written by resume_after_grant)
                        # plus the original __HARNESS_PENDING__ (carries the request
                        # fallback/kind for the deny branch). Consumed at resume-start
                        # via _apply_harness_grant → _apply_materialised_grant (the
                        # SAME helper the autonomous GRANT path uses — no re-arbitrate).
                        try:
                            import json as _json
                            _grant_note = next(
                                (n for n in snap.sticky_notes
                                 if n.startswith("__HARNESS_GRANT__::")), None)
                            _pending_note = next(
                                (n for n in snap.sticky_notes
                                 if n.startswith("__HARNESS_PENDING__::")), None)
                            if _grant_note:
                                _gpayload = _json.loads(_grant_note.split("::", 2)[2])
                                if _pending_note:
                                    try:
                                        _ppayload = _json.loads(
                                            _pending_note.split("::", 2)[2])
                                        # carry the original fallback so a DENY can
                                        # tell the agent what to do instead.
                                        _gpayload.setdefault(
                                            "fallback", _ppayload.get("fallback", ""))
                                        _gpayload.setdefault(
                                            "kind", _ppayload.get("kind", ""))
                                    except Exception:
                                        pass
                                self._resume_harness_grant = _gpayload
                        except Exception:
                            logger.debug("[Runtime] resume harness-grant parse failed",
                                         exc_info=True)
                        # v0.9.33 Bug 2/3: restore the per-run harness-request
                        # count + nesting depth from the ALREADY-READ snapshot
                        # (no second read) BEFORE delete_snapshot consumes it, so
                        # a resumed run keeps counting toward the cap and the depth
                        # guard. Missing/garbage fields floor to 0 (backward-compat).
                        try:
                            harness_requests_this_run = max(
                                0, int(getattr(snap, "requests_this_run", 0) or 0)
                            )
                            self._subagent_depth = max(
                                int(getattr(self, "_subagent_depth", 0) or 0),
                                int(getattr(snap, "subagent_depth", 0) or 0),
                            )
                            # v0.9.39 Bug 15: inherit the run-tree id from the
                            # snapshot so this resumed exec keeps the SAME root as
                            # the pre-suspend run — unless an explicit param root
                            # was given (a child spawned mid-resume), which wins.
                            if not root_execution_id:
                                _snap_root = getattr(snap, "root_execution_id", None)
                                if _snap_root:
                                    root_eid = _snap_root
                        except Exception:
                            logger.debug(
                                "[Runtime] harness-count resume restore failed",
                                exc_info=True,
                            )
                        logger.info(
                            "[Runtime] resumed from snapshot of %s — restored %d completed objective(s), %d sticky note(s)",
                            resume_from_execution_id,
                            len(snap.completed_objective_ids),
                            len(snap.sticky_notes),
                        )
                        delete_snapshot(resume_from_execution_id)
                    else:
                        logger.info(
                            "[Runtime] resume requested for %s but no snapshot found — starting fresh",
                            resume_from_execution_id,
                        )
                except Exception:
                    logger.exception("[Runtime] resume hook crashed — starting fresh")

            # v0.8.22.1 (R6): consume a resume-start stuck answer if present.
            _pending = getattr(self, "_resume_stuck_answer", None)
            if _pending and use_objectives:
                _obj_id, _ans = _pending
                self._resume_stuck_answer = None
                _stuck_obj = next((o for o in objectives if o.id == _obj_id),
                                  objectives[-1] if objectives else None)
                if _stuck_obj is not None:
                    from functools import partial as _partial
                    _fin = _partial(self._finalize_stuck, context=context,
                                    reason="resumed", stuck_on=_stuck_obj.id,
                                    completed=list(completed_objectives),
                                    iteration=int(getattr(context, "_resume_iteration", 0)),
                                    tool_calls_made=0, scroll=scroll, shadow=shadow,
                                    execution_id=execution_id, exec_start=exec_start,
                                    total_objectives=len(objectives))
                    _action, _res = self._apply_stuck_answer(_stuck_obj, _ans, finalize=_fin)
                    if _action == "finalize":
                        return _res
                    # else "continue": hint is now in self._operator_hint; loop retries

            last_snap_ab = 0
            iteration    = 0
            consecutive_thinks = 0  # throttle THINK storms
            # v0.8.16: llm_ref for the most-recent tier-2 decision LLM call —
            # {exec_id, call_index} into the per-execution transcript file.  Set
            # right after each LLM call (Task 8); consumed by _iteration_event so
            # the panes can lazily load the raw completion on expand.
            _last_llm_ref: Optional[Dict[str, Any]] = None

            # v0.4.0-d: Intelligent Supervisor (opt-in via config).  When enabled,
            # an ExecutionMind subscribes to events for this run, observes failures,
            # and emits directives into a small inbox the loop drains each tick.
            # When disabled the inbox stays empty and shadow runtime behaves
            # exactly as in v0.3.x.
            # v0.4.1-a: per-shadow opt-in.  The supervisor activates when EITHER
            # the global config flag OR the shadow's own ``supervisor_enabled`` is
            # True.  Lets the operator A/B test on one specialist before flipping
            # the global switch.
            execution_mind = None
            directive_inbox = None
            _supervisor_globally_on = bool(getattr(self.config, "intelligent_supervisor_enabled", False))
            _supervisor_per_shadow_on = bool(getattr(shadow, "supervisor_enabled", False))
            if _supervisor_globally_on or _supervisor_per_shadow_on:
                try:
                    from systemu.runtime.execution_mind import ExecutionMind, DirectiveInbox
                    directive_inbox = DirectiveInbox()
                    execution_mind = ExecutionMind(
                        execution_id=execution_id,
                        shadow_id=getattr(shadow, "id", None),
                        config=self.config,
                        directive_sink=directive_inbox.append,
                        # When only the per-shadow flag is on (global still off),
                        # force the Mind to enable itself rather than reading from
                        # the global config it doesn't know about.
                        force_enabled=_supervisor_per_shadow_on,
                        origin=self._origin,   # v0.8.16: strategy-stream ticks partition on origin
                    )
                    # Stash on self so _handle_tool_call (a method) can reach it
                    # without threading another parameter through the call chain.
                    self._execution_mind = execution_mind
                except Exception:
                    logger.exception("[Runtime] ExecutionMind construction failed — disabling supervisor")
                    execution_mind = None
                    directive_inbox = None
                    self._execution_mind = None
            else:
                self._execution_mind = None

            import asyncio

            # ─── THE AGENTIC LOOP ─────────────────────────────────────────────────
            # v0.9.7: deterministic stall corrector — detects round-about repetition
            # (same tool+args+result, or A↔B ping-pong) and nudges/forces a finish.
            loop_guard = LoopGuard(self.config)
            loop_guard_nudge = None  # pending verdict to inject next iteration
            # v0.9.7 Phase 2: one Governor per execution so harness leases + the
            # ledger stay coherent across REQUEST_HARNESS calls; leases are
            # revoked at terminal state (default-deny never leaks across runs).
            governor = None
            if _intent_engine_enabled(self.config):
                try:
                    from systemu.runtime.governor import Governor as _GovernorCls
                    governor = _GovernorCls(self.config)
                    # v0.10.0: let the Governor write lease-mint/revoke ledger events
                    # to THIS run's ledger (revoke_leases carries no vault param).
                    try:
                        governor._active_ledger_vault = self.vault
                    except Exception:
                        pass
                except Exception:
                    governor = None

            # v0.10.0 Task 1.6: tool names invoked this run, for terminal request-
            # outcome reconciliation (granted_used vs granted_unused).
            _called_tools: set = set()
            _used_harness = False        # set True once the agent makes a harness request
            _harness_finalized = {"done": False}
            # v0.9.36 Bug 9: MCP servers THIS run registered into the process-global
            # v2 registry — torn down at the terminal finalize so they don't leak
            # into the next run's catalog (the lease-keyed revoke can't reach a
            # resumed run's server; see _revoke_harness_leases below). Reset per
            # run; resume is a fresh ShadowRuntime so a fresh set is correct.
            self._mcp_servers_registered_this_run = set()

            def _revoke_harness_leases(run_success: bool = True, record_run: bool = True,
                                       reconcile: bool = True):
                # Idempotent terminal finalize — safe to call from any terminal path
                # AND from the execute() finally block, which GUARANTEES it runs
                # exactly once per run even on the partial / max-iterations /
                # exception exits no explicit call site covers (v0.9.36 Bug 9 — was
                # firing in ~3% of runs). ``record_run`` doubles as the
                # terminal-vs-parked signal: True = a real terminal exit; False = a
                # suspend/park that WILL resume (so we must NOT tear down the MCP
                # tools the resumed run still needs). INVARIANT: every suspend
                # return calls this with record_run=False BEFORE the finally runs,
                # so the finally's record_run=True fallback only ever fires on a
                # genuine terminal exit.
                # ``reconcile`` (v0.9.37 Bug 11): suspends pass reconcile=False so the
                # premature escalate_unresolved is NOT written — the request is pending
                # operator approval, not unresolved, and writing it now would
                # double-count the terminal granted_* produced after resume.
                if _harness_finalized["done"] or governor is None:
                    return
                _harness_finalized["done"] = True
                # Bug 11: an escalate→suspend→approve→resume lifecycle splits across
                # two execution ids — the request + escalate/grant arb rows + lease-mint
                # live in the ORIGINAL (pre-suspend) exec's ledger (the operator's
                # approve calls materialise under it), while the capability is actually
                # USED in the RESUMED run. Reconcile the resumed run's tools
                # (``_called_tools``) against BOTH ledgers so the lifecycle classifies
                # granted_used (not escalate_unresolved). reconcile_outcomes collapses
                # the escalate+grant rows for one request_id to a single granted_*.
                _prior_eid = (resume_from_execution_id
                              if (resume_from_execution_id
                                  and resume_from_execution_id != execution_id)
                              else None)
                if reconcile:
                    try:
                        # v0.9.39 Bug 15: reconcile EVERY ledger in the run-tree —
                        # the suspend→resume predecessors AND the sub-agent children —
                        # via the per-root lineage index, at ANY genuine terminal.
                        # NOT gated on ``execution_id == root_eid``: in a suspend→
                        # resume chain the ROOT is the FIRST exec and SUSPENDS
                        # (reconcile=False), so it never reaches a terminal — the exec
                        # that actually terminates is the LAST resume, which is not the
                        # root. Gating on root meant the sweep never fired for that
                        # (dominant SUBAGENT) shape — 8 distinct grants reconciled to 1
                        # event. Sweeping at every genuine terminal is safe: the
                        # per-request_id collapse + the ``already`` dedup make redundant
                        # sweeps idempotent, so the last terminal to run reconciles
                        # every still-open grant in the tree. Falls back to
                        # [_prior_eid] only when no sidecar exists (test stubs).
                        _also_ids = []
                        try:
                            _also_ids = [
                                e for e in governor.runtree_execution_ids(
                                    root_eid, self.vault)
                                if e and e != execution_id
                            ]
                        except Exception:
                            _also_ids = []
                        if _prior_eid and _prior_eid not in _also_ids:
                            _also_ids.append(_prior_eid)   # immediate resume predecessor
                        _rec_kw = {"run_success": run_success, "vault": self.vault}
                        if _also_ids:
                            _rec_kw["also_ids"] = _also_ids
                        governor.write_outcome_reconciliation(
                            execution_id, _called_tools, **_rec_kw)
                    except Exception:
                        logger.debug("[Runtime] outcome reconciliation failed", exc_info=True)
                for _eid in ([execution_id] + ([_prior_eid] if _prior_eid else [])):
                    try:
                        governor.revoke_leases(_eid)
                    except Exception:
                        logger.debug("[Runtime] lease revocation failed for %s",
                                     _eid, exc_info=True)
                # v0.9.36 Bug 9 (Symptom A — cross-run MCP leak): the v2 tool
                # registry is a PROCESS-GLOBAL singleton, but a resumed run mints
                # its MCP lease under the now-dead pre-suspend Governor, so the
                # lease-keyed unregister above finds nothing and the namespaced
                # tools leak into the next run. Defense in depth: on a genuine
                # TERMINAL exit (never a suspend), unregister every server THIS run
                # registered, regardless of lease state. Idempotent —
                # unregister_server_tools no-ops on an already-clean prefix.
                if record_run:
                    _registered = getattr(self, "_mcp_servers_registered_this_run", None)
                    if _registered:
                        try:
                            from systemu.runtime.mcp.sdk.registry_bridge import (
                                unregister_server_tools,
                            )
                            for _srv in list(_registered):
                                try:
                                    unregister_server_tools(_srv)
                                except Exception:
                                    logger.debug(
                                        "[Runtime] mcp terminal unregister failed for %s",
                                        _srv, exc_info=True)
                            _registered.clear()
                        except Exception:
                            logger.debug("[Runtime] mcp terminal unregister skipped",
                                         exc_info=True)
                # v0.10.0 Task 1.7(c): harness-usage metric slice (additive + harness-only,
                # so no double-count with the base recorder; skipped on parked/suspended
                # runs via record_run=False).
                if record_run:
                    try:
                        from systemu.runtime.affinity_log import compute_intent_hash
                        from systemu.runtime.shadow_metrics import get_shadow_metrics
                        get_shadow_metrics().note_harness_usage(
                            shadow_id=getattr(shadow, "id", ""),
                            intent_hash=compute_intent_hash(
                                intent=getattr(scroll, "intent", "") or "",
                                objectives=getattr(scroll, "objectives", None),
                            ),
                            used_harness=_used_harness,
                            success=run_success,
                        )
                    except Exception:
                        logger.debug("[Runtime] harness-usage slice skipped", exc_info=True)

            # v0.9.7 Phase 3: resolve execution-adherence + a mutable iteration
            # budget for THIS run.
            #   • A COMPUTE harness grant can extend ``_iter_budget`` at runtime.
            #   • ``_adherence`` (free/guided/strict) is resolved from the operator
            #     pin (config.execution_adherence) → else auto: records honor the
            #     per-SOP adherence saved at save-time, chat → free. Under "strict"
            #     the lenient goal-level acceptance shortcut is suppressed so the
            #     per-objective / SOP contract is honored verbatim.
            # All adherence-conditioned behavior remains behind the intent-engine
            # flag (the resolver itself is side-effect-free).
            _iter_budget = MAX_ITERATIONS
            _sop_adherence = (getattr(scroll, "adherence", None) or "").strip() or None
            _origin_l = (origin or getattr(self, "_origin", "") or "").strip().lower()
            _req_kind = "record" if (_origin_l in {"record", "sop", "replay"} or _sop_adherence) else "chat"
            _adherence = "free"
            try:
                from systemu.runtime.adherence import resolve_adherence as _resolve_adh
                _adherence = _resolve_adh(self.config, request_kind=_req_kind, sop_adherence=_sop_adherence)
            except Exception:
                _adherence = "free"
            if _intent_engine_enabled(self.config):
                logger.info("[Runtime] intent-engine: execution adherence=%s (kind=%s).", _adherence, _req_kind)

            # ── RCA fix: per-objective verifier baseline timing ──────────────
            # Capture the verifier baseline ONCE here, at run start, BEFORE the
            # agent writes any deliverable. ObjectiveState.baseline was never
            # populated, so process_completion_claim fell through to
            # capture_baseline() at verify-time — i.e. AFTER the tool call had
            # already written the file that same turn. The baseline absorbed the
            # deliverable → empty StateDelta → false "no durable evidence"
            # rejection that trapped the agent re-proving a finished objective
            # (so it never reached later objectives). A run-start snapshot lets
            # compute_delta see everything the run produces. Applies to BOTH
            # engines — this is the v0.9.1 Layer-4 contract, not new-engine-only.
            _run_verifier_baseline = None
            if use_objectives:
                try:
                    _run_verifier_baseline = state_delta.capture_baseline(
                        vault=self.vault, execution_id=execution_id, objective_id=0,
                        default_output_dir=_resolve_verifier_output_dir(
                            self.config, getattr(self, "user_profile", None)),
                    )
                except Exception:
                    logger.debug("[Runtime] run-start verifier baseline capture failed",
                                 exc_info=True)
                    _run_verifier_baseline = None

            # ── v0.9.35 (Phase 3): resolve recorded scroll PARAMETERS ──────────
            # A BROAD-generalized scroll carries captured specifics as
            # `scroll.parameters`. Ask the operator once (captured value
            # pre-filled, editable), then substitute the answers into the
            # objectives/intent/constraints. Standard/narrow scrolls have no
            # parameters ⇒ this whole block is skipped (byte-identical path).
            # Skip on a resume (the operator already answered — the grant is
            # consumed by _apply_harness_grant_async below) and in dry_run.
            # `_stash_scroll_parameters(scroll)` already ran at scroll-load so it
            # is cached for BOTH fresh and resume paths.
            if not dry_run and not resume_from_execution_id:
                _param_req = self._resolve_scroll_parameters(scroll)
                if _param_req is not None:
                    from systemu.interface.notifications import (
                        is_headless, _get_decision_queue,
                    )
                    if _get_decision_queue() is None and is_headless():
                        # No operator channel: proceed with captured defaults
                        # (the pre-filled values are the recorded specifics) —
                        # never hang a non-interactive run. Make it visible.
                        context.add_observation({
                            "type": "parameters_resolved",
                            "message": (
                                "No operator available to confirm recorded "
                                "parameters; using the captured values as-is."
                            ),
                            "resolved": {
                                p.name: p.default for p in self._scroll_parameters
                            },
                        }, current_ab)
                    else:
                        from systemu.runtime.governor import Governor
                        from systemu.runtime.execution_snapshot import (
                            capture_from_context, write_snapshot,
                        )
                        from systemu.interface.harness_review import (
                            surface_harness_request,
                        )
                        # Use a distinct local name here (the `_pgov` below) so the
                        # v0.9.7 flag-gating order-guard test keeps anchoring on the
                        # REQUEST_HARNESS loop branch's Governor reuse, not this
                        # system-initiated pre-task confirmation step.
                        _pgov = governor or Governor(self.config)
                        _arb_ctx = _harness_arbitration_context(
                            harness_requests_this_run,
                            int(getattr(self, "_subagent_depth", 0)),
                        )
                        _verdict = _pgov.arbitrate(_param_req, context=_arb_ctx)
                        try:
                            _snap = capture_from_context(
                                execution_id=execution_id,
                                shadow_id=getattr(shadow, "id", ""),
                                scroll_id=getattr(scroll, "id", ""),
                                iteration=iteration,
                                current_action_block=current_ab,
                                completed_objectives=set(completed_objectives),
                                context=context,
                                activity_id=getattr(activity, "id", ""),
                                requests_this_run=harness_requests_this_run,
                                subagent_depth=int(getattr(self, "_subagent_depth", 0)),
                                root_execution_id=root_eid,
                            )
                            import json as _json
                            _snap.sticky_notes.append(
                                f"__HARNESS_PENDING__::{execution_id}::"
                                + _json.dumps({
                                    "request_id": _param_req.request_id,
                                    "kind":       _param_req.kind.value,
                                    "spec":       _param_req.spec,
                                    "fallback":   _param_req.fallback,
                                })
                            )
                            write_snapshot(_snap)
                        except Exception:
                            logger.debug("[Runtime] P3 param snapshot failed",
                                         exc_info=True)
                        try:
                            surface_harness_request(
                                _param_req, _verdict, execution_id=execution_id,
                                activity_id=activity.id, shadow_id=shadow.id,
                                vault=self.vault,
                            )
                        except Exception:
                            logger.debug("[Runtime] P3 surface_harness_request failed",
                                         exc_info=True)
                        # v0.9.36 Bug 9: a park-for-params is a suspend, not a
                        # terminal — reconcile + revoke but do NOT record a run and
                        # do NOT tear down capabilities (record_run=False), and mark
                        # finalized so the execute() finally fallback no-ops here.
                        _revoke_harness_leases(record_run=False, reconcile=False)   # v0.9.37 Bug 11: defer reconcile to terminal
                        _susp = context.build_result(
                            status="suspended_harness_escalation",
                            final_summary=(
                                "Parked awaiting operator confirmation of recorded "
                                "task parameters."
                            ),
                        )
                        _susp["activity_id"] = activity.id
                        _susp["shadow_id"]   = shadow.id
                        return _susp

            # ── v0.9.7 grant-resume: consume a resume-start harness grant ──────
            # When the run was parked on a blocking ESCALATE and the operator
            # resolved it, resume_after_grant stamped a __HARNESS_GRANT__ note we
            # peeled above into self._resume_harness_grant. Apply it now (BEFORE
            # the loop), reusing the SAME _apply_materialised_grant the autonomous
            # GRANT path uses — resume APPLIES the operator's authoritative verdict,
            # it never re-arbitrates the request.
            _hgrant = getattr(self, "_resume_harness_grant", None)
            if _hgrant is not None:
                self._resume_harness_grant = None
                # v0.9.35 (P1): a re-dispatch closure so an INPUT param grant can
                # re-run the original tool call (which re-validates via the seam).
                # A still-missing sentinel ⇒ a missing_required_params observation
                # so the resumed loop's next LLM turn re-issues the call (Task 4
                # then intercepts it live).
                async def _resume_redispatch(_decision, _ab=current_ab):
                    _r = await self._handle_tool_call(
                        _decision, tools, context, _ab, dry_run,
                        shadow=shadow, execution_id=execution_id,
                    )
                    if (_r is not None
                            and isinstance(getattr(_r, "parsed", None), dict)
                            and _r.parsed.get("__needs_input__")):
                        context.add_observation({
                            "type": "missing_required_params",
                            "success": False,
                            "tool_name": _decision.get("tool_name", "") or "?",
                            "error": ("Still missing required parameter(s) after "
                                      "the operator's answer. Re-issue the call with "
                                      "the remaining values or FAIL."),
                            "error_type": "missing_required_params",
                        }, _ab)
                        return None
                    return _r
                self._resume_redispatch = _resume_redispatch
                try:
                    _iter_budget = await self._apply_harness_grant_async(
                        _hgrant, context=context, tools=tools,
                        tool_index=tool_index, current_ab=current_ab,
                        iter_budget=_iter_budget,
                    )
                except Exception:
                    logger.debug("[Runtime] _apply_harness_grant_async failed — proceeding",
                                 exc_info=True)
                finally:
                    self._resume_redispatch = None

            # v0.9.35 P3 (seam fix): a param-substitution resume grant rewrites
            # context.scroll_json / scroll_intent with the operator's confirmed/
            # edited values. Refresh the loop locals from context so this run's
            # per-iteration prompt (objectives + pending_objs, below) reflects
            # them — otherwise the substitution wrote to fields the prompt never
            # reads. Strict no-op unless the objects were actually replaced
            # (identity check), so standard/narrow runs are byte-identical.
            if (use_objectives
                    and getattr(context, "scroll_json", None) is not None
                    and context.scroll_json is not scroll_json):
                from systemu.core.models import Objective as _Objective
                scroll_json = context.scroll_json
                objectives = [_Objective.model_validate(o) for o in scroll_json]

            while iteration < _iter_budget:
                iteration += 1

                # v0.9.1 (Layer 4): reset per-turn verifier call counter for all objectives
                # at the start of each new LLM turn so the cap is per-turn, not per-run.
                for _vs in self._objective_states.values():
                    _vs.calls_this_turn = 0

                # ── v0.4.0-d: drain supervisor directive inbox and apply ─────────
                # ExecutionMind populates this asynchronously; we apply pending
                # directives at the top of each iteration so they shape the
                # next LLM decision.  Empty when supervisor is disabled.
                if directive_inbox is not None and len(directive_inbox) > 0:
                    # v0.5.1-e: stash loop state on the context so RECALIBRATE_TOOL
                    # snapshot capture can read it without threading every state
                    # variable through every helper.
                    context._resume_iteration = iteration
                    context._resume_current_ab = current_ab
                    context._resume_completed_objectives = (
                        set(completed_objectives) if use_objectives else set()
                    )
                    # v0.9.33 Bug 2/3: stash the per-run harness-request count +
                    # nesting depth so the recalibration snapshot helper (which
                    # pulls loop state off context, not loop-locals) persists them
                    # and a recalibration-resume keeps counting toward the cap.
                    context._resume_requests_this_run = harness_requests_this_run
                    context._resume_subagent_depth = int(getattr(self, "_subagent_depth", 0))
                    _apply_supervisor_directives(
                        directive_inbox.drain(),
                        context=context,
                        config=self.config,
                        shadow=shadow,
                        scroll=scroll,
                        execution_id=execution_id,
                        vault=self.vault,
                        consec_tool_fails=self._consec_tool_fails,
                        origin=self._origin,   # v0.8.16: stamp origin on supervisor cards
                    )

                # ── Cancellation gate — Supervisor watchdog may request clean exit ──
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(
                        "[Runtime] Cancellation requested by Supervisor watchdog at iter=%d "
                        "— exiting cleanly (execution_id=%s)",
                        iteration, execution_id,
                    )
                    _record_terminal_telemetry(
                        shadow=shadow, execution_id=execution_id, scroll=scroll,
                        status="cancelled", iteration=iteration,
                    )
                    return {
                        "status":        "cancelled",
                        "final_summary": f"Shadow cancelled by watchdog at iteration {iteration}.",
                        "error":         "WatchdogCancelled",
                        "execution_id":  execution_id,
                    }

                if use_objectives:
                    # Only show objectives whose dependencies are fully satisfied.
                    # Objectives with unmet depends_on are withheld — they become
                    # visible once their prerequisite IDs appear in completed_objectives.
                    pending_objs = [
                        obj.model_dump(mode="json") for obj in objectives
                        if obj.id not in completed_objectives
                        and all(dep in completed_objectives for dep in obj.depends_on)
                    ]
                    logger.debug("[Runtime] Iteration %d/%d — %d/%d objectives done",
                                 iteration, MAX_ITERATIONS, len(completed_objectives), total_objectives)
                else:
                    pending_objs = None
                    logger.debug("[Runtime] Iteration %d/%d — ActionBlock %d",
                                 iteration, MAX_ITERATIONS, current_ab)

                # Build and send the decision prompt
                messages = context.build_messages(
                    current_ab if not use_objectives else 0,
                    completed_objectives=completed_objectives if use_objectives else None,
                )
                # v0.4.0-a: THINK throttle ceiling is now config-driven.
                think_ceiling = getattr(self.config, "max_consecutive_think", 5) or 5
                # v0.8.16: build the decision-prompt system/user once so the LLM
                # transcript writer can record a request summary (the raw
                # completion is recorded after the call returns).
                _llm_system = (
                    step_prompt
                    if consecutive_thinks < think_ceiling else
                    step_prompt + (
                        "\n\n# ENFORCEMENT OVERRIDE\n"
                        f"You have produced {consecutive_thinks} consecutive THINK responses "
                        "with NO tool call. Your NEXT response MUST have "
                        "action==TOOL_CALL, COMPLETE, or FAIL. No more THINK "
                        "will be accepted. Act now."
                    )
                )
                _user_payload = _build_user_payload(
                    shadow_name=shadow.name,
                    # output_dir: where Shadow-generated files must be written.
                    # Bind-mounted to the host's ./outputs/ directory so files
                    # are accessible outside the container.
                    output_dir=self.config.output_dir,
                    # Temporal context — avoids LLM THINK storms over "what is today's date?"
                    current_date=_datetime_module.date.today().isoformat(),
                    current_datetime_utc=utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    use_objectives=use_objectives,
                    # intent/pending_objs are only emitted in objective mode by
                    # the helper; passing them unconditionally is harmless
                    # (pending_objs is None in action-block mode and ignored).
                    intent=getattr(context, "scroll_intent", None) or scroll.intent,
                    scroll_json=scroll_json,
                    completed_objectives=completed_objectives,
                    pending_objectives=pending_objs,
                    current_ab=current_ab,
                    available_tools=tool_index,
                    history=_build_history_slice(context),
                    last_snapshot=(
                        context._snapshots[-1].summary if context._snapshots else None
                    ),
                    # v0.9.33-C: surface the LIVE iteration budget. _iter_budget is
                    # dynamic — a COMPUTE harness grant extends it mid-run — so the
                    # agent sees the real remaining headroom, not a fixed ceiling.
                    iteration=iteration,
                    iter_budget=_iter_budget,
                )
                # v0.9.6 T0: per-iteration guard — ensure v2 tools are always
                # present in the LLM's available_tools even if tool_index was
                # assembled before v2 discovery completed, or if a future code
                # path resets/filters tool_index between the boot-time augmentation
                # (v0.9.5 T0, ~line 1759) and here.  This closes the gap observed
                # in the v0.9.5 live burrito test where the LLM never saw v2 tools.
                _v2_live = _build_llm_tool_catalog(
                    vault=None,  # v2 portion only — v1 already in tool_index
                    config=getattr(self, "config", None),
                )
                _at_live_names = {t.get("name") for t in _user_payload["available_tools"]}
                for _at_entry in _v2_live:
                    if _at_entry["name"] not in _at_live_names:
                        _user_payload["available_tools"].append(_at_entry)
                        _at_live_names.add(_at_entry["name"])
                # v0.8.21: one-shot operator hint fold-back (cleared after this iteration).
                if self._operator_hint:
                    _user_payload["operator_hint"] = self._operator_hint
                    self._operator_hint = None
                # v0.9.7: inject the loop-guard verdict (round-about detection).
                # On 'block', strip tools so the agent MUST COMPLETE or FAIL.
                if loop_guard_nudge is not None:
                    _user_payload["loop_guard_notice"] = loop_guard_nudge.get("message", "")
                    if loop_guard_nudge.get("level") == "block":
                        _user_payload["available_tools"] = []
                        _user_payload["loop_guard_force_finalize"] = True
                    loop_guard_nudge = None
                _llm_user = json.dumps(_user_payload)
                loop = asyncio.get_event_loop()
                try:
                    decision = await loop.run_in_executor(
                        None,
                        lambda: llm_call_json(
                            tier=2,
                            system=_llm_system,
                            user=_llm_user,
                            config=self.config,
                            temperature=0.1,
                            max_tokens=4096,
                        )
                    )
                    # v0.8.16: record the raw decision completion to the per-
                    # execution transcript and remember its index so the
                    # per-iteration event (Task 7) can reference it via llm_ref
                    # for lazy UI expand.  Best-effort — append_call never raises.
                    try:
                        from systemu.runtime.llm_transcript import append_call
                        _call_index = append_call(
                            self.vault.root, execution_id,
                            {
                                "iteration": iteration,
                                "tier":      2,
                                "system":    _llm_system,
                                "user":      _llm_user,
                                "response":  json.dumps(decision),
                            },
                        )
                        _last_llm_ref = (
                            {"exec_id": execution_id, "call_index": _call_index}
                            if _call_index >= 0 else None
                        )
                    except Exception:
                        _last_llm_ref = None
                except Exception as exc:
                    logger.error("[Runtime] LLM error on iteration %d: %s", iteration, exc)
                    log_event("ERROR", "shadow", f"LLM error in execution {execution_id} iteration {iteration}: {exc}", {"shadow_id": shadow.id, "origin": getattr(self, "_origin", "manual")})
                    context.add_thought(f"LLM call failed: {exc}", current_ab)
                    continue

                action = decision.get("action", "THINK")
                # v0.8.21: stuck-guard — every iteration without a TOOL_CALL still counts toward stuck.
                if action != "TOOL_CALL":
                    self._update_stuck_counters(action=action, tool_name=None,
                                                  tool_success=None, credited_obj_id=None)
                if action == "THINK":  # LOAD_RESOURCE is productive — only throttle idle THINK
                    consecutive_thinks += 1
                else:
                    consecutive_thinks = 0
                logger.info("[Runtime] iter=%d action=%s", iteration, action)

                # ── v0.10.0 pull-decision instrumentation (observability only) ──
                # One row per iteration capturing the action + the blockage signals
                # active at decision time, so pull-decision quality (did a request
                # follow genuine blockage?) is reconstructable post-run. Never raises.
                try:
                    from systemu.runtime.decision_audit import (
                        IterationDecision, append_iteration_decision,
                    )
                    _rh = action == "REQUEST_HARNESS"
                    _lg_msg = None
                    try:
                        _lg_msg = _user_payload.get("loop_guard_notice") or None
                    except Exception:
                        _lg_msg = None
                    append_iteration_decision(
                        self.vault.root, execution_id,
                        IterationDecision(
                            execution_id=execution_id,
                            iteration=iteration,
                            action=action,
                            reasoning=str(
                                decision.get("reasoning")
                                or decision.get("thought")
                                or decision.get("reason") or ""
                            )[:500],
                            consecutive_thinks=consecutive_thinks,
                            loop_guard_active=bool(_lg_msg),
                            loop_guard_message=_lg_msg,
                            stuck_round_count=self._iters_since_obj_credit,
                            consec_research_reads=getattr(self, "_consec_research_reads", 0),
                            consec_tool_failures=sum(self._same_tool_fail_streak.values()),
                            is_request_harness=_rh,
                            harness_kind=(decision.get("kind") if _rh else None),
                            harness_confidence=(decision.get("confidence") if _rh else None),
                            harness_attempts_before=(decision.get("attempts_before") if _rh else None),
                        ),
                    )
                except Exception:
                    logger.debug("[Runtime] decision-audit write failed", exc_info=True)

                # ── Supervisor heartbeat — signals watchdog this shadow is alive ──
                try:
                    from systemu.runtime.supervisor import Supervisor
                    Supervisor.get().update_heartbeat(activity.id)
                except Exception:
                    pass  # Supervisor not running in CLI/test mode — safe to ignore

                # ── Publish per-iteration event to Systemu Chat ───────────────────
                try:
                    from systemu.interface.event_bus import EventBus
                    EventBus.get().publish(self._stamp({
                        "ts":       utcnow().isoformat() + "Z",
                        "level":    "INFO",
                        "category": "shadow",
                        "message":  f"[{shadow.name}] iter={iteration} action={action}",
                        "context":  {
                            "shadow_id":    shadow.id,
                            "execution_id": execution_id,
                            "iteration":    iteration,
                            "action":       action,
                            "objectives_done": len(completed_objectives) if use_objectives else current_ab - 1,
                            "objectives_total": total_objectives if use_objectives else len(scroll_json),
                        },
                    }))
                except Exception:
                    pass  # EventBus is optional — never break execution

                # ── v0.8.16: per-iteration detail event (reasoning + tool I/O) ─────
                # Carries a bounded `details` dict the live panes render on expand.
                # For TOOL_CALL the publish is deferred to AFTER the tool runs (so
                # tool_result is captured); every other action publishes here.
                if action != "TOOL_CALL":
                    try:
                        from systemu.interface.event_bus import EventBus
                        EventBus.get().publish(self._iteration_event(
                            iteration=iteration,
                            decision=decision,
                            execution_id=execution_id,
                            llm_ref=_last_llm_ref,
                        ))
                    except Exception:
                        pass  # EventBus is optional — never break execution

                # ── COMPLETE ───────────────────────────────────────────────────────
                if action == "COMPLETE":
                    # v0.9.7 intent-engine (flagged, default OFF): GOAL-level
                    # acceptance. Accept COMPLETE when the GOAL is verified from
                    # durable evidence, even if some refiner-baked per-objective
                    # criteria (possibly mis-framed — e.g. a durable-evidence
                    # check on an in-memory "determine X" step) weren't credited.
                    _goal_ok = False
                    if (_intent_engine_enabled(self.config)
                            and _adherence != "strict"
                            and use_objectives
                            and len(completed_objectives) < total_objectives):
                        try:
                            from systemu.runtime import goal_verifier as _gv
                            _gbaseline = state_delta._Baseline(
                                iteration_start_ts="1970-01-01T00:00:00Z")
                            _gdelta = state_delta.compute_delta(
                                baseline=_gbaseline, vault=self.vault,
                                default_output_dir=_resolve_verifier_output_dir(
                                    self.config, getattr(self, "user_profile", None)),
                                chat_result=decision.get("summary"),
                                config=self.config, execution_id=execution_id,
                            )
                            _gres = _gv.verify_goal(
                                goal=(getattr(scroll, "raw_request", None) or getattr(scroll, "intent", "") or ""),
                                delta=_gdelta, config=self.config,
                                chat_result=decision.get("summary"),
                            )
                            _goal_ok = bool(_gres.get("verified"))
                            logger.info(
                                "[Runtime] intent-engine goal-verify: %s — %s",
                                "PASS" if _goal_ok else "no-pass",
                                str(_gres.get("reason", ""))[:160],
                            )
                        except Exception:
                            logger.debug("[Runtime] goal-level verify errored", exc_info=True)

                    # Reject premature COMPLETE when objectives are still pending,
                    # UNLESS goal-level verification accepted it.
                    if use_objectives and len(completed_objectives) < total_objectives and not _goal_ok:
                        missing = [obj.id for obj in objectives if obj.id not in completed_objectives]
                        logger.warning(
                            "[Runtime] COMPLETE rejected — %d/%d objectives still pending: %s",
                            len(missing), total_objectives, missing,
                        )
                        context.add_observation(
                            {
                                "warning": (
                                    f"COMPLETE rejected: {len(missing)} objective(s) not yet "
                                    f"verified: {missing}. Finish all objectives before COMPLETE."
                                )
                            },
                            current_ab,
                        )
                        continue  # Return to loop — shadow must finish remaining objectives

                    summary = decision.get("summary", "Task completed.")
                    logger.info("[Runtime] Execution COMPLETE: %s", summary)
                    self._append_to_shadow_log(
                        shadow, execution_id, "success", summary,
                        iteration_count=iteration, tool_calls_made=tool_call_count,
                        objectives_completed=len(completed_objectives) if use_objectives else len(scroll_json),
                        objectives_total=total_objectives if use_objectives else len(scroll_json),
                        duration_seconds=__import__("time").time() - exec_start,
                    )
                    res = context.build_result(
                        status="success",
                        final_summary=summary,
                    )
                    _revoke_harness_leases(run_success=True)   # v0.10.0: finalize harness (idempotent)
                    # v0.4.3-a: record success in ShadowMetrics so the supervisor's
                    # affinity-routing alternative-selection learns this shadow
                    # handles this intent_hash well.
                    _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                    _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                    # v0.9.2: episodic capture — best-effort, never raises
                    _trigger_episodic_capture(
                        vault=getattr(self, 'vault', None),
                        config=getattr(self, 'config', None),
                        session_id=execution_id,
                        intent=getattr(scroll, "intent", ""),
                        chat_result=summary,
                        files_produced=[],
                        status="success",
                        execution_id=execution_id,
                    )
                    return res

                # ── FAIL ───────────────────────────────────────────────────────────
                elif action == "FAIL":
                    reason = decision.get("reason", "Unknown failure.")
                    logger.warning("[Runtime] Execution FAIL: %s", reason)
                    self._append_to_shadow_log(
                        shadow, execution_id, "failure", reason,
                        iteration_count=iteration, tool_calls_made=tool_call_count,
                        objectives_completed=len(completed_objectives) if use_objectives else current_ab - 1,
                        objectives_total=total_objectives if use_objectives else len(scroll_json),
                        duration_seconds=__import__("time").time() - exec_start,
                    )
                    res = context.build_result(
                        status="failure",
                        final_summary=f"Shadow reported failure: {reason}",
                        error=reason,
                    )
                    _revoke_harness_leases(run_success=False)   # v0.10.0: finalize harness (idempotent)
                    _record_terminal_telemetry(
                        shadow=shadow, execution_id=execution_id, scroll=scroll,
                        status="failure", iteration=iteration,
                    )
                    _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                    # v0.9.2: episodic capture — best-effort, never raises
                    _trigger_episodic_capture(
                        vault=getattr(self, 'vault', None),
                        config=getattr(self, 'config', None),
                        session_id=execution_id,
                        intent=getattr(scroll, "intent", ""),
                        chat_result=reason,
                        files_produced=[],
                        status="failure",
                        execution_id=execution_id,
                    )
                    return res

                # ── THINK ──────────────────────────────────────────────────────────
                elif action == "THINK":
                    thought = decision.get("thought", "")
                    context.add_thought(thought, current_ab)
                    logger.debug("[Runtime] THINK: %s", thought[:120])
                    # THINK is reasoning-only — it cannot credit objective completion.
                    # Only a successful TOOL_CALL result gates objective advancement.

                # ── REFLECT (v0.4.0-b) ─────────────────────────────────────────────
                # An explicit diagnosis step after a cluster of failures.  The LLM
                # names the strategy it intends to follow and may optionally
                # invoke ROLLBACK to rewind context to the last snapshot.  Treated
                # like a structured THINK: persists a thought and a sticky note,
                # does NOT credit objective completion, does NOT count as a fresh
                # THINK for the throttle (resets the consecutive count).
                elif action == "REFLECT":
                    strategy = (decision.get("strategy") or "").strip().upper()
                    rationale = (decision.get("rationale") or "").strip()
                    logger.info(
                        "[Runtime] REFLECT strategy=%s rationale=%s",
                        strategy or "(none)", rationale[:160],
                    )
                    context.add_thought(
                        f"REFLECT — strategy={strategy or 'unspecified'}: {rationale}"[:500],
                        current_ab,
                    )
                    # Sticky note survives a subsequent rollback so the LLM keeps
                    # memory of "we tried X, then we said we'd try Y" even after
                    # context is rewound.
                    if strategy:
                        context.add_sticky_note(
                            f"Reflected: chose strategy {strategy}"
                            + (f" — {rationale[:160]}" if rationale else "")
                        )
                    # Optional ROLLBACK in the same decision: lets the LLM combine
                    # "I'm changing strategy" with "and I want to rewind context".
                    if decision.get("rollback") or strategy == "ROLLBACK_AND_REPLAN":
                        rolled = context.rollback_to_last_snapshot()
                        if rolled is None:
                            context.add_thought(
                                "Requested rollback but no snapshot available — continuing forward.",
                                current_ab,
                            )
                        else:
                            # Replace any pending reflection so the rolled-back
                            # LLM sees a fresh post-rollback nudge, not stale.
                            context.queue_reflection_block(
                                "Context was rolled back to the last snapshot at your "
                                f"request.  Sticky notes above record what was tried.  "
                                f"Now choose a different approach than before."
                            )

                # ── LOAD_RESOURCE ──────────────────────────────────────────────────
                elif action == "LOAD_RESOURCE":
                    resource_type = decision.get("resource_type", "")
                    resource_id   = decision.get("resource_id", "")
                    md_content    = ""
                    load_error    = None

                    try:
                        if resource_type == "skill":
                            obj = self.vault.get_skill(resource_id)
                            if obj.skill_md_path and Path(obj.skill_md_path).exists():
                                md_content = Path(obj.skill_md_path).read_text(encoding="utf-8")
                            else:
                                md_content = f"# {obj.name}\n\n{obj.instructions_md or '_No instructions available._'}"
                        elif resource_type == "tool":
                            obj = self.vault.get_tool(resource_id)
                            if obj.tool_md_path and Path(obj.tool_md_path).exists():
                                md_content = Path(obj.tool_md_path).read_text(encoding="utf-8")
                            else:
                                md_content = f"# {obj.name}\n\n{obj.description}"
                        elif resource_type == "memory":
                            if resource_id == "global":
                                md_content = self.vault.load_global_memory()
                                if not md_content.strip():
                                    md_content = "# Global Memory\n\n_No global memory yet._"
                            else:
                                # resource_id == "self" or shadow id
                                md_path = shadow.memory_md_path
                                if md_path and Path(md_path).exists():
                                    md_content = Path(md_path).read_text(encoding="utf-8")
                                    resource_id = shadow.id
                                else:
                                    md_content = (
                                        f"# Memory: {shadow.name}\n\n"
                                        f"_No memory persisted yet — this shadow has not been consolidated._"
                                    )
                                    resource_id = shadow.id
                        else:
                            load_error = (
                                f"Unknown resource_type {resource_type!r}. "
                                f"Use 'skill', 'tool', or 'memory'."
                            )
                    except KeyError:
                        load_error = f"Resource {resource_type}/{resource_id!r} not found in vault."
                    except OSError as exc:
                        load_error = f"Could not read {resource_type} manifest for {resource_id!r}: {exc}"

                    if load_error:
                        context.add_observation({"error": load_error}, current_ab)
                    else:
                        context.add_resource_load(resource_type, resource_id, md_content, current_ab)
                        logger.debug("[Runtime] LOAD_RESOURCE %s/%s", resource_type, resource_id)
                        # v0.6.1-c: track loaded skills so _maybe_decay_loaded_skills
                        # knows which skills were in scope when failures hit.
                        if resource_type == "skill":
                            loaded = getattr(context, "_loaded_skill_ids", None)
                            if loaded is None:
                                loaded = set()
                                context._loaded_skill_ids = loaded
                            loaded.add(resource_id)

                # ── REQUEST_HARNESS / ASK_OPERATOR (v0.9.7 Reverse-Harness) ─────────
                # The inverse of TOOL_CALL: the agent asks the Governor to provision
                # a capability it lacks (forge a tool) or to ask the operator. Phase
                # 1: GRANT materialises inline + the new tool is offered to the
                # executor; DENY/ESCALATE return a structured observation (full
                # snapshot/suspend/resume operator round-trip is Phase 2).
                elif action in ("REQUEST_HARNESS", "ASK_OPERATOR"):
                    if not _intent_engine_enabled(self.config):
                        context.add_observation({
                            "type": "harness_disabled",
                            "message": "Capability provisioning is not enabled; use an available tool or FAIL.",
                        }, current_ab)
                        continue
                    # v0.9.33 Bug 2: capture the PRE-increment count for the
                    # arbiter (its cap contract is: count == max → cap; count ==
                    # max-1 → proceed), THEN advance the counter so exactly
                    # max_requests_per_run requests succeed. The increment runs
                    # for every evaluated REQUEST_HARNESS/ASK_OPERATOR.
                    _pre_inc_requests = harness_requests_this_run
                    harness_requests_this_run = _next_harness_request_no(
                        harness_requests_this_run
                    )
                    # v0.9.39 Bug 15: the cap is RUN-TREE-WIDE. Bump the persistent
                    # per-root counter (shared across the suspend→resume chain AND
                    # sub-agent children) and use ITS pre-increment total as the
                    # arbiter's cap operand, so a tree of executions can no longer
                    # each restart at 0 and blow past max_requests_per_run. Falls
                    # back to the per-exec count when no sidecar is writable
                    # (no vault / governor — test stubs keep their old behaviour).
                    _act_pre = None
                    if governor is not None:
                        try:
                            _tree_pre = governor.next_runtree_request(
                                root_eid, execution_id, self.vault)
                        except Exception:
                            _tree_pre = None
                        if _tree_pre is not None:
                            _pre_inc_requests = _tree_pre
                        # Fix #6: bump the per-ACTIVITY cumulative counter (keyed by
                        # activity_id — stable across resume AND retry, unlike the
                        # per-run-tree counter), so a task can no longer forge
                        # unboundedly across its retries. Pre-increment total feeds
                        # the arbiter's per-activity cap below.
                        try:
                            _act_pre = governor.next_activity_request(
                                getattr(activity, "id", ""), execution_id, self.vault)
                        except Exception:
                            _act_pre = None
                    try:
                        from systemu.runtime.governor import Governor
                        from systemu.core.models import (
                            HarnessRequest, HarnessKind, HarnessDecision,
                        )
                        if action == "ASK_OPERATOR":
                            # v0.9.35 (P1): optional structured fields. When the
                            # agent supplies a requested_schema (or a `fields`
                            # list), thread it so the operator gets a multi-field
                            # form; absent ⇒ byte-identical free-text question.
                            _ask_spec = {
                                "question": decision.get("question")
                                or decision.get("rationale", "")
                            }
                            _ask_schema = decision.get("requested_schema")
                            if not _ask_schema and isinstance(decision.get("fields"), list):
                                from systemu.runtime.elicitation import (
                                    elicitation_schema_from_fields,
                                    split_secret_fields,
                                )
                                _form_fields, _secret = split_secret_fields(
                                    decision.get("fields") or []
                                )
                                _ask_schema = elicitation_schema_from_fields(_form_fields)
                                if _secret:
                                    _ask_spec["secret_fields"] = [f["name"] for f in _secret]
                            if isinstance(_ask_schema, dict) and _ask_schema.get("properties"):
                                _ask_spec["requested_schema"] = _ask_schema
                            _req = HarnessRequest(
                                kind=HarnessKind.INPUT,
                                spec=_ask_spec,
                                rationale=decision.get("rationale", ""),
                                fallback=decision.get("fallback", ""),
                            )
                        else:
                            try:
                                _hk = HarnessKind((decision.get("kind") or "tool").lower())
                            except Exception:
                                _hk = HarnessKind.TOOL
                            # v0.10.0 pull-decision instrumentation: thread the
                            # agent's stated confidence/attempts + a provenance trail
                            # of what was tried + which blockage signals were active.
                            # Clamp/coerce defensively — a hallucinated value must
                            # never raise and abort the run.
                            try:
                                _conf = float(decision.get("confidence", 0.5))
                            except (TypeError, ValueError):
                                _conf = 0.5
                            _conf = min(1.0, max(0.0, _conf))
                            try:
                                _att = int(decision.get("attempts_before", 0))
                            except (TypeError, ValueError):
                                _att = 0
                            _att = max(0, _att)
                            _crr = getattr(self, "_consec_research_reads", 0)
                            try:
                                _lg_on = bool(_user_payload.get("loop_guard_notice"))
                            except Exception:
                                _lg_on = False
                            _prov = {
                                "tool_attempts": [
                                    {"name": k, "failures": v}
                                    for k, v in self._same_tool_fail_streak.items() if v > 0
                                ],
                                "blocked_signals": (
                                    (["loop_guard"] if _lg_on else [])
                                    + ([f"stuck:{self._iters_since_obj_credit}"]
                                       if self._iters_since_obj_credit >= 1 else [])
                                    + ([f"research_reads:{_crr}"] if _crr >= 1 else [])
                                ),
                            }
                            _req = HarnessRequest(
                                kind=_hk, spec=decision.get("spec") or {},
                                rationale=decision.get("rationale", ""),
                                fallback=decision.get("fallback", ""),
                                # v0.9.7 grant-resume: the agent may declare
                                # blocking semantics — blocking=True parks the run
                                # on a non-auto-grantable ESCALATE (suspend → resume
                                # after the operator decides); blocking=False keeps
                                # the proceed-with-fallback behaviour.
                                blocking=bool(decision.get("blocking", True)),
                                confidence=_conf,
                                attempts_before_request=_att,
                                provenance=_prov,
                            )
                        _gov = governor or Governor(self.config)
                        _used_harness = True   # v0.10.0 Task 1.7(c): this run pulled the harness
                        # v0.9.33 Bug 2/3: thread real arbitration context. This
                        # single change revives the per-run request cap
                        # (requests_this_run — the PRE-increment count, matching
                        # the arbiter's count==max cap contract) AND feeds the
                        # ACTUAL nesting depth to the SUBAGENT depth guard
                        # (subagent_depth), instead of trusting model-claimed
                        # spec.depth alone.
                        _arb_ctx = _harness_arbitration_context(
                            _pre_inc_requests,
                            int(getattr(self, "_subagent_depth", 0)),
                        )
                        # Fix #6: thread the per-ACTIVITY cumulative pre-increment
                        # count so the arbiter can enforce max_requests_per_activity
                        # across this activity's resumes+retries.
                        _arb_ctx["requests_this_activity"] = int(_act_pre or 0)
                        _verdict = _gov.arbitrate(_req, context=_arb_ctx)
                        # v0.9.41: a cap-exceeded DENY otherwise writes NO ledger row
                        # (only the GRANT path logs, via materialise), so the
                        # over-delegation requests vanished from the request-outcome
                        # denominator. Record the arb row explicitly (the sanctioned
                        # manual-append path) with a cap marker so reconciliation
                        # surfaces it as the dedicated `cap_exceeded` category.
                        if (_verdict.decision == HarnessDecision.DENY
                                and getattr(_verdict, "cap_exceeded", False)):
                            try:
                                _gov._ledger_append(
                                    _gov._ledger_entry(
                                        _req, _verdict, {"cap_exceeded": True},
                                        execution_id),
                                    vault=self.vault, execution_id=execution_id,
                                )
                            except Exception:
                                logger.debug("[Runtime] cap-deny ledger write failed",
                                             exc_info=True)
                        if _verdict.decision == HarnessDecision.GRANT:
                            _mat = _gov.materialise(
                                _req, _verdict, vault=self.vault,
                                config=self.config, execution_id=execution_id,
                            )
                            # The failure-fallback branch of the shared helper reads
                            # the request's fallback off the materialise dict (the
                            # only loop-local the helper signature doesn't carry).
                            _mat.setdefault("fallback", _req.fallback)
                            # v0.10.0 Build 3: a GRANTed SUBAGENT is REAL — spawn a
                            # parallel fleet of child ShadowRuntime loops and inject the
                            # collated synthesis (partial-success aware: what ran + what
                            # is missing). Gated behind SYSTEMU_DELEGATE_USE_PARALLEL
                            # (default off → unchanged observation-only path, no regression).
                            # v0.9.33 Bug 3: a CHILD runtime's config has
                            # delegate_use_parallel forced False (SubagentFleet.
                            # _build_child_config), so a granted child SUBAGENT
                            # always takes the observation-only else-branch below —
                            # no native fleet recursion is possible.
                            if (_mat.get("materialised") and _mat.get("subagent")
                                    and getattr(self.config, "delegate_use_parallel", False)):
                                _sa = _mat.get("subagent") or {}
                                _tasks = (
                                    (_req.spec.get("tasks") if isinstance(_req.spec, dict) else None)
                                    or ([_sa.get("task")] if _sa.get("task") else [])
                                )
                                try:
                                    from systemu.runtime.subagent_fleet import SubagentFleet
                                    _fleet = SubagentFleet(
                                        parent_execution_id=execution_id,
                                        config=self.config, vault=self.vault,
                                        # v0.9.39 Bug 15: children join THIS run-tree.
                                        root_execution_id=root_eid,
                                    )
                                    _fres = await _fleet.spawn_children(shadow, activity, _tasks)
                                    # v0.9.33 Bug 3: TERMINAL fleet observation.
                                    # Delegation has run; the agent must synthesize
                                    # the children's results and COMPLETE — it must
                                    # NOT re-delegate (re-entering this branch each
                                    # iteration cascaded sub-fleets). The children's
                                    # work is still credited (synthesis flows through).
                                    context.add_observation({
                                        "type": "harness_granted",
                                        "message": (
                                            (_fres.get("synthesis")
                                             or "Sub-agents completed.")
                                            + " Delegation complete — synthesize these"
                                              " results into your answer and COMPLETE the"
                                              " objective now;"
                                              " do not re-delegate or request more sub-agents."
                                        ),
                                        "fleet": {
                                            "any_succeeded": _fres.get("any_succeeded"),
                                            "all_succeeded": _fres.get("all_succeeded"),
                                            "budget": _fres.get("budget"),
                                            "terminal": True,
                                        },
                                    }, current_ab)
                                except Exception:
                                    logger.debug("[Runtime] SUBAGENT fleet spawn failed", exc_info=True)
                                    context.add_observation({
                                        "type": "harness_grant_failed",
                                        "message": (f"Sub-agent fleet could not run. "
                                                    f"{_req.fallback or 'Proceed with an alternative or FAIL.'}"),
                                    }, current_ab)
                            else:
                                # v0.9.7 Phase 3: apply the materialised grant into THIS
                                # run via the shared helper (same code the deferred harness
                                # grant-resume replays → resume is byte-identical to an
                                # autonomous grant; the helper returns the updated budget).
                                _iter_budget = self._apply_materialised_grant(
                                    _mat, context=context, tools=tools, tool_index=tool_index,
                                    current_ab=current_ab, iter_budget=_iter_budget,
                                )
                        elif (_verdict.decision == HarnessDecision.ESCALATE
                              and getattr(_req, "blocking", True)):
                            # ── v0.9.7 grant-resume: BLOCKING ESCALATE → suspend ──
                            # The Governor can neither auto-grant nor auto-deny a
                            # blocking request — it needs an operator decision and
                            # the run cannot proceed without the capability. Mirror
                            # the stuck-park rail: snapshot the live execution (so a
                            # resume can restore objectives + history), stamp a
                            # __HARNESS_PENDING__ note (the daemon reconciler reads
                            # kind/spec/fallback off it), surface the operator card,
                            # then RETURN a suspended_harness_escalation result so the
                            # Supervisor parks the activity (Task 1: _handle_result
                            # leaves it ASSIGNED, no retry/dead-letter). The operator's
                            # Approve → reconciler → resume_after_grant → resume-peel
                            # (4b) replays the grant via _apply_materialised_grant.
                            try:
                                from systemu.runtime.execution_snapshot import (
                                    capture_from_context, write_snapshot,
                                )
                                _snap = capture_from_context(
                                    execution_id=execution_id,
                                    shadow_id=getattr(shadow, "id", ""),
                                    scroll_id=getattr(scroll, "id", ""),
                                    iteration=iteration,
                                    current_action_block=current_ab,
                                    completed_objectives=set(completed_objectives),
                                    context=context,
                                    activity_id=getattr(activity, "id", ""),
                                    # v0.9.33 Bug 2/3: carry the per-run cap count +
                                    # nesting depth so a resume keeps counting.
                                    requests_this_run=harness_requests_this_run,
                                    subagent_depth=int(getattr(self, "_subagent_depth", 0)),
                                    root_execution_id=root_eid,
                                )
                                import json as _json
                                _snap.sticky_notes.append(
                                    f"__HARNESS_PENDING__::{execution_id}::"
                                    + _json.dumps({
                                        "request_id": _req.request_id,
                                        "kind":       _req.kind.value,
                                        "spec":       _req.spec,
                                        "fallback":   _req.fallback,
                                    })
                                )
                                write_snapshot(_snap)
                            except Exception:
                                logger.debug("[Runtime] harness-escalate snapshot failed",
                                             exc_info=True)
                            try:
                                from systemu.interface.harness_review import surface_harness_request
                                _did = surface_harness_request(
                                    _req, _verdict, execution_id=execution_id,
                                    activity_id=activity.id, shadow_id=shadow.id,
                                    vault=self.vault, arb_context=_arb_ctx,
                                )
                                logger.info(
                                    "[Runtime] harness blocking ESCALATE → parked "
                                    "(snapshot written, operator card %s)", _did,
                                )
                            except Exception:
                                logger.debug("[Runtime] surface_harness_request failed",
                                             exc_info=True)
                            # Parked (not a completed run): reconcile + revoke, but do
                            # NOT record a harness-usage run (record_run=False).
                            _revoke_harness_leases(record_run=False, reconcile=False)   # v0.9.37 Bug 11: defer reconcile to terminal
                            # Suspend-return — match the stuck-park's mechanism
                            # (context.build_result) + carry the resume coords the
                            # Supervisor's _handle_result / reconciler read.
                            _susp = context.build_result(
                                status="suspended_harness_escalation",
                                final_summary=(
                                    "Parked awaiting operator harness decision: "
                                    f"{_req.kind.value} — {_verdict.rationale}"
                                ),
                            )
                            _susp["activity_id"] = activity.id
                            _susp["shadow_id"]   = shadow.id
                            return _susp
                        else:
                            # Non-blocking ESCALATE or DENY: surface (ESCALATE only)
                            # + tell the agent to proceed with its fallback; the loop
                            # CONTINUES. (Non-blocking requests never park.)
                            if _verdict.decision == HarnessDecision.ESCALATE:
                                try:
                                    from systemu.interface.harness_review import surface_harness_request
                                    _did = surface_harness_request(
                                        _req, _verdict, execution_id=execution_id,
                                        activity_id=activity.id, shadow_id=shadow.id,
                                        vault=self.vault, arb_context=_arb_ctx,
                                    )
                                    logger.info("[Runtime] harness ESCALATE surfaced to operator: %s", _did)
                                except Exception:
                                    logger.debug("[Runtime] surface_harness_request failed", exc_info=True)
                            _alts = ", ".join(_verdict.alternatives) if _verdict.alternatives else ""
                            context.add_observation({
                                "type": "harness_" + _verdict.decision.value,
                                "message": (
                                    f"Harness request {_verdict.decision.value}: {_verdict.rationale}. "
                                    f"{('Alternatives: ' + _alts + '. ') if _alts else ''}"
                                    f"{_req.fallback or 'Proceed with an alternative approach or FAIL.'}"
                                    + (" (An operator approval card was raised; proceed with your fallback meanwhile.)"
                                       if _verdict.decision == HarnessDecision.ESCALATE else "")
                                ),
                            }, current_ab)
                    except Exception:
                        logger.debug("[Runtime] REQUEST_HARNESS handling errored", exc_info=True)
                        context.add_observation({
                            "type": "harness_error",
                            "message": "Harness request could not be processed; proceed with available tools or FAIL.",
                        }, current_ab)

                # ── TOOL_CALL ──────────────────────────────────────────────────────
                elif action == "TOOL_CALL":
                    result = await self._handle_tool_call(
                        decision, tools, context, current_ab, dry_run,
                        shadow=shadow, execution_id=execution_id,
                    )
                    # ── v0.9.35 (P1): missing-required → suspend for operator input ──
                    # _handle_tool_call returned a __needs_input__ sentinel: route it
                    # through the SAME blocking-ESCALATE suspend rail the harness uses
                    # (no new status). Headless / no-queue ⇒ fail-closed observation.
                    if (result is not None
                            and isinstance(getattr(result, "parsed", None), dict)
                            and result.parsed.get("__needs_input__")):
                        _req = result.parsed.get("harness_request")
                        # v0.9.35 (review HIGH): surface the form whenever an
                        # operator channel exists — the decision QUEUE (the no-TTY
                        # queue-mode daemon = production topology) OR a TTY. Mirror
                        # the blocking-ESCALATE rail (which has no is_headless guard
                        # and relies on the queue). Fail-closed ONLY when there is
                        # genuinely no operator channel (no queue AND no TTY) —
                        # is_headless() alone wrongly disabled elicitation on every
                        # queue-mode daemon.
                        from systemu.interface.notifications import (
                            is_headless, _get_decision_queue,
                        )
                        if _get_decision_queue() is None and is_headless():
                            context.add_observation({
                                "type": "missing_required_params",
                                "success": False,
                                "tool_name": decision.get("tool_name", "") or "?",
                                "error": (
                                    "Tool needs required parameter(s) that are "
                                    "missing, and no operator is available to "
                                    "supply them (non-interactive run). Provide the "
                                    "values yourself in the next TOOL_CALL, use an "
                                    "alternative tool, or FAIL — do NOT fabricate."),
                                "error_type": "missing_required_params",
                            }, current_ab)
                            continue
                        # Interactive: arbitrate (INPUT always ESCALATEs) + suspend.
                        try:
                            from systemu.runtime.governor import Governor
                            from systemu.core.models import HarnessDecision
                            _gov = governor or Governor(self.config)
                            _arb_ctx = _harness_arbitration_context(
                                harness_requests_this_run,
                                int(getattr(self, "_subagent_depth", 0)),
                            )
                            _verdict = _gov.arbitrate(_req, context=_arb_ctx)
                        except Exception:
                            logger.debug("[Runtime] INPUT arbitrate failed", exc_info=True)
                            context.add_observation({
                                "type": "missing_required_params",
                                "success": False,
                                "tool_name": decision.get("tool_name", "") or "?",
                                "error": ("Could not raise an input request; provide "
                                          "the missing parameters yourself or FAIL."),
                                "error_type": "missing_required_params",
                            }, current_ab)
                            continue
                        # Snapshot + __HARNESS_PENDING__ (mirror the blocking-ESCALATE rail).
                        try:
                            from systemu.runtime.execution_snapshot import (
                                capture_from_context, write_snapshot,
                            )
                            _snap = capture_from_context(
                                execution_id=execution_id,
                                shadow_id=getattr(shadow, "id", ""),
                                scroll_id=getattr(scroll, "id", ""),
                                iteration=iteration,
                                current_action_block=current_ab,
                                completed_objectives=set(completed_objectives),
                                context=context,
                                activity_id=getattr(activity, "id", ""),
                                requests_this_run=harness_requests_this_run,
                                subagent_depth=int(getattr(self, "_subagent_depth", 0)),
                                root_execution_id=root_eid,
                            )
                            import json as _json
                            _snap.sticky_notes.append(
                                f"__HARNESS_PENDING__::{execution_id}::"
                                + _json.dumps({
                                    "request_id": _req.request_id,
                                    "kind":       _req.kind.value,
                                    "spec":       _req.spec,
                                    "fallback":   _req.fallback,
                                })
                            )
                            write_snapshot(_snap)
                        except Exception:
                            logger.debug("[Runtime] INPUT-escalate snapshot failed",
                                         exc_info=True)
                        try:
                            from systemu.interface.harness_review import surface_harness_request
                            _did = surface_harness_request(
                                _req, _verdict, execution_id=execution_id,
                                activity_id=activity.id, shadow_id=shadow.id,
                                vault=self.vault,
                            )
                            logger.info(
                                "[Runtime] missing-required INPUT → parked "
                                "(operator card %s)", _did,
                            )
                        except Exception:
                            logger.debug("[Runtime] surface_harness_request failed",
                                         exc_info=True)
                        _revoke_harness_leases(record_run=False, reconcile=False)   # v0.9.37 Bug 11: defer reconcile to terminal
                        _susp = context.build_result(
                            status="suspended_harness_escalation",
                            final_summary=(
                                "Parked awaiting operator input: missing required "
                                f"parameter(s) for tool "
                                f"{decision.get('tool_name', '') or '?'}."
                            ),
                        )
                        _susp["activity_id"] = activity.id
                        _susp["shadow_id"]   = shadow.id
                        return _susp
                    if result is None:
                        continue   # User denied destructive call
                    tool_call_count += 1
                    # Define tool_name once for this branch. Pre-existing latent bug:
                    # the W12/F9 credit-nudge code below referenced a bare ``tool_name``
                    # that was never assigned in this branch → NameError on any tool
                    # call that didn't go through the completes_objective path.
                    tool_name = decision.get("tool_name") or "?"
                    # v0.10.0 Task 1.6: record invoked tool for outcome reconciliation.
                    try:
                        _called_tools.add(tool_name)
                    except Exception:
                        pass

                    # v0.8.21: stuck-guard — record tool outcome.
                    # (objective-credit reset is applied below at the credit site.)
                    self._update_stuck_counters(
                        action="TOOL_CALL",
                        tool_name=decision.get("tool_name") or "?",
                        tool_success=bool(getattr(result, "success", False)),
                        credited_obj_id=None,
                    )

                    # v0.9.7: deterministic loop-guard — signature is
                    # (tool, args, success-class), so a tool repeatedly called
                    # with the same args and the same outcome escalates to a
                    # corrective nudge (warn) then a forced finish (block) on the
                    # NEXT iteration. Never let it crash the loop.
                    try:
                        _lg = loop_guard.record(
                            decision.get("tool_name", "") or "",
                            decision.get("parameters") or {},
                            bool(getattr(result, "success", False)),
                        )
                        if _lg:
                            loop_guard_nudge = _lg
                    except Exception:
                        logger.debug("[Runtime] loop_guard.record failed", exc_info=True)

                    # ── v0.9.8 KEYSTONE: auto-audit successful tool calls ──────────
                    # The objective verifier only sees the StateDelta (files +
                    # audit_entries + chat_result). The runtime never wrote an audit
                    # entry for ordinary tool calls, so an intermediate "obtain X"
                    # objective had ZERO durable evidence and got rejected. Write one
                    # compact audit row for every tool that SUCCEEDS so that
                    # state_delta.compute_delta (which calls query_action_audit) can
                    # surface it to the verifier. Best-effort — must NEVER break the run.
                    if result is not None and getattr(result, "success", False):
                        try:
                            _audit_obj_id = decision.get("completes_objective")
                            if not isinstance(_audit_obj_id, int):
                                _audit_obj_id = _current_objective_id_for_audit(
                                    objectives if use_objectives else None,
                                    completed_objectives if use_objectives else None,
                                )
                            _audit_entry = _build_tool_audit_entry(
                                execution_id=execution_id,
                                objective_id=_audit_obj_id,
                                tool_name=decision.get("tool_name", "") or "?",
                                params=decision.get("parameters") or {},
                            )
                            self.vault.append_action_audit(
                                _audit_entry,
                                namespace_path=getattr(self, "_audit_namespace", None),
                            )
                        except Exception:
                            logger.debug("[Runtime] v0.9.8 tool-success auto-audit failed", exc_info=True)

                    # ── v0.9.8 (B2): research-loop convergence steer ───────────────
                    # The keystone credits "search/obtain" objectives from audit
                    # evidence, which resets _iters_since_obj_credit — so the stall
                    # path never fires on a "research forever, never write" loop
                    # (observed live: 9 web_search/web_read calls, no file, MAX_ITER).
                    # This counter is INDEPENDENT of objective-credit: it counts
                    # consecutive read-only research calls and force-steers the agent
                    # to produce its deliverable once it has clearly gathered enough.
                    try:
                        _rl_thresh = int(getattr(self.config, "research_loop_threshold", 5) or 5)
                        _rl_cap = int(getattr(self.config, "research_loop_max_steers", 2) or 2)
                        self._consec_research_reads, self._research_loop_steers_used, _rl_steer = \
                            _research_loop_steer(
                                tool_name=decision.get("tool_name", "") or "",
                                success=(result is not None and getattr(result, "success", False)),
                                consec_reads=self._consec_research_reads,
                                steers_used=self._research_loop_steers_used,
                                threshold=_rl_thresh, cap=_rl_cap,
                            )
                        if _rl_steer:
                            self._operator_hint = _rl_steer
                            logger.info(
                                "[Runtime] B2 research-loop convergence steer %d/%d "
                                "(>=%d consecutive read-only research calls, no deliverable)",
                                self._research_loop_steers_used, _rl_cap, _rl_thresh,
                            )
                    except Exception:
                        logger.debug("[Runtime] B2 research-loop steer failed", exc_info=True)

                    # v0.8.16: per-iteration detail event AFTER the tool runs, so
                    # the bounded `details` dict carries the tool result the live
                    # panes render on expand.  Raw LLM is referenced via llm_ref.
                    try:
                        from systemu.interface.event_bus import EventBus
                        _tool_result_for_event = (
                            result.parsed if getattr(result, "parsed", None) is not None
                            else getattr(result, "output", None) or result
                        )
                        EventBus.get().publish(self._iteration_event(
                            iteration=iteration,
                            decision=decision,
                            tool_result=_tool_result_for_event,
                            execution_id=execution_id,
                            llm_ref=_last_llm_ref,
                        ))
                    except Exception:
                        pass  # EventBus is optional — never break execution

                    # v0.8.17: fail-fast after 3 consecutive degraded web-search results.
                    # "degraded" means the entire provider chain failed (not just zero results) —
                    # reset on any non-degraded search so a single blip doesn't end the run.
                    _tool_name_for_ff = decision.get("tool_name", "")
                    _parsed_for_ff = getattr(result, "parsed", None)
                    if _is_degraded_search_result(_tool_name_for_ff, _parsed_for_ff):
                        self._consec_degraded_search += 1
                    else:
                        self._consec_degraded_search = 0
                    if self._consec_degraded_search >= _MAX_CONSEC_DEGRADED_SEARCH:
                        _ff_msg = (
                            f"Web search capability unavailable — search backends failed "
                            f"{self._consec_degraded_search}x. Set SYSTEMU_TAVILY_API_KEY or "
                            f"SYSTEMU_EXA_API_KEY for reliable search."
                        )
                        logger.warning("[Runtime] fail-fast: %s", _ff_msg)
                        self._append_to_shadow_log(
                            shadow, execution_id, "failure", _ff_msg,
                            iteration_count=iteration, tool_calls_made=tool_call_count,
                            objectives_completed=len(completed_objectives) if use_objectives else current_ab - 1,
                            objectives_total=total_objectives if use_objectives else len(scroll_json),
                            duration_seconds=__import__("time").time() - exec_start,
                        )
                        _ff_res = context.build_result(
                            status="failure",
                            final_summary=f"Shadow reported failure: {_ff_msg}",
                            error=_ff_msg,
                        )
                        _record_terminal_telemetry(
                            shadow=shadow, execution_id=execution_id, scroll=scroll,
                            status="failure", iteration=iteration,
                        )
                        _dispatch_refinery(shadow, scroll, _ff_res, context, self.config, self.vault)
                        return _ff_res

                    # v0.6.9: iteration-loop circuit breaker — bail when the LLM
                    # is stuck re-invoking the same broken tool with the same
                    # failure class. Saves 20+ wasted iterations on a recoverable
                    # blocker (op needs to use the recovery URL).
                    if not result.success:
                        cb_tool_name = decision.get("tool_name", "") or "?"
                        cb_reason = (
                            (result.parsed or {}).get("error_type")
                            or (result.parsed or {}).get("classified_reason")
                            or "TOOL_FAILED"
                        )
                        tripped = self._record_tool_failure(cb_tool_name, cb_reason)
                        if tripped:
                            logger.warning(
                                "[Runtime] v0.6.9 circuit breaker tripped: "
                                "tool=%s reason=%s after %d consecutive failures",
                                cb_tool_name, cb_reason, self.CIRCUIT_BREAKER_FAILURES,
                            )
                            return {
                                "status": "failure",
                                "final_summary": (
                                    f"Circuit breaker: tool {cb_tool_name} failed "
                                    f"{self.CIRCUIT_BREAKER_FAILURES} consecutive "
                                    f"times with reason {cb_reason}. Apply the fix at "
                                    f"the recovery URL surfaced in prior iterations."
                                ),
                                "execution_id": execution_id,
                            }

                    if use_objectives:
                        # Credit objective only when the tool actually succeeded.
                        # A failed tool (result.success=False) cannot advance the objective —
                        # the shadow must try again or choose a different approach.
                        completed_obj = decision.get("completes_objective")
                        if isinstance(completed_obj, int) and completed_obj not in completed_objectives:
                            if result is not None and result.success:
                                # v0.9.1 (Layer 4): run the durable-outcome verifier before
                                # crediting.  Best-effort: verifier errors fall through to the
                                # legacy credit so a bad verifier config can't stall the run.
                                _do_credit = True
                                try:
                                    _obj_for_verify = next(
                                        (o for o in objectives if o.id == completed_obj), None)
                                    if _obj_for_verify is not None:
                                        _vstate = self._objective_states.setdefault(
                                            completed_obj, ObjectiveState())
                                        # RCA fix: use the run-start baseline (captured
                                        # before any deliverable was written) instead of
                                        # the lazy post-write capture that absorbs it.
                                        if (getattr(_vstate, "baseline", None) is None
                                                and _run_verifier_baseline is not None):
                                            _vstate.baseline = _run_verifier_baseline
                                        _v_outcome = process_completion_claim(
                                            objective=_obj_for_verify,
                                            vault=self.vault,
                                            config=self.config,
                                            execution_id=execution_id,
                                            default_output_dir=_resolve_verifier_output_dir(
                                                self.config, getattr(self, "user_profile", None)
                                            ),
                                            chat_result=None,
                                            state=_vstate,
                                            fresh_work_since_last_call=self._fresh_work_since_last_verifier_call,
                                            user_id=None,
                                        )
                                        self._objective_states[completed_obj] = _v_outcome.state
                                        self._fresh_work_since_last_verifier_call = False
                                        _do_credit = _v_outcome.credited
                                        if not _v_outcome.credited:
                                            if _v_outcome.bypassed_verifier:
                                                logger.debug(
                                                    "[Runtime] Verifier per-turn cap hit for obj=%d "
                                                    "— bypassed, not crediting this turn.",
                                                    completed_obj,
                                                )
                                            else:
                                                logger.warning(
                                                    "[Runtime] Verifier rejected obj=%d: %s",
                                                    completed_obj, _v_outcome.feedback_message,
                                                )
                                            if _v_outcome.feedback_message:
                                                context.add_observation(
                                                    {
                                                        "type": "verifier_rejection",
                                                        "objective_id": completed_obj,
                                                        "message": _v_outcome.feedback_message,
                                                    },
                                                    current_ab,
                                                )
                                            if _v_outcome.escalate_stuck:
                                                # Treat budget-exceeded rejection as a stuck event.
                                                self._iters_since_obj_credit = max(
                                                    self._iters_since_obj_credit,
                                                    _stuck_thresholds()[0],
                                                )
                                except Exception:
                                    logger.debug(
                                        "[Runtime] v0.9.1 verifier hook crashed — crediting without verify",
                                        exc_info=True,
                                    )

                                if _do_credit:
                                    completed_objectives.add(completed_obj)
                                    # v0.8.19 (R2): publish updated objective_state so the
                                    # live pane ticks the checklist.  Best-effort.
                                    try:
                                        from systemu.interface.event_bus import EventBus
                                        EventBus.get().publish(_objective_state_event(
                                            objectives, completed_objectives, execution_id, stamp=self._stamp))
                                    except Exception:
                                        pass
                                    # v0.8.21: stuck-guard — credit resets BOTH counters.
                                    self._update_stuck_counters(
                                        action="TOOL_CALL",
                                        tool_name=decision.get("tool_name") or "?",
                                        tool_success=True,
                                        credited_obj_id=completed_obj,
                                    )
                                    logger.info("[Runtime] Objective %d complete. %d/%d done.",
                                                completed_obj, len(completed_objectives), total_objectives)

                                    if (len(completed_objectives) % SNAPSHOT_INTERVAL) == 0:
                                        context.take_snapshot(len(completed_objectives), self.config)

                                    if len(completed_objectives) >= total_objectives:
                                        logger.info("[Runtime] All objectives complete via advancement.")
                                        self._append_to_shadow_log(
                                            shadow, execution_id, "success", "All objectives completed.",
                                            iteration_count=iteration, tool_calls_made=tool_call_count,
                                            objectives_completed=len(completed_objectives),
                                            objectives_total=total_objectives,
                                            duration_seconds=__import__("time").time() - exec_start,
                                        )
                                        res = context.build_result(
                                            status="success",
                                            final_summary="All objectives completed successfully.",
                                        )
                                        _revoke_harness_leases(run_success=True)   # v0.9.36 Bug 9: finalize (idempotent)
                                        _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                                        _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                                        return res
                            else:
                                logger.warning(
                                    "[Runtime] TOOL_CALL claimed completes_objective=%d "
                                    "but tool failed (success=%s) — objective NOT credited.",
                                    completed_obj,
                                    result.success if result is not None else None,
                                )
                                # W12 (F9): remember the failed claim so a later
                                # SUCCESS of the same tool can nudge a re-claim.
                                try:
                                    getattr(self, "_failed_objective_claims", {})[
                                        tool_name] = completed_obj
                                except Exception:
                                    pass
                        elif (completed_obj is None and result is not None
                                and result.success):
                            # W12 (F9): this tool previously claimed an objective
                            # and FAILED; now it succeeded with no claim. Without
                            # the nudge the objective is never credited, the run
                            # never completes, and the watchdog cancels finished
                            # work (seen live in the A2 audit).
                            _missed = getattr(
                                self, "_failed_objective_claims", {}).pop(
                                tool_name, None)
                            if (_missed is not None
                                    and _missed not in completed_objectives):
                                context.add_observation(
                                    {
                                        "type": "credit_nudge",
                                        "objective_id": _missed,
                                        "message": (
                                            f"Your earlier FAILED attempt claimed "
                                            f"objective {_missed}; this call "
                                            f"SUCCEEDED without a claim. If the "
                                            f"objective is now complete, declare "
                                            f"completes_objective={_missed} on your "
                                            f"next TOOL_CALL or COMPLETE action."),
                                    },
                                    current_ab,
                                )
                    else:
                        # Legacy ActionBlock completion tracking
                        completed_ab = decision.get("completes_action_block")
                        if isinstance(completed_ab, int) and completed_ab >= current_ab:
                            current_ab = completed_ab + 1
                            logger.info("[Runtime] Advanced to ActionBlock %d", current_ab)

                            if (current_ab - last_snap_ab) >= SNAPSHOT_INTERVAL:
                                context.take_snapshot(completed_ab, self.config)
                                last_snap_ab = completed_ab

                            if current_ab > len(scroll_json):
                                logger.info("[Runtime] All ActionBlocks complete via advancement.")
                                self._append_to_shadow_log(
                                    shadow, execution_id, "success", "All steps completed.",
                                    iteration_count=iteration, tool_calls_made=tool_call_count,
                                    objectives_completed=current_ab - 1, objectives_total=len(scroll_json),
                                    duration_seconds=__import__("time").time() - exec_start,
                                )
                                res = context.build_result(
                                    status="success",
                                    final_summary="All ActionBlocks completed successfully.",
                                )
                                _revoke_harness_leases(run_success=True)   # v0.9.36 Bug 9: finalize (idempotent)
                                _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                                _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                                return res

                    # v0.8.21: stuck-guard — check after this iteration's effects are recorded.
                    triggered, reason = self._stuck_trigger()
                    if triggered and use_objectives:
                        # v0.9.7 intent-engine: before parking on per-objective stuck,
                        # accept on GOAL-level success. The per-objective contract is
                        # fragile (impl-path / path-mangling / delta-timing all reject
                        # legitimate work); the goal verifier (epoch baseline) just
                        # checks whether the GOAL's artifact exists. Default OFF.
                        if _intent_engine_enabled(self.config) and _adherence != "strict" and _intent_goal_success(
                                vault=self.vault, config=self.config,
                                user_profile=getattr(self, "user_profile", None),
                                scroll=scroll, execution_id=execution_id,
                                summary=(decision.get("summary") if isinstance(decision, dict) else None)):
                            logger.info(
                                "[Runtime] intent-engine: goal met at stuck-park — "
                                "finalizing SUCCESS instead of parking (%d/%d objectives credited).",
                                len(completed_objectives), total_objectives,
                            )
                            self._append_to_shadow_log(
                                shadow, execution_id, "success",
                                "Goal completed (goal-level verification at stuck-park).",
                                iteration_count=iteration, tool_calls_made=tool_call_count,
                                objectives_completed=len(completed_objectives),
                                objectives_total=total_objectives,
                                duration_seconds=__import__("time").time() - exec_start,
                            )
                            res = context.build_result(
                                status="success",
                                final_summary="Goal completed (verified at goal level).",
                            )
                            _revoke_harness_leases(run_success=True)
                            _record_shadow_metric(shadow=shadow, scroll=scroll, status="success")
                            _dispatch_refinery(shadow, scroll, res, context, self.config, self.vault)
                            return res
                        # which objective are we stuck on? the first pending (lowest id whose deps are met & not done)
                        pending = [o for o in objectives if o.id not in completed_objectives
                                   and all(d in completed_objectives for d in (o.depends_on or []))]
                        stuck_obj = pending[0] if pending else objectives[-1]
                        # v0.8.22.1 (Fix 4): exclude tools that ultimately succeeded.
                        # _update_progress_counters sets a succeeded tool's streak to 0
                        # but keeps the key; only tools with an active failure streak
                        # belong in the operator's "tools tried" line.
                        # W6.3: ALL tools attempted since last credit — failure
                        # streaks alone hid successful-but-useless calls, so the
                        # operator read "Tools tried: (none)" mid-loop.
                        tools_tried = self._tools_tried_since_credit()
                        # v0.9.8 Phase 2: autonomous mid-run steering coach. Before
                        # escalating to a human operator, FIRST try to self-steer:
                        # ask an LLM for one concrete corrective instruction and inject
                        # it as a hint, then retry the loop. Only after
                        # auto_coach_max_steers self-steers fail do we fall through to
                        # the operator escalation below.
                        if getattr(self.config, "auto_coach_enabled", True) and \
                                self._coach_steers_used < int(getattr(self.config, "auto_coach_max_steers", 2)):
                            try:
                                from systemu.runtime.coach import generate_steer
                                _steer = generate_steer(
                                    objective=stuck_obj,
                                    reason=reason,
                                    tools_tried=tools_tried,
                                    history=_build_history_slice(context),
                                    config=self.config,
                                )
                            except Exception:
                                logger.debug("[Runtime] coach generate_steer raised — no steer",
                                             exc_info=True)
                                _steer = ""
                            if _steer:
                                self._operator_hint = (
                                    f"## Coach steer (Objective {stuck_obj.id})\n{_steer}"
                                )
                                self._iters_since_obj_credit = 0
                                self._same_tool_fail_streak.clear()
                                self._coach_steers_used += 1
                                logger.info(
                                    "[Runtime] auto-coach steer %d/%d on Objective %s: %s",
                                    self._coach_steers_used,
                                    int(getattr(self.config, "auto_coach_max_steers", 2)),
                                    stuck_obj.id, _steer,
                                )
                                try:
                                    context.add_thought(
                                        f"Auto-coach self-steer {self._coach_steers_used}: {_steer}",
                                        current_ab,
                                    )
                                except Exception:
                                    pass
                                # Retry the loop with the steer applied; do NOT escalate
                                # to the operator this round.
                                continue
                        # Fix #2/#4: coach budget spent + still stuck on the same
                        # objective for N rounds → fail fast instead of re-parking
                        # (which just spawns more operator gates the agent can't
                        # satisfy, e.g. an input file that doesn't exist).
                        _fin_after = int(getattr(self.config, "auto_coach_finalize_after_rounds", 2) or 0)
                        _round_now = self._stuck_round_for_obj.get(stuck_obj.id, 0) + 1
                        if _should_force_finalize_stuck(
                                coach_steers_used=self._coach_steers_used,
                                max_steers=int(getattr(self.config, "auto_coach_max_steers", 2)),
                                stuck_round=_round_now,
                                finalize_after_rounds=_fin_after):
                            logger.warning(
                                "[Runtime] no-progress force-finalize: Objective %s stuck "
                                "%d rounds after coach budget — %s",
                                stuck_obj.id, _round_now, reason,
                            )
                            return self._finalize_stuck(
                                context=context, status="partial", reason=reason,
                                stuck_on=stuck_obj.id, completed=list(completed_objectives),
                                iteration=iteration, tool_calls_made=tool_call_count,
                                scroll=scroll, shadow=shadow, execution_id=execution_id,
                                exec_start=exec_start, total_objectives=total_objectives)
                        # v0.8.22.1 (R1): persist a resume snapshot at stuck-park so
                        # the operator's answer (via the daemon re-dispatch handler)
                        # can resume this run with completed objectives intact.
                        try:
                            from systemu.runtime.execution_snapshot import (
                                capture_from_context, write_snapshot,
                            )
                            _snap = capture_from_context(
                                execution_id=execution_id,
                                shadow_id=getattr(shadow, "id", ""),
                                scroll_id=getattr(scroll, "id", ""),
                                iteration=iteration,
                                current_action_block=current_ab,
                                completed_objectives=set(completed_objectives),
                                context=context,
                                activity_id=getattr(activity, "id", ""),
                                # v0.9.33 Bug 2/3: the stuck-park is the common
                                # operator-park path — carry the cap count + depth
                                # so a resumed run keeps counting toward the cap.
                                requests_this_run=harness_requests_this_run,
                                subagent_depth=int(getattr(self, "_subagent_depth", 0)),
                                root_execution_id=root_eid,
                            )
                            # R3: persist the per-objective stuck round counters so
                            # rounds accumulate across resumes (carried as a sticky tag).
                            import json as _json
                            _snap.sticky_notes.append(
                                "__STUCK_ROUNDS__::" + _json.dumps(self._stuck_round_for_obj)
                            )
                            # Fix #5: carry the no-progress counter so the resumed run
                            # keeps its 'iterations since objective credit' pressure
                            # instead of restarting at 0 and re-doing futile work.
                            _snap.sticky_notes.append(
                                _encode_no_progress_note(self._iters_since_obj_credit))
                            write_snapshot(_snap)
                        except Exception:
                            logger.debug("[Runtime] stuck-park snapshot failed", exc_info=True)
                        ans = self._ask_stuck_or_degrade(execution_id=execution_id,
                                                           current_objective=stuck_obj,
                                                           tools_tried=tools_tried, reason=reason,
                                                           scroll_id=getattr(scroll, "id", ""),
                                                           activity_id=getattr(activity, "id", ""),
                                                           shadow_id=getattr(shadow, "id", ""))
                        if ans is None:
                            # headless — degrade as 'partial' (closest to MaxIterations semantics)
                            return self._finalize_stuck(context=context, status="partial",
                                                         reason=reason, stuck_on=stuck_obj.id,
                                                         completed=list(completed_objectives),
                                                         iteration=iteration,
                                                         tool_calls_made=tool_call_count,
                                                         scroll=scroll, shadow=shadow,
                                                         execution_id=execution_id,
                                                         exec_start=exec_start,
                                                         total_objectives=total_objectives)
                        from functools import partial as _partial
                        _fin = _partial(self._finalize_stuck, context=context,
                                        reason=reason, stuck_on=stuck_obj.id,
                                        completed=list(completed_objectives),
                                        iteration=iteration, tool_calls_made=tool_call_count,
                                        scroll=scroll, shadow=shadow,
                                        execution_id=execution_id, exec_start=exec_start,
                                        total_objectives=total_objectives)
                        _action, _res = self._apply_stuck_answer(stuck_obj, ans, finalize=_fin)
                        if _action == "finalize":
                            return _res
                        # else "continue": hint applied, loop proceeds

                else:
                    logger.warning("[Runtime] Unknown action: %s — treating as THINK", action)
                    context.add_thought(f"Unrecognised action type: {action}", current_ab)

            # ── Max iterations hit ─────────────────────────────────────────────────
            logger.warning("[Runtime] Max iterations (%d) reached without COMPLETE.", MAX_ITERATIONS)
            self._append_to_shadow_log(
                shadow, execution_id, "partial",
                f"Reached max iterations ({MAX_ITERATIONS}).",
                iteration_count=iteration, tool_calls_made=tool_call_count,
                objectives_completed=len(completed_objectives) if use_objectives else current_ab - 1,
                objectives_total=total_objectives if use_objectives else len(scroll_json),
                duration_seconds=__import__("time").time() - exec_start,
            )
            _record_terminal_telemetry(
                shadow=shadow, execution_id=execution_id, scroll=scroll,
                status="partial", iteration=iteration,
                extra={"reason": "MaxIterationsExceeded"},
            )
            # Fix 2: an honest, specific partial summary (uncompleted objectives +
            # which tools structurally failed) instead of the generic one, and a
            # structural_failure flag so the supervisor skips re-running into the
            # same wall.
            _failed_tools = sorted(getattr(self, "_structural_tool_failures", set()))
            if use_objectives:
                _done = len(completed_objectives)
                _pending = [getattr(o, "goal", str(o.id)) for o in scroll.objectives
                            if o.id not in completed_objectives][:5]
            else:
                _done, _pending = current_ab - 1, []
            _parts = [f"Execution reached max iterations ({MAX_ITERATIONS}); task incomplete.",
                      f"Objectives completed: {_done}/{total_objectives}."]
            if _pending:
                _parts.append("Not completed: " + "; ".join(_pending) + ".")
            if _failed_tools:
                _parts.append("Tools that structurally failed: " + ", ".join(_failed_tools) + ".")
            res = context.build_result(
                status="partial",
                final_summary=" ".join(_parts),
                error="MaxIterationsExceeded",
            )
            res["structural_failure"] = bool(_failed_tools)
            return res
        finally:
            # v0.9.36 Bug 9: GUARANTEE the harness terminal finalize runs exactly
            # once per run — request-outcome reconciliation + lease-revoke + MCP
            # unregister — on EVERY exit, including the partial / max-iterations /
            # exception paths no explicit call site covers. Idempotent (no-ops if a
            # terminal path already finalized); run_success=False/record_run=True is
            # correct for these uncovered fall-through exits (all non-success
            # terminals — the success and suspend exits finalize explicitly
            # upstream). Guarded: an exception before the closure is defined leaves
            # the name unbound.
            try:
                _revoke_harness_leases(run_success=False, record_run=True)
            except Exception:
                pass
            try:
                from systemu.runtime.chat_submission_ctx import set_chat_submission_id
                set_chat_submission_id(None, reset_token=self._chat_submission_token)
            except Exception:
                pass
            try:
                from systemu.runtime.chat_submission_ctx import set_execution_id
                set_execution_id(None, reset_token=getattr(self, "_execution_id_token", None))
            except Exception:
                pass
            try:
                # v0.9.34 P0 (H3): reset the run-scoped MCP session-id carrier.
                # getattr(..., None) so an early-exit path that never reached the
                # set-point resets harmlessly (reset_token=None is the clear branch).
                from systemu.runtime.mcp_run_ctx import set_mcp_session_id
                set_mcp_session_id(None, reset_token=getattr(
                    self, "_mcp_session_token", None))
            except Exception:
                pass

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _handle_tool_call(
        self,
        decision:    Dict[str, Any],
        tools:       List[Tool],
        context:     ExecutionContext,
        current_ab:  int,
        dry_run:     bool,
        *,
        shadow:      Optional[Shadow] = None,
        execution_id: Optional[str] = None,
    ) -> Optional[ToolResult]:
        """Execute a TOOL_CALL decision. Returns None if user denied.

        ``shadow`` + ``execution_id`` are optional for backward-compatibility
        with any older test paths that call the method directly, but the
        runtime's own call site supplies both so the memory invalidator
        can write contradicting lessons when a previously-failed tool now
        succeeds.
        """
        tool_name  = decision.get("tool_name", "")
        # Extract args, tolerating aliases that some LLMs emit instead of
        # the prompt-specified `parameters` key.  Observed: Deepseek
        # occasionally uses `args` or `inputs`; left unhandled this leads
        # to every tool call running with no kwargs and failing its
        # required-arg guard.
        parameters = decision.get("parameters")
        if not parameters:
            for alias in ("args", "inputs", "kwargs", "input", "arguments"):
                if decision.get(alias):
                    parameters = decision[alias]
                    logger.warning(
                        "[Runtime] LLM used '%s' key instead of 'parameters' "
                        "for tool=%s — accepting; prompt may need clarification",
                        alias, tool_name,
                    )
                    break
        parameters = parameters or {}
        # v0.9.7: some LLMs emit a bare scalar (e.g. a URL string) instead of a
        # {param: value} dict for single-argument tools. Without this guard the
        # next line crashes on ``parameters.keys()`` (AttributeError on str).
        if not isinstance(parameters, dict):
            _orig_params = parameters
            parameters = _coerce_scalar_parameter(parameters, tool_name, tools)
            logger.warning(
                "[Runtime] tool=%s received non-dict parameters %r — coerced to %s",
                tool_name, _orig_params, list(parameters.keys()) or "{}",
            )
        logger.debug("[Runtime] TOOL_CALL tool=%s args=%s",
                     tool_name, list(parameters.keys()))
        reasoning  = decision.get("reasoning", "")
        is_destructive = decision.get("is_destructive", False)

        # Fallback heuristic check
        if not is_destructive:
            is_destructive = ToolSandbox.is_destructive_call(tool_name, parameters)

        # ── v0.9.35 (P1): missing-required detection seam ────────────────────
        # One chokepoint for v1/v2/MCP: after alias/scalar coercion (above) and
        # BEFORE the destructive gate, the v2 short-circuit, and v1 dispatch.
        # A non-empty gap builds a kind=INPUT HarnessRequest carrying the
        # requested_schema + pending_tool and returns a SENTINEL ToolResult.
        # The TOOL_CALL loop branch routes the sentinel through the existing
        # blocking-ESCALATE suspend rail (one suspend implementation).
        # Empty schema ⇒ empty gap ⇒ zero behavior change.
        if not dry_run:
            try:
                from systemu.runtime.param_validation import missing_required
                from systemu.runtime.tool_registry_v2 import registry as _v2_reg
                _gap = missing_required(
                    tool_name, parameters, tools=tools, v2_registry=_v2_reg,
                )
            except Exception:
                logger.debug("[Runtime] missing_required seam errored — skipping",
                             exc_info=True)
                _gap = []
            if _gap:
                from systemu.core.models import HarnessRequest, HarnessKind
                from systemu.runtime.elicitation import (
                    elicitation_schema_from_fields, split_secret_fields,
                )
                # v0.9.35 (P1): URL-mode secrets. Split credential fields OUT of
                # the typed form before building requested_schema, so a secret
                # never enters the form schema (and therefore never the LLM/logs).
                # Secret NAMES are carried for the URL-mode card label only.
                _form_fields, _secret_fields = split_secret_fields(_gap)
                _req = HarnessRequest(
                    kind=HarnessKind.INPUT,
                    spec={
                        "question": (
                            f"Tool '{tool_name}' needs "
                            f"{len(_gap)} more parameter(s) to run."
                        ),
                        "requested_schema": elicitation_schema_from_fields(_form_fields),
                        "secret_fields": [f["name"] for f in _secret_fields],
                        "pending_tool": {
                            "tool_name": tool_name,
                            "parameters": dict(parameters),
                        },
                    },
                    rationale=(
                        f"Missing required parameter(s): "
                        f"{', '.join(f['name'] for f in _gap)}."
                    ),
                    fallback=reasoning or "",
                    blocking=True,
                )
                logger.info(
                    "[Runtime] tool=%s missing required %s — raising INPUT elicitation",
                    tool_name, [f["name"] for f in _gap],
                )
                return ToolResult(
                    success=False,
                    parsed={"__needs_input__": True, "harness_request": _req},
                    error="missing_required_params",
                )

        # Safety gate for destructive calls.
        # v0.9.32 (D.5): shell tools are gated at the ToolSandbox chokepoint
        # (command gate → PendingOperatorDecision → park/resume); the legacy
        # headless auto-deny here would pre-empt that, so we skip it for them.
        if is_destructive and not dry_run and _legacy_autodeny_applies(tool_name):
            approved = confirm(
                f"Shadow '{self.vault}' wants to perform a potentially destructive action:\n"
                f"  Tool: {tool_name}\n"
                f"  Params: {json.dumps(parameters)}\n"
                f"  Reason: {reasoning}\n"
                "  Allow?",
                default=False,
            )
            if not approved:
                # Wave 1.1: in headless contexts confirm() auto-denies — make
                # that VISIBLE (event log + WARNING) instead of a silent
                # degradation the operator only discovers via a partial result.
                from systemu.interface.notifications import is_headless
                if is_headless():
                    logger.warning(
                        "[Runtime] destructive tool call AUTO-DENIED "
                        "(non-interactive context): tool=%s — run interactively "
                        "or pre-approve to allow it", tool_name,
                    )
                    try:
                        log_event(
                            "WARNING", "tool",
                            f"Destructive call to '{tool_name}' auto-denied "
                            "(non-interactive run). Re-run interactively to allow it.",
                            context={"tool_name": tool_name},
                        )
                    except Exception:
                        logger.debug("[Runtime] log_event failed for auto-deny notice")
                # W12 (audit F6): a headless auto-deny must feed the SAME
                # failure-streak machinery as a failed call — a bare
                # `return None` left the governor blind and the model
                # retried the identical denied command to max-iterations
                # (30 → PARTIAL after ~90s in the A2 audit run). The
                # observation also tells the model HOW to adapt instead of
                # the misleading "User denied" (no user exists headless).
                from systemu.interface.notifications import is_headless as _ih
                if _ih():
                    deny_obs = {
                        "type": "safety_gate_denied",
                        "success": False,
                        "tool_name": tool_name,
                        "error": (
                            f"Safety gate: '{tool_name}' with these params is "
                            "classified destructive and auto-denied in "
                            "non-interactive runs. Do NOT retry the same call — "
                            "use a read-only alternative (e.g. a query-only "
                            "command or a file/read tool), or COMPLETE/FAIL "
                            "with what you have."),
                        "error_type": "destructive_auto_denied",
                    }
                    context.add_observation(deny_obs, current_ab)
                    return ToolResult(success=False, parsed=deny_obs,
                                      error=deny_obs["error"])
                context.add_observation(
                    {"type": "user_denied", "tool_name": tool_name,
                     "message": "User denied this destructive action."},
                    current_ab,
                )
                return None

        # ── v0.9.33 (A): v2 (code-registered) tool short-circuit ──────────
        # _build_llm_tool_catalog advertises v2 tools, but the v1 lookup
        # below only knows vault tools — without this, every advertised v2
        # tool returned "not found". Dispatch via ToolSandbox.execute (which
        # consults tool_registry_v2, runs entry.handler, injects _root, and
        # records the capability ledger by name). The dry-run short-circuit
        # and the destructive gate above already applied; shell tools are not
        # v2-registered, so the v0.9.32 command gate is unaffected.
        from systemu.runtime.tool_registry_v2 import registry as _v2_registry
        _v2_entry = _v2_registry.get(tool_name)
        if _v2_entry is not None and _v2_registry.available(tool_name, self.config):
            # ── v0.9.33 Bug 3: child-runtime recursion barrier (v2 path) ──────
            # spawn_subagent / delegate / mixture_of_agents are now dispatchable
            # (Section A) and form a SECOND delegation path whose handler uses
            # Config.from_env() — it ignores the recursion-disabled child config.
            # So a child (depth>=1) must be refused here, mirroring the native
            # REQUEST_HARNESS kind=subagent depth guard. No native fleet AND no
            # v2 fork → the cascade is closed on both paths. Parents (depth 0)
            # and all non-delegation v2 tools are unaffected.
            if (tool_name in _V2_DELEGATION_TOOL_NAMES
                    and int(getattr(self, "_subagent_depth", 0)) >= 1):
                context.add_tool_call(decision, current_ab)
                _refusal = ToolResult(
                    success=False,
                    parsed={"refused": True, "tool": tool_name,
                            "reason": "subagent_recursion_barrier"},
                    error=("Delegation is not available to a sub-agent "
                           f"(depth {int(getattr(self, '_subagent_depth', 0))}): "
                           "synthesize the work yourself and COMPLETE; do not "
                           "re-delegate or spawn further sub-agents."),
                )
                context.add_observation(_refusal.to_dict(), current_ab)
                logger.info(
                    "[Runtime] refused v2 delegation tool %s for child runtime "
                    "(depth=%d) — recursion barrier",
                    tool_name, int(getattr(self, "_subagent_depth", 0)),
                )
                return _refusal
            # Record the call exactly like the v1 path.
            context.add_tool_call(decision, current_ab)
            # DRY RUN — skip real execution (mirror the v1 dry-run path).
            if dry_run:
                fake_result = ToolResult(
                    success=True,
                    parsed={"dry_run": True, "tool": tool_name, "params": parameters},
                )
                context.add_observation(fake_result.to_dict(), current_ab)
                logger.debug("[Runtime] DRY RUN (v2): %s(%s)", tool_name, parameters)
                return fake_result
            # LIVE — dispatch through the v2 dispatcher (returns a dict).
            v2_dict = await self.sandbox.execute(tool_name, parameters)
            v2_success = bool(v2_dict.get("success", True)) if isinstance(v2_dict, dict) else True
            v2_result = ToolResult(
                success=v2_success,
                parsed=v2_dict if isinstance(v2_dict, dict) else {"value": v2_dict},
                error=(v2_dict.get("error") if isinstance(v2_dict, dict) and not v2_success else None),
            )
            context.add_observation(v2_result.to_dict(), current_ab)
            # Record verified artifacts on success (mirror the v1 path).
            if v2_result.success:
                try:
                    from systemu.runtime.artifacts import collect_artifact_paths
                    context.add_files(collect_artifact_paths(
                        tool_name, parameters, v2_result.parsed))
                except Exception:
                    logger.debug("[Runtime] v2 artifact collection skipped", exc_info=True)
            else:
                logger.warning("[Runtime] v2 tool %s failed: %s",
                               tool_name, v2_result.error)
            return v2_result

        # Find the Tool object
        tool_obj = next((t for t in tools if t.name == tool_name), None)
        if tool_obj is None:
            obs = {"error": f"Tool '{tool_name}' not found in available tools."}
            context.add_observation(obs, current_ab)
            return None

        # Record the call
        context.add_tool_call(decision, current_ab)

        # DRY RUN — skip actual execution
        if dry_run:
            fake_result = ToolResult(
                success=True,
                parsed={"dry_run": True, "tool": tool_name, "params": parameters},
            )
            context.add_observation(fake_result.to_dict(), current_ab)
            logger.debug("[Runtime] DRY RUN: %s(%s)", tool_name, parameters)
            return fake_result

        # LIVE — execute in sandbox
        if not tool_obj.implementation_path:
            obs = {"error": f"Tool '{tool_name}' has no implementation (status: {tool_obj.status})."}
            context.add_observation(obs, current_ab)
            return None

        # Suppress retries for tools that already failed with a dep error in
        # THIS run — but first re-check whether the blocking packages are now
        # approved (v0.3.6 no-restart fix).  When all are approved, drop the
        # suppression and let the actual call proceed; the registry's
        # self-heal path will install + retry the import.
        if tool_name in self._dep_failed_tools:
            blocking = self._dep_failed_tools.get(tool_name) or []
            cleared  = self._maybe_clear_dep_suppression(tool_name, blocking)
            if not cleared:
                obs = {
                    "success":    False,
                    "error":      (f"Tool '{tool_name}' is permanently unavailable this run: "
                                   f"a required Python package is missing. "
                                   f"Do not retry — check Notifications for install instructions."),
                    "error_type": "missing_dependency",
                }
                context.add_observation(obs, current_ab)
                return ToolResult(success=False, parsed=obs, error=obs["error"])

        # W2.2: forged-and-untrusted tools run OUT-OF-PROCESS (subprocess
        # backend) — the in-process fast path is reserved for built-ins and
        # operator-trusted tools.
        from systemu.runtime.tool_sandbox import requires_subprocess_isolation
        result = await self.sandbox.execute_tool(
            tool_obj.implementation_path,
            parameters,
            extra_packages=tool_obj.dependencies or [],
            tool_type=getattr(tool_obj.tool_type, "value", tool_obj.tool_type),
            force_subprocess=requires_subprocess_isolation(tool_obj),
        )

        # v0.9.1 (T8 must-wire): apply max_result_size_chars truncation.
        # truncate_result is a module-level function in tool_sandbox; it is a
        # no-op when tool_obj.max_result_size_chars is None.
        try:
            from systemu.runtime.tool_sandbox import truncate_result as _truncate_result
            result = _truncate_result(result, tool_obj)
        except Exception:
            logger.debug("[Runtime] truncate_result hook skipped", exc_info=True)

        # W8.4: record verified artifacts from this call (params + parsed,
        # exists-on-disk filtered) so build_result()["files_produced"] is real.
        if result.success:
            try:
                from systemu.runtime.artifacts import collect_artifact_paths
                context.add_files(collect_artifact_paths(
                    tool_name, parameters, result.parsed))
            except Exception:
                logger.debug("[Runtime] artifact collection skipped", exc_info=True)

        # Detect dependency-related result types and suppress retries.
        # Four error_types map to a single behaviour ("don't call this tool
        # again in this run") but trigger distinct operator-facing event-log
        # lines so the action to take is unambiguous.
        error_type = result.parsed.get("error_type") if result.parsed else None
        if error_type in (
            "missing_dependency",
            "dependency_install_blocked",
            "dependency_install_pending_approval",
            "dependency_install_failed",
        ):
            missing_list = _resolve_missing_packages(
                result.parsed.get("missing_packages"),
                list(getattr(tool_obj, "dependencies", []) or []),
            )
            hint = result.parsed.get("install_hint") or _install_hint(missing_list)
            # Remember the EXACT packages that blocked this tool so we can
            # clear the suppression precisely when they're approved.
            self._dep_failed_tools[tool_name] = list(missing_list)

            llm_msg, op_msg, op_level = _dep_failure_messages(
                tool_name=tool_name,
                error_type=error_type,
                missing_packages=missing_list,
                hint=hint,
                pip_tail=result.parsed.get("pip_stderr_tail"),
            )

            enriched_obs = dict(result.to_dict())
            enriched_obs["error"] = llm_msg
            context.add_observation(enriched_obs, current_ab)

            try:
                log_event(
                    op_level, "tool",
                    op_msg,
                    {
                        "tool_name":        tool_name,
                        "error_type":       error_type,
                        "missing_packages": missing_list,
                        "install_hint":     hint,
                        "origin":           getattr(self, "_origin", "manual"),
                    },
                )
            except Exception:
                pass
            logger.warning(
                "[Runtime] Tool '%s' dep issue (%s, pkgs=%s) — suppressing retries for this run",
                tool_name, error_type, missing_list,
            )
            return ToolResult(success=False, parsed=enriched_obs, error=enriched_obs["error"])

        context.add_observation(result.to_dict(), current_ab)

        if not result.success:
            logger.warning(
                "[Runtime] Tool %s failed: %s", tool_name, result.error or result.stderr[:500]
            )
            # v0.4.0-0: structured telemetry so we can build a real failure-mode
            # histogram before designing the supervisor.  Best-effort: telemetry
            # write failures are swallowed inside the module.
            try:
                from systemu.runtime.failure_telemetry import record_tool_failure
                error_type = None
                if result.parsed:
                    error_type = result.parsed.get("error_type")
                record_tool_failure(
                    shadow_id=(shadow.id if shadow is not None else None),
                    execution_id=execution_id,
                    tool_name=tool_name,
                    error_type=error_type,
                    error=result.error or (result.stderr[:500] if result.stderr else None),
                    extra={
                        "exit_code":      result.exit_code,
                        "timed_out":      result.timed_out,
                        "missing_packages": (result.parsed or {}).get("missing_packages"),
                    },
                )
            except Exception:
                logger.debug("[Runtime] telemetry write skipped", exc_info=True)

            # v0.4.4-a: tool-level metrics (per-tool lifetime success rate).
            # Used for operator visibility + Evolution proposals when tools
            # have chronically low success rates.  Keyed by tool_id so cross-
            # shadow signal accumulates.  Dependency-blocked failures are
            # tracked separately and excluded from the success-rate
            # denominator (those reflect the install env, not the tool).
            try:
                from systemu.runtime.tool_metrics import get_tool_metrics
                err_type_for_metrics = (result.parsed or {}).get("error_type") if result.parsed else None
                get_tool_metrics().record(
                    tool_id=getattr(tool_obj, "id", "") or tool_name,
                    success=False,
                    error_type=err_type_for_metrics,
                    timed_out=bool(result.timed_out),
                )
            except Exception:
                logger.debug("[Runtime] tool_metrics record skipped", exc_info=True)
            # v0.9.3: capability ledger — record failed invocation.
            try:
                self.sandbox._record_capability_outcome(
                    tool=tool_obj,
                    success=False,
                    error=str(result.error or result.stderr[:200] if result.stderr else result.error or ""),
                )
            except Exception:
                logger.debug("[Runtime] capability ledger (failure) skipped", exc_info=True)

            # v0.4.0-b: in-loop reflection.  Classify cheaply, count
            # consecutive failures for THIS tool, and queue a reflection
            # block for the next iteration.  After 3 consecutive failures,
            # the block explicitly forces a strategy choice via REFLECT.
            cls = None
            consec = 0
            try:
                from systemu.runtime.failure_classifier import (
                    classify_tool_result, reflection_strategies_for,
                )
                cls = classify_tool_result(result)
                self._consec_tool_fails[tool_name] = (
                    self._consec_tool_fails.get(tool_name, 0) + 1
                )
                consec = self._consec_tool_fails[tool_name]

                # v0.6.1-c: decay loaded-skill effectiveness on this failure.
                # Threshold-crossing queues RECALIBRATE_SKILL on pending_directives.
                try:
                    _maybe_decay_loaded_skills(
                        context, vault=self.vault, status="failure",
                    )
                except Exception:
                    logger.debug(
                        "[Runtime] skill decay hook crashed (per-iteration failure)",
                        exc_info=True,
                    )
                strategies = list(reflection_strategies_for(cls.category))
                force_reflect = consec >= 3
                block = _build_reflection_block(
                    tool_name=tool_name,
                    category=cls.category,
                    keyword=cls.keyword,
                    consec=consec,
                    strategies=strategies,
                    force_reflect=force_reflect,
                )
                context.queue_reflection_block(block)
            except Exception:
                logger.debug("[Runtime] reflection injection skipped", exc_info=True)

            # v0.4.0-d: notify Intelligent Supervisor of this failure so it
            # can decide whether to layer additional intervention on top of
            # the rule-based reflection block already queued above.
            mind = getattr(self, "_execution_mind", None)
            if mind is not None and mind.enabled:
                try:
                    mind.evaluate(
                        trigger="tool_failure",
                        recent_events=_build_history_slice(context, max_events=3),
                        classifier=(cls.category if cls else None),
                        consec_failures=consec,
                        iteration=0,  # exact iteration unknown at this seam; supervisor records the count of failures instead
                    )
                except Exception:
                    logger.debug("[Runtime] supervisor evaluate failed", exc_info=True)
        else:
            # Reset the per-tool failure counter on success.
            self._consec_tool_fails.pop(tool_name, None)
            # v0.9.1 (Layer 4): mark that fresh effectful work has landed so the
            # verifier per-turn cap clears for the next completion claim.
            self._fresh_work_since_last_verifier_call = True
            # v0.9.1 (final-review fix): invoke action-tool audit hook.
            # _after_successful_call was implemented in T8 (tool_sandbox) but
            # never called from production; without this wire, action-tool audit
            # is dead code and audit_log verifier hints always return verified=False.
            try:
                self.sandbox._after_successful_call(
                    tool=tool_obj,
                    params=parameters or {},
                    execution_id=execution_id,
                    objective_id=int(decision.get("completes_objective") or 0),
                    user_id=None,
                )
            except Exception:
                logger.debug("[Runtime] action-audit hook skipped", exc_info=True)
            # v0.9.3: capability ledger — record successful invocation.
            try:
                self.sandbox._record_capability_outcome(
                    tool=tool_obj, success=True, error=None,
                )
            except Exception:
                logger.debug("[Runtime] capability ledger (success) skipped", exc_info=True)
            # v0.4.4-a: record success in tool metrics.
            try:
                from systemu.runtime.tool_metrics import get_tool_metrics
                get_tool_metrics().record(
                    tool_id=getattr(tool_obj, "id", "") or tool_name,
                    success=True,
                )
            except Exception:
                logger.debug("[Runtime] tool_metrics record (success) skipped", exc_info=True)
            # v0.5.0-a: capture successful params for the backward-compat
            # replay used by RECALIBRATE_TOOL's bump-version path.  Rolling
            # buffer capped at 20 entries; secret-like keys redacted.
            try:
                from systemu.pipelines.tool_dry_run import record_successful_params
                record_successful_params(tool_obj, parameters or {}, self.vault)
            except Exception:
                logger.debug("[Runtime] last_successful_params capture skipped", exc_info=True)

        # v0.3.4: On a successful tool call, check whether this tool was
        # previously gated by a missing-dep failure (either earlier in
        # *this* run via ``_dep_failed_tools``, or in a prior run that
        # left a stale ``failure_patterns`` lesson in the shadow's buffer).
        # When so, append a contradicting memory entry so the consolidator
        # downweights the obsolete "switch formats" advice.
        if result.success and shadow is not None:
            previously_failed_in_run = tool_name in self._dep_failed_tools
            try:
                from systemu.runtime.memory_invalidator import maybe_invalidate_dep_lesson
                maybe_invalidate_dep_lesson(
                    self.vault, shadow, tool_name,
                    previously_failed=previously_failed_in_run,
                    execution_id=execution_id,
                )
            except Exception:
                # Never let memory bookkeeping crash an execution.
                logger.debug("[Runtime] memory invalidation hook errored", exc_info=True)

        return result

    def _maybe_clear_dep_suppression(self, tool_name: str, blocking: List[str]) -> bool:
        """Re-check whether every blocking package is now approved.

        When all are approved (operator clicked ✓ since this tool last
        failed), drop the suppression so the next call attempts the tool
        again.  Returns True when the suppression was cleared.

        Reads the approval store via the ToolSandbox's already-resolved
        ``_approvals`` reference so we re-use the same store the
        registry consults — and so the read picks up out-of-process
        mutations (v0.3.6 store re-reads on every check).
        """
        if not blocking:
            self._dep_failed_tools.pop(tool_name, None)
            return True
        approvals = getattr(self.sandbox, "_approvals", None)
        if approvals is None:
            return False
        try:
            all_approved = all(approvals.is_approved(p) for p in blocking)
        except Exception:
            logger.debug("[Runtime] approval re-check failed", exc_info=True)
            return False
        if all_approved:
            logger.info(
                "[Runtime] Dep suppression cleared for tool '%s' — all blocking "
                "packages now approved: %s", tool_name, blocking,
            )
            self._dep_failed_tools.pop(tool_name, None)
            return True
        return False

    def _load_skills(self, skill_ids: List[str]) -> List[Skill]:
        """Load all skills required by the activity."""
        skills = []
        for sid in skill_ids:
            try:
                skills.append(self.vault.get_skill(sid))
            except KeyError:
                logger.warning("[Runtime] Skill %s not found in vault", sid)
        return skills

    def _load_tools(self, tool_ids: List[str], *, dry_run: bool = False) -> List[Tool]:
        """Load tool objects that are ready for execution.

        Normal run : DEPLOYED, TESTED (dry-run passed), UPGRADED (evolved)
        Dry-run    : also includes FORGED (code exists, not yet enabled)
        """
        allowed_statuses = set(_RUNTIME_READY_STATUSES)
        if dry_run:
            allowed_statuses.add(ToolStatus.FORGED)

        tools = []
        for tid in tool_ids:
            try:
                t = self.vault.get_tool(tid)
                if t.status in allowed_statuses:
                    tools.append(t)
                else:
                    logger.warning(
                        "[Runtime] Tool %s (%s) is %s — skipping%s",
                        t.name, tid, t.status,
                        " (use --dry-run to include forged/tested tools)" if not dry_run else ""
                    )
            except KeyError:
                logger.warning("[Runtime] Tool %s not found in vault", tid)
        return tools

    def _append_to_shadow_log(
        self,
        shadow:        Shadow,
        execution_id:  str,
        status:        str,
        summary:       str,
        *,
        iteration_count:     int = 0,
        tool_calls_made:     int = 0,
        objectives_completed: int = 0,
        objectives_total:    int = 0,
        duration_seconds:    float = 0.0,
    ) -> None:
        """Append execution result to Shadow's log, persist, and record flywheel metrics."""
        from datetime import datetime
        timestamp = utcnow().isoformat()
        shadow.execution_log.append({
            "execution_id": execution_id,
            "status":       status,
            "summary":      summary[:500],
            "timestamp":    timestamp,
        })
        # Keep last 100 log entries
        shadow.execution_log = shadow.execution_log[-100:]
        try:
            self.vault.save_shadow(shadow)
            self.vault.prune_old_executions(
                max_keep=getattr(self.config, "execution_retention_count", 50)
            )
        except Exception as exc:
            logger.warning("[Runtime] Could not persist shadow log: %s", exc)

        # Record flywheel metrics
        try:
            from systemu.runtime.metrics_tracker import record_execution
            shadow_dir = (
                Path(self.config.vault_dir) / "shadow_army" / f"shadow_{shadow.id}"
            )
            record_execution(
                shadow_id=shadow.id,
                shadow_name=shadow.name,
                shadow_dir=shadow_dir,
                execution_id=execution_id,
                status=status,
                iteration_count=iteration_count,
                tool_calls_made=tool_calls_made,
                objectives_completed=objectives_completed,
                objectives_total=objectives_total,
                duration_seconds=duration_seconds,
                memory_md_path=shadow.memory_md_path,
            )
        except Exception as exc:
            logger.warning("[Runtime] Metrics recording failed (non-fatal): %s", exc)
