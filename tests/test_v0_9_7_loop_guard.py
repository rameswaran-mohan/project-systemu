"""v0.9.7 Phase 0b Task B1 — deterministic ReAct stall-corrector.

Tests for systemu/runtime/loop_guard.py.

Coverage:
  1. Same (tool, args) repeated warn_threshold → "warn"
  2. Same (tool, args) repeated block_threshold → "block"
  3. A novel signature between repeats resets the streak (no premature warn).
  4. Ping-pong A→B→A→B→... escalates (warn then block).
  5. Disabled guard always returns None.
  6. Window is bounded — old signatures age out and do not contribute to streak.
  7. Result hashing: different result → different signature (no false stall).
"""
from __future__ import annotations

import os
import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_guard(*, enabled=True, warn=3, block=6, window=30):
    """Create a LoopGuard with explicit thresholds (bypasses env vars)."""
    from systemu.runtime.loop_guard import LoopGuard

    class _Cfg:
        loop_guard_enabled = enabled
        loop_guard_warn = warn
        loop_guard_block = block
        loop_guard_window = window

    return LoopGuard(config=_Cfg())


def _call(guard, tool="search", args=None, result=None):
    if args is None:
        args = {"q": "test"}
    return guard.record(tool, args, result)


# ── 1. warn on warn_threshold repeats ────────────────────────────────────────

def test_warn_at_threshold():
    guard = _make_guard(warn=3, block=6)
    # First 2 repeats: no verdict
    for _ in range(2):
        v = _call(guard)
        assert v is None, f"Expected None before threshold, got {v}"
    # 3rd repeat: warn
    v = _call(guard)
    assert v is not None
    assert v["level"] == "warn"
    assert "search" in v["message"]


# ── 2. block on block_threshold repeats ──────────────────────────────────────

def test_block_at_threshold():
    guard = _make_guard(warn=3, block=6)
    verdicts = []
    for _ in range(6):
        verdicts.append(_call(guard))

    # First 2 → None, then 3–5 → warn, 6th → block
    assert verdicts[0] is None
    assert verdicts[1] is None
    for v in verdicts[2:5]:
        assert v is not None
        assert v["level"] == "warn", f"Expected warn, got {v}"
    assert verdicts[5]["level"] == "block"


# ── 3. novel signature between repeats resets streak ─────────────────────────

def test_novel_signature_resets_streak():
    guard = _make_guard(warn=3, block=6)
    # Two identical calls
    _call(guard, args={"q": "same"})
    _call(guard, args={"q": "same"})
    # Novel call — resets streak
    v_novel = _call(guard, args={"q": "DIFFERENT"})
    assert v_novel is None, f"Novel call should return None, got {v_novel}"
    # Two more identical calls after novel (streak is 1 after novel, then 2 after
    # this call) — still below warn_threshold=3 until the 3rd
    _call(guard, args={"q": "same"})
    v = _call(guard, args={"q": "same"})
    # At this point streak for "same" is: 1 (call 1), 2 (call 2), reset by novel,
    # 1 (call 4), 2 (call 5) — NOT yet at warn=3
    assert v is None, f"After reset, 2nd repeat should return None, got {v}"
    # Third repeat for "same" after the novel break: streak=3 → warn
    v = _call(guard, args={"q": "same"})
    assert v is not None
    assert v["level"] == "warn"


# ── 4. ping-pong A→B→A→B escalates ──────────────────────────────────────────

def test_pingpong_escalates():
    guard = _make_guard(warn=3, block=6)
    tool_a_args = {"q": "A"}
    tool_b_args = {"q": "B"}

    verdicts = []
    for i in range(12):
        if i % 2 == 0:
            verdicts.append(guard.record("search", tool_a_args))
        else:
            verdicts.append(guard.record("search", tool_b_args))

    # At some point we should see warn, then block.
    levels = [v["level"] for v in verdicts if v is not None]
    assert "warn" in levels, f"Expected warn in verdicts, got levels={levels}"
    assert "block" in levels, f"Expected block in verdicts, got levels={levels}"


# ── 5. disabled guard always returns None ────────────────────────────────────

def test_disabled_guard_always_none():
    guard = _make_guard(enabled=False, warn=1, block=2)
    for _ in range(20):
        assert _call(guard) is None


# ── 6. window is bounded — old signatures age out ────────────────────────────

def test_window_bounds_signatures():
    """With window=5 and warn=3, if we record 5 unique calls then repeat one
    from before the window, the repeat counter must start fresh (no false stall).
    """
    guard = _make_guard(warn=3, block=6, window=5)
    # Fill window with 5 unique calls
    for i in range(5):
        v = guard.record("tool", {"i": i})
        assert v is None, f"Unique call {i} should return None, got {v}"

    # Now repeat call 0 — it has aged out of the window (window=5, we added 5
    # unique items so call 0 is at the boundary of expiry).
    # The streak for sig(tool, {i:0}) starts at 1 now, not 2.
    v = guard.record("tool", {"i": 0})
    assert v is None, f"Aged-out signature should start fresh, got {v}"


# ── 7. result hashing: different result → different signature ─────────────────

def test_different_result_different_signature():
    """result=None vs result={...} should be treated as distinct signatures."""
    guard = _make_guard(warn=3, block=6)
    # Three calls with different results — streak should never exceed 1.
    v1 = guard.record("tool", {"x": 1}, result=None)
    v2 = guard.record("tool", {"x": 1}, result={"ok": True})
    v3 = guard.record("tool", {"x": 1}, result={"ok": False})
    assert v1 is None
    assert v2 is None
    assert v3 is None


# ── 8. unhashable / large args are tolerated ─────────────────────────────────

def test_large_args_are_tolerated():
    """Large or deeply nested args must not raise — should be capped/serialised."""
    from systemu.runtime.loop_guard import LoopGuard
    guard = LoopGuard()  # defaults
    large_result = {"data": "x" * 100_000, "nested": {"a": list(range(1000))}}
    v = guard.record("big_tool", {"payload": large_result}, result=large_result)
    # Just must not raise; verdict can be anything.
    assert v is None or isinstance(v, dict)


# ── 9. env-var defaults are honoured when config not provided ─────────────────

def test_env_defaults_applied(monkeypatch):
    from systemu.runtime.loop_guard import LoopGuard
    monkeypatch.setenv("SYSTEMU_LOOP_GUARD_ENABLED", "true")
    monkeypatch.setenv("SYSTEMU_LOOP_GUARD_WARN", "2")
    monkeypatch.setenv("SYSTEMU_LOOP_GUARD_BLOCK", "4")
    monkeypatch.setenv("SYSTEMU_LOOP_GUARD_WINDOW", "10")
    guard = LoopGuard()  # no config — reads from env
    assert guard.enabled is True
    assert guard.warn_threshold == 2
    assert guard.block_threshold == 4
    assert guard.window_size == 10

    # Verify warn fires at threshold=2
    guard.record("t", {})
    v = guard.record("t", {})
    assert v is not None and v["level"] == "warn"


def test_env_disabled(monkeypatch):
    from systemu.runtime.loop_guard import LoopGuard
    monkeypatch.setenv("SYSTEMU_LOOP_GUARD_ENABLED", "false")
    guard = LoopGuard()
    assert guard.enabled is False
    for _ in range(10):
        assert guard.record("t", {}) is None
