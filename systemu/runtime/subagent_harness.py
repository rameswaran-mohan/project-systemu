"""Plan 0 Build 3 (Task 3.3 — paper fleet): subagent child builders.

Helpers that materialise the per-child execution objects when a parent Shadow
delegates a sub-objective to a forked subagent:

  * :func:`build_child_shadow` — derive a child Shadow from its parent, stripping
    any delegate/spawn_subagent tool so children cannot recurse (Hermes
    non-recursion invariant), while inheriting the parent's skills and persona.
  * :func:`build_child_activity` — persist a single-objective child Scroll for a
    subtask plus the Activity that references it, both saved to the vault.

These are pure constructors over the canonical ``systemu.core.models`` types —
no LLM calls. The runtime drives them; here we only build + persist.
"""
from __future__ import annotations

import logging

from systemu.core.models import (
    Activity,
    ActivityStatus,
    Objective,
    Scroll,
    ScrollStatus,
    Shadow,
    ShadowStatus,
)
from systemu.core.utils import generate_id

logger = logging.getLogger(__name__)

# Tool ids/names that grant delegation. A child must never inherit these —
# otherwise it could spawn its own children (infinite recursion / fan-out).
# Mirrors ``systemu.runtime.tools.delegate._compute_child_whitelist``.
_DELEGATE_TOOL_NAMES = frozenset({"delegate", "spawn_subagent"})


def build_child_shadow(parent_shadow: Shadow, child_id: str) -> Shadow:
    """Build a child Shadow derived from ``parent_shadow``.

    The child:
      * has a distinct id (``shadow_<hex>`` — ``child_id`` is recorded in the
        description for traceability, not used as the raw id);
      * inherits the parent's ``available_tool_ids`` MINUS any delegate/
        spawn_subagent tool (hard non-recursion);
      * inherits the parent's ``skill_ids`` and persona (``system_prompt`` is a
        computed field over ``identity_block``, so we copy the parent's resolved
        ``system_prompt`` into the child's ``identity_block``).

    Never raises for the strip step — best-effort over whatever the parent holds.
    """
    inherited_tools = [
        t for t in (parent_shadow.available_tool_ids or [])
        if t not in _DELEGATE_TOOL_NAMES
    ]

    return Shadow(
        id=generate_id("shadow"),
        name=f"{parent_shadow.name} · child {child_id}",
        description=(
            f"Subagent child ({child_id}) forked from shadow "
            f"{parent_shadow.id} for a delegated sub-objective."
        ),
        # system_prompt is computed from identity_block; carry the parent's
        # resolved prompt verbatim so the child speaks with the same persona.
        identity_block=parent_shadow.system_prompt,
        available_tool_ids=inherited_tools,
        skill_ids=list(parent_shadow.skill_ids or []),
        status=ShadowStatus.ACTIVE,
    )


def build_child_activity(
    parent_activity: Activity,
    subtask: str,
    child_id: str,
    vault,
) -> Activity:
    """Build + persist a child Scroll (single objective) and Activity for ``subtask``.

    A child Scroll is created with exactly one :class:`Objective` whose ``goal``
    is ``subtask``, then a child :class:`Activity` referencing that scroll.
    Both are saved to ``vault`` and the Activity is returned.

    Tool/skill requirements are inherited from ``parent_activity`` so the child
    activity carries the same execution context for the narrower goal.
    """
    scroll = Scroll(
        id=generate_id("scroll"),
        name=f"Subtask · {child_id}",
        source_session_id=parent_activity.scroll_id,
        raw_instructions_path="",
        narrative_md=subtask,
        intent=subtask,
        objectives=[
            Objective(id=1, goal=subtask, success_criteria=subtask),
        ],
        status=ScrollStatus.APPROVED,
    )

    activity = Activity(
        id=generate_id("activity"),
        name=f"Subtask activity · {child_id}",
        scroll_id=scroll.id,
        required_tool_ids=list(parent_activity.required_tool_ids or []),
        required_skill_ids=list(parent_activity.required_skill_ids or []),
        status=ActivityStatus.ASSIGNED,
        intent_snapshot=subtask,
    )

    try:
        vault.save_scroll(scroll)
        vault.save_activity(activity)
    except Exception:
        # Best-effort persistence — match the codebase's defensive ledger style.
        # The caller still receives the constructed Activity even if the write
        # hiccuped, so the in-memory fleet can proceed.
        logger.exception(
            "[SubagentHarness] could not persist child scroll/activity for %s",
            child_id,
        )

    return activity
