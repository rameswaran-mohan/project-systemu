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
# R-A10 (§5.2): module-level so tests can spy/patch `shadow_runtime.run_open_world_planner`.
from systemu.runtime.open_world_planner import run_open_world_planner
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


def _recredit_blocked_ids(objective_graph) -> set:
    """R-A10 B9 (Fix C): objective ids the durable-evidence recredit-on-resume hook
    must NOT re-credit — those gated on a still-MISSING runtime_error requirement.

    Derives the set from the PERSISTED objective graph (which carries the B9
    backchain mutation), NOT the static scroll tree the recredit loop iterates:

      missing     = ids of graph nodes with a runtime_error requirement in state
                    "missing" (an unsatisfied backchain credential/decision precede)
      blocked_ids = missing ∪ every node that (transitively) depends_on a missing one

    Transitive closure over ``depends_on`` (a chained precede → precede → target
    is all-or-nothing; the closure is cheap and closes that leak). Entries may be
    Objective instances (the read-path coerces to these) OR plain JSON dicts
    (defensive) — read via a dict/attr shim.

    DEFENSIVE: a ``[]``/None graph (legacy pre-G1 snapshot) → set() → the recredit
    loop is byte-unchanged. Never raises — any structural surprise degrades to the
    empty set (zero legacy behavior change), never a crash."""
    try:
        graph = list(objective_graph or [])
        if not graph:
            return set()

        def _field(node, name, default=None):
            if isinstance(node, dict):
                return node.get(name, default)
            return getattr(node, name, default)

        def _req_field(req, name, default=None):
            if isinstance(req, dict):
                return req.get(name, default)
            return getattr(req, name, default)

        missing: set = set()
        for node in graph:
            _id = _field(node, "id")
            if _id is None:
                continue
            for r in (_field(node, "requirements", None) or []):
                if (_req_field(r, "source") == "runtime_error"
                        and _req_field(r, "state") == "missing"):
                    missing.add(_id)
                    break

        if not missing:
            return set()

        # id -> its depends_on list, for the transitive closure.
        deps_by_id = {
            _field(n, "id"): list(_field(n, "depends_on", None) or [])
            for n in graph if _field(n, "id") is not None
        }
        blocked = set(missing)
        changed = True
        while changed:
            changed = False
            for _nid, _deps in deps_by_id.items():
                if _nid in blocked:
                    continue
                if any(d in blocked for d in _deps):
                    blocked.add(_nid)
                    changed = True
        return blocked
    except Exception:
        logger.debug("[Runtime] _recredit_blocked_ids failed — treating as none", exc_info=True)
        return set()


# ── S4 (fail-closed external-effect credit): external-evidence store helpers ──
def _read_external_ok(context, objective_id) -> bool:
    """S4 — the FAIL-CLOSED external-evidence gate read.

    Returns True ONLY when a persisted ExternalEvidence for ``objective_id`` has
    ``confirmed is True`` (a deterministic matcher, S3/R-A7, set it — NEVER an LLM,
    NEVER S4). Returns FALSE on an absent id, a non-dict store, a malformed entry,
    or ANY exception — it NEVER raises (mirrors ``_recredit_blocked_ids``'s
    defensive posture). No LLM path.

    Key normalisation: JSON round-trips dict keys to str, so the on-disk store is
    str-keyed; we look up both ``str(objective_id)`` and the raw id so an int
    objective_id still matches a str-keyed store. ``confirmed`` must be the bool
    ``True`` (an ``is True`` check) — a truthy non-bool (1, "yes") is a malformed
    entry the fail-closed read must NOT trust."""
    try:
        store = getattr(context, "_external_evidence", None)
        if not isinstance(store, dict):
            return False
        entry = store.get(str(objective_id))
        if entry is None:
            entry = store.get(objective_id)      # tolerate an int-keyed in-memory store
        if not isinstance(entry, dict):
            return False
        # R-A13b-1: a RECORD-ONLY shadow-meter entry (shadow=True) is NEVER a live
        # credit signal — it may carry confirmed=True purely as the would-credit
        # measurement. Refuse it so a shadow record can never credit an objective,
        # even across a SHADOW→ENFORCE resume mode-flip. A live entry never carries
        # ``shadow`` ⇒ OFF/ENFORCE are byte-identical.
        if entry.get("shadow") is True:
            return False
        return entry.get("confirmed") is True     # fail-closed: only a real bool True credits
    except Exception:
        logger.debug("[Runtime] _read_external_ok failed — fail-closed False", exc_info=True)
        return False


def _augment_summary_with_committed_effects(summary: str, context) -> str:
    """IMPL-7 / §5.6 — append the DETERMINISTIC committed-effects ledger to a
    HANDOFF / terminal-BLOCKED / stuck / partial ``final_summary``.

    Renders ONLY from persisted ``ExternalEvidence`` (via ``render_committed_effects``,
    which credits solely ``confirmed is True`` entries — set by the deterministic
    S3/R-A7 matcher, NEVER an LLM) — never from model prose. So a handoff that is
    precise about what it needs is ALSO honest about what it already did.

    A no-op (returns ``summary`` unchanged) when there are zero confirmed effects,
    when the context has no ``_external_evidence`` store, or on ANY error — it NEVER
    raises, so it can never break a terminal/handoff finalize. getattr-guarded so a
    context without the store is safe."""
    try:
        from systemu.runtime.committed_effects import render_committed_effects
        _ledger = render_committed_effects(
            getattr(context, "_external_evidence", {}) or {})
        if _ledger:
            return f"{summary}\n\n{_ledger}"
    except Exception:
        logger.debug("[Runtime IMPL-7] committed-effects augment failed (swallowed)",
                     exc_info=True)
    return summary


def _persist_external_evidence(context, evidence) -> None:
    """S4 — write ``evidence`` (an ExternalEvidence) into
    ``context._external_evidence[str(objective_id)]`` as a plain dict so it
    round-trips through the snapshot store. Fail-safe: creates the store if the
    context lacks one; swallows any bad input (None context / bad evidence) —
    never raises."""
    try:
        oid = getattr(evidence, "objective_id", None)
        if oid is None:
            return
        payload = (evidence.model_dump(mode="json")
                   if hasattr(evidence, "model_dump") else dict(evidence))
        store = getattr(context, "_external_evidence", None)
        if not isinstance(store, dict):
            store = {}
            context._external_evidence = store
        store[str(oid)] = payload
    except Exception:
        logger.debug("[Runtime] _persist_external_evidence failed (swallowed)", exc_info=True)
        return

    # fold-in #3: ALSO write a DURABLE, DISPLAY-ONLY copy of the receipt so the UI
    # can render a verified/claimed badge after the run completes (the live
    # context._external_evidence rides the ExecutionSnapshot, which is deleted on
    # completion). This is purely additive + best-effort: the credit gate reads the
    # live in-run evidence above (UNCHANGED), and NOTHING but the UI reads this
    # store, so a tampered receipts.json can never credit an effect.
    try:
        from systemu.runtime import receipts_store
        from systemu.runtime.chat_submission_ctx import current_execution_id
        eid = current_execution_id()
        if eid:
            receipts_store.write_receipt(eid, oid, {
                "objective_id": oid,
                "confirmed": payload.get("confirmed") is True,
                "method": payload.get("method"),
                "detail": payload.get("detail"),
                "stamped_at": payload.get("stamped_at"),
            })
    except Exception:
        logger.debug("[Runtime] durable receipt write skipped (swallowed)", exc_info=True)


# ── S3 / R-A7 wave-3a — wiring the ExternalVerifier at the credit seam ────────
#
# A DETERMINISTIC-ONLY hook: for a requires_external_verification objective, after
# a successful effectful TOOL_CALL it builds an evidence_input from the tool
# result + a pre-submit freshness snapshot, classifies the effect (money-move via
# money_move_net_applies), calls ExternalVerifier.verify(...), and PERSISTS the
# resulting ExternalEvidence into the store. It NEVER touches the S4 credit
# decision (only _persist_external_evidence) and NEVER calls an LLM. Guarded by
# the caller on `_needs_external` so a non-external objective is byte-identical.

def _external_from_result(result) -> "dict":
    """Pull the tool's self-declared external-verification envelope out of a
    ToolResult. A tool that participates in external verification exposes an
    ``external`` sub-dict on ``result.parsed`` carrying the strategy + the
    submission-unique token + (for the hardened api_readback path) the
    ``readback_url``/``submit_host``. Never raises; a tool that exposes nothing
    returns ``{}`` (⇒ the verifier fails closed).

    ── R-A13b-2i: the ``parsed['external']`` DIRECTIVE contract ──
    The envelope carries DIRECTIVES only — *where/how* to look, NEVER a confirmation:
        {
          "strategy":        "api_readback" | "email_confirm" | "web_assertion" | ...,
          "expected_tokens": [<submission-unique token(s) to match on read-back>],
          "readback_url":    "https://<submit_host>/…"  (hardened path; https + host-pin),
          "submit_host":     "<host the effect was submitted to>",
          "idempotency_key": "<client key>"             (IMPL-6 anti-double-submit),
        }
    The independent client fetches ``readback_url`` and the deterministic matcher runs
    RUNTIME-side, so a directive can never forge a confirmation. FRESHNESS is NOT a
    directive field for a money-move: it comes ONLY from the runtime's independent
    pre-submit probe (``_capture_presubmit_external_snapshot``) — a tool-carried
    ``pre_submit_absent``/``presubmit_tokens`` is trusted for NON-money effects only.
    The same directive is mirrored on the pre-submit call's ``parameters['external']``
    so the probe can read the create-once target BEFORE the effect issues."""
    try:
        parsed = getattr(result, "parsed", None)
        if not isinstance(parsed, dict):
            return {}
        ext = parsed.get("external")
        if isinstance(ext, dict):
            return dict(ext)
        return {}
    except Exception:
        return {}


def _presubmit_probe_directive(params) -> "Optional[dict]":
    """Extract the PRE-SUBMIT readback directive from a decision's parameters. A
    tool participating in external verification declares, on the call it is about to
    submit, a ``parameters['external']`` directive mirroring the result envelope
    (``readback_url`` + ``expected_tokens`` + ``submit_host``) so the runtime can do
    an INDEPENDENT create-once probe BEFORE the effect issues. Returns the directive
    (with a non-empty readback_url + expected_tokens) or None. Never raises."""
    try:
        if not isinstance(params, dict):
            return None
        directive = params.get("external")
        if not isinstance(directive, dict):
            return None
        if directive.get("readback_url") and directive.get("expected_tokens"):
            return directive
        return None
    except Exception:
        return None


def _probe_presubmit_absence(runtime, directive) -> "Optional[dict]":
    """Do an INDEPENDENT, host-pinned, https readback of the create-once target
    BEFORE submit via ``runtime._external_api_client`` and record whether the
    expected tokens are ABSENT pre-submit.

    Returns ``{presubmit_tokens, pre_submit_absent}`` (the authoritative, non-self-
    reported freshness snapshot) or None when no probe could run (no client / no
    admissible directive / any error) ⇒ freshness stays UNPROVABLE (fail-closed).
    Never raises. Mirrors the _api_readback host-pin + https gate so the probe reads
    exactly the target the post-submit verify will."""
    try:
        client = getattr(runtime, "_external_api_client", None)
        if client is None or not hasattr(client, "readback"):
            return None
        from systemu.runtime.external_verifier import (
            _as_token_list, _tokens_all_present, _url_host, _url_scheme,
            ExternalVerifier)
        url = directive.get("readback_url")
        expected = _as_token_list(directive.get("expected_tokens"))
        submit_host = str(directive.get("submit_host") or "").lower().strip()
        if not url or not expected:
            return None
        # host-pin + https (same admissibility the hardened readback enforces).
        if _url_scheme(url) != "https":
            return None
        if not submit_host or _url_host(url) != submit_host:
            return None
        envelope = client.readback(url)
        observed = ExternalVerifier._observed_from_envelope(envelope)
        # a create-once token ALREADY present pre-submit is STALE (replay) — record
        # it so the freshness gate refuses; otherwise the effect is absent pre-submit.
        present = [str(e) for e in expected if _tokens_all_present([e], observed)]
        # C3: record the EXACT (url, tokens) this probe read so the money-move branch
        # can bind the freshness proof to the resource verify actually credits — a
        # probe of URL_A must NOT vouch for a credit at a DIFFERENT URL_B.
        base = {"probed_url": str(url), "probed_tokens": list(expected)}
        if present:
            base.update({"presubmit_tokens": present, "pre_submit_absent": False})
        else:
            base.update({"presubmit_tokens": [], "pre_submit_absent": True})
        return base
    except Exception:
        logger.debug("[Runtime] presubmit probe failed — fail-closed (no snapshot)",
                     exc_info=True)
        return None


def _capture_presubmit_external_snapshot(decision, *, runtime=None) -> "dict":
    """Capture the PRE-SUBMIT freshness snapshot BEFORE an effectful call issues.

    The freshness proof (token-freshness / create-once) is what lets the hardened
    api_readback confirm THIS run produced the effect: a token already present
    pre-submit is stale. Two sources, in order of authority:

      1. a REAL independent pre-submit PROBE (R-A13b-2i): when the decision carries a
         ``parameters['external']`` readback directive and the runtime has an
         injected independent client, read the create-once target back and record
         token ABSENCE (``probe_ran=True`` — the ONLY freshness source a money-move
         will trust; a tool cannot self-attest it).
      2. a tool/decision self-report on ``parameters`` (``presubmit_tokens`` /
         ``pre_submit_absent``) — the already-wired NON-money fallback only.

    Fail-closed default: with neither a probe nor a self-report, return
    ``pre_submit_absent=False`` + EMPTY ``presubmit_tokens`` (+ ``probe_ran=False``)
    — freshness is then UNPROVABLE and the hardened _api_readback REFUSES. Never
    raises."""
    snap = {"presubmit_tokens": [], "pre_submit_absent": False, "probe_ran": False,
            "probed_url": "", "probed_tokens": []}
    try:
        params = decision.get("parameters") if isinstance(decision, dict) else None
        # (2) the decision/tool self-report (non-money fallback).
        if isinstance(params, dict):
            pt = params.get("presubmit_tokens")
            if isinstance(pt, (list, tuple, set)):
                snap["presubmit_tokens"] = [str(x) for x in pt]
            if params.get("pre_submit_absent") is True:
                snap["pre_submit_absent"] = True
        # (1) the REAL independent probe (authoritative — overwrites the self-report).
        directive = _presubmit_probe_directive(params)
        if directive is not None and runtime is not None:
            probe = _probe_presubmit_absence(runtime, directive)
            if probe is not None:
                snap["presubmit_tokens"] = probe["presubmit_tokens"]
                snap["pre_submit_absent"] = probe["pre_submit_absent"]
                snap["probe_ran"] = True
                # C3: carry the EXACT resource the probe read so the money-move
                # verify branch can require the credited envelope to match it.
                snap["probed_url"] = str(probe.get("probed_url") or "")
                snap["probed_tokens"] = list(probe.get("probed_tokens") or [])
    except Exception:
        logger.debug("[Runtime] presubmit snapshot capture failed — fail-closed",
                     exc_info=True)
    return snap


# R-A13b-2ii-a — the meter-bucket severity order. money_move dominates (via the
# money_move_net_applies short-circuit) but this also fixes the NON-money multi-tag
# bucket: a net_mutate+send_message tool must bucket under send_message, not the
# alphabetically-first net_mutate. Higher position = more severe.
_EFFECT_SEVERITY = (
    "money_move", "send_message", "oauth_call", "net_mutate",
    "local_delete", "shell_exec", "local_write", "net_read", "local_read",
)


def _most_severe_effect(effect_tags) -> "Optional[str]":
    """Pick the MOST-severe tag (by `_EFFECT_SEVERITY`) from a tool's effect_tags, so
    the meter buckets a multi-tag tool under its most-significant effect. An
    unlisted/exotic tag ranks least-severe but is still returned when it is the only
    candidate (preserves the pre-fix "return something" behaviour). Empty ⇒ None."""
    if not effect_tags:
        return None
    def _rank(t):
        v = str(getattr(t, "value", t)).strip().lower()
        try:
            return _EFFECT_SEVERITY.index(v)
        except ValueError:
            return len(_EFFECT_SEVERITY)
    chosen = min(effect_tags, key=_rank)   # min is stable → first-seen on a tie
    return str(getattr(chosen, "value", chosen)).strip().lower()


def _classify_external_effect(objective, decision, tool) -> "Optional[str]":
    """Deterministic effect classification for the verifier's money-move gate.

    Computes the money-move disjunction (BLOCKER-3) over the objective goal text,
    the call parameters, and the tool's effect tags. When the net applies, return
    the ``money_move`` effect class so ExternalVerifier.verify()'s internal
    money-move gate catches it (verify() ORs effect_class into the objective's tag
    set). Otherwise return the tool's declared effect class (or None). No LLM."""
    try:
        from systemu.runtime.financial_signal import money_move_net_applies
        from systemu.runtime.effect_tags import EffectTag
        goal = ""
        for attr in ("goal", "text", "success_criteria"):
            v = getattr(objective, attr, None)
            if isinstance(v, str) and v:
                goal += " " + v
        params = decision.get("parameters") if isinstance(decision, dict) else None
        effect_tags = list(getattr(tool, "effect_tags", None) or [])
        requires_external = bool(
            getattr(objective, "requires_external_verification", False))
        if money_move_net_applies(effect_tags, goal, params, requires_external):
            return EffectTag.MONEY_MOVE.value
        # otherwise pass through the tool's MOST-SEVERE declared effect tag (advisory)
        # — not the alphabetically-first tag, which mis-buckets multi-tag tools.
        return _most_severe_effect(effect_tags)
    except Exception:
        # a classification failure must NOT open the money-move gate — but the
        # verify() money-move gate ALSO recomputes _is_money_move over the
        # objective, so returning None here never weakens the gate.
        logger.debug("[Runtime] external effect classification failed", exc_info=True)
        return None


def _is_money_move_seam(objective, decision, tool) -> bool:
    """SAFETY (R-A13b-2i) — a FAIL-CLOSED money-move test used to (a) FORBID the
    branch-2 self-reported readback and (b) FORBID tool-self-carried freshness. Any
    error ⇒ True (treat as money-move ⇒ trust ONLY the injected independent client +
    the runtime probe). Mirrors ``_classify_external_effect``'s money-move disjunct
    (the ExternalVerifier's own money-move gate recomputes this over the objective —
    keeping the two consistent)."""
    try:
        from systemu.runtime.financial_signal import money_move_net_applies
        goal = ""
        for attr in ("goal", "text", "success_criteria"):
            v = getattr(objective, attr, None)
            if isinstance(v, str) and v:
                goal += " " + v
        params = decision.get("parameters") if isinstance(decision, dict) else None
        effect_tags = list(getattr(tool, "effect_tags", None) or [])
        requires_external = bool(
            getattr(objective, "requires_external_verification", False))
        return bool(money_move_net_applies(effect_tags, goal, params, requires_external))
    except Exception:
        logger.debug("[Runtime] money-move seam classification failed — fail-closed "
                     "(treating as money-move)", exc_info=True)
        return True


def _build_external_api_client(runtime, ev_in, *, is_money_move=False):
    """Resolve the api_readback transport for the hardened path.

    Precedence:
      1. an injected client on the runtime (``runtime._external_api_client``) —
         production wires a real independent-https reader here (ProdReadbackClient);
         tests inject a mock.
      2. (NON-money ONLY) an in-memory adapter over a ``readback_envelope`` the tool
         already captured from its OWN post-submit readback (deterministic — no live
         I/O in the hook, no LLM).
      3. None — the hardened path then reports "no api_client" and fails closed.

    R-A13b-2i (self-attestation hole): a MONEY-MOVE may NEVER resolve to branch-2 —
    a tool echoing its own ``readback_envelope`` would self-confirm a money-move. So
    for ``is_money_move`` we SKIP branch-2 entirely: with no injected independent
    client there is NO admissible transport ⇒ None ⇒ fail closed (would-PARK).
    """
    injected = getattr(runtime, "_external_api_client", None)
    if injected is not None:
        return injected
    if is_money_move:
        # no independent client + money-move ⇒ NO self-reported fallback is admissible.
        return None
    envelope = ev_in.get("readback_envelope") if isinstance(ev_in, dict) else None
    if envelope is not None:
        class _EnvelopeClient:
            def __init__(self, env):
                self._env = env
            def readback(self, url):  # noqa: D401 — matches the verifier contract
                return self._env
        return _EnvelopeClient(envelope)
    return None


def _run_external_verification(runtime, *, objective, decision, tool, result,
                               presubmit):
    """Build the evidence_input, classify the effect, run ExternalVerifier.verify
    over the HARDENED api_readback path (host-pin + https + token-freshness), and
    return the ExternalEvidence. DETERMINISTIC-ONLY (no LLM). Never raises — any
    failure yields an unconfirmed evidence (fail-closed)."""
    from systemu.runtime.external_verifier import ExternalVerifier
    from systemu.core.models import ExternalEvidence
    try:
        ev_in = dict(_external_from_result(result))
        is_money = _is_money_move_seam(objective, decision, tool)
        # ── PRE-SUBMIT freshness threading (R-A13b-2i anti-replay) ──
        if is_money:
            # SECURITY: a money-move's freshness proof may come ONLY from the
            # runtime's INDEPENDENT pre-submit probe — NEVER the tool's own envelope
            # NOR a decision-param self-report (both are forgeable ⇒ replay). Trust
            # the snapshot ONLY when a real probe ran; else zero it (⇒ freshness
            # unprovable ⇒ the hardened readback refuses). This OVERWRITES any
            # tool-carried pre_submit_absent/presubmit_tokens on ev_in.
            #
            # C3: the proof must ALSO be BOUND to the exact resource verify credits.
            # The probe read ``probed_url``/``probed_tokens`` (pre-submit); verify
            # credits off the RESULT envelope's readback_url/expected_tokens. A probe
            # of a benign-absent URL_A cannot vouch for a credit at a DIFFERENT,
            # pre-existing URL_B (a stale receipt). So trust probe_ran ONLY when the
            # envelope's url == the probed url AND EVERY credited token was among the
            # tokens the probe proved absent (env_tokens ⊆ probed_tokens — the
            # security-sound direction: never bless an un-probed token). Any mismatch
            # ⇒ zero the freshness ⇒ the hardened readback refuses ⇒ would-PARK.
            probe_binds = False
            if presubmit.get("probe_ran"):
                env_url = str(ev_in.get("readback_url") or "").strip()
                probed_url = str(presubmit.get("probed_url") or "").strip()
                from systemu.runtime.external_verifier import _as_token_list as _atl
                env_tokens = set(_atl(ev_in.get("expected_tokens")))
                probed_tokens = set(_atl(presubmit.get("probed_tokens")))
                probe_binds = bool(
                    env_url and probed_url and env_url == probed_url
                    and env_tokens and env_tokens <= probed_tokens)
            if probe_binds:
                ev_in["pre_submit_absent"] = presubmit.get("pre_submit_absent") is True
                ev_in["presubmit_tokens"] = list(presubmit.get("presubmit_tokens") or [])
            else:
                ev_in["pre_submit_absent"] = False
                ev_in["presubmit_tokens"] = []
        else:
            # NON-money (byte-identical to pre-2i): a tool-provided proof on the
            # result envelope wins; otherwise the runtime snapshot fills in. The
            # snapshot NEVER upgrades a tool that already proved absence.
            if not ev_in.get("pre_submit_absent"):
                if presubmit.get("pre_submit_absent") is True:
                    ev_in["pre_submit_absent"] = True
            if not ev_in.get("presubmit_tokens"):
                ev_in["presubmit_tokens"] = list(presubmit.get("presubmit_tokens") or [])
        effect_class = _classify_external_effect(objective, decision, tool)
        api_client = _build_external_api_client(
            runtime, ev_in, is_money_move=is_money)
        verifier = ExternalVerifier(api_client=api_client)
        return verifier.verify(objective, effect_class, ev_in)
    except Exception:
        logger.debug("[Runtime] external verification hook failed — fail-closed",
                     exc_info=True)
        oid = getattr(objective, "id", 0) or 0
        return ExternalEvidence(objective_id=oid, confirmed=False)


# ── R-A14a — the MCP → S3/S4 verification LINKAGE (decoupled from S4_STAMP) ────
#
# An MCP mutation is INVISIBLE to the S3/S4 net today: MCP tools carry no effect_tags
# and are NOT bound as capabilities, so the requirement binder never stamps
# ``requires_external_verification`` → the hardened api_readback never runs → no
# receipt. R-A14a fixes this WITHOUT the flag-gated binder stamp: the ``mcp``
# ActuationModality carries its OWN per-actuation obligation. For a KNOWN-mutation MCP
# call this hook drives the modality's ``capture_evidence`` (→ the EXISTING
# money-move-safe ``_run_external_verification`` → verify() + hardened api_readback),
# persists the ExternalEvidence, and signals that the credit must be GATED on the
# confirmed bit — DECOUPLED from SYSTEMU_S4_STAMP (works OFF/SHADOW). The money-move
# fail-closed invariant is PRESERVED BY REUSE (verify() demotes a money-move unless a
# hardened independent api_readback confirms). Fully guarded — never raises.

def _known_mutation_mcp_entry(decision):
    """Resolve the v2 ToolEntry for a namespaced MCP mutation call, else None.

    A namespaced MCP tool is registered as ``mcp__<slug>__<tool>`` with
    ``is_action_tool = not readOnlyHint`` (registry_bridge). A read-only tool
    (``is_action_tool`` False) is NOT a mutation ⇒ None (no obligation ⇒ today's
    behavior). An MCP call dispatches via the v2 registry (not the ``tools`` list), so
    the entry is resolved from the registry, not the run's tool list. Never raises."""
    try:
        name = decision.get("tool_name") if isinstance(decision, dict) else None
        if not isinstance(name, str) or not name.startswith("mcp__"):
            return None
        from systemu.runtime.tool_registry_v2 import registry
        entry = registry.get(name)
        if entry is None or not bool(getattr(entry, "is_action_tool", False)):
            return None
        return entry
    except Exception:
        return None


def _mcp_actuation_link(runtime, context, *, objective, decision, result) -> bool:
    """Drive the ``mcp`` modality's per-actuation verification obligation at the credit
    seam, PERSIST the resulting ExternalEvidence receipt (always, best-effort), and
    return whether the credit must be GATED on it. Never raises.

    Gating SEMANTICS (R-A14a regression fix):
      * MONEY-MOVE MCP mutation → returns True (FAIL-CLOSED gate). The obligation gates
        the credit: the reused ``verify()`` credits a money-move ONLY via a hardened
        independent host-pinned+fresh api_readback (attestation / self-report / advisory /
        inline tokens can NEVER credit one). A money-move ALWAYS gates — even if capture
        produced no evidence, or errored. THE INVARIANT — never weakened here.
      * NON-money MCP mutation → returns False (NON-GATING). The receipt is still
        PERSISTED (confirmed=True ⇒ a "verified" provenance receipt, confirmed=False ⇒
        "claimed") but it does NOT block the credit: the credit proceeds via the NORMAL
        path (the local verifier verdict / result.success), exactly as before R-A14a. A
        non-money MCP mutation therefore credits whether or not verification confirms —
        no over-gating, no stall, no regression. The receipt is best-effort DEC-13
        provenance only.

    DECOUPLED from SYSTEMU_S4_STAMP (the obligation is per-actuation, declared by the
    modality for a declared MCP mutation — NOT the flag-gated binder stamp, which is
    never touched here)."""
    entry = _known_mutation_mcp_entry(decision)
    if entry is None:
        return False
    # money-move classification FIRST so a capture error still gates a money-move.
    is_money = _is_money_move_seam(objective, decision, entry)
    try:
        from systemu.runtime.actuation.mcp_modality import McpActuationModality
        from systemu.runtime.actuation.modality import Action, ActionResult
        parsed = getattr(result, "parsed", None)
        response = parsed.get("response") if isinstance(parsed, dict) else None
        action = Action(
            modality="mcp",
            name=(decision.get("tool_name") if isinstance(decision, dict) else "") or "",
            params=(decision.get("parameters") if isinstance(decision, dict) else {}) or {},
            is_mutation=True, objective=objective, tool=entry)
        aresult = ActionResult(
            success=bool(getattr(result, "success", False)),
            response=response, raw=parsed)
        # thread the PRE-SUBMIT freshness snapshot (captured before the mutation, one-
        # shot) so a money-move MCP effect with a curated readback template can be
        # credited on a provably-fresh readback; consume it. None ⇒ the modality's
        # default (probe_ran=False) ⇒ money-move stays fail-closed.
        _mcp_presub = getattr(runtime, "_mcp_presubmit_snapshot", None)
        try:
            runtime._mcp_presubmit_snapshot = None
        except Exception:
            pass
        ev = McpActuationModality(runtime).capture_evidence(
            action, aresult, presubmit=_mcp_presub)
        # PERSIST the receipt regardless of gating (best-effort provenance for DEC-13).
        if ev is not None:
            _persist_external_evidence(context, ev)
        # GATE only for a money-move (fail-closed). Non-money is a non-gating receipt.
        return bool(is_money)
    except Exception:
        logger.debug("[Runtime R-A14a] mcp actuation link errored — money-move stays "
                     "gated (fail-closed)", exc_info=True)
        return bool(is_money)


# ── R-A13b-1 — the SHADOW park-surface METER (record-only) ────────────────────
#
# Stage 2 of the 3-stage external-verification activation: a RECORD-ONLY branch at
# the S4 credit seam. When an objective WOULD-stamp under the current S4 stamp mode
# but the LIVE gate field was NOT written (SHADOW — the binder set _s4_stamp_shadow
# only), run S3 evidence production ANYWAY, compute would-credit/would-park, and
# RECORD it to the sink (context._external_evidence + the metrics_store bucket) — but
# do NOT credit, enqueue a card, or suspend. This measures the REAL park-surface the
# net would present, without arming it. Fully fail-safe: any error is a no-op. OFF and
# ENFORCE never reach this (the caller guards on shadow-mode + _s4_stamp_shadow), so
# they are byte-identical.

def _armed_meter_objective(objective):
    """Return a NON-MUTATING shallow copy of ``objective`` with the SHADOW
    would-stamp state reflected onto the LIVE ``requires_external_verification``
    field, so the meter measures the SAME armed net ENFORCE would run (invariant
    I5 — faithful park-surface).

    Why this matters: the external-verifier's money-move demotion is conditioned on
    ``requires_external_verification`` (ExternalVerifier._is_money_move →
    money_move_net_applies' fail-closed disjunct for an UNKNOWN effect). SHADOW
    NEVER writes that live field (the binder sets only ``_s4_stamp_shadow``), so an
    un-armed meter would run a WEAKER net and record a spurious would-CREDIT where
    ENFORCE would-PARK — UNDER-counting the very park surface the Stage-3 arm gate
    reads. Reflecting the would-stamp onto a copy closes that gap.

    Record-only + fail-safe: the real objective is NEVER mutated (a shallow copy
    isolates the write to the copy's ``__dict__``); on any copy error we return the
    original objective (the meter degrades to the weaker net rather than crash)."""
    try:
        would_stamp = bool(getattr(objective, "_s4_stamp_shadow", False))
        if not would_stamp:
            return objective
        import copy as _copy
        armed = _copy.copy(objective)          # shallow: a NEW __dict__, no mutation of the original
        try:
            armed.requires_external_verification = True
        except Exception:
            armed.__dict__["requires_external_verification"] = True
        return armed
    except Exception:
        logger.debug("[Runtime S4-METER] armed-objective copy failed — measuring un-armed",
                     exc_info=True)
        return objective


def _record_s4_shadow_meter(runtime, context, *, objective, decision, tool, result,
                            presubmit) -> None:
    """Run S3 evidence production for a would-stamp SHADOW objective and RECORD the
    would-credit/would-park outcome. RECORD-ONLY — never touches the credit decision.
    Never raises."""
    try:
        # I5 (faithful park-surface): measure the ARMED net. The real objective is
        # never mutated (record-only) — the copy reflects the would-stamp onto the
        # live requires_external_verification field the money-move demotion reads.
        _armed = _armed_meter_objective(objective)
        ev = _run_external_verification(runtime, objective=_armed, decision=decision,
                                        tool=tool, result=result, presubmit=presubmit)
        would_credit = bool(getattr(ev, "confirmed", False))
        effect_class = _classify_external_effect(_armed, decision, tool) or "unknown"
        # run-local sink: reuse the single-writer persist, then AUGMENT the stored
        # entry with the shadow-meter tags (shadow=True keeps _read_external_ok from
        # ever mistaking it for a live credit).
        try:
            _persist_external_evidence(context, ev)
            oid = getattr(objective, "id", None)
            store = getattr(context, "_external_evidence", None)
            entry = store.get(str(oid)) if (isinstance(store, dict) and oid is not None) else None
            if isinstance(entry, dict):
                entry["shadow"] = True
                entry["would_credit"] = would_credit
                entry["would_park"] = not would_credit
                entry["effect_class"] = effect_class
        except Exception:
            logger.debug("[Runtime S4-METER] shadow evidence persist failed (swallowed)",
                         exc_info=True)
        # cross-run aggregation: the SINGLE pinnable metrics writer-site (CONC-MAP).
        try:
            from systemu.runtime.metrics_store import MetricsStore
            MetricsStore(Path(runtime.vault.root) / "metrics").incr_s4_shadow_meter(
                effect_class, would_credit=would_credit)
        except Exception:
            logger.debug("[Runtime S4-METER] metrics incr failed (swallowed)", exc_info=True)
        logger.debug("[Runtime S4-METER] obj=%s effect=%s → %s (record-only)",
                     getattr(objective, "id", None), effect_class,
                     "would_credit" if would_credit else "would_park")
    except Exception:
        logger.debug("[Runtime S4-METER] shadow meter errored — no-op", exc_info=True)


# ── S3 / R-A7 wave-3b — IMPL-6 mid-run ambiguous-outcome detector ─────────────
#
# A transport-ambiguous failure of an EFFECTFUL call is one where the effect MIGHT
# have landed even though the client saw a failure — a timeout AFTER send, a
# connection reset, or a 5xx-after-send. Those are the ONLY failures IMPL-6
# intercepts (before any retry), and ONLY for a requires_external_verification
# objective. A CLEAN, unambiguous failure (a 4xx the server rejected before any
# effect, a validation error) provably never landed, so today's retry behavior is
# safe and IMPL-6 stays out of it → the call is byte-identical to today.

# error substrings that mark a transport-ambiguous failure (effect may have
# landed after send). Timeouts are handled via result.timed_out separately.
_IMPL6_AMBIGUOUS_HINTS = (
    "timed out", "timeout",
    "connection reset", "reset by peer", "econnreset",
    "connection aborted", "broken pipe",
    "500", "502", "503", "504",
    "bad gateway", "gateway timeout", "service unavailable",
    "internal server error",
)

# substrings that mark a CLEAN, unambiguous failure — the effect provably never
# landed (server rejected the request before any state change). NOT an IMPL-6 case.
_IMPL6_CLEAN_HINTS = (
    "400", "401", "403", "404", "409", "422",
    "bad request", "unauthorized", "forbidden", "not found",
    "validation", "invalid",
)


def _is_ambiguous_effectful_failure(*, objective, result_dict) -> bool:
    """True IFF this is a transport-ambiguous failure of an EFFECTFUL call that
    IMPL-6 must intercept before any retry: the objective is
    requires_external_verification AND the failure is genuinely ambiguous (the
    effect MIGHT have landed after send). Non-external or clean-failure ⇒ False
    (today's retry behavior is unchanged). Never raises."""
    try:
        if not bool(getattr(objective, "requires_external_verification", False)):
            return False
        if not isinstance(result_dict, dict):
            return False
        if result_dict.get("success") is True:
            return False
        # a timeout AFTER send is the canonical ambiguous case.
        if result_dict.get("timed_out") is True:
            return True
        blob = " ".join(
            str(result_dict.get(k) or "")
            for k in ("error", "error_type", "classified_reason", "stderr")
        ).lower()
        # a CLEAN client-side rejection (4xx / validation) is NOT ambiguous — the
        # effect never landed, so today's retry is safe. Check this FIRST so a
        # "400 invalid" never trips an ambiguous hint.
        if any(h in blob for h in _IMPL6_CLEAN_HINTS):
            return False
        return any(h in blob for h in _IMPL6_AMBIGUOUS_HINTS)
    except Exception:
        logger.debug("[Runtime IMPL-6] ambiguous-failure detect errored — treating "
                     "as not-ambiguous (today's behavior)", exc_info=True)
        return False


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


def _apply_nested_answers(target: dict, answers: dict) -> None:
    """R-A13a — merge operator answers into ``target`` (a tool's pending parameters) at
    each answer key's schema_path position. A key WITHOUT '/' sets a top-level param —
    IDENTICAL to the pre-R-A13a flat merge (the v0.9.35 missing_required rail keys by
    top-level name, never '/'). A key WITH '/' (the bundled scope card keys by full
    schema_path, e.g. 'message/subject') sets the nested position, creating intermediate
    dicts. A '[]' segment (an array element) is not resolvable to a single position → the
    key is set flat (Stage-1 limitation; array-element bind-back is deferred) — never
    dropped."""
    for k, v in (answers or {}).items():
        segs = str(k).split("/")
        if len(segs) == 1 or any(s == "[]" for s in segs):
            target[k] = v
            continue
        cur = target
        ok = True
        for seg in segs[:-1]:
            nxt = cur.get(seg) if isinstance(cur, dict) else None
            if isinstance(nxt, dict):
                cur = nxt
            elif nxt is None:
                nxt = {}
                cur[seg] = nxt
                cur = nxt
            else:                                # a scalar occupies the slot → don't clobber
                ok = False
                break
        if ok:
            cur[segs[-1]] = v
        else:
            target[k] = v                        # fail-safe: never drop the answer


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
#  R-A10 B10 — RequirementReport producer + ask_bundle → elicitation rail
# ─────────────────────────────────────────────────────────────────────────


def _populate_requirement_report(context, *, objectives, capability, situation,
                                 vault=None, config=None) -> None:
    """B10 producer wiring: invoke ``build_requirement_report`` and stash the
    result on ``context._requirement_report`` (B6 captures + resume-restores +
    persists it), then — if the report carries a non-empty ``ask_bundle`` —
    surface the FIRST requirement through the elicitation rail (single-card).

    FAIL-SAFE (like R-A9's survey stage): ANY binder/render error is logged and
    swallowed — the run proceeds EXACTLY as today (no report, no crash, no hang).

    AC6-SAFE: a report with an EMPTY ask_bundle (no missing requirements) surfaces
    NO elicitation AND is stashed-NEUTRAL — ``context._requirement_report`` is left
    UNSET so ``capture_from_context`` persists ``None`` (the value a run without a
    producer already had). Only a report carrying REAL gaps (a non-empty ask_bundle,
    worth restoring on resume) is stashed. This mirrors the ``_objective_graph``
    conditional: a no-gap run is byte-identical to today's snapshot (no perturbation).

    Single-card SCOPE: only the FIRST ask_bundle requirement is surfaced. The
    batched multi-requirement scope card (one card, N requirements) + re-plan-on-
    resume is deferred to **R-A12**; B10 gives the binder a live consumer. The
    accepted value is NOT bound back into the objective/schema here — that
    bind-back (and the resume-driven re-plan) is R-A12 scope too; B10 is
    surfaced-only so the operator gets the card and the run suspends via the rail.

    ``PendingChoiceRequest`` (raised while awaiting the operator) is allowed to
    PROPAGATE — the suspend IS the rail and the caller sits in the resume-aware
    spine. It is NOT swallowed by the fail-safe guard below.
    """
    from systemu.approval.exceptions import PendingChoiceRequest

    # Build the report (fail-safe). build_requirement_report never raises by
    # contract, but we guard anyway so a producer-side surprise can't crash the run.
    try:
        from systemu.runtime.requirement_binder import build_requirement_report
        report = build_requirement_report(objectives, capability, situation, context)
        report_dict = report.model_dump()
    except PendingChoiceRequest:
        raise                                    # never here, but keep the rail honest
    except Exception:
        logger.debug(
            "[Runtime] requirement-report producer skipped (non-fatal)",
            exc_info=True,
        )
        return

    # AC6 no-op: no missing requirements ⇒ leave the snapshot BYTE-IDENTICAL. We do
    # NOT stash an empty-ask report (that would flip the persisted requirement_report
    # from None → {} on every run); a resume needs nothing restored when there's no
    # gap. Only a report with REAL gaps is stashed + surfaced.
    ask = (report_dict or {}).get("ask_bundle") or []
    if not ask:
        return

    # A report with real gaps: stash it (B6 persists + resume-restores) and surface.
    context._requirement_report = report_dict

    # Single-card: surface the FIRST requirement (batched card = R-A12). Let a
    # PendingChoiceRequest PROPAGATE (the suspend is the rail); swallow only
    # NON-suspend errors so a render glitch can't crash the run.
    try:
        from systemu.runtime.elicitation import surface_ask_bundle_requirement
        surface_ask_bundle_requirement(ask[0], vault=vault, config=config)
    except PendingChoiceRequest:
        raise
    except Exception:
        logger.debug(
            "[Runtime] ask_bundle surface skipped (non-fatal)",
            exc_info=True,
        )


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


def _resolve_objectives_for_run(
    *,
    use_objectives: bool,
    objectives: list,
    scroll_json: list,
    context,
    resume_objective_graph,
):
    """Decide the authoritative objective list for this run, folding the rebuild
    sources into one place with explicit precedence (G1 / R-A2 / R-A10 B12):

      0. BOTH a persisted, non-empty ``objective_graph`` AND a param-substitution
         grant present on the same (doubly-mutated) resume → MERGE by re-applying the
         operator's ACTUAL string-substitution to the graph nodes (RISK-3): the graph
         is authoritative for STRUCTURE (which objectives + which ``requirements``
         exist, ``depends_on`` incl. inserted precede ids, ``origin``,
         ``requires_external_verification``), and EVERY string leaf of each graph node
         is re-substituted with the operator's ``(old → new)`` pairs — so a value
         substituted inside ``requirements`` (or any other leaf) is NO LONGER silently
         dropped. Graph nodes with no param-sub counterpart (inserted precedes) are
         substituted too (harmless — their strings don't contain the old value). This
         prevents branch 1 from silently DROPPING the operator's substituted values on
         a resume that also inserted a precede (planner/backchain).
      1. A persisted, non-empty ``objective_graph`` (no param-sub) — the durable
         mutated graph. Rebuild from it (it IS the resumed state).
      2. A param-substitution grant that replaced ``context.scroll_json`` (no graph)
         (v0.9.35 seam-fix; identity check). Rebuild from ``context.scroll_json``.
      3. Neither — return ``objectives`` / ``scroll_json`` UNCHANGED, by identity.
         This is the AC6 byte-identical path: ``pending_objs`` derives purely from
         ``objectives``, so identity-preservation ⇒ byte-identical schedule.

    Precedence order: merge(graph, param-sub) > graph-only > param-sub-only >
    static identity.

    Returns ``(objectives, scroll_json)``.
    """
    if not use_objectives:
        return objectives, scroll_json
    from systemu.core.models import Objective as _Objective

    ctx_scroll_json = getattr(context, "scroll_json", None)
    _has_paramsub = ctx_scroll_json is not None and ctx_scroll_json is not scroll_json

    # 0. Doubly-mutated resume — MERGE the param-sub onto the persisted graph (B12 /
    #    RISK-3). Only fires when BOTH sources are present, so branches 2/3 (esp. the
    #    AC6 identity floor) are byte-unchanged: a no-replanning run never reaches here.
    if resume_objective_graph and _has_paramsub:
        return _merge_paramsub_onto_graph(
            resume_objective_graph, ctx_scroll_json, _Objective,
            paramsub_pairs=getattr(context, "_paramsub_pairs", None),
            pre_sub_scroll_json=scroll_json,
        )

    # 1. Persisted mutated graph wins (no param-sub to fold in).
    if resume_objective_graph:
        rebuilt = [
            o if isinstance(o, _Objective) else _Objective.model_validate(o)
            for o in resume_objective_graph
        ]
        return rebuilt, [o.model_dump(mode="json") for o in rebuilt]

    # 2. Param-substitution seam-fix (unchanged behavior, identity-guarded).
    if _has_paramsub:
        rebuilt = [_Objective.model_validate(o) for o in ctx_scroll_json]
        return rebuilt, ctx_scroll_json

    # 3. Static scroll tree — untouched, byte-identical (AC6).
    return objectives, scroll_json


def _derive_paramsub_pairs_by_diff(pre_sub_scroll_json, ctx_scroll_json):
    """FALLBACK: reconstruct ``(old_s, new_s)`` substitution pairs by diffing
    corresponding STRING leaves of the pre-sub scroll against the post-sub
    ``ctx_scroll_json`` for matched objective ids.

    Used only when the grant-apply site could NOT thread the exact pairs
    (``context._paramsub_pairs`` absent). This is coarser than the real pairs
    (it emits whole-leaf ``old → new`` pairs, so a sub-token replacement inside a
    DIFFERENT field may not be captured), but it re-substitutes the leaves that
    actually changed and never fabricates a pair. The threaded-pairs path is
    strongly preferred and is what the runtime uses.
    """
    pre_by_id: Dict[Any, Dict[str, Any]] = {}
    for raw in (pre_sub_scroll_json or []):
        d = raw if isinstance(raw, dict) else raw.model_dump(mode="json")
        oid = d.get("id")
        if oid is not None:
            pre_by_id[oid] = d

    pairs: list = []
    seen: set = set()

    def _walk(pre: Any, post: Any) -> None:
        if isinstance(pre, str) and isinstance(post, str):
            if pre and pre != post and (pre, post) not in seen:
                seen.add((pre, post))
                pairs.append((pre, post))
            return
        if isinstance(pre, dict) and isinstance(post, dict):
            for k in pre.keys() & post.keys():
                _walk(pre[k], post[k])
            return
        if isinstance(pre, list) and isinstance(post, list):
            for a, b in zip(pre, post):
                _walk(a, b)
            return

    for raw in (ctx_scroll_json or []):
        d = raw if isinstance(raw, dict) else raw.model_dump(mode="json")
        oid = d.get("id")
        pre = pre_by_id.get(oid)
        if pre is not None:
            _walk(pre, d)
    return pairs


def _merge_paramsub_onto_graph(
    resume_objective_graph,
    ctx_scroll_json,
    _Objective,
    *,
    paramsub_pairs=None,
    pre_sub_scroll_json=None,
):
    """B12 (RISK-3): merge a param-sub grant onto a persisted objective graph by
    RE-APPLYING the operator's actual string-substitution to the graph nodes.

    The graph is the STRUCTURAL base (inserted precedes + ``depends_on`` wiring +
    backchain-added ``requirements`` + ``origin`` + ``requires_external_verification``).
    Rather than overlaying a hand-listed set of value fields from the param-subbed
    objects (which silently DROPPED substituted values living in fields NOT on the
    list — e.g. inside ``requirements``'s ``schema_path`` / ``rationale`` /
    ``bound_value_ref``), we replay the SAME ``_replace_in_obj(node, old, new)`` that
    ``substitute_parameters`` applied, for each ``(old, new)`` pair. This makes each
    merged node = graph STRUCTURE (id, ``depends_on``, inserts, which ``requirements``
    exist) + EVERY string leaf substituted (goal, success_criteria, hints values,
    verifier, AND each requirement's string leaves) — complete by construction.

    The pairs come THREADED from the grant-apply site (``context._paramsub_pairs`` —
    exactly what ``substitute_parameters`` computed). When they're unavailable we fall
    back to deriving them by diffing ``pre_sub_scroll_json`` against
    ``ctx_scroll_json`` for matched ids (see ``_derive_paramsub_pairs_by_diff``).

    Inserted precedes (graph-only nodes) are substituted too — harmless, their strings
    just don't contain the old value. Same ``id`` preserved throughout.

    Returns ``(objectives, scroll_json_dump)``.
    """
    from systemu.runtime.param_resolution import _replace_in_obj

    graph_objs = [
        o if isinstance(o, _Objective) else _Objective.model_validate(o)
        for o in resume_objective_graph
    ]

    pairs = list(paramsub_pairs or [])
    if not pairs:
        # No threaded pairs — reconstruct them from the pre/post scroll diff.
        pairs = _derive_paramsub_pairs_by_diff(pre_sub_scroll_json, ctx_scroll_json)

    merged: list = []
    for g in graph_objs:
        orig = g.model_dump(mode="json")           # the pre-substitution graph node
        node = orig
        for old_s, new_s in pairs:
            node = _replace_in_obj(node, old_s, new_s)

        # RESTORE the graph-authoritative STRUCTURAL fields: the graph is authoritative
        # for STRUCTURE (incl. the Literal/enum-typed fields), param-sub only rewrites
        # VALUE leaves. Without this, a substitution whose OLD value IS/contains a literal
        # token (a capture equal to "operator" / "missing" / "input" / "planner") rewrites
        # the enum → _Objective.model_validate raises → the resume CRASHES (HIGH). We keep
        # the substituted VALUE leaves (goal, success_criteria, hints values, requirements'
        # schema_path/rationale/bound_value_ref) and pin the enums back to the original.
        if isinstance(node, dict) and isinstance(orig, dict):
            node = dict(node)
            # Objective.origin — Literal["planner","discovery","retry","backchain"].
            if "origin" in orig:
                node["origin"] = orig["origin"]
            # int/list structural fields _replace_in_obj can't touch (int/list scalars),
            # but restore defensively in case a stringified variant ever appears.
            if "id" in orig:
                node["id"] = orig["id"]
            if "depends_on" in orig:
                node["depends_on"] = orig["depends_on"]
            # Each Requirement's Literal fields (state/kind/source/value_origin) are
            # graph-authoritative; requirements are same-order (graph-authoritative), so
            # restore by index. VALUE leaves inside each requirement stay substituted.
            orig_reqs = orig.get("requirements")
            node_reqs = node.get("requirements")
            if isinstance(orig_reqs, list) and isinstance(node_reqs, list):
                restored_reqs = []
                for i, nr in enumerate(node_reqs):
                    if isinstance(nr, dict) and i < len(orig_reqs) and isinstance(orig_reqs[i], dict):
                        nr = dict(nr)
                        for _lit in ("state", "kind", "source", "value_origin"):
                            if _lit in orig_reqs[i]:
                                nr[_lit] = orig_reqs[i][_lit]
                    restored_reqs.append(nr)
                node["requirements"] = restored_reqs

        # BELT-AND-SUSPENDERS: the resume must NEVER crash on a param-sub. If the
        # substituted+restored node still fails validation (a value leaf the structural
        # restore can't cover), fall back to the ORIGINAL (un-substituted) graph node.
        try:
            merged.append(_Objective.model_validate(node))
        except Exception:
            logger.debug("[B12] param-sub node failed validation; falling back to the "
                         "original graph node (no resume crash)", exc_info=True)
            merged.append(g if isinstance(g, _Objective) else _Objective.model_validate(orig))

    return merged, [o.model_dump(mode="json") for o in merged]


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
        # R-A13b-2i / R-A14a: inject the PRODUCTION independent-https readback client so
        # the hardened api_readback path (and the SHADOW park-surface meter) can perform a
        # real, INDEPENDENT read-back — resolving _build_external_api_client's branch-1 in
        # a live run. Tests may override runtime._external_api_client with a mock.
        #
        # R-A14a: injected UNCONDITIONALLY — including OFF. R-A14a decoupled the MCP
        # verification OBLIGATION from SYSTEMU_S4_STAMP (_mcp_actuation_link runs at the
        # credit seam regardless of the stamp mode), so the readback TRANSPORT must be
        # available net-OFF too, else a decoupled MCP obligation is UN-SATISFIABLE (a
        # non-money MCP mutation could never verify → the OFF regression this closes).
        #
        # DORMANCY (why injecting in OFF stays byte-identical for non-MCP): the client is
        # ONLY read by _build_external_api_client, which is ONLY reached from
        # _run_external_verification. For a NON-MCP effect that hook runs ONLY when the
        # binder stamped requires_external_verification (or the SHADOW meter fires) — which
        # OFF never does — so the injected client is NEVER read for a non-MCP OFF effect
        # (no outbound GET, byte-identical credit). It fires ONLY on an MCP obligation
        # (via _mcp_actuation_link) — exactly the path R-A14a needs net-OFF. The client is
        # ALSO a credential-less INDEPENDENT reader (no money-transport a non-MCP effect
        # would use to self-confirm): the money-move fail-closed gate is unchanged.
        try:
            from systemu.runtime.readback_client import ProdReadbackClient
            self._external_api_client = ProdReadbackClient()
        except Exception:
            logger.debug("[Runtime] prod readback client injection skipped", exc_info=True)
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
        # IMPL-7 / §5.6: a stuck/partial/cancelled terminal is a HANDOFF — be honest
        # about the external effects already committed this run (deterministic).
        _summary = _augment_summary_with_committed_effects(_summary, context)
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
                _tid = mat.get("tool_id")
                _nt = None
                if mat.get("reused"):
                    # ── REUSE: resolve STRICTLY by the re-verified id ──
                    # Seam B re-verified THIS specific id (DEPLOYED+enabled+not-
                    # rejected). Do NOT fall back to status-blind name resolution: if
                    # the id vanished cross-process in the sub-ms window between Seam B
                    # and here, find_tool_by_name(name) could pick a same-named PROPOSED
                    # twin and deploy_forged_tool it → UNREVIEWED code deployed under the
                    # LOW reuse grant (a forge-review bypass — the same class Seam B
                    # guards). A 404 here = genuinely stale ⇒ _nt stays None (the
                    # harness_granted_pending branch handles it; NO twin deploy).
                    try:
                        _nt = self.vault.get_tool(_tid) if _tid else None
                    except Exception:
                        _nt = None
                else:
                    for _resolve in (
                        # Forge: prefer the exact id (the freshly-forged tool), then
                        # name fallbacks for legacy/robustness.
                        lambda: self.vault.get_tool(_tid) if _tid else None,
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

    def _apply_fold_depth_exemption(self, *, tool_name, loop_guard):
        """R-A10 B9 (AC4 / Fix 2): neutralize the stuck bounds for a folded runtime
        error — a DISCOVERED REQUIREMENT is not lack of progress. Resets the
        no-progress counter, drops this tool's same-tool-fail / consec-fail streaks,
        and clears the loop-guard streaks so the fold's retry isn't counted against a
        stall verdict (LoopGuard has no reset(); clear inline). Shared by the
        fresh-fold path AND the idempotent-pending no-op so a repeated 401 on a
        still-missing credential NEVER counts toward the stuck bound. Never raises."""
        self._iters_since_obj_credit = 0
        self._same_tool_fail_streak.pop(tool_name, None)
        self._consec_tool_fails.pop(tool_name, None)
        try:
            if getattr(loop_guard, "_fail_tool", None) == tool_name:
                loop_guard._fail_tool = None
                loop_guard._fail_streak = 0
            loop_guard._leader = None
            loop_guard._streak = 0
            loop_guard._pingpong_streak = 0
        except Exception:
            logger.debug("[Runtime B9] loop_guard clear skipped", exc_info=True)

    # ── S3 / R-A7 wave-3b — IMPL-6 mid-run ambiguous-outcome handler ───────────
    def _impl6_handle_ambiguous(self, *, objective, decision, result, context,
                                tools=None):
        """Run the IMPL-6 read-back-before-retry for a transport-ambiguous failure
        of an effectful call, keyed to the CLIENT idempotency key that was injected
        BEFORE send. Returns an ``Impl6Outcome`` (or None on error — the caller
        then falls through to today's retry behavior).

        The idempotency key + readback URL come from the tool's self-declared
        ``external`` envelope on ``result.parsed`` (the same envelope
        _external_from_result reads at the credit seam) — a tool participating in
        external verification exposes:
          * ``idempotency_key`` — the CLIENT key it stamped the submit with, OR
          * the runtime-minted key stashed on ``self._impl6_client_key`` when the
            runtime injected it at the MCP transport.
        Absent a client key or a keyable read-back, handle_ambiguous_effect returns
        an INDETERMINATE outcome (operator card), NEVER confirmed-absent."""
        from systemu.runtime.external_verifier import ExternalVerifier
        try:
            ext = _external_from_result(result)
            # the CLIENT key: the tool's declared key wins; else the runtime-minted
            # key stashed when it injected the header at the transport.
            key = (str(ext.get("idempotency_key") or "").strip()
                   or str(getattr(self, "_impl6_client_key", "") or "").strip())
            readback_url = ext.get("readback_url")
            submit_host = ext.get("submit_host")
            _impl6_tool = next(
                (t for t in (tools or [])
                 if getattr(t, "name", None) == decision.get("tool_name")), None)
            effect_class = _classify_external_effect(objective, decision, _impl6_tool)
            api_client = _build_external_api_client(
                self, ext,
                is_money_move=_is_money_move_seam(objective, decision, _impl6_tool))
            verifier = ExternalVerifier(api_client=api_client)
            return verifier.handle_ambiguous_effect(
                objective=objective, effect_class=effect_class,
                idempotency_key=key, readback_url=readback_url,
                submit_host=submit_host)
        except Exception:
            logger.debug("[Runtime IMPL-6] _impl6_handle_ambiguous errored", exc_info=True)
            return None

    def _impl6_enqueue_operator_card(self, *, objective, execution_id, detail=""):
        """Enqueue the IMPL-6 mid-run operator card (reuses the wave-3a InboxQueue
        operator rail) when the read-back is indeterminate. Never raises; returns
        the card id or None."""
        try:
            from systemu.interface.command.gate import GateDescriptor
            oid = getattr(objective, "id", 0)
            descriptor = GateDescriptor(
                title=("Operator: an effectful external call failed AMBIGUOUSLY — "
                       "confirm the outcome before any retry"),
                risk="high",
                inspect=(
                    f"Objective {oid}: an effectful external submit failed in a "
                    "TRANSPORT-AMBIGUOUS way (a timeout after send / connection reset "
                    "/ 5xx-after-send). The effect MAY OR MAY NOT have landed, and the "
                    "outcome could NOT be read back deterministically "
                    f"({detail or 'no idempotency primitive to key the read-back'}). "
                    "Re-submitting could DOUBLE-SUBMIT (e.g. a duplicate payment). "
                    "Confirm the prior submit's outcome before authorising a retry."),
                options=["Do not retry (park)",
                         "Retry — I confirm the effect did NOT go through"],
                safe_default="Do not retry (park)",
                what_approve_does=(
                    "Authorises the shadow to RE-EXECUTE the external submit. Only "
                    "choose this if you have verified the effect did not already occur."),
                dedup=f"external_ambiguous:{execution_id}:{oid}",
            )
            return InboxQueue(self.vault).enqueue(
                descriptor, gate_type="operator", body="",
                context_extras={
                    "kind": "external_ambiguous_guard",
                    "objective_id": oid,
                    "execution_id": execution_id,
                })
        except Exception:
            logger.debug("[Runtime IMPL-6] operator-card enqueue failed", exc_info=True)
            return None

    def _enqueue_operator_attest_and_suspend(
        self, *, objective, execution_id, context, result, decision, tool,
        activity, shadow):
        """R-A13 Stage-3a — enqueue the operator_attest ENFORCE-fallback card + SUSPEND.

        Called at the not-credited external branch (ENFORCE only) when an external
        effect could not be independently confirmed AND no independent readback
        channel was even available. Builds the RENDER-ONLY attest card over the RAW
        (redacted) evidence envelope (never the agent's prose), enqueues it on the
        InboxQueue operator rail carrying the marker + the enqueue-time effect
        classification the resume applier needs, and returns the suspend result dict.
        The credit itself is NEVER granted here — that is the resume-time
        verify(operator_attest) path. Fully guarded — returns None on ANY error so the
        caller falls back to the existing silent not-credit behavior. Money-move is
        excluded by the CALLER (this method assumes a non-money effect)."""
        try:
            from systemu.runtime.external_verifier import ExternalVerifier
            oid = getattr(objective, "id", 0)
            # the enqueue-time effect classification: the effectful tool is resolvable
            # HERE (decision.tool_name) but NOT at resume, and a requires_external
            # objective with no KNOWN effect tag is money-move via the fail-closed
            # fallback — so the resume applier must be handed a known non-money
            # effect_class to let verify() credit. Carried in the card context.
            effect_class = _classify_external_effect(objective, decision, tool)
            # render-only card over the RAW, REDACTED evidence envelope; the builder
            # sets dedup = operator_attest:{oid} and options ["Dismiss","Attest occurred"].
            descriptor = ExternalVerifier().build_operator_attest_artifact(
                objective,
                evidence_input={"raw_evidence": _external_from_result(result)})
            card_id = InboxQueue(self.vault).enqueue(
                descriptor, gate_type="operator", body="",
                context_extras={
                    "kind_marker": "operator_attest",
                    "objective_id": oid,
                    "execution_id": execution_id,
                    "is_money_move": False,
                    "effect_class": effect_class,
                    # carried so _dispatch_resume / the reconciler re-dispatch this
                    # operator gate (both require a chat_submission_id in context).
                    "chat_submission_id": getattr(self, "_chat_submission_id", None),
                })
            _susp = context.build_result(
                status="suspended_operator_attest",
                final_summary=_augment_summary_with_committed_effects(
                    "Parked awaiting operator attestation: an external effect could "
                    "not be independently confirmed and no independent readback "
                    "channel was available. Only the operator may vouch it occurred "
                    "(a money-move can never be credited this way).", context))
            _susp["activity_id"] = getattr(activity, "id", "")
            _susp["shadow_id"] = getattr(shadow, "id", "")
            if card_id is not None:
                _susp["operator_card_id"] = card_id
            logger.warning(
                "[Runtime S4-ATTEST] PARKED obj=%d — unconfirmed external effect, no "
                "independent channel; operator-attest card %s (ENFORCE fallback).",
                oid, card_id)
            return _susp
        except Exception:
            logger.debug("[Runtime S4-ATTEST] attest enqueue/suspend failed — "
                         "falling back to silent not-credit", exc_info=True)
            return None

    def _apply_operator_attest_sticky(self, snap, objectives, context) -> None:
        """R-A13 Stage-3a — the resume-start APPLIER for a resolved operator-attest.

        Peels the ``__OPERATOR_ATTEST__::obj_<id>::<json>`` sticky (mirrors the
        ``__STUCK_ANSWER__`` peel) and, for choice == "Attest occurred" ONLY (Dismiss
        / anything else ⇒ NO credit, the safe default), and ONLY when the objective is
        NOT a money-move (fail-closed skip — attestation can NEVER credit a money-move),
        runs ``ExternalVerifier.verify(strategy="operator_attest", attested=True)`` with
        the enqueue-time ``effect_class`` and PERSISTS the resulting evidence. The
        credit then lands via the S4 resume short-circuit reading the confirmed bit —
        the effectful tool is NEVER re-invoked, and the credit routes through the verify
        STRATEGY (money-move-gated), never ``build_operator_attest_artifact``.

        Ordered BEFORE the recredit loop / resubmit guard so the short-circuit credits
        and the guard does not re-park. Gated on ENFORCE (behind the flag). Never
        raises — any error yields no credit (fail-closed)."""
        try:
            from systemu.runtime.requirement_binder import _s4_stamp_mode as _s4_mode_b
            if _s4_mode_b() != "enforce":
                return
            notes = list(getattr(snap, "sticky_notes", None) or [])
            note = next((n for n in notes
                         if isinstance(n, str)
                         and n.startswith("__OPERATOR_ATTEST__::")), None)
            if not note:
                return
            import json as _json
            _parts = note.split("::", 2)  # __OPERATOR_ATTEST__::obj_<id>::<json>
            oid = int(_parts[1].replace("obj_", ""))
            try:
                payload = _json.loads(_parts[2])
            except Exception:
                payload = {"choice": _parts[2]}
            choice = str(payload.get("choice") or "").strip().lower()
            if choice != "attest occurred":
                # Dismiss / any non-affirmative choice ⇒ NO credit (the safe default).
                logger.info(
                    "[Runtime S4-ATTEST] resume: operator did NOT attest obj=%d "
                    "(choice=%r) — not credited.", oid, payload.get("choice"))
                return
            effect_class = payload.get("effect_class")
            objective = next(
                (o for o in objectives if getattr(o, "id", None) == oid), None)
            if objective is None:
                return
            # gate (b): FAIL-CLOSED money-move skip at CREDIT time, INDEPENDENT of the
            # enqueue gate. The effectful tool is not linked to the objective at resume,
            # so re-run the money-move net with a surrogate carrying the enqueue-time
            # effect_class (a money-move goal still trips is_financial_signal alone).
            import types as _types
            _surrogate = _types.SimpleNamespace(
                effect_tags=[effect_class] if effect_class else [])
            if _is_money_move_seam(objective, None, _surrogate):
                logger.warning(
                    "[Runtime S4-ATTEST] resume: obj=%d re-classified money-move at "
                    "credit time — attestation REFUSED (a money-move can never credit "
                    "via attest).", oid)
                return
            # the credit ENGINE: verify(operator_attest) confirms a NON-money effect;
            # its own money-move gate (gate c) demotes a money-move to confirmed=False.
            from systemu.runtime.external_verifier import ExternalVerifier
            ev = ExternalVerifier().verify(
                objective, effect_class,
                {"strategy": "operator_attest", "attested": True})
            _persist_external_evidence(context, ev)
            logger.warning(
                "[Runtime S4-ATTEST] resume: operator attested obj=%d — persisted %s "
                "evidence (confirmed=%s) for the S4 credit.", oid,
                getattr(ev, "method", "?"), getattr(ev, "confirmed", None))
        except Exception:
            logger.debug("[Runtime S4-ATTEST] resume applier errored — no credit "
                         "(fail-closed)", exc_info=True)

    def _fold_runtime_error_and_suspend(
        self, *, objectives, completed_objectives, decision, sub, tool_name,
        context, scroll, shadow, activity, execution_id, root_eid, iteration,
        current_ab, harness_requests_this_run, loop_guard, revoke_harness_leases,
    ):
        """R-A10 B9 (AC4): fold an auth/semantic http_error into a Requirement +
        backchain precede-objective, EXEMPT the stuck counters, and SUSPEND via the
        INPUT rail so the operator supplies the credential/decision.

        Returns ``{"objectives": <new tree>, "result": <suspend dict>}`` on a
        successful fold, ``{"objectives": <tree>, "already_pending": True}`` on an
        idempotent-pending no-op (exemption applied, seam must skip the stuck path),
        or ``None`` when the fold degrades (unresolvable current objective /
        genuinely unfoldable) so the caller falls through to the normal
        reflection + stuck path. NEVER raises — any failure returns ``None``."""
        try:
            from systemu.runtime.runtime_fold import fold_runtime_error

            # Resolve the CURRENT objective — the one whose tool call is in flight.
            # Prefer the decision's explicit claim; else the audit helper (first
            # not-yet-completed objective whose deps are satisfied).
            _cur = decision.get("completes_objective")
            if not isinstance(_cur, int):
                _cur = _current_objective_id_for_audit(objectives, completed_objectives)
            # A derived id of 0 means "no current objective" → can't fold safely.
            if not _cur:
                return None

            # Derive a service hint for the credential/decision ask. Prefer the
            # tool's declared service/host; fall back to the tool name.
            _service_hint = None
            try:
                _service_hint = (decision.get("service")
                                 or (decision.get("parameters") or {}).get("service"))
            except Exception:
                _service_hint = None

            _next_id = int(getattr(context, "_next_objective_id", 0)
                           or (max((getattr(o, "id", 0) for o in objectives), default=0) + 1))
            _fold = fold_runtime_error(
                objectives=objectives,
                current_obj_id=_cur,
                sub=sub,
                tool_name=tool_name,
                service_hint=_service_hint,
                next_id=_next_id,
            )
            if _fold is None:
                return None

            # ── Fix 2: IDEMPOTENT-PENDING — a precede for this service is already
            # inserted and still missing (a repeated 401 across the loop / a resume).
            # STILL apply the depth-exemption so this iteration NEVER counts toward
            # the stuck bound, then signal the seam to skip _update_stuck_counters /
            # loop_guard.record / _stuck_trigger for this iteration (the run stays
            # parked on the still-pending precede — it is not lack of progress). We do
            # NOT re-suspend/re-surface a second operator card (the first is still
            # pending); we just neutralize the counters and continue.
            if getattr(_fold, "already_pending", False):
                self._apply_fold_depth_exemption(tool_name=tool_name, loop_guard=loop_guard)
                logger.info(
                    "[Runtime B9] idempotent-pending %s error on %s — precede already "
                    "pending; stuck counters exempt, iteration skipped", sub, tool_name,
                )
                return {"objectives": objectives, "already_pending": True}

            new_objectives = _fold.objectives

            # ── Fix A (HIGH, safety): UN-CREDIT the precede on a wrong-credential
            # RE-ASK. The reuse path (fold_runtime_error) flips a satisfied precede's
            # requirement state="have"→"missing" and re-suspends, but that precede was
            # CREDITED into completed_objectives on the prior resume. If we leave the
            # credit, the ORIGINAL objective's depends_on gate stays OPEN with the
            # credential now missing — the LLM could advance/COMPLETE it via any other
            # succeeding action and finish the run UNAUTHENTICATED. Discard the re-ask
            # id from EVERY set that tracks precede completion (completed_objectives +
            # the resume precede-credit set) BEFORE the re-suspend snapshot is written,
            # so the gate RE-CLOSES and the re-suspend snapshot rehydrates honestly.
            _reask_pid = getattr(_fold, "reask_precede_id", None)
            if isinstance(_reask_pid, int):
                try:
                    completed_objectives.discard(_reask_pid)
                except Exception:
                    logger.debug("[Runtime B9] re-ask un-credit: completed_objectives.discard failed",
                                 exc_info=True)
                try:
                    self._resume_completed_precedes.discard(_reask_pid)
                except Exception:
                    pass
                logger.info(
                    "[Runtime B9] wrong-credential re-ask: precede %d UN-CREDITED "
                    "(gate re-closed; original objective waits for the new credential)",
                    _reask_pid,
                )

            # Persist the authoritative post-fold graph (B5) so a resume rehydrates
            # the inserted precede, and bump the id-allocator floor.
            try:
                context._objective_graph = [
                    o.model_dump(mode="json") for o in new_objectives
                ]
            except Exception:
                logger.debug("[Runtime B9] objective_graph persist skipped", exc_info=True)
            context._next_objective_id = _fold.next_id

            # ── DEPTH-EXEMPTION (AC4) ─────────────────────────────────────────
            # This failure is a DISCOVERED REQUIREMENT, not lack of progress —
            # neutralize the stuck bounds like the coach-steer reset so it can never
            # push the run toward the no-progress / same-tool-fail terminal.
            self._apply_fold_depth_exemption(tool_name=tool_name, loop_guard=loop_guard)

            # ── Build the INPUT request for the missing credential/decision ──
            # Fix 1: carry the FAILED tool's original params + the inserted precede id
            # so the resume rail re-dispatches the tool and the resume site can
            # satisfy + credit the precede.
            try:
                _failed_params = dict(decision.get("parameters") or {})
            except Exception:
                _failed_params = {}
            _req = self._build_runtime_fold_input_request(
                sub=sub, tool_name=tool_name, requirement=_fold.requirement,
                pending_params=_failed_params, precede_id=_fold.precede_id,
            )

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
                logger.debug("[Runtime B9] fold suspend snapshot failed", exc_info=True)

            try:
                from systemu.interface.harness_review import surface_harness_request
                from systemu.core.models import HarnessVerdict, HarnessDecision
                _verdict = HarnessVerdict(
                    request_id=_req.request_id,
                    decision=HarnessDecision.ESCALATE,
                    rationale=("Runtime error folded into an operator requirement "
                               f"({_fold.requirement.kind})."),
                )
                _did = surface_harness_request(
                    _req, _verdict, execution_id=execution_id,
                    activity_id=getattr(activity, "id", ""),
                    shadow_id=getattr(shadow, "id", ""),
                    vault=self.vault,
                )
                logger.info(
                    "[Runtime B9] %s http_error folded into %s requirement → parked "
                    "(operator card %s)", sub, _fold.requirement.kind, _did,
                )
            except Exception:
                logger.debug("[Runtime B9] surface_harness_request failed", exc_info=True)

            revoke_harness_leases(record_run=False, reconcile=False)
            _susp = context.build_result(
                status="suspended_harness_escalation",
                final_summary=_augment_summary_with_committed_effects(
                    f"Parked awaiting operator input: {tool_name} failed with a "
                    f"{'credential' if sub == 'auth' else 'bad-request'} error; a "
                    f"{_fold.requirement.kind} requirement was folded into the plan.",
                    context),
            )
            _susp["activity_id"] = getattr(activity, "id", "")
            _susp["shadow_id"]   = getattr(shadow, "id", "")
            return {"objectives": new_objectives, "result": _susp}
        except Exception:
            logger.debug("[Runtime B9] fold-and-suspend degraded to None", exc_info=True)
            return None

    def _build_runtime_fold_input_request(
        self, *, sub, tool_name, requirement,
        pending_params=None, precede_id=None,
    ):
        """Build the ``kind=INPUT`` HarnessRequest for a B9 fold — a credential
        (auth) or decision (semantic) the operator must supply before the failed
        objective retries. Mirrors the missing-param INPUT rail shape.

        Fix 1: carries ``pending_tool`` (the failed tool + its ORIGINAL params) so
        the PROVEN resume re-dispatch rail (``_apply_harness_grant_async``'s
        ``pending_tool`` + ``param_answers`` branch → merge + RE-DISPATCH) satisfies
        the credential/decision and re-runs the call — instead of only injecting an
        advisory observation (which never delivered the credential to the tool). For
        an auth fold the credential field is a SECRET (URL-mode: stored out-of-band
        into the credential store/env, never typed, never in the form or logs). The
        ``runtime_fold`` markers (kind / schema_path / precede_id) let the resume
        site satisfy the backchain precede + credit it into ``completed_objectives``
        so the original objective's ``depends_on`` gate opens and it retries."""
        from systemu.core.models import HarnessRequest, HarnessKind
        if sub == "auth":
            _q = (f"'{tool_name}' failed to authenticate (401/403). Provide the "
                  f"credential for {requirement.schema_path} so it can retry.")
            _schema = {
                "type": "object",
                "properties": {
                    "credential": {
                        "type": "string",
                        # 'password' format ⇒ is_secret_field() ⇒ URL-mode.
                        "format": "password",
                        "description": (f"Credential / token for "
                                        f"{requirement.schema_path}."),
                    },
                },
                "required": ["credential"],
            }
            _secret = ["credential"]
        else:  # semantic
            _q = (f"'{tool_name}' failed with a bad-request error (422/404). "
                  f"How should the request be corrected?")
            _schema = {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "description": ("How to correct the request "
                                        "(the missing decision)."),
                    },
                },
                "required": ["decision"],
            }
            _secret = []
        _spec = {
            "question": _q,
            "requested_schema": _schema,
            "secret_fields": _secret,
            # Fix 1: carry the failed tool + its ORIGINAL params so the resume rail
            # re-dispatches (auth: URL-mode secret is in the store/env by then;
            # semantic: the operator's decision merges into the params). Same shape
            # the missing-required INPUT rail uses (harness_review rides pending_tool
            # through to the reconciler → grant_payload → _apply_harness_grant_async).
            "pending_tool": {
                "tool_name": tool_name,
                "parameters": dict(pending_params or {}),
            },
            # Fold markers — the resume site reads these to satisfy + credit the precede.
            "runtime_fold": True,
            "requirement_kind": requirement.kind,
            "requirement_schema_path": requirement.schema_path,
        }
        if precede_id is not None:
            _spec["precede_id"] = int(precede_id)
        return HarnessRequest(
            kind=HarnessKind.INPUT,
            spec=_spec,
            rationale=requirement.rationale or (
                f"Runtime {sub} error on {tool_name} folded into a "
                f"{requirement.kind} requirement."),
            fallback="",
            blocking=True,
        )

    def _build_bundled_scope_card(self, tool_name, ask_bundle, parameters, reasoning=""):
        """R-A13a §5.6 — one INPUT HarnessRequest for a cross-objective ask_bundle, each
        requirement mapped to a form slot keyed by its FULL schema_path (NOT the leaf —
        the leaf would collide same-named nested paths and lose nesting). Mirrors the
        missing_required INPUT shape (:7381-7439): carries requested_schema + pending_tool
        so the SAME resume rail re-dispatches with the operator's answers nested back in.
        Returns None when the bundle yields no form slot."""
        from systemu.core.models import HarnessRequest, HarnessKind
        from systemu.runtime.elicitation import (
            elicitation_schema_from_fields, split_secret_fields,
        )
        fields = []
        for r in (ask_bundle or []):
            sp = getattr(r, "schema_path", None)
            if not sp:
                continue
            # R-A13a Stage 1: an ARRAY-element gap (schema_path with a '[]' segment)
            # cannot round-trip yet — _apply_nested_answers flat-stores a '[]'-keyed
            # answer the tool never reads, so surfacing it poses an ask the operator's
            # answer can NOT fill. SKIP it: the top-level missing_required backstop asks
            # for the array param satisfiably on the next call. (Array-element bind-back
            # is a named Stage-1+ follow-up — adversarial-review LOW finding.)
            if "[]" in str(sp):
                continue
            f = {"name": str(sp), "type": "string",
                 "description": getattr(r, "rationale", "") or str(sp)}
            if getattr(r, "kind", None) == "credential":
                f["format"] = "password"          # → is_secret_field ⇒ URL-mode
            fields.append(f)
        if not fields:
            return None
        form_fields, secret_fields = split_secret_fields(fields)
        return HarnessRequest(
            kind=HarnessKind.INPUT,
            spec={
                "question": (f"'{tool_name}' needs {len(fields)} value(s) to proceed."),
                "requested_schema": elicitation_schema_from_fields(form_fields),
                "secret_fields": [f["name"] for f in secret_fields],
                "pending_tool": {"tool_name": tool_name, "parameters": dict(parameters or {})},
            },
            rationale=(f"Requirements for '{tool_name}': "
                       f"{', '.join(f['name'] for f in fields)}."),
            fallback=reasoning or "",
            blocking=True,
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
            from systemu.runtime.param_resolution import (
                substitute_parameters, paramsub_pairs,
            )
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
            # B12 (RISK-3): stash the EXACT (old→new) substitution pairs this grant
            # applied so a resume that ALSO carries a persisted objective graph can
            # re-apply the identical string-substitution to the graph nodes (every
            # string leaf — requirements included), instead of overlaying a hand-listed
            # subset of value fields and silently DROPPING substituted values inside
            # requirements/other leaves. See _merge_paramsub_onto_graph.
            context._paramsub_pairs = paramsub_pairs(_params, _answers)
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
            # Fix 1: a B9 runtime_fold AUTH grant carries an EMPTY param_answers by
            # design — the credential is a SECRET supplied out-of-band (URL-mode)
            # into the credential store/env, so it never flows through the form.
            # Re-dispatch anyway: the retried tool re-reads the credential from env.
            # (A NON-fold empty answer still means "declined" and must not re-dispatch.)
            _is_runtime_fold = bool(payload.get("runtime_fold"))
            if _is_runtime_fold:
                # Fix 1: record the fold's satisfy+credit intent for the resume site
                # (which owns the rehydrated graph + completed_objectives). We stash
                # the operator value (for a SEMANTIC decision it's in param_answers;
                # for an AUTH credential the value is a URL-mode secret held out of
                # band — only a marker is stashed, never the secret).
                try:
                    self._resume_fold_credit = {
                        "precede_id": payload.get("precede_id"),
                        "requirement_kind": payload.get("requirement_kind"),
                        "requirement_schema_path": payload.get("requirement_schema_path"),
                        "operator_value": (dict(_answers) if _answers else None),
                    }
                except Exception:
                    self._resume_fold_credit = None
            if not _answers and not _is_runtime_fold:
                context.add_observation({
                    "type": "harness_grant_failed",
                    "message": (
                        "Operator declined to supply the missing parameter(s). "
                        "Use an alternative tool or FAIL — do not fabricate values."
                    ),
                }, current_ab)
                return iter_budget
            import copy as _copy
            _merged = _copy.deepcopy(dict(_pending.get("parameters") or {}))
            _apply_nested_answers(_merged, _answers)
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

    def _apply_resume_fold_credit(self, *, objectives, completed_objectives, context):
        """R-A10 B9 (Fix 1): on a runtime_fold resume, satisfy the backchain precede's
        Requirement and CREDIT its id into ``completed_objectives`` so the original
        objective's ``depends_on`` gate opens and it retries.

        Locates the precede by the stashed ``precede_id`` (else by the matching
        ``origin="backchain"`` runtime_error requirement whose ``schema_path`` equals
        the fold's ``requirement_schema_path``). Flips its Requirement
        ``state`` "missing"→"have" (the model's terminal/bound state — there is no
        "satisfied" literal), stashes the operator value on ``bound_value_ref`` (never
        a raw secret — a marker only), and adds the precede id to
        ``completed_objectives`` using the SAME mechanism the standard
        ``completes_objective`` credit path uses. Returns the (possibly rebuilt)
        objective list. NEVER raises — a structural miss just credits nothing."""
        _fc = getattr(self, "_resume_fold_credit", None) or {}
        _pid = _fc.get("precede_id")
        _schema = _fc.get("requirement_schema_path")
        _kind = _fc.get("requirement_kind")
        _op_value = _fc.get("operator_value")

        def _is_target(o):
            if _pid is not None and getattr(o, "id", None) == _pid:
                return True
            if getattr(o, "origin", None) != "backchain":
                return False
            for r in (getattr(o, "requirements", None) or []):
                if (getattr(r, "source", None) == "runtime_error"
                        and getattr(r, "schema_path", None) == _schema):
                    return True
            return False

        _target = next((o for o in objectives if _is_target(o)), None)
        if _target is None:
            logger.debug("[Runtime B9] resume fold-credit: no matching precede (pid=%s schema=%s)",
                         _pid, _schema)
            return objectives

        _tid = getattr(_target, "id", None)

        # Fix (schema-None over-flip guard): decide WHICH runtime_error requirement
        # this credit satisfies. When a schema_path is stashed, match on it (precise).
        # When it is None (the value_origin was stripped upstream), do NOT flip ALL
        # runtime_error requirements — a precede could carry more than one (e.g. a
        # credential AND a decision) and the operator supplied only one. Narrow by the
        # fold's requirement_kind; if that too is unknown, flip only the FIRST
        # runtime_error requirement (never flip-all).
        def _flip_matches(r) -> bool:
            if getattr(r, "source", None) != "runtime_error":
                return False
            if _schema is not None:
                return getattr(r, "schema_path", None) == _schema
            if _kind is not None:
                return getattr(r, "kind", None) == _kind
            return True  # unknown schema + unknown kind → first-match fallback below

        # For the ambiguous (schema None) path, flip at most ONE requirement so a
        # multi-requirement precede never over-credits the operator's single answer.
        _flip_one_only = _schema is None
        _flipped = False

        # Flip the folded Requirement "missing"→"have" + stash a value REFERENCE
        # (never the raw secret). Rebuild via model_copy so the durable graph carries
        # the satisfied state on the next snapshot.
        try:
            _new_reqs = []
            for r in (getattr(_target, "requirements", None) or []):
                if _flip_matches(r) and not (_flip_one_only and _flipped):
                    _ref = None
                    if isinstance(_op_value, dict) and _op_value:
                        # decision fold: the value is non-secret → a compact reference.
                        _ref = "operator:" + ",".join(sorted(_op_value.keys()))
                    else:
                        # credential fold: the value is a URL-mode secret held out of
                        # band — record only that it was supplied, never the value.
                        _ref = "operator:credential"
                    _new_reqs.append(r.model_copy(update={"state": "have",
                                                          "bound_value_ref": _ref}))
                    _flipped = True
                else:
                    _new_reqs.append(r)
            _new_target = _target.model_copy(update={"requirements": _new_reqs})
            objectives = [(_new_target if getattr(o, "id", None) == _tid else o)
                          for o in objectives]
        except Exception:
            logger.debug("[Runtime B9] resume fold-credit: requirement flip skipped",
                         exc_info=True)

        # CREDIT the precede id — same mechanism as the standard completes_objective
        # path (completed_objectives.add). This opens the original objective's gate.
        if isinstance(_tid, int):
            completed_objectives.add(_tid)
            try:
                self._resume_completed_precedes.add(_tid)
            except Exception:
                pass
            # Reset the stuck counters (crediting an objective resets them everywhere).
            try:
                self._update_stuck_counters(
                    action="RESUME_FOLD_CREDIT", tool_name=None,
                    tool_success=True, credited_obj_id=_tid,
                )
            except Exception:
                self._iters_since_obj_credit = 0
            # Re-persist the durable graph so the satisfied precede + credit survive
            # the next snapshot.
            try:
                context._objective_graph = [o.model_dump(mode="json") for o in objectives]
            except Exception:
                logger.debug("[Runtime B9] resume fold-credit: graph persist skipped",
                             exc_info=True)
            logger.info("[Runtime B9] resume fold-credit: precede %d satisfied + credited "
                        "(%d/%d objectives now complete)",
                        _tid, len(completed_objectives), len(objectives))
        return objectives

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
        self._resume_fold_credit = None    # R-A10 B9 (Fix 1): satisfy+credit intent for a resumed runtime_fold precede
        self._resume_completed_precedes = set()  # R-A10 B9 (Fix 1): precede ids credited on this resume
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
            # G1 (R-A2): the mutated objective graph + id-allocator floor peeled
            # from a resume snapshot; None on a fresh run (→ static scroll tree).
            _resume_objective_graph = None
            _resume_next_objective_id = None
            # R-A9 (Task 9): the cached situational-inventory (report, stamps) peeled
            # from a resume snapshot; None/{} on a fresh run (→ cold survey). Fed to
            # survey_situation's `cache` below so unchanged slices are reused (AC3).
            _resume_situation_report = None
            _resume_situation_stamps: Dict[str, Any] = {}
            if resume_from_execution_id:
                try:
                    from systemu.runtime.execution_snapshot import (
                        apply_to_context, delete_snapshot, read_snapshot,
                    )
                    from systemu.runtime.snapshot_migrations import SnapshotRefused
                    try:
                        snap = read_snapshot(resume_from_execution_id)
                    except SnapshotRefused as _refused:
                        # DEC-9: a newer-than-supported snapshot must not silently
                        # start fresh (that could re-execute effectful actions).
                        # This is THE fresh-vs-resume chokepoint — fail honestly.
                        logger.error("[Runtime] resume refused: %s", _refused)
                        return {
                            "status": "failure",
                            # DEC-9: this build cannot read a newer/garbage snapshot;
                            # re-running won't fix it, and retrying FRESH (the supervisor's
                            # generic failure→retry path drops resume_from_execution_id)
                            # would re-execute the parked run's effects. Mark structural so
                            # _should_retry routes it straight to terminal — never a fresh retry.
                            "structural_failure": True,
                            "error": f"resume refused: {_refused}",
                            "execution_id": execution_id,
                        }
                    if snap is not None:
                        apply_to_context(snap, context=context)
                        # R-A13 Stage-3a — apply a resolved operator-attest sticky
                        # BEFORE the recredit loop + the resubmit guard, so the S4
                        # resume short-circuit credits the objective from the applier's
                        # persisted confirmed bit and the guard does NOT re-park. Gated
                        # on ENFORCE inside the applier; a no-op without an
                        # __OPERATOR_ATTEST__ sticky (none is ever minted in OFF/SHADOW,
                        # so those modes are byte-identical). Never raises.
                        if use_objectives:
                            self._apply_operator_attest_sticky(snap, objectives, context)
                            # R-A13 Stage-3a (double-submit FIX): HOIST the S4
                            # external-evidence CREDIT short-circuit OUT of the
                            # ``snap.completed_objective_ids`` guard below. That guard is
                            # only truthy when the snapshot carried a completed sibling,
                            # so a parked external objective with NO completed sibling
                            # (a single external objective — e.g. the operator-attest
                            # resume for it) was NEVER credited, even though the applier
                            # (or a confirmed api_readback) had already persisted its
                            # confirmed bit. The resubmit guard then EXCLUDED it (already
                            # ``_read_external_ok``) so the run neither parked nor
                            # credited → it fell through and the LLM RE-DROVE the
                            # effectful tool = a double-submit. Credit confirmed externals
                            # HERE, regardless of the completed set. It is CREDIT-ONLY
                            # (the unconfirmed-external observation stays in the block
                            # below / the resubmit guard) and SKIPS ids the snapshot
                            # already lists as completed (those are seeded below), so
                            # every OTHER case — non-empty completed, OFF/SHADOW — is
                            # byte-identical: it credits EXACTLY the confirmed externals
                            # the old short-circuit did, and the sole change is that an
                            # empty-completed confirmed external now credits. Idempotent
                            # via the completed_objectives set. Fail-safe: never raises.
                            try:
                                _seed_ids = set(snap.completed_objective_ids or [])
                                _ext_blocked = _recredit_blocked_ids(
                                    getattr(snap, "objective_graph", None))
                                for _ext_obj in objectives:
                                    if (getattr(_ext_obj, "requires_external_verification", False)
                                            and _ext_obj.id not in completed_objectives
                                            and _ext_obj.id not in _seed_ids
                                            and _ext_obj.id not in _ext_blocked
                                            and _read_external_ok(context, _ext_obj.id)):
                                        completed_objectives.add(_ext_obj.id)
                                        logger.info(
                                            "[Runtime S4] resume re-credit: external obj=%d "
                                            "cleared by a stored confirmed ExternalEvidence.",
                                            _ext_obj.id,
                                        )
                            except Exception:
                                logger.debug(
                                    "[Runtime S4] hoisted external resume re-credit "
                                    "crashed — skipping", exc_info=True)
                        if use_objectives and snap.completed_objective_ids:
                            # Restore completed objective ids — the runtime won't
                            # ask the LLM to redo them.
                            completed_objectives.update(snap.completed_objective_ids)
                            # v0.9.1 (Layer 4): re-credit any objectives NOT in the snapshot
                            # that already have durable evidence on disk.  This covers the
                            # case where the shadow completed work but the snapshot was taken
                            # before the verifier ran (e.g., mid-run restart).
                            try:
                                # R-A10 B9 (Fix C, HIGH/safety): the durable-evidence
                                # recredit hook must NOT re-credit an objective that is
                                # gated on a still-MISSING runtime_error requirement (a
                                # backchain credential/decision precede — or an objective
                                # that depends_on one). objective_verifier.run short-
                                # circuits to verified=True for a legacy verifier=None
                                # objective BEFORE any durable check, so without this
                                # guard the recredit loop — which iterates the STATIC
                                # scroll tree (no depends_on / no requirements) — would
                                # trivially re-credit the credential-gated objective with
                                # the credential still absent → an unauthenticated
                                # "success" that UNDOES B9's Fix A gate. The "which
                                # objectives are blocked" fact lives on the PERSISTED
                                # graph (snap.objective_graph carries the B9 mutation),
                                # NOT the static `objectives` this loop iterates. A
                                # working credential is delivered ONLY via B9's explicit
                                # _apply_resume_fold_credit path, never this shortcut.
                                # ADDITIVE + scoped to runtime_error requirements: legacy
                                # snapshots (empty/None graph, no runtime_error reqs) →
                                # blocked_ids == set() → legacy recredit byte-unchanged.
                                blocked_ids = _recredit_blocked_ids(snap.objective_graph)
                                _uncredited = [
                                    o for o in objectives
                                    if o.id not in completed_objectives
                                    and o.id not in blocked_ids
                                ]
                                for _uc_obj in _uncredited:
                                    # S4 (fail-closed external-effect credit): an
                                    # objective demanding external ground-truth is
                                    # re-credited on resume ONLY from the STORED
                                    # ExternalEvidence.confirmed bit — the epoch-delta
                                    # local verifier (recredit_on_resume) would soft-pass
                                    # a verifier=None objective and silently re-credit an
                                    # unverified external effect. SHORT-CIRCUIT before it:
                                    # no re-fetch / no re-verify, the stored bit decides.
                                    # Non-external objectives fall through to the unchanged
                                    # recredit path below.
                                    if getattr(_uc_obj, "requires_external_verification", False):
                                        if _read_external_ok(context, _uc_obj.id):
                                            completed_objectives.add(_uc_obj.id)
                                            logger.info(
                                                "[Runtime S4] resume re-credit: external obj=%d "
                                                "cleared by a stored confirmed ExternalEvidence.",
                                                _uc_obj.id,
                                            )
                                        else:
                                            logger.warning(
                                                "[Runtime S4] resume re-credit SKIPPED for external "
                                                "obj=%d — no stored confirmed evidence (local "
                                                "epoch-delta verifier is advisory-only).",
                                                _uc_obj.id,
                                            )
                                            context.add_observation(
                                                {
                                                    "type": "UNVERIFIED_EXTERNAL",
                                                    "objective_id": _uc_obj.id,
                                                    "message": (
                                                        "external-effect objective not re-credited "
                                                        "on resume — no confirmed independent "
                                                        "evidence"
                                                    ),
                                                },
                                                current_ab,
                                            )
                                        continue
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
                        # ── S3 / R-A7 wave-3a — RESUME no-silent-re-submit guard ──
                        # A resume whose external objective is still UNCONFIRMED (no
                        # stored ExternalEvidence.confirmed) must NOT let the loop
                        # SILENTLY re-execute its effectful submit path — a blind
                        # re-submit is a double-submit hazard. Surface an OPERATOR CARD
                        # (the existing InboxQueue rail) and PARK instead: only an
                        # explicit operator decision may authorise a re-submit. This
                        # STACKS on S4's resume short-circuit (which already credits a
                        # CONFIRMED bit with no re-fetch); it fires ONLY when an external
                        # objective is genuinely uncredited + unconfirmed on a resume, so
                        # a non-external resume is byte-identical. Fail-safe: any error
                        # in the guard degrades to NOT parking (the S4 gate still fails
                        # the credit closed downstream).
                        if use_objectives:
                            try:
                                _resub_ext = [
                                    o for o in objectives
                                    if getattr(o, "requires_external_verification", False)
                                    and o.id not in completed_objectives
                                    and o.id not in _recredit_blocked_ids(
                                        getattr(snap, "objective_graph", []) or [])
                                    and not _read_external_ok(context, o.id)
                                ]
                            except Exception:
                                logger.debug(
                                    "[Runtime S3] resubmit-guard scan failed — not parking",
                                    exc_info=True)
                                _resub_ext = []
                            if _resub_ext:
                                _card_id = None
                                try:
                                    _oids = ", ".join(str(o.id) for o in _resub_ext)
                                    _descriptor = GateDescriptor(
                                        title=("Operator: authorise re-submit of an "
                                               "unverified external effect"),
                                        risk="high",
                                        inspect=(
                                            "Resuming a run with external-effect "
                                            f"objective(s) [{_oids}] that have NO "
                                            "confirmed independent evidence. Re-running "
                                            "the submit could DOUBLE-SUBMIT (e.g. a "
                                            "duplicate payment). Confirm the prior "
                                            "submit's outcome before re-submitting."),
                                        options=["Do not re-submit (park)",
                                                 "Re-submit — I confirm it did NOT go through"],
                                        safe_default="Do not re-submit (park)",
                                        what_approve_does=(
                                            "Authorises the shadow to RE-EXECUTE the "
                                            "external submit. Only choose this if you "
                                            "have verified the effect did not already "
                                            "occur."),
                                        dedup=f"external_resubmit:{execution_id}:{_oids}",
                                    )
                                    _card_id = InboxQueue(self.vault).enqueue(
                                        _descriptor,
                                        gate_type="operator",
                                        body="",
                                        context_extras={
                                            "kind": "external_resubmit_guard",
                                            "objective_ids": [o.id for o in _resub_ext],
                                            "execution_id": execution_id,
                                        },
                                    )
                                    logger.warning(
                                        "[Runtime S3] resume PARKED — external obj(s) [%s] "
                                        "unconfirmed; operator card %s raised to authorise "
                                        "any re-submit (no silent re-submit).",
                                        _oids, _card_id,
                                    )
                                except Exception:
                                    logger.debug(
                                        "[Runtime S3] resubmit-guard card enqueue failed",
                                        exc_info=True)
                                _susp = context.build_result(
                                    status="suspended_external_resubmit",
                                    final_summary=_augment_summary_with_committed_effects(
                                        "Parked awaiting operator decision: a resumed "
                                        "external-effect objective has no confirmed "
                                        "evidence; re-submitting could double-submit. "
                                        "The operator must confirm the prior outcome "
                                        "before any re-submit.",
                                        context),
                                )
                                _susp["activity_id"] = getattr(activity, "id", "")
                                _susp["shadow_id"] = getattr(shadow, "id", "")
                                if _card_id is not None:
                                    _susp["operator_card_id"] = _card_id
                                return _susp
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
                                        # Fix 1 (B9): a runtime_fold pending request
                                        # carries pending_tool + the fold markers in
                                        # its SPEC. The daemon reconciler only forwards
                                        # pending_tool/param_answers on the grant; lift
                                        # the fold markers off the pending spec here so
                                        # the resume rail (a) re-dispatches even when
                                        # the credential went URL-mode (empty answers)
                                        # and (b) can satisfy + credit the precede.
                                        _pspec = _ppayload.get("spec") or {}
                                        if isinstance(_pspec, dict) and _pspec.get("runtime_fold"):
                                            _gpayload.setdefault("runtime_fold", True)
                                            for _k in ("requirement_kind",
                                                       "requirement_schema_path",
                                                       "precede_id"):
                                                if _pspec.get(_k) is not None:
                                                    _gpayload.setdefault(_k, _pspec[_k])
                                            # If the reconciler didn't forward a
                                            # pending_tool (older path), fall back to
                                            # the one on the pending spec.
                                            if not _gpayload.get("pending_tool") and _pspec.get("pending_tool"):
                                                _gpayload["pending_tool"] = _pspec["pending_tool"]
                                            _gpayload.setdefault("param_answers", {})
                                    except Exception:
                                        logger.debug(
                                            "[Runtime B9] pending-spec fold-marker lift skipped",
                                            exc_info=True)
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
                        # G1 (R-A2): peel the durable objective graph before the
                        # snapshot is consumed. Applied below at the single
                        # authoritative objectives-rebuild site.
                        try:
                            _resume_objective_graph = list(getattr(snap, "objective_graph", []) or [])
                            _resume_next_objective_id = getattr(snap, "next_objective_id", None)
                        except Exception:
                            _resume_objective_graph = None
                            _resume_next_objective_id = None
                        # R-A9 (Task 9): peel the cached situational-inventory
                        # (report, stamps) before the snapshot is consumed, so the
                        # pre-plan survey below can reuse unchanged slices (AC3).
                        try:
                            _resume_situation_report = getattr(snap, "situation_report", None)
                            _rss = getattr(snap, "situation_stamps", {})
                            _resume_situation_stamps = dict(_rss) if isinstance(_rss, dict) else {}
                        except Exception:
                            _resume_situation_report = None
                            _resume_situation_stamps = {}
                        # Fix 2 (de-dup): the objective_graph + requirement_report
                        # re-seed formerly duplicated here is now the SOLE
                        # responsibility of apply_to_context (called above at the top
                        # of this `if snap is not None:` block, well before this point
                        # and before delete_snapshot). apply_to_context is the re-seed
                        # AUTHORITY — see execution_snapshot.apply_to_context. Keeping a
                        # second copy here risked divergence: this block's
                        # requirement_report write was UNCONDITIONAL (it clobbered a
                        # pre-set report with a snapshot's None), whereas apply guards on
                        # `is not None`. Once B10 adds a requirement_report producer that
                        # divergence becomes reachable, so the duplicate is removed and
                        # apply's guarded re-seed wins.
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
                            final_summary=_augment_summary_with_committed_effects(
                                "Parked awaiting operator confirmation of recorded "
                                "task parameters.",
                                context),
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
            #
            # v0.9.35 param-substitution seam-fix + G1 (R-A2) persisted-graph
            # rehydrate, folded into one authoritative objectives assignment with
            # explicit precedence (persisted graph > param-sub > static scroll tree).
            objectives, scroll_json = _resolve_objectives_for_run(
                use_objectives=use_objectives,
                objectives=objectives,
                scroll_json=scroll_json,
                context=context,
                resume_objective_graph=_resume_objective_graph,
            )

            # ── R-A10 B9 (Fix 1): satisfy + credit a resumed runtime_fold precede ──
            # A runtime_fold grant was applied above; the operator supplied the
            # credential/decision. Now that the graph is rehydrated, flip the
            # backchain precede's Requirement "missing"→"have" (stash the operator
            # value on it) and CREDIT the precede id into completed_objectives so the
            # original objective's depends_on gate opens and it retries. Fail-safe.
            if use_objectives and getattr(self, "_resume_fold_credit", None):
                try:
                    objectives = self._apply_resume_fold_credit(
                        objectives=objectives,
                        completed_objectives=completed_objectives,
                        context=context,
                    )
                except Exception:
                    logger.debug("[Runtime B9] resume fold-credit skipped", exc_info=True)
                finally:
                    self._resume_fold_credit = None

            # ── R-A9 (§5.1): situational-inventory pre-plan stage ──────────────
            # MOVED UP (R-A10 B7): the survey MUST run BEFORE the open-world planner
            # (which reads context._situation_report) and BEFORE the completion
            # denominator / id-floor / B5 write (which the planner's inserts feed).
            # Behavior-preserving: the survey only READS scroll/self.vault/the resume
            # cache and stashes context._situation_report/_situation_stamps — it does
            # NOT use total_objectives/next_objective_id/objectives, so moving it
            # ahead of them changes nothing (R-A9 asserts behavior, not line order).
            #
            # Defensive, FAIL-SAFE, NON-BLOCKING. survey_situation is internally
            # per-source-timeout-bounded and runs its blocking builders off-loop
            # (asyncio.to_thread); we ALSO wrap it in an OVERALL timeout as belt-and-
            # suspenders, and swallow ANY failure/timeout so the run is left EXACTLY
            # as it is today (no report, no crash, no hang, no operator card — AC7).
            # On a resume the peeled (report, stamps) feed the survey's cache so
            # unchanged slices are reused (AC3). It ONLY stashes on context — it never
            # touches `objectives`, the schedule, or any decision (AC6 no-perturbation).
            try:
                from systemu.runtime.situational_inventory import survey_situation
                import asyncio as _asyncio
                _cache = (
                    {"report": _resume_situation_report,
                     "stamps": _resume_situation_stamps}
                    if _resume_situation_report else None
                )
                _report, _stamps = await _asyncio.wait_for(
                    survey_situation(scroll, vault=self.vault, cache=_cache),
                    timeout=20.0,   # overall bound over the per-source timeouts
                )
                context._situation_report = _report.model_dump()
                context._situation_stamps = _stamps
            except Exception:
                logger.debug(
                    "[Runtime] situational inventory survey skipped (non-fatal)",
                    exc_info=True,
                )

            # ── R-A10 (§5.2): run-time open-world PLANNER stage ────────────────
            # FRESH runs only. A resume rehydrates any prior planner inserts from the
            # persisted objective_graph (fold branch 1); re-running the planner would
            # DOUBLE-insert them and could perturb a resumed schedule, so we gate on
            # `not resume_from_execution_id` — the resume REQUEST itself (the same
            # fresh-vs-resume chokepoint DEC-9 uses). Even a resume whose snapshot was
            # missing/corrupt must not re-plan (that is exactly the double-insert
            # hazard), so we key off the request, not the peeled graph.
            #
            # FAIL-SAFE + AC6: run_open_world_planner returns the SAME `objectives`
            # object BY IDENTITY on a "no precede-objectives" decision (or any bad
            # LLM response). We only rebind `objectives` here, so an empty decision
            # leaves `objectives is scroll.objectives` true → the B5 write below skips
            # → a no-replanning run stays byte-identical. Any error → static tree.
            if (use_objectives and not resume_from_execution_id
                    and getattr(context, "_situation_report", None)):
                try:
                    # id-floor for the planner's inserts: monotonic past both the
                    # current max id and any resume-restored allocator value. The
                    # AUTHORITATIVE next_objective_id below is recomputed AFTER the
                    # planner so it reflects the inserts.
                    _pre_floor = max((o.id for o in objectives), default=0) + 1
                    # planner runs only on fresh runs (gated on `not
                    # resume_from_execution_id`), so _resume_next_objective_id is
                    # always None here → this reduces to _pre_floor; the ternary
                    # mirrors the authoritative recompute at ~:3806 for symmetry.
                    _planner_next = (
                        max(_resume_next_objective_id, _pre_floor)
                        if _resume_next_objective_id is not None
                        else _pre_floor
                    )
                    objectives = await run_open_world_planner(
                        objectives=objectives,
                        scroll_intent=getattr(context, "scroll_intent", None) or scroll.intent,
                        situation_report=context._situation_report,
                        config=self.config,
                        next_id=_planner_next,
                        scroll=scroll,
                    )
                except Exception:
                    logger.debug(
                        "[Runtime] open-world planner skipped (non-fatal)",
                        exc_info=True,
                    )
                    # objectives untouched → static tree (AC6 fail-safe).

            # G1 (R-A2): the fold may have swapped `objectives` for a longer persisted
            # graph, and the R-A10 planner may have inserted precede-objectives —
            # re-derive the completion denominator so the gate/progress logs count the
            # ACTUAL objectives this run must satisfy (not the stale scroll tree).
            total_objectives = len(objectives)
            # G1: id-allocator floor for post-resume inserts. Monotonic and
            # collision-proof: never sits below max(existing id)+1, so a
            # corrupt/hand-edited restored value can't seed an id collision; a
            # legitimately-advanced restored value (>= floor) wins. Computed ONCE
            # here (AFTER the planner) so it reflects any inserted ids. Stashed on
            # context so capture_from_context persists it on the next snapshot.
            _floor = max((o.id for o in objectives), default=0) + 1
            next_objective_id = (
                max(_resume_next_objective_id, _floor)
                if _resume_next_objective_id is not None
                else _floor
            )
            context._next_objective_id = next_objective_id
            # R-A10 (RISK-2): persist the authoritative post-fold objective graph
            # so a mutation (persisted graph / planner insert / param-sub) survives
            # the NEXT snapshot — capture_from_context reads context._objective_graph.
            # CONDITIONAL on divergence from the static scroll tree BY IDENTITY: a
            # never-mutated run has `objectives is scroll.objectives` (the fold's
            # branch-3 identity return AND the planner's no-insert identity return),
            # so we leave _objective_graph UNSET → capture persists [] and the next
            # resume takes the identity branch (AC6 byte-identical schedule, snapshot
            # bytes UNCHANGED vs G1). A mutated / planner-inserted / param-sub /
            # resumed-from-graph run diverged → we write the graph so it re-persists
            # (and a resumed run keeps re-persisting it, not [] on the second
            # snapshot — the resume block re-seeds _objective_graph below).
            if use_objectives and objectives is not scroll.objectives:
                context._objective_graph = [o.model_dump(mode="json") for o in objectives]

            # ── R-A10 (§5.3 / §5.6): RequirementReport producer (B10) ──────────
            # Invoke build_requirement_report so context._requirement_report gets a
            # real producer (B6 persists + resume-restores it). FRESH runs only —
            # a resume rehydrates the prior report from the snapshot via
            # apply_to_context, so re-producing here would clobber it (the same
            # fresh-vs-resume chokepoint the planner uses).
            #
            # CAPABILITY-RESOLUTION (grounded): an Objective carries NO declared /
            # resolvable target capability pre-loop (the LLM selects a tool per
            # objective INSIDE the loop), so a clean per-objective capability is
            # NOT available here. Rather than fabricate a capability GUESS, we pass
            # the resolvable capability we have — none, pre-loop — which makes the
            # binder return an EMPTY report: a no-op that establishes the producer
            # seam + the persistence round-trip WITHOUT a hallucinated diff. The
            # mid-loop binder-in-loop (where a real per-objective capability IS
            # selected, and the ask_bundle carries real gaps) is R-A12.
            #
            # FAIL-SAFE + AC6: _populate_requirement_report swallows any binder/
            # render error (empty ask_bundle ⇒ no elicitation, no perturbation), and
            # lets a PendingChoiceRequest PROPAGATE (the suspend is the rail).
            if use_objectives and not resume_from_execution_id:
                _populate_requirement_report(
                    context,
                    objectives=objectives,
                    capability=None,   # no per-objective capability pre-loop (R-A12)
                    situation=getattr(context, "_situation_report", None) or {},
                    vault=self.vault,
                    config=self.config,
                )

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
                # R-P3a: carry this run's ambient execution_id (set at run top)
                # across the run_in_executor thread hop so the router's cost hook
                # attributes the decision call to THIS run instead of orphaning.
                # run_in_executor does NOT copy contextvars — copy_context()+ctx.run
                # does. (A sub-agent child runs its own execute() under its own eid,
                # so its decision call attributes to the child, which rolls up to
                # the run-tree root.)
                import contextvars as _cv
                _llm_ctx = _cv.copy_context()
                try:
                    decision = await loop.run_in_executor(
                        None,
                        lambda: _llm_ctx.run(
                            llm_call_json,
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

                    # R-A10 B9 (Fix C, ROOT-CAUSE): the credential-gate must hold at the
                    # COMPLETE goal-level accept too. A goal-verifier PASS (_goal_ok)
                    # knows NOTHING about the backchain precede, so on its own it would
                    # finalize status="success" while a required credential/decision is
                    # still missing. If _recredit_blocked_ids over the LIVE objective list
                    # (which after a fold carries the precede + the original's updated
                    # depends_on) reports ANY objective still gated on a missing
                    # runtime_error requirement, the goal CANNOT be truly complete — reject
                    # the COMPLETE and steer the shadow to work the prerequisite instead of
                    # finalizing. ADDITIVE + scoped: empty for any non-credential-gated run
                    # → legacy/normal COMPLETE handling is byte-unchanged.
                    _blocked_ids = _recredit_blocked_ids(objectives) if use_objectives else set()
                    if use_objectives and _blocked_ids:
                        _goal_ok = False  # a known-missing credential defeats a goal-level pass

                    # S4 (fail-closed external-effect credit): an uncredited
                    # external-effect objective defeats a goal-level PASS at the
                    # COMPLETE accept too. The goal verifier judges only LOCAL
                    # StateDelta and is BLIND to external effects — a PASS there
                    # would otherwise finalize status="success" while a pending
                    # requires_external_verification objective never got confirmed
                    # ExternalEvidence (it correctly failed closed at the per-objective
                    # site + emitted UNVERIFIED_EXTERNAL). Mirror the B9 _blocked_ids
                    # gate: a pending external objective with no persisted confirmed
                    # evidence (_read_external_ok is not True) forces reject. ADDITIVE
                    # + scoped: _ext_pending_ids is empty for any run with no pending
                    # external objective → non-external / legacy COMPLETE handling is
                    # BYTE-IDENTICAL (exactly like the B9 gate).
                    _ext_pending_ids = {
                        _o.id for _o in objectives
                        if _o.id not in completed_objectives
                        and getattr(_o, "requires_external_verification", False)
                        and _read_external_ok(context, _o.id) is not True
                    } if use_objectives else set()
                    if _ext_pending_ids:
                        _goal_ok = False  # a pending unconfirmed external effect defeats a goal-level pass

                    # Reject premature COMPLETE when objectives are still pending,
                    # UNLESS goal-level verification accepted it — OR when a credential
                    # gate is still open (blocked non-empty), which always rejects — OR
                    # when a pending external objective lacks confirmed evidence
                    # (_ext_pending_ids non-empty), which always rejects. These compose:
                    # the B9 and S4 gates each independently force reject and neither
                    # weakens the other.
                    if use_objectives and (
                            _blocked_ids
                            or _ext_pending_ids
                            or (len(completed_objectives) < total_objectives and not _goal_ok)):
                        missing = [obj.id for obj in objectives if obj.id not in completed_objectives]
                        if _ext_pending_ids and not _blocked_ids:
                            # S4: steer the shadow to obtain confirmed external evidence
                            # rather than re-attempting COMPLETE (mirrors the per-objective
                            # UNVERIFIED_EXTERNAL site + the B9 credential-gate branch).
                            logger.warning(
                                "[Runtime S4] COMPLETE rejected — external-effect "
                                "objective(s) %s pending without confirmed independent "
                                "evidence; the goal verifier is blind to external effects. "
                                "Obtain confirmed evidence before COMPLETE.",
                                sorted(_ext_pending_ids),
                            )
                            for _eid in sorted(_ext_pending_ids):
                                context.add_observation(
                                    {
                                        "type": "UNVERIFIED_EXTERNAL",
                                        "objective_id": _eid,
                                        "message": (
                                            "COMPLETE rejected: external-effect objective "
                                            f"{_eid} has no confirmed independent evidence "
                                            "(the goal verifier cannot see external effects). "
                                            "Obtain confirmed evidence for the external "
                                            "objective before finalizing."
                                        ),
                                    },
                                    current_ab,
                                )
                            continue  # Return to loop — shadow must confirm the external effect
                        if _blocked_ids:
                            logger.warning(
                                "[Runtime B9] COMPLETE rejected — objective(s) %s blocked on a "
                                "missing runtime_error requirement (credential/decision gate); the "
                                "goal cannot be complete until the operator supplies it.",
                                sorted(_blocked_ids),
                            )
                            context.add_observation(
                                {
                                    "type": "objective_blocked_credential_gate",
                                    "objective_id": sorted(_blocked_ids)[0],
                                    "message": (
                                        f"COMPLETE rejected: objective(s) {sorted(_blocked_ids)} "
                                        f"are blocked on a missing credential/decision requirement "
                                        f"(runtime_error). The goal cannot be complete until that "
                                        f"requirement is satisfied by the operator — work the "
                                        f"prerequisite objective."
                                    ),
                                },
                                current_ab,
                            )
                        else:
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
                            # R-A13.5 — accrete this harness ask into the deterministic
                            # avoidable-ask corpus (observability-only, append-only; it
                            # NEVER affects the run). INPUT asks (the if-branch above)
                            # are operator-input by nature and excluded — only this
                            # instrumented else-branch (tool/credential/decision asks,
                            # where "did it try to resolve first" is a meaningful §10
                            # question) is recorded.
                            try:
                                from systemu.runtime import replay_metrics as _rm
                                _rm.record_ask(
                                    self.vault,
                                    kind=str(getattr(_hk, "value", _hk) or ""),
                                    attempts_before=_att,
                                    tool_attempts=len(_prov.get("tool_attempts") or []),
                                    blocked_signals=_prov.get("blocked_signals") or [],
                                    confidence=_conf,
                                )
                            except Exception:
                                pass
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
                        # ── Seam A (R-A11b-2): discovery-before-forge auto-reuse ──
                        # For a kind=tool forge request only, run ONE deterministic
                        # pass over the DEPLOYED+enabled vault catalog. A confident
                        # match → inject its name into enabled_tools (activating the
                        # arbiter's LOW-GRANT reuse branch) + stash reuse_tool_id so
                        # Seam B reuses it instead of forging. Always stash the pass
                        # summary so the operator card / ledger cite it (AC2). Keep
                        # the SAME arbitrate() call so BOTH v0.9.47 caps still count.
                        # Fail-safe: any error leaves _arb_ctx unchanged → forge.
                        _disc = None
                        if _req.kind == HarnessKind.TOOL and self.vault is not None:
                            try:
                                from systemu.runtime.discovery_pass import (
                                    deployed_enabled_catalog, discovery_pass,
                                )
                                _cat = deployed_enabled_catalog(self.vault)
                                _disc = discovery_pass(
                                    (_req.spec or {}).get("name", ""),
                                    _req.rationale or "",
                                    _cat,
                                )
                                _req.spec["discovery"] = {
                                    "searched": _disc.searched,
                                    "best_score": _disc.best_score,
                                    "floor": _disc.floor,
                                }
                                if _disc.reuse_tool_id:
                                    # Inject the REQUESTED name (what the arbiter's
                                    # is_reuse checks: spec.name in enabled_tools), NOT
                                    # the matched tool name — so the LOW-GRANT reuse
                                    # branch fires EXACTLY when discovery decided to
                                    # reuse (an exact-name match), regardless of case.
                                    # Seam B resolves the actual tool by reuse_tool_id.
                                    _arb_ctx["enabled_tools"] = [
                                        (_req.spec or {}).get("name", "")]
                                    _req.spec["reuse_tool_id"] = _disc.reuse_tool_id
                                    _req.spec["reuse_score"] = _disc.best_score
                            except Exception:
                                logger.debug("[Runtime] discovery pass failed — "
                                             "falling through to forge", exc_info=True)
                                _disc = None
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
                        # R-A11b-2 MISS audit: a kind=tool forge that ran a
                        # discovery pass but did NOT reuse (no confident match)
                        # records the pass so the operator sees the forge was
                        # requested only AFTER discovery found nothing (AC2). Uses
                        # the same sanctioned manual-append path as the cap-deny row.
                        if (_disc is not None and not _disc.reuse_tool_id
                                and _verdict.decision != HarnessDecision.GRANT):
                            try:
                                _gov._ledger_append(
                                    _gov._ledger_entry(
                                        _req, _verdict,
                                        {"discovery_miss": {
                                            "searched": _disc.searched,
                                            "best_score": _disc.best_score,
                                            "floor": _disc.floor,
                                        }},
                                        execution_id),
                                    vault=self.vault, execution_id=execution_id,
                                )
                            except Exception:
                                logger.debug("[Runtime] discovery-miss ledger write "
                                             "failed", exc_info=True)
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
                                final_summary=_augment_summary_with_committed_effects(
                                    "Parked awaiting operator harness decision: "
                                    f"{_req.kind.value} — {_verdict.rationale}",
                                    context),
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
                    # ── R-A13a (§5.5/§5.6) — MID-LOOP per-objective requirement binder ──
                    # The LLM has now chosen a tool + its params for this objective, so a
                    # REAL per-objective capability is resolvable (unlike the pre-loop
                    # producer's capability=None). Run the binder with the call's provided
                    # params (source #0, path-descent): a fully-provided call ⇒ EMPTY
                    # ask_bundle ⇒ fall through UNCHANGED; a genuine (incl. NESTED) gap ⇒
                    # a bundled scope card on the WORKING harness_request rail ⇒ SUSPEND.
                    # Placed before the pre-submit guard because Stage 2/3 will need the
                    # stamp landed first; with S4_STAMP=OFF the binder writes no stamp, so
                    # ordering is inert today. Fully guarded: a missing tool / non-int
                    # objective / no schema / any binder error is a NO-OP.
                    if use_objectives:
                        try:
                            _b_tool = next(
                                (t for t in tools if getattr(t, "name", None)
                                 == decision.get("tool_name")), None)
                            _b_co = decision.get("completes_objective")
                            _b_obj = (next((o for o in objectives if o.id == _b_co), None)
                                      if isinstance(_b_co, int) else None)
                            _b_report = None
                            if (_b_tool is not None and _b_obj is not None
                                    and getattr(_b_tool, "parameters_schema", None)):
                                from systemu.runtime.requirement_binder import (
                                    build_requirement_report)
                                _b_report = build_requirement_report(
                                    [_b_obj], _b_tool,
                                    getattr(context, "_situation_report", None) or {},
                                    context,
                                    provided_params=decision.get("parameters") or {})
                            _b_ask = list(getattr(_b_report, "ask_bundle", None) or []) \
                                if _b_report is not None else []
                        except Exception:
                            logger.debug("[Runtime] R-A13a mid-loop binder skipped (non-fatal)",
                                         exc_info=True)
                            _b_ask = []
                        if _b_ask:
                            try:
                                _b_req = self._build_bundled_scope_card(
                                    decision.get("tool_name") or "?", _b_ask,
                                    decision.get("parameters") or {},
                                    decision.get("reasoning", ""))
                            except Exception:
                                _b_req = None
                                logger.debug("[Runtime] R-A13a card build failed — "
                                             "falling through (fail-safe)", exc_info=True)
                            if _b_req is not None:
                                # Surface via the WORKING rail (mirror the missing_required
                                # suspend rail). Headless with no queue ⇒ fall through (let
                                # the tool call proceed / missing_required handle it) rather
                                # than wedge.
                                from systemu.interface.notifications import (
                                    is_headless, _get_decision_queue)
                                if not (_get_decision_queue() is None and is_headless()):
                                    try:
                                        from systemu.runtime.governor import Governor
                                        _b_gov = governor or Governor(self.config)
                                        _b_arb = _harness_arbitration_context(
                                            harness_requests_this_run,
                                            int(getattr(self, "_subagent_depth", 0)))
                                        _b_verdict = _b_gov.arbitrate(_b_req, context=_b_arb)
                                    except Exception:
                                        _b_verdict = None
                                    if _b_verdict is not None:
                                        try:
                                            from systemu.runtime.execution_snapshot import (
                                                capture_from_context, write_snapshot)
                                            import json as _json
                                            _b_snap = capture_from_context(
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
                                                root_execution_id=root_eid)
                                            _b_snap.sticky_notes.append(
                                                f"__HARNESS_PENDING__::{execution_id}::"
                                                + _json.dumps({
                                                    "request_id": _b_req.request_id,
                                                    "kind": _b_req.kind.value,
                                                    "spec": _b_req.spec,
                                                    "fallback": _b_req.fallback}))
                                            write_snapshot(_b_snap)
                                        except Exception:
                                            logger.debug("[Runtime] R-A13a snapshot failed",
                                                         exc_info=True)
                                        try:
                                            from systemu.interface.harness_review import (
                                                surface_harness_request)
                                            _b_did = surface_harness_request(
                                                _b_req, _b_verdict, execution_id=execution_id,
                                                activity_id=activity.id, shadow_id=shadow.id,
                                                vault=self.vault)
                                            logger.info("[Runtime] R-A13a bundled scope card "
                                                        "parked (operator card %s)", _b_did)
                                        except Exception:
                                            logger.debug("[Runtime] R-A13a surface failed",
                                                         exc_info=True)
                                        _revoke_harness_leases(record_run=False, reconcile=False)
                                        _b_susp = context.build_result(
                                            status="suspended_harness_escalation",
                                            final_summary=_augment_summary_with_committed_effects(
                                                "Parked awaiting operator input: requirement(s) "
                                                f"for tool {decision.get('tool_name') or '?'}.",
                                                context))
                                        _b_susp["activity_id"] = activity.id
                                        _b_susp["shadow_id"] = shadow.id
                                        return _b_susp

                    # ── S3 / R-A7 wave-3a — PRE-SUBMIT freshness snapshot ──
                    # Before the effectful call issues, capture the pre-submit
                    # tokens/state that prove token-freshness (a token that already
                    # existed pre-submit is stale and cannot confirm). R-A13b-2i: fire
                    # on the ARMED view (live requires_external_verification OR the
                    # SHADOW _s4_stamp_shadow, via _armed_meter_objective) so capture
                    # and the meter's verify agree on the armed net — SHADOW never
                    # writes the live field, so the un-widened guard never captured and
                    # even a correct echo would-PARKED (the SEAM-3 artifact). The
                    # capture body now runs a REAL independent pre-submit probe
                    # (runtime=self). Cheap + deterministic; fail-closed when no clean
                    # snapshot is available. Threaded into the verifier hook at the
                    # credit seam via ``_presubmit_external_snapshot``.
                    self._presubmit_external_snapshot = None
                    try:
                        _co = decision.get("completes_objective")
                        if isinstance(_co, int) and use_objectives:
                            _co_obj = next(
                                (o for o in objectives if o.id == _co), None)
                            if _co_obj is not None and getattr(
                                    _armed_meter_objective(_co_obj),
                                    "requires_external_verification", False):
                                self._presubmit_external_snapshot = (
                                    _capture_presubmit_external_snapshot(
                                        decision, runtime=self))
                    except Exception:
                        logger.debug("[Runtime] presubmit snapshot guard failed",
                                     exc_info=True)
                    # R-A14a: for a KNOWN-mutation money-move MCP call whose readback
                    # target is knowable PRE-SUBMIT (a curated idempotency template),
                    # run the independent pre-submit freshness probe BEFORE the mutation
                    # and stash it for the credit-seam capture_evidence. DORMANT + no-op
                    # when no template is curated (probe_ran=False) → byte-identical to
                    # today until a real money-move MCP tool is curated.
                    self._mcp_presubmit_snapshot = None
                    try:
                        _mcp_entry = _known_mutation_mcp_entry(decision)
                        if _mcp_entry is not None and _is_money_move_seam(
                                None, decision, _mcp_entry):
                            from systemu.runtime.actuation.mcp_modality import (
                                McpActuationModality)
                            from systemu.runtime.actuation.modality import Action as _Act
                            _mcp_probe_action = _Act(
                                modality="mcp",
                                name=(decision.get("tool_name") or ""),
                                params=(decision.get("parameters") or {}),
                                is_mutation=True, objective=None, tool=_mcp_entry)
                            self._mcp_presubmit_snapshot = (
                                McpActuationModality(self).probe_presubmit(_mcp_probe_action))
                    except Exception:
                        logger.debug("[Runtime R-A14a] mcp pre-submit probe guard failed",
                                     exc_info=True)
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
                            final_summary=_augment_summary_with_committed_effects(
                                "Parked awaiting operator input: missing required "
                                f"parameter(s) for tool "
                                f"{decision.get('tool_name', '') or '?'}.",
                                context),
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

                    # ── R-A10 B9 (AC4): runtime-error-as-requirement fold ──────────
                    # A tool failure that is really a MISSING REQUIREMENT (a 401/403
                    # auth failure, a 422/404 bad-request) should NOT count toward the
                    # stuck bound and fail the run. Fold it into a Requirement + a
                    # backchain precede-objective, EXEMPT the stuck counters (this is a
                    # discovered requirement, not a lack of progress), and SUSPEND via
                    # the INPUT rail so the operator supplies the credential/decision;
                    # on resume the precede is satisfied and the original objective
                    # retries. A 500/other http_error or a non-http failure falls
                    # through UNCHANGED to the normal reflection + stuck path below.
                    if use_objectives and result is not None and not getattr(result, "success", False):
                        try:
                            from systemu.runtime.failure_classifier import (
                                classify_tool_result, http_error_subclass,
                            )
                            _cls = classify_tool_result(result)
                            _sub = (http_error_subclass(result)
                                    if _cls.category == "http_error" else "other")
                        except Exception:
                            _sub = "other"
                        if _sub in ("auth", "semantic"):
                            _fold_susp = self._fold_runtime_error_and_suspend(
                                objectives=objectives,
                                completed_objectives=completed_objectives,
                                decision=decision,
                                sub=_sub,
                                tool_name=tool_name,
                                context=context,
                                scroll=scroll,
                                shadow=shadow,
                                activity=activity,
                                execution_id=execution_id,
                                root_eid=root_eid,
                                iteration=iteration,
                                current_ab=current_ab,
                                harness_requests_this_run=harness_requests_this_run,
                                loop_guard=loop_guard,
                                revoke_harness_leases=_revoke_harness_leases,
                            )
                            if _fold_susp is not None and _fold_susp.get("already_pending"):
                                # Fix 2: idempotent-pending — a precede for this
                                # service is already inserted and still missing (a
                                # repeated 401 across the loop). The exemption was
                                # applied inside the fold; SKIP _update_stuck_counters /
                                # loop_guard.record / _stuck_trigger for this iteration
                                # so this failure NEVER counts toward the stuck bound,
                                # and continue the loop (the run stays parked on the
                                # still-pending precede — it is not lack of progress).
                                objectives = _fold_susp["objectives"]
                                continue
                            if _fold_susp is not None:
                                # The fold mutated the tree in place-of-return; adopt it
                                # so any post-suspend bookkeeping sees the new schedule.
                                objectives = _fold_susp["objectives"]
                                return _fold_susp["result"]
                            # else: fold degraded (unresolvable obj / genuinely
                            # unfoldable) → fall through to the normal reflection +
                            # stuck path (a repeated-pending case never reaches here).

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

                    # ── S3 / R-A7 wave-3b — IMPL-6 ambiguous-outcome protocol ──
                    # On a TRANSPORT-AMBIGUOUS failure of an EFFECTFUL
                    # (requires_external_verification) call — a timeout AFTER send,
                    # a connection reset, a 5xx-after-send — the effect MIGHT have
                    # landed. Run a read-back keyed to the CLIENT idempotency key
                    # (minted+injected BEFORE send) BEFORE any retry decision (i.e.
                    # before the circuit breaker below):
                    #   * confirmed-present ⇒ persist ExternalEvidence(confirmed) +
                    #     route to the credit path — NO re-submit (fall through to
                    #     the credit seam, which now sees result.success via the
                    #     evidence bit); suppress the retry.
                    #   * confirmed-absent  ⇒ a retry is SAFE — fall through to
                    #     today's failure/retry behavior (the loop re-issues).
                    #   * indeterminate / no-primitive ⇒ operator card + PARK —
                    #     never a silent retry, never confirmed-absent. Uses the
                    #     same InboxQueue operator-card rail as the wave-3a resume
                    #     guard. Guarded on the detector → a non-external / clean
                    #     failure is byte-identical to today.
                    if not result.success and use_objectives:
                        try:
                            _co_i6 = decision.get("completes_objective")
                            _obj_i6 = (
                                next((o for o in objectives if o.id == _co_i6), None)
                                if isinstance(_co_i6, int) else None)
                            if _obj_i6 is not None and _is_ambiguous_effectful_failure(
                                    objective=_obj_i6,
                                    result_dict=(result.to_dict()
                                                 if hasattr(result, "to_dict") else {})):
                                _i6_out = self._impl6_handle_ambiguous(
                                    objective=_obj_i6, decision=decision,
                                    result=result, context=context, tools=tools)
                                if _i6_out is not None:
                                    if _i6_out.decision == "indeterminate":
                                        # operator card + PARK — no silent retry.
                                        _card_id = self._impl6_enqueue_operator_card(
                                            objective=_obj_i6, execution_id=execution_id,
                                            detail=_i6_out.detail)
                                        _susp = context.build_result(
                                            status="suspended_external_ambiguous",
                                            final_summary=_augment_summary_with_committed_effects(
                                                "Parked awaiting operator decision: an "
                                                "effectful external call failed AMBIGUOUSLY "
                                                "(the effect may or may not have landed) and "
                                                "the outcome could not be read back "
                                                "deterministically. Re-submitting could "
                                                "double-submit; the operator must confirm the "
                                                "prior outcome before any retry.",
                                                context),
                                        )
                                        _susp["activity_id"] = getattr(activity, "id", "")
                                        _susp["shadow_id"] = getattr(shadow, "id", "")
                                        if _card_id is not None:
                                            _susp["operator_card_id"] = _card_id
                                        logger.warning(
                                            "[Runtime IMPL-6] PARKED obj=%d — ambiguous "
                                            "effectful failure, read-back indeterminate "
                                            "(%s); operator card %s (no silent retry).",
                                            getattr(_obj_i6, "id", 0), _i6_out.detail,
                                            _card_id)
                                        return _susp
                                    if _i6_out.decision == "confirmed_present":
                                        # the effect LANDED — persist the confirmed
                                        # evidence + route to the CREDIT path with NO
                                        # re-submit. Flip result.success so the credit
                                        # seam runs; the S4 gate then credits on the
                                        # persisted confirmed bit (_external_ok).
                                        _persist_external_evidence(
                                            context, _i6_out.evidence)
                                        result.success = True
                                        logger.warning(
                                            "[Runtime IMPL-6] obj=%d ambiguous failure but "
                                            "read-back CONFIRMED the effect landed "
                                            "(idempotency key present) — crediting with NO "
                                            "re-submit.", getattr(_obj_i6, "id", 0))
                                    # confirmed_absent ⇒ fall through to today's
                                    # retry behavior (a retry is SAFE).
                        except Exception:
                            logger.debug(
                                "[Runtime IMPL-6] ambiguous-outcome hook errored — "
                                "falling through to today's retry behavior",
                                exc_info=True)

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
                        # R-A10 B9 (Fix C, ROOT-CAUSE): the credential-gate must hold at
                        # THIS live credit site too, not only the resume-recredit hook.
                        # An objective that (per _recredit_blocked_ids over the LIVE
                        # objective list — which after a fold carries the backchain
                        # precede + the original's updated depends_on) is gated on a
                        # still-MISSING runtime_error requirement — the precede itself,
                        # OR anything transitively depending on it — must NEVER be
                        # credited here. objective_verifier soft-passes verified=True for
                        # a verifier=None objective BEFORE any durable check, so without
                        # this guard a resume WITHOUT an operator credential could credit
                        # the precede then the original on succeeding tool calls and reach
                        # status="success" with the credential still absent. The ONLY
                        # sanctioned path that clears the gate is _apply_resume_fold_credit
                        # flipping the requirement missing→have. ADDITIVE + scoped:
                        # _recredit_blocked_ids is empty for any objective list with no
                        # missing runtime_error requirement → legacy/normal runs unchanged.
                        _blocked_ids = _recredit_blocked_ids(objectives)
                        if isinstance(completed_obj, int) and completed_obj in _blocked_ids:
                            logger.warning(
                                "[Runtime B9] live credit SKIPPED for obj=%d — blocked on a "
                                "missing runtime_error requirement (credential/decision gate); "
                                "only the operator-supplied credential path can clear it.",
                                completed_obj,
                            )
                            context.add_observation(
                                {
                                    "type": "objective_blocked_credential_gate",
                                    "objective_id": completed_obj,
                                    "message": (
                                        f"Objective {completed_obj} is blocked on a missing "
                                        f"credential/decision requirement (runtime_error); it "
                                        f"cannot be completed until that requirement is satisfied "
                                        f"by the operator — work the prerequisite objective."
                                    ),
                                },
                                current_ab,
                            )
                        elif isinstance(completed_obj, int) and completed_obj not in completed_objectives:
                            if result is not None and result.success:
                                # v0.9.1 (Layer 4): run the durable-outcome verifier before
                                # crediting.  Best-effort: verifier errors fall through to the
                                # legacy credit so a bad verifier config can't stall the run.
                                _do_credit = True
                                # S4 (fail-closed external-effect credit): resolve the
                                # Objective being credited and read whether it demands
                                # external ground-truth. For a requires_external_verification
                                # objective the LOCAL verifier below is ADVISORY-ONLY — the
                                # credit DECISION is the persisted ExternalEvidence.confirmed
                                # bit (_read_external_ok), computed BEFORE the legacy try so it
                                # is available in BOTH the try's conjunct and the except's
                                # fail-closed branch. Non-external → _needs_external False →
                                # ALL S4 logic is skipped and this site is byte-identical.
                                _s4_obj = next(
                                    (o for o in objectives if o.id == completed_obj), None)
                                _needs_external = bool(getattr(
                                    _s4_obj, "requires_external_verification", False))
                                # ── S3 / R-A7 wave-3a — run the DETERMINISTIC external
                                # verifier and POPULATE the store, BEFORE the S4 gate
                                # reads it. This is the ONLY thing S3 adds at the seam:
                                # it never touches the credit-decision code below (it
                                # only calls _persist_external_evidence). Guarded on
                                # _needs_external → a non-external objective is
                                # byte-identical. Deterministic-only (no LLM): the hook
                                # runs ExternalVerifier over the tool result + the
                                # pre-submit freshness snapshot; a confirmed match sets
                                # ExternalEvidence.confirmed, which _read_external_ok
                                # (below) then credits. Fail-closed on any error (the
                                # helper never raises). Skip re-verify when a confirmed
                                # bit already exists (e.g. seeded on resume) so we never
                                # re-fetch a proven effect.
                                if _needs_external and _s4_obj is not None:
                                    try:
                                        if not _read_external_ok(context, completed_obj):
                                            _presub = (
                                                getattr(self,
                                                        "_presubmit_external_snapshot", None)
                                                or {"presubmit_tokens": [],
                                                    "pre_submit_absent": False})
                                            _tool_for_ext = next(
                                                (t for t in tools
                                                 if getattr(t, "name", None)
                                                 == decision.get("tool_name")), None)
                                            _ev = _run_external_verification(
                                                self, objective=_s4_obj,
                                                decision=decision, tool=_tool_for_ext,
                                                result=result, presubmit=_presub)
                                            _persist_external_evidence(context, _ev)
                                    except Exception:
                                        logger.debug(
                                            "[Runtime S3] external verifier hook "
                                            "errored — fail-closed (no confirm)",
                                            exc_info=True)
                                    finally:
                                        # one-shot: consume the pre-submit snapshot.
                                        self._presubmit_external_snapshot = None
                                # ── R-A14a: decoupled per-actuation MCP obligation ──
                                # For a KNOWN-mutation MCP call, drive the mcp modality's
                                # capture_evidence (→ the EXISTING money-move-safe
                                # verify() + hardened api_readback) and PERSIST the receipt
                                # — regardless of SYSTEMU_S4_STAMP (the binder never stamps
                                # MCP tools). The credit is GATED on it ONLY for a
                                # MONEY-MOVE MCP mutation (fail-closed): a NON-money MCP
                                # mutation's receipt is NON-GATING (best-effort provenance),
                                # so it credits via the normal path whether or not
                                # verification confirms — no over-gating / no regression.
                                # Runs AFTER the binder hook above (guarded on the ORIGINAL
                                # _needs_external so it never runs for pure-MCP) so the
                                # modality's evidence is authoritative (no clobber). Fully
                                # guarded — never raises.
                                _mcp_money_gate = False
                                try:
                                    if _s4_obj is not None:
                                        _mcp_money_gate = _mcp_actuation_link(
                                            self, context, objective=_s4_obj,
                                            decision=decision, result=result)
                                except Exception:
                                    logger.debug("[Runtime R-A14a] mcp actuation link "
                                                 "guard errored — no-op", exc_info=True)
                                    _mcp_money_gate = False
                                # The effective obligation: the binder stamp OR the
                                # MONEY-MOVE MCP obligation only. A NON-money MCP mutation
                                # never sets this (its receipt is non-gating), and non-MCP +
                                # non-mutation is byte-identical (_mcp_money_gate stays False).
                                _needs_external_eff = _needs_external or _mcp_money_gate
                                _external_ok = (
                                    _read_external_ok(context, completed_obj)
                                    if _needs_external_eff else False)
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
                                    # S4: for an external-effect objective the legacy
                                    # "credit without verify" fall-through is UNSAFE — a
                                    # crash in the credit path (TLS/timeout on an
                                    # independent readback, a bad verifier) must NEVER
                                    # credit an unverified external effect. Fail CLOSED.
                                    # Non-external keeps the exact legacy except behavior
                                    # (_do_credit stays True). R-A14a: a MONEY-MOVE MCP
                                    # obligation (folded into _needs_external_eff) fails
                                    # closed here too; a NON-money MCP mutation is non-gating
                                    # (_needs_external_eff False) ⇒ legacy except behavior.
                                    if _needs_external_eff:
                                        _do_credit = False

                                # S4 (§5.8 — external evidence is AUTHORITATIVE): an
                                # external-effect objective credits ONLY on the persisted
                                # ExternalEvidence.confirmed bit (_external_ok). The local
                                # verifier's verdict (_do_credit above) is ADVISORY-ONLY —
                                # it judges LOCAL StateDelta and CANNOT see an external
                                # effect, so it does NOT gate the credit. Re-ground the
                                # decision on _external_ok alone (NOT a conjunct): a
                                # confirmed external bit CREDITS even on a local hard-reject
                                # (a false local reject on a confirmed money-move would
                                # otherwise block the credit and risk a double-submit on
                                # retry), and a soft-pass WITHOUT a confirmed bit still does
                                # NOT credit (_external_ok False). The local verifier still
                                # RAN above (its verifier_rejection observation is the
                                # advisory/audit trail). The except-branch above still fails
                                # CLOSED (_do_credit=False) for external. Any not-credited
                                # external objective emits an UNVERIFIED_EXTERNAL steering
                                # observation (mirrors the B9 credential-gate note). Guarded
                                # on _needs_external_eff → non-external + NON-money MCP + non-
                                # mutation is byte-identical (R-A14a folds ONLY the MONEY-MOVE
                                # MCP obligation into this authoritative-external-bit gate; a
                                # non-money MCP receipt is non-gating and credits normally).
                                if _needs_external_eff:
                                    _do_credit = _external_ok
                                    if not _do_credit:
                                        logger.warning(
                                            "[Runtime S4] external-effect obj=%d NOT credited "
                                            "— no confirmed independent evidence (local verifier "
                                            "is advisory-only for external effects).",
                                            completed_obj,
                                        )
                                        context.add_observation(
                                            {
                                                "type": "UNVERIFIED_EXTERNAL",
                                                "objective_id": completed_obj,
                                                "message": (
                                                    "external-effect objective not credited — "
                                                    "no confirmed independent evidence"
                                                ),
                                            },
                                            current_ab,
                                        )
                                        # ── R-A13 Stage-3a — operator_attest ENFORCE
                                        # fallback (behind the flag) ──
                                        # When the external effect could NOT be
                                        # independently confirmed AND no independent
                                        # readback channel was even available (no
                                        # readback_url in the envelope ⇒ independent
                                        # confirmation was IMPOSSIBLE, so attest is the
                                        # genuine FALLBACK, not a shortcut — the
                                        # anti-fatigue guardrail), AND the objective is
                                        # NOT a money-move (attestation can never credit a
                                        # money-move), surface an operator-attest card +
                                        # PARK. OFF/SHADOW never enter (the enforce
                                        # conjunct) so they are byte-identical; the
                                        # UNVERIFIED_EXTERNAL observation above is
                                        # UNCHANGED. Fully guarded — ANY error falls back
                                        # to the existing silent not-credit behavior.
                                        try:
                                            from systemu.runtime.requirement_binder import (
                                                _s4_stamp_mode as _s4_mode_a)
                                            if _s4_mode_a() == "enforce":
                                                _attest_tool = next(
                                                    (t for t in tools
                                                     if getattr(t, "name", None)
                                                     == decision.get("tool_name")), None)
                                                _ev_in_a = _external_from_result(result)
                                                _no_indep_channel = not bool(
                                                    _ev_in_a.get("readback_url")
                                                    if isinstance(_ev_in_a, dict) else None)
                                                if (_no_indep_channel
                                                        and not _is_money_move_seam(
                                                            _s4_obj, decision,
                                                            _attest_tool)):
                                                    _susp_attest = (
                                                        self._enqueue_operator_attest_and_suspend(
                                                            objective=_s4_obj,
                                                            execution_id=execution_id,
                                                            context=context,
                                                            result=result,
                                                            decision=decision,
                                                            tool=_attest_tool,
                                                            activity=activity,
                                                            shadow=shadow))
                                                    if _susp_attest is not None:
                                                        return _susp_attest
                                        except Exception:
                                            logger.debug(
                                                "[Runtime S4-ATTEST] enqueue guard "
                                                "errored — no-op (existing not-credit "
                                                "behavior)", exc_info=True)

                                # ── R-A13b-1: SHADOW park-surface METER (RECORD-ONLY) ──
                                # When the objective WOULD-stamp under the current S4
                                # stamp mode but the LIVE gate field was NOT written
                                # (SHADOW: _needs_external False + _s4_stamp_shadow True),
                                # run S3 evidence production and RECORD would-credit/
                                # would-park — but NEVER credit/card/suspend. Additive:
                                # OFF/ENFORCE never enter this branch (byte-identical);
                                # _do_credit is already finalized above and is untouched.
                                # Fully fail-safe (guarded end-to-end).
                                try:
                                    from systemu.runtime.requirement_binder import (
                                        _s4_stamp_mode as _s4_mode)
                                    if (not _needs_external_eff and _s4_obj is not None
                                            and _s4_mode() == "shadow"
                                            and bool(getattr(_s4_obj, "_s4_stamp_shadow", False))):
                                        _meter_tool = next(
                                            (t for t in tools if getattr(t, "name", None)
                                             == decision.get("tool_name")), None)
                                        _meter_presub = (
                                            getattr(self, "_presubmit_external_snapshot", None)
                                            or {"presubmit_tokens": [], "pre_submit_absent": False})
                                        _record_s4_shadow_meter(
                                            self, context, objective=_s4_obj, decision=decision,
                                            tool=_meter_tool, result=result,
                                            presubmit=_meter_presub)
                                except Exception:
                                    logger.debug("[Runtime S4-METER] meter guard errored — no-op",
                                                 exc_info=True)

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
                        #
                        # R-A10 B9 (Fix C, ROOT-CAUSE): the credential-gate must hold here
                        # too. _intent_goal_success knows NOTHING about the backchain
                        # precede, so a goal-level pass would otherwise finalize
                        # status="success" while a required credential/decision is still
                        # missing. If _recredit_blocked_ids over the LIVE objective list
                        # reports ANY objective still gated on a missing runtime_error
                        # requirement, the goal cannot be truly complete — SKIP the accept
                        # and fall through to the honest park path below. ADDITIVE +
                        # scoped: empty for any non-credential-gated run → unchanged.
                        _stuck_blocked_ids = _recredit_blocked_ids(objectives)
                        if _stuck_blocked_ids:
                            logger.info(
                                "[Runtime B9] stuck-park goal-accept SKIPPED — objective(s) %s "
                                "blocked on a missing runtime_error requirement (credential/"
                                "decision gate); parking honestly instead of finalizing success.",
                                sorted(_stuck_blocked_ids),
                            )
                        # S4 (fail-closed external-effect credit): the stuck-park
                        # goal-accept is a SEPARATE finalization route that also
                        # finalizes status="success" via _intent_goal_success, which
                        # (like the goal verifier) judges only LOCAL state and is BLIND
                        # to external effects. Mirror the B9 skip: a pending
                        # requires_external_verification objective with no persisted
                        # confirmed ExternalEvidence forces the goal-accept to be SKIPPED
                        # so the run parks/degrades to 'partial' honestly instead of
                        # crediting an unverified external effect. ADDITIVE + scoped:
                        # _stuck_ext_pending is empty for any run with no pending external
                        # objective → the non-external stuck-park is BYTE-IDENTICAL.
                        _stuck_ext_pending = {
                            _o.id for _o in objectives
                            if _o.id not in completed_objectives
                            and getattr(_o, "requires_external_verification", False)
                            and _read_external_ok(context, _o.id) is not True
                        }
                        if _stuck_ext_pending:
                            logger.warning(
                                "[Runtime S4] stuck-park goal-accept SKIPPED — external-"
                                "effect objective(s) %s pending without confirmed "
                                "independent evidence; parking honestly instead of "
                                "finalizing success (the goal probe is blind to external "
                                "effects).",
                                sorted(_stuck_ext_pending),
                            )
                            for _eid in sorted(_stuck_ext_pending):
                                context.add_observation(
                                    {
                                        "type": "UNVERIFIED_EXTERNAL",
                                        "objective_id": _eid,
                                        "message": (
                                            "stuck-park goal-accept skipped: external-effect "
                                            f"objective {_eid} has no confirmed independent "
                                            "evidence. Obtain confirmed evidence for the "
                                            "external objective before it can be credited."
                                        ),
                                    },
                                    current_ab,
                                )
                        if (not _stuck_blocked_ids
                                and not _stuck_ext_pending
                                and _intent_engine_enabled(self.config) and _adherence != "strict"
                                and _intent_goal_success(
                                vault=self.vault, config=self.config,
                                user_profile=getattr(self, "user_profile", None),
                                scroll=scroll, execution_id=execution_id,
                                summary=(decision.get("summary") if isinstance(decision, dict) else None))):
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
            # IMPL-7 / §5.6: a max-iterations partial is a HANDOFF — enumerate the
            # external effects already committed this run (deterministic, honest).
            _mi_summary = _augment_summary_with_committed_effects(" ".join(_parts), context)
            res = context.build_result(
                status="partial",
                final_summary=_mi_summary,
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
            # fold-in: persist this run's cost DURABLY before the ambient eid is
            # reset — so the per-run cost survives a daemon restart (the live ledger
            # is in-process; the ExecutionSnapshot cost is deleted on completion).
            # Best-effort; a no-op for a zero-LLM run.
            try:
                from systemu.runtime import costing as _costing
                from systemu.runtime.chat_submission_ctx import current_execution_id
                _costing.persist_run_cost(current_execution_id())
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
            tool=tool_obj,   # S1b: thread the Tool so the sandbox action gate can score it
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
