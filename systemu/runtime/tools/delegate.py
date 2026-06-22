"""v0.9.5 L6 delegate.spawn_subagent — first-class fork-as-tool.

The delegate/subagent pattern: the agent calls this tool to decompose a goal
and dispatch sub-goals to forked Tier-1 subagents. Each child:
- Inherits parent's tool whitelist MINUS 'delegate'/'spawn_subagent' (no recursion)
- Runs up to ``Config.delegate_max_turns_per_child`` iterations
- Reports back a structured summary

For v0.9.5: the child is a simplified Tier-1 LLM call (planning + Q&A).
Full recursive ShadowRuntime as the child runtime is v0.9.6+ work.

dynamic_schema_overrides: the tool's description in the LLM catalog reflects
the live ``Config.delegate_max_depth`` + ``delegate_max_turns_per_child`` so
the model sees accurate constraints at every render.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from systemu.core.llm_router import llm_call_json
from systemu.runtime.tool_registry_v2 import registry

logger = logging.getLogger(__name__)


def _compute_child_whitelist(parent_whitelist: Set[str]) -> Set[str]:
    """Delegation invariant: child sees parent_whitelist minus delegate-class tools.

    Eliminates 'delegate' AND 'spawn_subagent' (both names point at this fn).
    """
    excluded = {"delegate", "spawn_subagent"}
    return {name for name in parent_whitelist if name not in excluded}


def spawn_subagent(
    *,
    task: str,
    config,
    parent_depth: int = 0,
    parent_whitelist: Optional[Set[str]] = None,
    max_turns: Optional[int] = None,
) -> Dict[str, Any]:
    """Spawn a child Tier-1 LLM to handle ``task``.

    Returns a dict shaped:
        {
          "success": bool,
          "depth": int,            # new depth (parent_depth + 1)
          "summary": str,          # child's final summary
          "key_findings": List[str],
          "error": str (only on failure),
        }
    """
    max_depth = int(getattr(config, "delegate_max_depth", 3))
    if parent_depth >= max_depth:
        return {
            "success": False,
            "depth": parent_depth,
            "error": f"max_depth reached (parent_depth={parent_depth}, max={max_depth})",
        }

    child_depth = parent_depth + 1
    turns = max_turns or int(getattr(config, "delegate_max_turns_per_child", 20))

    child_whitelist = _compute_child_whitelist(parent_whitelist or set())

    system_prompt = (
        "You are a delegated subagent. You have NO conversation history with the "
        "parent agent — work from the task description alone.\n\n"
        f"Depth: {child_depth} (max {max_depth}).\n"
        f"Turn budget: {turns}.\n"
        "Tool whitelist: {whitelist}.\n\n"
        "Return strict JSON:\n"
        "{{\n"
        "  \"summary\": \"<1-3 sentence summary of what you accomplished>\",\n"
        "  \"key_findings\": [\"<finding 1>\", \"<finding 2>\"]\n"
        "}}"
    ).format(whitelist=", ".join(sorted(child_whitelist)) or "<none>")

    try:
        result = llm_call_json(
            tier=1,
            system=system_prompt,
            user=task,
            config=config,
            max_tokens=600,
            temperature=0.2,
        )
    except Exception as exc:
        logger.warning("[Delegate] child (depth=%d) failed: %s", child_depth, exc)
        return {
            "success": False,
            "depth": child_depth,
            "error": f"child LLM unavailable: {exc}",
        }

    if not isinstance(result, dict):
        return {
            "success": False,
            "depth": child_depth,
            "error": f"child returned non-dict: {type(result).__name__}",
        }

    summary = str(result.get("summary", ""))
    findings = result.get("key_findings") or []
    if not isinstance(findings, list):
        findings = []
    findings = [str(f) for f in findings][:10]

    return {
        "success": True,
        "depth": child_depth,
        "summary": summary[:1500],
        "key_findings": findings,
    }


# ── Tool registration ─────────────────────────────────────────────────

_DELEGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The sub-goal to delegate. Self-contained — child has no history.",
        },
    },
    "required": ["task"],
}


def _delegate_handler(**kwargs) -> Dict[str, Any]:
    from sharing_on.config import Config
    cfg = Config.from_env()
    return spawn_subagent(
        task=kwargs.get("task", ""),
        config=cfg,
        parent_depth=int(kwargs.get("_parent_depth", 0)),
    )


def _delegate_dynamic_schema():
    """The dynamic-schema pattern: surface current Config limits in the tool
    description so the model sees accurate constraints at LLM-render time."""
    from sharing_on.config import Config
    try:
        cfg = Config.from_env()
        depth = int(getattr(cfg, "delegate_max_depth", 3))
        turns = int(getattr(cfg, "delegate_max_turns_per_child", 20))
        concurrent = int(getattr(cfg, "delegate_max_concurrent_children", 2))
    except Exception:
        depth, turns, concurrent = 3, 20, 2
    return {
        "description": (
            f"Delegate a self-contained sub-goal to a forked subagent. "
            f"Current limits: max_depth={depth}, max_turns_per_child={turns}, "
            f"max_concurrent_children={concurrent}. Child has no conversation "
            f"history and CANNOT call delegate (no recursion)."
        ),
    }


registry.register(
    name="spawn_subagent", toolset="delegate",
    schema=_DELEGATE_SCHEMA, handler=_delegate_handler,
    dynamic_schema_overrides=_delegate_dynamic_schema,
    description="Delegate a sub-goal to a forked Tier-1 subagent.",
    is_action_tool=False,
    max_result_size_chars=10_000,
)
