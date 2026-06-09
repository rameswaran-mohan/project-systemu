"""Route dep gate: hitting BLOCKED_PENDING_APPROVAL enqueues ONE dep gate."""
from unittest.mock import MagicMock, patch


def test_pending_dep_enqueues_one_gate(monkeypatch):
    # When the registry's self-heal returns BLOCKED_PENDING_APPROVAL, a dep
    # GateDescriptor must be enqueued (dedup dep:<package>), exactly once.
    import systemu.runtime.tool_registry as tr

    captured = {}

    class _FakeInbox:
        def __init__(self, vault):
            captured["vault"] = vault

        def enqueue(self, descriptor, *, gate_type, **kw):
            captured.setdefault("calls", []).append((descriptor, gate_type))

    monkeypatch.setattr(tr, "InboxQueue", _FakeInbox, raising=False)

    # Drive _maybe_enqueue_dep_gate directly with a minimal pending payload.
    tr._maybe_enqueue_dep_gate(
        vault=MagicMock(),
        tool_id="tool_x", tool_name="X",
        package="Pillow", request_count=2,
    )
    assert len(captured["calls"]) == 1
    descriptor, gate_type = captured["calls"][0]
    assert gate_type == "dep"
    assert descriptor.dedup == "dep:Pillow"
    assert descriptor.risk == "high"
