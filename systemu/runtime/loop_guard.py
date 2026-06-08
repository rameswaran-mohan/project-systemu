"""Deterministic ReAct stall-corrector — Phase 0b Task B1 (v0.9.7).

Tracks a sliding window of tool-call signatures and fires without any LLM
hop when the loop goes "round-about": same signature repeated, or A→B→A→B
ping-pong with zero novelty.

Public API
----------
    guard = LoopGuard(config=None)
    verdict = guard.record(tool_name, args, result=None)
    # verdict is None (no stall) or {"level": "warn"|"block", "message": "..."}

Config fields (optional — module reads env vars with sensible defaults):
    config.loop_guard_enabled   (bool)   — default True
    config.loop_guard_warn      (int)    — repeat count that triggers warn (default 3)
    config.loop_guard_block     (int)    — repeat count that triggers block (default 6)
    config.loop_guard_window    (int)    — sliding window size (default 30)

Env fallbacks (checked when config field is absent):
    SYSTEMU_LOOP_GUARD_ENABLED  — "true"/"false"  (default "true")
    SYSTEMU_LOOP_GUARD_WARN     — int              (default 3)
    SYSTEMU_LOOP_GUARD_BLOCK    — int              (default 6)
    SYSTEMU_LOOP_GUARD_WINDOW   — int              (default 30)

This module MUST NOT import shadow_runtime — it is a pure, dependency-free
utility consumed by shadow_runtime, not the other way around.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import deque
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Cap how many bytes of the result representation we hash — prevents O(N)
# hashing for large tool outputs while keeping the signature meaningful.
_RESULT_HASH_CAP = 4096


def _canonical_json(obj: Any, cap: Optional[int] = None) -> str:
    """Stable, sort-keyed JSON representation safe for unhashable/large objects."""
    raw = json.dumps(obj, sort_keys=True, default=str)
    if cap and len(raw) > cap:
        raw = raw[:cap]
    return raw


def _make_signature(tool_name: str, args: dict, result: Any) -> str:
    """Return a short hex digest that uniquely identifies (tool, args, result)."""
    parts = (
        tool_name,
        _canonical_json(args),
        _canonical_json(result, cap=_RESULT_HASH_CAP),
    )
    combined = "\x00".join(parts)
    return hashlib.sha1(combined.encode("utf-8", errors="replace")).hexdigest()[:16]


class LoopGuard:
    """Sliding-window, hash-based stall detector for the ReAct loop.

    Usage::

        guard = LoopGuard()          # or LoopGuard(config=cfg)
        verdict = guard.record(tool_name, args, result)
        if verdict:
            # inject verdict["message"] into the prompt / abort the loop
    """

    def __init__(self, config: Any = None) -> None:
        # ── enabled ──────────────────────────────────────────────────────────
        self.enabled: bool = bool(
            getattr(
                config, "loop_guard_enabled",
                os.getenv("SYSTEMU_LOOP_GUARD_ENABLED", "true").lower() != "false",
            )
        )

        # ── thresholds ───────────────────────────────────────────────────────
        self.warn_threshold: int = int(
            getattr(
                config, "loop_guard_warn",
                int(os.getenv("SYSTEMU_LOOP_GUARD_WARN", "3")),
            )
        )
        self.block_threshold: int = int(
            getattr(
                config, "loop_guard_block",
                int(os.getenv("SYSTEMU_LOOP_GUARD_BLOCK", "6")),
            )
        )
        self.window_size: int = int(
            getattr(
                config, "loop_guard_window",
                int(os.getenv("SYSTEMU_LOOP_GUARD_WINDOW", "30")),
            )
        )

        # ── state ─────────────────────────────────────────────────────────────
        # Sliding window of recent signatures (bounded deque).
        self._window: deque[str] = deque(maxlen=self.window_size)
        # How many consecutive times the current "leader" signature appeared.
        self._streak: int = 0
        # The signature that is currently on a repeat streak.
        self._leader: Optional[str] = None
        # Ping-pong streak: count of alternating-pair appearances.
        self._pingpong_streak: int = 0

        logger.debug(
            "[LoopGuard] enabled=%s warn=%d block=%d window=%d",
            self.enabled, self.warn_threshold, self.block_threshold, self.window_size,
        )

    # ── public ───────────────────────────────────────────────────────────────

    def record(
        self, tool_name: str, args: dict, result: Any = None
    ) -> Optional[Dict[str, str]]:
        """Record a tool call and return a stall verdict or None.

        Parameters
        ----------
        tool_name:
            Name of the tool that was called.
        args:
            Argument dict passed to the tool.
        result:
            Return value / result dict from the tool (may be None, a dict, or
            any JSON-serialisable object).

        Returns
        -------
        None
            No stall detected.
        {"level": "warn", "message": "..."}
            The same (tool, args, result) signature has been seen
            ``warn_threshold`` times without novelty.
        {"level": "block", "message": "..."}
            The signature has been seen ``block_threshold`` times (or a
            ping-pong has reached the block threshold) — the loop must stop
            calling tools and produce a final answer.
        """
        if not self.enabled:
            return None

        sig = _make_signature(tool_name, args, result)
        verdict = self._update_state(sig, tool_name, args)
        return verdict

    # ── internal ─────────────────────────────────────────────────────────────

    def _update_state(
        self, sig: str, tool_name: str, args: dict
    ) -> Optional[Dict[str, str]]:
        """Core state machine — returns verdict or None."""
        window_list = list(self._window)
        self._window.append(sig)

        # ── Ping-pong detection ────────────────────────────────────────────────
        # Look at the last few distinct signatures in the window.  If the
        # pattern is strictly A,B,A,B,… with exactly 2 distinct values and no
        # novelty in between, it's a ping-pong.
        pp_verdict = self._check_pingpong(sig)
        if pp_verdict:
            return pp_verdict

        # ── Repeat-streak detection ───────────────────────────────────────────
        if sig == self._leader:
            self._streak += 1
        else:
            # New signature — reset streak.
            self._streak = 1
            self._leader = sig

        streak = self._streak

        if streak >= self.block_threshold:
            msg = (
                f"BLOCK: tool '{tool_name}' with these exact arguments has been called "
                f"{streak} consecutive time(s) without progress. "
                "You MUST stop calling this tool and either produce a final answer "
                "or explicitly state what is blocking you."
            )
            logger.warning("[LoopGuard] block — sig=%s streak=%d", sig[:8], streak)
            return {"level": "block", "message": msg}

        if streak >= self.warn_threshold:
            msg = (
                f"WARN: tool '{tool_name}' with these exact arguments has been called "
                f"{streak} time(s) without a different outcome. "
                "You are repeating yourself. Try a different approach, different "
                "arguments, or a different tool."
            )
            logger.info("[LoopGuard] warn — sig=%s streak=%d", sig[:8], streak)
            return {"level": "warn", "message": msg}

        return None

    def _check_pingpong(self, current_sig: str) -> Optional[Dict[str, str]]:
        """Detect A→B→A→B alternation in the recent window.

        We look at the tail of the window (before the current sig is appended)
        to see if the sequence ends in a strict ping-pong of exactly 2 sigs.
        The ping-pong streak counter increments only when the pair matches;
        resets on genuine novelty.
        """
        window_list = list(self._window)  # current sig is already appended
        if len(window_list) < 4:
            return None

        # The last 4 items in the window (current sig is last).
        tail = window_list[-4:]  # [a, b, a, b] — or something else

        # Check if tail forms an alternating pair.
        a, b, c, d = tail
        if a == c and b == d and a != b and d == current_sig:
            # Still alternating — increment ping-pong streak.
            self._pingpong_streak += 1
        else:
            # Novelty or broken pattern — reset.
            self._pingpong_streak = 0
            return None

        pp = self._pingpong_streak

        if pp + 1 >= self.block_threshold:
            msg = (
                "BLOCK: your ReAct loop is alternating between two tools/states "
                f"({pp + 1} ping-pong cycles) with no progress. "
                "You MUST break out of this loop: produce a final answer or "
                "explicitly state what is blocking you."
            )
            logger.warning("[LoopGuard] ping-pong block — streak=%d", pp + 1)
            return {"level": "block", "message": msg}

        if pp + 1 >= self.warn_threshold:
            msg = (
                "WARN: your ReAct loop is alternating between two tools/states "
                f"({pp + 1} cycles) without making progress. "
                "Try a different approach to break out of this pattern."
            )
            logger.info("[LoopGuard] ping-pong warn — streak=%d", pp + 1)
            return {"level": "warn", "message": msg}

        return None
