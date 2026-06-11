"""ParallelVault — dual-write adapter for zero-downtime file→SQLite migration.

Usage during migration (set SYSTEMU_STORAGE=parallel):
  1. All WRITES go to both primary (FileVault) and secondary (SqliteVault).
  2. All READS come from primary.
  3. On every read, the primary result is shadow-checked against secondary
     and any mismatches are logged as WARNINGS — never raises, never blocks.
  4. Once you're confident the SQLite vault is consistent, flip primary to
     SqliteVault and remove ParallelVault from the stack.

This is the "Strangler Fig" shadow-write pattern:
  - Zero risk: primary vault stays in charge of all reads.
  - Gradual validation: mismatches surface before the cutover.
  - Atomic cutover: flip one env var, no data migration needed.

Limitations:
  - NOT meant for production high-throughput — secondary writes are
    synchronous and add latency.  Use in migration windows only.
  - Memory operations (shadow/elder) write to both but only read from primary
    since they're large blobs that change frequently.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from systemu.core.models import (
    Activity, ActivityStatus,
    Evolution, EvolutionStatus,
    Notification,
    Scroll, ScrollStatus,
    Shadow, ShadowStatus,
    Skill,
    Tool, ToolStatus,
)

logger = logging.getLogger(__name__)


class ParallelVault:
    """Dual-write IVault proxy.

    Args:
        primary:   IVault that is authoritative for reads (typically FileVault).
        secondary: IVault that receives shadow writes (typically SqliteVault).
    """

    def __init__(self, primary: Any, secondary: Any) -> None:
        self._p = primary
        self._s = secondary

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _write_secondary(self, method: str, *args, **kwargs) -> None:
        """Call secondary vault method, log but never propagate exceptions."""
        try:
            getattr(self._s, method)(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "[ParallelVault] Secondary.%s failed (non-fatal): %s", method, exc
            )

    def _check_mismatch(self, entity_id: str, primary_result: Any, method: str) -> None:
        """Shadow-read from secondary and warn on mismatch (best-effort)."""
        # Only compare JSON-serialisable fields at the dict level to avoid
        # datetime / type coercion noise.  Never raises.
        try:
            secondary_result = getattr(self._s, method)(entity_id)
            p_dump = _safe_dump(primary_result)
            s_dump = _safe_dump(secondary_result)
            if p_dump != s_dump:
                logger.warning(
                    "[ParallelVault] Mismatch on %s(%r): primary=%s secondary=%s",
                    method, entity_id,
                    _diff_keys(p_dump, s_dump),
                    "(secondary)",
                )
        except KeyError:
            logger.warning(
                "[ParallelVault] %s(%r) missing in secondary vault — write may have failed",
                method, entity_id,
            )
        except Exception as exc:
            logger.debug("[ParallelVault] Shadow-read check failed (non-fatal): %s", exc)

    # ── load_index ────────────────────────────────────────────────────────────

    def load_index(self, entity: str) -> List[Dict[str, Any]]:
        return self._p.load_index(entity)

    # ── Scroll ────────────────────────────────────────────────────────────────

    def save_scroll(self, scroll: Scroll) -> None:
        self._p.save_scroll(scroll)
        self._write_secondary("save_scroll", scroll)

    def get_scroll(self, scroll_id: str) -> Scroll:
        result = self._p.get_scroll(scroll_id)
        self._check_mismatch(scroll_id, result, "get_scroll")
        return result

    def list_scrolls(self, status: Optional[ScrollStatus] = None) -> List[Dict[str, Any]]:
        return self._p.list_scrolls(status)

    # ── Skill ─────────────────────────────────────────────────────────────────

    def save_skill(self, skill: Skill) -> None:
        self._p.save_skill(skill)
        self._write_secondary("save_skill", skill)

    def get_skill(self, skill_id: str) -> Skill:
        result = self._p.get_skill(skill_id)
        self._check_mismatch(skill_id, result, "get_skill")
        return result

    def find_skill_by_name(self, name: str) -> Optional[Skill]:
        return self._p.find_skill_by_name(name)

    def list_skills(self) -> List[Dict[str, Any]]:
        return self._p.list_skills()

    # ── Tool ──────────────────────────────────────────────────────────────────

    def save_tool(self, tool: Tool) -> None:
        self._p.save_tool(tool)
        self._write_secondary("save_tool", tool)

    def get_tool(self, tool_id: str) -> Tool:
        result = self._p.get_tool(tool_id)
        self._check_mismatch(tool_id, result, "get_tool")
        return result

    def find_tool_by_name(self, name: str) -> Optional[Tool]:
        return self._p.find_tool_by_name(name)

    def list_tools(self, status: Optional[ToolStatus] = None) -> List[Dict[str, Any]]:
        return self._p.list_tools(status)

    # ── Activity ──────────────────────────────────────────────────────────────

    def save_activity(self, activity: Activity) -> None:
        self._p.save_activity(activity)
        self._write_secondary("save_activity", activity)

    def get_activity(self, activity_id: str) -> Activity:
        result = self._p.get_activity(activity_id)
        self._check_mismatch(activity_id, result, "get_activity")
        return result

    def list_activities(self, status: Optional[ActivityStatus] = None) -> List[Dict[str, Any]]:
        return self._p.list_activities(status)

    # ── Shadow ────────────────────────────────────────────────────────────────

    def save_shadow(self, shadow: Shadow) -> None:
        self._p.save_shadow(shadow)
        self._write_secondary("save_shadow", shadow)

    def get_shadow(self, shadow_id: str) -> Shadow:
        result = self._p.get_shadow(shadow_id)
        self._check_mismatch(shadow_id, result, "get_shadow")
        return result

    def list_shadows(self, status: Optional[ShadowStatus] = None) -> List[Dict[str, Any]]:
        return self._p.list_shadows(status)

    # ── Shadow memory ─────────────────────────────────────────────────────────

    def save_shadow_memory(self, shadow_id: str, memory_md: str) -> None:
        self._p.save_shadow_memory(shadow_id, memory_md)
        self._write_secondary("save_shadow_memory", shadow_id, memory_md)

    def load_shadow_memory(self, shadow_id: str) -> Tuple[str, List[Dict[str, Any]]]:
        return self._p.load_shadow_memory(shadow_id)

    def append_memory_buffer(self, shadow_id: str, entry: Dict[str, Any]) -> None:
        self._p.append_memory_buffer(shadow_id, entry)
        self._write_secondary("append_memory_buffer", shadow_id, entry)

    def clear_memory_buffer(self, shadow_id: str) -> None:
        self._p.clear_memory_buffer(shadow_id)
        self._write_secondary("clear_memory_buffer", shadow_id)

    # Memory tier gate-keepers (v0.2.2).  Validation runs on primary; the
    # augmented entry is mirrored to the secondary using its low-level
    # append method (skip the secondary gate-keeper — the primary has
    # already validated, double-validation would be wasted work).
    def append_shadow_memory_buffer(
        self, shadow_id: str, entry: Dict[str, Any], *, source: str,
    ) -> Dict[str, Any]:
        augmented = self._p.append_shadow_memory_buffer(
            shadow_id, entry, source=source,
        )
        self._write_secondary("append_memory_buffer", shadow_id, augmented)
        return augmented

    def append_elder_buffer(
        self, entry: Dict[str, Any], *, source: str,
    ) -> Dict[str, Any]:
        augmented = self._p.append_elder_buffer(entry, source=source)
        self._write_secondary("append_elder_memory_buffer", augmented)
        return augmented

    def prune_old_executions(self, max_keep: int = 50) -> int:
        return self._p.prune_old_executions(max_keep)

    # ── Elder / global memory ─────────────────────────────────────────────────

    def save_elder_memory(self, md_text: str) -> None:
        self._p.save_elder_memory(md_text)
        self._write_secondary("save_elder_memory", md_text)

    def load_elder_memory(self) -> str:
        return self._p.load_elder_memory()

    def append_elder_memory_buffer(self, entry: Dict[str, Any]) -> None:
        self._p.append_elder_memory_buffer(entry)
        self._write_secondary("append_elder_memory_buffer", entry)

    def load_elder_memory_buffer(self) -> List[Dict[str, Any]]:
        return self._p.load_elder_memory_buffer()

    def clear_elder_memory_buffer(self) -> None:
        self._p.clear_elder_memory_buffer()
        self._write_secondary("clear_elder_memory_buffer")

    def load_global_memory(self) -> str:
        return self._p.load_global_memory()

    def save_global_memory(self, md_text: str) -> None:
        self._p.save_global_memory(md_text)
        self._write_secondary("save_global_memory", md_text)

    def append_global_memory_buffer(self, entry: Dict[str, Any]) -> None:
        self._p.append_global_memory_buffer(entry)
        self._write_secondary("append_global_memory_buffer", entry)

    def clear_global_memory_buffer(self) -> None:
        self._p.clear_global_memory_buffer()
        self._write_secondary("clear_global_memory_buffer")

    # ── Chat history ──────────────────────────────────────────────────────────

    def append_chat_history(self, entry: Dict[str, Any]) -> None:
        self._p.append_chat_history(entry)
        self._write_secondary("append_chat_history", entry)

    def load_chat_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._p.load_chat_history(limit)

    def update_chat_history_entry(self, ts: str, updates: Dict[str, Any]) -> None:
        self._p.update_chat_history_entry(ts, updates)
        self._write_secondary("update_chat_history_entry", ts, updates)

    def get_latest_chat_scroll(self):
        return self._p.get_latest_chat_scroll()

    # ── Evolution ─────────────────────────────────────────────────────────────

    def save_evolution(self, evolution: Evolution) -> None:
        self._p.save_evolution(evolution)
        self._write_secondary("save_evolution", evolution)

    def get_evolution(self, evolution_id: str) -> Evolution:
        result = self._p.get_evolution(evolution_id)
        self._check_mismatch(evolution_id, result, "get_evolution")
        return result

    def list_evolutions(self, status: Optional[EvolutionStatus] = None) -> List[Dict[str, Any]]:
        return self._p.list_evolutions(status)

    # ── Notification ──────────────────────────────────────────────────────────

    def queue_notification(self, notification: Notification) -> None:
        self._p.queue_notification(notification)
        self._write_secondary("queue_notification", notification)

    def resolve_notification(self, notification_id: str, resolution: str) -> None:
        self._p.resolve_notification(notification_id, resolution)
        self._write_secondary("resolve_notification", notification_id, resolution)

    def list_pending_notifications(self) -> List[Dict[str, Any]]:
        return self._p.list_pending_notifications()

    # ── Decisions (OperatorDecisionQueue backing) ─────────────────────────────
    #
    # Wave 1.5: wrapper drift — the decisions surface (v0.8.0) and the
    # episodic surface (v0.9.2) were added to the inner Vault + FileVault but
    # never to this adapter, so in SYSTEMU_STORAGE=parallel mode the Inbox
    # could not read/write OperatorDecision records and episodic capture
    # failed with AttributeError.  Writes dual-write (primary + best-effort
    # secondary); reads come from primary, matching every method above.

    def save_decision(self, decision) -> None:
        self._p.save_decision(decision)
        self._write_secondary("save_decision", decision)

    def get_decision(self, decision_id: str):
        return self._p.get_decision(decision_id)

    # ── Episodic memory (v0.9.2 session summaries) ───────────────────────────

    def append_session_summary(self, summary) -> None:
        self._p.append_session_summary(summary)
        self._write_secondary("append_session_summary", summary)

    def query_session_summaries(self, **kwargs):
        return self._p.query_session_summaries(**kwargs)

    def search_session_summaries(self, query: str, **kwargs):
        return self._p.search_session_summaries(query, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  Diff helpers (non-blocking, best-effort)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_dump(obj: Any) -> Dict[str, Any]:
    """Convert a Pydantic model or dict to a plain dict for comparison."""
    try:
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        return dict(obj) if obj else {}
    except Exception:
        return {}


def _diff_keys(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    """Return list of keys where a and b differ."""
    all_keys = set(a.keys()) | set(b.keys())
    return [k for k in all_keys if a.get(k) != b.get(k)]
