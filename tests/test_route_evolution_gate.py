"""Route evolution gate: a stored proposal enqueues ONE gate, not a parallel
non-gate operator decision."""
from unittest.mock import MagicMock, patch


def test_store_proposal_enqueues_evolution_gate(monkeypatch):
    import systemu.pipelines.evolution_engine as ee

    calls = []

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, **kw):
            calls.append((descriptor, gate_type))

    monkeypatch.setattr(ee, "InboxQueue", _FakeInbox, raising=False)

    vault = MagicMock()
    prop = {"type": "upgrade", "entity_type": "scroll",
            "target_ids": ["scroll_1"], "description": "Tighten step 3",
            "rationale": "fewer retries"}
    evo = ee._store_proposal(prop, vault)
    assert vault.save_evolution.called
    # Exactly one evolution gate enqueued for the stored proposal.
    assert [g for _, g in calls] == ["evolution"]
    assert calls[0][0].dedup == f"evolution:{evo.id}"
