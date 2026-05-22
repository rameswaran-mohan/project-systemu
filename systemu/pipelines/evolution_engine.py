"""Pipeline E — Evolution Engine.

Runs on a daily schedule (or on-demand via `sharing_on evolve`).

Uses Tier 1 with Progressive Loading:
  1. Send a lightweight vault summary index (~2-4K tokens).
  2. The LLM analyses for upgrade/merge/split/combine/discover opportunities.
  3. Each proposal is stored as an Evolution in the vault.
  4. User is notified for each proposal (approve / reject).
  5. On approval, the evolution is applied to the target entities.

Progressive loading: the LLM can call fetch_entity_detail(entity_type, entity_id)
to inspect any entity in full detail before making a proposal. This caps the base
context at ~5K tokens regardless of vault size.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import (
    Evolution, EvolutionStatus, EvolutionType,
    ScrollStatus, ShadowStatus,
)
from systemu.core.utils import generate_id, load_prompt, utcnow
from systemu.interface.notifications import notify_user
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────

def run_evolution_check(config: Config, vault: Vault) -> List[Evolution]:
    """Run the daily evolution check.

    Returns the list of Evolution proposals generated (all start as PROPOSED).
    """
    logger.info("[Evolution] Starting daily evolution check ...")

    # ── Build lightweight summary index ───────────────────────────────────
    vault_index = _build_summary_index(vault)
    token_estimate = len(json.dumps(vault_index)) // 4   # rough word count
    logger.info("[Evolution] Summary index: ~%d tokens", token_estimate)

    if not any(vault_index[k] for k in ["scrolls", "activities", "shadows", "tools", "skills"]):
        logger.info("[Evolution] Vault is empty — nothing to evolve yet.")
        return []

    # ── Tier 1 analysis with progressive loading ───────────────────────────
    # The LLM can call fetch_entity_detail via tool use to inspect entities.
    try:
        result = llm_call_json(
            tier=1,
            system=load_prompt("propose_evolution.md"),
            user=json.dumps(vault_index),
            config=config,
            temperature=0.3,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.error("[Evolution] LLM call failed: %s", exc)
        return []

    logger.info(
        "[Evolution] Analysis: %s",
        result.get("analysis_summary", "—"),
    )

    proposals = result.get("evolutions", [])
    if not proposals:
        logger.info("[Evolution] No evolution proposals this run.")
        return []

    # ── Store and notify for each proposal ────────────────────────────────
    saved: List[Evolution] = []
    for prop in proposals[:5]:    # cap at 5 per run
        try:
            evolution = _store_proposal(prop, vault)
            saved.append(evolution)
            _notify_evolution(evolution, vault)
        except Exception as exc:
            logger.warning("[Evolution] Failed to process proposal: %s — %s", prop, exc)

    logger.info("[Evolution] %d proposals stored.", len(saved))
    return saved


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_summary_index(vault: Vault) -> Dict[str, Any]:
    """Build a compact summary of the vault for the evolution engine."""
    scroll_headers = vault.load_index("scrolls")
    activity_headers = vault.load_index("activities")
    shadow_headers = vault.load_index("shadow_army")
    tool_headers = vault.load_index("tools")
    skill_headers = vault.load_index("skills")
    rejected = [
        e for e in vault.load_index("evolutions")
        if e.get("status") == EvolutionStatus.REJECTED.value
    ]

    return {
        "scrolls": [
            {"id": s["id"], "name": s["name"], "status": s["status"],
             "tags": s.get("tags", [])}
            for s in scroll_headers
        ],
        "activities": [
            {"id": a["id"], "name": a["name"], "status": a["status"],
             "assigned_shadow_id": a.get("assigned_shadow_id"),
             "missing_tools": a.get("missing_tools", [])}
            for a in activity_headers
        ],
        "shadows": [
            _summarize_shadow(s["id"], vault)
            for s in shadow_headers
        ],
        "tools": [
            {"id": t["id"], "name": t["name"], "status": t["status"],
             "tool_type": t.get("tool_type", "")}
            for t in tool_headers
        ],
        "skills": [
            {"id": s["id"], "name": s["name"], "category": s.get("category", ""),
             "evidence_count": len(s.get("evidence_scroll_ids", []))}
            for s in skill_headers
        ],
        "rejected_evolutions": [
            {"description": e.get("description", ""), "target_entity_type": e.get("target_entity_type", "")}
            for e in rejected
        ],
    }


def _summarize_shadow(shadow_id: str, vault: Vault) -> Dict[str, Any]:
    """Helper to load a Shadow fully and extract success ratios and execution stats."""
    try:
        sh = vault.get_shadow(shadow_id)
        runs = sh.execution_log
        successes = sum(1 for r in runs if r.get("status") == "success")
        return {
            "id": sh.id,
            "name": sh.name,
            "status": sh.status.value if hasattr(sh.status, "value") else sh.status,
            "activity_count": len(sh.assigned_activity_ids),
            "skill_count": len(sh.skill_ids),
            "tool_count": len(sh.available_tool_ids),
            "total_executions": len(runs),
            "success_rate": f"{(successes / max(1, len(runs))) * 100:.0f}%",
            "recent_failures": [r.get("summary") for r in runs if r.get("status") == "failure"][-3:]
        }
    except Exception:
        return {"id": shadow_id, "error": "failed to load"}


def _store_proposal(prop: Dict[str, Any], vault: Vault) -> Evolution:
    """Validate and store an evolution proposal."""
    raw_type = prop.get("type", "upgrade")
    try:
        evo_type = EvolutionType(raw_type)
    except ValueError:
        evo_type = EvolutionType.UPGRADE

    entity_type = prop.get("entity_type", "scroll")
    target_ids  = prop.get("target_ids", [])
    description = prop.get("description", "")
    rationale   = prop.get("rationale", "")

    if not description or not target_ids:
        raise ValueError(f"Malformed evolution proposal: {prop}")

    evolution = Evolution(
        id=generate_id("evolution"),
        evolution_type=evo_type,
        target_entity_type=entity_type,
        target_entity_ids=target_ids,
        description=description,
        rationale=rationale,
        status=EvolutionStatus.PROPOSED,
    )
    vault.save_evolution(evolution)
    return evolution


def _notify_evolution(evolution: Evolution, vault: Vault) -> None:
    """Notify user about a new evolution proposal and handle approval."""
    priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
        getattr(evolution, "priority", "medium"), "🟡"
    )

    # "Reject" is listed first so headless/scheduled runs default to safe no-op
    # (notify_user auto-selects actions[0] when there is no TTY).
    choice = notify_user(
        title=f"{priority_icon} Evolution Opportunity — {evolution.evolution_type.value.title()}",
        message=(
            f"Target: {evolution.target_entity_type} — {', '.join(evolution.target_entity_ids)}\n\n"
            f"[bold]Proposal:[/bold] {evolution.description}\n\n"
            f"[dim]Rationale:[/dim] {evolution.rationale}"
        ),
        actions=["Reject", "Approve"],
    )

    if choice.lower() == "approve":
        evolution.status = EvolutionStatus.APPROVED
        evolution.resolved_at = utcnow()
        vault.save_evolution(evolution)
        logger.info("[Evolution] Approved: %s", evolution.id)
        # Note: actual application (merging, upgrading entities) is Phase S3+
        # For now, mark as approved so it's tracked.
    else:
        evolution.status = EvolutionStatus.REJECTED
        evolution.resolved_at = utcnow()
        vault.save_evolution(evolution)
        logger.info("[Evolution] Rejected: %s", evolution.id)


def apply_evolution(evolution_id: str, config: Config, vault: Vault) -> bool:
    """Apply an approved evolution to its target entities.

    Currently supports: upgrade (description applied to shadow system_prompt).
    Full merge/split/combine support is a Phase S3 item.
    """
    try:
        evolution = vault.get_evolution(evolution_id)
    except KeyError:
        logger.error("[Evolution] Not found: %s", evolution_id)
        return False

    if evolution.status != EvolutionStatus.APPROVED:
        logger.warning("[Evolution] %s is not in APPROVED state", evolution_id)
        return False

    evo_type = evolution.evolution_type

    if evo_type == EvolutionType.UPGRADE and evolution.target_entity_type == "shadow":
        return _apply_shadow_upgrade(evolution, config, vault)

    logger.info(
        "[Evolution] Apply for type=%s entity=%s is deferred to Phase S3.",
        evo_type, evolution.target_entity_type,
    )
    # Mark as applied anyway so it doesn't re-surface
    evolution.status = EvolutionStatus.APPLIED
    evolution.resolved_at = utcnow()
    vault.save_evolution(evolution)
    return True


def _apply_shadow_upgrade(evolution: Evolution, config: Config, vault: Vault) -> bool:
    """Re-generate a Shadow's identity_block incorporating the evolution's description.

    identity split: the upgrade touches **only** the operator-controlled
    ``identity_block`` half of the Shadow's identity tier.  The
    consolidator-grown ``accumulated_voice`` is intentionally NOT proposed
    by the Evolution Engine — that field is the runtime's observation of
    demonstrated traits and shouldn't be overwritten by a separate
    pipeline.  See docs/memory-model.md for the contract.
    """
    from systemu.core.llm_router import llm_call_json as _llm

    for shadow_id in evolution.target_entity_ids:
        try:
            shadow = vault.get_shadow(shadow_id)
        except KeyError:
            logger.warning("[Evolution] Shadow %s not found", shadow_id)
            continue

        # Use Tier 1 to enrich the persona's identity_block.  We pass the
        # current identity_block (operator-controlled) rather than the
        # composed system_prompt so the LLM doesn't accidentally rewrite
        # accumulated_voice content into the identity block.
        result = _llm(
            tier=1,
            system=(
                "You are updating an AI agent's identity block to incorporate "
                "a new capability.  The identity block is the operator-controlled "
                "persona contract — preserve all existing identity, role, and "
                "constraints exactly.  Add the new capability naturally and "
                "concisely.  Do NOT include observed traits or behavioural "
                "patterns — those live in a separate field the consolidator "
                "owns. Return JSON: {\"updated_system_prompt\": \"...\"}"
            ),
            user=json.dumps({
                "shadow_name":             shadow.name,
                "current_identity_block":  shadow.identity_block,
                "evolution_description":   evolution.description,
                "rationale":               evolution.rationale,
            }),
            config=config,
            temperature=0.3,
            max_tokens=3000,
        )

        updated_prompt = result.get("updated_system_prompt", "")
        if updated_prompt:
            shadow.evolution_history.append({
                "evolution_id": evolution.id,
                "description": evolution.description,
                "applied_at": utcnow().isoformat(),
            })
            # identity split: shadow.system_prompt is a computed
            # property composed from identity_block + accumulated_voice.
            # Shadow upgrades touch the operator-controlled half — write
            # to identity_block.  accumulated_voice is owned by the
            # memory consolidator and must not be overwritten here.
            shadow.identity_block = updated_prompt
            vault.save_shadow(shadow)
            logger.info("[Evolution] Shadow '%s' upgraded successfully", shadow.name)

    evolution.status = EvolutionStatus.APPLIED
    evolution.resolved_at = utcnow()
    vault.save_evolution(evolution)
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Wild Card reflection
# ─────────────────────────────────────────────────────────────────────────────

def reflect_on_wild_card(
    shadow:           Any,
    activity:         Any,
    execution_result: Dict[str, Any],
    vault:            Vault,
    config:           Config,
) -> None:
    """Analyse a completed Wild Card run and emit evolution proposals + memory observations.

    Tier 1 call using reflect_wild_card.md.  All outputs are best-effort:
    failures are logged but never re-raised so the caller's result is unaffected.
    """
    logger.info("[Evolution] Reflecting on Wild Card run for activity '%s' ...", activity.name)

    try:
        scroll = vault.get_scroll(activity.scroll_id)
        scroll_data = {
            "intent":     scroll.intent,
            "objectives": [obj.model_dump(mode="json") for obj in scroll.objectives],
        }
    except Exception:
        scroll_data = {"intent": activity.name, "objectives": []}

    # Highest specialist score before Wild Card was chosen (best-effort)
    specialists = [s for s in vault.list_shadows() if s.get("name") != "Wild Card"]
    req_tools  = set(activity.required_tool_ids)
    req_skills = set(activity.required_skill_ids)
    wild_card_score = 0.0
    for header in specialists:
        sh_skills = set(header.get("skill_ids", []))
        sh_tools  = set(header.get("tool_ids", []))
        so = len(req_skills & sh_skills) / max(1, len(req_skills)) if req_skills else 0.0
        to = len(req_tools & sh_tools)  / max(1, len(req_tools))  if req_tools  else 0.0
        wild_card_score = max(wild_card_score, 0.5 * so + 0.5 * to)

    try:
        result = llm_call_json(
            tier=1,
            system=load_prompt("reflect_wild_card.md"),
            user=json.dumps({
                "scroll":           scroll_data,
                "execution_result": execution_result,
                "existing_shadows": vault.load_index("shadow_army"),
                "wild_card_score":  round(wild_card_score, 3),
            }),
            config=config,
            temperature=0.3,
            max_tokens=3000,
        )
    except Exception as exc:
        logger.error("[Evolution] Wild Card reflection LLM call failed: %s", exc)
        return

    # ── Proposed shadow ────────────────────────────────────────────────────
    proposed_shadow = result.get("proposed_shadow")
    if proposed_shadow and isinstance(proposed_shadow, dict) and proposed_shadow.get("name"):
        try:
            evo = Evolution(
                id=generate_id("evolution"),
                evolution_type=EvolutionType.DISCOVER,
                target_entity_type="shadow",
                target_entity_ids=[shadow.id],
                description=(
                    f"New specialist shadow proposal: {proposed_shadow['name']} — "
                    f"{proposed_shadow.get('description', '')}"
                ),
                rationale=proposed_shadow.get("rationale", ""),
                status=EvolutionStatus.PROPOSED,
            )
            vault.save_evolution(evo)
            logger.info("[Evolution] Proposed new shadow '%s' from Wild Card run",
                        proposed_shadow["name"])
        except Exception as exc:
            logger.warning("[Evolution] Could not store shadow proposal: %s", exc)

    # ── Proposed tools ─────────────────────────────────────────────────────
    for tool_prop in result.get("proposed_tools", []):
        if not tool_prop.get("name"):
            continue
        try:
            evo = Evolution(
                id=generate_id("evolution"),
                evolution_type=EvolutionType.DISCOVER,
                target_entity_type="tool",
                target_entity_ids=[],
                description=(
                    f"New tool proposal: {tool_prop['name']} — "
                    f"{tool_prop.get('description', '')}"
                ),
                rationale=tool_prop.get("rationale", ""),
                status=EvolutionStatus.PROPOSED,
            )
            vault.save_evolution(evo)
            logger.info("[Evolution] Proposed new tool '%s' from Wild Card run", tool_prop["name"])
        except Exception as exc:
            logger.warning("[Evolution] Could not store tool proposal: %s", exc)

    # ── Proposed skills ────────────────────────────────────────────────────
    for skill_prop in result.get("proposed_skills", []):
        if not skill_prop.get("name"):
            continue
        try:
            evo = Evolution(
                id=generate_id("evolution"),
                evolution_type=EvolutionType.DISCOVER,
                target_entity_type="skill",
                target_entity_ids=[],
                description=(
                    f"New skill proposal: {skill_prop['name']} — "
                    f"{skill_prop.get('description', '')}"
                ),
                rationale=skill_prop.get("rationale", ""),
                status=EvolutionStatus.PROPOSED,
            )
            vault.save_evolution(evo)
            logger.info("[Evolution] Proposed new skill '%s' from Wild Card run", skill_prop["name"])
        except Exception as exc:
            logger.warning("[Evolution] Could not store skill proposal: %s", exc)

    # ── Memory observations → Elder buffer ────────────────────────────────
    # Use the v0.2.2 gate-keeper helper — stamps tier provenance and
    # rejects cross-tier writes per docs/memory-model.md.
    for obs in result.get("memory_observations", []):
        if not obs.get("observation"):
            continue
        try:
            vault.append_elder_buffer(
                {
                    "category":    obs.get("category", "Workflow Patterns"),
                    "observation": obs["observation"],
                    "confidence":  obs.get("confidence", 1),
                    "shadow_id":   shadow.id,
                    "exec_id":     execution_result.get("execution_id", ""),
                    "timestamp":   utcnow().isoformat(),
                },
                source="evolution_engine",
            )
        except Exception as exc:
            logger.warning("[Evolution] Could not append memory observation: %s", exc)

    logger.info("[Evolution] Wild Card reflection complete for activity '%s'", activity.name)
