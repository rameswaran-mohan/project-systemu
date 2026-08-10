"""PACKET G / F13 — the CLI and the daemon can silently run DIFFERENT code.

THE LIVE DEFECT
    ``start_daemon`` builds the child environment and SETS ``PYTHONPATH`` to the
    resolved project root, DISCARDING whatever the parent had. In a real session
    the CLI ran systemu 0.10.22 out of a worktree while the daemon it spawned
    loaded systemu 0.10.21 out of site-packages (a stale editable install
    pointing at a different worktree). Every surface said "Ready". The daemon
    served stale code for twenty minutes and two defects were wrongly concluded
    to be unfixed. For an end user the same skew arrives via a half-finished
    upgrade, a stale editable install, a venv/system-python mix, or two systemu
    installs on one box — with NO signal anywhere.

PROPERTY
    The operator can never be unaware that the running daemon is executing a
    different systemu build than the CLI addressing it. EVERY surface that
    reports the daemon as running also establishes WHICH BUILD it is running,
    and a mismatch is reported.

    Surfaces that disclose "the daemon is up" today — all four move together
    (GATE-7a corollary: never widen one gate of a multi-surface fact):
      * ``sharing_on daemon start``   — cli_commands.daemon_start
      * ``sharing_on daemon status``  — cli_commands.daemon_status
      * ``sharing_on doctor``         — cli_commands._render_doctor_report
      * the dashboard /health page    — interface.pages.health

FENCE
    The build fact rides the SAME mint as readiness (DEC-43 — one mint per
    operator-facing fact, no parallel derivation): ``probe_readiness`` returns
    it on ``DaemonReadiness``, and every surface CONSUMES that value.

    Two structural sub-fences:
      * only the process that LOADED the code may record which code it loaded —
        the parent may never write a ``build=`` into the runtime sidecar; and
      * ``build_match`` is TRI-STATE. A daemon that recorded no build is
        UNVERIFIED, never "agrees" (fail-closed: DEC-27 — completeness must be
        witnessed, never inferred).

    A mismatch is reported LOUDLY but is never fatal: a user mid-upgrade must
    still be able to run ``daemon stop``.

WITNESS
    Delete any one of the four disclosures, or let the parent stamp its own
    version onto the child's record, or let "unrecorded" collapse into "agrees",
    and a named test here goes red.
"""
from __future__ import annotations

import ast
import inspect
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def vault_dir(tmp_path) -> str:
    v = tmp_path / "home" / "vault"
    v.mkdir(parents=True)
    return str(v)


def _write_pidfile(vault_dir: str, pid: int) -> None:
    daemon_mod._pid_file_path(vault_dir).write_text(str(pid))


def _up(vault_dir: str, port: int, *, build) -> None:
    """Make this test process stand in for a tracked, live daemon."""
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port,
                                    build=build)


# ═════════════════════════════════════════════════════════════════════════════
#  G.a — the MINT carries the build fact (DEC-43: no second derivation)
# ═════════════════════════════════════════════════════════════════════════════

def test_the_mint_reports_agreement_when_the_daemon_recorded_this_very_build(vault_dir):
    port = _free_port()
    _up(vault_dir, port, build=daemon_mod._own_build())

    r = daemon_mod.probe_readiness(vault_dir, timeout=0.3)

    assert r.build_match is True, r
    assert r.daemon_version == daemon_mod._own_build()["version"]
    assert r.cli_version == daemon_mod._own_build()["version"]
    assert r.build_note, "agreement is still stated, not silent"


def test_the_mint_reports_a_MISMATCH_when_the_daemon_loaded_another_version(vault_dir):
    """The exact live incident: CLI 0.10.22, daemon 0.10.21 from site-packages."""
    port = _free_port()
    _up(vault_dir, port, build={"version": "0.10.21",
                                "path": r"C:\python\Lib\site-packages\systemu"})

    r = daemon_mod.probe_readiness(vault_dir, timeout=0.3)

    assert r.build_match is False, r
    assert r.daemon_version == "0.10.21"
    assert r.cli_version != "0.10.21"
    low = r.build_note.lower()
    assert "0.10.21" in r.build_note and r.cli_version in r.build_note, r.build_note
    assert "site-packages" in r.build_note, "the note must name WHERE, not just what"
    assert "skew" in low, r.build_note


def test_the_mint_reports_a_MISMATCH_when_only_the_import_PATH_differs(vault_dir):
    """Two worktrees at the same version are still different code. The incident
    was 'an old editable install pointing at a different worktree entirely'."""
    port = _free_port()
    mine = daemon_mod._own_build()
    _up(vault_dir, port, build={"version": mine["version"],
                                "path": r"D:\some\other\worktree\systemu"})

    r = daemon_mod.probe_readiness(vault_dir, timeout=0.3)

    assert r.build_match is False, r
    assert "other" in r.build_note.replace("\\", "/"), r.build_note


def test_a_daemon_that_recorded_NO_build_is_UNVERIFIED_and_never_agrees(vault_dir):
    """FAIL-CLOSED (DEC-27). An old daemon predates this record — which is
    ITSELF the skew case. Absence of a mismatch is not agreement."""
    port = _free_port()
    _up(vault_dir, port, build=None)          # exactly what an old daemon leaves

    r = daemon_mod.probe_readiness(vault_dir, timeout=0.3)

    assert r.build_match is None, r
    assert r.daemon_version is None
    assert r.build_note, "an unverifiable build must still be DISCLOSED"
    low = r.build_note.lower()
    assert "did not record" in low or "unverified" in low, r.build_note
    # the wording may not collide with the readiness vocabulary the other
    # surfaces assert on ("ready" / "running")
    assert "ready" not in low and "running" not in low, r.build_note


def test_no_daemon_at_all_makes_no_build_claim(vault_dir):
    r = daemon_mod.probe_readiness(vault_dir, port=_free_port(), timeout=0.3)
    assert r.build_match is None, r
    assert r.build_note == "", "silence when there is nothing to disclose"


def test_a_corrupt_build_record_is_UNVERIFIED_not_a_crash_and_not_agreement(vault_dir):
    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._runtime_file_path(vault_dir).write_text(
        '{"pid": 1, "port": %d, "build": "0.10.21"}' % port, encoding="utf-8")

    r = daemon_mod.probe_readiness(vault_dir, timeout=0.3)

    assert r.build_match is None, r
    assert r.daemon_version is None


def test_get_status_projects_the_build_fact(vault_dir):
    port = _free_port()
    _up(vault_dir, port, build={"version": "9.9.9", "path": "/nowhere/systemu"})

    st = daemon_mod.get_status(vault_dir)

    assert st["build_match"] is False, st
    assert st["daemon_version"] == "9.9.9"
    assert st["cli_version"] == daemon_mod._own_build()["version"]
    assert st["build_note"]


# ═════════════════════════════════════════════════════════════════════════════
#  G.b — ONLY the process that loaded the code may record which code it loaded
# ═════════════════════════════════════════════════════════════════════════════

def test_the_parent_never_stamps_its_own_build_onto_the_childs_record():
    """The parent CLI knows its OWN build; it knows nothing about the child's.
    A parent-written build would manufacture agreement for the very skew this
    packet exists to report."""
    tree = ast.parse(Path(daemon_mod.__file__).read_text(encoding="utf-8"))
    offenders = []
    forwarders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if type(name) is not str or not name.startswith("_write_runtime_state"):
                continue
            for kw in node.keywords:
                if kw.arg != "build":
                    continue
                if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                    continue        # explicitly recording NO build is fine
                if (isinstance(kw.value, ast.Name) and kw.value.id == "build"
                        and any(a.arg == "build"
                                for a in (fn.args.kwonlyargs + fn.args.args))):
                    forwarders.append(fn.name)
                    continue        # a pure pass-through wrapper decides nothing
                offenders.append(fn.name)
    assert offenders == ["_run_daemon_loop"], (
        "the systemu build may be recorded ONLY by the daemon process that "
        f"actually imported it (_run_daemon_loop); found writers: {offenders}")
    assert forwarders == ["_write_runtime_state"], (
        "unexpected pass-through writer(s) of the build record: "
        f"{forwarders}")


def test_the_daemon_loop_records_the_build_it_actually_imported(tmp_path):
    """The child's sidecar write carries _own_build() — the version AND the
    resolved package directory of the systemu THAT PROCESS imported."""
    loop_src = inspect.getsource(daemon_mod._run_daemon_loop)
    assert "_own_build()" in loop_src, (
        "the daemon loop no longer records the build it loaded — every surface "
        "downstream silently degrades to UNVERIFIED")

    path = tmp_path / "state.json"
    daemon_mod._write_runtime_state_at(path, pid=4242, port=1234,
                                       build=daemon_mod._own_build())
    import json
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["build"]["version"] == daemon_mod._own_build()["version"]
    assert Path(rec["build"]["path"]).name == "systemu"
    assert (Path(rec["build"]["path"]) / "__init__.py").exists(), (
        "the recorded path must be the REAL package dir the process imported")


def test_start_daemon_clears_a_stale_build_record_before_spawning(monkeypatch, vault_dir):
    """A previous daemon's build must never be attributed to the new one."""
    port = _free_port()
    daemon_mod._write_runtime_state(vault_dir, pid=999999, port=port,
                                    build={"version": "0.0.1", "path": "/old"})
    order = []

    class _P:
        pid = os.getpid()

    def _spy_popen(*a, **kw):
        order.append(daemon_mod._read_runtime_state(vault_dir).get("build"))
        return _P()

    monkeypatch.setattr("subprocess.Popen", _spy_popen)
    monkeypatch.setattr(daemon_mod, "await_readiness",
                        lambda *a, **kw: daemon_mod.probe_readiness(vault_dir, port=port,
                                                                    timeout=0.2))
    daemon_mod.start_daemon(vault_dir, SimpleNamespace(), SimpleNamespace(),
                            port=port, wait_timeout_s=0)

    assert order == [None], (
        "the stale build record survived into the new daemon's lifetime: "
        f"{order}")


# ═════════════════════════════════════════════════════════════════════════════
#  G.c — THE MULTI-SURFACE FENCE: all four disclosures move together
# ═════════════════════════════════════════════════════════════════════════════

def _skew_mint(*, port: int, match, dv="0.10.21", dp=r"C:\py\site-packages\systemu"):
    note = {
        False: (f"BUILD SKEW: the daemon is executing systemu {dv} from {dp}, "
                f"but this CLI is systemu 9.9.9 from D:\\wt\\systemu."),
        None: "the daemon process did not record which systemu build it loaded.",
        True: "daemon and CLI are executing the same systemu build 9.9.9.",
    }[match]
    return daemon_mod.DaemonReadiness(
        ready=True, pid=4242, process_alive=True, host="127.0.0.1", port=port,
        reason=f"accepting connections on http://127.0.0.1:{port}",
        daemon_version=(dv if match is not None else None),
        daemon_path=(dp if match is not None else None),
        cli_version="9.9.9", cli_path=r"D:\wt\systemu",
        build_match=match, build_note=note,
    )


@pytest.fixture()
def _no_spawn(monkeypatch):
    class _P:
        pid = os.getpid()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _P())
    monkeypatch.setenv("SYSTEMU_DAEMON_START_TIMEOUT", "0")
    import sharing_on.setup_flow as _sf
    monkeypatch.setattr(_sf, "key_present", lambda: True)


def test_daemon_start_reports_the_skew_it_just_created(monkeypatch, vault_dir, _no_spawn):
    port = _free_port()
    monkeypatch.setattr(daemon_mod, "probe_readiness",
                        lambda vd, **kw: _skew_mint(port=port, match=False))
    from systemu.interface.cli_commands import daemon_start

    res = CliRunner().invoke(
        daemon_start, ["--port", str(port)],
        obj={"config": SimpleNamespace(vault_dir=vault_dir), "vault": SimpleNamespace()})

    out = res.output
    assert "0.10.21" in out and "9.9.9" in out, out
    assert "skew" in out.lower(), out
    assert res.exit_code == 0, (
        "a build mismatch is REPORTED, never fatal — a user mid-upgrade must "
        "still be able to run `daemon stop`\n" + out)


def test_daemon_status_reports_the_skew(monkeypatch, vault_dir):
    port = _free_port()
    monkeypatch.setattr(daemon_mod, "probe_readiness",
                        lambda vd, **kw: _skew_mint(port=port, match=False))
    from systemu.interface.cli_commands import daemon_status

    res = CliRunner().invoke(
        daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_dir), "vault": SimpleNamespace()})

    out = res.output
    assert "0.10.21" in out and "9.9.9" in out, out
    assert "skew" in out.lower(), out
    assert res.exit_code == 0, out


def test_daemon_status_states_the_build_even_when_the_two_AGREE(monkeypatch, vault_dir):
    """"Ready" alone is exactly the pre-fix surface. WHICH build is part of the
    report whether or not there is a problem."""
    port = _free_port()
    monkeypatch.setattr(daemon_mod, "probe_readiness",
                        lambda vd, **kw: _skew_mint(port=port, match=True))
    from systemu.interface.cli_commands import daemon_status

    out = CliRunner().invoke(
        daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_dir),
             "vault": SimpleNamespace()}).output
    assert "9.9.9" in out, out


def test_daemon_status_discloses_an_UNVERIFIED_build(monkeypatch, vault_dir):
    port = _free_port()
    monkeypatch.setattr(daemon_mod, "probe_readiness",
                        lambda vd, **kw: _skew_mint(port=port, match=None))
    from systemu.interface.cli_commands import daemon_status

    out = CliRunner().invoke(
        daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_dir),
             "vault": SimpleNamespace()}).output
    assert "did not record" in out.lower(), out


def test_doctor_report_and_render_carry_the_daemon_build(vault_dir):
    from systemu.runtime import platform_profile as pp

    st = {"running": True, "ready": True, "pid": 4242, "process_alive": True,
          "host": "127.0.0.1", "port": 8765, "url": "http://127.0.0.1:8765",
          "reason": "accepting connections",
          "daemon_version": "0.10.21", "daemon_path": r"C:\py\site-packages\systemu",
          "cli_version": "9.9.9", "cli_path": r"D:\wt\systemu",
          "build_match": False, "build_note": "BUILD SKEW: 0.10.21 vs 9.9.9"}

    rep = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                                 keyring_locked=False, daemon_state=st)

    assert rep["daemon"]["build"]["match"] is False, rep["daemon"]
    assert rep["daemon"]["build"]["daemon_version"] == "0.10.21"
    assert any(p["id"] == "daemon_build_skew" for p in rep["problems"]), rep["problems"]
    assert rep["ok"] is True, "build skew is a WARNING — doctor must not block on it"

    from systemu.interface.cli_commands import _render_doctor_report
    import systemu.interface.cli_commands as cc
    with cc.console.capture() as cap:
        _render_doctor_report(rep)
    blob = cap.get()
    assert "0.10.21" in blob, blob
    assert "9.9.9" in blob, blob


def test_doctor_disambiguates_whose_systemu_version_it_is_printing(vault_dir):
    """GATE-7a: the pre-fix report printed "Daemon: running" next to a bare
    "systemu version" row that was the CLI's. That row now says whose it is —
    otherwise the partial fix manufactures the contradiction."""
    from systemu.runtime import platform_profile as pp
    from systemu.interface.cli_commands import _render_doctor_report
    import systemu.interface.cli_commands as cc

    rep = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                                 keyring_locked=False, daemon_running=True)
    with cc.console.capture() as cap:
        _render_doctor_report(rep)
    blob = cap.get().lower()
    assert "systemu version" in blob
    assert "cli" in blob, (
        "a bare 'systemu version' row beside 'Daemon: running' reads as the "
        "DAEMON's version:\n" + blob)


def test_doctor_flags_an_UNVERIFIED_daemon_build_as_a_warning(vault_dir):
    from systemu.runtime import platform_profile as pp
    st = {"running": True, "ready": True, "pid": 1, "process_alive": True,
          "host": "127.0.0.1", "port": 8765, "url": "u", "reason": "r",
          "daemon_version": None, "daemon_path": None,
          "cli_version": "9.9.9", "cli_path": "p",
          "build_match": None,
          "build_note": "the daemon process did not record its systemu build."}
    rep = pp.build_doctor_report(provider_configured=True, provider_reachable=True,
                                 keyring_locked=False, daemon_state=st)
    assert any(p["id"] == "daemon_build_unverified" for p in rep["problems"]), rep["problems"]
    assert rep["ok"] is True


def test_health_view_carries_the_daemon_build():
    from systemu.interface.pages import health
    st = {"running": True, "ready": True, "pid": 1, "process_alive": True,
          "host": "127.0.0.1", "port": 8765, "url": "u", "reason": "r",
          "daemon_version": "0.10.21", "daemon_path": "p1",
          "cli_version": "9.9.9", "cli_path": "p2",
          "build_match": False, "build_note": "BUILD SKEW"}
    v = health.health_view(provider_configured=True, provider_reachable=True,
                           keyring_locked=False, daemon_state=st,
                           load={"load_state": "unknown"})
    assert v["daemon"]["build"]["match"] is False, v["daemon"]
    assert v["status_chip"] == "warn"


class _Node:
    """Chainable stand-in for a nicegui element (see test_rux2_health_load_chip)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def classes(self, *a, **k):
        return self

    def style(self, *a, **k):
        return self

    def props(self, *a, **k):
        return self

    def tooltip(self, *a, **k):
        return self


class _RecordingUI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node()
        return _call

    def texts(self):
        return [str(a[0]) for _n, a, _k in self.calls if a]


def test_the_health_PAGE_actually_renders_the_daemon_build_row():
    """REACHABILITY PIN — executed, not merely parsed.

    Deleting the render leaves ``health_view()`` green and the operator blind:
    the dominant failure mode in this project is code that lands with no wiring.
    So EXECUTE the real ``build_health_page`` against a recording ui stub and
    require the skew text on the page.
    """
    import sys
    import types
    from unittest import mock
    from systemu.interface.pages import health

    st = {"running": True, "ready": True, "pid": 1, "process_alive": True,
          "host": "127.0.0.1", "port": 8765, "url": "u", "reason": "r",
          "daemon_version": "0.10.21", "daemon_path": r"C:\py\site-packages\systemu",
          "cli_version": "9.9.9", "cli_path": r"D:\wt\systemu",
          "build_match": False, "build_note": "BUILD SKEW: 0.10.21 vs 9.9.9"}

    ui = _RecordingUI()
    fake = types.ModuleType("nicegui")
    fake.ui = ui

    real_view = health.health_view          # bind BEFORE patching

    def _view(**kw):
        return real_view(
            load={"load_state": "unknown"}, provider_configured=True,
            provider_reachable=True, keyring_locked=False, daemon_state=st)

    with mock.patch.dict(sys.modules, {"nicegui": fake}), \
            mock.patch.object(health, "health_view", _view):
        health.build_health_page()

    blob = " | ".join(ui.texts())
    assert "Daemon build" in blob, blob
    assert "0.10.21" in blob, (
        "the /health page reports the daemon as running without ever saying "
        "WHICH build it is running:\n" + blob)
    assert "site-packages" in blob, blob
    assert "this process" in blob, (
        "a bare 'systemu version' row beside 'Daemon: running' reads as the "
        "daemon's version:\n" + blob)


def test_every_cli_surface_reads_the_build_off_the_minted_value():
    """Reachability pin for the two CLI surfaces + the doctor renderer."""
    import systemu.interface.cli_commands as cc
    for fn, needle in (
        (cc.daemon_start, "build"),
        (cc.daemon_status, "build"),
        (cc._render_doctor_report, "build"),
    ):
        src = inspect.getsource(fn.callback if hasattr(fn, "callback") else fn)
        assert needle in src, (
            f"{getattr(fn, 'name', fn)} no longer discloses which build the "
            "daemon is executing")


def test_a_build_mismatch_never_blocks_daemon_stop(monkeypatch, vault_dir):
    """A user mid-upgrade must still be able to shut the wrong daemon down."""
    port = _free_port()
    _up(vault_dir, port, build={"version": "0.0.1", "path": "/old/systemu"})
    assert daemon_mod.probe_readiness(vault_dir, timeout=0.3).build_match is False

    monkeypatch.setattr(daemon_mod, "_process_alive", lambda pid: False)
    # stop_daemon must not consult the build fact at all
    assert "build" not in inspect.getsource(daemon_mod.stop_daemon)


# ═════════════════════════════════════════════════════════════════════════════
#  G.d — the SECONDARY root cause: the child's PYTHONPATH was CLOBBERED
# ═════════════════════════════════════════════════════════════════════════════

def _spawn_env(monkeypatch, vault_dir, port) -> dict:
    captured = {}

    class _P:
        pid = os.getpid()

    def _spy(*a, **kw):
        captured.update(kw.get("env") or {})
        return _P()

    monkeypatch.setattr("subprocess.Popen", _spy)
    monkeypatch.setattr(daemon_mod, "await_readiness",
                        lambda *a, **kw: daemon_mod.probe_readiness(vault_dir,
                                                                    port=port,
                                                                    timeout=0.2))
    daemon_mod.start_daemon(vault_dir, SimpleNamespace(), SimpleNamespace(),
                            port=port, wait_timeout_s=0)
    return captured


def test_the_childs_pythonpath_preserves_the_parents_and_prepends_the_root(
        monkeypatch, vault_dir, tmp_path):
    """THE ROOT CAUSE. The parent's PYTHONPATH is how the operator said "run THIS
    checkout". Discarding it is how the daemon came to load a different systemu
    than the CLI that spawned it."""
    inherited = str(tmp_path / "operators_checkout")
    monkeypatch.setenv("PYTHONPATH", inherited)

    env = _spawn_env(monkeypatch, vault_dir, _free_port())

    parts = env["PYTHONPATH"].split(os.pathsep)
    assert inherited in parts, (
        "the parent's PYTHONPATH was DISCARDED — the daemon can no longer see "
        f"the code the CLI was told to run: {env['PYTHONPATH']!r}")
    assert parts[0] != inherited, "the project root must still win"


def test_the_packaged_install_case_is_byte_for_byte_unchanged(monkeypatch, vault_dir):
    """PYTHONPATH unset is the COMMON path (pip install) and must not change."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    import systemu
    expected = str(Path(systemu.__file__).parent.parent.absolute())

    env = _spawn_env(monkeypatch, vault_dir, _free_port())

    assert env["PYTHONPATH"] == expected, env["PYTHONPATH"]


def test_the_project_root_is_never_duplicated_in_the_childs_pythonpath(
        monkeypatch, vault_dir):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    import systemu
    root = str(Path(systemu.__file__).parent.parent.absolute())
    monkeypatch.setenv("PYTHONPATH", root + os.pathsep + root)

    env = _spawn_env(monkeypatch, vault_dir, _free_port())

    parts = [p for p in env["PYTHONPATH"].split(os.pathsep) if p]
    assert len(parts) == len(set(os.path.normcase(p) for p in parts)), parts
