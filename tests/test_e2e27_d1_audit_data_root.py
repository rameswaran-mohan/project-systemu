"""D1 -- the audit data root is minted once, and never read off the CLI's cwd.

WITNESSED DEFECT (v0.10.27, scratch install)
    ``systemu debug avoidable-ask`` rendered ``Inventory-hit rate: 75% (3/4 ...)``
    when the operator happened to be standing in the directory the daemon was
    started from, and ``Inventory-hit: NOT MEASURED -- no run has persisted a
    RequirementReport yet`` from anywhere else -- with the SAME vault env and the
    SAME snapshot file on disk. The second is a GLOBAL claim about the corpus
    derived from a LOCAL accident of the shell's working directory.

    Both ends resolved the string ``"data"`` against their own process cwd:
    ``execution_snapshot.write_snapshot`` (the daemon child, spawned with
    ``cwd=operating_home``) and ``cli_commands._persisted_requirement_rows`` (the
    operator's shell, standing wherever they stand).

THE PIN
    ONE resolver -- ``execution_snapshot.audit_data_root`` -- rooted at the
    vault-root mint, consumed by BOTH the writer and the reader. The tests below
    drive the two ends from DIFFERENT working directories with one shared vault
    env, which is the only arrangement that can tell a minted answer apart from a
    cwd-relative one. An explicit ``data_dir`` still wins (the daemon's own
    callers pass one), so the fix cannot move any existing caller's files.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import vault_root as vr
from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot, audit_data_root, write_snapshot,
)
from systemu.vault.vault import Vault


def _requirement_report() -> dict:
    """A persisted RequirementReport with 3 of 4 inventory-supplied requirements
    kept out of a from-scratch gap -- the witnessed ``75% (3/4 ...)`` shape."""
    def req(state: str) -> dict:
        return {"source": "situation", "state": state, "value_origin": "operator"}
    return {"per_objective": {"1": [req("have"), req("have"), req("have"),
                                    req("missing")]}}


def _record_runs(prefix: str) -> None:
    """Four runs through the PRODUCTION record writer (no ``data_dir``, exactly as
    ``shadow_runtime`` calls it), three of which the table answered -- the witnessed
    ``75% (3/4 ...)`` shape, in the durable record."""
    from systemu.core.models import Requirement, RequirementReport
    from systemu.runtime import table_payoff_record as tpr
    from systemu.runtime.situational_inventory import SituationReport, compose_table
    from systemu.runtime.table_store import TableItem
    item = TableItem(id="t-1", kind="service", name="recipient",
                     ref={"server": "https://example.invalid/t1"})
    hit = Requirement(kind="decision", schema_path="/a", state="have",
                      source="situation", value_origin="content_derived",
                      confidence=0.8, table_item_id="t-1")
    for i, reqs in enumerate(([hit], [hit], [hit], [])):
        status = tpr.record_binder_pass(
            RequirementReport(per_objective={1: reqs}),
            compose_table(SituationReport(), [item]),
            root_execution_id=f"{prefix}{i}", execution_id=f"{prefix}{i}",
            site="midloop")
        assert status["consulted"] and status["error"] is None, status


def _layout(tmp_path, monkeypatch):
    """A home with a vault, an unrelated directory to stand in, and the ONE env
    var both processes share in the witnessed repro."""
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    vault_dir = home / "systemu" / "vault"
    vault_dir.mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.setenv(vr.VAULT_DIR_ENV, str(vault_dir))
    return home, elsewhere, vault_dir


def _run_report(monkeypatch, vault_dir):
    monkeypatch.setattr(cc, "_get_vault_and_config",
                        lambda ctx: (None, Vault(str(vault_dir))))
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output
    return res.output


# --------------------------------------------------------------------------- #
# THE PIN: writer and reader agree across two working directories
# --------------------------------------------------------------------------- #

def test_the_inventory_hit_block_reads_the_writers_record_from_another_cwd(
        tmp_path, monkeypatch):
    """Write through the PRODUCTION record-writer path (no ``data_dir``, exactly as
    ``shadow_runtime`` calls it) standing in the home; read from somewhere else.
    (Re-pointed from the retired resume-snapshot reader to the durable record.)"""
    home, elsewhere, vault_dir = _layout(tmp_path, monkeypatch)

    monkeypatch.chdir(home)
    _record_runs("exec_d1a")
    assert (home / "data" / "audit" / "table_payoff").is_dir(), "fixture premise"

    monkeypatch.chdir(elsewhere)
    out = _run_report(monkeypatch, vault_dir)

    assert "Inventory-hit (table record): 75% of runs (3/4" in out, out
    assert "Inventory-hit (table record): NOT MEASURED" not in out, (
        "the reader answered from the operator's cwd, not from the minted root")


def test_the_snapshot_writer_roots_the_audit_dir_at_the_mint_not_the_cwd(
        tmp_path, monkeypatch):
    """The other end of the same agreement: the writer's default is the mint too,
    so a run started from an unrelated directory still writes where the reader
    looks."""
    home, elsewhere, _vault_dir = _layout(tmp_path, monkeypatch)

    monkeypatch.chdir(elsewhere)
    written = write_snapshot(ExecutionSnapshot(
        execution_id="d1b", shadow_id="sh", scroll_id="sc",
        requirement_report=_requirement_report()))

    assert written == home / "data" / "audit" / "exec_d1b" / "resume_snapshot.json", (
        f"snapshot landed at {written}")
    assert not (elsewhere / "data").exists(), (
        "the writer still resolved 'data' against its own cwd")


def test_the_not_measured_line_names_the_directory_it_searched(
        tmp_path, monkeypatch):
    """A legitimate NOT MEASURED must be a claim about a NAMED directory. The
    witnessed line made a global assertion ('no run has persisted a
    RequirementReport yet') that was false for the machine it was printed on."""
    home, elsewhere, vault_dir = _layout(tmp_path, monkeypatch)

    monkeypatch.chdir(elsewhere)
    out = _run_report(monkeypatch, vault_dir)

    line = next((ln for ln in out.splitlines()
                 if ln.startswith("Inventory-hit (table record):")), None)
    assert line is not None and "NOT MEASURED" in line, out
    assert "NOT 0%" in line, line
    assert str(home / "data" / "audit" / "table_payoff") in out, (
        "the unmeasured verdict does not name the directory it searched:\n" + out)


# --------------------------------------------------------------------------- #
# The resolver itself
# --------------------------------------------------------------------------- #

def test_the_resolver_is_byte_identical_to_the_old_relative_answer_by_default(
        tmp_path, monkeypatch):
    """With no vault env set, the mint's default IS ``<cwd>/systemu/vault``, so the
    audit root stays ``<cwd>/data`` -- the location every existing install already
    holds. The fix may not relocate anybody's files."""
    monkeypatch.delenv(vr.VAULT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert audit_data_root() == Path(tmp_path).resolve() / "data"


def test_an_explicit_data_dir_still_wins(tmp_path, monkeypatch):
    """The daemon's own callers (``scheduler/jobs.py``, ``supervisor.py``) pass an
    explicit ``data_dir``; the resolver must never override one."""
    monkeypatch.setenv(vr.VAULT_DIR_ENV, str(tmp_path / "v"))
    assert audit_data_root(tmp_path / "given") == Path(tmp_path / "given")
