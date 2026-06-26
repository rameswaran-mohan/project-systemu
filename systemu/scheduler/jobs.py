"""Scheduled background jobs for Systemu.

  hourly_shadow_sweep        — re-evaluate unassigned activities
  daily_evolution_check      — run the evolution engine
  consolidate_shadow_memory  — fold JSONL buffer into SHADOW_MEMORY.md
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from systemu.core.utils import utcnow
# v0.6.8-c: hoist _dr import to module level so tests can monkeypatch
# jobs_mod._dr.dry_run_tool without triggering a fresh import each call.
from systemu.pipelines import tool_dry_run as _dr
from systemu.approval.exceptions import PendingOperatorDecision

logger = logging.getLogger(__name__)

# These are set by daemon.py before scheduling
_config    = None
_vault     = None
_scheduler = None   # APScheduler instance — set by daemon after start()

MAX_RECOVERY_ATTEMPTS = 2   # v0.8.13: re-extraction retries before terminating a scroll

# v0.8.7: schedules whose next_fire_at is older than this threshold at tick
# time are considered "missed" — they don't fire, the operator gets alerted.
SCHEDULE_MISSED_THRESHOLD_SECONDS = int(
    os.environ.get("SYSTEMU_SCHEDULE_MISSED_THRESHOLD_SECONDS", "300")
)


def init_jobs(config, vault) -> None:
    global _config, _vault
    _config = config
    _vault  = vault


def set_scheduler(scheduler) -> None:
    """Store the live APScheduler instance so the dashboard can query job info."""
    global _scheduler
    _scheduler = scheduler


def get_scheduler():
    """Return the live APScheduler instance (or None before daemon start)."""
    return _scheduler


def _resume_waiting_chat_entry(vault, activity_id: str) -> None:
    """v0.8.13: when a parked (waiting_on_tools) activity becomes runnable,
    flip its chat-history entry to running and wire a completion update."""
    try:
        for entry in vault.load_chat_history(limit=50):
            if entry.get("activity_id") == activity_id and entry.get("status") == "waiting_on_tools":
                ts = entry.get("ts")
                vault.update_chat_history_entry(ts, {"status": "running"})
                try:
                    from systemu.pipelines.direct_task import _wire_chat_history_completion
                    _wire_chat_history_completion(vault, ts, activity_id, "recovery-autorun")
                except Exception:
                    logger.debug("[Job] could not wire chat completion for %s", activity_id, exc_info=True)
                return
    except Exception:
        logger.debug("[Job] _resume_waiting_chat_entry failed for %s", activity_id, exc_info=True)


def reconcile_resolved_stuck_decisions(vault, supervisor, data_dir=None) -> int:
    """Cross-process safety net for v0.8.22.1 resume-after-decision.

    The EventBus subscriber registered by ``resume_on_decision.register``
    only fires for resolutions that happen IN the daemon process. The CLI
    command ``sharing_on decisions resolve`` runs in a separate process
    and its publish never reaches the daemon. Without this poll, a CLI
    resolution would mark the decision resolved but leave the chat task
    parked forever.

    Walks the decisions index, filters to resolved structured_question
    decisions that carry resume coords and haven't been dispatched yet
    (per the persisted ``decision.context["resume_dispatched"]`` flag),
    and calls ``resume_on_decision._dispatch_resume`` for each. The
    persisted marker ensures we never double-dispatch even if the
    EventBus subscriber already handled the same decision earlier.

    Returns the number of decisions actually re-dispatched.
    Best-effort: per-decision failures are logged and the loop continues.
    """
    from systemu.runtime import resume_on_decision as _rod

    try:
        headers = vault.load_index("decisions") or []
    except Exception:
        logger.debug("[ResumeReconciler] could not load decisions index", exc_info=True)
        return 0

    candidate_ids = [h["id"] for h in headers if h.get("status") == "resolved"]
    if not candidate_ids:
        return 0

    dispatched = 0
    for did in candidate_ids:
        try:
            decision = vault.get_decision(did)
        except Exception:
            logger.debug("[ResumeReconciler] could not load decision %s", did, exc_info=True)
            continue
        dctx = decision.context or {}
        # Cheap filters before touching the dispatcher
        if dctx.get("kind") != "structured_question":
            continue
        if dctx.get("resume_dispatched"):
            continue
        if not dctx.get("chat_submission_id"):
            continue
        if not (dctx.get("execution_id") and dctx.get("activity_id") and dctx.get("shadow_id")):
            continue
        try:
            if _rod._dispatch_resume(
                decision, vault=vault, supervisor=supervisor, data_dir=data_dir,
            ):
                dispatched += 1
        except Exception:
            logger.exception(
                "[ResumeReconciler] _dispatch_resume failed for decision %s", did,
            )

    if dispatched:
        logger.info(
            "[ResumeReconciler] re-dispatched %d resolved stuck-decision(s) "
            "via cross-process poll", dispatched,
        )
    return dispatched


def _resume_on_decision_reconciler_job() -> None:
    """APScheduler entry: thin wrapper around ``reconcile_resolved_stuck_decisions``
    using the daemon-initialised globals.  Pulls the live Supervisor on demand."""
    if _vault is None:
        return
    try:
        from systemu.runtime.supervisor import Supervisor
        supervisor = Supervisor.get()
    except Exception:
        # Supervisor not started yet (very early boot) — try again next tick.
        return
    try:
        from pathlib import Path
        reconcile_resolved_stuck_decisions(_vault, supervisor, data_dir=Path("data"))
    except Exception:
        logger.exception("[ResumeReconciler] job crashed")


def _map_grant_payload(harness_kind: str, materialise: dict) -> dict:
    """Map a Governor ``materialise()`` outcome dict → the per-kind
    ``grant_payload`` that shadow_runtime's ``_apply_harness_grant`` consumes.

    The operator's verdict is AUTHORITATIVE and already materialised here —
    the payload is the REPLAY instruction the resumed run applies verbatim
    (it never re-arbitrates, never re-calls the Governor). Keys are matched
    exactly to ``_apply_harness_grant`` (shadow_runtime.py):

      * TOOL     → reads ``tool_id`` / ``granted_tool`` (name) / ``lease_id``
      * COMPUTE  → reads ``compute_grant`` (dict) / ``lease_id``
      * SKILL    → reads ``skill`` / ``lease_id``
      * ACCESS   → reads ``access`` (advisory lease; no sandbox patch — D.2)
      * SUBAGENT → reads ``subagent``
      * MCP      → reads ``mcp`` (server block) / ``lease_id``; oauth_pending
                   forwards ``reason`` + ``authorize_url``
      * INPUT    → reads ``operator_answer`` (handled by the caller, not here)

    ``kind`` + ``granted`` are carried through for the helper's kind dispatch.
    """
    materialise = materialise or {}
    kind = (harness_kind or "").lower()
    payload: dict = {"kind": kind, "granted": True}
    if kind == "tool":
        payload["tool_id"] = materialise.get("tool_id")
        # _apply_harness_grant resolves the tool ref via tool_id first, then
        # the name — provide both (`tool` and `granted_tool` are aliases).
        tool_name = materialise.get("tool")
        payload["tool"] = tool_name
        payload["granted_tool"] = tool_name
        payload["lease_id"] = materialise.get("lease_id")
    elif kind == "compute":
        payload["compute_grant"] = materialise.get("compute_grant") or {}
        payload["lease_id"] = materialise.get("lease_id")
    elif kind == "skill":
        payload["skill"] = materialise.get("skill")
        payload["lease_id"] = materialise.get("lease_id")
    elif kind == "access":
        payload["access"] = materialise.get("access")
        # `apply` patch removed — advisory lease only, nothing consumed it
        # (Bug 5 / D.2).
        payload["lease_id"] = materialise.get("lease_id")
    elif kind == "subagent":
        payload["subagent"] = materialise.get("subagent")
        payload["lease_id"] = materialise.get("lease_id")
    elif kind == "mcp":
        # P3: carry the discovered-server block (or the oauth_pending handoff)
        # the resumed run replays into the live registry via registry_bridge.
        if materialise.get("mcp"):
            payload["mcp"] = materialise.get("mcp")
            payload["lease_id"] = materialise.get("lease_id")
        else:
            # not materialised (e.g. oauth_pending) — forward the honest reason
            # so the resumed run narrates the handoff rather than a phantom grant.
            payload["reason"] = materialise.get("reason")
            payload["authorize_url"] = materialise.get("authorize_url")
    # else: unknown/unmaterialised capability — kind+granted only; the helper
    # narrates generically via its no-key branch.
    return payload


def reconcile_resolved_harness_grants(*, vault, supervisor, data_dir=None) -> int:
    """Daemon-tick EXECUTOR for operator-resolved harness ESCALATE gates.

    ``resolve_gate`` deliberately keeps a harness decision QUEUED — it does NOT
    materialise the capability or resume the run.  THIS reconciler is the
    executor: for every decision that is

      * ``status == "resolved"``,
      * ``context.kind == "gate"`` and ``context.gate_type == "harness"``,
      * carries ``execution_id`` / ``activity_id`` / ``shadow_id`` resume coords,
      * and has NOT already been dispatched
        (per the persisted ``context["harness_grant_dispatched"]`` flag —
        DISTINCT from the stuck reconciler's ``resume_dispatched`` so the two
        reconcilers never interfere),

    it acts ONCE:

      * Deny / reject / skip → ``grant_payload = {"kind", "denied": True,
        "rationale"}`` (no Governor call); the resumed run proceeds with its
        fallback.
      * Approve / Edit spec → reconstruct a ``HarnessRequest`` from the gate
        context, build a forced-GRANT ``HarnessVerdict``, call
        ``Governor(config).materialise(...)`` exactly ONCE, then map the
        outcome → the per-kind ``grant_payload`` ``_apply_harness_grant``
        consumes.  INPUT carries the operator's free-text answer instead.
      * Call ``supervisor.resume_after_grant(execution_id=, activity_id=,
        shadow_id=, grant_payload=)`` — the snapshot-stamp inside that method
        is the second (cross-process) idempotency layer.
      * Stamp ``context["harness_grant_dispatched"] = True`` + ``save_decision``.

    Fully defensive: a per-row exception (a Governor/materialise failure, a
    bad context, a resume failure) is logged and that row is skipped WITHOUT
    stamping the flag (so a later tick can retry) and WITHOUT crashing the
    tick.  Returns the number of rows actually dispatched.
    """
    from systemu.core.models import (
        HarnessRequest,
        HarnessVerdict,
        HarnessKind,
        HarnessDecision,
        RiskBand,
    )

    try:
        headers = vault.load_index("decisions") or []
    except Exception:
        logger.debug("[HarnessGrantReconciler] could not load decisions index", exc_info=True)
        return 0

    candidate_ids = [h["id"] for h in headers if h.get("status") == "resolved"]
    if not candidate_ids:
        return 0

    # Acquire config the same way the stuck reconciler / wrapper does: prefer
    # the daemon-initialised module global, else build from env.
    config = _config
    if config is None:
        try:
            from sharing_on.config import Config
            config = Config.from_env()
        except Exception:
            logger.debug(
                "[HarnessGrantReconciler] Config.from_env() failed — "
                "proceeding with config=None (Governor tolerates it)",
                exc_info=True,
            )
            config = None

    # Resolve the Governor symbol honouring a test monkeypatch on this module.
    Governor = globals().get("Governor")
    if Governor is None:
        from systemu.runtime.governor import Governor as Governor  # noqa: F811

    dispatched = 0
    for did in candidate_ids:
        try:
            decision = vault.get_decision(did)
        except Exception:
            logger.debug("[HarnessGrantReconciler] could not load decision %s", did, exc_info=True)
            continue
        dctx = decision.context or {}

        # Cheap filters before any work.
        if dctx.get("kind") != "gate" or dctx.get("gate_type") != "harness":
            continue
        # P4: an mcp_oauth URL-mode follow-up is NOT a terminal harness grant —
        # it must never be materialised here nor stamp harness_grant_dispatched,
        # so the ORIGINAL escalation can still complete on its own gate.
        if dctx.get("follow_up") == "mcp_oauth":
            continue
        if dctx.get("harness_grant_dispatched"):
            continue
        execution_id = dctx.get("execution_id")
        activity_id = dctx.get("activity_id")
        shadow_id = dctx.get("shadow_id")
        if not (execution_id and activity_id and shadow_id):
            continue

        try:
            harness_kind = str(dctx.get("harness_kind") or "").lower()
            choice = str(decision.choice or "").strip().lower()

            if choice in {"deny", "reject", "skip"}:
                grant_payload = {
                    "kind": harness_kind,
                    "denied": True,
                    "rationale": (
                        dctx.get("verdict_rationale")
                        or dctx.get("rationale")
                        or "Operator denied the capability request."
                    ),
                }
            elif harness_kind == "input":
                # ASK_OPERATOR / elicitation — the operator's answer is the choice
                # itself, not a materialised capability.
                _req_schema = dctx.get("requested_schema") or {}
                _pending = dctx.get("pending_tool") or {}
                _is_param_sub = bool(dctx.get("param_substitution"))
                if _req_schema and _req_schema.get("properties"):
                    # v0.9.35 (P1) / v0.9.45: a structured elicitation OR a
                    # synthesized free-text schema. The choice is the form JSON;
                    # type-coerce per the schema. (A "Deny"/non-JSON choice is
                    # handled by the deny branch above / coerces to empty.)
                    import json as _json
                    from systemu.runtime.elicitation import param_answers_from_choice
                    try:
                        _raw = _json.loads(decision.choice or "{}")
                        if not isinstance(_raw, dict):
                            _raw = {}
                    except Exception:
                        _raw = {}
                    _coerced = param_answers_from_choice(_req_schema, _raw)
                    if _pending:
                        # missing-param: merge the typed answers + re-dispatch the
                        # original tool call (which re-validates).
                        grant_payload = {
                            "kind": "input", "param_answers": _coerced,
                            "requested_schema": _req_schema, "pending_tool": _pending,
                        }
                    elif _is_param_sub:
                        # scroll-parameter confirmation — substitute into context.
                        grant_payload = {
                            "kind": "input", "param_answers": _coerced,
                            "requested_schema": _req_schema, "param_substitution": True,
                        }
                    else:
                        # v0.9.45: free-text ASK_OPERATOR — inject the CLEAN typed
                        # VALUE (e.g. "42"), not the raw form JSON or a button label,
                        # so the agent gets a usable answer and does NOT re-ask.
                        _props = list((_req_schema.get("properties") or {}).keys())
                        _key = "answer" if "answer" in _props else (
                            _props[0] if _props else "answer")
                        grant_payload = {
                            "kind": "input",
                            "operator_answer": str(_coerced.get(_key, "") or ""),
                            "requested_schema": _req_schema,
                        }
                else:
                    grant_payload = {
                        "kind": "input",
                        "operator_answer": (
                            dctx.get("operator_answer")
                            or decision.choice
                            or ""
                        ),
                    }
            else:
                # Approve (optionally amended) → Governor.grant materialises ONCE.
                _amended = dctx.get("amended_spec")
                _spec = _amended or dctx.get("spec") or {}

                def _mk(spec):
                    return HarnessRequest(
                        request_id=dctx.get("request_id", "") or "",
                        kind=HarnessKind(harness_kind),
                        spec=spec or {},
                        rationale=dctx.get("rationale", "") or "",
                        fallback=dctx.get("fallback", "") or "",
                    )

                _request = _mk(_spec)
                _prior = _mk(dctx.get("spec")) if _amended else None
                _confirmed = bool((dctx.get("amend_band_escalation") or {}).get("confirmed"))
                _g = Governor(config).grant(
                    _request, context=dctx.get("arb_context") or {},
                    vault=vault, config=config, execution_id=execution_id,
                    prior_request=_prior, band_escalation_confirmed=_confirmed,
                )
                if _g.get("denied"):
                    grant_payload = {
                        "kind": harness_kind, "denied": True,
                        "rationale": _g.get("reason") or "amend rejected",
                        "amend_rejected": bool(_amended),
                    }
                else:
                    grant_payload = _map_grant_payload(harness_kind, _g.get("result") or {})

            supervisor.resume_after_grant(
                execution_id=execution_id,
                activity_id=activity_id,
                shadow_id=shadow_id,
                grant_payload=grant_payload,
                origin=dctx.get("origin"),
                chat_submission_id=dctx.get("chat_submission_id"),
            )

            # Stamp the persisted idempotency flag ONLY after a successful
            # dispatch — a failure above skips this row for a later retry.
            decision.context["harness_grant_dispatched"] = True
            vault.save_decision(decision)
            dispatched += 1
        except Exception:
            logger.exception(
                "[HarnessGrantReconciler] dispatch failed for decision %s "
                "(execution_id=%s) — skipping this row, will retry next tick",
                did, execution_id,
            )

    if dispatched:
        logger.info(
            "[HarnessGrantReconciler] materialised + resumed %d harness grant(s)",
            dispatched,
        )
    return dispatched


# P4 OAuth-pending timeout: a URL-mode handoff the operator never completes is
# abandoned after this many seconds (clean timeout ⇒ harness_grant_failed).
MCP_OAUTH_PENDING_TIMEOUT_SECONDS = int(
    os.environ.get("SYSTEMU_MCP_OAUTH_TIMEOUT_SECONDS", "1800")
)


def reconcile_resolved_mcp_oauth(*, vault, supervisor, data_dir=None) -> int:
    """Daemon-tick executor for URL-mode OAuth follow-up gates (P4).

    Distinct from ``reconcile_resolved_harness_grants``: this gate is a NESTED
    follow-up of an MCP connect escalation. It deliberately does NOT touch
    ``harness_grant_dispatched`` — the original escalation owns that flag and must
    still be able to complete. The run stays parked ASSIGNED while pending; this
    reconciler resumes it when the operator finishes (Approve) or abandons it
    (Deny / clean timeout ⇒ harness_grant_failed).

    Idempotency is keyed on its OWN flag ``context["mcp_oauth_dispatched"]`` so it
    never interferes with the two existing reconcilers.

    Acts ONCE per resolved row:
      * Approve → resume with a {"kind":"mcp","granted":True} payload (the SDK
        provider has already written the token to the 0600 store by now).
      * Deny / reject / skip → resume with {"kind":"mcp","denied":True,
        "rationale": "operator denied OAuth"} ⇒ run gets harness_grant_failed.
      * Pending past the timeout → resume with the same denied payload (clean
        timeout, also harness_grant_failed) and stamp the flag so it's not retried.

    Fully defensive: a per-row failure is logged and the row is skipped WITHOUT
    stamping the flag, for a later-tick retry. Returns rows actually dispatched.
    """
    from systemu.core.utils import utcnow
    from datetime import datetime, timedelta

    try:
        headers = vault.load_index("decisions") or []
    except Exception:
        logger.debug("[McpOAuthReconciler] could not load decisions index", exc_info=True)
        return 0

    candidate_ids = [h["id"] for h in headers if h.get("status") == "resolved"]
    if not candidate_ids:
        return 0

    dispatched = 0
    now = utcnow()
    for did in candidate_ids:
        try:
            decision = vault.get_decision(did)
        except Exception:
            logger.debug("[McpOAuthReconciler] could not load decision %s", did, exc_info=True)
            continue
        dctx = decision.context or {}

        if dctx.get("gate_type") != "mcp_oauth" and dctx.get("follow_up") != "mcp_oauth":
            continue
        if dctx.get("mcp_oauth_dispatched"):
            continue
        execution_id = dctx.get("execution_id")
        activity_id = dctx.get("activity_id")
        shadow_id = dctx.get("shadow_id")
        if not (execution_id and activity_id and shadow_id):
            continue

        try:
            choice = str(decision.choice or "").strip().lower()
            denied = choice in {"deny", "reject", "skip"}

            # Clean-timeout safety net (a card resolved but pending past the bound,
            # or never actioned but reaped to resolved by another sweep).
            if not denied:
                created_raw = dctx.get("created_at")
                if created_raw:
                    try:
                        created = datetime.fromisoformat(str(created_raw))
                        if (now - created) > timedelta(seconds=MCP_OAUTH_PENDING_TIMEOUT_SECONDS) \
                                and choice not in {"approve", "approved"}:
                            denied = True
                    except ValueError:
                        pass

            if denied:
                grant_payload = {
                    "kind": "mcp",
                    "denied": True,
                    "rationale": dctx.get("rationale") or "operator denied OAuth",
                }
            else:
                grant_payload = {"kind": "mcp", "granted": True,
                                 "server_id": dctx.get("server_id")}

            supervisor.resume_after_grant(
                execution_id=execution_id,
                activity_id=activity_id,
                shadow_id=shadow_id,
                grant_payload=grant_payload,
                origin=dctx.get("origin"),
                chat_submission_id=dctx.get("chat_submission_id"),
            )

            # Stamp OUR OWN flag — never harness_grant_dispatched.
            decision.context["mcp_oauth_dispatched"] = True
            vault.save_decision(decision)
            dispatched += 1
        except Exception:
            logger.exception(
                "[McpOAuthReconciler] dispatch failed for decision %s (execution_id=%s) "
                "— skipping, will retry next tick", did, execution_id)

    if dispatched:
        logger.info("[McpOAuthReconciler] resumed %d oauth follow-up run(s)", dispatched)
    return dispatched


def _mcp_oauth_reconciler_job() -> None:
    """APScheduler entry: thin wrapper around ``reconcile_resolved_mcp_oauth``
    using the daemon-initialised globals. Mirrors ``_harness_grant_reconciler_job``;
    never crashes the tick."""
    if _vault is None:
        return
    try:
        from systemu.runtime.supervisor import Supervisor
        supervisor = Supervisor.get()
    except Exception:
        return
    try:
        from pathlib import Path
        reconcile_resolved_mcp_oauth(vault=_vault, supervisor=supervisor,
                                     data_dir=Path("data"))
    except Exception:
        logger.exception("[McpOAuthReconciler] job crashed")


def _harness_grant_reconciler_job() -> None:
    """APScheduler entry: thin wrapper around ``reconcile_resolved_harness_grants``
    using the daemon-initialised globals.  Pulls the live Supervisor on demand.
    Mirrors ``_resume_on_decision_reconciler_job``; never crashes the tick."""
    if _vault is None:
        return
    try:
        from systemu.runtime.supervisor import Supervisor
        supervisor = Supervisor.get()
    except Exception:
        # Supervisor not started yet (very early boot) — retry next tick.
        return
    try:
        from pathlib import Path
        reconcile_resolved_harness_grants(
            vault=_vault, supervisor=supervisor, data_dir=Path("data"),
        )
    except Exception:
        logger.exception("[HarnessGrantReconciler] job crashed")


def reconcile_recovery_gates(*, vault, engine=None, inbox_cls=None, queue_cls=None) -> None:
    """Scan recovery diagnoses → keep the Inbox's recovery gates in sync.

    recovery has no persisted producer (diagnoses are on-demand scans), so this
    daemon job IS the producer: it enqueues a recovery gate for every currently
    diagnosed action (``InboxQueue.enqueue`` posts on a dedup so re-enqueue is
    idempotent) and expires any pending recovery gate whose action has
    self-healed (no longer in the scan), so a stale row can't be applied to a
    fixed entity.

    Params are injectable for unit tests; they default to the real
    ``RecoveryEngine`` / ``InboxQueue`` / ``OperatorDecisionQueue`` via lazy
    import. Fully defensive — every step is best-effort so a single failure can
    never crash the daemon tick (a scan failure logs + returns; per-action
    enqueue / expire failures are logged at debug and skipped)."""
    from systemu.recovery.engine import RecoveryEngine
    from systemu.interface.command.inbox import InboxQueue
    from systemu.interface.command.gate import GateDescriptor
    from systemu.approval.decision_queue import OperatorDecisionQueue
    engine = engine or RecoveryEngine(vault)
    inbox_cls = inbox_cls or InboxQueue
    queue_cls = queue_cls or OperatorDecisionQueue
    try:
        actions = engine.scan_all()
    except Exception:
        logger.exception("[Recovery] scan_all failed")
        return
    current = {
        f"recovery:{a.scope_kind}:{a.scope_id}:{a.kind}": a for a in actions
    }
    inbox = inbox_cls(vault)
    for a in current.values():
        try:
            inbox.enqueue(GateDescriptor.from_recovery_action(a), gate_type="recovery")
        except Exception:
            logger.debug("[Recovery] enqueue skipped", exc_info=True)
    q = queue_cls(vault)
    try:
        pending = q.list_pending()
    except Exception:
        logger.debug("[Recovery] list_pending failed", exc_info=True)
        return
    for d in pending:
        ctx = getattr(d, "context", {}) or {}
        if ctx.get("kind") == "gate" and ctx.get("gate_type") == "recovery":
            if getattr(d, "dedup_key", "") not in current:
                try:
                    q.expire_by_dedup_key(d.dedup_key)
                except Exception:
                    logger.debug("[Recovery] expire skipped", exc_info=True)


def _recovery_gate_reconciler_job() -> None:
    """APScheduler entry: thin wrapper around ``reconcile_recovery_gates`` using
    the daemon-initialised ``_vault`` global. Mirrors
    ``_resume_on_decision_reconciler_job``. Best-effort: never crashes the tick."""
    if _vault is None:
        return
    try:
        reconcile_recovery_gates(vault=_vault)
    except Exception:
        logger.exception("[Recovery] gate reconciler job crashed")


def startup_recovery_sweep() -> None:
    """Run once at daemon start: audit the vault for pipeline states left incomplete
    by a prior crash. Safe to call multiple times — every step is idempotent.

    Four passes in dependency order:
      1. APPROVED scrolls with no linked activity  → re-run extraction
      2. PARTIAL activities whose tools are all enabled → heal → decide_shadow
      3. UNASSIGNED activities with no shadow      → decide_shadow
      4. ASSIGNED activities whose shadow never ran → submit to Supervisor
    """
    if _config is None or _vault is None:
        logger.warning("[Job] startup_recovery_sweep called before init_jobs()")
        return

    from systemu.core.models import ActivityStatus, ScrollStatus
    from systemu.pipelines.shadow_decision import decide_shadow
    from systemu.interface.notifications import log_event

    # v0.8.13: the scheduler process must initialise the extraction pipeline
    # before re-extraction, or extract_and_process raises "not initialised".
    try:
        from systemu.pipelines.activity_extractor import init_pipeline
        init_pipeline(_config, _vault)
    except Exception:
        logger.exception("[Job] Recovery: init_pipeline failed — re-extraction may be skipped")

    logger.info("[Job] Startup recovery sweep — scanning vault for incomplete pipeline states ...")

    # ── Pass 1: APPROVED scrolls with no linked activity ─────────────────────
    # Indicates a crash during extract_and_process (before the activity was saved).
    for header in _vault.list_scrolls(status=ScrollStatus.APPROVED):
        if header.get("activity_id"):
            continue
        scroll_id   = header["id"]
        scroll_name = header.get("name", scroll_id)

        scroll = _vault.get_scroll(scroll_id)
        if getattr(scroll, "recovery_attempts", 0) >= MAX_RECOVERY_ATTEMPTS:
            scroll.status = ScrollStatus.EXTRACTION_FAILED
            _vault.save_scroll(scroll)
            logger.warning("[Job] Recovery: scroll '%s' exceeded %d attempts — marked EXTRACTION_FAILED",
                           scroll_name, MAX_RECOVERY_ATTEMPTS)
            log_event("WARNING", "scroll",
                      f"Scroll '{scroll_name}' extraction repeatedly failed — needs attention.",
                      {"scroll_id": scroll_id})
            continue

        scroll.recovery_attempts = getattr(scroll, "recovery_attempts", 0) + 1
        _vault.save_scroll(scroll)
        logger.info("[Job] Recovery: scroll '%s' APPROVED but no activity — re-extracting (attempt %d/%d)",
                    scroll_name, scroll.recovery_attempts, MAX_RECOVERY_ATTEMPTS)
        log_event("WARNING", "scroll",
                  f"Scroll '{scroll_name}' was approved but extraction never completed — re-running.",
                  {"scroll_id": scroll_id})
        try:
            from systemu.pipelines.scroll_refiner import approve_pending_scroll
            scroll = _vault.get_scroll(scroll_id)
            scroll.status = ScrollStatus.PENDING_APPROVAL
            _vault.save_scroll(scroll)
            approve_pending_scroll(scroll_id, _vault)
        except Exception as exc:
            logger.warning("[Job] Recovery re-extraction failed for scroll %s: %s", scroll_id, exc)

    # ── Pass 2: PARTIAL activities whose required tools are all now enabled ───
    # Indicates a crash between _toggle_enabled saving the tool and healing the activity.
    for header in _vault.list_activities(status=ActivityStatus.PARTIAL):
        try:
            activity = _vault.get_activity(header["id"])
            if not activity.required_tool_ids:
                continue
            all_ready = all(
                _vault.get_tool(tid).enabled
                for tid in activity.required_tool_ids
            )
            if not all_ready:
                continue
            activity.status       = ActivityStatus.UNASSIGNED
            activity.missing_tools = []
            _vault.save_activity(activity)
            logger.info("[Job] Recovery: healed PARTIAL activity '%s' → UNASSIGNED", activity.name)
            _resume_waiting_chat_entry(_vault, activity.id)
            decide_shadow(activity, _config, _vault)
        except Exception as exc:
            logger.warning("[Job] Recovery PARTIAL heal failed for %s: %s", header["id"], exc)

    # ── Pass 3: UNASSIGNED activities ─────────────────────────────────────────
    # Indicates a crash during decide_shadow / create_shadow.
    # decide_shadow's idempotency guard prevents duplicate shadows.
    for header in _vault.list_activities(status=ActivityStatus.UNASSIGNED):
        try:
            activity = _vault.get_activity(header["id"])
            decide_shadow(activity, _config, _vault)
        except Exception as exc:
            logger.warning("[Job] Recovery UNASSIGNED sweep failed for %s: %s", header["id"], exc)

    # ── Pass 4: ASSIGNED activities whose shadow never ran ────────────────────
    # Covers the gap where shadow assignment completed but Supervisor.submit()
    # was never called (e.g. prior daemon run without auto-submit, or a crash
    # between save_activity and submit).  Only shadows with empty execution_log
    # are re-submitted — shadows that have run at least once are left alone.
    _resubmit_unexecuted_assigned(_vault)

    # ── Pass 5: Dependency audit — advisory scan of deployed+enabled tools ───────
    # Uses find_spec() as a best-effort check only (pip name ≠ import name is
    # a known limitation — e.g. beautifulsoup4 → bs4).  Never blocks anything.
    # Batches all at-risk tools into ONE notification, deduped by tool-id set.
    _startup_dep_audit(_vault)

    # ── Pass 6 (v0.6.1-d): backfill tool-header schema summaries ────────────────
    # New _tool_header carries parameters_schema_summary + return_schema_summary
    # so the catalog builders (scroll_validator, activity_extractor) don't N+1
    # vault.get_tool().  Existing on-disk index entries from before v0.6.1 are
    # missing these — re-save each tool once to rewrite the header.  Idempotent
    # (no-op once every header has the new keys).
    _backfill_tool_headers_v061(_vault)

    # ── Pass 7 (v0.6.5-f, updated v0.7.4): dry-run any pending FORGED or
    # PROPOSED tools that haven't been validated yet.  v0.7.4 removed the
    # `enabled=True` filter — the new reconciler advances tools through
    # the lifecycle independent of operator enable-intent. Failures
    # auto-disable + emit operator card. Closes the "web_screenshot tool
    # failed at runtime" finding from the 2026-05-17 weather E2E.
    try:
        dry_run_all_pending_tools(_vault, _config)
    except Exception:
        logger.exception("[Job] v0.6.5-f tool dry-run sweep failed")

    logger.info("[Job] Startup recovery sweep complete.")


def _find_pending_dry_run_via_index(headers):
    """Return tool index entries whose dry-run hasn't been validated yet.

    v0.7.4: previously filtered on `enabled=True`. Removed — the
    reconciler advances FORGED tools to DEPLOYED regardless of operator
    enable state, because `enabled` is operator-intent (do I want this
    available to shadows?) not lifecycle-state (has this been validated?).
    """
    return [
        h for h in headers
        if h.get("dry_run_status") in (None, "not_run")
        # status=None covers legacy index entries written before `status` was a required field
        and h.get("status") in ("forged", "proposed", None)
    ]


def dry_run_all_pending_tools(vault, config, *, max_concurrent: int = 5) -> None:
    """v0.6.5-f / v0.6.8-c: one-shot startup sweep.

    For each FORGED/proposed tool with ``dry_run_status in {None, 'not_run'}``,
    dispatch the v0.5.0-a dry-run pipeline.  Bounded by ``max_concurrent``
    (default 5).  Each dry-run is capped at 30s by the existing sandbox.

    v0.7.4: ``enabled`` is no longer part of the pending filter — see
    ``_find_pending_dry_run_via_index`` for rationale.

    v0.6.8-c: this sweep is now NON-DESTRUCTIVE.  Failures (whether a
    returned ``success=False`` result or a raised exception like an
    uncaught ImportError) record ``dry_run_status='failed'`` plus a
    classified evidence dict on the tool, but never set ``enabled=False``.
    Operators recover via /recover/tool/<id>.  This generalises the
    v0.6.5-i hotfix (which only kept the tool enabled when the failure
    string matched ``"treating all packages as pending"``) to ANY failure.
    """
    from concurrent.futures import ThreadPoolExecutor

    # v0.6.8-c: prefer the dedicated vault helper if it exists (mockable in
    # unit tests).  Fall back to scanning load_index("tools") so existing
    # vault implementations without the helper still work.
    pending = None
    finder = getattr(vault, "find_tools_pending_dry_run", None)
    if callable(finder):
        try:
            pending = list(finder() or [])
        except Exception:
            logger.debug(
                "[Job] v0.6.8-c: find_tools_pending_dry_run() raised — falling back to index scan",
                exc_info=True,
            )
            pending = None
    if pending is None:
        pending = _find_pending_dry_run_via_index(vault.load_index("tools") or [])
    if not pending:
        logger.debug("[Job] v0.6.5-f: no tools pending dry-run")
        return

    logger.info(
        "[Job] v0.6.5-f: dry-running %d tools (max %d concurrent)",
        len(pending), max_concurrent,
    )

    def _resolve_tool(item):
        """Accept either an index header dict or an already-loaded Tool/MagicMock."""
        if isinstance(item, dict):
            try:
                return vault.get_tool(item["id"])
            except Exception:
                logger.exception("[Job] v0.6.5-f: get_tool failed for %s", item.get("id"))
                return None
        return item

    def _record_failure(tool, error_text: str) -> None:
        """Populate dry_run_evidence + status without disabling the tool."""
        from systemu.recovery.classifier import classify_dry_run_error
        from systemu.recovery.links import recover_url

        classified = classify_dry_run_error(
            error_text,
            missing_packages=(tool.dry_run_evidence or {}).get("missing_packages")
            if isinstance(getattr(tool, "dry_run_evidence", None), dict) else None,
        )
        evidence = {
            "error": error_text,
            "classified_reason": classified.kind,
            "missing_package": classified.missing_package,
            "timestamp": datetime.utcnow().isoformat(),
        }
        tool.dry_run_status = "failed"
        tool.dry_run_evidence = evidence
        # v0.6.8-c: NEVER set tool.enabled = False here.  Operators recover
        # via the dashboard recovery panel.
        try:
            vault.save_tool(tool)
        except Exception:
            logger.debug(
                "[Job] v0.6.8-c: save_tool failed for %s", getattr(tool, "id", "?"),
                exc_info=True,
            )
        try:
            link = recover_url("tool", getattr(tool, "id", ""))
        except Exception:
            link = "/recover/tool/<id>"
        logger.warning(
            "[Job] v0.6.8-c: tool %s dry-run failed (%s) — left ENABLED; recover at %s",
            getattr(tool, "name", "?"), classified.kind, link,
        )

    def _record_success(tool) -> None:
        tool.dry_run_status = "passed"
        try:
            tool.dry_run_evidence = {}
        except Exception:
            pass
        try:
            vault.save_tool(tool)
        except Exception:
            logger.debug(
                "[Job] v0.6.8-c: save_tool failed for %s", getattr(tool, "id", "?"),
                exc_info=True,
            )

    def _run_one(item):
        tool = _resolve_tool(item)
        if tool is None:
            return
        try:
            # v0.6.5-i hotfix: dry_run_tool's signature is (tool, *, vault, config).
            result = _dr.dry_run_tool(tool, vault=vault, config=config)
        except Exception as exc:
            # v0.6.8-c: an UNCAUGHT exception from the dry-run pipeline (e.g.
            # a downstream ImportError on a missing dep) is still a failure
            # signal — record it but keep the tool enabled.
            error_text = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[Job] v0.6.8-c: dry-run for %s raised %s — recording as failed (tool stays enabled)",
                getattr(tool, "name", "?"), error_text,
            )
            _record_failure(tool, error_text)
            return

        # v0.6.8-c: tolerate fake/stub returns (None, plain dict, etc.) — only
        # treat an explicit success=False as a failure.  If the stub returns
        # None we assume success (used by tests).
        if result is None:
            _record_success(tool)
            return

        success = bool(getattr(result, "success", True))
        if success:
            _record_success(tool)
            return

        err_str = (getattr(result, "error", None) or "").lower()
        evidence = getattr(result, "evidence", None) or {}
        ev_str = str(evidence).lower()
        is_dep_pending = (
            "treating all packages as pending" in err_str
            or ("pending" in ev_str and "install" in ev_str)
            or getattr(result, "status", "") == "skipped"
        )
        error_text = getattr(result, "error", None) or "(no error detail)"
        _record_failure(tool, error_text)
        if is_dep_pending:
            logger.info(
                "[Job] v0.6.5-i: tool %s dry-run pending dep approval — "
                "leaving enabled (operator must approve deps via /tools)",
                getattr(tool, "name", "?"),
            )

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        list(ex.map(_run_one, pending))


def dry_run_one_tool(tool_id: str) -> None:
    """v0.6.8-e: Re-run dry-run for ONE tool (used after a dep is approved).

    v0.6.9: lazy-init vault + config from env when called outside the
    daemon (CLI, dashboard, tests). Silent no-op if neither init_jobs()
    has run nor SYSTEMU_DATABASE_URL is set.
    """
    vault = _vault
    config = _config
    if vault is None or config is None:
        import os as _os
        from sharing_on.config import Config
        from systemu.storage.sqlite.vault import SqliteVault
        db_url = _os.environ.get("SYSTEMU_DATABASE_URL")
        if not db_url:
            logger.warning(
                "[Job] dry_run_one_tool: no vault available and "
                "SYSTEMU_DATABASE_URL unset — silently skipping"
            )
            return
        if vault is None:
            vault = SqliteVault(database_url=db_url)
        if config is None:
            config = Config.from_env()

    try:
        tool = vault.get_tool(tool_id)
    except (KeyError, AttributeError):
        logger.debug("[Job] dry_run_one_tool: tool %s not found", tool_id)
        return
    if tool is None:
        return

    try:
        result = _dr.dry_run_tool(tool, vault=vault, config=config)
    except Exception as exc:
        from systemu.recovery.classifier import classify_dry_run_error
        error_text = f"{type(exc).__name__}: {exc}"
        classified = classify_dry_run_error(
            error_text,
            missing_packages=(tool.dry_run_evidence or {}).get("missing_packages")
            if isinstance(getattr(tool, "dry_run_evidence", None), dict) else None,
        )
        tool.dry_run_status = "failed"
        tool.dry_run_evidence = {
            "error": error_text,
            "classified_reason": classified.kind,
            "missing_package": classified.missing_package,
            "timestamp": datetime.utcnow().isoformat(),
        }
        vault.save_tool(tool)
        # v0.9.48 Phase 3: a fresh dry-run that failed must auto-disable a
        # tool that was already DEPLOYED+enabled, so it can't stay callable.
        try:
            from systemu.pipelines.tool_service import disable_if_dry_run_failed
            disable_if_dry_run_failed(tool.id, vault)
        except Exception:
            logger.debug("[Job] disable_if_dry_run_failed failed for %s", tool.id, exc_info=True)
        return

    success = bool(getattr(result, "success", True)) if result is not None else True
    if success:
        tool.dry_run_status = "passed"
        tool.dry_run_evidence = {}
    else:
        from systemu.recovery.classifier import classify_dry_run_error
        error_text = getattr(result, "error", None) or "(no error detail)"
        classified = classify_dry_run_error(
            error_text,
            missing_packages=(tool.dry_run_evidence or {}).get("missing_packages")
            if isinstance(getattr(tool, "dry_run_evidence", None), dict) else None,
        )
        tool.dry_run_status = "failed"
        tool.dry_run_evidence = {
            "error": error_text,
            "classified_reason": classified.kind,
            "missing_package": classified.missing_package,
            "timestamp": datetime.utcnow().isoformat(),
        }
    vault.save_tool(tool)
    # v0.9.48 Phase 3: auto-disable on a fresh `failed` dry-run (no-op otherwise).
    if tool.dry_run_status == "failed":
        try:
            from systemu.pipelines.tool_service import disable_if_dry_run_failed
            disable_if_dry_run_failed(tool.id, vault)
        except Exception:
            logger.debug("[Job] disable_if_dry_run_failed failed for %s", tool.id, exc_info=True)


def _emit_dry_run_fail_card(tool, error) -> None:
    """v0.6.5-f: surface a 'tool dry-run failed' operator card via the
    existing v0.3.6 supervisor-flash bus."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        from systemu.interface.event_bus import EventBus
        EventBus.get().publish({
            "ts": _dt.now(tz=_tz.utc).isoformat(timespec="seconds"),
            "level": "WARNING",
            "category": "approval",
            "message": f"Tool '{getattr(tool, 'name', '?')}' failed dry-run — auto-disabled",
            "context": {
                "approval_message": (
                    f"Tool {getattr(tool, 'name', '?')} failed startup dry-run:\n\n"
                    f"{error or '(no error detail)'}\n\n"
                    f"Auto-disabled.  Re-enable on /tools after fixing the underlying issue."
                ),
                "redirect_to": "/tools",
                "dedup_key":   f"tool-dry-run-fail:{getattr(tool, 'id', '?')}",
                "tool_id":     getattr(tool, "id", None),
            },
        })
    except Exception:
        logger.debug("[Job] v0.6.5-f: could not emit dry-run-fail card", exc_info=True)


def _resubmit_unexecuted_assigned(vault) -> None:
    """Submit ASSIGNED and EXECUTABLE activities whose shadow has never executed.

    Covers two status values:
      ASSIGNED   — shadow created and linked, waiting to run
      EXECUTABLE — all required tools now deployed (subset of ASSIGNED semantics)

    Safe to call multiple times: Supervisor.submit() checks both _pending_activity_ids
    and _running, so duplicate submissions are silently dropped. Shadows whose
    execution_log is non-empty have already run (or are retrying via the Supervisor
    internally) and are skipped here.
    """
    from systemu.core.models import ActivityStatus

    candidates: list = []
    for status in (ActivityStatus.ASSIGNED, ActivityStatus.EXECUTABLE):
        candidates.extend(vault.list_activities(status=status))

    if not candidates:
        return

    try:
        from systemu.runtime.supervisor import Supervisor
        supervisor = Supervisor.get()
    except RuntimeError:
        logger.debug("[Job] Supervisor not running — skipping re-submission of assigned activities")
        return

    submitted = 0
    for header in candidates:
        try:
            activity = vault.get_activity(header["id"])
            if not activity.assigned_shadow_id:
                continue
            shadow = vault.get_shadow(activity.assigned_shadow_id)
            if shadow.execution_log:
                # Shadow has run before — leave it alone (completed / failed / retrying
                # via Supervisor's internal MAX_RETRIES mechanism)
                continue
            supervisor.submit(
                activity.id,
                shadow.id,
                reason="startup_recovery_assigned",
                # v0.8.16: preserve the activity's true trigger origin across a
                # recovery re-submit so a recovered chat/capture task partitions
                # into its real pane.  When the activity has no origin recorded,
                # coerce_origin(reason) falls back to "system" (recovery noise).
                origin=getattr(activity, "origin", None),
            )
            submitted += 1
            logger.info(
                "[Job] Recovery: re-submitted %s activity '%s' → shadow '%s'",
                header.get("status", "assigned"), activity.name, shadow.name,
            )
        except Exception as exc:
            logger.warning("[Job] Recovery re-submit failed for %s: %s", header.get("id"), exc)

    if submitted:
        logger.info("[Job] Recovery: submitted %d previously-stuck activity/activities", submitted)


def _backfill_tool_headers_v061(vault) -> None:
    """v0.6.1-d: re-save every tool to rewrite its index header with the new
    schema-summary fields (parameters_schema_summary + return_schema_summary).

    Idempotent — running on a vault that already has the new headers is a
    no-op (the early-return guard checks for the new key on at least one
    header).  Failures per-tool are best-effort logged; one bad tool does
    not block the rest of the sweep.
    """
    try:
        tools_index = vault.load_index("tools") or []
        if not tools_index:
            return
        # Only re-save when at least one header is missing the new key
        if all("parameters_schema_summary" in t for t in tools_index):
            return
        count = 0
        for header in tools_index:
            try:
                full = vault.get_tool(header["id"])
                vault.save_tool(full)
                count += 1
            except Exception:
                logger.debug(
                    "[Job] header backfill failed for tool %s",
                    header.get("id"), exc_info=True,
                )
        if count:
            logger.info(
                "[Job] v0.6.1-d: backfilled %d tool header(s) with schema summaries",
                count,
            )
    except Exception:
        logger.debug("[Job] header backfill sweep skipped", exc_info=True)


def _startup_dep_audit(vault) -> None:
    """Advisory dep audit: collect deployed+enabled tools with declared
    dependencies that look potentially missing, then queue a single batched
    notification.  Pure advisory — never alters tool or activity state."""
    import importlib.util

    from systemu.core.models import Notification, ToolStatus
    from systemu.core.utils import generate_id

    try:
        deployed_headers = vault.list_tools(status=ToolStatus.DEPLOYED)
    except Exception as exc:
        logger.warning("[Job] Dep audit: could not list tools — %s", exc)
        return

    at_risk: list[dict] = []   # {"tool_id", "tool_name", "missing_hints"}
    for header in deployed_headers:
        if not header.get("enabled"):
            continue
        tool_id   = header.get("id", "")
        tool_name = header.get("name", tool_id)
        deps      = header.get("dependencies") or []
        if not deps:
            continue

        missing_hints = []
        for dep in deps:
            # find_spec() uses import name; advisory only — false negatives possible
            try:
                spec = importlib.util.find_spec(dep)
                if spec is None:
                    missing_hints.append(dep)
            except (ModuleNotFoundError, ValueError):
                missing_hints.append(dep)

        if missing_hints:
            at_risk.append({
                "tool_id":      tool_id,
                "tool_name":    tool_name,
                "missing_hints": missing_hints,
            })

    if not at_risk:
        return

    # Dedup: skip if a dep_approval notification covering these exact tool IDs exists
    at_risk_ids = sorted(item["tool_id"] for item in at_risk)
    try:
        pending = vault.list_pending_notifications()
        for n in pending:
            ctx = n.get("context", {})
            if (ctx.get("notification_type") == "dep_approval"
                    and sorted(ctx.get("tool_ids", [])) == at_risk_ids):
                logger.debug("[Job] Dep audit: suppressed duplicate notification")
                return
    except Exception:
        pass

    # v0.8.13: map each missing package -> the (first) tool_id that needs it,
    # so the notification's "Install <pkg>" actions can route to approve_and_install.
    pkg_tool_map = {}
    for item in at_risk:
        for pkg in item["missing_hints"]:
            pkg_tool_map.setdefault(pkg, item["tool_id"])

    # Build a single batched message
    lines = ["The following enabled tools have declared Python dependencies that"]
    lines.append("may not be installed (advisory — false positives are possible):\n")
    install_cmds = []
    for item in at_risk:
        hints = ", ".join(item["missing_hints"])
        lines.append(f"  • {item['tool_name']}: {hints}")
        install_cmds.extend(item["missing_hints"])

    unique_cmds = list(dict.fromkeys(install_cmds))   # preserve order, dedup
    lines.append(f"\nTo install: pip install {' '.join(unique_cmds)}")
    lines.append("\nIf a package is already installed under a different import name")
    lines.append("(e.g. beautifulsoup4 → bs4) you can ignore this reminder.")
    lines.append("Real failures will be reported in the Event Log with exact install hints.")

    try:
        notif = Notification(
            id=generate_id("notif"),
            title=f"Dependency check: {len(at_risk)} tool(s) may need packages installed",
            message="\n".join(lines),
            # v0.8.13: actionable — one "Install <pkg>" per missing package + Dismiss.
            actions=[f"Install {pkg}" for pkg in unique_cmds] + ["Dismiss"],
            context={
                "notification_type": "dep_approval",
                "tool_ids":          at_risk_ids,
                "pkg_tool_map":      pkg_tool_map,
            },
        )
        vault.queue_notification(notif)
        logger.info(
            "[Job] Dep audit: queued advisory notification for %d tool(s): %s",
            len(at_risk),
            [item["tool_name"] for item in at_risk],
        )
    except Exception as exc:
        logger.warning("[Job] Dep audit: failed to queue notification — %s", exc)


def hourly_shadow_sweep() -> None:
    """Supplementary: re-evaluate unassigned activities and re-submit assigned ones.

    Three passes:
      1. PARTIAL activities whose tools are now enabled → heal → decide_shadow
      2. UNASSIGNED activities → decide_shadow (assign or create shadow)
      3. ASSIGNED/EXECUTABLE activities never executed → re-submit to Supervisor

    Pass 3 acts as a belt-and-suspenders backstop for the (rare but real) case
    where shadow assignment happened but Supervisor.submit() was never called,
    or the activity was assigned between daemon restarts and missed the startup
    recovery sweep.  Supervisor.submit() deduplicates, so this is safe to call
    even for activities that are already pending or running.
    """
    if _config is None or _vault is None:
        logger.warning("[Job] hourly_shadow_sweep called before init_jobs()")
        return

    from systemu.core.models import ActivityStatus
    from systemu.pipelines.shadow_decision import decide_shadow

    # ── Pass 1: PARTIAL activities whose tools are all enabled ────────────────
    healed = 0
    for header in _vault.list_activities(status=ActivityStatus.PARTIAL):
        try:
            activity = _vault.get_activity(header["id"])
            if not activity.required_tool_ids:
                continue
            all_ready = all(
                _vault.get_tool(tid).enabled
                for tid in activity.required_tool_ids
            )
            if not all_ready:
                continue
            activity.status        = ActivityStatus.UNASSIGNED
            activity.missing_tools = []
            _vault.save_activity(activity)
            healed += 1
            decide_shadow(activity, _config, _vault)
        except PendingOperatorDecision as pd:
            # v0.8.0 Pattern 1: queue-mode raised — the decision is persisted
            # in the queue, operator will resolve via dashboard. Log INFO not
            # WARNING so monitoring doesn't fire false alerts.
            logger.info(
                "[Job] Hourly heal: activity %s awaiting operator decision %s "
                "(dedup_key=%s) — will retry next sweep.",
                header["id"], pd.decision_id, pd.dedup_key,
            )
        except Exception as exc:
            logger.warning("[Job] Hourly heal error for activity %s: %s", header["id"], exc)

    # ── Pass 2: UNASSIGNED activities ─────────────────────────────────────────
    unassigned = _vault.list_activities(status=ActivityStatus.UNASSIGNED)
    if unassigned:
        logger.info("[Job] Hourly sweep: healed=%d unassigned=%d", healed, len(unassigned))
        for header in unassigned:
            try:
                activity = _vault.get_activity(header["id"])
                decide_shadow(activity, _config, _vault)
            except PendingOperatorDecision as pd:
                # v0.8.0 Pattern 1: queue-mode raised — see Pass 1 above.
                logger.info(
                    "[Job] Sweep: activity %s awaiting operator decision %s "
                    "(dedup_key=%s) — will retry next sweep.",
                    header["id"], pd.decision_id, pd.dedup_key,
                )
            except Exception as exc:
                logger.warning("[Job] Sweep error for activity %s: %s", header["id"], exc)

    # ── Pass 3: ASSIGNED/EXECUTABLE activities never executed ─────────────────
    _resubmit_unexecuted_assigned(_vault)

    if not unassigned and not healed:
        logger.info("[Job] Hourly sweep: nothing to do.")


def daily_evolution_check() -> None:
    """Run the evolution engine — propose improvements to vault entities."""
    if _config is None or _vault is None:
        logger.warning("[Job] daily_evolution_check called before init_jobs()")
        return

    from systemu.pipelines.evolution_engine import run_evolution_check
    try:
        proposals = run_evolution_check(_config, _vault)
        logger.info("[Job] Evolution check complete — %d proposals.", len(proposals))
    except Exception as exc:
        logger.error("[Job] Evolution check failed: %s", exc)


# Tunables for the consolidation job (also read by the dashboard page)
BUFFER_THRESHOLD        = 10       # entries → triggers consolidation (cron + refinery auto-trigger)
STALE_AFTER_DAYS        = 7        # days since last consolidation → trigger anyway
_GRADUATION_CONF        = 5        # confidence required to propose a heuristic as a skill
_GRADUATION_MIN_SCROLLS = 3        # distinct evidence scrolls required for graduation

# Back-compat aliases (old internal names)
_BUFFER_THRESHOLD = BUFFER_THRESHOLD
_STALE_AFTER_DAYS = STALE_AFTER_DAYS


def consolidate_shadow_memory() -> None:
    """Scheduler entry-point: fold buffered lessons into SHADOW_MEMORY.md.

    Delegates to run_consolidation_for_all() using the daemon-initialised
    globals.  The dashboard's "Run All Now" button calls run_consolidation_for_all()
    directly with explicit config/vault so it doesn't depend on globals.
    """
    if _config is None or _vault is None:
        logger.warning("[Job] consolidate_shadow_memory called before init_jobs()")
        return
    run_consolidation_for_all(_config, _vault)


def curator_review_job() -> None:
    """v0.9.6 L7 — inactivity-triggered curator review (idle-curator pattern).

    Registered in the daemon on a frequent interval (hourly), but the heavy
    lifecycle pass only fires when ``curator.should_run()`` says the
    configured interval (default 168h / weekly) has elapsed AND the curator
    is enabled + not paused.  This is the *idle-triggered* lifecycle review —
    distinct in TRIGGER from ``consolidate_shadow_memory`` (which runs
    unconditionally daily at 02:00).

    The pass action reuses the proven ``run_consolidation_for_all`` machinery
    (memory consolidation + skill-graduation) — genuine skill-lifecycle work.
    Richer pin/archive lifecycle (the forked review agent) remains future work
    (Task 5); when it lands it slots in here ahead of mark_run_complete.

    Wrapped end-to-end so a curator failure can NEVER crash the scheduler.
    """
    from systemu.runtime import curator
    if _config is None or _vault is None:
        logger.warning("[Job] curator_review_job called before init_jobs()")
        return
    vault_root = getattr(_config, "vault_dir", None)
    if not vault_root:
        return
    try:
        if not curator.should_run(vault_root, _config):
            return
    except Exception as exc:  # never let the gate crash the tick
        logger.warning("[Curator] should_run() check failed (non-fatal): %s", exc)
        return
    import time as _time
    _t0 = _time.time()
    try:
        count = run_consolidation_for_all(_config, _vault)
    except Exception as exc:
        logger.warning("[Curator] review pass failed (non-fatal): %s", exc)
        count = -1
    try:
        summary = (
            f"curator review: consolidated {count} shadow(s)"
            if count >= 0 else "curator review: consolidation errored"
        )
        curator.mark_run_complete(
            vault_root, summary=summary, duration_seconds=_time.time() - _t0,
        )
        logger.info("[Curator] idle-triggered review complete — %s", summary)
    except Exception as exc:
        logger.warning("[Curator] mark_run_complete failed (non-fatal): %s", exc)


def run_consolidation_for_all(config, vault) -> int:
    """Consolidate every shadow that needs it.  Returns the count updated.

    Callable from both the scheduler job and the NiceGUI dashboard.
    Triggers consolidation when either:
      • buffer_entries >= BUFFER_THRESHOLD, or
      • time since last consolidation >= STALE_AFTER_DAYS
    After each shadow is done, runs the skill-graduation pass.
    Writes a lightweight metadata JSON (memory_consolidation_meta.json) so
    the dashboard can show when the last full run completed.
    """
    import json as _json
    from datetime import datetime, timedelta
    from pathlib import Path

    now = utcnow()
    shadow_index = vault.load_index("shadow_army")
    if not shadow_index:
        logger.info("[Job] No shadows to consolidate.")
        return 0

    consolidated = 0
    for header in shadow_index:
        sid = header.get("id")
        if not sid:
            continue
        try:
            shadow = vault.get_shadow(sid)
        except KeyError:
            continue

        md_text, buffer_entries = vault.load_shadow_memory(sid)

        # Decide whether to consolidate
        last_consolidated = _parse_last_consolidated(md_text)
        is_stale = (now - last_consolidated) > timedelta(days=STALE_AFTER_DAYS)
        if len(buffer_entries) < BUFFER_THRESHOLD and not is_stale:
            continue
        if not buffer_entries and not is_stale:
            continue

        try:
            new_md = _consolidate_one(shadow, md_text, buffer_entries, config)
        except Exception as exc:
            logger.warning("[Job] Consolidation failed for shadow %s: %s", sid, exc)
            continue

        if not new_md or not new_md.lstrip().startswith("---"):
            logger.warning(
                "[Job] Consolidation for shadow %s produced invalid output — skipping write", sid,
            )
            continue

        vault.save_shadow_memory(sid, new_md)
        vault.clear_memory_buffer(sid)
        consolidated += 1

        # Skill graduation pass — propose any matured heuristic as a Skill.
        try:
            _graduate_memory_to_skills(shadow, new_md, vault)
        except Exception as exc:
            logger.warning("[Job] Skill graduation failed for shadow %s: %s", sid, exc)

    logger.info("[Job] Memory consolidation complete — %d shadow(s) updated.", consolidated)

    # Write last-run metadata for the dashboard
    try:
        meta = {
            "last_run":        now.isoformat(),
            "shadows_updated": consolidated,
            "shadows_total":   len(shadow_index),
        }
        meta_path = Path(vault.root) / "memory_consolidation_meta.json"
        meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[Job] Could not write consolidation meta: %s", exc)

    return consolidated


def _parse_last_consolidated(md_text: str):
    """Extract `last_consolidated` ISO timestamp from MEMORY.md frontmatter.

    Returns a year-old default for missing/malformed values so the staleness
    check always picks them up on the next run.
    """
    from datetime import datetime, timedelta
    import re
    fallback = utcnow() - timedelta(days=365)
    if not md_text:
        return fallback
    m = re.search(r"^last_consolidated:\s*(.+)$", md_text, re.MULTILINE)
    if not m:
        return fallback
    try:
        return datetime.fromisoformat(m.group(1).strip().replace("Z", ""))
    except ValueError:
        return fallback


def _consolidate_one(shadow, md_text, buffer_entries, config) -> str:
    """Run Tier-1 consolidation for a single shadow. Returns the new MEMORY.md text.

    Uses raw text mode (not JSON) since the prompt asks the LLM to emit a complete
    SHADOW_MEMORY.md file directly. This avoids fighting JSON-mode escaping of the
    multi-line markdown payload.
    """
    import json
    from datetime import datetime

    from systemu.core.llm_router import _run_coroutine, llm_call
    from systemu.core.utils import load_prompt

    payload = {
        "shadow_id":     shadow.id,
        "shadow_name":   shadow.name,
        "today":         utcnow().date().isoformat(),
        "current_memory_md": md_text or "(empty — first consolidation)",
        "buffer_entries":    buffer_entries,
    }

    resp = _run_coroutine(llm_call(
        tier=1,
        system=load_prompt("consolidate_memory.md"),
        user=json.dumps(payload, default=str),
        config=config,
        temperature=0.2,
        max_tokens=4096,
    ))
    raw = resp.get("content", "")
    if isinstance(raw, dict):
        for key in ("memory_md", "content", "result"):
            if key in raw and isinstance(raw[key], str):
                return raw[key]
        return ""
    return raw if isinstance(raw, str) else ""


def _graduate_memory_to_skills(shadow, memory_md: str, vault) -> None:
    """Scan consolidated memory for matured heuristics and propose them as Skills.

    Graduation criteria:
      - lives in the Heuristics section
      - confidence >= _GRADUATION_CONF
      - evidence spans >= _GRADUATION_MIN_SCROLLS distinct exec_ids
      (We use exec_id distinctness as a proxy for cross-scroll generalisation —
      a lesson confirmed across many runs is worth promoting.)

    Emits a Notification queued for user approval rather than auto-creating the
    skill, mirroring existing tool/skill approval gates.
    """
    import re
    from systemu.core.models import Notification, NotificationStatus
    from systemu.core.utils import generate_id

    # Extract the Heuristics section
    m = re.search(
        r"##\s+Heuristics\s*\n(.+?)(?=\n##\s+|\Z)",
        memory_md, re.DOTALL,
    )
    if not m:
        return
    body = m.group(1)

    bullet_re = re.compile(
        r"^-\s*\[conf:(\d+)[^\]]*evidence:\s*([^\]]+)\]\s*(.+?)$",
        re.MULTILINE,
    )

    proposed = 0
    for match in bullet_re.finditer(body):
        conf      = int(match.group(1))
        evidence  = [e.strip() for e in match.group(2).split(",") if e.strip()]
        lesson    = match.group(3).strip()

        if conf < _GRADUATION_CONF:
            continue
        if len(set(evidence)) < _GRADUATION_MIN_SCROLLS:
            continue

        # Skip if a notification for this exact lesson is already pending
        already = any(
            n.get("title", "").startswith("Memory graduation")
            and lesson[:80] in (n.get("message") or "")
            for n in vault.list_pending_notifications()
        )
        if already:
            continue

        notification = Notification(
            id=generate_id("notif"),
            title=f"Memory graduation: '{shadow.name}' has a matured heuristic",
            message=(
                f"Shadow '{shadow.name}' has a heuristic with confidence={conf} "
                f"observed across {len(set(evidence))} distinct executions:\n\n"
                f"  {lesson}\n\n"
                f"Promote this to a reusable Skill?"
            ),
            # v0.6.1-b: safe-default first (auto-reject in non-interactive mode)
            actions=["Reject", "Approve"],
            context={
                "notification_type": "memory_graduation",
                "shadow_id":         shadow.id,
                "lesson":            lesson,
                "confidence":        conf,
                "evidence_ids":      list(set(evidence)),
            },
        )
        vault.queue_notification(notification)
        proposed += 1

    if proposed:
        logger.info("[Job] Proposed %d skill graduation(s) for shadow %s", proposed, shadow.id)


def _scheduled_execute_job() -> None:
    """v0.8.6 + v0.8.7: APScheduler job — every minute.

    v0.8.7: split due schedules into "fresh" (within SCHEDULE_MISSED_THRESHOLD_SECONDS)
    and "missed" (older). Fresh ones dispatch normally. Missed ones get the
    skip-and-alert treatment (no dispatch, notification queued, event published).
    """
    if _config is None or _vault is None:
        return

    from datetime import datetime, timezone
    from systemu.scheduler.schedule_registry import list_active_schedules

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    threshold = SCHEDULE_MISSED_THRESHOLD_SECONDS
    due = []
    missed = []

    for s in list_active_schedules(_vault):
        if s.next_fire_at > now:
            continue
        age_seconds = (now - s.next_fire_at).total_seconds()
        if age_seconds <= threshold:
            due.append(s)
        else:
            missed.append((s, age_seconds))

    if not due and not missed:
        return

    if missed:
        logger.warning(
            "[Scheduler] %d schedule(s) missed (staleness > %ds) — skipping dispatch, surfacing alerts",
            len(missed), threshold,
        )
        for schedule, age in missed:
            try:
                _handle_missed_schedule(schedule, now, age, _config, _vault)
            except Exception:
                logger.exception("[Scheduler] missed-handling failed for %s", schedule.id)

    if due:
        logger.info("[Scheduler] %d schedule(s) due — dispatching", len(due))
        for schedule in due:
            try:
                _dispatch_scheduled(schedule, now, _config, _vault)
            except Exception:
                logger.exception("[Scheduler] dispatch failed for schedule %s", schedule.id)


def _dispatch_scheduled(schedule, now, config, vault) -> None:
    """Fire one scheduled execution via JobManager, then advance the schedule.

    v0.8.7: no dedup skip. If a previous run of the same (shadow, scroll) is
    still active, this fire dispatches anyway — operator's expressed intent
    via the schedule is honored.
    """
    from systemu.interface.jobs import JobManager
    from systemu.scheduler.schedule_registry import mark_fired
    from pathlib import Path
    import sys

    jm = JobManager.get()
    dedup_key = f"execute:{schedule.shadow_id}:{schedule.scroll_id}"

    project_root = str(Path(config.vault_dir).parent.parent.resolve())
    cmd = [
        sys.executable, "-m", "sharing_on",
        "army", "execute", schedule.shadow_id, schedule.scroll_id,
        # v0.8.16: a schedule fire is the "scheduled" trigger origin — the CLI
        # threads this into ShadowRuntime.execute(origin=...) so every event
        # partitions into Manual Logs (scheduled), not Supervisor (chat).
        "--origin", "scheduled",
    ]
    if schedule.dry_run:
        cmd.append("--dry-run")

    try:
        job = jm.start_job(
            name=f"Scheduled Execute: {schedule.scroll_id[:12]}",
            job_type="execute",
            cmd=cmd,
            cwd=project_root,
            dedup_key=dedup_key,
        )
        logger.info(
            "[Scheduler] Schedule %s fired → job %s (%s)",
            schedule.id, job.id, job.status.value,
        )
    except RuntimeError as exc:
        # Queue full, etc. — log + advance schedule (skip-missed semantics)
        logger.warning("[Scheduler] Could not dispatch schedule %s: %s", schedule.id, exc)

    mark_fired(schedule.id, now, vault)


def _compute_next_valid_fire(schedule, now):
    """v0.8.7: For RECURRING — smallest scheduled_at + N*interval > now.

    Example: scheduled_at=09:00, interval=60min, now=14:30 → returns 15:00
    (not 15:30, not 09:00+5*60min=14:00). This gives the operator the next
    valid future slot.
    """
    from datetime import timedelta
    interval = timedelta(minutes=schedule.interval_minutes)
    elapsed = now - schedule.scheduled_at
    n = int(elapsed.total_seconds() // (schedule.interval_minutes * 60)) + 1
    return schedule.scheduled_at + n * interval


def _format_age(seconds: float) -> str:
    """1234 -> '20m 34s', 7200 -> '2h', 90000 -> '1d 1h'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _queue_missed_notification(schedule, age_seconds, advanced_to, vault) -> None:
    """Queue an operator-visible Notification card for a missed schedule fire."""
    from systemu.core.models import Notification
    from systemu.core.utils import generate_id

    shadow_name = schedule.shadow_id
    scroll_name = schedule.scroll_id
    try:
        sh = vault.get_shadow(schedule.shadow_id)
        shadow_name = getattr(sh, "name", schedule.shadow_id)
    except Exception:
        pass
    try:
        sc = vault.get_scroll(schedule.scroll_id)
        scroll_name = getattr(sc, "name", schedule.scroll_id)
    except Exception:
        pass

    age_human = _format_age(age_seconds)

    if advanced_to is None:
        title = f"⏰ One-time schedule missed: {scroll_name}"
        message = (
            f"Scheduled fire for \"{scroll_name}\" via shadow \"{shadow_name}\" was due "
            f"{age_human} ago. Dashboard was likely down at the fire time. "
            f"Schedule has been marked completed without running. "
            f"Re-create the schedule if you still want it to execute."
        )
    else:
        title = f"⏰ Recurring schedule fire missed: {scroll_name}"
        message = (
            f"Scheduled fire for \"{scroll_name}\" via shadow \"{shadow_name}\" was due "
            f"{age_human} ago. Skipping this fire (dashboard was likely down). "
            f"Next fire is at {advanced_to.strftime('%Y-%m-%d %H:%M UTC')}. "
            f"Total missed fires for this schedule: {schedule.missed_fires_count + 1}."
        )

    notif = Notification(
        id=generate_id("notif"),
        title=title,
        message=message,
        actions=["OK"],
        context={
            "notification_type":  "schedule_missed",
            "schedule_id":        schedule.id,
            "shadow_id":          schedule.shadow_id,
            "scroll_id":          schedule.scroll_id,
            "age_seconds":        int(age_seconds),
            "advanced_to":        advanced_to.isoformat() if advanced_to else None,
        },
    )
    try:
        vault.queue_notification(notif)
    except Exception as exc:
        logger.warning("[Scheduler] Could not queue missed-schedule notification: %s", exc)


def _publish_missed_event(schedule, age_seconds, advanced_to) -> None:
    """Publish a WARNING event for the missed schedule. Visible in /insights → Events."""
    try:
        from systemu.interface.event_bus import EventBus
        msg = (
            f"Schedule {schedule.id} missed: due {_format_age(age_seconds)} ago"
        )
        if advanced_to is not None:
            msg += f", next_fire advanced to {advanced_to.strftime('%H:%M UTC')}"
        else:
            msg += ", marked COMPLETED (one-time, will not run)"
        EventBus.get().publish({
            "category": "scheduler",
            "level":    "WARNING",
            "message":  msg,
            "context": {
                "schedule_id":  schedule.id,
                "shadow_id":    schedule.shadow_id,
                "scroll_id":    schedule.scroll_id,
                "age_seconds":  int(age_seconds),
                "advanced_to":  advanced_to.isoformat() if advanced_to else None,
            },
        })
    except Exception:
        logger.debug("[Scheduler] could not publish missed-schedule event", exc_info=True)


def _handle_missed_schedule(schedule, now, age_seconds, config, vault) -> None:
    """v0.8.7: Skip a missed schedule, advance state, queue operator alert.

    For ONCE: status → COMPLETED with missed=True. Schedule never fires.
    For RECURRING: next_fire_at recomputed to next valid future slot;
                   missed_fires_count incremented. Schedule resumes normally.
    """
    from systemu.scheduler.schedule_registry import mark_missed
    from systemu.core.models import ScheduleMode

    if schedule.mode == ScheduleMode.ONCE:
        mark_missed(schedule.id, now, vault, advance_to=None)
        _queue_missed_notification(schedule, age_seconds, advanced_to=None, vault=vault)
        _publish_missed_event(schedule, age_seconds, advanced_to=None)
    else:  # RECURRING
        next_fire = _compute_next_valid_fire(schedule, now)
        mark_missed(schedule.id, now, vault, advance_to=next_fire)
        _queue_missed_notification(schedule, age_seconds, advanced_to=next_fire, vault=vault)
        _publish_missed_event(schedule, age_seconds, advanced_to=next_fire)
