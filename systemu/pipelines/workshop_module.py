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
    
    # 5. Save back into the Vault with status reset to PENDING_APPROVAL.
    # The content has changed — downstream artifacts (skills, tools, shadow)
    # were extracted from the previous version and must be re-derived.
    prior_status          = scroll.status
    had_linked_activity   = bool(scroll.activity_id)
    updated_scroll.status = ScrollStatus.PENDING_APPROVAL
    vault.save_scroll(updated_scroll)
    logger.info(
        "[Workshop] Saved rebuilt scroll %s (prior status: %s → PENDING_APPROVAL)",
        scroll.id, prior_status,
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

    notif = Notification(
        id=generate_id("notif"),
        title=f"🔄 Rebuilt Scroll — Re-approval Required: {updated_scroll.name}",
        message=(
            f"Scroll \"{updated_scroll.name}\" was rebuilt in the Workshop "
            f"(previous status: {prior_status}).\n"
            f"Review the updated content and approve to re-run skill/tool extraction."
            + supersede_note
        ),
        # safe-default first (auto-reject in non-interactive mode)
        actions=["Reject", "Approve"],
        context={
            "notification_type": "scroll_approval",
            "scroll_id":         scroll.id,
            "rebuilt":           True,
        },
    )
    try:
        vault.queue_notification(notif)
    except Exception as exc:
        logger.warning("[Workshop] Could not queue re-approval notification: %s", exc)

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
