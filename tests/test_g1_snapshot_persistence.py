"""G1 (R-A2): ExecutionSnapshot persists objective_graph + next_objective_id + schema_version."""
from systemu.core.models import Objective
from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot, write_snapshot, read_snapshot, capture_from_context,
)


def _data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "audit").mkdir(parents=True, exist_ok=True)
    return d


def test_graph_round_trips(tmp_path):
    dd = _data_dir(tmp_path)
    graph = [
        Objective(id=1, goal="a", success_criteria="a-done"),
        Objective(id=2, goal="b", success_criteria="b-done",
                  depends_on=[1], origin="backchain"),
    ]
    write_snapshot(
        ExecutionSnapshot(execution_id="exec_G", shadow_id="s", scroll_id="sc",
                          objective_graph=graph, next_objective_id=3),
        data_dir=dd,
    )
    snap = read_snapshot("exec_G", data_dir=dd)
    assert snap is not None
    assert [o.id for o in snap.objective_graph] == [1, 2]
    assert all(isinstance(o, Objective) for o in snap.objective_graph)   # not dicts
    assert snap.objective_graph[1].depends_on == [1]                     # edges intact
    assert snap.objective_graph[1].origin == "backchain"
    assert snap.next_objective_id == 3


def test_legacy_snapshot_missing_new_fields_loads(tmp_path):
    import json
    dd = _data_dir(tmp_path)
    # read_snapshot() locates the file via _snapshot_path, which prefixes the
    # execution_id with "exec_" -> the on-disk dir for id "exec_legacy" is
    # "exec_exec_legacy". Hand-write there so read_snapshot("exec_legacy") finds it.
    exec_dir = dd / "audit" / "exec_exec_legacy"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "resume_snapshot.json").write_text(json.dumps({
        "execution_id": "exec_legacy", "shadow_id": "s", "scroll_id": "sc",
        "completed_objective_ids": [0], "sticky_notes": [],
    }), encoding="utf-8")
    snap = read_snapshot("exec_legacy", data_dir=dd)
    assert snap is not None
    assert snap.objective_graph == []
    assert snap.next_objective_id == 1
    assert snap.completed_objective_ids == [0]     # existing field still restored


def test_capture_reads_graph_from_context_stash(tmp_path):
    class _Ctx:
        def get_sticky_notes(self): return []
    snap = capture_from_context(
        execution_id="e", shadow_id="s", scroll_id="sc",
        iteration=1, current_action_block=1, completed_objectives=set(),
        context=_Ctx(),
    )
    assert snap.objective_graph == []
    assert snap.next_objective_id == 1

    stashed = _Ctx()
    stashed._objective_graph = [Objective(id=7, goal="g", success_criteria="s")]
    stashed._next_objective_id = 8
    snap2 = capture_from_context(
        execution_id="e", shadow_id="s", scroll_id="sc",
        iteration=1, current_action_block=1, completed_objectives=set(),
        context=stashed,
    )
    assert [o.id for o in snap2.objective_graph] == [7]
    assert snap2.next_objective_id == 8


def test_next_objective_id_zero_is_floored_to_one_on_read(tmp_path):
    """next_objective_id is a 1-based allocator floor; a 0 on disk (corruption /
    hand-edit) must floor to 1 on read, matching capture_from_context's coercion,
    so a later resume consumer never uses 0 as the allocator floor."""
    import json
    dd = tmp_path / "data"
    exec_dir = dd / "audit" / "exec_exec_zero"     # read_snapshot prefixes id with 'exec_'
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "resume_snapshot.json").write_text(json.dumps({
        "execution_id": "exec_zero", "shadow_id": "s", "scroll_id": "sc",
        "next_objective_id": 0,
    }), encoding="utf-8")
    from systemu.runtime.execution_snapshot import read_snapshot
    snap = read_snapshot("exec_zero", data_dir=dd)
    assert snap is not None
    assert snap.next_objective_id == 1     # floored, not 0
