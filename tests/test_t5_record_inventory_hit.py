"""T5 (DEC-27) -- the inventory-hit metric over the DURABLE per-run table record.

The denominator is the set of runs that CONSULTED the table (a ``consult`` row in the
record), witnessed by the same record that supplies the numerator -- never "whatever
resume snapshots happen to survive". An empty record is NOT MEASURED, never 0%. A run
file that exists but cannot be read is COUNTED and named, never silently dropped.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from click.testing import CliRunner

from systemu.core.models import Requirement, RequirementReport
from systemu.interface import cli_commands as cc
from systemu.runtime import table_payoff
from systemu.runtime import table_payoff_record as tpr
from systemu.runtime.situational_inventory import SituationReport, compose_table
from systemu.runtime.table_payoff_record import RecordScan, RunRecord
from systemu.runtime.table_store import TableItem


def _row(item="t-1", bound="prefilled_confirm", path="p"):
    return {"event": "supplied", "table_item_id": item, "item_name": item,
            "schema_path": path, "objective_id": 1, "tool": "send", "state": "have",
            "bound_as": bound}


def _rec(root, *rows, consulted=True):
    return RunRecord(root_execution_id=root, consulted=consulted, supplied=tuple(rows),
                     executions=frozenset({root}))


def _scan(*records, unreadable=0, error=None):
    return RecordScan(searched="/x/audit/table_payoff/run_*.jsonl",
                      records=tuple(records), unreadable=unreadable, error=error)


def _lines(scan):
    return table_payoff.format_record_inventory_hit(
        table_payoff.record_inventory_hit_report(scan))


# == the report =================================================================
def test_the_denominator_is_runs_that_consulted_the_table():
    rep = table_payoff.record_inventory_hit_report(_scan(
        _rec("a", _row(path="x"), _row(path="y")), _rec("b", _row()), _rec("c")))
    assert rep["runs_consulted"] == 3 and rep["runs_hit"] == 2
    assert rep["rate"] == pytest.approx(2 / 3)
    assert rep["supplied"] == 3 and rep["prefilled_confirm"] == 3 and rep["silent"] == 0
    assert rep["measured"] is True


def test_an_empty_record_has_no_rate_at_all():
    rep = table_payoff.record_inventory_hit_report(_scan())
    assert rep["measured"] is False and rep["rate"] is None


def test_silent_is_reported_apart_from_confirms_never_summed():
    rep = table_payoff.record_inventory_hit_report(_scan(
        _rec("a", _row(path="x"), _row(path="y", bound="silent"))))
    assert rep["silent"] == 1 and rep["prefilled_confirm"] == 1


# == the lines ==================================================================
def test_the_measured_lines_state_the_rate_over_the_witnessed_population():
    lines = _lines(_scan(_rec("a", _row()), _rec("b")))
    assert lines[0] == ("Inventory-hit (table record): 50% of runs (1/2 runs that "
                        "had your table on hand had it answer at least one requirement)")
    assert any("answered from your table: 1" in ln and "pre-filled one-click confirm: 1"
               in ln and "silent: 0" in ln for ln in lines), lines
    assert any("2 run records, 0 unreadable" in ln for ln in lines), lines


def test_an_empty_record_is_not_measured_and_says_it_is_not_zero():
    lines = _lines(_scan())
    assert "NOT MEASURED" in lines[0] and "NOT 0%" in lines[0]
    assert not any("% of runs" in ln for ln in lines)
    assert any("/x/audit/table_payoff/run_*.jsonl" in ln for ln in lines), lines


def test_an_unreadable_run_record_is_named_not_dropped():
    lines = _lines(_scan(_rec("a", _row()), unreadable=2))
    assert any("2 run record(s) could not be read" in ln and "EXCLUDES" in ln
               for ln in lines), lines


def test_all_records_unreadable_is_not_measured_without_claiming_no_run_consulted():
    """DEC-27: 'I could not read them' and 'there were none' are different claims."""
    lines = _lines(_scan(unreadable=3))
    assert "NOT MEASURED" in lines[0]
    assert "could not be read" in lines[0]
    assert "no run has consulted" not in " ".join(lines)


def test_a_directory_that_cannot_be_listed_is_not_measured_and_says_why():
    lines = _lines(_scan(error="PermissionError"))
    assert "NOT MEASURED" in lines[0] and "PermissionError" in lines[0]


def test_a_silent_table_bind_puts_the_clamp_alarm_first():
    lines = _lines(_scan(_rec("a", _row(bound="silent"))))
    assert lines[0].startswith("  !! CLAMP REGRESSION")


@pytest.mark.parametrize("scan", [_scan(), _scan(_rec("a", _row(bound="silent"))),
                                  _scan(unreadable=1), _scan(error="E")])
def test_every_line_is_ascii(scan):
    for ln in _lines(scan):
        assert ln.isascii(), ln


# == the CLI reads the record ===================================================
class _Vault:
    def __init__(self, root):
        self.root = str(root)


@pytest.fixture()
def audit_home(tmp_path, monkeypatch):
    from systemu.runtime import vault_root as vr
    monkeypatch.delenv(vr.VAULT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "data"


def _record(data_dir, root, *, hit=True):
    item = TableItem(id="t-1", kind="service", name="recipient",
                     ref={"server": "https://example.invalid/t1"})
    reqs = [Requirement(kind="decision", schema_path="recipient", state="have",
                        source="situation", value_origin="content_derived",
                        confidence=0.8, table_item_id="t-1")] if hit else []
    tpr.record_binder_pass(RequirementReport(per_objective={1: reqs}),
                           compose_table(SituationReport(), [item]),
                           root_execution_id=root, execution_id=root, site="midloop",
                           data_dir=data_dir)


def _cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_get_vault_and_config",
                        lambda ctx: (None, _Vault(tmp_path / "vault")))
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output
    return res.output


def test_the_cli_measures_runs_whose_resume_snapshot_was_consumed(audit_home,
                                                                  monkeypatch, tmp_path):
    """THE defect: a run that suspended and resumed lost its snapshot and dropped out
    of the old population. The record keeps it."""
    from systemu.runtime.execution_snapshot import (
        ExecutionSnapshot, delete_snapshot, write_snapshot)
    _record(audit_home, "exec_a", hit=True)
    _record(audit_home, "exec_b", hit=False)
    write_snapshot(ExecutionSnapshot(execution_id="exec_a", shadow_id="s", scroll_id="c"))
    assert delete_snapshot("exec_a") is True
    out = _cli(monkeypatch, tmp_path)
    assert ("Inventory-hit (table record): 50% of runs (1/2 runs that had your table "
            "on hand had it answer at least one requirement)") in out, out


def test_the_cli_reports_not_measured_on_an_empty_record(audit_home, monkeypatch,
                                                         tmp_path):
    out = _cli(monkeypatch, tmp_path)
    line = next(ln for ln in out.splitlines()
                if ln.startswith("Inventory-hit (table record):"))
    assert "NOT MEASURED" in line and "NOT 0%" in line
    assert str(audit_home / "audit" / "table_payoff") in out


def test_the_cli_metric_reader_is_the_record(monkeypatch, tmp_path):
    """SEAM pin: what the CLI prints for this block comes from scan_records."""
    monkeypatch.setattr(tpr, "scan_records",
                        lambda **k: _scan(_rec("a", _row()), _rec("b", _row()),
                                          _rec("c"), _rec("d")))
    out = _cli(monkeypatch, tmp_path)
    assert "Inventory-hit (table record): 50% of runs (2/4" in out, out


def test_the_cli_block_calls_the_record_reader_and_its_report():
    """AST pin: delete the call site and this goes red."""
    tree = ast.parse(Path(cc.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "debug_avoidable_ask")
    called = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", None))
    assert {"scan_records", "record_inventory_hit_report",
            "format_record_inventory_hit"} <= called, called


# == the snapshot-read block is RETIRED (ruled) ================================
def test_the_snapshot_reading_inventory_hit_helpers_are_retired():
    """The snapshot-read inventory-hit block measured a population that shrank with
    every resume (DEC-27) and is retired; its helpers had no other production caller
    and go with it. inventory_hit_report STAYS: the runtime clamp_tripwire uses it."""
    assert not hasattr(cc, "_persisted_requirement_rows")
    assert not hasattr(table_payoff, "format_inventory_hit")
    assert callable(getattr(table_payoff, "inventory_hit_report", None))
    assert callable(getattr(table_payoff, "clamp_tripwire", None))


def test_the_command_no_longer_points_at_resume_snapshots(audit_home, monkeypatch,
                                                          tmp_path):
    """The resume snapshots still exist (they serve resume); the metrics command just
    no longer reads or names them."""
    out = _cli(monkeypatch, tmp_path)
    assert "resume_snapshot" not in out, out
    assert "persisted RequirementReport" not in out, out
