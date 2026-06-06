"""Stages 3 + 4 — Activity Extractor.

Stage 3: Call Tier 1 to extract Skills and Tools from an approved Scroll.
         Store each entity one-by-one with index deduplication.
         New skills get Agent Skills Standard folder structure (SKILL.md).
         New tools are marked PROPOSED (no implementation yet).

Stage 4: Bundle the scroll's skills and tools into an Activity.
         Determine status: PARTIAL (if any tools still PROPOSED) or UNASSIGNED.
         Chain immediately to Stage 5 (shadow_decision.py).
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import (
    Activity, ActivityStatus,
    Scroll, ScrollStatus,
    Skill, Tool, ToolStatus,
)
from systemu.core.utils import generate_id, load_prompt
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


def _log_forge_rationale(spec: dict) -> None:
    """v0.8.22 (B): log forge_rationale when the LLM proposes a new tool.
    Provides the diagnostic data needed before deciding G3 (approval gate)."""
    if not isinstance(spec, dict):
        return
    if not spec.get("is_new"):
        return
    rationale = (spec.get("forge_rationale") or "").strip()
    name = spec.get("name", "?")
    if rationale:
        logger.info("[ActivityExtractor] forge rationale for %r: %s", name, rationale[:240])
    else:
        logger.warning("[ActivityExtractor] new tool %r proposed WITHOUT forge_rationale "
                       "(prompt instructs to explain why)", name)


# ─────────────────────────────────────────────────────────────────────────────

def extract_and_process(
    scroll: Scroll,
    config: Config | None = None,
    vault: Vault | None = None,
    *,
    skip_shadow_decision: bool = False,
) -> Optional[Activity]:
    """Stages 3 + 4: extract skills/tools from an approved Scroll, create Activity.

    NOTE: config and vault are stored as module-level singletons after the
    first pipeline call so callers deep in the chain don't have to pass them.
    """
    cfg, vlt = _resolve_deps(config, vault)

    # [A.4] Idempotency guard: if this scroll already has an activity, return it.
    # The ACTIVE/LINKED status set by a previous extraction pass is the canonical
    # signal that Stage 3 completed — there is nothing to re-extract.
    if scroll.activity_id:
        try:
            existing_activity = vlt.get_activity(scroll.activity_id)
            logger.info(
                "[Extract] Scroll '%s' already has activity %s — returning existing (idempotent)",
                scroll.name, scroll.activity_id,
            )
            return existing_activity
        except KeyError:
            # Activity record was deleted — fall through and re-extract.
            logger.warning(
                "[Extract] Scroll '%s' has activity_id %s but record not found — re-extracting",
                scroll.name, scroll.activity_id,
            )

    logger.info("[Extract] Processing scroll '%s' ...", scroll.name)

    # ── Stage 3a: Load existing indexes for deduplication ─────────────────
    # v0.6.0-d: catalogs now include schema-level info so the LLM can do
    # data-flow reasoning instead of name-keyword matching.  We still keep
    # the payload bounded by truncating schemas to {field: type} pairs and
    # capping field count per record.
    existing_skills = [
        _enrich_skill_for_catalog(s)
        for s in vlt.load_index("skills")
    ]
    existing_tools = [
        _enrich_tool_for_catalog(t, vlt)
        for t in vlt.load_index("tools")
    ]

    # ── Stage 3b: Tier 1 extraction call ──────────────────────────────────
    prompt = load_prompt("extract_skills_tools.md")

    # Use objectives (new format) when available; fall back to action_blocks for legacy scrolls.
    # Exclude observed_preferences — GUI tool names there (Chrome, Word, Snipping Tool)
    # confuse the model into thinking no programmatic tools should be extracted.
    if scroll.objectives:
        task_spec = {
            "scroll_name":      scroll.name,
            "intent":           scroll.intent,
            # v0.6.0-d: also surface expected_outcome from Stage 2
            "expected_outcome": getattr(scroll, "expected_outcome", ""),
            "narrative":        scroll.narrative_md,
            "objectives":       [obj.model_dump(mode="json") for obj in scroll.objectives],
            "constraints":      scroll.constraints,
        }
    else:
        task_spec = {
            "scroll_name":   scroll.name,
            "narrative":     scroll.narrative_md,
            "action_blocks": [ab.model_dump(mode="json") for ab in scroll.action_blocks],
        }
    # v0.9.0 (Layer 1): pre-populate the user's default output directory so
    # extracted objectives can reference concrete write paths.
    try:
        _prof = vlt.get_user_profile()
        if _prof is not None:
            task_spec["default_output_dir"] = _prof.default_output_dir
    except Exception:
        pass

    task_spec["existing_skills"] = existing_skills
    task_spec["existing_tools"]  = existing_tools

    try:
        result = llm_call_json(
            tier=1,
            system=prompt,
            user=json.dumps(task_spec),
            config=cfg,
            temperature=0.1,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.error("[Extract] LLM extraction call failed for scroll '%s': %s", scroll.name, exc)
        from systemu.interface.notifications import log_event as _log_event
        _log_event(
            "ERROR", "scroll",
            f"Extraction LLM call failed for '{scroll.name}': {exc}. Scroll reset to PENDING_APPROVAL.",
            {"scroll_id": scroll.id},
        )
        scroll.status = ScrollStatus.PENDING_APPROVAL
        vlt.save_scroll(scroll)
        return None

    # ── Stage 3c: Store Tools FIRST — IDs must exist before skills link them ─
    tool_ids:         List[str] = []
    missing_tool_ids: List[str] = []

    for spec in result.get("tools", []):
        _log_forge_rationale(spec)  # v0.8.22 (B): diagnostic
        tid, is_new = _upsert_tool(spec, vlt)
        tool_ids.append(tid)
        if is_new:
            missing_tool_ids.append(tid)

    # ── Stage 3d: Store Skills SECOND — can now resolve tool IDs from vault ─
    skill_ids: List[str] = []
    for spec in result.get("skills", []):
        sid = _upsert_skill(spec, scroll.id, vlt)
        skill_ids.append(sid)

    missing_tool_names = []
    for tid in missing_tool_ids:
        try:
            missing_tool_names.append(vlt.get_tool(tid).name)
        except KeyError:
            missing_tool_names.append(tid)

    # ── Guard: empty extraction — retry once without existing indexes ─────────
    # The most common failure mode: the model sees existing_tools/skills and
    # misinterprets the deduplication rule as "don't return tools that already
    # exist". Retry with an empty index so the model proposes tools freely,
    # then we deduplicate programmatically by name.
    if not skill_ids and not tool_ids:
        logger.warning(
            "[Extract] First extraction pass returned empty for scroll '%s' — "
            "retrying without existing indexes (deduplication confusion guard).",
            scroll.name,
        )
        retry_spec = {k: v for k, v in task_spec.items()
                      if k not in ("existing_skills", "existing_tools")}
        retry_spec["existing_skills"] = []
        retry_spec["existing_tools"]  = []
        try:
            result = llm_call_json(
                tier=1,
                system=prompt,
                user=json.dumps(retry_spec),
                config=cfg,
                temperature=0.1,
                max_tokens=4096,
            )
            # Re-process with fresh result
            tool_ids          = []
            missing_tool_ids  = []
            skill_ids         = []
            for spec in result.get("tools", []):
                tid, is_new = _upsert_tool(spec, vlt)
                tool_ids.append(tid)
                if is_new:
                    missing_tool_ids.append(tid)
            for spec in result.get("skills", []):
                sid = _upsert_skill(spec, scroll.id, vlt)
                skill_ids.append(sid)
            missing_tool_names = []
            for tid in missing_tool_ids:
                try:
                    missing_tool_names.append(vlt.get_tool(tid).name)
                except KeyError:
                    missing_tool_names.append(tid)
        except Exception as exc:
            logger.error("[Extract] Retry extraction also failed for '%s': %s", scroll.name, exc)

    if not skill_ids and not tool_ids:
        logger.error(
            "[Extract] Extraction returned empty skills and tools for scroll '%s' "
            "after both primary and retry passes. Scroll reset to PENDING_APPROVAL.",
            scroll.name,
        )
        from systemu.interface.notifications import log_event as _log_event
        _log_event(
            "ERROR", "scroll",
            f"Extraction failed for '{scroll.name}': LLM returned no skills or tools "
            "after retry. Review the scroll's narrative/objectives and re-approve.",
            {"scroll_id": scroll.id},
        )
        scroll.status = ScrollStatus.PENDING_APPROVAL
        vlt.save_scroll(scroll)
        return None

    # ── Stage 4: Create Activity ───────────────────────────────────────────
    # v0.8.16: stamp the trigger origin from the scroll source so every
    # downstream event partitions into the right pane.  A chat scroll
    # (source_session_id == "chat") → "chat"; any other source (a recorded
    # capture session) → "capture".  direct_task re-asserts "chat" after this
    # for the chat path; decide_shadow propagates this value into submit().
    _origin = "chat" if getattr(scroll, "source_session_id", "") == "chat" else "capture"
    activity = Activity(
        id=generate_id("activity"),
        name=scroll.name,
        scroll_id=scroll.id,
        required_tool_ids=tool_ids,
        required_skill_ids=skill_ids,
        missing_tools=missing_tool_names,
        status=ActivityStatus.PARTIAL if missing_tool_ids else ActivityStatus.UNASSIGNED,
        origin=_origin,
        # v0.6.0-f: freeze the scroll's intent on the activity so Stage 5
        # (shadow tiebreak) can do semantic matching without re-loading the
        # scroll on every decision.
        intent_snapshot=getattr(scroll, "intent", "") or "",
    )
    vlt.save_activity(activity)

    # Update scroll: ACTIVE (activity extracted; shadow assignment follows in Stage 5)
    # LINKED is set by shadow_decision once a shadow is successfully assigned.
    scroll.activity_id = activity.id
    scroll.status      = ScrollStatus.ACTIVE
    vlt.save_scroll(scroll)

    logger.info(
        "[Extract] Activity '%s' created — skills=%d tools=%d missing=%d",
        activity.name, len(skill_ids), len(tool_ids), len(missing_tool_ids),
    )

    # ── Stage 4b: Queue forge notifications (Gate 1 entry point for users) ──
    if missing_tool_ids:
        _queue_forge_notifications(missing_tool_ids, activity, scroll, vlt)

    # ── Stage 4b1: Pre-flight credential scan (v0.8.18) ───────────────────────
    # Heads-up only: scan the deployed tools' declared credentials and, if any
    # are unresolved, post ONE batched decision so the operator can connect them
    # up front. Gate-4 (tool_registry.execute) is the actual run-time enforcement.
    _queue_credential_requests(tool_ids, activity_id=activity.id, vlt=vlt)

    # ── Stage 4b2: Output-type coverage check ─────────────────────────────────
    # Warn when the selected tools don't appear to cover the scroll's expected
    # outputs. This catches the "wrong tool" scenario where the LLM picks an
    # existing tool by surface-keyword match but its output type doesn't match
    # the objective (e.g. web_screenshot chosen for a CSV-extraction task).
    _check_tool_output_coverage(scroll, tool_ids, vlt)

    # ── Stage 4c: Auto-forge (DEV escape hatch — bypasses all security gates) ─
    if missing_tool_ids and cfg.auto_forge_tools:
        from systemu.pipelines.tool_forge import forge_proposed_tools
        from systemu.pipelines.tool_service import enable_tool
        forged = forge_proposed_tools(activity, cfg, vlt)
        for t in forged:
            enable_tool(t.id, vlt)
            # Advisory dep reminder — auto-forge bypasses the UI toggle so the
            # notification would otherwise never be queued for these tools.
            try:
                from systemu.interface.notifications import queue_dependency_reminder
                queue_dependency_reminder(t, vlt)
            except Exception:
                pass
        logger.warning(
            "[Extract] auto_forge_tools active — %d tool(s) forged and enabled without review",
            len(forged),
        )
        # Heal the activity: all tools are now deployed, so PARTIAL is no longer correct.
        # Without this, the Stage 5 guard (`activity.status != PARTIAL`) would permanently
        # block decide_shadow() even though all required tools are ready.
        if forged:
            activity.status = ActivityStatus.UNASSIGNED
            activity.missing_tools = []
            vlt.save_activity(activity)
            logger.info(
                "[Extract] auto_forge healed activity '%s' → UNASSIGNED (%d tools deployed)",
                activity.name, len(forged),
            )

    # ── Stage 5: Shadow Decision ───────────────────────────────────────────
    # Skipped when skip_shadow_decision=True (direct_task.py owns the call).
    # Skipped for PARTIAL activities — re-entered via _heal_activities_for_tool()
    # once their tools are deployed, or routed to Wild Card by decide_shadow().
    if not skip_shadow_decision and activity.status != ActivityStatus.PARTIAL:
        from systemu.pipelines.shadow_decision import decide_shadow
        decide_shadow(activity, cfg, vlt)

    return activity


# ─── Helpers ──────────────────────────────────────────────────────────────────

# ── v0.6.0-d catalog enrichment helpers ────────────────────────────────────

def _summarise_schema(schema: dict) -> dict:
    """Strip a JSON Schema to {field: type} pairs (max 20 fields).

    Keeps the LLM's catalog payload bounded.  Used for both
    `parameters_schema` and `return_schema` of every catalog tool.
    """
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") if "properties" in schema else schema
    if not isinstance(props, dict):
        return {}
    out: dict = {}
    for k, v in list(props.items())[:20]:
        if isinstance(v, dict):
            out[k] = str(v.get("type") or v.get("$type") or "any")[:30]
        else:
            out[k] = "any"
    return out


def _enrich_tool_for_catalog(t: dict, vlt: Vault) -> dict:
    """v0.6.1-d: prefer index header's schema summaries (cheap, no I/O).
    v0.8.22: when summary fields are empty, fall back to the per-tool body
    JSON so the LLM sees real schemas for tools whose index header pre-dates
    the summary fields (very common in user vaults)."""
    params = t.get("parameters_schema_summary") or {}
    returns = t.get("return_schema_summary") or {}
    if not params or not returns:
        tid = t.get("id")
        try:
            from pathlib import Path
            body_path = Path(vlt.root) / "tools" / f"tool_{tid}.json"
            if body_path.exists():
                body = json.loads(body_path.read_text(encoding="utf-8"))
                params = params or body.get("parameters_schema") or {}
                returns = returns or body.get("return_schema") or {}
        except Exception:
            pass
    return {
        "id":                t.get("id", ""),
        "name":              t.get("name", ""),
        "description":       (t.get("description") or "")[:200],
        "parameters_schema": params,
        "return_schema":     returns,
    }


def _enrich_skill_for_catalog(s: dict) -> dict:
    """Build a catalog entry for one skill.  v0.6.0-d.5 intent-contract
    fields (`target_outcomes`, `produces`) default to empty lists when the
    skill predates Stage 3.5 (e.g., starter-pack skills before migration).
    """
    return {
        "id":              s.get("id", ""),
        "name":            s.get("name", ""),
        "description":     (s.get("description") or "")[:200],
        "target_outcomes": s.get("target_outcomes") or [],
        "produces":        s.get("produces") or [],
    }


def _upsert_skill(spec: dict, scroll_id: str, vlt: Vault) -> str:
    """Return skill_id: reuse existing or create new following Anthropic Agent Skills standard."""
    existing_id = spec.get("existing_id")
    is_new      = spec.get("is_new", True)

    if not is_new and existing_id:
        try:
            skill = vlt.get_skill(existing_id)
            updated = False
            if scroll_id not in skill.evidence_scroll_ids:
                skill.evidence_scroll_ids.append(scroll_id)
                updated = True
            new_instructions = spec.get("instructions_md", "")
            if new_instructions and len(new_instructions) > len(skill.instructions_md):
                skill.instructions_md = new_instructions
                updated = True
            if updated:
                vlt.save_skill(skill)
            logger.debug("[Extract] Reused skill %s (%s)", existing_id, spec.get("name"))
            return existing_id
        except KeyError:
            logger.warning("[Extract] existing_id %s not found — creating new skill", existing_id)

    # Deterministic name guard: if a skill with this name already exists, reuse it —
    # regardless of what the LLM decided. Prevents duplicates when the LLM mis-names a match.
    name_match = vlt.find_skill_by_name(spec.get("name", ""))
    if name_match:
        if scroll_id not in name_match.evidence_scroll_ids:
            name_match.evidence_scroll_ids.append(scroll_id)
            vlt.save_skill(name_match)
        logger.debug("[Extract] Name dedup: reusing existing skill %s (%s)", name_match.id, name_match.name)
        return name_match.id

    # Keep the raw tool names from the LLM spec (used verbatim in SKILL.md frontmatter)
    spec_tool_names: List[str] = spec.get("required_tools", [])

    # Resolve IDs for internal vault linking (best-effort; names stay canonical)
    all_tools = vlt.load_index("tools")
    required_tool_ids: List[str] = []
    for tool_name in spec_tool_names:
        match = next((t for t in all_tools if t.get("name") == tool_name), None)
        if match:
            required_tool_ids.append(match["id"])
        else:
            logger.debug("[Extract] Skill required_tool '%s' not yet in vault", tool_name)

    skill = Skill(
        id=generate_id("skill"),
        name=spec.get("name", "unknown_skill"),
        description=spec.get("description", ""),
        category=spec.get("category", "general"),
        proficiency_level=spec.get("proficiency_level", "intermediate"),
        evidence_scroll_ids=[scroll_id],
        required_tool_ids=required_tool_ids,
        required_tool_names=spec_tool_names,
        instructions_md=spec.get("instructions_md", ""),
        # v0.6.0-d.5: pick up the intent-contract fields the LLM emitted
        # under the new extract_skills_tools.md schema.  Default to empty
        # lists when absent so backward-compat with older prompts works.
        target_outcomes=list(spec.get("target_outcomes") or []),
        produces=list(spec.get("produces") or []),
    )
    vlt.save_skill(skill)
    logger.debug("[Extract] Created skill %s (%s) with %d required tools", skill.id, skill.name, len(required_tool_ids))
    return skill.id



def _upsert_tool(spec: dict, vlt: Vault) -> tuple[str, bool]:
    """Return (tool_id, is_new): reuse existing or register as PROPOSED."""
    existing_id = spec.get("existing_id")
    is_new      = spec.get("is_new", True)

    if not is_new and existing_id:
        try:
            tool = vlt.get_tool(existing_id)
            updated = False
            new_desc = spec.get("description", "")
            if new_desc and len(new_desc) > len(tool.description):
                tool.description = new_desc
                updated = True
            new_notes = spec.get("implementation_notes", "")
            if new_notes and len(new_notes) > len(tool.implementation_notes):
                tool.implementation_notes = new_notes
                updated = True
            if updated:
                vlt.save_tool(tool)   # also regenerates TOOL.md
            logger.debug("[Extract] Reused tool %s (%s)", existing_id, spec.get("name"))
            return existing_id, False
        except KeyError:
            logger.warning("[Extract] existing_id %s not found — creating new tool", existing_id)

    # v0.8.22.1 (Fix 1a): reuse ANY existing same-name tool, regardless of
    # enabled/status, so the activity converges on a single tool id and the
    # forge flow never spawns duplicates. is_new only when the tool genuinely
    # still needs forging (PROPOSED with no code yet). A forged/deployed-but-
    # disabled tool is NOT "missing" — direct_task's readiness gate (Fix 1b)
    # parks the activity as waiting_on_tools so the operator enables it.
    name_match = vlt.find_tool_by_name(spec.get("name", ""))
    if name_match:
        still_needs_forge = (
            name_match.status == ToolStatus.PROPOSED
            and not getattr(name_match, "implementation_path", None)
        )
        logger.debug("[Extract] Name dedup: reusing %s (%s) status=%s enabled=%s needs_forge=%s",
                     name_match.id, name_match.name, name_match.status,
                     name_match.enabled, still_needs_forge)
        return name_match.id, still_needs_forge

    tool = Tool(
        id=generate_id("tool"),
        name=spec.get("name", "unknown_tool"),
        description=spec.get("description", ""),
        # v0.8.13: pass raw; Tool's before-validator coerces (web->api_call, unknown/None->python_function).
        tool_type=spec.get("tool_type"),
        parameters_schema=spec.get("parameters_schema", {}),
        return_schema=spec.get("return_schema", {}),
        implementation_notes=spec.get("implementation_notes", ""),
        dependencies=spec.get("dependencies", []),
        status=ToolStatus.PROPOSED,
        forged_by_systemu=True,
    )
    vlt.save_tool(tool)
    logger.debug("[Extract] Registered tool %s (%s) as PROPOSED — impl_notes=%s",
                 tool.id, tool.name, bool(tool.implementation_notes))
    return tool.id, True


def _queue_forge_notifications(
    tool_ids: List[str],
    activity: "Activity",
    scroll: "Scroll",
    vlt: Vault,
) -> None:
    """Queue one rich Notification per PROPOSED tool.

    Each notification carries the full tool spec in its context dict so
    the UI can render an editable JSON card and trigger forge on approval.
    No blocking I/O — returns immediately.
    """
    from systemu.core.models import Notification
    from systemu.core.utils import generate_id
    import json as _json

    for tid in tool_ids:
        try:
            tool = vlt.get_tool(tid)
        except KeyError:
            continue

        # Build a human-readable summary of the tool spec
        param_names = list(tool.parameters_schema.keys()) if tool.parameters_schema else []
        deps_str = ", ".join(tool.dependencies) if tool.dependencies else "none"
        message = (
            f"Tool: {tool.name}\n"
            f"Type: {tool.tool_type}\n"
            f"Description: {tool.description}\n"
            f"Parameters: {', '.join(param_names) or 'none'}\n"
            f"Dependencies: {deps_str}\n"
            f"Scroll: {scroll.name}\n\n"
            f"Review and edit the tool definition JSON below before approving forge."
        )

        notif = Notification(
            id=generate_id("notif"),
            title=f"🔧 Forge Tool: {tool.name}",
            message=message,
            # v0.6.1-b: safe-default first (auto-skip in non-interactive mode)
            actions=["Skip", "Forge"],
            context={
                "notification_type": "forge_tool",
                "tool_id":           tool.id,
                "activity_id":       activity.id,
                "scroll_id":         scroll.id,
                "tool_spec_json":    _json.dumps(tool.model_dump(mode="json"), indent=2),
            },
        )
        try:
            vlt.queue_notification(notif)
            from systemu.interface.notifications import log_event
            log_event("INFO", "tool",
                      f"Tool '{tool.name}' queued for forge — awaiting user approval",
                      {"tool_id": tool.id, "activity_id": activity.id})
            logger.info("[Extract] Queued forge notification for tool '%s' (%s)", tool.name, tid)
        except Exception as exc:
            logger.warning("[Extract] Could not queue forge notification for %s: %s", tid, exc)


def _get_decision_queue():
    try:
        from systemu.interface.notifications import _get_decision_queue as _g
        return _g()
    except Exception:
        return None


def _queue_credential_requests(tool_ids, *, activity_id: str, vlt) -> None:
    """v0.8.18 pre-flight: post ONE batched decision listing all missing
    credentials across the run's tools (heads-up; Gate-4 enforces at run time)."""
    from systemu.runtime.credentials.resolver import CredentialResolver
    resolver = CredentialResolver()
    missing, seen = [], set()
    for tid in tool_ids:
        try:
            tool = vlt.get_tool(tid)
        except Exception:
            continue
        for req in (getattr(tool, "requires_credentials", None) or []):
            if req.key in seen:
                continue
            if resolver.resolve(req)[0] is None:
                seen.add(req.key)
                missing.append(req)
    if not missing:
        return
    queue = _get_decision_queue()
    if queue is None:
        return
    lines = [f"- {r.label} ({r.key})" + (f" - {r.signup_url}" if r.signup_url else "") for r in missing]
    queue.post(
        title=f"This task needs {len(missing)} credential(s)",
        body="Connect these on the Connections page (Settings -> Connections), then re-run:\n" + "\n".join(lines),
        options=["I've connected them", "Proceed anyway", "Cancel run"],
        context={"kind": "credential_batch", "keys": [r.key for r in missing]},
        dedup_key=f"creds:{activity_id}",
    )


# ─── Dependency singleton (avoids passing config/vault through every call) ────

_config: Config | None = None
_vault:  Vault  | None = None


def _check_tool_output_coverage(scroll: "Scroll", tool_ids: list, vlt: "Vault") -> None:
    """Warn when the selected tools don't appear to cover the scroll's output objectives.

    Heuristic only — does not block the pipeline. Logs a WARNING and queues a
    notification so the operator can review the extraction before the activity runs.

    Checks each objective that expects a file or data output against the combined
    descriptions of all selected tools. If no tool description contains write/save/
    export/generate keywords for a file objective, or fetch/extract/parse keywords
    for a data objective, flag it.
    """
    import json as _json

    try:
        objectives = _json.loads(scroll.objectives) if isinstance(scroll.objectives, str) else (scroll.objectives or [])
    except Exception:
        return

    # Gather tool descriptions once
    tool_descriptions: list[str] = []
    for tid in tool_ids:
        try:
            t = vlt.get_tool(tid)
            tool_descriptions.append(f"{t.name} {t.description}".lower())
        except KeyError:
            pass
    combined = " ".join(tool_descriptions)

    FILE_KEYWORDS  = {"write", "save", "export", "output", "create", "generate", "dump"}
    DATA_KEYWORDS  = {"fetch", "extract", "parse", "scrape", "read", "load", "request", "query"}

    gaps: list[str] = []
    for obj in objectives:
        if not isinstance(obj, dict):
            continue
        output_type = obj.get("output_type", "")
        goal        = obj.get("goal", "?")

        if output_type == "file":
            if not any(kw in combined for kw in FILE_KEYWORDS):
                gaps.append(f"Objective '{goal[:80]}' expects a FILE output but no selected tool writes/saves files.")
        elif output_type == "data":
            if not any(kw in combined for kw in DATA_KEYWORDS):
                gaps.append(f"Objective '{goal[:80]}' expects DATA output but no selected tool fetches/parses data.")

    if gaps:
        from systemu.interface.notifications import log_event, notify_user
        gap_text = "\n".join(f"  - {g}" for g in gaps)
        logger.warning(
            "[Extract] Tool-output coverage gap detected for scroll '%s':\n%s",
            scroll.name, gap_text,
        )
        log_event(
            "WARNING", "tool",
            f"Possible wrong-tool selection for '{scroll.name}': {len(gaps)} objective(s) may lack correct tools.",
            {"scroll_id": scroll.id, "gaps": gaps},
        )
        notify_user(
            title="Review Tool Selection",
            message=(
                f"The activity extractor may have selected incorrect tools for:\n"
                f"  Scroll: \"{scroll.name}\"\n\n"
                f"Gaps detected:\n{gap_text}\n\n"
                f"If these tools are wrong, re-approve the scroll to re-run extraction."
            ),
            actions=["OK"],
            context={
                "notification_type": "tool_coverage_warning",
                "scroll_id": scroll.id,
                "gaps": gaps,
            },
        )


def init_pipeline(config: Config, vault: Vault) -> None:
    """Called once at startup to inject shared dependencies."""
    global _config, _vault
    _config = config
    _vault  = vault


def _resolve_deps(
    config: Config | None,
    vault:  Vault  | None,
) -> tuple[Config, Vault]:
    cfg = config or _config
    vlt = vault  or _vault
    if cfg is None or vlt is None:
        raise RuntimeError(
            "Pipeline dependencies not initialised. "
            "Call systemu.pipelines.activity_extractor.init_pipeline(config, vault) first."
        )
    return cfg, vlt
