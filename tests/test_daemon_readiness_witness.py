"""PACKET A — daemon lifecycle: readiness witness (F1) + fresh-vault advice (F5).

PROPERTY (F1)
    No operator-facing surface reports the daemon as ready/running unless an
    actual connection to its port has been OBSERVED to succeed, and every such
    surface derives that verdict from the SINGLE shared readiness mint
    ``systemu.scheduler.daemon.probe_readiness``.

    Surfaces that disclose the fact today:
      * ``sharing_on daemon start``   (the success claim + exit code)
      * ``sharing_on daemon status``  (the rendered verdict)
      * ``sharing_on doctor``         (platform_profile._probe_daemon_running)

    DEC-41: an exit code is the tool CLAIM, not the effect WITNESS.
    DEC-43: one MINT per operator-facing verdict fact; every surface consumes
            the minted value — never a PROXY. A pidfile is a proxy. So is
            process liveness.
    GATE-7a corollary: never widen ONE gate of a multi-gate fact.

PROPERTY (F5)
    The operator is never instructed to fix a condition the system is fixing in
    the same startup: the "run `sharing_on init`" advice is derived from the
    vault state AFTER the seed migrator has had its chance to run.
"""
from __future__ import annotations

import ast
import inspect
import os
import socket
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """A port number that is (as of this instant) NOT accepting connections."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def vault_dir(tmp_path) -> str:
    """A vault dir whose PARENT holds the daemon pid/runtime files."""
    v = tmp_path / "home" / "vault"
    v.mkdir(parents=True)
    return str(v)


def _write_pidfile(vault_dir: str, pid: int) -> None:
    daemon_mod._pid_file_path(vault_dir).write_text(str(pid))


# ═════════════════════════════════════════════════════════════════════════════
#  F1.a — the MINT itself: a live process is NOT a listening socket
# ═════════════════════════════════════════════════════════════════════════════

def test_mint_is_not_ready_when_process_is_alive_but_port_is_closed(vault_dir):
    """The exact live defect: pidfile + live PID, nothing accepting yet."""
    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())          # certainly alive
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port)

    r = daemon_mod.probe_readiness(vault_dir, timeout=0.4)

    assert r.ready is False, r
    assert r.process_alive is True, "the proxy is still reported as CONTEXT"
    assert r.port == port
    assert r.reason, "a not-ready verdict must carry an honest reason"


def test_mint_is_ready_only_once_a_connection_actually_succeeds(vault_dir):
    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port)

    assert daemon_mod.probe_readiness(vault_dir, timeout=0.4).ready is False

    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        r = daemon_mod.probe_readiness(vault_dir, timeout=1.0)
        assert r.ready is True, r
        assert r.pid == os.getpid()
    finally:
        srv.close()

    # ...and the verdict flips back the moment the socket goes away.
    assert daemon_mod.probe_readiness(vault_dir, timeout=0.4).ready is False


def test_await_readiness_is_bounded_and_reports_the_timeout_honestly(vault_dir):
    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port)

    import time as _time
    t0 = _time.monotonic()
    r = daemon_mod.await_readiness(vault_dir, port=port, timeout_s=0.5,
                                   poll_s=0.05, probe_timeout=0.2)
    elapsed = _time.monotonic() - t0

    assert r.ready is False
    assert elapsed < 15.0, "the wait must be bounded, not open-ended"
    assert "timed out" in r.reason.lower() or "not accepting" in r.reason.lower()


# ═════════════════════════════════════════════════════════════════════════════
#  F1.b — get_status (the `daemon status` verdict) consumes the mint
# ═════════════════════════════════════════════════════════════════════════════

def test_get_status_running_is_false_for_a_live_process_that_is_not_listening(vault_dir):
    """THE regression: `daemon status` said "Running (PID n)" for 12 seconds
    while an independent TCP connect was refused."""
    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port)

    st = daemon_mod.get_status(vault_dir)

    assert st["running"] is False, st
    assert st["ready"] is False
    assert st["process_alive"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  F1.c — THE MULTI-GATE FENCE (GATE-7a): every surface consumes ONE mint
# ═════════════════════════════════════════════════════════════════════════════

def _mint(*, ready: bool, port: int, pid: int | None = 4242):
    return daemon_mod.DaemonReadiness(
        ready=ready,
        pid=pid,
        process_alive=True,
        host="127.0.0.1",
        port=port,
        reason=("accepting connections" if ready else "nothing is accepting connections"),
    )


@pytest.mark.parametrize("ready", [True, False])
def test_every_operator_surface_consumes_the_single_readiness_mint(
        monkeypatch, tmp_path, vault_dir, ready):
    """Patch the ONE mint; all three disclosing surfaces must move with it.

    A surface that re-derives the verdict from a proxy (pidfile, process
    liveness, exit code of the spawn) would ignore the patched mint and this
    test goes red.
    """
    port = _free_port()
    calls = {"n": 0}

    def _fake_probe(vd, *, port=None, host=None, timeout=1.0):
        calls["n"] += 1
        return _mint(ready=ready, port=port or 9999)

    monkeypatch.setattr(daemon_mod, "probe_readiness", _fake_probe)
    # keep the bounded wait from actually polling for a minute
    monkeypatch.setenv("SYSTEMU_DAEMON_START_TIMEOUT", "0")

    # never spawn a real daemon in a unit test
    class _P:
        pid = os.getpid()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _P())

    from systemu.interface.cli_commands import daemon_start, daemon_status
    import sharing_on.setup_flow as _sf
    monkeypatch.setattr(_sf, "key_present", lambda: True)

    cfg = SimpleNamespace(vault_dir=vault_dir)
    obj = {"config": cfg, "vault": SimpleNamespace()}

    # ---- surface 1: daemon start (claim + exit code) ------------------------
    res_start = CliRunner().invoke(daemon_start, ["--port", str(port)], obj=dict(obj))
    if ready:
        assert res_start.exit_code == 0, res_start.output
        assert "ready" in res_start.output.lower()
    else:
        assert res_start.exit_code != 0, (
            "daemon start claimed success without a readiness witness:\n"
            + res_start.output)
        assert "did not become ready" in res_start.output.lower()

    # ---- surface 2: daemon status ------------------------------------------
    res_status = CliRunner().invoke(daemon_status, ["--port", str(port)], obj=dict(obj))
    low = res_status.output.lower()
    if ready:
        assert "ready" in low or "running" in low, res_status.output
    else:
        assert "running" not in low.replace("not running", ""), res_status.output

    # ---- surface 3: doctor ---------------------------------------------------
    from systemu.runtime import platform_profile as pp
    assert pp._probe_daemon_running(vault_dir) is ready

    assert calls["n"] > 0, "no surface consulted the mint at all"


# ═════════════════════════════════════════════════════════════════════════════
#  F1.d — the SAME property with the mint NOT patched.
#
#  The parametrized test above proves every surface consumes ONE mint, but it
#  proves it by patching that mint — so it cannot catch a mint that is itself
#  wrong. These drive the real functions against a real socket: the only fake
#  is the pidfile (this test process stands in for the daemon).
# ═════════════════════════════════════════════════════════════════════════════

def test_daemon_status_cli_tracks_a_real_socket_with_nothing_patched(vault_dir):
    """`daemon status` printed "Running (PID 22424)" while an independent TCP
    connect to the same port was refused. Drive the real CLI over a real port."""
    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port)

    from systemu.interface.cli_commands import daemon_status
    obj = {"config": SimpleNamespace(vault_dir=vault_dir), "vault": SimpleNamespace()}

    # nothing is listening — a live PID must NOT read as running
    closed = CliRunner().invoke(daemon_status, [], obj=dict(obj)).output.lower()
    assert "running" not in closed.replace("not running", ""), closed
    assert "ready" not in closed, closed

    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        opened = CliRunner().invoke(daemon_status, [], obj=dict(obj)).output.lower()
    finally:
        srv.close()
    assert "ready" in opened, opened

    # and it flips straight back when the socket goes away
    again = CliRunner().invoke(daemon_status, [], obj=dict(obj)).output.lower()
    assert "ready" not in again, again


def test_doctor_daemon_probe_tracks_a_real_socket_with_nothing_patched(vault_dir):
    """The doctor / dashboard-health surface, same port, no patching."""
    from systemu.runtime import platform_profile as pp

    port = _free_port()
    _write_pidfile(vault_dir, os.getpid())
    daemon_mod._write_runtime_state(vault_dir, pid=os.getpid(), port=port)

    assert pp._probe_daemon_running(vault_dir) is False

    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        assert pp._probe_daemon_running(vault_dir) is True
    finally:
        srv.close()

    assert pp._probe_daemon_running(vault_dir) is False


def test_a_foreign_listener_on_the_port_is_not_disclosed_as_the_daemon(vault_dir):
    """No pidfile: something else owns the port. Reporting that as "the daemon"
    would be a NEW over-claim, so the witness is necessary but not sufficient."""
    port = _free_port()
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        r = daemon_mod.probe_readiness(vault_dir, port=port, timeout=1.0)
    finally:
        srv.close()

    assert r.ready is False, r
    assert r.process_alive is False
    assert "no systemu daemon is tracked" in r.reason, r.reason


def test_daemon_start_aborting_on_a_missing_key_does_not_exit_zero(monkeypatch, vault_dir):
    """DEC-41: exit 0 is a success CLAIM. An aborted start is not a success."""
    import sharing_on.setup_flow as _sf
    monkeypatch.setattr(_sf, "key_present", lambda: False)
    monkeypatch.setattr(daemon_mod, "start_daemon",
                        lambda *a, **kw: pytest.fail("must not spawn without a key"))
    from systemu.interface.cli_commands import daemon_start

    res = CliRunner().invoke(
        daemon_start, ["--port", str(_free_port())],
        obj={"config": SimpleNamespace(vault_dir=vault_dir), "vault": SimpleNamespace()},
    )
    assert res.exit_code != 0, res.output


def test_daemon_start_never_claims_readiness_from_a_port_it_did_not_bind(
        vault_dir, monkeypatch):
    """Caught by the LIVE run of this very fix.

    A stale daemon was holding the port. `daemon start` spawned a child, probed,
    and the connection succeeded — against the OTHER server — while the child it
    had just spawned was still alive and one instant from dying on its own
    port-in-use check. It printed "Daemon ready." and exited 0 for a daemon that
    never ran. The witness is real, but it must witness OUR daemon.
    """
    port = _free_port()
    spawned = {"n": 0}

    class _P:
        pid = os.getpid()

    def _spy_popen(*a, **kw):
        spawned["n"] += 1
        return _P()

    monkeypatch.setattr("subprocess.Popen", _spy_popen)

    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    try:
        v = daemon_mod.start_daemon(vault_dir, SimpleNamespace(), SimpleNamespace(),
                                    port=port, wait_timeout_s=0)
    finally:
        srv.close()

    assert v is not None, "start must return a verdict the CLI can gate on"
    assert v.ready is False, v
    assert spawned["n"] == 0, "must not spawn a daemon that cannot bind the port"
    assert "already in use" in v.reason.lower(), v.reason


def test_only_the_mint_opens_the_witnessing_connection(vault_dir):
    """DEC-43 structural fence: ONE function opens the witnessing socket, and
    the projection consumed by the CLI/doctor re-derives nothing from a proxy."""
    tree = ast.parse(Path(daemon_mod.__file__).read_text(encoding="utf-8"))
    sites = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_connection_succeeds"):
                sites.append(fn.name)
    assert sites == ["probe_readiness"], (
        "the readiness witness must be opened in exactly one place; "
        f"found call sites: {sites}")

    gs = inspect.getsource(daemon_mod.get_status)
    assert "probe_readiness(" in gs, "get_status must CONSUME the mint"
    for proxy in ("_pidfile_process", "_process_alive", "pid_file"):
        assert proxy not in gs, (
            f"get_status re-derives the operator verdict from the {proxy!r} "
            "proxy instead of consuming the mint (DEC-43)")


def test_daemon_start_does_not_print_the_success_line_before_the_witness(
        monkeypatch, vault_dir):
    """DEC-41: the success CLAIM may not precede the effect WITNESS."""
    port = _free_port()
    monkeypatch.setenv("SYSTEMU_DAEMON_START_TIMEOUT", "0")
    monkeypatch.setattr(daemon_mod, "probe_readiness",
                        lambda vd, **kw: _mint(ready=False, port=port))

    class _P:
        pid = os.getpid()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _P())

    import sharing_on.setup_flow as _sf
    monkeypatch.setattr(_sf, "key_present", lambda: True)
    from systemu.interface.cli_commands import daemon_start

    res = CliRunner().invoke(
        daemon_start, ["--port", str(port)],
        obj={"config": SimpleNamespace(vault_dir=vault_dir), "vault": SimpleNamespace()},
    )
    assert "started in background" not in res.output.lower(), res.output
    assert res.exit_code != 0


def test_foreground_start_is_unchanged(monkeypatch, vault_dir):
    """--foreground still blocks in-process and never waits on a socket."""
    seen = {}

    def _fake_loop(config, vault, port, pid_file):
        seen["port"] = port

    monkeypatch.setattr(daemon_mod, "_run_daemon_loop", _fake_loop)
    monkeypatch.setattr(daemon_mod, "probe_readiness",
                        lambda *a, **kw: pytest.fail("foreground must not probe"))

    out = daemon_mod.start_daemon(vault_dir, SimpleNamespace(), SimpleNamespace(),
                                  port=8899, foreground=True)
    assert out is None
    assert seen["port"] == 8899


# ═════════════════════════════════════════════════════════════════════════════
#  F5 — the fresh-vault "run <program> init" instruction
# ═════════════════════════════════════════════════════════════════════════════

def _init_advice_spellings() -> tuple:
    """Every way the advice may legitimately spell itself.

    F23: the wheel installs TWO equal console scripts for one entry point, and
    the advice now leads with the documented name. Pinning one literal made the
    positive assertion fail on a pure rename AND — far worse — made the NEGATIVE
    assertion below pass vacuously, since a string that exists nowhere is absent
    from every blob. Resolving against `[project.scripts]` keeps both arms
    load-bearing under either spelling.
    """
    import tomllib
    scripts = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml")
        .read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts, "pyproject declares no console scripts"
    return tuple(f"{p} init" for p in scripts)


class _FakeVault:
    def __init__(self, root):
        self.root = str(root)
        self._idx = {"tools": [], "skills": []}

    def load_index(self, kind):
        return list(self._idx.get(kind, []))


@pytest.fixture()
def seeding_migrator(monkeypatch):
    """Patch the real migrator so it SEEDS the vault, like it does on a fresh
    install (log: 'VaultMigrator 0.0.0 -> 0.10.21: added=41')."""
    holder = {}

    def _seed(vault_dir, *, logger_=None):
        v = holder.get("vault")
        if v is not None:
            v._idx["tools"] = [{"name": f"t{i}"} for i in range(41)]
        return {"added": 41}

    monkeypatch.setattr("systemu.runtime.vault_migrator.run", _seed)
    monkeypatch.setattr(
        "systemu.runtime.first_gate_review.maybe_post_first_gate_review",
        lambda **kw: None)
    return holder


def _capture_advice(monkeypatch):
    events = []
    monkeypatch.setattr("systemu.interface.notifications.log_event",
                        lambda *a, **kw: events.append((a, kw)))
    return events


def test_fresh_vault_is_not_told_to_run_init_when_the_migrator_seeds_it(
        monkeypatch, caplog, tmp_path, seeding_migrator):
    v = _FakeVault(tmp_path / "vault")
    seeding_migrator["vault"] = v
    events = _capture_advice(monkeypatch)

    with caplog.at_level("WARNING", logger=daemon_mod.logger.name):
        daemon_mod._seed_and_advise(v, logger_=daemon_mod.logger)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert not [s for s in _init_advice_spellings() if s in blob], blob
    assert "Vault is empty" not in blob, blob
    assert events == [], f"a bogus operator notification was posted: {events}"


def test_a_genuinely_empty_vault_still_gets_the_advice(monkeypatch, caplog, tmp_path):
    """The advice is not deleted — it fires when the migrator seeds NOTHING."""
    monkeypatch.setattr("systemu.runtime.vault_migrator.run",
                        lambda vault_dir, **kw: {"fast_path": True})
    monkeypatch.setattr(
        "systemu.runtime.first_gate_review.maybe_post_first_gate_review",
        lambda **kw: None)
    v = _FakeVault(tmp_path / "vault")
    events = _capture_advice(monkeypatch)

    with caplog.at_level("WARNING", logger=daemon_mod.logger.name):
        daemon_mod._seed_and_advise(v, logger_=daemon_mod.logger)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert [s for s in _init_advice_spellings() if s in blob], blob
    assert events, "the operator notification must still be posted"


def test_the_seed_advice_has_exactly_one_call_site_and_it_follows_the_migrator():
    """Structural fence: reintroducing the bug means calling the emptiness check
    somewhere other than after the migrator — which trips here."""
    src = Path(daemon_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    sites = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_advise_if_vault_empty"):
                sites.append(fn.name)
    assert sites == ["_seed_and_advise"], (
        "the vault-empty advice must be reachable from exactly one place, "
        f"immediately after the migrator; found call sites: {sites}")

    body = textwrap.dedent(inspect.getsource(daemon_mod._seed_and_advise))
    assert body.index("_v0822_run_vault_migrator") < body.index("_advise_if_vault_empty"), (
        "the emptiness check must run AFTER the migrator has had its chance to seed")

    # and _run_daemon_loop must go through the ordered helper
    loop_src = inspect.getsource(daemon_mod._run_daemon_loop)
    assert "_seed_and_advise(" in loop_src
