"""v0.9.7 Phase 0 — _build_history_slice must return the NEWEST useful events.

Regression guard for the round-about-loop root cause: the old implementation
iterated the recent window oldest-first and broke after N, returning the OLDEST
N events and silently dropping the most recent ones — so the model could not see
what it had just done and re-proposed it.
"""


class _Evt:
    def __init__(self, i, event_type="tool_call"):
        self.event_type = event_type
        self.action_block_num = i
        if event_type == "tool_call":
            self.content = {"tool_name": f"tool_{i}", "parameters": {}, "completes_objective": None}
        elif event_type == "thought":
            self.content = {"thought": f"thought_{i}"}
        else:
            self.content = {"result": f"result_{i}"}


class _Ctx:
    def __init__(self, events):
        self._history = events


def test_history_slice_returns_newest_not_oldest():
    from systemu.runtime.shadow_runtime import _build_history_slice
    ctx = _Ctx([_Evt(i) for i in range(50)])
    out = _build_history_slice(ctx, max_events=30)
    tools = [e["tool"] for e in out]
    assert len(out) == 30
    # The newest event MUST be present — the bug dropped it.
    assert "tool_49" in tools, f"newest event missing; got {tools[:3]}…{tools[-3:]}"
    # Chronological order preserved, ending on the newest.
    assert tools[-1] == "tool_49"
    assert tools[0] == "tool_20"  # last 30 == tool_20..tool_49


def test_history_slice_short_history_returns_all_in_order():
    from systemu.runtime.shadow_runtime import _build_history_slice
    ctx = _Ctx([_Evt(i) for i in range(5)])
    out = _build_history_slice(ctx, max_events=30)
    tools = [e["tool"] for e in out]
    assert tools == [f"tool_{i}" for i in range(5)]


def test_history_slice_mixed_types_keeps_newest():
    from systemu.runtime.shadow_runtime import _build_history_slice
    evts = []
    for i in range(40):
        evts.append(_Evt(i, "tool_call"))
        evts.append(_Evt(i, "thought"))
    ctx = _Ctx(evts)
    out = _build_history_slice(ctx, max_events=30)
    assert len(out) == 30
    # The very last event (thought_39) must survive.
    assert out[-1].get("role") == "thought"
    assert out[-1].get("thought") == "thought_39"
