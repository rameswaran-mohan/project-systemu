"""Context builder — manages the agentic loop's message window.

Responsibilities:
  1. Build the messages list for each LLM call in the loop.
  2. Track execution history (tool calls + observations).
  3. Trigger Tier 3 snapshot compaction after each completed ActionBlock.
  4. Support rollback to the last successful snapshot.
  5. Persist snapshots to vault/executions/<execution_id>/snapshots/.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from systemu.core.utils import utcnow
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionEvent:
    """A single event in the execution history."""

    event_type:  str               # "tool_call" | "observation" | "thought" | "snapshot"
    content:     Dict[str, Any]
    timestamp:   str = field(default_factory=lambda: utcnow().isoformat())
    action_block_num: Optional[int] = None


@dataclass
class Snapshot:
    """A compacted summary of execution history up to a given ActionBlock."""

    action_block_num: int
    summary:          str
    timestamp:        str = field(default_factory=lambda: utcnow().isoformat())


# ─────────────────────────────────────────────────────────────────────────────
#  ExecutionContext
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionContext:
    """Manages the conversation window for a Shadow's agentic execution.

    The context window strategy:
      - Start: system_prompt + skeleton tool index + scroll action blocks
      - During: append tool calls and their observations
      - At ActionBlock boundary: compact history into a snapshot (Tier 3)
      - On failure: roll back to last snapshot checkpoint

    Args:
        execution_id:     Unique ID for this execution run.
        system_prompt:    The Shadow's system prompt.
        scroll_json:      JSON serialisable list of action block dicts.
        tool_index:       Lightweight list of {id, name, description} dicts.
        snapshot_dir:     Optional path to persist snapshots for recovery.
    """

    def __init__(
        self,
        execution_id: str,
        system_prompt: str,
        scroll_json: List[Dict[str, Any]],
        tool_index: List[Dict[str, str]],
        snapshot_dir: Optional[Path] = None,
        skill_index: Optional[List[Dict[str, str]]] = None,
        recalled_memory: str = "",
        use_objectives: bool = False,
        scroll_intent: str = "",
    ):
        self.execution_id    = execution_id
        self.system_prompt   = system_prompt
        self.scroll_json     = scroll_json
        self.tool_index      = tool_index
        self.skill_index     = skill_index or []
        self.snapshot_dir    = snapshot_dir
        self.recalled_memory = recalled_memory
        self.use_objectives  = use_objectives
        self.scroll_intent   = scroll_intent

        self._history:   List[ExecutionEvent] = []
        self._snapshots: List[Snapshot]       = []

        # v0.4.0-b: persistent across rollback (rollback rewinds _history;
        # sticky notes survive so the LLM doesn't replay failed paths with
        # amnesia).  Reflection blocks are also stored here so they end up
        # in the very next iteration's system prompt regardless of where
        # in the history we currently sit.
        self._sticky_notes:        List[str] = []
        self._pending_reflection:  Optional[str] = None

        if snapshot_dir:
            snapshot_dir.mkdir(parents=True, exist_ok=True)

    # ── Message building ──────────────────────────────────────────────────────

    def build_messages(
        self,
        current_action_block: int,
        completed_objectives: Optional[set] = None,
    ) -> List[Dict[str, str]]:
        """Build the messages list for the next LLM iteration.

        Supports both intent-driven (objectives) and legacy (action_blocks) modes.
        """
        messages: List[Dict[str, str]] = []

        # ── System ────────────────────────────────────────────────────────────
        skill_skeleton = json.dumps([
            {"id": s.get("id", ""), "name": s.get("name", ""),
             "category": s.get("category", ""), "description": s.get("description", "")}
            for s in self.skill_index
        ], indent=2) if self.skill_index else "[]"

        tool_skeleton = json.dumps([
            {"id": t.get("id", ""), "name": t.get("name", ""), "description": t.get("description", "")}
            for t in self.tool_index
        ], indent=2)

        memory_block = (
            f"\n\n{self.recalled_memory.rstrip()}"
            if self.recalled_memory else ""
        )

        # v0.4.0-b: sticky notes survive rollback so the rolled-back LLM
        # doesn't replay the exact same failed path.  Empty by default.
        sticky_block = ""
        if self._sticky_notes:
            sticky_lines = "\n".join(f"- {n}" for n in self._sticky_notes)
            sticky_block = (
                "\n\n## Sticky Notes (persist across rollback)\n\n"
                f"{sticky_lines}"
            )

        # v0.4.0-b: pending reflection block (one-shot — consumed here).
        reflection_block = ""
        pending = self._consume_pending_reflection()
        if pending:
            reflection_block = f"\n\n## Failure Reflection (one-shot)\n\n{pending}"

        messages.append({
            "role": "system",
            "content": (
                f"{self.system_prompt}\n\n"
                f"## Available Resources\n\n"
                f"Use `LOAD_RESOURCE` to fetch a resource's full instructions or parameter schema on demand.\n\n"
                f"### Skills\n\n```json\n{skill_skeleton}\n```\n\n"
                f"### Tools\n\n```json\n{tool_skeleton}\n```"
                f"{memory_block}"
                f"{sticky_block}"
                f"{reflection_block}"
            ),
        })

        # ── Task spec (objectives or action blocks) ───────────────────────────
        if self.use_objectives:
            done_ids = completed_objectives or set()
            pending = [obj for obj in self.scroll_json if obj.get("id") not in done_ids]
            intent_header = f"**Intent:** {self.scroll_intent}\n\n" if self.scroll_intent else ""
            messages.append({
                "role": "user",
                "content": (
                    f"## Task\n\n"
                    f"{intent_header}"
                    f"### Pending Objectives ({len(pending)} remaining)\n\n"
                    f"```json\n{json.dumps(pending, indent=2)}\n```\n\n"
                    f"Completed objective IDs: {sorted(done_ids)}"
                ),
            })
        else:
            pending = [ab for ab in self.scroll_json
                       if ab.get("step_number", 0) >= current_action_block]
            messages.append({
                "role": "user",
                "content": (
                    f"## Remaining ActionBlocks (starting from step {current_action_block})\n\n"
                    f"```json\n{json.dumps(pending, indent=2)}\n```"
                ),
            })

        # ── Last snapshot ─────────────────────────────────────────────────────
        if self._snapshots:
            last_snap = self._snapshots[-1]
            messages.append({
                "role": "user",
                "content": (
                    f"## Context Snapshot (up to step {last_snap.action_block_num})\n\n"
                    f"{last_snap.summary}"
                ),
            })

        # ── Recent history since last snapshot ────────────────────────────────
        snap_boundary = 0
        if self._snapshots:
            snap_boundary = self._snapshots[-1].action_block_num

        recent = [
            e for e in self._history
            if (e.action_block_num or 0) >= snap_boundary
            and e.event_type != "snapshot"
        ]

        for event in recent[-30:]:   # cap at last 30 events to control context size
            if event.event_type == "tool_call":
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(event.content),
                })
            elif event.event_type == "observation":
                messages.append({
                    "role": "user",
                    "content": f"## Tool Result\n\n```json\n{json.dumps(event.content, indent=2)}\n```",
                })
            elif event.event_type == "thought":
                messages.append({
                    "role": "assistant",
                    "content": f"## Thought\n\n{event.content.get('thought', '')}",
                })

        # ── Decision prompt ───────────────────────────────────────────────────
        if self.use_objectives:
            prompt_line = "What is your next decision to advance toward the pending objectives? Return only valid JSON."
        else:
            prompt_line = f"Current step: ActionBlock {current_action_block}. What is your next decision? Return only valid JSON."
        messages.append({
            "role": "user",
            "content": prompt_line,
        })

        return messages

    # ── History management ────────────────────────────────────────────────────

    def add_tool_call(
        self,
        decision: Dict[str, Any],
        action_block_num: int,
    ) -> None:
        self._history.append(ExecutionEvent(
            event_type="tool_call",
            content=decision,
            action_block_num=action_block_num,
        ))

    def add_observation(
        self,
        result: Dict[str, Any],
        action_block_num: int,
    ) -> None:
        self._history.append(ExecutionEvent(
            event_type="observation",
            content=result,
            action_block_num=action_block_num,
        ))

    def add_thought(
        self,
        thought: str,
        action_block_num: int,
    ) -> None:
        self._history.append(ExecutionEvent(
            event_type="thought",
            content={"thought": thought},
            action_block_num=action_block_num,
        ))

    def add_resource_load(
        self,
        resource_type: str,
        resource_id: str,
        content: str,
        action_block_num: int,
    ) -> None:
        """Record a LOAD_RESOURCE event — injects a resource's full markdown content."""
        self._history.append(ExecutionEvent(
            event_type="observation",
            content={
                "type": "resource_loaded",
                "resource_type": resource_type,
                "resource_id": resource_id,
                "content": content,
            },
            action_block_num=action_block_num,
        ))

    def get_full_history(self) -> List[Dict[str, Any]]:
        """Return the full execution history as a flat list of dicts.

        Used by the refinery and memory extraction passes to inspect what
        actually happened during execution. Excludes snapshots since they're
        derivative summaries, not new facts.
        """
        return [
            {
                "event_type":       e.event_type,
                "content":          e.content,
                "timestamp":        e.timestamp,
                "action_block_num": e.action_block_num,
            }
            for e in self._history
            if e.event_type != "snapshot"
        ]

    # ── Snapshotting ──────────────────────────────────────────────────────────

    def take_snapshot(
        self,
        action_block_num: int,
        config,                 # sharing_on Config
    ) -> Snapshot:
        """Compact recent history into a Tier 3 snapshot.

        Tier 3 (fast/cheap) is ideal for summarisation — no deep reasoning needed.
        """
        # llm_call is async — use the sync runner so take_snapshot() stays non-async
        from systemu.core.llm_router import _run_coroutine, llm_call

        recent_events = [
            e for e in self._history
            if e.event_type != "snapshot"
        ]
        history_text = "\n\n".join(
            f"[{e.event_type.upper()}] {json.dumps(e.content)}"
            for e in recent_events[-40:]  # last 40 events
        )

        logger.debug(
            "[Context] Taking snapshot at ActionBlock %d (%d events) ...",
            action_block_num, len(recent_events),
        )

        try:
            resp = _run_coroutine(llm_call(
                tier=3,
                system=(
                    "You are a context compactor. Summarise the execution history below "
                    "into a dense, factual paragraph (max 200 words). "
                    "Focus on: what was accomplished, what data was collected, what is the current state. "
                    "Do NOT include reasoning chains — only facts and state."
                ),
                user=f"## Execution History (up to ActionBlock {action_block_num})\n\n{history_text}",
                config=config,
                temperature=0.1,
                max_tokens=400,
            ))
            summary = resp.get("content", "(snapshot failed — raw history retained)")
        except Exception as exc:
            logger.warning("[Context] Snapshot LLM call failed: %s", exc)
            summary = f"(snapshot failed at step {action_block_num}) — {len(recent_events)} events in history"

        snapshot = Snapshot(action_block_num=action_block_num, summary=summary)
        self._snapshots.append(snapshot)

        # Mark history events as snapshotted
        self._history.append(ExecutionEvent(
            event_type="snapshot",
            content={"summary": summary, "action_block_num": action_block_num},
            action_block_num=action_block_num,
        ))

        # Persist to disk
        if self.snapshot_dir:
            self._persist_snapshot(snapshot, action_block_num)

        logger.info(
            "[Context] Snapshot taken at ActionBlock %d: %s",
            action_block_num, summary[:80] + "…" if len(summary) > 80 else summary,
        )
        return snapshot

    def rollback_to_last_snapshot(self) -> Optional[int]:
        """Roll back context window to the last successful snapshot.

        Returns the ActionBlock number of the rollback target, or None if no snapshots.
        NOTE: This only rewinds the context window — it does NOT undo real-world side effects.
        Sticky notes (see :meth:`add_sticky_note`) DO survive rollback so the LLM
        retains memory of what was tried and failed on the rolled-back path.
        """
        if not self._snapshots:
            logger.warning("[Context] Rollback requested but no snapshots exist.")
            return None

        last = self._snapshots[-1]
        # Trim history to events from before or at the snapshot boundary
        self._history = [
            e for e in self._history
            if (e.action_block_num or 0) <= last.action_block_num
        ]
        logger.info(
            "[Context] Rolled back to ActionBlock %d snapshot (sticky notes preserved: %d)",
            last.action_block_num, len(self._sticky_notes),
        )
        return last.action_block_num

    # ── v0.4.0-b: Sticky notes + reflection blocks ──────────────────────────
    #
    # These are deliberately stored OUTSIDE ``_history`` so they survive a
    # :meth:`rollback_to_last_snapshot` call.  Without this safeguard a
    # rolled-back LLM would replay the exact same failed path with amnesia.

    def add_sticky_note(self, text: str, *, max_notes: int = 8) -> None:
        """Pin a short note that survives rollback.  Bounded by ``max_notes``.

        Notes appear in the next iteration's system message under a
        "Sticky notes" section, prefixed with a small index so the LLM can
        reference them.  Older notes are dropped FIFO when the cap is hit
        (keeps the prompt bounded).
        """
        text = (text or "").strip()
        if not text:
            return
        self._sticky_notes.append(text[:300])
        if len(self._sticky_notes) > max_notes:
            self._sticky_notes = self._sticky_notes[-max_notes:]

    def queue_reflection_block(self, text: str) -> None:
        """Stage a one-shot reflection block for the next iteration's prompt.

        Unlike sticky notes the reflection block is *consumed* the moment
        :meth:`build_messages` is called — it's a single nudge after a
        failure, not a recurring reminder.  Stored on the context (not in
        ``_history``) so a rollback doesn't lose it.
        """
        self._pending_reflection = (text or "").strip() or None

    def _consume_pending_reflection(self) -> Optional[str]:
        out = self._pending_reflection
        self._pending_reflection = None
        return out

    def get_sticky_notes(self) -> List[str]:
        """Read-only snapshot of pinned notes for templating into the prompt."""
        return list(self._sticky_notes)

    def _persist_snapshot(self, snapshot: Snapshot, step: int) -> None:
        # Snapshot disk persistence was removed — rollback operates in-memory only.
        # The snapshot_dir parameter is kept on the constructor to avoid breaking
        # any external callers, but nothing is written to disk.
        pass

    # ── Result building ───────────────────────────────────────────────────────

    def build_result(
        self,
        status: str,
        final_summary: str,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the final ExecutionResult dict.

        v0.9.6 (L7): additionally surfaces run metadata derived from the
        history — ``tools_called`` / ``tool_calls`` / ``rounds`` — so the
        post-run auto-skill-extraction hook can decide whether the run is worth
        capturing as a SKILL.md (Odysseus threshold: >=2 rounds OR >=2 tool
        calls).  Additive keys only; existing callers read by name.
        """
        tool_events = [e for e in self._history if e.event_type == "tool_call"]
        tools_called: List[str] = []
        for e in tool_events:
            content = e.content if isinstance(e.content, dict) else {}
            tname = content.get("tool_name") or content.get("tool")
            if tname:
                tools_called.append(str(tname))
        # rounds ≈ number of distinct action blocks the run actually touched
        # (snapshots excluded). Falls back to tool-call count if action-block
        # numbers are unavailable.
        block_nums = {
            getattr(e, "action_block_num", None)
            for e in self._history if e.event_type != "snapshot"
        }
        block_nums.discard(None)
        rounds = len(block_nums) if block_nums else len(tool_events)
        return {
            "execution_id":   self.execution_id,
            "status":         status,      # "success" | "failure" | "partial"
            "summary":        final_summary,
            "error":          error,
            "snapshots_taken": len(self._snapshots),
            "total_events":   len(self._history),
            "tools_called":   tools_called,
            "tool_calls":     len(tool_events),
            "rounds":         rounds,
            "timestamp":      utcnow().isoformat(),
        }
