"""Workshop Module - Pipeline for rebuilding artifactories via LLM prompt.

Handles the logic of reading an entity from the Vault, passing it to the 
LLM alongside a user prompt for structure-preserving modification, and
validating the result before saving it back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from sharing_on.config import Config
from systemu.core.llm_router import async_llm_call_json
from systemu.core.models import Notification, Scroll, ScrollStatus
from systemu.core.utils import generate_id, utcnow
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

async def rebuild_scroll(scroll_id: str, prompt: str, config: Config, vault: Vault) -> Scroll:
    """Rebuild a scroll using an LLM based on user prompt, preserving schema integrity."""
    logger.info("[Workshop] Rebuilding scroll %s with prompt: %s", scroll_id, prompt)
    
    # 1. Fetch the exact state of the Scroll from the Vault
    try:
        scroll = vault.get_scroll(scroll_id)
    except KeyError:
        raise ValueError(f"Scroll {scroll_id} not found in the vault.")
    
    scroll_data = scroll.model_dump(mode="json")
    
    # 2. System prompt ensuring we return the same rigid schema but updated contents
    system_prompt = (
        "You are the Systemu Workshop Rebuilder. Your task is to update a specific data structure "
        "according to the user's instructions. You MUST strictly preserve all system fields (id, created_at, "
        "updated_at, source_session_id). Only alter the content specifically requested (like narrative_md "
        "or action_blocks if requested). The output must be EXACTLY valid JSON matching the provided schema, "
        "without any markdown formatting wrapping the JSON, and must contain every key from the original object."
    )
    
    user_prompt = json.dumps({
        "original_scroll": scroll_data,
        "modification_instructions": prompt
    })
    
    # 3. Call the Tier 2 reasoning engine
    decision = await async_llm_call_json(
        tier=2,
        system=system_prompt,
        user=user_prompt,
        config=config,
        temperature=0.3,
        max_tokens=4096,
    )
    
    # 4. Integrity Testing: Ensure the LLM provided valid Pydantic structure
    try:
        updated_scroll = Scroll.model_validate(decision)
    except Exception as exc:
        logger.error("[Workshop] Scroll schema validation failed after rebuild: %s", exc)
        raise ValueError(f"Integrity Validation Failed: the AI broke the entity schema. Details: {exc}")
    
    # Force immutable keys to stay identical just in case the LLM tries to be sneaky
    updated_scroll.id = scroll.id
    updated_scroll.source_session_id = scroll.source_session_id
    updated_scroll.created_at = scroll.created_at
    from datetime import datetime
    updated_scroll.updated_at = utcnow()
    
    # 5. Save back into the Vault.
    # The content has changed — downstream artifacts (skills, tools, shadow)
    # were extracted from the previous version and must be re-derived.
    prior_status          = scroll.status
    had_linked_activity   = bool(scroll.activity_id)

    # v0.8.4: run the validator + propose-bridge on the rebuilt content
    # BEFORE deciding the new status.  If the rebuilt scroll still has tool
    # gaps, this proposes Tool records (status=PROPOSED) and posts a
    # decision card to /insights → Pending Actions — same surface as the
    # CLI refine path.  If validator passes, scroll proceeds to
    # PENDING_APPROVAL as before.
    #
    # Pre-v0.8.4: Workshop skipped the validator entirely and set
    # PENDING_APPROVAL unconditionally → the v0.8.1 propose-bridge never
    # fired for operators who used Workshop to edit blocked scrolls.
    new_status = ScrollStatus.PENDING_APPROVAL
    try:
        from systemu.pipelines.scroll_validator import is_enabled
        if is_enabled(config):
            from systemu.pipelines.scroll_refiner import validate_and_propose_tools
            v_result = validate_and_propose_tools(updated_scroll, config=config, vault=vault)
            if not v_result.satisfiable:
                from systemu.core.models import ScrollStatus as _SS
                new_status = _SS.VALIDATOR_BLOCKED
                logger.warning(
                    "[Workshop] Rebuilt scroll '%s' still blocked by validator — "
                    "status=VALIDATOR_BLOCKED.  Check /tools for proposed tools "
                    "and /insights → Pending Actions for the decision card.",
                    updated_scroll.name,
                )
    except Exception:
        # Fail-open: never let a validator error block the rebuild from
        # being saved.  Falls through to PENDING_APPROVAL.
        logger.exception("[Workshop] validator failed during rebuild; defaulting to PENDING_APPROVAL")

    updated_scroll.status = new_status
    vault.save_scroll(updated_scroll)
    logger.info(
        "[Workshop] Saved rebuilt scroll %s (prior status: %s → %s)",
        scroll.id, prior_status, new_status.value,
    )

    # 6. Dismiss any stale scroll_approval notification for this scroll so the
    #    Notifications page doesn't show a duplicate approval card.
    try:
        for notif in vault.list_pending_notifications():
            ctx = notif.get("context", {})
            if ctx.get("notification_type") == "scroll_approval" and ctx.get("scroll_id") == scroll.id:
                vault.resolve_notification(notif["id"], "superseded_by_rebuild")
    except Exception as exc:
        logger.warning("[Workshop] Could not dismiss stale approval notification: %s", exc)

    # 7. Queue a fresh approval notification that makes the rebuild context explicit.
    supersede_note = ""
    if had_linked_activity:
        supersede_note = (
            "\n\n⚠️ This scroll was previously processed. Existing skills, tools, "
            "and shadow assignment were based on the prior version. "
            "Re-approving will run fresh extraction and may update or supersede them."
        )

    # v0.8.5: queue a notification appropriate to the new scroll status.
    # Pre-v0.8.5 always queued an Approve/Reject card regardless of status,
    # so clicking Approve on a VALIDATOR_BLOCKED card silently errored in
    # approve_pending_scroll (status check rejected non-PENDING_APPROVAL).
    if new_status == ScrollStatus.PENDING_APPROVAL:
        notif = Notification(
            id=generate_id("notif"),
            title=f"🔄 Rebuilt Scroll — Re-approval Required: {updated_scroll.name}",
            message=(
                f"Scroll \"{updated_scroll.name}\" was rebuilt in the Workshop "
                f"(previous status: {prior_status}).\n"
                f"Review the updated content and approve to re-run skill/tool extraction."
                + supersede_note
            ),
            # v0.6.1-b: safe-default first (auto-reject in non-interactive mode)
            actions=["Reject", "Approve"],
            context={
                "notification_type": "scroll_approval",
                "scroll_id":         scroll.id,
                "rebuilt":           True,
            },
        )
    else:
        # VALIDATOR_BLOCKED (or any other non-PENDING_APPROVAL state):
        # queue an informational card with NO Approve action.  Once the
        # required tools are deployed, scroll_refiner's revalidation hook
        # (v0.8.5 Fix B) will auto-advance the scroll and queue a fresh
        # Approve/Reject card.
        notif = Notification(
            id=generate_id("notif"),
            title=f"⏳ Rebuilt Scroll Blocked — Tools Required: {updated_scroll.name}",
            message=(
                f"Scroll \"{updated_scroll.name}\" was rebuilt but the validator "
                f"identified missing tools. Forge them on /tools, then return to "
                f"/insights → Pending Actions to re-approve.\n\n"
                f"Once all required tools are deployed, the scroll will "
                f"automatically advance to ready-for-approval."
            ),
            actions=["OK"],
            context={
                "notification_type": "scroll_blocked_info",
                "scroll_id":         scroll.id,
                "rebuilt":           True,
            },
        )
    try:
        vault.queue_notification(notif)
    except Exception as exc:
        logger.warning("[Workshop] Could not queue post-rebuild notification: %s", exc)

    # 8. Log event so the Event Log tab shows the rebuild.
    try:
        from systemu.interface.notifications import log_event
        log_event(
            "INFO", "scroll",
            f"Scroll '{updated_scroll.name}' rebuilt in Workshop — awaiting re-approval",
            {"scroll_id": scroll.id, "prior_status": str(prior_status)},
        )
    except Exception:
        pass

    return updated_scroll
