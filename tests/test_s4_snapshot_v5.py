"""S4 wave1 Step 2 — ExecutionSnapshot.external_evidence + v4->v5 migration.

Models on tests/test_ra10_snapshot_v4.py's exact 4-touch-point field pattern.
The external-evidence store rides the snapshot as a plain dict
({str(objective_id): ExternalEvidence.model_dump()}) so a resumed run keeps its
fail-closed evidence and does NOT silently re-credit an unverified external
effect. Store-agnostic + cycle-free.
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


def test_migrator_current_is_5():
    assert CURRENT_SCHEMA_VERSION == 5


def test_migrate_v4_to_v5_defaults_key():
    data = {"schema_version": 4, "requirement_report": None}
    out = migrate_snapshot_dict(dict(data))
    assert out["schema_version"] == 5
    assert out["external_evidence"] == {}


def test_migrate_v1_to_v5_full_chain():
    """A v1 dict migrates 1->2->3->4->5, gaining every new key incl. external_evidence."""
    data = {"schema_version": 1}
    out = migrate_snapshot_dict(dict(data))
    assert out["schema_version"] == 5
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


def test_external_evidence_round_trips(tmp_path):
    """external_evidence written then read back comes out intact."""
    store = {"1": {"objective_id": 1, "confirmed": True, "method": "api_readback",
                   "detail": "x", "stamped_at": None}}
    snap = ExecutionSnapshot(
        execution_id="exec-rt", shadow_id="sh", scroll_id="sc",
        external_evidence=store,
    )
    write_snapshot(snap, data_dir=tmp_path)
    loaded = read_snapshot("exec-rt", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.external_evidence == store


def test_legacy_v4_snapshot_defaults_external_evidence(tmp_path):
    """A hand-written v4 snapshot (no external_evidence) migrates 4->5 with
    external_evidence={}."""
    target = _snapshot_path(tmp_path, "exec-legacy")
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "execution_id": "exec-legacy", "shadow_id": "sh", "scroll_id": "sc",
        "schema_version": 4,
        "objective_graph": [], "next_objective_id": 1,
        "situation_report": None, "situation_stamps": {},
        "requirement_report": None,
        # NOTE: no external_evidence key.
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = read_snapshot("exec-legacy", data_dir=tmp_path)

    assert loaded is not None
    assert loaded.external_evidence == {}
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION


def test_poisoned_external_evidence_degrades_to_empty_dict(tmp_path):
    """A non-dict external_evidence (poison/garbage on disk) degrades to {} on read
    — fail-closed (no evidence ⇒ no credit), never a crash."""
    target = _snapshot_path(tmp_path, "exec-poison")
    target.parent.mkdir(parents=True, exist_ok=True)
    poisoned = {
        "execution_id": "exec-poison", "shadow_id": "sh", "scroll_id": "sc",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "external_evidence": "corrupt",   # non-dict!
        "requirement_report": None,
        "situation_report": None, "situation_stamps": {},
        "objective_graph": [], "next_objective_id": 1,
    }
    target.write_text(json.dumps(poisoned), encoding="utf-8")

    loaded = read_snapshot("exec-poison", data_dir=tmp_path)

    assert loaded is not None            # did not raise
    assert loaded.external_evidence == {}
