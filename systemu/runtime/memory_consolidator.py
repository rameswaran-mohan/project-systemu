"""Consolidate a shadow's execution_log into an LLM-friendly view.

The audit log on `shadow.execution_log` is the source of truth.  This
module produces only the *view* fed back to the LLM, so failures whose
root cause has been resolved don't poison future iterations.

v0.9.6 (Layer 7 — Proactive Surfacing): adds `consolidate_run` — a
post-run LLM pass that surfaces "things learned" (facts + patterns)
beyond the per-user fact extraction from v0.9.0/v0.9.1. Idempotent via
SHA256 fingerprint cache.
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
        """v0.6.9: produce the LLM-facing memory view from BOTH channels —
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


# ---------------------------------------------------------------------------
# v0.9.6 (Layer 7 — Proactive Surfacing): consolidate_run
# ---------------------------------------------------------------------------

import hashlib
import json as _json
import logging as _logging
from pathlib import Path as _Path
from typing import Any as _Any, Dict as _Dict, Optional as _Optional

from systemu.core.llm_router import llm_call_json

_consolidate_logger = _logging.getLogger(__name__ + ".consolidate_run")

_CONSOLIDATE_SYSTEM_PROMPT = """You are a memory consolidation agent. Given a chat history,
extract NEW knowledge worth remembering across sessions.

Return strict JSON:
{
  "facts_learned": ["<short fact>", ...],
  "patterns_observed": ["<recurring pattern>", ...]
}

Conservative: only surface NEW things. Empty lists are fine.
"""


def _fingerprint(chat_history: list) -> str:
    """SHA256 of canonical-JSON form of role+content pairs."""
    canon = _json.dumps(
        [{"role": h.get("role"), "content": h.get("content")} for h in (chat_history or [])],
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _cache_dir(root: "_Optional[_Path]") -> "_Optional[_Path]":
    if root is None:
        return None
    p = _Path(root) / "memory_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def consolidate_run(
    *,
    chat_history: "list[_Dict[str, _Any]]",
    config: "_Any",
    cache_root: "_Optional[_Path]" = None,
) -> "_Optional[_Dict[str, _Any]]":
    """Consolidate memory from a chat history.

    Returns dict with facts_learned + patterns_observed lists. None when:
    - config.memory_consolidation_enabled is False
    - LLM exception (degrades silently)

    Idempotent via SHA256 fingerprint cache when cache_root is provided.
    """
    if not getattr(config, "memory_consolidation_enabled", True):
        return None

    fp = _fingerprint(chat_history)
    cache = _cache_dir(cache_root)
    if cache is not None:
        cached = cache / f"{fp}.json"
        if cached.exists():
            try:
                return _json.loads(cached.read_text(encoding="utf-8"))
            except Exception:
                pass

    try:
        result = llm_call_json(
            tier=1,
            system=_CONSOLIDATE_SYSTEM_PROMPT,
            user=_json.dumps({"chat_history": chat_history}, separators=(",", ":")),
            config=config,
            max_tokens=400,
            temperature=0.2,
        )
    except Exception as exc:
        _consolidate_logger.warning("[MemoryConsolidator] LLM failed: %s", exc)
        return None

    if not isinstance(result, dict):
        return None

    facts = result.get("facts_learned") or []
    patterns = result.get("patterns_observed") or []
    if not isinstance(facts, list):
        facts = []
    if not isinstance(patterns, list):
        patterns = []

    consolidated = {
        "facts_learned": [str(f) for f in facts][:20],
        "patterns_observed": [str(p) for p in patterns][:10],
    }

    if cache is not None:
        try:
            (cache / f"{fp}.json").write_text(
                _json.dumps(consolidated, indent=2), encoding="utf-8",
            )
        except Exception:
            pass

    return consolidated


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
