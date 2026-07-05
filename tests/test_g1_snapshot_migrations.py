"""G1 (R-A2 / DEC-9): the SnapshotMigrator — migrate older, refuse newer, backup, stay intact."""
import json
import pytest

from systemu.runtime.snapshot_migrations import (
    CURRENT_SCHEMA_VERSION, SnapshotRefused, migrate_snapshot_dict,
)


def test_current_version_is_fast_path_no_change():
    data = {"schema_version": CURRENT_SCHEMA_VERSION, "x": 1}
    out = migrate_snapshot_dict(dict(data), path=None)
    assert out == data


def test_missing_version_treated_as_v1_and_migrated():
    out = migrate_snapshot_dict({"execution_id": "e"}, path=None)
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION
    assert out["objective_graph"] == []      # v1->v2 fills the new keys
    assert out["next_objective_id"] == 1


def test_newer_version_refuses(tmp_path):
    p = tmp_path / "resume_snapshot.json"
    original = json.dumps({"schema_version": 999, "execution_id": "e"})
    p.write_text(original, encoding="utf-8")
    with pytest.raises(SnapshotRefused):
        migrate_snapshot_dict(json.loads(original), path=p)
    assert p.read_text(encoding="utf-8") == original    # refusal must not mutate original


def test_backup_written_before_downgrade_migration(tmp_path):
    p = tmp_path / "resume_snapshot.json"
    original = json.dumps({"schema_version": 1, "execution_id": "e"})
    p.write_text(original, encoding="utf-8")
    migrate_snapshot_dict(json.loads(original), path=p)
    bak = p.with_suffix(p.suffix + ".bak")
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == original   # backup == pre-migration bytes
    assert p.read_text(encoding="utf-8") == original      # migrator never rewrites the original


def test_mid_chain_exception_leaves_original_intact(tmp_path, monkeypatch):
    import systemu.runtime.snapshot_migrations as m
    p = tmp_path / "resume_snapshot.json"
    original = json.dumps({"schema_version": 1, "execution_id": "e"})
    p.write_text(original, encoding="utf-8")

    def _boom(_data):
        raise RuntimeError("migration blew up")

    monkeypatch.setitem(m._MIGRATIONS, 1, _boom)
    with pytest.raises(RuntimeError):
        m.migrate_snapshot_dict(json.loads(original), path=p)
    assert p.read_text(encoding="utf-8") == original


def test_read_snapshot_migrates_legacy_file(tmp_path):
    from systemu.runtime.execution_snapshot import read_snapshot
    dd = tmp_path / "data"
    exec_dir = dd / "audit" / "exec_exec_v1"     # read_snapshot prefixes id with 'exec_'
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "resume_snapshot.json").write_text(json.dumps({
        "execution_id": "exec_v1", "shadow_id": "s", "scroll_id": "sc",
        "completed_objective_ids": [0],
    }), encoding="utf-8")   # no schema_version → v1
    snap = read_snapshot("exec_v1", data_dir=dd)
    assert snap is not None
    assert snap.schema_version == CURRENT_SCHEMA_VERSION
    assert snap.objective_graph == []


def test_read_snapshot_refuses_newer_file(tmp_path):
    from systemu.runtime.execution_snapshot import read_snapshot
    dd = tmp_path / "data"
    exec_dir = dd / "audit" / "exec_exec_v999"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "resume_snapshot.json").write_text(json.dumps({
        "schema_version": 999, "execution_id": "exec_v999",
        "shadow_id": "s", "scroll_id": "sc",
    }), encoding="utf-8")
    with pytest.raises(SnapshotRefused):
        read_snapshot("exec_v999", data_dir=dd)   # NOT None — refuses loudly


@pytest.mark.parametrize("bad", ["abc", None, [2], {"x": 1}])
def test_uncoercible_schema_version_refuses(bad):
    """A present-but-garbage schema_version must refuse loudly, never migrate or
    silently pass — same safety posture as a newer-than-supported version."""
    with pytest.raises(SnapshotRefused):
        migrate_snapshot_dict({"schema_version": bad, "execution_id": "e"}, path=None)


@pytest.mark.parametrize("bad", [0, -1])
def test_sub_one_schema_version_refuses(bad):
    """Versions are 1-based; a sub-1 version is corrupt → refuse."""
    with pytest.raises(SnapshotRefused):
        migrate_snapshot_dict({"schema_version": bad, "execution_id": "e"}, path=None)


def test_read_snapshot_refuses_garbage_version(tmp_path):
    """read_snapshot must PROPAGATE the refusal for a garbage version, not return None."""
    from systemu.runtime.execution_snapshot import read_snapshot
    dd = tmp_path / "data"
    exec_dir = dd / "audit" / "exec_exec_garbage"   # read_snapshot prefixes id with 'exec_'
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "resume_snapshot.json").write_text(json.dumps({
        "schema_version": "abc", "execution_id": "exec_garbage",
        "shadow_id": "s", "scroll_id": "sc",
    }), encoding="utf-8")
    with pytest.raises(SnapshotRefused):
        read_snapshot("exec_garbage", data_dir=dd)
