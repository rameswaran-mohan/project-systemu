"""Consolidate a shadow's execution_log into an LLM-friendly view.

The audit log on `shadow.execution_log` is the source of truth.  This
module produces only the *view* fed back to the LLM, so failures whose
root cause has been resolved don't poison future iterations.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List


@dataclass
class MemoryConsolidator:
    max_repeats: int = 2

    def consolidate(self, log: List, vault) -> str:
        if not log:
            return ""

        # Drop non-dict junk early
        log = [e for e in log if isinstance(e, dict)]
        if not log:
            return ""

        out_lines: List[str] = []
        i = 0
        n = len(log)
        while i < n:
            entry = log[i]
            if entry.get("status") == "success":
                out_lines.append(self._render_success(entry))
                i += 1
                continue

            # failed branch
            if self._is_resolved(entry, vault):
                i += 1
                continue

            # find run of identical (tool, reason) starting at i
            key = self._fail_key(entry)
            run_end = i + 1
            while run_end < n and self._fail_key(log[run_end]) == key:
                run_end += 1
            run = log[i:run_end]
            kept = run[: self.max_repeats]
            extras = len(run) - len(kept)
            for e in kept:
                out_lines.append(self._render_failure(e))
            if extras > 0:
                out_lines.append(f"  (and {extras} more identical failures)")
            i = run_end

        return "\n".join(out_lines)

    @staticmethod
    def _fail_key(e: dict):
        return (e.get("tool"), e.get("reason"))

    @staticmethod
    def _render_success(e: dict) -> str:
        tool = e.get("tool", "?")
        summary = e.get("summary", "")
        return f"SUCCESS [{tool}]: {summary}"

    @staticmethod
    def _render_failure(e: dict) -> str:
        tool = e.get("tool", "?")
        reason = e.get("reason", "")
        return f"FAILED  [{tool}]: {reason}"

    @staticmethod
    def _is_resolved(entry: dict, vault) -> bool:
        """A failure is 'resolved' if the underlying cause no longer holds.

        - 'not enabled' / 'GATE_3' -> resolved if tool.enabled now True
        - 'DEP_PENDING' / 'No module named' -> resolved if tool.dry_run_status='passed'
        - Anything else -> resolved only if tool is both enabled AND passed dry-run

        Without a tool_id we can't check vault state, so treat as live.
        """
        tool_id = entry.get("tool_id")
        if not tool_id:
            return False
        tool = vault.find_tool(tool_id)
        if tool is None:
            return False
        reason = (entry.get("reason") or "").lower()

        if "not enabled" in reason or "gate_3" in reason:
            return bool(getattr(tool, "enabled", False))

        if "dep_pending" in reason or "no module named" in reason:
            return getattr(tool, "dry_run_status", None) == "passed"

        return (bool(getattr(tool, "enabled", False))
                and getattr(tool, "dry_run_status", None) == "passed")

    def consolidate_with_buffer(
        self,
        execution_log: list,
        buffer_entries: list,
        vault,
    ) -> str:
        """produce the LLM-facing memory view from BOTH channels —
        execution_log (recent runs, filtered for resolved causes) and
        memory_buffer (refined lessons, also filtered).

        Sections are separated by a markdown header so the LLM understands
        the provenance difference between "what just happened" and "what
        we've learned".
        """
        sections: list[str] = []

        log_view = self.consolidate(execution_log or [], vault)
        if log_view:
            sections.append(log_view)

        buffer_view = self._render_buffer(buffer_entries or [], vault)
        if buffer_view:
            sections.append("## Lessons\n" + buffer_view)

        return "\n\n".join(sections)

    @staticmethod
    def _render_buffer(entries: list, vault) -> str:
        """Filter + render the memory_buffer view.

        - failure_patterns entries are dropped when the referenced tool
          now passes dry-run AND is enabled (cause resolved — the lesson
          is obsolete).
        - Observational categories (tool_quirks, heuristics, domain_glossary,
          self_assessment) pass through unconditionally — they're not
          gated on a resolvable failure.
        """
        out_lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            category = entry.get("category", "")
            text = (entry.get("lesson") or "").strip()
            if not text:
                continue

            if category == "failure_patterns":
                tool_id = entry.get("tool_id")
                if tool_id:
                    tool = vault.find_tool(tool_id)
                    if tool is not None:
                        if (bool(getattr(tool, "enabled", False))
                                and getattr(tool, "dry_run_status", None) == "passed"):
                            continue  # resolved — drop the lesson

            tool_name = entry.get("tool_name") or "?"
            out_lines.append(f"- [{category}] {tool_name}: {text}")
        return "\n".join(out_lines)


def reset_shadow_memory(*, shadow_id: str, keep_successes: bool, vault) -> None:
    """Prune a shadow's execution_log via get/save (Pydantic round-trip).

    ``vault.save_shadow`` expects the Pydantic ``Shadow`` model. Prefer
    ``get_shadow`` (Pydantic) over ``find_shadow`` (raw ORM row) when both
    exist so save_shadow accepts the result. Tests can still mock with
    ``find_shadow``."""
    shadow = None
    if hasattr(vault, "get_shadow"):
        try:
            shadow = vault.get_shadow(shadow_id)
        except KeyError:
            return
    if shadow is None and hasattr(vault, "find_shadow"):
        shadow = vault.find_shadow(shadow_id)
    if shadow is None:
        return
    if keep_successes:
        shadow.execution_log = [
            e for e in (shadow.execution_log or [])
            if isinstance(e, dict) and e.get("status") == "success"
        ]
    else:
        shadow.execution_log = []
    vault.save_shadow(shadow)
