"""FileVault — IVault adapter around the existing file-based Vault.

Zero behaviour change.  Every method delegates 1-to-1 to the underlying
Vault instance so that call sites using IVault are decoupled from the
concrete Vault class without any risk of subtle behaviour drift.

Usage:
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault

    raw  = Vault(config.vault_dir)
    vault: IVault = FileVault(raw)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from systemu.core.models import (
    Activity, ActivityStatus,
    Evolution, EvolutionStatus,
    Notification,
    Scroll, ScrollStatus,
    Shadow, ShadowStatus,
    Skill,
    Tool, ToolStatus,
)
from systemu.vault.vault import Vault


class FileVault:
    """IVault implementation backed by the original JSON file store."""

    def __init__(self, vault: Vault) -> None:
        self._v = vault
        # Expose .root so any code that checks vault.root still works
        self.root = vault.root

    # ── Index ─────────────────────────────────────────────────────────────────

    def load_index(self, entity: str) -> List[Dict[str, Any]]:
        return self._v.load_index(entity)

    # ── Scroll ────────────────────────────────────────────────────────────────

    def save_scroll(self, scroll: Scroll) -> None:
        self._v.save_scroll(scroll)

    def get_scroll(self, scroll_id: str) -> Scroll:
        return self._v.get_scroll(scroll_id)

    def list_scrolls(self, status: Optional[ScrollStatus] = None) -> List[Dict[str, Any]]:
        return self._v.list_scrolls(status)

    # ── Skill ─────────────────────────────────────────────────────────────────

    def save_skill(self, skill: Skill) -> None:
        self._v.save_skill(skill)

    def get_skill(self, skill_id: str) -> Skill:
        return self._v.get_skill(skill_id)

    def find_skill_by_name(self, name: str) -> Optional[Skill]:
        return self._v.find_skill_by_name(name)

    def list_skills(self) -> List[Dict[str, Any]]:
        return self._v.list_skills()

    # ── Tool ──────────────────────────────────────────────────────────────────

    def save_tool(self, tool: Tool) -> None:
        self._v.save_tool(tool)

    def get_tool(self, tool_id: str) -> Tool:
        return self._v.get_tool(tool_id)

    def find_tool_by_name(self, name: str) -> Optional[Tool]:
        return self._v.find_tool_by_name(name)

    def list_tools(self, status: Optional[ToolStatus] = None) -> List[Dict[str, Any]]:
        return self._v.list_tools(status)

    # ── Activity ──────────────────────────────────────────────────────────────

    def save_activity(self, activity: Activity) -> None:
        self._v.save_activity(activity)

    def get_activity(self, activity_id: str) -> Activity:
        return self._v.get_activity(activity_id)

    def list_activities(self, status: Optional[ActivityStatus] = None) -> List[Dict[str, Any]]:
        return self._v.list_activities(status)

    # ── Shadow ────────────────────────────────────────────────────────────────

    def save_shadow(self, shadow: Shadow) -> None:
        self._v.save_shadow(shadow)

    def get_shadow(self, shadow_id: str) -> Shadow:
        return self._v.get_shadow(shadow_id)

    def list_shadows(self, status: Optional[ShadowStatus] = None) -> List[Dict[str, Any]]:
        return self._v.list_shadows(status)

    def prune_old_executions(self, max_keep: int = 50) -> int:
        return self._v.prune_old_executions(max_keep)

    # ── Shadow memory ─────────────────────────────────────────────────────────

    def save_shadow_memory(self, shadow_id: str, memory_md: str) -> None:
        self._v.save_shadow_memory(shadow_id, memory_md)

    def load_shadow_memory(self, shadow_id: str) -> tuple[str, List[Dict[str, Any]]]:
        return self._v.load_shadow_memory(shadow_id)

    def append_memory_buffer(self, shadow_id: str, entry: Dict[str, Any]) -> None:
        self._v.append_memory_buffer(shadow_id, entry)

    def clear_memory_buffer(self, shadow_id: str) -> None:
        self._v.clear_memory_buffer(shadow_id)

    # Memory tier gate-keepers (v0.2.2) — forward to the wrapped Vault.
    def append_shadow_memory_buffer(
        self, shadow_id: str, entry: Dict[str, Any], *, source: str,
    ) -> Dict[str, Any]:
        return self._v.append_shadow_memory_buffer(shadow_id, entry, source=source)

    def append_elder_buffer(
        self, entry: Dict[str, Any], *, source: str,
    ) -> Dict[str, Any]:
        return self._v.append_elder_buffer(entry, source=source)

    # ── Elder / Global memory ─────────────────────────────────────────────────

    def load_elder_memory(self) -> str:
        return self._v.load_elder_memory()

    def save_elder_memory(self, md_text: str) -> None:
        self._v.save_elder_memory(md_text)

    def append_elder_memory_buffer(self, entry: Dict[str, Any]) -> None:
        self._v.append_elder_memory_buffer(entry)

    def load_elder_memory_buffer(self) -> List[Dict[str, Any]]:
        return self._v.load_elder_memory_buffer()

    def clear_elder_memory_buffer(self) -> None:
        self._v.clear_elder_memory_buffer()

    def load_global_memory(self) -> str:
        return self._v.load_global_memory()

    def save_global_memory(self, md_text: str) -> None:
        self._v.save_global_memory(md_text)

    def append_global_memory_buffer(self, entry: Dict[str, Any]) -> None:
        self._v.append_global_memory_buffer(entry)

    def clear_global_memory_buffer(self) -> None:
        self._v.clear_global_memory_buffer()

    # ── Chat history ──────────────────────────────────────────────────────────

    def append_chat_history(self, entry: Dict[str, Any]) -> None:
        self._v.append_chat_history(entry)

    def load_chat_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._v.load_chat_history(limit)

    def update_chat_history_entry(self, ts: str, updates: Dict[str, Any]) -> None:
        self._v.update_chat_history_entry(ts, updates)

    def get_latest_chat_scroll(self) -> Optional[Scroll]:
        return self._v.get_latest_chat_scroll()

    # ── Evolution ─────────────────────────────────────────────────────────────

    def save_evolution(self, evolution: Evolution) -> None:
        self._v.save_evolution(evolution)

    def get_evolution(self, evolution_id: str) -> Evolution:
        return self._v.get_evolution(evolution_id)

    def list_evolutions(self, status: Optional[EvolutionStatus] = None) -> List[Dict[str, Any]]:
        return self._v.list_evolutions(status)

    # ── Notification ──────────────────────────────────────────────────────────

    def queue_notification(self, notification: Notification) -> None:
        self._v.queue_notification(notification)

    def resolve_notification(self, notification_id: str, resolution: str) -> None:
        self._v.resolve_notification(notification_id, resolution)

    def list_pending_notifications(self) -> List[Dict[str, Any]]:
        return self._v.list_pending_notifications()

    # ── Decisions (v0.8.0 Pattern 1 — OperatorDecisionQueue backing) ─────────
    #
    # These methods were added to the inner Vault class in v0.8.0 (commit
    # 78bce27) but the FileVault adapter wrapper was not updated to proxy
    # them.  Result: the dashboard's `/insights → Pending Actions` tab (which
    # consumes `AppState.vault`, which is a FileVault in `SYSTEMU_STORAGE=file`
    # mode) could not read or write OperatorDecision records — every dashboard
    # render of the queue showed the empty-state message even when CLI
    # `sharing_on decisions list` clearly showed pending records.  The CLI
    # path uses the raw Vault directly (`open_vault(config)`) and so was
    # unaffected.  See v0.8.0.1 UAT report for the live trace.

    def save_decision(self, decision) -> None:
        return self._v.save_decision(decision)

    def get_decision(self, decision_id: str):
        return self._v.get_decision(decision_id)

    # ── Episodic memory (v0.9.2 session summaries) ───────────────────────────
    #
    # Wave 1.5: same wrapper-drift class as the decisions incident above —
    # these three were added to the inner Vault in v0.9.2 but never proxied,
    # so in SYSTEMU_STORAGE=file mode (the default) every episodic capture
    # failed with a non-fatal AttributeError and episodic memory was
    # silently disabled.

    def append_session_summary(self, summary) -> None:
        return self._v.append_session_summary(summary)

    def query_session_summaries(self, **kwargs):
        return self._v.query_session_summaries(**kwargs)

    def search_session_summaries(self, query: str, **kwargs):
        return self._v.search_session_summaries(query, **kwargs)
