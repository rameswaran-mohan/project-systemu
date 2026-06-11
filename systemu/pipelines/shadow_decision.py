"""Stages 5 + 6 — Shadow Decision & Creation.

Stage 5: Score existing shadows heuristically against the activity's required
         skills and tools. Five paths:

         PARTIAL + chat activity  → Wild Card immediately (tools not needed for chat)
         PARTIAL + capture scroll → defer; _heal_activities_for_tool() re-triggers later
         top_score >= 0.85 + gap  → ASSIGN_EXISTING (no LLM call)
         top_score in [0.4, 0.85) → LLM tiebreak (evaluate_shadow_fit.md)
         top_score < 0.4 + chat   → Wild Card fallback
         top_score < 0.4 + capture→ LLM tiebreak with allow_wild_card=False → CREATE_NEW

Wild Card is only valid for chat-originated tasks. Capture-based scrolls always
get a dedicated specialist shadow.

Stage 6: Generate Shadow persona via Tier 1, store to vault, assign Activity.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import Activity, ActivityStatus, ScrollStatus, Shadow, ShadowStatus
from systemu.core.utils import generate_id, load_prompt
from systemu.interface.notifications import notify_user, log_event
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

def decide_shadow(
    activity: Activity,
    config: Config,
    vault: Vault,
    *,
    skip_supervisor: bool = False,
) -> Optional[Shadow]:
    """Stage 5: Assign activity to a shadow using heuristic pre-rank + LLM tiebreak.

    Returns:
        The Shadow that was assigned/created, or None if user skipped.
    """
    logger.info("[Shadow] Deciding shadow assignment for activity '%s' ...", activity.name)

    # ── Idempotency guard ─────────────────────────────────────────────────────
    # Handles crash between vault.save_shadow() and vault.save_activity() in
    # create_shadow(): the shadow file exists with this activity in its list,
    # but the activity record was never updated to ASSIGNED.
    for header in vault.list_shadows():
        if activity.id in header.get("assigned_activity_ids", []):
            shadow = vault.get_shadow(header["id"])
            activity.assigned_shadow_id = shadow.id
            activity.status             = ActivityStatus.ASSIGNED
            vault.save_activity(activity)
            logger.info(
                "[Shadow] Idempotency: activity '%s' already owned by shadow '%s' — re-linking",
                activity.name, shadow.name,
            )
            return shadow

    # Capture-based scrolls must be assigned to a specialist shadow.
    # Wild Card is only allowed for chat-originated tasks.
    is_chat = _is_chat_activity(activity, vault)

    # ── Shortcut: PARTIAL activity (required tools are still PROPOSED) ────
    if activity.status == ActivityStatus.PARTIAL:
        if is_chat:
            # Chat tasks don't need task-specific tools — Wild Card can run them now.
            logger.info("[Shadow] PARTIAL chat activity — routing to Wild Card immediately")
            wc = _get_or_create_wild_card(vault)
            _assign_shadow_to_activity(activity, wc, vault, skip_supervisor=skip_supervisor)
            log_event("INFO", "shadow",
                      f"PARTIAL chat activity '{activity.name}' routed to Wild Card",
                      {"shadow_id": wc.id, "activity_id": activity.id})
            return wc
        else:
            # Capture scroll: leave PARTIAL and wait for tools to be deployed.
            # _heal_activities_for_tool() will re-trigger decide_shadow() once
            # all required tools are enabled.
            logger.info(
                "[Shadow] PARTIAL capture activity '%s' — deferring until tools are deployed",
                activity.name,
            )
            return None

    # ── Heuristic pre-rank ────────────────────────────────────────────────
    req_skills = set(activity.required_skill_ids)
    req_tools  = set(activity.required_tool_ids)

    specialists = [s for s in vault.list_shadows() if s.get("name") != "Wild Card"]
    scored: List[tuple[float, dict]] = []

    for header in specialists:
        sh_skills = set(header.get("skill_ids", []))
        sh_tools  = set(header.get("tool_ids", []))

        skill_overlap = (
            len(req_skills & sh_skills) / max(1, len(req_skills)) if req_skills else 0.0
        )
        tool_overlap = (
            len(req_tools & sh_tools) / max(1, len(req_tools)) if req_tools else 0.0
        )
        score = 0.5 * skill_overlap + 0.5 * tool_overlap
        scored.append((score, header))

    scored.sort(key=lambda x: x[0], reverse=True)

    top_score    = scored[0][0] if scored else 0.0
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    top_header   = scored[0][1] if scored else None

    logger.info(
        "[Shadow] Heuristic scores — top=%.2f second=%.2f specialists=%d",
        top_score, second_score, len(specialists),
    )

    # ── Decision tree ──────────────────────────────────────────────────────
    if top_score >= 0.85 and (top_score - second_score) >= 0.10 and top_header:
        # Clear winner — assign directly, no LLM call
        shadow = vault.get_shadow(top_header["id"])
        logger.info("[Shadow] Clear winner '%s' (score=%.2f) — skipping LLM", shadow.name, top_score)
        new_skill_ids = [s for s in activity.required_skill_ids if s not in shadow.skill_ids]
        new_tool_ids  = [t for t in activity.required_tool_ids  if t not in shadow.available_tool_ids]
        return _assign_to_existing(activity, shadow.id, new_skill_ids, new_tool_ids, vault,
                                   skip_supervisor=skip_supervisor)

    elif top_score >= 0.4:
        # Moderate fit — LLM tiebreak. Capture scrolls cannot be routed to Wild
        # Card even if the LLM suggests it; chat tasks can.
        logger.info("[Shadow] Moderate fit — calling LLM tiebreak (allow_wild_card=%s)", is_chat)
        return _llm_shadow_decision(activity, config, vault, allow_wild_card=is_chat,
                                    skip_supervisor=skip_supervisor)

    else:
        # No specialist fits — Wild Card for chat, CREATE_NEW for capture scrolls
        if is_chat:
            logger.info("[Shadow] No specialist fits (top=%.2f) — Wild Card fallback (chat)", top_score)
            wc = _get_or_create_wild_card(vault)
            _assign_shadow_to_activity(activity, wc, vault, skip_supervisor=skip_supervisor)
            log_event("INFO", "shadow",
                      f"Activity '{activity.name}' routed to Wild Card (no specialist fit)",
                      {"shadow_id": wc.id, "activity_id": activity.id})
            return wc
        else:
            logger.info(
                "[Shadow] No specialist fits (top=%.2f) — capture scroll, requesting new shadow",
                top_score,
            )
            return _llm_shadow_decision(activity, config, vault, allow_wild_card=False,
                                        skip_supervisor=skip_supervisor)


# ─── Shadow assignment helpers ────────────────────────────────────────────────

def _submit_to_supervisor(
    activity_id: str,
    shadow_id: str,
    shadow_name: str,
    *,
    origin: str | None = None,
) -> None:
    """Submit the activity to the Supervisor for immediate execution.

    Silently skips if the Supervisor is not running (CLI / test mode).

    v0.8.16: ``origin`` carries the *activity's* trigger origin (chat / capture)
    so a shadow-awakened submission partitions into the right event pane.  The
    awaken ``reason`` is kept for the audit trail but does NOT drive the origin.
    """
    try:
        from systemu.runtime.supervisor import Supervisor
        Supervisor.get().submit(activity_id, shadow_id, reason="shadow_awakened", origin=origin)
        logger.info("[Shadow] Submitted activity '%s' to Supervisor via shadow '%s'",
                    activity_id, shadow_name)
    except RuntimeError:
        logger.debug("[Shadow] Supervisor not running — '%s' will await next daemon sweep",
                     shadow_name)
    except Exception as exc:
        logger.warning("[Shadow] Could not submit to Supervisor: %s", exc)


def _assign_shadow_to_activity(
    activity: Activity,
    shadow: Shadow,
    vault: Vault,
    skip_supervisor: bool = False,
) -> None:
    """Assign a shadow to an activity and notify the user."""
    if activity.id not in shadow.assigned_activity_ids:
        shadow.assigned_activity_ids.append(activity.id)
        vault.save_shadow(shadow)
    activity.assigned_shadow_id = shadow.id
    activity.status             = ActivityStatus.ASSIGNED
    vault.save_activity(activity)
    logger.info("[Shadow] Activity '%s' assigned to shadow '%s'",
                activity.name, shadow.name)
    notify_user(
        title="Activity Assigned",
        message=(
            f"Activity: \"{activity.name}\"\n"
            f"Shadow:   {shadow.name}"
        ),
        actions=["OK"],
    )
    if not skip_supervisor:
        _submit_to_supervisor(activity.id, shadow.id, shadow.name,
                              origin=getattr(activity, "origin", None))


def _assign_to_existing(
    activity:        Activity,
    shadow_id:       str,
    new_skill_ids:   list,
    new_tool_ids:    list,
    vault:           Vault,
    skip_supervisor: bool = False,
) -> Shadow:
    """Tag new skills/tools to an existing Shadow and assign the Activity."""
    shadow = vault.get_shadow(shadow_id)

    for sid in new_skill_ids:
        if sid not in shadow.skill_ids:
            shadow.skill_ids.append(sid)

    for tid in new_tool_ids:
        if tid not in shadow.available_tool_ids:
            shadow.available_tool_ids.append(tid)

    if activity.id not in shadow.assigned_activity_ids:
        shadow.assigned_activity_ids.append(activity.id)

    vault.save_shadow(shadow)

    activity.assigned_shadow_id = shadow.id
    activity.status             = ActivityStatus.ASSIGNED
    vault.save_activity(activity)

    # Mark scroll LINKED now that a shadow is confirmed
    _advance_scroll_after_shadow_assignment(activity.scroll_id, vault)

    logger.info(
        "[Shadow] Activity '%s' assigned to existing Shadow '%s' (+%d skills, +%d tools)",
        activity.name, shadow.name, len(new_skill_ids), len(new_tool_ids),
    )
    log_event("SUCCESS", "shadow",
              f"Activity '{activity.name}' assigned to shadow '{shadow.name}'",
              {"shadow_id": shadow.id, "activity_id": activity.id})

    notify_user(
        title="Activity Assigned to Existing Shadow",
        message=(
            f"Activity: \"{activity.name}\"\n"
            f"Shadow:   {shadow.name}\n"
            f"New skills tagged: {len(new_skill_ids)}  |  "
            f"New tools tagged: {len(new_tool_ids)}"
        ),
        actions=["OK"],
    )
    if not skip_supervisor:
        _submit_to_supervisor(activity.id, shadow.id, shadow.name,
                              origin=getattr(activity, "origin", None))
    return shadow


# ─── LLM tiebreak ─────────────────────────────────────────────────────────────

def _llm_shadow_decision(
    activity:        Activity,
    config:          Config,
    vault:           Vault,
    allow_wild_card: bool = True,
    skip_supervisor: bool = False,
) -> Optional[Shadow]:
    """LLM tiebreak: evaluate existing specialist shadows and decide assignment.

    Wild Card is intentionally excluded from the shadows_index payload.
    It has 100% skill/tool coverage so the LLM would always choose it via
    ASSIGN_EXISTING, preventing any new specialist from ever being proposed.
    Wild Card assignment is a system-level routing decision handled by the
    caller (decide_shadow), not an LLM choice.

    allow_wild_card=False (capture scrolls): if the LLM somehow targets Wild
    Card via ASSIGN_EXISTING, redirect to CREATE_NEW.
    """
    # Exclude Wild Card from the LLM's view — it must only reason about
    # specialist shadows so CREATE_NEW is proposed when no specialist fits.
    specialists_for_llm = [
        s for s in vault.load_index("shadow_army")
        if s.get("name") != "Wild Card"
    ]

    # v0.6.0-f: enrich the payload with the scroll's intent + expected_outcome
    # so the LLM tiebreak can score semantic match between the activity's
    # intent and each shadow's specialty/memory — not just ID overlap.  Pulls
    # from Activity.intent_snapshot (frozen at extraction time in Stage 3)
    # with a fall-back to a live scroll lookup if the snapshot is empty.
    scroll_intent = getattr(activity, "intent_snapshot", "") or ""
    scroll_expected_outcome = ""
    if not scroll_intent:
        try:
            scroll = vault.get_scroll(activity.scroll_id)
            scroll_intent = getattr(scroll, "intent", "") or ""
            scroll_expected_outcome = getattr(scroll, "expected_outcome", "") or ""
        except Exception:
            pass
    else:
        try:
            scroll = vault.get_scroll(activity.scroll_id)
            scroll_expected_outcome = getattr(scroll, "expected_outcome", "") or ""
        except Exception:
            pass

    try:
        decision = llm_call_json(
            tier=1,
            system=load_prompt("evaluate_shadow_fit.md"),
            user=json.dumps({
                "new_activity":     activity.model_dump(mode="json"),
                "scroll_intent":           scroll_intent,
                "scroll_expected_outcome": scroll_expected_outcome,
                "skills_index":     vault.load_index("skills"),
                "tools_index":      vault.load_index("tools"),
                "shadows_index":    specialists_for_llm,
                "activities_index": vault.load_index("activities"),
            }),
            config=config,
            temperature=0.2,
            max_tokens=3000,
        )
    except Exception as exc:
        logger.error(
            "[Shadow] LLM tiebreak call failed for activity '%s': %s — defaulting to CREATE_NEW",
            activity.name, exc,
        )
        log_event("WARNING", "shadow",
                  f"Shadow tiebreak LLM failed for '{activity.name}': {exc} — defaulting to CREATE_NEW",
                  {"activity_id": activity.id})
        decision = {}

    verdict       = decision.get("decision", "CREATE_NEW")
    target_shadow = decision.get("target_shadow_id")
    name_hint     = decision.get("proposed_shadow_name_hint", "NewShadow")
    reasoning     = decision.get("reasoning", "")
    new_skill_ids = decision.get("new_skills_to_tag", [])
    new_tool_ids  = decision.get("new_tools_to_tag", [])

    logger.info("[Shadow] LLM decision: %s | hint: %s | allow_wild_card: %s",
                verdict, name_hint, allow_wild_card)

    # Runtime guard: if the LLM somehow targets Wild Card via ASSIGN_EXISTING
    # (e.g. stale cache with Wild Card in the payload), treat as CREATE_NEW.
    if verdict == "ASSIGN_EXISTING" and target_shadow:
        try:
            candidate = vault.get_shadow(target_shadow)
            if candidate.name == "Wild Card":
                logger.warning(
                    "[Shadow] LLM targeted Wild Card via ASSIGN_EXISTING — overriding to CREATE_NEW"
                )
                verdict = "CREATE_NEW"
                target_shadow = None
        except KeyError:
            # Shadow not found — fall through to CREATE_NEW below
            verdict = "CREATE_NEW"
            target_shadow = None

    if verdict == "ASSIGN_EXISTING" and target_shadow:
        return _assign_to_existing(activity, target_shadow, new_skill_ids, new_tool_ids, vault,
                                   skip_supervisor=skip_supervisor)
    else:
        return _prompt_create_new(
            activity, name_hint, reasoning, new_skill_ids, new_tool_ids, config, vault,
            skip_supervisor=skip_supervisor,
        )


# ─── Wild Card bootstrap ──────────────────────────────────────────────────────

def _get_or_create_wild_card(vault: Vault) -> Shadow:
    """Idempotent Wild Card bootstrap.

    On first call: create a Shadow named 'Wild Card' with all deployed tools + all skills.
    On subsequent calls: refresh available_tool_ids and skill_ids to current vault state.
    """
    _DEPLOYED_STATUSES = {"deployed", "tested", "upgraded"}

    # Check if Wild Card already exists
    for header in vault.list_shadows():
        if header.get("name") == "Wild Card":
            shadow = vault.get_shadow(header["id"])
            # Refresh to current vault state so newly-deployed tools are visible
            shadow.available_tool_ids = [
                t["id"] for t in vault.load_index("tools")
                if t.get("enabled") and t.get("status", "").lower() in _DEPLOYED_STATUSES
            ]
            shadow.skill_ids = [s["id"] for s in vault.load_index("skills")]
            vault.save_shadow(shadow)
            logger.debug(
                "[Shadow] Wild Card refreshed — %d tools, %d skills",
                len(shadow.available_tool_ids), len(shadow.skill_ids),
            )
            return shadow

    # First time — create it
    all_tool_ids  = [
        t["id"] for t in vault.load_index("tools")
        if t.get("enabled") and t.get("status", "").lower() in _DEPLOYED_STATUSES
    ]
    all_skill_ids = [s["id"] for s in vault.load_index("skills")]

    wild_card = Shadow(
        id=generate_id("shadow"),
        name="Wild Card",
        description=(
            "Generalist shadow — handles novel tasks that no specialist covers. "
            "Runs with all deployed tools and all skills."
        ),
        system_prompt=(
            "You are a generalist. You have access to every tool in the system. "
            "Prefer programmatic tools over manual steps. "
            "Be explicit when blocked — declare FAIL rather than silently looping. "
            "Leave a clear trail of WHY for each decision so the system can learn from your runs."
        ),
        available_tool_ids=all_tool_ids,
        skill_ids=all_skill_ids,
        status=ShadowStatus.AWAKENED,
    )
    vault.save_shadow(wild_card)
    logger.info(
        "[Shadow] Wild Card bootstrapped — %d tools, %d skills",
        len(all_tool_ids), len(all_skill_ids),
    )
    log_event("INFO", "shadow", "Wild Card shadow bootstrapped",
              {"shadow_id": wild_card.id})
    return wild_card


# ─── Create-new flow (prompted) ───────────────────────────────────────────────

def _prompt_create_new(
    activity:        Activity,
    name_hint:       str,
    reasoning:       str,
    new_skill_ids:   list,
    new_tool_ids:    list,
    config:          Config,
    vault:           Vault,
    skip_supervisor: bool = False,
) -> Optional[Shadow]:
    """Prompt the user to confirm creation of a new Shadow."""
    reasoning_preview = reasoning[:300] + "..." if len(reasoning) > 300 else reasoning

    choice = notify_user(
        title="New Shadow Recommended",
        message=(
            f"Activity: \"{activity.name}\"\n"
            f"Suggested name: {name_hint}\n\n"
            f"Reasoning: {reasoning_preview}"
        ),
        # v0.6.1-b: safe-default first (auto-skip in non-interactive mode)
        actions=["Skip", "Assign to Existing", "Awaken"],
        prompt_for_name=True,
        # v0.8.0 Pattern 1: dedup_key routes the decision to the dashboard
        # /insights → Pending Actions queue when SYSTEMU_DECISION_QUEUE=true.
        # PendingOperatorDecision propagates up to the CLI wrapper.
        dedup_key=f"shadow_decision:{activity.id}",
    )

    if choice.lower().startswith("awaken"):
        parts = choice.split(":", 1)
        shadow_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else name_hint
        import os
        dims = None
        if os.environ.get("SYSTEMU_PERSONA_CREATIVITY"):
            dims = {
                "creativity":      int(os.environ.get("SYSTEMU_PERSONA_CREATIVITY", "50")),
                "professionalism": int(os.environ.get("SYSTEMU_PERSONA_PROFESSIONALISM", "50")),
                "techie":          int(os.environ.get("SYSTEMU_PERSONA_TECHIE", "50")),
                "thinking":        int(os.environ.get("SYSTEMU_PERSONA_THINKING", "50")),
            }
        return create_shadow(activity, shadow_name, config, vault, persona_dimensions=dims,
                             skip_supervisor=skip_supervisor)

    elif choice.lower().startswith("assign"):
        shadows = vault.list_shadows()
        if not shadows:
            logger.warning("[Shadow] No existing shadows to assign to — leaving unassigned")
            return None
        shadow_list = [{"id": s["id"], "name": s["name"], "status": s["status"]} for s in shadows]
        notify_user(
            title="Manual Shadow Assignment Required",
            message=(
                f"Activity \"{activity.name}\" needs to be manually assigned.\n"
                f"Open the Shadows page in the dashboard to assign it.\n\n"
                f"Available shadows: {', '.join(s['name'] for s in shadow_list)}"
            ),
            actions=["OK"],
            context={
                "notification_type": "shadow_assignment",
                "activity_id": activity.id,
                "available_shadows": shadow_list,
            },
        )
        return None

    else:
        logger.info("[Shadow] User skipped shadow creation for activity '%s'", activity.name)
        return None


def _advance_scroll_after_shadow_assignment(scroll_id: str, vault: Vault) -> None:
    """Advance the scroll to LINKED after shadow assignment succeeds.

    Pre-v0.8.5 this only advanced ACTIVE -> LINKED.  v0.8.5 broadens to
    also cover VALIDATOR_BLOCKED and PENDING_APPROVAL -> LINKED, because:

      - VALIDATOR_BLOCKED: when the scroll was regressed to blocked by a
        post-hoc validator pass but the pre-existing activity already has
        a shadow doing the work, the blocked display is stale.

      - PENDING_APPROVAL: rare edge case where shadow is created via the
        startup-recovery sweep before scroll was explicitly approved.
    """
    try:
        scroll = vault.get_scroll(scroll_id)
        if scroll.status in (
            ScrollStatus.ACTIVE,
            ScrollStatus.VALIDATOR_BLOCKED,
            ScrollStatus.PENDING_APPROVAL,
        ):
            prior = scroll.status
            scroll.status = ScrollStatus.LINKED
            vault.save_scroll(scroll)
            logger.info(
                "[Shadow] Scroll %s advanced %s -> LINKED",
                scroll_id, prior.value,
            )
    except Exception as exc:
        logger.warning("[Shadow] Could not advance scroll %s: %s", scroll_id, exc)


# Backward-compat alias (will be removed in v0.9.0).  Some internal callers
# may still use the old name; this keeps them working.
_mark_scroll_linked = _advance_scroll_after_shadow_assignment


def _is_chat_activity(activity: Activity, vault: Vault) -> bool:
    """Return True if the activity originated from a chat session (not a screen capture)."""
    try:
        scroll = vault.get_scroll(activity.scroll_id)
        return scroll.source_session_id == "chat"
    except Exception:
        return False


def create_shadow(
    activity:    Activity,
    shadow_name: str,
    config:      Config,
    vault:       Vault,
    *,
    persona_dimensions: dict | None = None,
    skip_supervisor: bool = False,
) -> Shadow:
    """Stage 6: Generate Shadow persona via Tier 1 and store to vault."""
    logger.info("[Shadow] Creating new Shadow '%s' for activity '%s' ...",
                shadow_name, activity.name)

    try:
        scroll = vault.get_scroll(activity.scroll_id)
    except KeyError:
        from systemu.core.models import Scroll as ScrollModel
        scroll = ScrollModel(
            id="stub", name=activity.name, source_session_id="cli",
            raw_instructions_path="", narrative_md=activity.name,
        )

    required_skills = []
    for sid in activity.required_skill_ids:
        try:
            required_skills.append(vault.get_skill(sid).model_dump(mode="json"))
        except KeyError:
            logger.warning(
                "[Shadow] activity '%s' references skill '%s' which is not in the "
                "vault — omitted from persona context (dangling capability ref)",
                activity.id, sid)

    required_tools = []
    for tid in activity.required_tool_ids:
        try:
            required_tools.append(vault.get_tool(tid).model_dump(mode="json"))
        except KeyError:
            logger.warning(
                "[Shadow] activity '%s' references tool '%s' which is not in the "
                "vault — omitted from persona context (dangling capability ref)",
                activity.id, tid)

    try:
        persona = llm_call_json(
            tier=1,
            system=load_prompt("generate_shadow_persona.md"),
            user=json.dumps({
                "shadow_name":       shadow_name,
                "activity":          activity.model_dump(mode="json"),
                "scroll":            scroll.model_dump(mode="json"),
                "required_skills":   required_skills,
                "required_tools":    required_tools,
                "persona_dimensions": persona_dimensions or {
                    "creativity": 50,
                    "professionalism": 50,
                    "techie": 50,
                    "thinking": 50,
                },
            }),
            config=config,
            temperature=0.4,
            max_tokens=3000,
        )
    except Exception as exc:
        logger.error(
            "[Shadow] Persona LLM call failed for shadow '%s': %s — using minimal defaults",
            shadow_name, exc,
        )
        log_event("WARNING", "shadow",
                  f"Persona generation failed for shadow '{shadow_name}': {exc} — using defaults",
                  {"activity_id": activity.id})
        persona = {}

    enabled_tool_ids = []
    for tid in activity.required_tool_ids:
        try:
            if vault.get_tool(tid).enabled:
                enabled_tool_ids.append(tid)
        except KeyError:
            # Already surfaced as a WARNING in the persona-context loop above;
            # debug here avoids double-logging the same dangling reference.
            logger.debug("[Shadow] dangling tool ref '%s' on activity '%s' "
                         "skipped from enabled set", tid, activity.id)

    # ── Deduplicate shadow name — append suffix if a shadow with this name exists ─
    existing_names = {s.get("name", "") for s in vault.list_shadows()}
    final_name = shadow_name
    if final_name in existing_names:
        suffix = 2
        while f"{shadow_name}_{suffix}" in existing_names:
            suffix += 1
        final_name = f"{shadow_name}_{suffix}"
        logger.info("[Shadow] Name '%s' already taken — using '%s'", shadow_name, final_name)

    shadow = Shadow(
        id=generate_id("shadow"),
        name=final_name,
        description=persona.get("description", f"Shadow specialising in: {activity.name}"),
        system_prompt=persona.get("system_prompt", ""),
        assigned_activity_ids=[activity.id],
        available_tool_ids=enabled_tool_ids,
        skill_ids=activity.required_skill_ids,
        status=ShadowStatus.AWAKENED,
    )
    vault.save_shadow(shadow)

    activity.assigned_shadow_id = shadow.id
    activity.status             = ActivityStatus.ASSIGNED
    vault.save_activity(activity)

    # Mark scroll LINKED now that a shadow is confirmed
    _advance_scroll_after_shadow_assignment(activity.scroll_id, vault)

    logger.info("[Shadow] Shadow '%s' (%s) awakened — assigned to activity '%s'",
                shadow.name, shadow.id, activity.name)
    log_event("SUCCESS", "shadow",
              f"Shadow '{shadow.name}' awakened with {len(shadow.skill_ids)} skills "
              f"and {len(shadow.available_tool_ids)} tools",
              {"shadow_id": shadow.id, "activity_id": activity.id})

    notify_user(
        title="Shadow Awakened",
        message=(
            f"Shadow \"{shadow.name}\" has been created and assigned.\n"
            f"Activity: {activity.name}\n"
            f"Skills: {len(shadow.skill_ids)}  |  Tools: {len(shadow.available_tool_ids)}"
        ),
        actions=["OK"],
    )
    if not skip_supervisor:
        _submit_to_supervisor(activity.id, shadow.id, shadow.name,
                              origin=getattr(activity, "origin", None))
    return shadow


# ─────────────────────────────────────────────────────────────────────────────
# v0.8.5: Register dispatcher handler so the dashboard can trigger shadow
# creation immediately after the operator clicks Awaken/Skip/Assign on the
# /insights -> Pending Actions card (instead of waiting up to 60min for the
# hourly sweep).
# ─────────────────────────────────────────────────────────────────────────────

def _handle_resolved_shadow_decision(decision, choice, config, vault):
    """Continuation for a resolved shadow_decision:* decision.

    Re-invokes decide_shadow on the referenced activity. decide_shadow's
    inner notify_user call will short-circuit to the resolved choice via
    queue.get_resolved_choice(dedup_key), then branch to create_shadow /
    _assign_to_existing / skip.
    """
    _, _, activity_id = decision.dedup_key.partition(":")
    if not activity_id:
        logger.warning("[Shadow] dispatcher: malformed dedup_key %r", decision.dedup_key)
        return
    try:
        activity = vault.get_activity(activity_id)
    except KeyError:
        logger.warning("[Shadow] dispatcher: activity %s not found", activity_id)
        return
    decide_shadow(activity, config, vault)


from systemu.approval.decision_dispatcher import register as _register_dispatch
_register_dispatch("shadow_decision", _handle_resolved_shadow_decision)

