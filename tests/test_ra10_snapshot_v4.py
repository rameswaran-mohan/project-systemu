"""R-A10 step B6 — ExecutionSnapshot.requirement_report + v3->v4 migration.

R-A10's binder produces a RequirementReport; to stop a resumed run from
re-asking the operator, its ask_bundle is persisted in the ExecutionSnapshot as
a plain dict (RequirementReport.model_dump()) so the snapshot stays
store-agnostic and cycle-free (we deliberately do NOT import RequirementReport).
This rides R-A9's exact 4-touch-point field pattern and adds a v3->v4 migration.
"""
from __future__ import annotations

import json

from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot,
    read_snapshot,
    write_snapshot,
    _snapshot_path,
)
from systemu.runtime.snapshot_migrations import (
    CURRENT_SCHEMA_VERSION,
    migrate_snapshot_dict,
)


def test_requirement_report_round_trips(tmp_path):
    """A requirement_report written then read back comes out intact."""
    report = {"ask_bundle": [{"question": "which DB?", "field": "db"}], "status": "complete"}
    snap = ExecutionSnapshot(
        execution_id="exec-rt",
        shadow_id="sh",
        scroll_id="sc",
        requirement_report=report,
    )
    write_snapshot(snap, data_dir=tmp_path)
    loaded = read_snapshot("exec-rt", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.requirement_report == report


def test_legacy_v3_snapshot_defaults_requirement_report(tmp_path):
    """A hand-written v3 snapshot (no requirement_report) migrates 3->4 with
    requirement_report=None."""
    target = _snapshot_path(tmp_path, "exec-legacy")
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "execution_id": "exec-legacy",
        "shadow_id": "sh",
        "scroll_id": "sc",
        "schema_version": 3,
        "objective_graph": [],
        "next_objective_id": 1,
        "situation_report": None,
        "situation_stamps": {},
        # NOTE: no requirement_report key.
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = read_snapshot("exec-legacy", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.requirement_report is None
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION


def test_poisoned_requirement_report_degrades_to_none(tmp_path):
    """A non-dict requirement_report (poison/garbage) degrades to None on read —
    a re-ask, never a crash."""
    target = _snapshot_path(tmp_path, "exec-poison")
    target.parent.mkdir(parents=True, exist_ok=True)
    poisoned = {
        "execution_id": "exec-poison",
        "shadow_id": "sh",
        "scroll_id": "sc",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "requirement_report": "corrupt",   # non-dict!
        "situation_report": None,
        "situation_stamps": {},
        "objective_graph": [],
        "next_objective_id": 1,
    }
    target.write_text(json.dumps(poisoned), encoding="utf-8")

    loaded = read_snapshot("exec-poison", data_dir=tmp_path)

    assert loaded is not None  # did not raise
    assert loaded.requirement_report is None


def test_migrator_current_is_4():
    assert CURRENT_SCHEMA_VERSION == 4


def test_migrate_v3_to_v4_defaults_key():
    data = {"schema_version": 3, "situation_report": None, "situation_stamps": {}}
    out = migrate_snapshot_dict(dict(data))
    assert out["schema_version"] == 4
    assert out["requirement_report"] is None


def test_migrate_v1_to_v4_full_chain():
    """A v1 dict migrates through 1->2->3->4 (full chain), gaining all new keys."""
    data = {"schema_version": 1}
    out = migrate_snapshot_dict(dict(data))
    assert out["schema_version"] == 4
    # G1 keys added at 1->2
    assert out["objective_graph"] == []
    assert out["next_objective_id"] == 1
    # R-A9 keys added at 2->3
    assert out["situation_report"] is None
    assert out["situation_stamps"] == {}
    # R-A10 key added at 3->4
    assert out["requirement_report"] is None
