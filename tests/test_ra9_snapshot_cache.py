"""R-A9 Task 8 — ExecutionSnapshot situation_report + situation_stamps cache.

The SituationReport (from survey_situation) is cached in the ExecutionSnapshot
as a plain dict (SituationReport.model_dump()) so the snapshot stays
store-agnostic and cycle-free, plus its freshness stamps. This rides G1's exact
6-touch-point field pattern and adds a v2->v3 migration.
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


def test_situation_fields_round_trip(tmp_path):
    """A report+stamps written then read back come out intact."""
    report = {"services": [{"name": "postgres", "port": 5432}], "roots": ["/home/x"]}
    stamps = {"roots": "hash-abc", "services": "hash-def"}
    snap = ExecutionSnapshot(
        execution_id="exec-rt",
        shadow_id="sh",
        scroll_id="sc",
        situation_report=report,
        situation_stamps=stamps,
    )
    write_snapshot(snap, data_dir=tmp_path)
    loaded = read_snapshot("exec-rt", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.situation_report == report
    assert loaded.situation_stamps == stamps


def test_legacy_v2_snapshot_defaults_new_fields(tmp_path):
    """A hand-written v2 snapshot (no situation fields) migrates 2->3 with
    situation_report=None and situation_stamps={}."""
    target = _snapshot_path(tmp_path, "exec-legacy")
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "execution_id": "exec-legacy",
        "shadow_id": "sh",
        "scroll_id": "sc",
        "schema_version": 2,
        "objective_graph": [],
        "next_objective_id": 1,
        # NOTE: no situation_report / situation_stamps keys.
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = read_snapshot("exec-legacy", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.situation_report is None
    assert loaded.situation_stamps == {}


def test_poisoned_report_degrades_to_none(tmp_path):
    """A non-dict situation_report (poison/garbage) degrades to None on read —
    a re-survey, never a crash."""
    target = _snapshot_path(tmp_path, "exec-poison")
    target.parent.mkdir(parents=True, exist_ok=True)
    poisoned = {
        "execution_id": "exec-poison",
        "shadow_id": "sh",
        "scroll_id": "sc",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "situation_report": "corrupt",   # non-dict!
        "situation_stamps": "also-bad",  # non-dict!
        "objective_graph": [],
        "next_objective_id": 1,
    }
    target.write_text(json.dumps(poisoned), encoding="utf-8")

    loaded = read_snapshot("exec-poison", data_dir=tmp_path)

    assert loaded is not None  # did not raise
    assert loaded.situation_report is None
    assert loaded.situation_stamps == {}


def test_migrator_current_is_6():
    # R-A12a bumped CURRENT_SCHEMA_VERSION 5 -> 6 (adds pending_waits).
    assert CURRENT_SCHEMA_VERSION == 6


def test_migrate_v2_to_v3_defaults_keys():
    data = {"schema_version": 2, "objective_graph": [], "next_objective_id": 1}
    out = migrate_snapshot_dict(dict(data))
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION
    assert out["situation_report"] is None
    assert out["situation_stamps"] == {}


def test_migrate_v1_to_v3_chain():
    """A v1 dict migrates through 1->2->3 (chain), gaining all new keys."""
    data = {"schema_version": 1}
    out = migrate_snapshot_dict(dict(data))
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION
    # G1 keys added at 1->2
    assert out["objective_graph"] == []
    assert out["next_objective_id"] == 1
    # R-A9 keys added at 2->3
    assert out["situation_report"] is None
    assert out["situation_stamps"] == {}
