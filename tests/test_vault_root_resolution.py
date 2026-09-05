"""VAULT ROOT — the daemon must never operate out of its own package tree.

THE LIVE DEFECT (observed v0.10.23+)
    `systemu daemon start` was launched from an EMPTY working directory (the
    package importable via PYTHONPATH).  The daemon resolved its OPERATING
    vault into the CHECKOUT ITSELF and wrote runtime state there --
    `systemu/.systemu_daemon.json`, `systemu/vault/.effect_tags_seed`,
    `.first_gate_review`, `.package_manifest_baseline.json`,
    `secrets/dashboard_session.secret`, `capabilities/`, `metrics/`, `table/`,
    `manual_events.jsonl`, two tool-body dirs -- and ran the effect-tags
    migration IN PLACE over the packaged seed tools (57 tracked files
    modified).  On a pip install that same shape mutates site-packages.

ROOT CAUSE
    `start_daemon` derived the child's working directory as

        cwd has .env/.systemu_mode ? cwd : Path(systemu.__file__).parent.parent

    and handed the child a RELATIVE `--vault-dir systemu/vault`.  The parent
    CLI resolved that relative path against ITS cwd; the child resolved the
    same string against the PACKAGE PARENT.  Two processes, one string, two
    different directories.  The divergence is cwd-SHAPE dependent, which is why
    a later boot from the same directory (once a `.env` existed) landed
    correctly -- and why the daemon's build record went to one vault while the
    CLI looked in the other ("UNVERIFIED build").

PROPERTY
    The operating vault root is derived from the OPERATING HOME (the process's
    own cwd), never from where the package happens to be installed; the parent
    and the child consume the SAME absolute value; and a root that lands inside
    the package tree is REFUSED at the boundary rather than written to.

WITNESS
    Remove the fence call site in `start_daemon` or in the daemon `__main__`
    block, or let the child be handed a relative vault dir again, and a named
    test here goes red.

SECTION G -- THE DOWNSTREAM CONSUMERS (phase 1b)
    Three sites carried the raw pre-fix pattern
    `os.getenv("SYSTEMU_VAULT_DIR", "systemu/vault")` and kept a RELATIVE base:
    the credential store, the memory-backend factory and the sqlite vault's
    memory-dir resolver.  They were safe only BY INHERITANCE -- the daemon
    pins an absolute `SYSTEMU_VAULT_DIR` into its child's environment, so the
    relative branch was never taken in the one process that mattered.  That is
    a property of the caller, not of the site: any process that reaches these
    sites with a relative value (a directly launched dashboard, a bare
    `python -c`, a test) re-derives the vault against its OWN cwd, which is the
    original defect wearing a different hat.  Section G pins the derivation to
    the mint at each site, so the safety is by construction rather than by
    inheritance.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import systemu
import systemu.scheduler.daemon as daemon_mod
from systemu.runtime.vault_root import (
    DEFAULT_RELATIVE_VAULT,
    VAULT_DIR_ENV,
    package_dir,
    refusal_message,
    resolve_vault_root,
)

PKG = Path(systemu.__file__).resolve().parent


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _same(a: str, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == \
           os.path.normcase(os.path.normpath(str(b)))


def _under(child, parent) -> bool:
    c = os.path.normcase(os.path.normpath(os.path.abspath(str(child))))
    p = os.path.normcase(os.path.normpath(os.path.abspath(str(parent))))
    return c == p or c.startswith(p + os.sep)


@pytest.fixture()
def bare_cwd(tmp_path, monkeypatch):
    """An EMPTY working directory -- the exact shape that mis-resolved."""
    home = tmp_path / "bare"
    home.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.delenv(VAULT_DIR_ENV, raising=False)
    return home


# ═════════════════════════════════════════════════════════════════════════════
#  A -- THE MINT: one derivation, rooted at the operating home
# ═════════════════════════════════════════════════════════════════════════════

def test_a_bare_cwd_resolves_the_vault_under_that_cwd_never_into_the_package(bare_cwd):
    """THE REPRO.  An empty cwd must not send the operating vault into the
    installed package tree."""
    v = resolve_vault_root()

    assert not _under(v.root, PKG), (
        "the operating vault resolved INSIDE the systemu package -- this is the "
        f"defect: root={v.root!r} package={str(PKG)!r}")
    assert _same(v.root, bare_cwd / DEFAULT_RELATIVE_VAULT), v.root
    assert Path(v.root).is_absolute(), v.root
    assert v.refused is False
    assert v.inside_package is False


def test_the_dotenv_shape_of_the_cwd_does_not_change_the_resolved_root(bare_cwd):
    """Resolution must be DETERMINISTIC.  The live defect flipped on the mere
    presence of a `.env`, so the same directory produced two different vaults on
    two consecutive boots."""
    before = resolve_vault_root().root
    (bare_cwd / ".env").write_text("OPENROUTER_API_KEY=x\n", encoding="utf-8")
    after = resolve_vault_root().root

    assert _same(before, after), (
        f"vault root moved when a .env appeared: {before!r} -> {after!r}")


def test_an_absolute_vault_dir_env_is_honoured_verbatim(bare_cwd, tmp_path):
    elsewhere = tmp_path / "operator_chosen"
    v = resolve_vault_root(env={VAULT_DIR_ENV: str(elsewhere)})
    assert _same(v.root, elsewhere)
    assert v.source == "env"


def test_a_relative_vault_dir_env_resolves_against_the_operating_home(bare_cwd):
    v = resolve_vault_root(env={VAULT_DIR_ENV: "data/vault"})
    assert _same(v.root, bare_cwd / "data" / "vault")
    assert _same(v.home, bare_cwd)


def test_an_explicit_root_is_absolutised_against_the_operating_home(bare_cwd):
    """This is how the parent CLI hands a value to the child: the string it
    already holds is turned into ONE absolute path."""
    v = resolve_vault_root(explicit=DEFAULT_RELATIVE_VAULT)
    assert _same(v.root, bare_cwd / DEFAULT_RELATIVE_VAULT)
    assert v.source == "explicit"


# ═════════════════════════════════════════════════════════════════════════════
#  B -- THE FENCE: a value, not a raise (DEC-32)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_root_inside_the_package_tree_is_flagged_and_refused(bare_cwd):
    v = resolve_vault_root(explicit=str(package_dir() / "vault"))

    assert v.inside_package is True
    assert v.refused is True
    assert v.reason, "a refusal with no reason tells the operator nothing"


def test_the_refusal_message_names_the_root_the_package_and_a_remedy(bare_cwd):
    v = resolve_vault_root(explicit=str(package_dir() / "vault"))
    msg = refusal_message(v)

    assert str(package_dir()) in msg
    assert v.root in msg
    assert "SYSTEMU_VAULT_DIR" in msg
    assert msg.isascii(), "operator-facing verdict text must be ASCII-only"


def test_standing_in_the_checkout_is_NOT_refused(monkeypatch):
    """The dev/source-checkout case: when the operator is standing in the tree
    that CONTAINS the package, `./systemu/vault` is the working vault by
    construction and has always been.  The fence is about falling INTO a package
    you are not standing in -- it must not outlaw the checkout."""
    checkout = PKG.parent
    monkeypatch.chdir(checkout)
    monkeypatch.delenv(VAULT_DIR_ENV, raising=False)

    v = resolve_vault_root()

    assert v.inside_package is True
    assert v.refused is False, (
        "running from the source checkout was refused -- the carve-out is gone")


def test_a_package_resident_root_is_refused_even_when_named_by_the_env(bare_cwd):
    """The env var is an INPUT to the mint, never an override of the fence."""
    v = resolve_vault_root(env={VAULT_DIR_ENV: str(package_dir() / "vault")})
    assert v.refused is True


# ═════════════════════════════════════════════════════════════════════════════
#  C -- CONFIG: everybody consumes the same mint
# ═════════════════════════════════════════════════════════════════════════════

def test_config_from_env_hands_out_an_absolute_root_under_the_cwd(bare_cwd):
    from sharing_on.config import Config

    cfg = Config.from_env()

    assert Path(cfg.vault_dir).is_absolute(), (
        f"config.vault_dir is still relative ({cfg.vault_dir!r}) -- every process "
        "that reads it re-resolves it against its OWN cwd, which is the defect")
    assert _same(cfg.vault_dir, bare_cwd / DEFAULT_RELATIVE_VAULT)
    assert not _under(cfg.vault_dir, PKG)


# ═════════════════════════════════════════════════════════════════════════════
#  D -- PARENT / CHILD AGREEMENT (the divergence itself)
# ═════════════════════════════════════════════════════════════════════════════

def _spawn(monkeypatch, vault_dir, port):
    """Drive `start_daemon` far enough to capture the child's argv/cwd/env.
    No daemon is ever booted -- Popen is replaced."""
    captured = {}

    class _P:
        pid = os.getpid()

    def _spy(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kw.get("cwd")
        captured["env"] = dict(kw.get("env") or {})
        return _P()

    monkeypatch.setattr("subprocess.Popen", _spy)
    monkeypatch.setattr(daemon_mod, "await_readiness",
                        lambda *a, **kw: daemon_mod.probe_readiness(
                            vault_dir, port=port, timeout=0.2))
    verdict = daemon_mod.start_daemon(vault_dir, SimpleNamespace(),
                                      SimpleNamespace(), port=port,
                                      wait_timeout_s=0)
    captured["verdict"] = verdict
    return captured


def test_the_spawned_child_is_handed_the_SAME_absolute_root_the_parent_resolved(
        bare_cwd, monkeypatch):
    """THE DIVERGENCE.  The parent used to pass the relative string
    `systemu/vault`; the child resolved it against a DIFFERENT cwd."""
    got = _spawn(monkeypatch, DEFAULT_RELATIVE_VAULT, _free_port())

    cmd = got["cmd"]
    handed = cmd[cmd.index("--vault-dir") + 1]

    assert Path(handed).is_absolute(), (
        f"the child was handed a RELATIVE vault dir ({handed!r}) -- it will "
        "re-resolve it against its own cwd")
    assert _same(handed, bare_cwd / DEFAULT_RELATIVE_VAULT), handed
    assert not _under(handed, PKG), handed
    assert _same(got["env"][VAULT_DIR_ENV], handed), (
        "argv and the child's SYSTEMU_VAULT_DIR disagree -- the child's own "
        "env reads would land somewhere else again")


def test_the_child_runs_in_the_operating_home_not_in_the_package_tree(
        bare_cwd, monkeypatch):
    got = _spawn(monkeypatch, DEFAULT_RELATIVE_VAULT, _free_port())

    assert _same(got["cwd"], bare_cwd), (
        f"the child was spawned in {got['cwd']!r} -- every relative path it "
        "touches lands there instead of in the operator's directory")


def test_start_daemon_REFUSES_a_package_resident_root_without_spawning(
        bare_cwd, monkeypatch):
    """The fence is on the VALUE crossing the boundary and its failure path
    cannot be produced by the success path: nothing is spawned, nothing is
    written, and the verdict says refused."""
    spawned = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **kw: spawned.append(a) or SimpleNamespace(pid=1))

    verdict = daemon_mod.start_daemon(str(package_dir() / "vault"),
                                      SimpleNamespace(), SimpleNamespace(),
                                      port=_free_port(), wait_timeout_s=0)

    assert spawned == [], "a daemon was spawned onto a package-resident vault"
    assert verdict is not None
    assert verdict.ready is False
    assert verdict.refused is True
    assert str(package_dir()) in verdict.reason


# ═════════════════════════════════════════════════════════════════════════════
#  E -- THE CHILD-SIDE BOUNDARY
# ═════════════════════════════════════════════════════════════════════════════

def test_the_child_entry_returns_the_absolute_root_on_the_success_path(bare_cwd):
    got = daemon_mod.resolve_child_vault_dir(DEFAULT_RELATIVE_VAULT)
    assert _same(got, bare_cwd / DEFAULT_RELATIVE_VAULT)


def test_the_child_entry_refuses_a_package_resident_root_with_a_nonzero_exit(
        bare_cwd, capsys):
    with pytest.raises(SystemExit) as exc:
        daemon_mod.resolve_child_vault_dir(str(package_dir() / "vault"))

    assert exc.value.code == daemon_mod.VAULT_ROOT_REFUSED_EXIT
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert str(package_dir()) in err
    assert "REFUSED" in err


def test_the_daemon_main_block_consumes_the_child_fence_before_opening_the_vault():
    """REACHABILITY PIN.  Delete the call site and this goes red."""
    src = Path(daemon_mod.__file__).read_text(encoding="utf-8")
    main_block = src.split('if __name__ == "__main__":', 1)
    assert len(main_block) == 2, "the daemon __main__ entry point moved"
    body = main_block[1]

    i_fence = body.find("resolve_child_vault_dir(")
    i_vault = body.find("Vault(")

    assert i_fence != -1, (
        "the daemon child no longer consults the vault-root fence -- a directly "
        "launched child can operate out of the package tree again")
    assert i_vault != -1
    assert i_fence < i_vault, (
        "the vault is opened BEFORE the fence runs -- the refusal path can no "
        "longer prevent the write")
    assert "Vault(_vault_root)" in body, (
        "the child opens a vault at a path the fence never judged")


# ═════════════════════════════════════════════════════════════════════════════
#  F -- THE CLI SURFACE
# ═════════════════════════════════════════════════════════════════════════════

def test_daemon_start_surfaces_the_refusal_and_exits_nonzero(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from systemu.interface.cli_commands import daemon_start

    refused = daemon_mod.DaemonReadiness(
        ready=False, pid=None, process_alive=False, host="127.0.0.1",
        port=8765, reason="REFUSED: package-resident vault root", refused=True,
    )
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)
    monkeypatch.setattr(daemon_mod, "start_daemon",
                        lambda *a, **kw: refused)

    result = CliRunner().invoke(
        daemon_start, [],
        obj={"config": SimpleNamespace(vault_dir=str(tmp_path / "vault")),
             "vault": SimpleNamespace()},
    )

    assert result.exit_code == daemon_mod.VAULT_ROOT_REFUSED_EXIT, result.output
    assert "REFUSED" in result.output, result.output


# =============================================================================
#  G -- THE DOWNSTREAM CONSUMERS: safety by construction, not by inheritance
# =============================================================================

@pytest.fixture()
def foreign_home(tmp_path, monkeypatch):
    """A process standing in a directory it did not choose, handed a RELATIVE
    `SYSTEMU_VAULT_DIR`.

    This is the shape the three phase-1b sites were only accidentally safe
    from: the daemon happens to pin an ABSOLUTE value into its child's
    environment, so the relative branch is unreachable from that one caller.
    Nothing at the site enforced it.
    """
    home = tmp_path / "foreign_home"
    home.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.setenv(VAULT_DIR_ENV, "relative_vault")
    return home


def _mint_root() -> str:
    """The one answer every site must agree with, taken from the mint itself
    rather than restated -- a site that agrees with a COPY of the rule has not
    consumed the rule."""
    return resolve_vault_root().root


# -- G1: systemu/runtime/credentials/store.py -------------------------------

def test_the_credential_store_base_is_the_mint_answer(foreign_home):
    from systemu.runtime.credentials.store import CredentialStore

    base = CredentialStore()._base

    assert Path(base).is_absolute(), (
        f"CredentialStore kept a RELATIVE base ({str(base)!r}) -- it is "
        "re-resolved against whatever cwd the reading process happens to have")
    assert _same(base, _mint_root()), f"{str(base)!r} != mint {_mint_root()!r}"
    assert not _under(base, PKG), (
        f"the credential store resolved INTO the systemu package: {str(base)!r}")


def test_the_credential_store_base_does_not_move_when_the_process_chdirs(
        foreign_home, tmp_path, monkeypatch):
    """THE DEFECT ITSELF: one string, two cwds, two directories.  A base that
    is still relative names a DIFFERENT directory the moment anything moves --
    and `.credentials.json` is a secret at rest, so the wrong answer here
    scatters credentials into whatever directory the process wandered into."""
    from systemu.runtime.credentials.store import CredentialStore

    store = CredentialStore()
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert _same(store._file.parent, foreign_home / "relative_vault"), store._file
    assert not _under(store._file, elsewhere), (
        f"the credential file followed the process to {str(elsewhere)!r} -- "
        "the base was never absolutised")


# -- G2: systemu/runtime/memory_backends/__init__.py ------------------------

def test_the_filesystem_memory_backend_root_is_the_mint_answer(
        foreign_home, monkeypatch):
    from systemu.runtime.memory_backends import get_backend

    monkeypatch.delenv("SYSTEMU_MEMORY_BACKEND", raising=False)
    root = get_backend(None)._root

    assert Path(root).is_absolute(), (
        f"the memory backend kept a RELATIVE root ({str(root)!r})")
    assert _same(root, Path(_mint_root()) / "memory"), str(root)
    assert not _under(root, PKG), (
        f"shadow memory resolved INTO the systemu package: {str(root)!r}")


def test_the_memory_backend_root_does_not_move_when_the_process_chdirs(
        foreign_home, tmp_path, monkeypatch):
    from systemu.runtime.memory_backends import get_backend

    monkeypatch.delenv("SYSTEMU_MEMORY_BACKEND", raising=False)
    backend = get_backend(None)
    elsewhere = tmp_path / "elsewhere_memory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert _same(backend._root, foreign_home / "relative_vault" / "memory")
    assert not _under(backend._root, elsewhere), (
        "the shadow memory root followed the process -- a resumed shadow reads "
        "an EMPTY buffer and silently loses its accumulated lessons")


# -- G3: systemu/storage/sqlite/vault.py ------------------------------------

def test_the_sqlite_postgres_memory_dir_is_the_mint_answer(foreign_home):
    from systemu.storage.sqlite.vault import _resolve_memory_dir

    got = _resolve_memory_dir("postgresql://u:p@h:5432/db", None)

    assert got.is_absolute(), f"a RELATIVE memory dir ({str(got)!r})"
    assert _same(got, Path(_mint_root()) / "memory"), str(got)
    assert not _under(got, PKG), str(got)


def test_the_sqlite_memory_dir_does_not_move_when_the_process_chdirs(
        foreign_home, tmp_path, monkeypatch):
    from systemu.storage.sqlite.vault import _resolve_memory_dir

    got = _resolve_memory_dir("postgres://u:p@h:5432/db", None)
    elsewhere = tmp_path / "elsewhere_sqlite"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert _same(got, foreign_home / "relative_vault" / "memory"), str(got)
    assert not _under(got, elsewhere), str(got)


# -- G4: THE CONTAINER PIN -- the docker default must not move --------------

def test_the_docker_default_memory_dir_is_byte_identical_when_the_env_is_absent(
        tmp_path, monkeypatch):
    """CONTAINER BEHAVIOUR PIN.

    In the docker images the vault is bind-mounted at `/data/vault` and
    `SYSTEMU_VAULT_DIR` is NOT set; a `postgresql://` URL is how this site
    detects that shape (see `_resolve_memory_dir`'s docstring, resolution rule
    3, and `captures/E2E_VERDICT_DOCKER.md` finding D).  Routing the ENV
    through the mint changes the env-PRESENT answer only: with the env absent
    the answer must stay exactly `/data/vault/memory`, and in particular must
    NOT become the mint's cwd-relative `<home>/systemu/vault` default -- that
    would put container memory back on the volatile writable layer that
    v0.6.6-d fixed.
    """
    from systemu.storage.sqlite.vault import _resolve_memory_dir

    monkeypatch.chdir(tmp_path)                       # cwd must not leak in
    monkeypatch.delenv(VAULT_DIR_ENV, raising=False)

    got = _resolve_memory_dir("postgresql://u:p@h:5432/db", None)

    assert str(got) == str(Path("/data/vault") / "memory"), str(got)
    assert not _same(got, Path(tmp_path) / DEFAULT_RELATIVE_VAULT / "memory"), (
        "the container default was replaced by the mint's cwd-derived default "
        "-- memory moves to the container's volatile layer, lost on rebuild")
