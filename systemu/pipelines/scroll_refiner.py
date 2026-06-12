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
from systemu.interface.command.gate import GateDescriptor
from systemu.interface.command.inbox import InboxQueue
from systemu.interface.notifications import notify_user, log_event
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─── v0.6.5-c: GUI-codification guard ───────────────────────────────────────

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
    """v0.6.5-c: scan objective.goal text for GUI verbs / app names / extensions.

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
    """v0.6.5-c: thin wrapper around llm_call_json that post-processes the
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
        "[ScrollRefiner] v0.6.5: %d objective(s) GUI-codified — rewriting: %s",
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
        logger.warning("[ScrollRefiner] v0.6.5: rewrite call failed: %s", exc)
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

def _apply_clarifications(result, session_id, call_refine, *, asker=None):
    """v0.8.19 (R3): if the draft emitted clarifying_questions, ask the operator a
    structured question (parks via PendingChoiceRequest until answered; degrades to a
    no-op when no decision queue / headless), then re-refine with the answers folded in.
    Returns the (possibly updated) draft dict. Conservative: only fires when the LLM
    explicitly emits clarifying_questions."""
    cqs = result.get("clarifying_questions") or []
    if not cqs:
        return result
    if asker is None:
        from systemu.interface.notifications import request_choice as asker
    answers = asker(cqs, dedup_key=f"clarify:{session_id}")  # may raise PendingChoiceRequest; None when headless
    if answers:
        ctx = ("## Operator answers to clarifying questions\n\n"
               + "\n".join(f"- {q}: {a}" for q, a in answers.items()))
        return call_refine(ctx)
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

    # ── Tier 1 call (v0.6.0-c: with self-check retry loop) ─────────────────
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

    # v0.6.0-c: one automatic re-prompt if the LLM's own self-check failed.
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

    # ── v0.6.5-c: GUI-codification guard ───────────────────────────────────
    # Detect GUI verbs/app names in objective goals; re-prompt once to rewrite
    # in outcome-only language.  Result records first/second-pass offenders.
    gui_offenders_first = detect_gui_codification(result.get("objectives") or [])
    gui_offenders_second: List[Tuple[int, str]] = []
    if gui_offenders_first:
        logger.warning(
            "[Scroll] v0.6.5: %d objective(s) GUI-codified — rewriting: %s",
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
            logger.warning("[Scroll] v0.6.5: GUI rewrite call failed: %s", exc)
            gui_offenders_second = gui_offenders_first

    # ── v0.8.19 (R3): ask clarifying questions for genuinely ambiguous requests ──
    result = _apply_clarifications(result, session_id, _call_refine)

    # ── Parse Objectives (intent-driven format) ────────────────────────────
    raw_objectives = result.get("objectives", [])
    objectives: List[Objective] = []
    for raw in raw_objectives:
        try:
            objectives.append(Objective.model_validate(raw))
        except Exception as exc:
            logger.warning("[Scroll] Skipping malformed Objective: %s — %s", raw, exc)

    # ── Build Scroll (v0.6.0-c: expected_outcome added) ────────────────────
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
    # v0.6.5-c: record GUI-guard outcome on the trace before save
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

    # W12 (audit F7): ring needs-you NOW — gates used to enqueue only when
    # the operator happened to render /work, so a freshly refined scroll sat
    # invisible (badge 0, rail empty, Inbox empty) until they wandered there.
    # Best-effort: the gate is enqueue-on-demand idempotent either way.
    try:
        from systemu.interface.scroll_gate import ensure_scroll_gate
        if str(getattr(scroll.status, "value", scroll.status)) == "pending_approval":
            ensure_scroll_gate(vault, scroll)
    except Exception:
        logger.debug("[Scroll] eager gate enqueue failed (non-fatal)",
                     exc_info=True)

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
    # v0.4.0-c: pre-execution validator runs BEFORE approval when enabled
    # (intelligent_supervisor_enabled OR SYSTEMU_SCROLL_VALIDATOR=1).  When the
    # validator says "not satisfiable", surface an operator approval card via
    # the v0.3.6 supervisor flash path and HOLD the scroll at PENDING_APPROVAL
    # regardless of auto-approve.  Fail-open on validator errors.
    # v0.8.4: validator + propose-bridge logic lives in
    # validate_and_propose_tools() (module scope) so the Workshop UI's
    # rebuild path can run the same checks after rebuilding scroll content.
    # Previously Workshop bypassed the validator entirely and the v0.8.1
    # propose-bridge never fired for operators who used the Workshop UI.
    try:
        from systemu.pipelines.scroll_validator import is_enabled
        if is_enabled(config):
            v_result = validate_and_propose_tools(scroll, config=config, vault=vault)

            if not v_result.satisfiable:
                logger.warning(
                    "[Scroll] pre-flight validator BLOCKED scroll '%s' — %s",
                    scroll.name, v_result.summary,
                )
                # v0.6.5-d: set explicit VALIDATOR_BLOCKED status + record
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
                        # v0.8.1: persist what the validator suggested as
                        # missing tools so the scroll history records the
                        # operator-reviewable surface (also visible via
                        # `sharing_on scrolls show <id>`).
                        "missing_tool_specs": [
                            s.__dict__ if hasattr(s, "__dict__") else dict(s)
                            for s in (getattr(v_result, "missing_tool_specs", None) or [])
                        ][:10],
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
                # v0.6.5-d: record validator-passed info event
                scroll.pipeline_trace.append(TraceEvent(
                    stage="validate", level="info",
                    message="validator passed",
                    detail={"confidence": v_result.confidence},
                ))
                vault.save_scroll(scroll)
    except Exception:
        logger.debug("[Scroll] validator pre-flight skipped", exc_info=True)

    # v0.6.1-b: renamed config.auto_approve_scrolls → config.non_interactive
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


def validate_and_propose_tools(scroll: Scroll, *, config: Config, vault: Vault):
    """Validate a scroll and run the v0.8.1 propose-bridge when blocked.

    Returns the ValidationResult.  Caller decides what status to set based
    on ``result.satisfiable``.

    Behavior:
      - Calls validate_scroll() (Tier-1 LLM via scroll_validator).
      - If blocked AND validator emitted missing_tool_specs:
          1. Creates Tool records with status=PROPOSED via
             propose_tools_from_specs (shows up on /tools page).
          2. Posts ONE OperatorDecisionQueue card so the operator sees the
             block on /insights → Pending Actions
             (dedup_key=f"validator_propose:{scroll.id}").
          3. Appends a pipeline_trace event recording the proposed tools.
      - If ``config.auto_forge_tools=True`` (dev escape-hatch), also
        immediately code-generates the proposed tools and re-validates.
      - Returns the (possibly re-evaluated) v_result.

    v0.8.4: extracted from refine_scroll's inline block so Workshop's
    rebuild_scroll can run the same checks after rebuilding scroll content.
    Previously the Workshop UI bypassed the validator entirely → the v0.8.1
    propose-bridge never fired for operators who used Workshop to fix
    blocked scrolls.
    """
    from systemu.pipelines.scroll_validator import validate_scroll
    v_result = validate_scroll(scroll, config=config, vault=vault)

    if v_result.satisfiable:
        return v_result

    specs = getattr(v_result, "missing_tool_specs", None) or []
    if not specs:
        # Validator blocks but emitted no specs — nothing to propose.
        # Caller still sees v_result.satisfiable=False and handles the
        # block notification.
        return v_result

    try:
        from systemu.pipelines.tool_forge import (
            propose_tools_from_specs,
            forge_proposed_tools_from_specs,
        )
        logger.info(
            "[Scroll] validator blocked '%s' with %d missing tool spec(s) — proposing for operator review",
            scroll.name, len(specs),
        )
        proposed = propose_tools_from_specs(specs, scroll, config, vault)

        if proposed:
            scroll.pipeline_trace.append(TraceEvent(
                stage="validate", level="info",
                message=f"validator-propose bridge: {len(proposed)} tool(s) proposed for review",
                detail={"proposed_tools": [
                    {"id": t.id, "name": t.name, "description": (t.description or "")[:120]}
                    for t in proposed
                ][:10]},
            ))

            try:
                from systemu.approval.decision_queue import OperatorDecisionQueue
                queue = OperatorDecisionQueue(vault)
                queue.post(
                    title=f"Scroll blocked: forge {len(proposed)} suggested tool(s)?",
                    body=(
                        f"The pre-flight validator blocked scroll "
                        f"'{scroll.name}' because it lacks tools to satisfy "
                        f"its objectives.  It proposed {len(proposed)} tool(s) "
                        f"you can forge to unblock it:\n\n"
                        + "\n".join(f"  • {t.name} — {(t.description or '')[:80]}"
                                    for t in proposed[:10])
                        + "\n\nReview the spec on /tools then click Forge per "
                        + "tool, or click Forge All here to forge them in one "
                        + "batch."
                    ),
                    options=["Skip", "Forge All"],
                    context={
                        "scroll_id":           scroll.id,
                        "scroll_name":         scroll.name,
                        "proposed_tool_ids":   [t.id for t in proposed],
                        "proposed_tool_names": [t.name for t in proposed],
                    },
                    dedup_key=f"validator_propose:{scroll.id}",
                )
            except Exception:
                logger.debug("[Scroll] could not post validator-propose decision", exc_info=True)

        # Dev escape-hatch: when auto_forge_tools=true, also code-generate
        # the proposed tools in-place and re-validate.  Preserves v0.7.3
        # Bug #14 backward-compat.
        if getattr(config, "auto_forge_tools", False) and proposed:
            forged = forge_proposed_tools_from_specs(specs, scroll, config, vault)
            if forged:
                scroll.pipeline_trace.append(TraceEvent(
                    stage="validate", level="info",
                    message=f"auto-forge bridge: {len(forged)} tool(s) forged from validator specs",
                    detail={"forged_tools": [t.name for t in forged][:10]},
                ))
                v_result = validate_scroll(scroll, config=config, vault=vault)
                if v_result.satisfiable:
                    logger.info(
                        "[Scroll] auto-forge bridge UNBLOCKED scroll '%s' (re-validation passed)",
                        scroll.name,
                    )
                else:
                    logger.info(
                        "[Scroll] re-validation still blocked after forge — proceeding to VALIDATOR_BLOCKED",
                    )
    except Exception:
        logger.exception("[Scroll] validator-propose bridge failed; treating as block")

    return v_result


def revalidate_blocked_scrolls_for_tool(
    tool_id: str,
    *,
    config: Config,
    vault: Vault,
) -> int:
    """Re-run validate_and_propose_tools on every VALIDATOR_BLOCKED scroll.

    Triggered by tool_service.heal_activities_for_tool when a tool transitions
    to DEPLOYED.  For each scroll that's now satisfiable, transitions to
    PENDING_APPROVAL and queues a fresh scroll_approval notification.

    Returns the number of scrolls advanced.

    Notes:
      - We re-validate ALL blocked scrolls, not just those referencing tool_id
        in missing_tool_specs.  The vault list is small in practice and this
        avoids brittle index tracking on changing tool_id sets.
      - Each scroll's revalidation is wrapped in try/except so one bad scroll
        doesn't block the others.
    """
    advanced = 0
    for header in vault.list_scrolls(status=ScrollStatus.VALIDATOR_BLOCKED):
        try:
            scroll = vault.get_scroll(header["id"])
            v_result = validate_and_propose_tools(scroll, config=config, vault=vault)
            if v_result.satisfiable:
                prior = scroll.status
                scroll.status = ScrollStatus.PENDING_APPROVAL
                vault.save_scroll(scroll)
                advanced += 1
                logger.info(
                    "[ScrollRefiner] Re-validation: scroll '%s' %s -> PENDING_APPROVAL "
                    "(tool %s deploy unblocked it)",
                    scroll.name, prior.value, tool_id,
                )
                _queue_ready_for_reapproval_notification(scroll, vault)
        except Exception:
            logger.exception(
                "[ScrollRefiner] re-validation failed for scroll %s",
                header.get("id"),
            )
    return advanced


def _queue_ready_for_reapproval_notification(scroll: Scroll, vault: Vault) -> None:
    """Enqueue the scroll-approve gate as a GateDescriptor (spec §4.3).

    Routed through the gate-mode dial (load_default_policy): under Bypass a
    non-floor scroll gate auto-approves (extraction runs); under Risk-tiered a
    medium-risk scroll asks; under Approve-only it always asks. Scroll is NOT a
    render-only/parking gate, so it's safe to carry a policy here (per the
    contract caveat — operator/credential paths stay policy=None)."""
    try:
        from systemu.interface.command.gate_mode import load_default_policy
        desc = GateDescriptor.from_scroll(
            scroll, summary="All required tools are deployed; ready to extract.")
        InboxQueue(vault).enqueue(
            desc, gate_type="scroll", policy=load_default_policy())
    except Exception:
        logger.exception("[ScrollRefiner] could not enqueue scroll gate %s",
                         getattr(scroll, "id", "?"))


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

    # v0.9.0 (Layer 1): inject user profile + recent/relevant facts so the
    # LLM can resolve "near me" / "for me" without asking the user.
    try:
        _prof = vault.get_user_profile()
        if _prof is not None:
            user_payload["user_profile"] = _prof.model_dump(mode="json")
        # Keep the fact budget tight: recent 20, no superseded.
        _facts = vault.load_user_facts(recent=20, include_superseded=False)
        if _facts:
            user_payload["user_facts"] = [
                {"fact": f.fact, "tags": f.tags, "confidence": f.confidence}
                for f in _facts
            ]
    except Exception:
        logger.debug("[Scroll] could not enrich user_payload with profile/facts",
                     exc_info=True)

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
        # v0.9.7 (Phase 3.3): Decision 0.1 #2 — store the verbatim user
        # message so the runtime goal-verifier can check work against the
        # original request rather than the refiner's paraphrase.
        # Use `prompt` (the raw arg) not `clean_prompt` from direct_task —
        # this function receives exactly the text to treat as authoritative.
        raw_request=prompt,
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


# ─────────────────────────────────────────────────────────────────────────────
# v0.8.5: dispatcher handler for validator_propose:* decisions.  When the
# operator clicks "Forge All" on a /insights -> Pending Actions card, this
# kicks off the LLM-forge pipeline immediately (matching the operator's
# expressed intent).  Pre-v0.8.5 the resolved choice sat until next sweep.
# ─────────────────────────────────────────────────────────────────────────────

def _handle_resolved_validator_propose(decision, choice, config, vault):
    if (choice or "").lower() not in ("forge all", "forge", "approve"):
        logger.info(
            "[ScrollRefiner] dispatcher: validator_propose choice %r — skipping forge",
            choice,
        )
        return

    _, _, scroll_id = decision.dedup_key.partition(":")
    if not scroll_id:
        logger.warning(
            "[ScrollRefiner] dispatcher: malformed dedup_key %r",
            decision.dedup_key,
        )
        return

    try:
        scroll = vault.get_scroll(scroll_id)
    except KeyError:
        logger.warning("[ScrollRefiner] dispatcher: scroll %s not found", scroll_id)
        return

    # Prefer specs carried on the decision (the snapshot at post time);
    # fall back to re-validating the live scroll if the context didn't carry them.
    specs = (decision.context or {}).get("missing_tool_specs")
    if not specs:
        v_result = validate_and_propose_tools(scroll, config=config, vault=vault)
        specs = getattr(v_result, "missing_tool_specs", None) or []

    if not specs:
        logger.info(
            "[ScrollRefiner] dispatcher: no missing tool specs to forge for %s",
            scroll_id,
        )
        return

    from systemu.pipelines.tool_forge import forge_proposed_tools_from_specs
    forge_proposed_tools_from_specs(specs, scroll, config, vault)


from systemu.approval.decision_dispatcher import register as _register_dispatch
_register_dispatch("validator_propose", _handle_resolved_validator_propose)
