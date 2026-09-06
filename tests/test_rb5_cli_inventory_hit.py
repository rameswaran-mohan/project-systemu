"""R-B5 / T5 (spec section 10, section 5.10.e AC7) -- the inventory-hit metric,
WIRED into the shipped CLI metrics surface.

``table_payoff.inventory_hit_report`` / ``format_inventory_hit`` were fully unit
tested and had ZERO production callers: the mechanism existed, the operator-visible
payoff never rendered. These are the REACHABILITY pins for the wiring. They drive the
real command (``systemu debug avoidable-ask``) through ``CliRunner`` -- the same way
``test_glearn_s4_threshold_evidence.test_cli_debug_avoidable_ask_renders`` drives it --
so a green here cannot mean "the helper works in isolation" again.

WHERE THE INPUT COMES FROM, and why these fixtures are written the way they are.
A run's ``RequirementReport`` survives the run in exactly one place:
``ExecutionSnapshot.requirement_report`` (systemu/runtime/execution_snapshot.py:102,
serialised at :272) on disk at
``<data_dir>/audit/exec_<execution_id>/resume_snapshot.json`` (:54). Everywhere else
it lives only on ``context._requirement_report`` for the life of the process
(shadow_runtime.py:1545). So the fixtures below are written by the REAL
``write_snapshot`` from a REAL ``RequirementReport``, not by hand-rolling the JSON:
a hand-rolled fixture would keep passing after the persisted shape moved, which is
precisely the failure mode this file exists to close.

AC7 is about a SPLIT, not a total: ``silent`` and ``prefilled_confirm`` must be
rendered as separate counts. Summing them would let a collapse in ``silent`` (which
is the DEC-25 clamp's healthy-is-structurally-rare reading) hide behind confirms.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from systemu.core.models import Requirement, RequirementReport
from systemu.interface import cli_commands as cc
from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot

_CLI_SOURCE = Path(cc.__file__)


# -- harness -----------------------------------------------------------------
def _req(**over) -> Requirement:
    base = dict(kind="decision", schema_path="/p", state="have", source="situation",
                value_origin="content_derived", confidence=0.8)
    base.update(over)
    return Requirement(**base)


def _persist(data_dir: Path, execution_id: str, reqs) -> Path:
    """Persist one run's RequirementReport through the REAL snapshot writer."""
    report = RequirementReport(per_objective={1: list(reqs)}, ask_bundle=[])
    snap = ExecutionSnapshot(
        execution_id=execution_id, shadow_id="sh", scroll_id="sc",
        requirement_report=report.model_dump(mode="json"),
    )
    path = write_snapshot(snap, data_dir=data_dir)
    assert path is not None, "fixture premise: the snapshot writer wrote the file"
    return Path(path)


@pytest.fixture()
def vault(tmp_path):
    from systemu.vault.vault import Vault
    return Vault(str(tmp_path / "vault"))


def _run(monkeypatch, tmp_path, vault):
    """Invoke the REAL metrics command with cwd at ``tmp_path``.

    The command resolves its snapshot directory the way production does (relative
    ``data/``, matching execution_snapshot.py:135's ``data_dir or "data"``), so the
    chdir -- not an injected seam -- is what points it at the fixtures. A seam the
    product does not use would leave the real path unexercised.
    """
    monkeypatch.setattr(cc, "_get_vault_and_config", lambda ctx: (None, vault))
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output
    return res.output


def _inventory_lines(output: str):
    return [ln for ln in output.splitlines() if "Inventory-hit" in ln]


# == 1. AC7 -- the split renders, over the REAL persisted input ===============
def test_cli_renders_silent_and_prefilled_confirm_as_separate_counts(
        monkeypatch, tmp_path, vault):
    """AC7. One silent bind and one pre-filled confirm, persisted by two DIFFERENT
    runs, so this also pins that the command reads the whole snapshot set rather
    than one file."""
    _persist(tmp_path / "data", "run-a",
             [_req(schema_path="/a", value_origin="operator")])      # -> silent
    _persist(tmp_path / "data", "run-b",
             [_req(schema_path="/b", value_origin="content_derived",  # -> confirm
                   table_item_id="t-1")])

    out = _run(monkeypatch, tmp_path, vault)

    assert "bound with no ask: 1" in out, out
    assert "pre-filled one-click confirm: 1" in out, out
    # the two are NEVER summed into one headline
    assert "bound with no ask: 2" not in out
    assert "Inventory-hit rate: 100% (2/2" in out, out
    assert "from your table: 1 of 1" in out, out


def test_the_split_is_a_split_not_a_relabelled_total(monkeypatch, tmp_path, vault):
    """Mutation guard for AC7: with only confirms on disk the silent count must read
    0 and the confirm count must read the real number. A formatter that printed the
    same total twice would pass the test above and fail here."""
    _persist(tmp_path / "data", "run-c", [
        _req(schema_path="/a", value_origin="content_derived"),
        _req(schema_path="/b", value_origin="content_derived"),
    ])
    out = _run(monkeypatch, tmp_path, vault)
    assert "bound with no ask: 0" in out, out
    assert "pre-filled one-click confirm: 2" in out, out


# == 2. the empty input set is NOT MEASURED, never a fabricated 0% ===========
def test_cli_reports_not_measured_when_no_run_has_persisted_a_report(
        monkeypatch, tmp_path, vault):
    """An unmeasured population is a different claim from a measured zero. Rendering
    ``0%`` here would launder "nothing to read" into a quotable headline -- the same
    rule ``resolver_replay.format_resolver_replay`` states as "this is NOT 0%"."""
    assert not (tmp_path / "data").exists(), "fixture premise: no snapshots on disk"

    out = _run(monkeypatch, tmp_path, vault)
    lines = _inventory_lines(out)

    assert lines, "the inventory-hit surface did not render at all"
    assert any("NOT MEASURED" in ln for ln in lines), lines
    assert not any("Inventory-hit rate:" in ln for ln in lines), \
        "a rate was rendered over an empty population"
    assert "bound with no ask: 0" not in out, \
        "zeros were rendered for a population that was never measured"


def test_not_measured_says_it_is_not_a_zero(monkeypatch, tmp_path, vault):
    """The honesty half: the line must say so in words, not merely omit the number."""
    out = _run(monkeypatch, tmp_path, vault)
    assert "NOT 0%" in out, out


# == 3. reachability -- the command really consults the metric ===============
def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name}: function {name!r} not found")


def _called_names(fn: ast.FunctionDef):
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_the_metrics_command_consults_inventory_hit_report():
    """THE reachability pin. Delete the call site in ``debug_avoidable_ask`` and this
    test goes red -- which is the whole point: R-B5 shipped once already with both
    payoff helpers fully unit tested and no production caller at all."""
    called = _called_names(_function_node(_CLI_SOURCE, "debug_avoidable_ask"))
    assert "inventory_hit_report" in called, \
        "the metrics command no longer consults inventory_hit_report"
    assert "format_inventory_hit" in called, \
        "the metrics command no longer renders the inventory-hit lines"


def test_the_metric_is_read_only_no_novelty_ledger_from_the_cli():
    """Slice boundary: this surface REPORTS. ``answered_from_table`` writes the
    novelty ledger, so calling it from a printout would burn an item's novelty every
    time an operator ran a metrics command."""
    called = _called_names(_function_node(_CLI_SOURCE, "debug_avoidable_ask"))
    assert "answered_from_table" not in called
    assert "using_from_table" not in called


# == 4. a metrics printout may never adjudicate a run ========================
def test_an_unreadable_snapshot_is_skipped_and_the_rest_still_counts(
        monkeypatch, tmp_path, vault):
    """``read_snapshot`` raises ``SnapshotRefused`` on a newer schema (DEC-9) -- right
    for a resume, wrong for a printout. A corrupt or future-schema file must degrade
    to "not counted", never to a crashed metrics command."""
    _persist(tmp_path / "data", "run-good",
             [_req(schema_path="/a", value_origin="operator")])

    junk = tmp_path / "data" / "audit" / "exec_run-bad"
    junk.mkdir(parents=True, exist_ok=True)
    (junk / "resume_snapshot.json").write_text("{not json", encoding="utf-8")

    future = tmp_path / "data" / "audit" / "exec_run-future"
    future.mkdir(parents=True, exist_ok=True)
    (future / "resume_snapshot.json").write_text(
        json.dumps({"execution_id": "run-future", "schema_version": 9999,
                    "requirement_report": "not-a-dict"}), encoding="utf-8")

    out = _run(monkeypatch, tmp_path, vault)
    assert "bound with no ask: 1" in out, out
