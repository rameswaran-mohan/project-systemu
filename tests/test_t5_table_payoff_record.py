"""T5 / R-B5 / R-A17 -- the DURABLE per-run record of what the operator's table supplied.

WHY THIS FILE EXISTS. The inventory-hit metric used to read per-run RESUME SNAPSHOTS
(``data/audit/exec_*/resume_snapshot.json``), which ``delete_snapshot`` removes the
moment a resume consumes them. The population it measured therefore SHRANK over time:
every run that suspended and then resumed dropped out of the denominator, so the rate
was computed over "whatever survived", never over a witnessed population (DEC-27).

The record pinned here is the replacement substrate:

  * append-only, ONE FILE PER ROOT RUN (``run_<root>.jsonl``) under the audit data
    root, so every resume of the same run (and every sub-agent child, which inherits
    the root) lands in the SAME file;
  * a ``consult`` row when a binder pass ran with the table on hand -- the metric's
    DENOMINATOR comes from this, not from whatever snapshot survived;
  * one ``supplied`` row per requirement the table supplied, with how it bound
    (``prefilled_confirm`` vs ``silent``) -- never the resolved VALUE;
  * it survives resume and run end (nothing in the snapshot lifecycle touches it).

Every test writes under ``tmp_path`` (``data_dir=``), never into the worktree.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from systemu.core.models import Requirement, RequirementReport
from systemu.runtime import table_payoff_record as tpr
from systemu.runtime.situational_inventory import (
    CapabilityRef, SituationReport, compose_table,
)
from systemu.runtime.table_store import TableItem


# -- harness -----------------------------------------------------------------
def _req(**over) -> Requirement:
    base = dict(kind="decision", schema_path="recipient", state="have",
                source="situation", value_origin="content_derived", confidence=0.8,
                table_item_id="t-1", bound_value_ref="inventory:declared_intents")
    base.update(over)
    return Requirement(**base)


def _report(*reqs, oid=1) -> RequirementReport:
    return RequirementReport(per_objective={oid: list(reqs)}, ask_bundle=[])


def _table_situation(*, name="recipient", item_id="t-1"):
    """A REAL SituationReport composed by the REAL compose_table from a REAL TableItem.

    The item references a service the live inventory does not have, so compose_table
    routes it into declared_intents -- the add-only channel, carrying the item's name.
    """
    item = TableItem(id=item_id, kind="service", name=name,
                     ref={"server": "https://example.invalid/" + item_id})
    return compose_table(SituationReport(), [item])


def _rows(path: Path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _write(tmp_path, report, situation=None, **kw):
    kw.setdefault("root_execution_id", "exec_root1")
    kw.setdefault("execution_id", kw["root_execution_id"])
    kw.setdefault("site", "midloop")
    return tpr.record_binder_pass(
        report, _table_situation() if situation is None else situation,
        data_dir=tmp_path / "data", **kw)


def _run_file(tmp_path, root="exec_root1") -> Path:
    return tpr.run_record_path(root, data_dir=tmp_path / "data")


# == 1. what gets written =====================================================
def test_a_table_supplied_requirement_writes_a_consult_and_a_supplied_row(tmp_path):
    status = _write(tmp_path, _report(_req()), tool="send",
                    scroll_id="scroll_1", activity_id="act_1")
    assert status["consulted"] is True and status["written"] == 2, status

    rows = _rows(_run_file(tmp_path))
    assert [r["event"] for r in rows] == ["consult", "supplied"]
    consult, supplied = rows
    assert consult["root_execution_id"] == "exec_root1"
    assert consult["scroll_id"] == "scroll_1" and consult["activity_id"] == "act_1"
    assert consult["table_entries"] >= 1
    assert supplied["table_item_id"] == "t-1"
    assert supplied["item_name"] == "recipient"      # named from the composed situation
    assert supplied["value_origin"] == "content_derived"
    assert supplied["bound_as"] == "prefilled_confirm"
    assert supplied["schema_path"] == "recipient"
    assert supplied["objective_id"] == 1 and supplied["tool"] == "send"
    assert isinstance(supplied["at"], str) and supplied["at"]


def test_the_record_lives_under_the_audit_data_root(tmp_path):
    _write(tmp_path, _report(_req()))
    assert _run_file(tmp_path).is_file()
    assert _run_file(tmp_path).parent == tmp_path / "data" / "audit" / "table_payoff"


def test_a_silent_table_bind_is_recorded_as_silent_not_as_a_confirm(tmp_path):
    """The DEC-25 tripwire's raw material. A trusted-origin ``have`` binds without an
    ask; if a table-attributed bind ever does that, the record must say ``silent``."""
    _write(tmp_path, _report(_req(value_origin="operator", state="have")))
    supplied = [r for r in _rows(_run_file(tmp_path)) if r["event"] == "supplied"]
    assert [r["bound_as"] for r in supplied] == ["silent"]


def test_the_resolved_value_and_the_source_handle_are_never_written(tmp_path):
    """Observability only: a bound handle can embed an account identity, and the value
    behind a bind can be anything. Neither belongs in an audit file."""
    secret = "acct-SECRET-handle-0123456789"
    _write(tmp_path, _report(_req(bound_value_ref=f"service:mail#{secret}",
                                  bound_value_digest="d" * 40)))
    text = _run_file(tmp_path).read_text(encoding="utf-8")
    assert secret not in text
    assert "d" * 40 not in text
    assert "bound_value_ref" not in text


def test_non_table_requirements_are_not_recorded(tmp_path):
    _write(tmp_path, _report(_req(table_item_id=None), _req(schema_path="x",
                                                             table_item_id=None)))
    rows = _rows(_run_file(tmp_path))
    assert [r["event"] for r in rows] == ["consult"], rows


def test_a_missing_gap_is_never_recorded_as_supplied(tmp_path):
    _write(tmp_path, _report(_req(state="missing")))
    assert [r["event"] for r in _rows(_run_file(tmp_path))] == ["consult"]


# == 2. the denominator: a consult row only when the table was on hand ========
def test_a_pass_with_no_table_on_hand_writes_nothing(tmp_path):
    status = _write(tmp_path, _report(), situation=SituationReport())
    assert status["consulted"] is False and status["written"] == 0
    assert not _run_file(tmp_path).exists()


def test_a_pass_with_the_table_on_hand_but_no_hit_still_counts_as_consulted(tmp_path):
    """The denominator must include the misses -- a rate computed only over runs that
    hit would be 100% by construction."""
    status = _write(tmp_path, _report())
    assert status["consulted"] is True
    assert [r["event"] for r in _rows(_run_file(tmp_path))] == ["consult"]


def test_an_annotated_live_entry_counts_as_the_table_on_hand(tmp_path):
    """compose_table's OTHER shape: a matched live capability carries table_item_id."""
    item = TableItem(id="t-cap", kind="tool", name="push", ref={"tool_id": "gh_push"})
    sit = compose_table(SituationReport(capabilities=[CapabilityRef(tool_id="gh_push")]),
                        [item])
    status = _write(tmp_path, _report(), situation=sit)
    assert status["consulted"] is True


def test_the_snapshot_dict_shape_of_the_situation_is_read_too(tmp_path):
    """A resumed run carries its situation as a model_dump dict, not a model."""
    status = _write(tmp_path, _report(_req()),
                    situation=_table_situation().model_dump(mode="json"))
    assert status["consulted"] is True and status["written"] == 2


def test_no_root_execution_id_writes_nothing(tmp_path):
    """No key, no record -- never a file named after ``None``."""
    status = tpr.record_binder_pass(_report(_req()), _table_situation(),
                                    root_execution_id=None, execution_id=None,
                                    site="preloop", data_dir=tmp_path / "data")
    assert status["written"] == 0
    assert not (tmp_path / "data").exists()


# == 3. one run, many passes: resumes share the root, rows are not repeated ====
def test_a_resume_appends_to_the_same_root_file(tmp_path):
    _write(tmp_path, _report(_req()), execution_id="exec_root1")
    _write(tmp_path, _report(_req(schema_path="subject", table_item_id="t-2")),
           situation=_table_situation(name="subject", item_id="t-2"),
           execution_id="exec_resume2")
    rows = _rows(_run_file(tmp_path))
    assert [r["event"] for r in rows] == ["consult", "supplied", "supplied"]
    assert {r["execution_id"] for r in rows} == {"exec_root1", "exec_resume2"}
    assert {r["root_execution_id"] for r in rows} == {"exec_root1"}


def test_a_repeated_pass_does_not_repeat_rows(tmp_path):
    """The mid-loop binder runs on EVERY tool call. One line per requirement the
    table supplied -- not one per loop iteration."""
    for _ in range(5):
        _write(tmp_path, _report(_req()))
    assert [r["event"] for r in _rows(_run_file(tmp_path))] == ["consult", "supplied"]


def test_a_later_silent_bind_of_the_same_requirement_is_still_recorded(tmp_path):
    """Dedupe must never hide the tripwire: confirm-then-silent is two facts."""
    _write(tmp_path, _report(_req()))
    _write(tmp_path, _report(_req(value_origin="operator", state="have")))
    bound = [r["bound_as"] for r in _rows(_run_file(tmp_path)) if r["event"] == "supplied"]
    assert bound == ["prefilled_confirm", "silent"]


def test_the_record_survives_the_resume_snapshot_being_consumed(tmp_path):
    """The defect this record replaces: delete_snapshot removed the only copy."""
    from systemu.runtime.execution_snapshot import (
        ExecutionSnapshot, delete_snapshot, write_snapshot)
    data = tmp_path / "data"
    write_snapshot(ExecutionSnapshot(execution_id="exec_root1", shadow_id="s",
                                     scroll_id="c"), data_dir=data)
    _write(tmp_path, _report(_req()))
    assert delete_snapshot("exec_root1", data_dir=data) is True
    rec = tpr.read_run("exec_root1", data_dir=data)
    assert rec is not None and len(rec.supplied) == 1


# == 4. concurrency: atomic append under a lock ===============================
def test_concurrent_passes_lose_no_row_and_tear_no_line(tmp_path):
    """8 threads x 25 distinct requirements against ONE root file. Every row lands,
    every line parses, and exactly one consult row exists."""
    def _worker(t):
        for i in range(25):
            _write(tmp_path, _report(_req(schema_path=f"p{t}_{i}")))

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    rows = _rows(_run_file(tmp_path))       # raises on a torn line
    assert sum(1 for r in rows if r["event"] == "consult") == 1
    assert sum(1 for r in rows if r["event"] == "supplied") == 200


# == 5. reading it back ========================================================
def test_read_run_projects_the_supplied_rows(tmp_path):
    _write(tmp_path, _report(_req(), _req(schema_path="subject")))
    rec = tpr.read_run("exec_root1", data_dir=tmp_path / "data")
    assert rec.consulted is True
    assert [s["schema_path"] for s in rec.supplied] == ["recipient", "subject"]
    assert rec.silent == 0 and rec.prefilled_confirm == 2


def test_read_run_on_an_absent_record_is_none(tmp_path):
    assert tpr.read_run("exec_nope", data_dir=tmp_path / "data") is None


def test_a_malformed_line_is_skipped_and_counted_not_fatal(tmp_path):
    _write(tmp_path, _report(_req()))
    with _run_file(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write("{torn\n")
    rec = tpr.read_run("exec_root1", data_dir=tmp_path / "data")
    assert rec.rows_skipped == 1 and len(rec.supplied) == 1


def test_read_run_dedupes_but_keeps_any_silent_reading(tmp_path):
    """The projection keeps ONE entry per requirement, and a requirement that was ever
    recorded silent stays silent -- a later confirm must not launder the alarm."""
    _write(tmp_path, _report(_req(value_origin="operator", state="have")))
    _write(tmp_path, _report(_req()))
    rec = tpr.read_run("exec_root1", data_dir=tmp_path / "data")
    assert len(rec.supplied) == 1 and rec.silent == 1 and rec.prefilled_confirm == 0


def test_read_run_keeps_a_silent_reading_that_arrives_after_a_confirm(tmp_path):
    """The other order: the first row for a requirement is a confirm, a LATER pass
    binds it silently. The projection must report the alarm, not the first row."""
    _write(tmp_path, _report(_req()))
    _write(tmp_path, _report(_req(value_origin="operator", state="have")))
    rec = tpr.read_run("exec_root1", data_dir=tmp_path / "data")
    assert len(rec.supplied) == 1
    assert rec.silent == 1 and rec.prefilled_confirm == 0


def test_find_run_resolves_a_resumed_execution_to_its_root(tmp_path):
    """The Work card knows the CURRENT execution id; after a resume that is not the
    root. The record must still be found."""
    _write(tmp_path, _report(_req()), execution_id="exec_resume2")
    rec = tpr.find_run(execution_id="exec_resume2", data_dir=tmp_path / "data")
    assert rec is not None and rec.root_execution_id == "exec_root1"
    assert tpr.find_run(execution_id="exec_root1",
                        data_dir=tmp_path / "data").root_execution_id == "exec_root1"


def test_find_run_resolves_a_chat_submission_to_its_root(tmp_path):
    """The chat card has its submission id while the run is in flight, and no
    execution id until the run ends."""
    sub = "2026-10-19T08:15:30.123456"          # chars a filename cannot hold
    _write(tmp_path, _report(_req()), chat_submission_id=sub)
    rec = tpr.find_run(chat_submission_id=sub, data_dir=tmp_path / "data")
    assert rec is not None and rec.root_execution_id == "exec_root1"


def test_find_run_with_nothing_recorded_is_none(tmp_path):
    assert tpr.find_run(execution_id="exec_x", chat_submission_id="s",
                        data_dir=tmp_path / "data") is None
    assert tpr.find_run(data_dir=tmp_path / "data") is None


@pytest.mark.parametrize("hostile", ["../../escape", "..\\..\\escape", "a/b", "C:x"])
def test_a_hostile_id_never_names_a_path_outside_the_record_dir(tmp_path, hostile):
    base = tmp_path / "data" / "audit" / "table_payoff"
    p = tpr.run_record_path(hostile, data_dir=tmp_path / "data")
    assert p.parent == base and p.name.startswith("run_")
    _write(tmp_path, _report(_req()), root_execution_id=hostile,
           execution_id="exec_other", chat_submission_id=hostile)
    leaked = [q for q in tmp_path.rglob("*")
              if q.is_file() and base not in q.parents]
    assert not leaked, leaked
    assert tpr.read_run(hostile, data_dir=tmp_path / "data") is not None


def test_scan_records_counts_every_run_file_and_names_the_unreadable(tmp_path):
    _write(tmp_path, _report(_req()), root_execution_id="exec_a", execution_id="exec_a")
    _write(tmp_path, _report(), root_execution_id="exec_b", execution_id="exec_b")
    scan = tpr.scan_records(data_dir=tmp_path / "data")
    assert sorted(r.root_execution_id for r in scan.records) == ["exec_a", "exec_b"]
    assert scan.unreadable == 0
    assert scan.searched.endswith("run_*.jsonl")


def test_scan_records_on_an_absent_directory_is_empty_not_an_error(tmp_path):
    scan = tpr.scan_records(data_dir=tmp_path / "data")
    assert scan.records == () and scan.unreadable == 0
