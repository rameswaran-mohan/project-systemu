"""Stage 2 — Scroll Refiner.

Takes a completed Sharing-On capture session directory and refines the
raw `instructions.md` into a structured Scroll using Tier 1 reasoning.

After refinement, the scroll is saved as PENDING_APPROVAL.
If auto_approve_scrolls is enabled in config, it is immediately advanced
to APPROVED and the extract_and_process pipeline is called.
Otherwise, the user is prompted via the CLI notification system.

Handoff to Stage 3 happens via:
  from systemu.pipelines.activity_extractor import extract_and_process
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import ActionBlock, Objective, Scroll, ScrollStatus, TraceEvent
from systemu.core.utils import generate_id, load_prompt
from systemu.interface.notifications import notify_user, log_event
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─── GUI-codification guard ───────────────────────────────────────

# Two patterns: word-boundary patterns (verbs/app names) and file extensions
# (which need to match the leading dot — \b doesn't help across `.docx`).
_WORD_GUI_PATTERNS = re.compile(
    r"\b("
    r"screenshots?|screen shots?|snips?|snipping|"
    r"capture image|capture screen|"
    r"paste into|click on|drag (?:from|to)|"
    r"open word|open notepad|open chrome|open excel|type into|"
    r"snipping tool|microsoft word|notepad"
    r")\b",
    re.IGNORECASE,
)
_EXT_GUI_PATTERNS = re.compile(
    r"(\.docx|\.png|\.jpe?g|\.xlsx)\b",
    re.IGNORECASE,
)


class _GuiPatternProxy:
    """Combined matcher exposing .search() to mimic re.compile output."""

    def search(self, text: str):
        m = _WORD_GUI_PATTERNS.search(text)
        if m:
            return m
        return _EXT_GUI_PATTERNS.search(text)


FORBIDDEN_GUI_PATTERNS = _GuiPatternProxy()


def detect_gui_codification(objectives) -> List[Tuple[int, str]]:
    """scan objective.goal text for GUI verbs / app names / extensions.

    Accepts both ``Objective`` instances and raw dicts (LLM output).  Returns
    ``[(obj.id, matched_pattern), ...]`` for each offender.  Empty list = clean.
    """
    out: List[Tuple[int, str]] = []
    for obj in (objectives or []):
        if isinstance(obj, dict):
            goal_text = obj.get("goal", "") or ""
            oid = obj.get("id")
        else:
            goal_text = getattr(obj, "goal", "") or ""
            oid = getattr(obj, "id", None)
        if not isinstance(goal_text, str):
            continue
        m = FORBIDDEN_GUI_PATTERNS.search(goal_text)
        if m:
            out.append((oid, m.group(1)))
    return out


def _refine_with_gui_guard(*, payload: Dict[str, Any], prompt_text: str,
                           config: Config) -> Dict[str, Any]:
    """thin wrapper around llm_call_json that post-processes the
    result for GUI-codified objectives and re-prompts once if any are detected.

    Returns the LLM result dict.  When a re-prompt fires, the returned dict
    has a ``_v065_gui_guard`` key with first/second pass offender lists for
    the caller to record on scroll.pipeline_trace.
    """
    result = llm_call_json(
        tier=1, system=prompt_text, user=json.dumps(payload, default=str),
        config=config, temperature=0.1, max_tokens=4096,
    )

    objectives = result.get("objectives") or []
    first_pass = detect_gui_codification(objectives)
    if not first_pass:
        return result

    logger.warning(
        "[ScrollRefiner] %d objective(s) GUI-codified — rewriting: %s",
        len(first_pass), first_pass,
    )

    # Re-prompt the LLM with the rewrite prompt
    fix_prompt = load_prompt("rewrite_objectives_outcome_only.md")
    fix_payload = {
        "objectives": [
            {
                "id": oid,
                "goal": next(
                    (
                        (o.get("goal") if isinstance(o, dict) else getattr(o, "goal", ""))
                        for o in objectives
                        if (o.get("id") if isinstance(o, dict) else getattr(o, "id", None)) == oid
                    ),
                    "",
                ),
                "matched_pattern": pat,
            }
            for oid, pat in first_pass
        ]
    }
    try:
        fix_result = llm_call_json(
            tier=1, system=fix_prompt, user=json.dumps(fix_payload),
            config=config, temperature=0.1, max_tokens=2048,
        )
    except Exception as exc:
        logger.warning("[ScrollRefiner] rewrite call failed: %s", exc)
        result["_v065_gui_guard"] = {
            "first_pass_offenders": [list(p) for p in first_pass],
            "second_pass_offenders": [list(p) for p in first_pass],
            "rewrite_error": str(exc),
        }
        return result

    # Merge rewrites back
    rewrites = {o.get("id"): o for o in (fix_result.get("objectives") or [])}
    for o in objectives:
        oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
        if oid in rewrites:
            new_goal = rewrites[oid].get("goal")
            if new_goal:
                if isinstance(o, dict):
                    o["goal"] = new_goal
                else:
                    setattr(o, "goal", new_goal)

    result["objectives"] = objectives
    second_pass = detect_gui_codification(objectives)
    result["_v065_gui_guard"] = {
        "first_pass_offenders": [list(p) for p in first_pass],
        "second_pass_offenders": [list(p) for p in second_pass],
    }
    return result


# ─────────────────────────────────────────────────────────────────────────────

def refine_scroll(
    session_dir: Path,
    config: Config,
    vault: Vault,
    *,
    auto_proceed: bool = False,
    force_refine: bool = False,
) -> Scroll:
    """Refine a capture session into a Scroll.

    Args:
        session_dir:  Path to the capture session directory.
        config:       Config carrying API key + tier model names.
        vault:        Vault instance for persistence.
        auto_proceed: Override auto_approve_scrolls from config (used in tests).
        force_refine: When True, skip the session-id dedup check and always
                      create a new Scroll (e.g. workshop-driven rebuild).

    Returns:
        The newly created Scroll (status = PENDING_APPROVAL or APPROVED),
        or an existing Scroll if this session was already refined (idempotent).
    """
    # Seed module-level singletons so Stage 3 can resolve config/vault even
    # when called from deep in the approval chain without explicit args.
    from systemu.pipelines.activity_extractor import init_pipeline
    from systemu.interface.notifications import set_vault
    init_pipeline(config, vault)
    set_vault(vault)

    session_dir = Path(session_dir)
    instructions_path = session_dir / "instructions.md"
    session_json_path = session_dir / "session.json"

    if not instructions_path.exists():
        raise FileNotFoundError(f"instructions.md not found in {session_dir}")
    if not session_json_path.exists():
        raise FileNotFoundError(f"session.json not found in {session_dir}")

    raw_md       = instructions_path.read_text(encoding="utf-8")
    session_meta = json.loads(session_json_path.read_text(encoding="utf-8"))
    session_name = session_meta.get("name", session_dir.name)
    session_id   = session_meta.get("session_id", session_dir.name)

    # [A.4] Session-id dedup: return existing Scroll if this session was already refined.
    # force_refine=True bypasses this (e.g. workshop rebuild after a bad first refinement).
    #
    # vault.list_scrolls() returns index dicts (typed List[Dict[str, Any]]),
    # not Pydantic Scroll instances.  We dedup on the dict, then hydrate
    # via get_scroll() so the caller receives a real Scroll.  Same root
    # cause class as the v0.2.2 migration-tool fix + v0.3.1 SqliteVault
    # seed-on-empty fix — surfaced again here by the v0.3 e2e capture-flow
    # validation.
    if not force_refine:
        match_id = next(
            (
                header["id"]
                for header in (vault.list_scrolls() or [])
                if header.get("source_session_id") == session_id
                and header.get("status") != ScrollStatus.DRAFT.value
            ),
            None,
        )
        if match_id:
            try:
                existing = vault.get_scroll(match_id)
            except KeyError:
                logger.warning(
                    "[Scroll] Header matched session %s but get_scroll(%s) failed — re-refining",
                    session_id, match_id,
                )
            else:
                logger.info(
                    "[Scroll] Session '%s' already refined → scroll %s (status=%s) — returning existing",
                    session_id, existing.id, existing.status,
                )
                return existing

    logger.info("[Scroll] Refining session '%s' with Tier 1 ...", session_name)

    # ── Tier 1 call (with self-check retry loop) ─────────────────
    prompt = load_prompt("refine_scroll.md")
    user_msg = f"Session name: {session_name}\n\n---\n\n{raw_md}"

    def _call_refine(extra_context: str = "") -> Dict[str, Any]:
        return llm_call_json(
            tier=1, system=prompt,
            user=user_msg + (("\n\n---\n\n" + extra_context) if extra_context else ""),
            config=config, temperature=0.2, max_tokens=6000,
        )

    try:
        result = _call_refine()
    except Exception as exc:
        logger.error("[Scroll] LLM refinement failed for session '%s': %s", session_name, exc)
        raise RuntimeError(
            f"Scroll refinement failed for '{session_name}': {exc}. "
            "Check your API key, model configuration, and network connectivity."
        ) from exc

    # one automatic re-prompt if the LLM's own self-check failed.
    # The prompt asks the LLM to verify each objective serves the stated intent;
    # if it can't, it sets self_check_passed=false with notes.  We give it ONE
    # chance to fix it before surfacing to the operator.
    self_check_ok = result.get("self_check_passed")
    if self_check_ok is False:
        notes = str(result.get("self_check_notes", ""))[:500]
        logger.info(
            "[Scroll] self-check failed for '%s' (%s) — one auto-retry",
            session_name, notes,
        )
        try:
            retry_context = (
                "## Operator feedback\n\n"
                "Your previous refinement attempt set self_check_passed=false with these notes:\n\n"
                f"> {notes}\n\n"
                "Re-emit a corrected refinement where every objective serves the stated intent.  "
                "If you still cannot pass the self-check, keep self_check_passed=false and we will "
                "surface this to the human operator.  Do NOT echo this feedback in the output JSON."
            )
            retry = _call_refine(retry_context)
            # Accept retry if it now passes self-check; otherwise stick with
            # the original (the operator card will fire).
            if retry.get("self_check_passed") is True:
                result = retry
                logger.info("[Scroll] retry passed self-check for '%s'", session_name)
        except Exception:
            logger.debug("[Scroll] self-check retry call failed", exc_info=True)

    # ── GUI-codification guard ───────────────────────────────────
    # Detect GUI verbs/app names in objective goals; re-prompt once to rewrite
    # in outcome-only language.  Result records first/second-pass offenders.
    gui_offenders_first = detect_gui_codification(result.get("objectives") or [])
    gui_offenders_second: List[Tuple[int, str]] = []
    if gui_offenders_first:
        logger.warning(
            "[Scroll] %d objective(s) GUI-codified — rewriting: %s",
            len(gui_offenders_first), gui_offenders_first,
        )
        try:
            fix_prompt = load_prompt("rewrite_objectives_outcome_only.md")
            fix_payload = {
                "objectives": [
                    {
                        "id": oid,
                        "goal": next(
                            (o.get("goal", "") for o in result.get("objectives") or []
                             if o.get("id") == oid),
                            "",
                        ),
                        "matched_pattern": pat,
                    }
                    for oid, pat in gui_offenders_first
                ]
            }
            fix_result = llm_call_json(
                tier=1, system=fix_prompt, user=json.dumps(fix_payload),
                config=config, temperature=0.1, max_tokens=2048,
            )
            rewrites = {
                o.get("id"): o
                for o in (fix_result.get("objectives") or [])
            }
            for o in (result.get("objectives") or []):
                oid = o.get("id")
                if oid in rewrites and rewrites[oid].get("goal"):
                    o["goal"] = rewrites[oid]["goal"]
                    if rewrites[oid].get("success_criteria"):
                        o["success_criteria"] = rewrites[oid]["success_criteria"]
            gui_offenders_second = detect_gui_codification(result.get("objectives") or [])
        except Exception as exc:
            logger.warning("[Scroll] GUI rewrite call failed: %s", exc)
            gui_offenders_second = gui_offenders_first

    # ── Parse Objectives (intent-driven format) ────────────────────────────
    raw_objectives = result.get("objectives", [])
    objectives: List[Objective] = []
    for raw in raw_objectives:
        try:
            objectives.append(Objective.model_validate(raw))
        except Exception as exc:
            logger.warning("[Scroll] Skipping malformed Objective: %s — %s", raw, exc)

    # ── Build Scroll (expected_outcome added) ────────────────────
    scroll = Scroll(
        id=generate_id("scroll"),
        name=result.get("title", session_name),
        source_session_id=session_id,
        raw_instructions_path=str(instructions_path),
        narrative_md=result.get("narrative_md", ""),
        intent=result.get("intent", ""),
        expected_outcome=result.get("expected_outcome", ""),
        objectives=objectives,
        constraints=result.get("constraints", {}),
        observed_preferences=result.get("observed_preferences", {}),
        action_blocks=[],   # empty for intent-driven scrolls
        tags=result.get("tags", []),
        status=ScrollStatus.PENDING_APPROVAL,
    )
    # record GUI-guard outcome on the trace before save
    if gui_offenders_first:
        if gui_offenders_second:
            scroll.pipeline_trace.append(TraceEvent(
                stage="refine", level="warn",
                message=(f"GUI codification persists on {len(gui_offenders_second)} "
                         f"objective(s) after rewrite"),
                detail={
                    "first_pass": [list(p) for p in gui_offenders_first],
                    "second_pass": [list(p) for p in gui_offenders_second],
                },
            ))
        else:
            scroll.pipeline_trace.append(TraceEvent(
                stage="refine", level="info",
                message=(f"GUI codification fixed on {len(gui_offenders_first)} "
                         f"objective(s) via re-prompt"),
                detail={"first_pass": [list(p) for p in gui_offenders_first]},
            ))

    vault.save_scroll(scroll)
    logger.info(
        "[Scroll] Created '%s' (%d objectives) — status: %s",
        scroll.name, len(scroll.objectives), scroll.status,
    )
    log_event("INFO", "scroll", f"Scroll created: '{scroll.name}' ({len(scroll.objectives)} objectives)", {"scroll_id": scroll.id})

    # ── Approval gate ──────────────────────────────────────────────────────
    # The Scroll is now persisted at PENDING_APPROVAL.  Three approval paths:
    #   1. auto_proceed=True (programmatic — used by tests and `analyze --auto`)
    #   2. config.auto_approve_scrolls=True (operator opt-in via .env)
    #   3. Operator clicks ✓ Approve on the Scrolls page in the dashboard
    #
    # We no longer fire an interactive CLI prompt here, even from a TTY.
    # That prompt only worked inside `sharing_on record` running in the
    # foreground without a dashboard; in every other context (analyze
    # subprocess, dashboard-spawned refine job, headless capture, piped
    # stdin) it either hung or errored.  Dashboard-first flow is the
    # canonical approval path now, and it logs the same event for any
    # CLI-only operator to inspect via `sharing_on scrolls list`.
    # pre-execution validator runs BEFORE approval when enabled
    # (intelligent_supervisor_enabled OR SYSTEMU_SCROLL_VALIDATOR=1).  When the
    # validator says "not satisfiable", surface an operator approval card via
    # the v0.3.6 supervisor flash path and HOLD the scroll at PENDING_APPROVAL
    # regardless of auto-approve.  Fail-open on validator errors.
    try:
        from systemu.pipelines.scroll_validator import is_enabled, validate_scroll
        if is_enabled(config):
            v_result = validate_scroll(scroll, config=config, vault=vault)
            if not v_result.satisfiable:
                logger.warning(
                    "[Scroll] pre-flight validator BLOCKED scroll '%s' — %s",
                    scroll.name, v_result.summary,
                )
                # set explicit VALIDATOR_BLOCKED status + record
                # error trace event so the operator sees it on /scrolls.
                scroll.status = ScrollStatus.VALIDATOR_BLOCKED
                scroll.pipeline_trace.append(TraceEvent(
                    stage="validate", level="error",
                    message=f"validator blocked: {len(v_result.blockers)} blocker(s)",
                    detail={
                        "summary":   v_result.summary[:200],
                        "blockers":  [b.__dict__ for b in v_result.blockers][:5],
                        "proposed_revision": (
                            v_result.proposed_revision.__dict__
                            if v_result.proposed_revision else None
                        ),
                    },
                ))
                vault.save_scroll(scroll)
                # Surface to operator chat via the existing approval-flash bus.
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    from systemu.interface.event_bus import EventBus
                    bus = EventBus.get()
                    bus.publish({
                        "ts": _dt.now(tz=_tz.utc).isoformat(timespec="seconds"),
                        "level": "WARNING",
                        "category": "approval",
                        "message": f"⚠️ Scroll '{scroll.name}' may not be satisfiable",
                        "context": {
                            "approval_message": (
                                v_result.summary or "Pre-flight validator blocked this scroll."
                            ) + "\n\nReview on the Scrolls page to refine or override.",
                            "options": [],
                            "redirect_to": "/scrolls",
                            "dedup_key":   f"scroll-validate:{scroll.id}",
                            "scroll_id":   scroll.id,
                            "blockers":    [b.__dict__ for b in v_result.blockers],
                        },
                    })
                except Exception:
                    logger.debug("[Scroll] could not flash validator card", exc_info=True)
                log_event(
                    "WARNING", "scroll",
                    f"Scroll validator blocked '{scroll.name}': {v_result.summary[:200]}",
                    {"scroll_id": scroll.id, "blockers": [b.__dict__ for b in v_result.blockers]},
                )
                return scroll
            else:
                # record validator-passed info event
                scroll.pipeline_trace.append(TraceEvent(
                    stage="validate", level="info",
                    message="validator passed",
                    detail={"confidence": v_result.confidence},
                ))
                vault.save_scroll(scroll)
    except Exception:
        logger.debug("[Scroll] validator pre-flight skipped", exc_info=True)

    # renamed config.auto_approve_scrolls → config.non_interactive
    # (the flag was already governing more than scroll approval — see notify_user's
    # action-ordering contract).
    should_auto = auto_proceed or config.non_interactive
    if should_auto:
        _approve_scroll(scroll, vault)
    else:
        logger.info(
            "[Scroll] '%s' awaiting approval — open the Scrolls page in the "
            "dashboard (or run `sharing_on scrolls approve %s`).",
            scroll.name, scroll.id,
        )
        log_event(
            "INFO", "scroll",
            f"Scroll awaiting approval: '{scroll.name}' (id={scroll.id})",
            {"scroll_id": scroll.id, "approval_required": True},
        )

    return scroll


def _approve_scroll(scroll: Scroll, vault: Vault) -> None:
    """Advance scroll to APPROVED and trigger Stage 3."""
    scroll.status = ScrollStatus.APPROVED
    vault.save_scroll(scroll)
    logger.info("[Scroll] Approved: %s → triggering activity extraction", scroll.id)
    log_event("SUCCESS", "scroll", f"Scroll approved: '{scroll.name}' — extracting activities", {"scroll_id": scroll.id})

    # Deferred import to avoid circular dependency
    from systemu.pipelines.activity_extractor import extract_and_process
    result = extract_and_process(scroll, vault=vault)
    if result is None:
        logger.warning(
            "[Scroll] Extraction returned no activity for '%s' — scroll reset to PENDING_APPROVAL",
            scroll.id,
        )


def refine_from_text(
    prompt: str,
    vault: Vault,
    config: Config,
    *,
    prior_task: dict | None = None,
) -> Scroll:
    """Synthesise a Scroll from a free-text chat prompt.

    The Scroll is immediately set to APPROVED — the user already submitted it,
    so the approval gate is considered passed.  Downstream stages (extract,
    shadow_decision) are the responsibility of the caller (direct_task.py).

    Args:
        prompt:     Raw user prompt text.
        vault:      Vault instance for persistence.
        config:     Config with API key + tier model names.
        prior_task: Optional {scroll_name, intent, objectives} from the most
                    recent chat Scroll, injected when the user prefixed with
                    /continue. The new Scroll's objectives become contextually
                    aware of what just happened without being coupled to it.

    Returns:
        The newly created and immediately APPROVED Scroll.
    """
    from systemu.pipelines.activity_extractor import init_pipeline
    from systemu.interface.notifications import set_vault
    init_pipeline(config, vault)
    set_vault(vault)

    global_memory = vault.load_global_memory()

    user_payload: dict = {"user_prompt": prompt}
    if global_memory.strip():
        user_payload["global_memory"] = global_memory
    if prior_task:
        user_payload["prior_task"] = prior_task

    logger.info("[Scroll] Refining chat prompt via elder_intake.md ...")
    intake_prompt = load_prompt("elder_intake.md")
    try:
        result = llm_call_json(
            tier=1,
            system=intake_prompt,
            user=json.dumps(user_payload),
            config=config,
            temperature=0.2,
            max_tokens=4000,
        )
    except Exception as exc:
        logger.error("[Scroll] elder_intake LLM call failed: %s", exc)
        raise RuntimeError(f"Chat scroll refinement failed: {exc}") from exc

    raw_objectives = result.get("objectives", [])
    objectives: List[Objective] = []
    for raw in raw_objectives:
        try:
            objectives.append(Objective.model_validate(raw))
        except Exception as exc:
            logger.warning("[Scroll] Skipping malformed Objective: %s — %s", raw, exc)

    scroll = Scroll(
        id=generate_id("scroll"),
        name=result.get("title", prompt[:60]),
        source_session_id="chat",
        raw_instructions_path="",
        narrative_md=result.get("narrative_md", ""),
        intent=result.get("intent", ""),
        objectives=objectives,
        constraints=result.get("constraints", {}),
        tags=result.get("tags", []),
        action_blocks=[],
        status=ScrollStatus.APPROVED,   # chat tasks skip the approval gate
    )
    vault.save_scroll(scroll)
    logger.info(
        "[Scroll] Chat scroll '%s' created (%d objectives) — APPROVED",
        scroll.name, len(scroll.objectives),
    )
    log_event("INFO", "scroll",
              f"Chat scroll '{scroll.name}' created and approved ({len(scroll.objectives)} objectives)",
              {"scroll_id": scroll.id})
    return scroll


def approve_pending_scroll(scroll_id: str, vault: Vault) -> Scroll:
    """CLI helper: approve a scroll that is in PENDING_APPROVAL state.

    Used by `sharing_on scrolls approve <scroll_id>`.
    """
    scroll = vault.get_scroll(scroll_id)
    if scroll.status != ScrollStatus.PENDING_APPROVAL:
        raise ValueError(
            f"Scroll {scroll_id} is in state '{scroll.status}', not PENDING_APPROVAL."
        )
    _approve_scroll(scroll, vault)
    return scroll
