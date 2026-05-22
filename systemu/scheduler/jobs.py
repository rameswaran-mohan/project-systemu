"""Scheduled background jobs for Systemu.

  hourly_shadow_sweep        — re-evaluate unassigned activities
  daily_evolution_check      — run the evolution engine
  consolidate_shadow_memory  — fold JSONL buffer into SHADOW_MEMORY.md
"""

from __future__ import annotations

import logging
from datetime import datetime

from systemu.core.utils import utcnow
# hoist _dr import to module level so tests can monkeypatch
# jobs_mod._dr.dry_run_tool without triggering a fresh import each call.
from systemu.pipelines import tool_dry_run as _dr

logger = logging.getLogger(__name__)

# These are set by daemon.py before scheduling
_config    = None
_vault     = None
_scheduler = None   # APScheduler instance — set by daemon after start()


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

    logger.info("[Job] Startup recovery sweep — scanning vault for incomplete pipeline states ...")

    # ── Pass 1: APPROVED scrolls with no linked activity ─────────────────────
    # Indicates a crash during extract_and_process (before the activity was saved).
    for header in _vault.list_scrolls(status=ScrollStatus.APPROVED):
        if header.get("activity_id"):
            continue
        scroll_id   = header["id"]
        scroll_name = header.get("name", scroll_id)
        logger.info("[Job] Recovery: scroll '%s' is APPROVED but has no activity — re-running extraction", scroll_name)
        log_event("WARNING", "scroll",
                  f"Scroll '{scroll_name}' was approved but extraction never completed — re-running.",
                  {"scroll_id": scroll_id})
        try:
            from systemu.pipelines.scroll_refiner import approve_pending_scroll
            # approve_pending_scroll checks for PENDING_APPROVAL; temporarily patch status
            scroll = _vault.get_scroll(scroll_id)
            from systemu.core.models import ScrollStatus as _SS
            scroll.status = _SS.PENDING_APPROVAL
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

    # ── Pass 7 (v0.6.5-f): dry-run any enabled tools that haven't been
    # validated yet.  Failures auto-disable + emit operator card.  Closes
    # the "web_screenshot tool failed at runtime" finding from the 2026-05-17
    # weather E2E — broken tools shouldn't reach shadow execution.
    try:
        dry_run_all_pending_tools(_vault, _config)
    except Exception:
        logger.exception("[Job] tool dry-run sweep failed")

    logger.info("[Job] Startup recovery sweep complete.")


def dry_run_all_pending_tools(vault, config, *, max_concurrent: int = 5) -> None:
    """One-shot startup sweep.

    For each ``enabled=True, dry_run_status in {None, 'not_run'}`` tool,
    dispatch the dry-run pipeline.  Bounded by ``max_concurrent``
    (default 5).  Each dry-run is capped at 30s by the existing sandbox.

    this sweep is now NON-DESTRUCTIVE.  Failures (whether a
    returned ``success=False`` result or a raised exception like an
    uncaught ImportError) record ``dry_run_status='failed'`` plus a
    classified evidence dict on the tool, but never set ``enabled=False``.
    Operators recover via /recover/tool/<id>.  This generalises the
    hotfix (which only kept the tool enabled when the failure
    string matched ``"treating all packages as pending"``) to ANY failure.
    """
    from concurrent.futures import ThreadPoolExecutor

    # prefer the dedicated vault helper if it exists (mockable in
    # unit tests).  Fall back to scanning load_index("tools") so existing
    # vault implementations without the helper still work.
    pending = None
    finder = getattr(vault, "find_tools_pending_dry_run", None)
    if callable(finder):
        try:
            pending = list(finder() or [])
        except Exception:
            logger.debug(
                "[Job] find_tools_pending_dry_run() raised — falling back to index scan",
                exc_info=True,
            )
            pending = None
    if pending is None:
        pending = [
            h for h in (vault.load_index("tools") or [])
            if h.get("enabled") and h.get("dry_run_status") in (None, "not_run")
        ]
    if not pending:
        logger.debug("[Job] no tools pending dry-run")
        return

    logger.info(
        "[Job] dry-running %d tools (max %d concurrent)",
        len(pending), max_concurrent,
    )

    def _resolve_tool(item):
        """Accept either an index header dict or an already-loaded Tool/MagicMock."""
        if isinstance(item, dict):
            try:
                return vault.get_tool(item["id"])
            except Exception:
                logger.exception("[Job] get_tool failed for %s", item.get("id"))
                return None
        return item

    def _record_failure(tool, error_text: str) -> None:
        """Populate dry_run_evidence + status without disabling the tool."""
        from systemu.recovery.classifier import classify_dry_run_error
        from systemu.recovery.links import recover_url

        classified = classify_dry_run_error(error_text)
        evidence = {
            "error": error_text,
            "classified_reason": classified.kind,
            "missing_package": classified.missing_package,
            "timestamp": datetime.utcnow().isoformat(),
        }
        tool.dry_run_status = "failed"
        tool.dry_run_evidence = evidence
        # NEVER set tool.enabled = False here.  Operators recover
        # via the dashboard recovery panel.
        try:
            vault.save_tool(tool)
        except Exception:
            logger.debug(
                "[Job] save_tool failed for %s", getattr(tool, "id", "?"),
                exc_info=True,
            )
        try:
            link = recover_url("tool", getattr(tool, "id", ""))
        except Exception:
            link = "/recover/tool/<id>"
        logger.warning(
            "[Job] tool %s dry-run failed (%s) — left ENABLED; recover at %s",
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
                "[Job] save_tool failed for %s", getattr(tool, "id", "?"),
                exc_info=True,
            )

    def _run_one(item):
        tool = _resolve_tool(item)
        if tool is None:
            return
        try:
            # hotfix: dry_run_tool's signature is (tool, *, vault, config).
            result = _dr.dry_run_tool(tool, vault=vault, config=config)
        except Exception as exc:
            # an UNCAUGHT exception from the dry-run pipeline (e.g.
            # a downstream ImportError on a missing dep) is still a failure
            # signal — record it but keep the tool enabled.
            error_text = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[Job] dry-run for %s raised %s — recording as failed (tool stays enabled)",
                getattr(tool, "name", "?"), error_text,
            )
            _record_failure(tool, error_text)
            return

        # tolerate fake/stub returns (None, plain dict, etc.) — only
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
                "[Job] tool %s dry-run pending dep approval — "
                "leaving enabled (operator must approve deps via /tools)",
                getattr(tool, "name", "?"),
            )

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        list(ex.map(_run_one, pending))


def dry_run_one_tool(tool_id: str) -> None:
    """Re-run dry-run for ONE tool (used after a dep is approved).

    lazy-init vault + config from env when called outside the
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
        classified = classify_dry_run_error(error_text)
        tool.dry_run_status = "failed"
        tool.dry_run_evidence = {
            "error": error_text,
            "classified_reason": classified.kind,
            "missing_package": classified.missing_package,
            "timestamp": datetime.utcnow().isoformat(),
        }
        vault.save_tool(tool)
        return

    success = bool(getattr(result, "success", True)) if result is not None else True
    if success:
        tool.dry_run_status = "passed"
        tool.dry_run_evidence = {}
    else:
        from systemu.recovery.classifier import classify_dry_run_error
        error_text = getattr(result, "error", None) or "(no error detail)"
        classified = classify_dry_run_error(error_text)
        tool.dry_run_status = "failed"
        tool.dry_run_evidence = {
            "error": error_text,
            "classified_reason": classified.kind,
            "missing_package": classified.missing_package,
            "timestamp": datetime.utcnow().isoformat(),
        }
    vault.save_tool(tool)


def _emit_dry_run_fail_card(tool, error) -> None:
    """surface a 'tool dry-run failed' operator card via the
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
        logger.debug("[Job] could not emit dry-run-fail card", exc_info=True)


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
    """re-save every tool to rewrite its index header with the new
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
                "[Job] backfilled %d tool header(s) with schema summaries",
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

    # Dedup: skip if a startup_dep_audit notification covering these exact tool IDs exists
    at_risk_ids = sorted(item["tool_id"] for item in at_risk)
    try:
        pending = vault.list_pending_notifications()
        for n in pending:
            ctx = n.get("context", {})
            if (ctx.get("notification_type") == "startup_dep_audit"
                    and sorted(ctx.get("tool_ids", [])) == at_risk_ids):
                logger.debug("[Job] Dep audit: suppressed duplicate notification")
                return
    except Exception:
        pass

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
            actions=["OK"],
            context={
                "notification_type": "startup_dep_audit",
                "tool_ids":          at_risk_ids,
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
            # safe-default first (auto-reject in non-interactive mode)
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
