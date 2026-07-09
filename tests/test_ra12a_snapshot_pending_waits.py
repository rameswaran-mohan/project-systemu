"""R-A12a — ExecutionSnapshot.pending_waits + v5->v6 migration.

R-A12a adds durable retry timers persisted in ExecutionSnapshot.pending_waits.
This rides S4's exact field pattern VERBATIM (dataclass field + migration +
_to_dict serialize + read_snapshot rebuild + capture_from_context mirror +
apply_to_context re-seed). The pending-waits list rides the snapshot as a plain
list of dicts so a resumed run keeps its durable retry timers. Store-agnostic +
cycle-free. Default [] = no pending waits.
"""
from __future__ import annotations

import json

from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot,
    capture_from_context,
    read_snapshot,
    write_snapshot,
    _snapshot_path,
)
from systemu.runtime.snapshot_migrations import (
    CURRENT_SCHEMA_VERSION,
    migrate_snapshot_dict,
)


def test_current_schema_version_is_6():
    assert CURRENT_SCHEMA_VERSION == 6


def test_migrate_v5_to_v6_adds_empty_pending_waits():
    out = migrate_snapshot_dict(
        {"schema_version": 5, "execution_id": "e", "external_evidence": {}}
    )
    assert out["pending_waits"] == []
    assert out["schema_version"] == 6


def test_migrate_v1_to_v6_full_chain():
    """A v1 dict migrates 1->2->3->4->5->6, gaining every new key incl. pending_waits."""
    out = migrate_snapshot_dict({"schema_version": 1})
    assert out["schema_version"] == 6
    # G1 (1->2)
    assert out["objective_graph"] == []
    assert out["next_objective_id"] == 1
    # R-A9 (2->3)
    assert out["situation_report"] is None
    assert out["situation_stamps"] == {}
    # R-A10 (3->4)
    assert out["requirement_report"] is None
    # S4 (4->5)
    assert out["external_evidence"] == {}
    # R-A12a (5->6)
    assert out["pending_waits"] == []


def test_snapshot_roundtrips_pending_waits(tmp_path):
    waits = [{"wait_id": "w1", "fire_at": 123.0, "dispatched": False}]
    snap = ExecutionSnapshot(
        execution_id="e1", shadow_id="sh", scroll_id="sc",
        pending_waits=waits,
    )
    write_snapshot(snap, data_dir=tmp_path)
    got = read_snapshot("e1", data_dir=tmp_path)

    assert got is not None
    assert got.pending_waits == waits


def test_pending_waits_defaults_empty(tmp_path):
    snap = ExecutionSnapshot(execution_id="e2", shadow_id="sh", scroll_id="sc")
    assert snap.pending_waits == []


def test_capture_from_context_mirrors_pending_waits():
    class C:
        pass

    c = C()
    c._pending_waits = [{"wait_id": "w9"}]
    snap = capture_from_context(
        execution_id="e-cap", shadow_id="sh", scroll_id="sc",
        iteration=0, current_action_block=1,
        completed_objectives=set(), context=c,
    )
    assert snap.pending_waits == [{"wait_id": "w9"}]


def test_capture_from_context_defaults_empty_when_absent():
    class C:
        pass

    snap = capture_from_context(
        execution_id="e-cap2", shadow_id="sh", scroll_id="sc",
        iteration=0, current_action_block=1,
        completed_objectives=set(), context=C(),
    )
    assert snap.pending_waits == []


def test_legacy_v5_snapshot_defaults_pending_waits(tmp_path):
    """A hand-written v5 snapshot (no pending_waits) migrates 5->6 with
    pending_waits=[]."""
    target = _snapshot_path(tmp_path, "exec-legacy")
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "execution_id": "exec-legacy", "shadow_id": "sh", "scroll_id": "sc",
        "schema_version": 5,
        "objective_graph": [], "next_objective_id": 1,
        "situation_report": None, "situation_stamps": {},
        "requirement_report": None, "external_evidence": {},
        # NOTE: no pending_waits key.
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = read_snapshot("exec-legacy", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.pending_waits == []
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION


def test_poisoned_pending_waits_degrades_to_empty_list(tmp_path):
    """A non-list pending_waits (poison/garbage on disk) degrades to [] on read —
    no phantom timers, never a crash."""
    target = _snapshot_path(tmp_path, "exec-poison")
    target.parent.mkdir(parents=True, exist_ok=True)
    poisoned = {
        "execution_id": "exec-poison", "shadow_id": "sh", "scroll_id": "sc",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "pending_waits": "corrupt",   # non-list!
        "external_evidence": {},
        "requirement_report": None,
        "situation_report": None, "situation_stamps": {},
        "objective_graph": [], "next_objective_id": 1,
    }
    target.write_text(json.dumps(poisoned), encoding="utf-8")

    loaded = read_snapshot("exec-poison", data_dir=tmp_path)

    assert loaded is not None            # did not raise
    assert loaded.pending_waits == []
