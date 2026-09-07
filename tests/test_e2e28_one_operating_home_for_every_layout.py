"""N9 -- ONE operating home, for every vault layout, owned by the mint.

WITNESSED (v0.10.28, reading the code after the D1 fix landed)
    ``execution_snapshot.audit_data_root()`` had two branches. Branch 1 inverts
    the DEFAULT layout: strip ``systemu/vault`` off the minted root and the home
    falls out, identically in every process that shares the vault env. Branch 2
    -- taken whenever the vault dir is an absolute path that does NOT end in
    ``systemu/vault``, which is exactly the Docker/mount shape the mint says it
    honours verbatim -- fell back to ``resolve_vault_root().home``, and that
    field is the PROCESS CWD.

    So the reader/writer invariance D1 bought held for the default layout only.
    On a mounted vault the writer (the daemon child, cwd = its operating home)
    and the reader (the operator's shell, standing anywhere) resolved two
    different audit trees again, and ``debug avoidable-ask`` would answer
    "NOT MEASURED" about a corpus that exists -- the D1 defect, in the layout
    D1 did not cover.

    ``scheduler/daemon.py`` derived the daemon child's cwd separately, from the
    same ``.home`` field. Two derivations of one fact, and on the non-default
    layout both of them were the accident of whoever called first.

THE RULING PINNED HERE
    ONE definition of the operating home for every layout, owned by the
    vault-root mint: the vault dir's parent, unless the layout is the default
    ``<home>/systemu/vault``, in which case ``<home>``. It is a pure function of
    the resolved root, so every process that resolves the same root gets the
    same home no matter where it was launched. BOTH the daemon's operating home
    and ``audit_data_root()`` consume that one field, and neither consults a cwd.

WHY THE MINT KEEPS ``home`` AS WELL
    ``home`` answers a DIFFERENT question -- "which directory was this root
    resolved against" -- and the fence needs it: the source-checkout carve-out
    asks whether the operator is STANDING in the tree that contains the package,
    which is a fact about the cwd and about nothing else. Collapsing the two
    would have moved the carve-out onto a root-derived directory, and a vault
    pointed at ``<site-packages>/systemu/vault`` would then have carved itself
    out of its own refusal. The tests below hold both fields to their own
    meaning and pin that no consumer of the operating home reads the other one.

NO DAEMON IS STARTED HERE. The operating-home derivation is called directly.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import execution_snapshot as es
from systemu.runtime import vault_root as vr
from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot, audit_data_root, write_snapshot,
)
from systemu.scheduler import daemon as daemon_mod
from systemu.vault.vault import Vault


def _requirement_report() -> dict:
    """3 of 4 inventory-supplied requirements kept out of a from-scratch gap."""
    def req(state: str) -> dict:
        return {"source": "situation", "state": state, "value_origin": "operator"}
    return {"per_objective": {"1": [req("have"), req("have"), req("have"),
                                    req("missing")]}}


def _mounted_layout(tmp_path, monkeypatch):
    """A NON-DEFAULT absolute vault dir -- the Docker-mount shape.

    ``<tmp>/mount/vault`` does not end in ``systemu/vault``, so it is precisely
    the layout branch 1 could not invert and branch 2 answered with a cwd.
    """
    mount = tmp_path / "mount"
    vault_dir = mount / "vault"
    elsewhere = tmp_path / "elsewhere"
    vault_dir.mkdir(parents=True)
    elsewhere.mkdir()
    assert not str(vault_dir).replace("\\", "/").endswith("systemu/vault"), (
        "fixture premise: this layout must NOT be the default one")
    monkeypatch.setenv(vr.VAULT_DIR_ENV, str(vault_dir))
    return mount, elsewhere, vault_dir


# --------------------------------------------------------------------------- #
# THE MINT
# --------------------------------------------------------------------------- #

def test_the_operating_home_of_a_mounted_vault_is_its_parent_not_the_cwd(
        tmp_path, monkeypatch):
    mount, elsewhere, vault_dir = _mounted_layout(tmp_path, monkeypatch)

    monkeypatch.chdir(elsewhere)
    v = vr.resolve_vault_root()

    assert Path(v.operating_home) == mount.resolve(), (
        f"operating home {v.operating_home!r} for root {v.root!r}")
    assert Path(v.operating_home) != Path(os.getcwd()).resolve(), (
        "the operating home is still the accident of where this process stands")


def test_the_operating_home_is_the_same_from_every_working_directory(
        tmp_path, monkeypatch):
    """THE property. Two processes, one vault env, two cwds, one answer."""
    mount, elsewhere, _vault_dir = _mounted_layout(tmp_path, monkeypatch)

    monkeypatch.chdir(mount)
    here = vr.resolve_vault_root().operating_home
    monkeypatch.chdir(elsewhere)
    there = vr.resolve_vault_root().operating_home

    assert here == there, f"{here!r} != {there!r}"


def test_the_default_layout_still_names_the_directory_it_always_did(
        tmp_path, monkeypatch):
    """The fix may not relocate any existing install's files."""
    monkeypatch.delenv(vr.VAULT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    v = vr.resolve_vault_root()

    assert Path(v.operating_home) == Path(tmp_path).resolve()
    assert Path(v.operating_home) == Path(v.home), (
        "on the default layout the two fields must agree -- if they can differ "
        "here, every existing install is being moved")


def test_an_absolute_vault_that_IS_the_default_layout_inverts_to_its_grandparent(
        tmp_path, monkeypatch):
    """``SYSTEMU_VAULT_DIR=<X>/systemu/vault`` is the default layout stated
    absolutely; its home is ``<X>``, not ``<X>/systemu``."""
    x = tmp_path / "x"
    (x / "systemu" / "vault").mkdir(parents=True)
    monkeypatch.setenv(vr.VAULT_DIR_ENV, str(x / "systemu" / "vault"))
    monkeypatch.chdir(tmp_path)

    assert Path(vr.resolve_vault_root().operating_home) == x.resolve()


def test_the_fence_still_refuses_a_vault_inside_the_package(tmp_path, monkeypatch):
    """The carve-out must keep asking about the CWD.

    Were it moved onto the operating home, this root's home would BE the package
    parent, the carve-out would fire, and the vault that writes into
    site-packages would refuse nothing at all.
    """
    monkeypatch.chdir(tmp_path)
    v = vr.resolve_vault_root(explicit=str(vr.package_dir() / "vault"))

    assert v.inside_package is True
    assert v.refused is True, (
        "the package-tree fence stopped firing -- the carve-out is reading the "
        "operating home instead of where the operator is standing")


# --------------------------------------------------------------------------- #
# THE TWO CONSUMERS
# --------------------------------------------------------------------------- #

def test_the_writer_and_the_reader_agree_on_a_mounted_vault(tmp_path, monkeypatch):
    """The D1 property, in the layout D1 did not cover. The PRODUCTION writer
    (no ``data_dir``, exactly as ``shadow_runtime`` calls it) runs from one cwd
    and the reader from another."""
    mount, elsewhere, vault_dir = _mounted_layout(tmp_path, monkeypatch)

    monkeypatch.chdir(elsewhere)
    written = write_snapshot(ExecutionSnapshot(
        execution_id="n9a", shadow_id="sh", scroll_id="sc",
        requirement_report=_requirement_report()))
    assert written is not None and written.is_file(), "fixture premise: a snapshot"
    assert not (elsewhere / "data").exists(), (
        f"the writer resolved 'data' against its own cwd: {written}")

    monkeypatch.chdir(mount)
    monkeypatch.setattr(cc, "_get_vault_and_config",
                        lambda ctx: (None, Vault(str(vault_dir))))
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output

    assert "Inventory-hit rate: 75% (3/4" in res.output, res.output
    assert "Inventory-hit: NOT MEASURED" not in res.output, (
        "the reader answered from its own cwd, not from the minted home")


def test_the_audit_root_is_the_minted_home_for_a_mounted_vault(
        tmp_path, monkeypatch):
    mount, elsewhere, _vault_dir = _mounted_layout(tmp_path, monkeypatch)
    monkeypatch.chdir(elsewhere)

    assert audit_data_root() == mount.resolve() / es.DEFAULT_DATA_DIRNAME


def test_the_daemons_operating_home_is_the_same_field(tmp_path, monkeypatch):
    """No daemon is started: the derivation is a named function and it is CALLED.

    The daemon spawns its child with ``cwd=<this>``, and the child's explicit
    ``data_dir=Path("data")`` callers (``scheduler/jobs.py``) resolve against it.
    If it ever diverges from ``audit_data_root``'s answer the two trees split
    again, so the two must be pinned to one field, not merely look alike.
    """
    mount, elsewhere, vault_dir = _mounted_layout(tmp_path, monkeypatch)
    monkeypatch.chdir(elsewhere)

    home = daemon_mod.daemon_operating_home(str(vault_dir))

    assert Path(home) == mount.resolve()
    assert Path(home) == Path(vr.resolve_vault_root().operating_home)
    assert audit_data_root() == Path(home) / es.DEFAULT_DATA_DIRNAME, (
        "the daemon's cwd and the audit root no longer share one home")


# --------------------------------------------------------------------------- #
# REACHABILITY: no second derivation, no cwd fallback
# --------------------------------------------------------------------------- #

def test_audit_data_root_consumes_the_minted_operating_home():
    """Remove the consumption and this goes red."""
    src = inspect.getsource(audit_data_root)
    assert ".operating_home" in src, (
        "`audit_data_root` no longer consumes the minted operating home "
        "FIELD -- prose naming it is not consumption:\n" + src)


def test_audit_data_root_has_no_cwd_fallback_and_no_second_derivation():
    """The defect was not a missing fact -- it was a SECOND answer, reached
    whenever the first did not apply."""
    src = inspect.getsource(audit_data_root)
    for banned in ("getcwd", "Path.cwd", "verdict.home", ".home)"):
        assert banned not in src, (
            f"`audit_data_root` still reaches for {banned!r}:\n{src}")


def test_the_daemon_operating_home_is_not_re_derived_from_a_cwd():
    src = inspect.getsource(daemon_mod.daemon_operating_home)
    assert "operating_home" in src, src
    for banned in ("getcwd", "Path.cwd"):
        assert banned not in src, (
            f"the daemon re-derives its operating home from {banned!r}:\n{src}")


def test_the_spawn_uses_that_function_rather_than_a_copy_of_it():
    """A named function nobody calls is not one definition, it is two."""
    src = inspect.getsource(daemon_mod.start_daemon)
    assert "daemon_operating_home(" in src, (
        "`start_daemon` does not call `daemon_operating_home` -- the spawn is "
        "deriving the child's cwd a second way:\n" + src)
