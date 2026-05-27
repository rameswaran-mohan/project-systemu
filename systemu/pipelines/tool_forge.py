"""Pipeline C — Tool Forge.

Two-step process using Tier 2 for both steps:
  Step 1 (Spec):  LLM designs the tool's interface (parameters, return schema)
  Step 2 (Code):  LLM writes the complete Python implementation
  Gate:           User must confirm before code is written and saved

Also exposes:
  forge_proposed_tools(activity, ...) — forge all PROPOSED tools for an activity
  forge_tool_by_name(name, ...) — manually trigger forge from CLI
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from sharing_on.config import Config
from systemu.core.llm_router import llm_call_json
from systemu.core.models import Activity, Scroll, Tool, ToolStatus, ToolType
from systemu.core.utils import generate_id, load_prompt
from systemu.interface.notifications import notify_user
from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


def _approved_packages_hint() -> list[str]:
    """Return the operator-approved pip allow-list as a plain string list.

    Surfaced in the forge prompt so the LLM prefers already-approved
    packages when there's a real choice.  Fail-quiet — an absent /
    unreadable approval store yields an empty list and the forge
    proceeds with no preference (today's behaviour).
    """
    try:
        from systemu.runtime.dep_approvals import DepApprovalStore
        store = DepApprovalStore(Path("data") / "dep_approvals.json")
        return [entry["package"] for entry in store.list_approved()]
    except Exception:
        logger.debug("[Forge] could not read approved-deps allow-list", exc_info=True)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Public entry points
# ─────────────────────────────────────────────────────────────────────────────

def forge_proposed_tools(
    activity: Activity,
    config: Config,
    vault: Vault,
) -> list[Tool]:
    """Forge all PROPOSED tools linked to an Activity without confirmation.

    Used only by the auto_forge_tools dev escape hatch. Bypasses notify_user()
    and calls _generate_and_save_code() directly. Callers are responsible for
    emitting the security warning before invoking this.
    """
    forged: list[Tool] = []
    for tool_id in activity.required_tool_ids:
        try:
            tool = vault.get_tool(tool_id)
        except KeyError:
            continue
        if tool.status == ToolStatus.PROPOSED:
            try:
                scroll = vault.get_scroll(activity.scroll_id)
            except KeyError:
                from systemu.core.models import Scroll as ScrollModel
                scroll = ScrollModel(
                    id="stub", name=tool.name, source_session_id="auto",
                    raw_instructions_path="", narrative_md=tool.description,
                )
            try:
                result = _generate_and_save_code(tool, scroll, config, vault)
            except Exception as exc:
                logger.error("[Forge] Unexpected error forging '%s': %s", tool.name, exc)
                result = None
            if result:
                forged.append(result)
    return forged


def preview_tool_code(
    tool: Tool,
    scroll: Scroll,
    config: Config,
) -> Optional[str]:
    """Gate 2 — generate implementation for human review. Does NOT write to disk.

    Returns the generated Python source string, or None on failure.
    Called from the UI after the user approves the spec (Gate 1). The returned
    code is shown in a read-only panel. Only after the user clicks
    'Approve & Sign Off' does save_approved_code() persist it.
    """
    logger.info("[Forge] Generating preview for '%s' (not saving yet) ...", tool.name)
    try:
        code_result = llm_call_json(
            tier=2,
            system=load_prompt("forge_tool_code.md"),
            user=json.dumps({
                "tool_spec":      tool.model_dump(mode="json"),
                "scroll_context": scroll.narrative_md,
            }),
            config=config,
            temperature=0.1,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.error("[Forge] LLM call failed during preview for '%s': %s", tool.name, exc)
        return None
    implementation = code_result.get("implementation", "").strip()
    if not implementation:
        logger.error("[Forge] Preview generation returned empty code for '%s'", tool.name)
        return None
    return implementation


def save_approved_code(
    tool: Tool,
    implementation: str,
    config: Config,
    vault: Vault,
) -> Tool:
    """Gate 2 final step — persist user-approved code. Sets enabled=False.

    Called after the user reads the code preview and clicks 'Approve & Sign Off'.
    The tool is written to vault with status=FORGED and enabled=False.
    The user must then explicitly enable it in the Tools Registry page (Gate 3).
    """
    from systemu.interface.notifications import log_event

    # v0.6.1-a: defense in depth — Pydantic guards Tool.name at construction,
    # but any non-Pydantic path supplying a tool (e.g. direct attribute mutation,
    # legacy deserialisation) still triggers this guard.  Catch BEFORE any
    # filesystem operation so a malicious name can't write outside impl_dir.
    from systemu.core.models import _SAFE_TOOL_NAME
    if not _SAFE_TOOL_NAME.match(tool.name or ""):
        raise ValueError(
            f"Refusing to write tool with unsafe name: {tool.name!r}"
        )

    impl_dir  = Path(config.vault_dir) / "tools" / "implementations"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_path = impl_dir / f"{tool.name}.py"
    impl_path.write_text(implementation, encoding="utf-8")

    tool.implementation_path = str(impl_path.relative_to(Path(config.vault_dir).parent))
    tool.status  = ToolStatus.FORGED
    tool.enabled = False   # Default-deny — user must flip toggle in Tools Registry
    vault.save_tool(tool)

    logger.info("[Forge] Tool '%s' approved & saved → %s (enabled=False)", tool.name, impl_path)
    log_event(
        "SUCCESS", "tool",
        f"Tool '{tool.name}' approved by user → FORGED (disabled until toggled ON)",
        {"tool_id": tool.id, "impl_path": str(impl_path)},
    )

    # v0.5.0-a: dry-run gate.  Tool stays disabled either way (operator must
    # still toggle ON), but a failed dry-run blocks the registry from
    # serving the tool even if the operator does enable it.  Operator can
    # use the "Re-forge with feedback" button on the Tools page to retry.
    try:
        from systemu.pipelines.tool_dry_run import dry_run_tool
        dr = dry_run_tool(tool, vault=vault, config=config)
        tool.dry_run_status = dr.status
        tool.dry_run_evidence = dr.to_evidence()
        vault.save_tool(tool)
        log_event(
            "SUCCESS" if dr.success else "WARNING",
            "tool",
            f"Tool '{tool.name}' dry-run {dr.status}"
            + (f" ({dr.error[:120]})" if dr.error else ""),
            {"tool_id": tool.id, "dry_run_status": dr.status,
             "elapsed_ms": dr.elapsed_ms},
        )
    except Exception as exc:
        logger.exception("[Forge] dry-run hook errored — tool left at not_run")
        try:
            log_event(
                "WARNING", "tool",
                f"Tool '{tool.name}' dry-run hook errored: {exc}",
                {"tool_id": tool.id},
            )
        except Exception:
            pass

    return tool


def forge_tool_from_spec(
    tool_id: str,
    edited_spec_json: str,
    config: Config,
    vault: Vault,
) -> Optional[Tool]:
    """Forge a tool from a user-edited spec JSON string.

    This is the UI-triggered path. The user has already approved in the
    notifications page, so we skip the confirm gate and go straight to code gen.

    Args:
        tool_id:         The vault ID of the PROPOSED tool.
        edited_spec_json: The full tool spec as edited by the user (JSON string).
        config:          Config instance.
        vault:           Vault instance.

    Returns:
        The updated Tool on success, or None on failure.
    """
    import json as _json

    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        logger.error("[Forge] Tool %s not found in vault", tool_id)
        return None

    # Apply user's edits to the Tool model
    try:
        edited = _json.loads(edited_spec_json)
        if "name" in edited:
            tool.name = edited["name"]
        if "description" in edited:
            tool.description = edited["description"]
        if "parameters_schema" in edited:
            tool.parameters_schema = edited["parameters_schema"]
        if "return_schema" in edited:
            tool.return_schema = edited["return_schema"]
        if "implementation_notes" in edited:
            tool.implementation_notes = edited["implementation_notes"]
        if "dependencies" in edited:
            tool.dependencies = edited["dependencies"]
        # Parse tool_type safely
        if "tool_type" in edited:
            try:
                tool.tool_type = ToolType(edited["tool_type"])
            except ValueError:
                pass
        vault.save_tool(tool)
        logger.info("[Forge] Applied user edits to tool '%s'", tool.name)
    except Exception as exc:
        logger.warning("[Forge] Could not apply edits to tool spec: %s — using original", exc)

    # Get scroll context (best-effort — use stub if not found)
    scroll: Optional[Scroll] = None
    for a_header in vault.list_activities():
        if tool.id in (a_header.get("required_tool_ids") or []):
            try:
                act    = vault.get_activity(a_header["id"])
                scroll = vault.get_scroll(act.scroll_id)
                break
            except KeyError:
                continue

    if scroll is None:
        from systemu.core.models import Scroll as ScrollModel
        scroll = ScrollModel(
            id="stub", name=tool.name, source_session_id="ui",
            raw_instructions_path="", narrative_md=tool.description,
        )

    # Code generation — no confirmation gate (user already approved via UI)
    return _generate_and_save_code(tool, scroll, config, vault)


def forge_tool_by_name(
    tool_name: str,
    config: Config,
    vault: Vault,
    *,
    context_hint: str = "",
) -> Optional[Tool]:
    """Manually forge or re-forge a tool by name (used from CLI).

    If the tool already exists in the vault, re-forge its implementation.
    If not, first generate its specification then forge.
    """
    existing = vault.find_tool_by_name(tool_name)
    if existing:
        # Get the first scroll referencing any activity that uses this tool
        activities = vault.list_activities()
        scroll: Optional[Scroll] = None
        for a_header in activities:
            if tool_name in (a_header.get("missing_tools") or []) or \
               existing.id in (a_header.get("required_tool_ids") or []):
                try:
                    act = vault.get_activity(a_header["id"])
                    scroll = vault.get_scroll(act.scroll_id)
                    break
                except KeyError:
                    continue

        if scroll is None:
            # Create a stub scroll for context
            from systemu.core.models import Scroll as ScrollModel
            scroll = ScrollModel(
                id="stub", name=tool_name, source_session_id="manual",
                raw_instructions_path="", narrative_md=context_hint,
            )
        return forge_tool(existing, scroll, config, vault)
    else:
        # v0.6.0-e: when we have a real scroll (not a stub), forward intent
        # context so the spec LLM can design schemas that fit the chain.
        scroll_intent = ""
        scroll_expected_outcome = ""
        requesting_objective: Optional[Dict[str, Any]] = None
        try:
            activities = vault.list_activities()
            for a_header in activities:
                if tool_name in (a_header.get("missing_tools") or []):
                    try:
                        act = vault.get_activity(a_header["id"])
                        sc  = vault.get_scroll(act.scroll_id)
                        scroll_intent = getattr(sc, "intent", "") or ""
                        scroll_expected_outcome = getattr(sc, "expected_outcome", "") or ""
                        # First objective whose hints / goal mentions this tool name,
                        # or fall back to the first objective.
                        objs = getattr(sc, "objectives", []) or []
                        match = next(
                            (o for o in objs
                             if tool_name in (str(o.goal or "") + str(o.hints or ""))),
                            objs[0] if objs else None,
                        )
                        if match:
                            requesting_objective = {
                                "id":   getattr(match, "id", None),
                                "goal": getattr(match, "goal", ""),
                                "success_criteria": getattr(match, "success_criteria", ""),
                                "output_type":      getattr(match, "output_type", ""),
                            }
                        break
                    except Exception:
                        continue
        except Exception:
            pass

        return _spec_and_forge_new(
            tool_name, context_hint, config, vault,
            scroll_intent=scroll_intent,
            scroll_expected_outcome=scroll_expected_outcome,
            requesting_objective=requesting_objective,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Core forge logic
# ─────────────────────────────────────────────────────────────────────────────

def forge_tool(
    tool: Tool,
    scroll: Scroll,
    config: Config,
    vault: Vault,
) -> Optional[Tool]:
    """Forge a single tool: show user the spec, confirm, generate code, save.

    Returns the updated Tool on success, or None if user skipped.
    """
    logger.info("[Forge] Proposing tool '%s' for scroll '%s'", tool.name, scroll.name)

    # ── User confirmation gate (CLI path) ─────────────────────────────────
    choice = notify_user(
        title="Forge New Tool?",
        message=(
            f"Tool: [bold]{tool.name}[/bold]\n"
            f"Type: {tool.tool_type}\n"
            f"Description: {tool.description}\n"
            f"Dependencies: {', '.join(tool.dependencies) or 'none'}\n\n"
            f"Context scroll: {scroll.name}"
        ),
        # v0.6.1-b: safe-default first (auto-skip in non-interactive mode)
        actions=["Skip", "Forge"],
        # v0.8.0 Pattern 1: dedup_key routes the decision to the dashboard
        # /insights → Pending Actions queue when SYSTEMU_DECISION_QUEUE=true.
        # PendingOperatorDecision propagates up to the CLI wrapper (Task 9).
        dedup_key=f"tool_forge:{tool.id}",
    )

    if choice.lower() != "forge":
        logger.info("[Forge] User skipped forging '%s'", tool.name)
        return None

    return _generate_and_save_code(tool, scroll, config, vault)


def _generate_and_save_code(
    tool: Tool,
    scroll: Scroll,
    config: Config,
    vault: Vault,
) -> Optional[Tool]:
    """Core code-generation step. Shared by forge_tool() and forge_tool_from_spec()."""
    from systemu.interface.notifications import log_event, notify_user

    logger.info("[Forge] Generating implementation for '%s' ...", tool.name)

    # ── LLM call — isolated so a transient failure doesn't kill a batch ──────
    try:
        code_result = llm_call_json(
            tier=2,
            system=load_prompt("forge_tool_code.md"),
            user=json.dumps({
                "tool_spec":      tool.model_dump(mode="json"),
                "scroll_context": scroll.narrative_md,
            }),
            config=config,
            temperature=0.1,
            max_tokens=8192,
        )
    except Exception as exc:
        logger.error("[Forge] LLM call failed for '%s': %s", tool.name, exc)
        log_event("ERROR", "tool", f"Forge failed — LLM call error for '{tool.name}': {exc}",
                  {"tool_id": tool.id})
        notify_user(
            title="Forge Failed — Retry Needed",
            message=(
                f"Tool '{tool.name}' could not be forged.\n"
                f"Reason: LLM call error — {exc}\n\n"
                f"The tool remains PROPOSED. Open Tools Registry and click "
                f"'Review & Forge' on '{tool.name}' to retry."
            ),
            actions=["OK"],
            context={"notification_type": "forge_retry", "tool_id": tool.id},
        )
        return None

    implementation = code_result.get("implementation", "")
    if not implementation or not implementation.strip():
        logger.error("[Forge] LLM returned empty implementation for '%s'", tool.name)
        log_event("ERROR", "tool", f"Forge failed — empty implementation for '{tool.name}'",
                  {"tool_id": tool.id})
        notify_user(
            title="Forge Failed — Retry Needed",
            message=(
                f"Tool '{tool.name}' could not be forged.\n"
                f"Reason: LLM returned an empty implementation.\n\n"
                f"The tool remains PROPOSED. Open Tools Registry and click "
                f"'Review & Forge' on '{tool.name}' to retry."
            ),
            actions=["OK"],
            context={"notification_type": "forge_retry", "tool_id": tool.id},
        )
        return None

    # ── Syntax smoke-check — catch LLM-generated broken Python before it hits disk ─
    try:
        compile(implementation, f"{tool.name}.py", "exec")
    except SyntaxError as exc:
        logger.error("[Forge] Generated code for '%s' has a syntax error: %s", tool.name, exc)
        log_event("ERROR", "tool",
                  f"Forge failed — syntax error in generated code for '{tool.name}': {exc}",
                  {"tool_id": tool.id, "syntax_error": str(exc)})
        notify_user(
            title="Forge Failed — Syntax Error",
            message=(
                f"Tool '{tool.name}' generated code has a syntax error:\n"
                f"  {exc}\n\n"
                f"The tool remains PROPOSED. Open Tools Registry and click "
                f"'Review & Forge' on '{tool.name}' to retry."
            ),
            actions=["OK"],
            context={"notification_type": "forge_retry", "tool_id": tool.id},
        )
        return None

    # Write implementation file
    impl_dir  = Path(config.vault_dir) / "tools" / "implementations"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl_path = impl_dir / f"{tool.name}.py"
    impl_path.write_text(implementation, encoding="utf-8")

    # Update tool record — explicitly gate enable to False so prior state can't leak through
    tool.implementation_path = str(impl_path.relative_to(Path(config.vault_dir).parent))
    tool.status  = ToolStatus.FORGED
    tool.enabled = False   # Gate 3: user must explicitly enable in Tools Registry
    vault.save_tool(tool)

    logger.info("[Forge] Tool '%s' forged → %s", tool.name, impl_path)
    log_event("SUCCESS", "tool",
              f"Tool '{tool.name}' forged successfully → {impl_path.name}",
              {"tool_id": tool.id, "impl_path": str(impl_path)})
    return tool


def _spec_and_forge_new(
    tool_name: str,
    context_hint: str,
    config: Config,
    vault: Vault,
    *,
    scroll_intent: str = "",
    scroll_expected_outcome: str = "",
    requesting_objective: Optional[Dict[str, Any]] = None,
    downstream_consumer: Optional[Dict[str, Any]] = None,
) -> Optional[Tool]:
    """Create a new tool from scratch: spec → confirm → code → save.

    v0.6.0-e (Stage 4): the spec LLM now receives the requesting scroll's
    intent + expected_outcome + the specific objective whose execution
    needs this tool, plus the next-objective's input shape if any.  This
    lets the forge design ``parameters_schema`` + ``return_schema`` that
    actually fit the data-flow chain, not just the bare tool name.
    """
    logger.info("[Forge] Generating spec for new tool '%s' ...", tool_name)

    # ── Step 1: Design spec (Tier 2) ──────────────────────────────────────
    # Surface the operator-approved pip allow-list to the spec LLM so it
    # prefers already-approved packages when there's a choice (e.g. it has
    # no reason to invent `python-docx-extra` when `python-docx` is already
    # in the allow-list).  This is advisory only — novel deps still pass
    # through the runtime PROMPT gate.
    spec_payload: Dict[str, Any] = {
        "tool_name":          tool_name,
        "scroll_narrative":   context_hint,
        "preferred_packages": _approved_packages_hint(),
    }
    # v0.6.0-e: intent + objective context (omit when empty so older callers
    # still produce identical prompts — the forge_tool_spec.md prompt treats
    # the new keys as optional).
    if scroll_intent:
        spec_payload["scroll_intent"] = scroll_intent
    if scroll_expected_outcome:
        spec_payload["scroll_expected_outcome"] = scroll_expected_outcome
    if requesting_objective:
        spec_payload["requesting_objective"] = requesting_objective
    if downstream_consumer:
        spec_payload["downstream_consumer"] = downstream_consumer

    spec_result = llm_call_json(
        tier=2,
        system=load_prompt("forge_tool_spec.md"),
        user=json.dumps(spec_payload),
        config=config,
        temperature=0.2,
        max_tokens=2048,
    )

    # Parse tool_type safely
    raw_type = spec_result.get("tool_type", "python_function")
    try:
        tool_type = ToolType(raw_type)
    except ValueError:
        tool_type = ToolType.PYTHON_FUNCTION

    tool = Tool(
        id=generate_id("tool"),
        name=spec_result.get("name", tool_name),
        description=spec_result.get("description", ""),
        tool_type=tool_type,
        parameters_schema=spec_result.get("parameters_schema", {}),
        return_schema=spec_result.get("return_schema", {}),
        implementation_notes=spec_result.get("implementation_notes", ""),
        dependencies=spec_result.get("dependencies", []),
        status=ToolStatus.PROPOSED,
        forged_by_systemu=True,
    )
    vault.save_tool(tool)

    # Create a minimal scroll stub for context
    from systemu.core.models import Scroll as ScrollModel
    stub_scroll = ScrollModel(
        id="stub", name=tool_name, source_session_id="manual",
        raw_instructions_path="", narrative_md=context_hint,
    )
    return forge_tool(tool, stub_scroll, config, vault)


def propose_tools_from_specs(
    specs: list,                # list[ProposedToolSpec] from validator
    scroll,                     # the Scroll being refined
    config: Config,
    vault: Vault,
) -> list:
    """Create Tool records (status=PROPOSED) from validator-emitted specs.

    Does NOT generate implementation code — that's the second step (handled by
    ``forge_proposed_tools_from_specs`` or by the operator clicking "Forge" on
    /tools page).  Each Tool record has a fully-populated spec
    (parameters_schema, return_schema, implementation_notes) generated via the
    Tier-2 LLM from the validator's hints, so the operator can review the
    spec before approving the forge.

    v0.8.1 (Pattern 3): replaces the previous "silently drop validator specs
    unless SYSTEMU_AUTO_FORGE_TOOLS=true" bridge with an operator-reviewable
    surface.  Returns the list of Tool records created (may be empty if all
    specs duplicated existing tools or LLM spec generation failed for each).
    """
    if not specs:
        return []
    proposed: list = []
    existing_names = set()
    try:
        for t in (vault.load_index("tools") or []):
            n = t.get("name")
            if n:
                existing_names.add(n)
    except Exception:
        logger.debug("[Forge] could not read tool index for de-dup", exc_info=True)

    for spec in specs:
        name = getattr(spec, "name", "") or ""
        if not name:
            logger.debug("[Forge] skipping spec with empty name")
            continue
        if name in existing_names:
            logger.info("[Forge] skipping spec '%s' — tool already exists", name)
            continue

        # Generate spec via LLM (Tier 2) and save with status=PROPOSED.
        spec_payload: Dict[str, Any] = {
            "tool_name":          name,
            "scroll_narrative":   (getattr(scroll, "narrative_md", "") or "")[:500],
            "preferred_packages": _approved_packages_hint(),
            "validator_hint":     {
                "description":     getattr(spec, "description", ""),
                "tool_type":       getattr(spec, "tool_type", "cli_command"),
                "parameter_hints": getattr(spec, "parameter_hints", []),
                "output_hint":     getattr(spec, "output_hint", ""),
                "rationale":       getattr(spec, "rationale", ""),
            },
        }
        try:
            spec_result = llm_call_json(
                tier=2,
                system=load_prompt("forge_tool_spec.md"),
                user=json.dumps(spec_payload),
                config=config,
                temperature=0.2,
                max_tokens=2048,
            )
        except Exception:
            logger.exception("[Forge] spec generation failed for '%s'", name)
            continue

        try:
            tool_type = ToolType(spec_result.get("tool_type", spec.tool_type))
        except ValueError:
            tool_type = ToolType.PYTHON_FUNCTION

        tool = Tool(
            id=generate_id("tool"),
            name=spec_result.get("name", name),
            description=spec_result.get("description", spec.description),
            tool_type=tool_type,
            parameters_schema=spec_result.get("parameters_schema", {}),
            return_schema=spec_result.get("return_schema", {}),
            implementation_notes=spec_result.get("implementation_notes", ""),
            dependencies=spec_result.get("dependencies", []),
            status=ToolStatus.PROPOSED,
            forged_by_systemu=True,
        )
        try:
            vault.save_tool(tool)
            proposed.append(tool)
            existing_names.add(tool.name)
            logger.info(
                "[Forge] PROPOSED tool '%s' (id=%s) from validator spec for scroll '%s'",
                tool.name, tool.id, getattr(scroll, "name", "?"),
            )
        except Exception:
            logger.exception("[Forge] could not save proposed tool '%s'", name)

    return proposed


def forge_proposed_tools_from_specs(
    specs: list,                # list[ProposedToolSpec] from validator
    scroll,                     # the Scroll being refined
    config: Config,
    vault: Vault,
) -> list:
    """Forge tools from validator-emitted ProposedToolSpec records.

    v0.7.3 Bug #14 fix — used by scroll_refiner's auto-forge bridge to attempt
    creating missing tools before validator-blocking the scroll.

    Bypasses the operator-confirmation gate (notify_user in forge_tool) because
    this path is only reached when SYSTEMU_AUTO_FORGE_TOOLS=true — the operator
    has already opted into the dev escape hatch. Mirrors how the existing
    ``forge_proposed_tools`` (called from activity_extractor) calls
    ``_generate_and_save_code`` directly.

    v0.8.1: now delegates the spec-creation step to ``propose_tools_from_specs``
    (shared with the v0.8.1 operator-review bridge), then code-generates the
    proposed tools in-place.

    Returns the list of successfully forged Tool objects (may be empty).
    """
    if not specs:
        return []
    # Step 1: spec + propose (creates Tool records with status=PROPOSED)
    proposed = propose_tools_from_specs(specs, scroll, config, vault)
    if not proposed:
        return []
    # Step 2: code-generate each PROPOSED tool from Step 1 (BYPASS the
    # notify_user gate; auto-forge mode has already opted into the dev
    # escape hatch)
    forged: list = []
    for tool in proposed:
        try:
            from systemu.core.models import Scroll as ScrollModel
            stub_scroll = ScrollModel(
                id="stub", name=tool.name, source_session_id="auto-forge-bridge",
                raw_instructions_path="",
                narrative_md=(getattr(scroll, "narrative_md", "") or "")[:500],
            )
            result = _generate_and_save_code(tool, stub_scroll, config, vault)
            if result is not None:
                forged.append(result)
                logger.info(
                    "[Forge] validator-spec bridge forged tool '%s' (id=%s)",
                    result.name, getattr(result, "id", "?"),
                )
        except Exception:
            logger.exception("[Forge] code generation failed for proposed tool '%s'", tool.name)

    return forged
