"""Invalidate stale dep-failure lessons when a tool starts succeeding.

v0.3.3 added self-healing for tool pip dependencies.  Once the dep
installer lands a missing package, the affected tool can succeed —
but the Shadow already wrote a "use PDF / alternative format instead"
lesson to its memory buffer based on the prior failure.  Left alone,
that obsolete lesson keeps nudging the LLM away from the now-working
tool until enough new successes have outweighed it.

This module closes the loop: when ``ShadowRuntime`` observes a tool
returning ``success=True`` and the Shadow's memory buffer contains a
``failure_patterns`` lesson that looks like it was triggered by a
missing pip dependency for THAT tool, append a single contradicting
entry that the memory consolidator will see on its next pass.

The detection is intentionally conservative:

* We only fire on tools known to have raised ``missing_dependency`` /
  ``dependency_install_*`` previously (this is signalled by the
  ``previously_failed`` argument from the caller — the caller maintains
  the per-runtime set in ``_dep_failed_tools``).
* For an across-process signal, we also do a *text* match against the
  shadow's existing buffer entries for the tool name + "missing" /
  "install" / "dependency" keywords.  Both signals are OR'd: either is
  sufficient to fire the invalidation.
* We never write more than one invalidation per (shadow, tool) per
  process — the second + subsequent successes are silent.

Format of the invalidation entry (a normal SHADOW_CLAIM_TYPES /
``failure_patterns`` entry plus a few sentinel fields the consolidator
recognises):

    {
      "category": "failure_patterns",
      "lesson":   "Tool 'create_word_doc' succeeded after a prior dep
                   failure was resolved via the installer. Prior advice
                   to switch formats / avoid this tool is OBSOLETE.",
      "evidence_action_blocks": [],
      "_invalidates": ["create_word_doc"],
      "_resolved_via": "dep_installer"
    }
"""

from __future__ import annotations

import logging
import re
from threading import Lock
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set

if TYPE_CHECKING:
    from systemu.core.models import Shadow
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

# Process-wide set of (shadow_id, tool_name) tuples we've already
# invalidated for, so we never write the same lesson twice in one
# session.  Survives until daemon restart — the consolidator will have
# absorbed the entry by then.
_already_invalidated: Set[tuple] = set()
_lock = Lock()

# Words that, when paired with the tool name in a failure_patterns
# lesson, mark it as likely-dep-related.  Tuned conservatively — we'd
# rather miss an invalidation than write a confusing one.
_DEP_KEYWORDS = (
    "missing",
    "install",
    "dependency",
    "module",
    "package",
    "not available",
    "not installed",
    "import",
    "pip",
)


def maybe_invalidate_dep_lesson(
    vault: "Vault",
    shadow: "Shadow",
    tool_name: str,
    *,
    previously_failed: bool,
    execution_id: Optional[str] = None,
) -> bool:
    """If the shadow's buffer has a stale dep-failure lesson for ``tool_name``,
    append a contradiction.  Returns True when an invalidation was written.

    Args:
        vault:              Vault for buffer read/write.
        shadow:             Shadow whose memory we're updating.
        tool_name:          Tool that just succeeded.
        previously_failed:  True when this process saw the same tool
                            return a dep-failure error_type earlier in
                            the same run.  An in-process signal that
                            an invalidation is warranted even if the
                            buffer is empty (rare).
        execution_id:       Optional current exec id, recorded as
                            evidence on the invalidation entry.
    """
    key = (shadow.id, tool_name)
    with _lock:
        if key in _already_invalidated:
            return False

    try:
        _md, entries = vault.load_shadow_memory(shadow.id)
    except Exception:
        logger.exception("[MemoryInvalidator] load_shadow_memory failed for %s", shadow.id)
        return False

    stale_indices = _find_stale_dep_lessons(entries, tool_name)
    if not stale_indices and not previously_failed:
        return False

    # Don't write a second invalidation if one already exists.
    if _has_invalidation_for(entries, tool_name):
        with _lock:
            _already_invalidated.add(key)
        return False

    lesson = _compose_invalidation(
        tool_name=tool_name,
        stale_count=len(stale_indices),
        within_run=previously_failed,
    )
    entry = {
        "category":               "failure_patterns",
        "lesson":                 lesson,
        "evidence_action_blocks": [],
        "_invalidates":           [tool_name],
        "_resolved_via":          "dep_installer",
        "_superseded_indices":    stale_indices,
        "_execution_id":          execution_id,
    }

    try:
        vault.append_shadow_memory_buffer(shadow.id, entry, source="dep_resolved")
    except Exception:
        logger.exception(
            "[MemoryInvalidator] append_shadow_memory_buffer failed for shadow %s tool %s",
            shadow.id, tool_name,
        )
        return False

    with _lock:
        _already_invalidated.add(key)
    logger.info(
        "[MemoryInvalidator] Wrote dep-success invalidation for shadow=%s tool=%s "
        "(superseded %d stale lesson(s))",
        shadow.id, tool_name, len(stale_indices),
    )
    return True


def reset_for_tests() -> None:
    """Clear the de-dup cache.  ONLY for tests."""
    with _lock:
        _already_invalidated.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Internals

def _find_stale_dep_lessons(entries: List[Dict], tool_name: str) -> List[int]:
    """Return indices of buffer entries that look like dep-failure lessons
    for ``tool_name``.  Conservative: requires the tool name AND at least
    one dep-keyword to co-occur in the lesson text.
    """
    needle = tool_name.lower()
    result: List[int] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("category") != "failure_patterns":
            continue
        if entry.get("_invalidates"):
            # Already an invalidation entry — don't double-flag.
            continue
        text = (entry.get("lesson") or "").lower()
        if needle in text and any(k in text for k in _DEP_KEYWORDS):
            result.append(i)
    return result


def _has_invalidation_for(entries: List[Dict], tool_name: str) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        invalidates: Iterable = entry.get("_invalidates") or []
        if tool_name in invalidates:
            return True
    return False


def _compose_invalidation(*, tool_name: str, stale_count: int, within_run: bool) -> str:
    """Build the lesson text.  Short, factual, names the resolution."""
    base = (
        f"Tool '{tool_name}' has now succeeded after the prior missing-dependency "
        f"failure was resolved via the v0.3.3 dependency installer. "
    )
    if stale_count:
        base += (
            f"The {stale_count} earlier failure-pattern lesson(s) for this tool "
            "are OBSOLETE — do NOT route around this tool any more; call it normally. "
        )
    if within_run:
        base += "(Both the failure and the recovery occurred in the current run.) "
    base += "Recorded automatically by memory_invalidator."
    # Hard cap to honour buffer line-size budgeting.
    return base[:500]
