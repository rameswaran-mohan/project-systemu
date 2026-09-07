"""P0 -- the venv launcher stub is not a daemon, so the banner must not count it.

THE WITNESS
-----------
On a Windows venv install ``python -m venv`` writes ``Scripts/python.exe`` as
``venvlauncher.exe``: a stub that CreateProcess-es the base interpreter with the
SAME argv and waits on it inside a job object. Two process rows, ONE daemon --
the fact D1c made the CLI say out loud.

The banner's D2 liveness fix (0.10.28) did not know it. The stub

  * carries the daemon's command line, so ``_marker_in`` selects it;
  * is genuinely alive, so ``verify_live_daemon`` verifies it;
  * has NO port -- the argv carries none on a default start, and the runtime
    sidecar records the CHILD's pid, so ``_recorded_port`` correctly refuses to
    lend the child's port to it.

Two counted processes, one of them with an unknowable port, is exactly the
fail-closed shape ``daemon_conflict_issue`` answers with DANGER and
``systemu daemon stop --all``. So the banner cried wolf on EVERY healthy
single-daemon venv install on Windows -- and the remedy it printed would have
stopped the operator's only daemon.

THE RULING
----------
    A same-argv process that is the direct PARENT (or an ancestor) of another
    same-argv process is a LAUNCHER, never a daemon. It is excluded before
    anything is counted.

This is the D1b fence's rule, consumed and not re-derived: that fence asserts
that for one vault and one port exactly one process EXECUTES the daemon and
every other process carrying the argv is an ANCESTOR of it -- the launcher on
the way in. A SIBLING or a DESCENDANT is a rival, and stays counted.

The exclusion is decided over the SNAPSHOT set, before liveness verification,
which is what makes the third shape below come out right: a stub whose child
has already died is a stub that is on its way out, not a daemon that is up.

WHAT THIS FILE DOES NOT CLAIM. A daemon that spawned a second daemon is an
ancestor of it too, and this rule would read the first one as a launcher. That
hole is named and closed by
``tests/test_dogfood28_d1_one_daemon_per_vault_and_port.py`` (b.5), whose
witness is the vault's append-only exec log rather than parentage. The banner's
job here is to stop crying wolf about the launcher pair; the self-spawn fence
lives where it can actually see one.

NO REAL PROCESS. ``psutil.process_iter`` is faked at its own module seam,
exactly as the production scan calls it, and the fake rows carry the parent
links the OS would report.
"""
from __future__ import annotations

import json

import pytest

psutil = pytest.importorskip("psutil")

from systemu.interface.components import health_banner as hb  # noqa: E402

_MARKER = "systemu.scheduler.daemon"

#: The sidecar filename the daemon writes about itself.
_SIDECAR = ".systemu_daemon.json"


def _argv(vault: str, port=None, exe: str = r"C:\proj\.venv\Scripts\python.exe") -> list:
    """The daemon's launch line. The stub and its child carry it IDENTICALLY --
    that identity is the whole defect, so the fixture must not soften it."""
    argv = [exe, "-m", _MARKER, "--vault-dir", vault]
    if port is not None:
        argv += ["--port", str(port)]
    return argv


class _Gone(Exception):
    """Stands in for psutil.NoSuchProcess -- the scan must treat it as gone."""


class _FakeProc:
    """One ``process_iter`` row, plus the parent link the OS would report.

    ``parent()`` is the seam the ruling names (psutil's ``parent()``/``ppid()``).
    A row with no parent among the daemon rows answers None, which is what an
    independently launched daemon looks like.
    """

    def __init__(self, pid, cmdline, *, alive=True, live_cmdline=None,
                 parent=None, cwd=None, exe=None):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": list(cmdline)}
        self._alive = alive
        self._live = (list(live_cmdline) if live_cmdline is not None
                      else list(cmdline))
        self._parent = parent
        self._cwd = cwd
        self._exe = exe

    def parent(self):
        return self._parent

    def is_running(self):
        return self._alive

    def cmdline(self):
        if not self._alive:
            raise _Gone("the process exited between the snapshot and this read")
        return list(self._live)

    def cwd(self):
        if self._cwd is None:
            raise _Gone("no cwd")
        return self._cwd

    def exe(self):
        if self._exe is None:
            raise _Gone("no exe")
        return self._exe


@pytest.fixture()
def scan(monkeypatch):
    """Fake ``psutil.process_iter`` at the module seam; reset the TTL caches.

    Returns a callable: give it the rows the OS would yield, get back the
    records the banner would count.
    """
    from systemu.runtime import provider_status as ps

    def _quiet(_url, _timeout=1.0):
        return (ps.STATE_UNREACHABLE, "nothing answered (test)")

    monkeypatch.setitem(ps._PROBES, "ollama", _quiet)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ps.clear_probe_cache()

    def _install(rows, *, self_pid=999999):
        monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: list(rows))
        monkeypatch.setattr(hb.os, "getpid", lambda: self_pid)
        hb._daemon_procs_cache["ts"] = -1e9
        hb._daemon_probe_cache["ts"] = -1e9
        return hb._scan_daemon_processes()

    yield _install
    ps.clear_probe_cache()


def _vault_with_sidecar(tmp_path, *, pid: int, port: int) -> str:
    """A vault whose runtime sidecar records THE CHILD's pid and port.

    That is the real layout: ``_run_daemon_loop`` writes the sidecar about
    itself, so the launcher stub's pid appears nowhere in it.
    """
    vault = tmp_path / "systemu" / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault.parent / _SIDECAR).write_text(
        json.dumps({"pid": pid, "port": port}), encoding="utf-8")
    return str(vault)


def _banner_daemon_issues(state) -> list:
    return [i for i in state.issues if "systemu daemon processes" in i.message]


# --------------------------------------------------------------------------- #
# (a) THE REPRO -- a stub and its child are ONE daemon
# --------------------------------------------------------------------------- #

def _stub_and_child(tmp_path):
    """The witnessed shape: no ``--port`` in the argv (a default start), and a
    sidecar naming the CHILD. The stub's port is therefore unknowable, which is
    what tipped the fail-closed diagnosis into DANGER."""
    vault = _vault_with_sidecar(tmp_path, pid=4321, port=8765)
    stub = _FakeProc(1234, _argv(vault), cwd=r"C:\proj",
                     exe=r"C:\proj\.venv\Scripts\python.exe")
    child = _FakeProc(4321, _argv(vault), parent=stub, cwd=r"C:\proj",
                      exe=r"C:\Python312\python.exe")
    return [stub, child], vault


def test_a_launcher_stub_and_its_child_count_as_one_daemon(scan, tmp_path):
    """THE REPRO. Two rows, one daemon -- and it is the CHILD that is it."""
    rows, _vault = _stub_and_child(tmp_path)
    records = scan(rows)

    assert [d.pid for d in records] == [4321], (
        "the launcher stub was counted as a daemon: " + repr(records))
    assert hb._count_systemu_daemons() == 1


def test_a_launcher_stub_and_its_child_raise_no_DANGER(scan, tmp_path):
    """The operator-facing half -- the cry-wolf itself. A healthy venv install
    must produce no banner issue at all, not a softer one."""
    rows, _vault = _stub_and_child(tmp_path)
    scan(rows)

    state = hb.build_health_state(vault_dir=None)
    assert _banner_daemon_issues(state) == [], (
        "a healthy single-daemon venv install still raises a multi-daemon "
        "banner: " + repr([i.message for i in _banner_daemon_issues(state)]))


def test_the_listing_names_the_child_and_never_the_stub(scan, tmp_path):
    """D2's own rule -- the banner names what it counted -- applied here: the
    pid the operator is told about must be the one that is executing."""
    rows, _vault = _stub_and_child(tmp_path)
    records = scan(rows)

    listing = hb.daemon_listing(records)
    assert "PID 4321" in listing, listing
    assert "PID 1234" not in listing, (
        "the listing names the launcher stub, which no remedy should target: "
        + listing)


def test_the_child_keeps_the_port_its_own_sidecar_records(scan, tmp_path):
    """Fixture premise AND a property: the exclusion must not cost the surviving
    record the port that makes the diagnosis port-aware."""
    rows, _vault = _stub_and_child(tmp_path)
    records = scan(rows)

    assert [d.port for d in records] == [8765], records


# --------------------------------------------------------------------------- #
# (b) two independent children on one port are still a real conflict
# --------------------------------------------------------------------------- #

def _two_pairs():
    """Two separate launches -- each its own stub, each its own child -- both
    children bound to 8765. A SIBLING is a rival, not a launcher."""
    stub_a = _FakeProc(10, _argv(r"C:\a\systemu\vault", 8765), cwd=r"C:\a",
                       exe=r"C:\a\.venv\Scripts\python.exe")
    child_a = _FakeProc(11, _argv(r"C:\a\systemu\vault", 8765), parent=stub_a,
                        cwd=r"C:\a", exe=r"C:\Python312\python.exe")
    stub_b = _FakeProc(20, _argv(r"C:\b\systemu\vault", 8765), cwd=r"C:\b",
                       exe=r"C:\b\.venv\Scripts\python.exe")
    child_b = _FakeProc(21, _argv(r"C:\b\systemu\vault", 8765), parent=stub_b,
                        cwd=r"C:\b", exe=r"C:\Python312\python.exe")
    return [stub_a, child_a, stub_b, child_b]


def test_two_independent_children_on_one_port_still_raise_DANGER(scan):
    scan(_two_pairs())

    state = hb.build_health_state(vault_dir=None)
    hits = _banner_daemon_issues(state)
    assert len(hits) == 1, hits
    assert hits[0].severity == "danger", hits[0]
    assert hits[0].message.startswith("2 systemu daemon processes are running."), (
        "the count includes the launcher stubs: " + hits[0].message)


def test_the_DANGER_names_both_children_and_neither_stub(scan):
    scan(_two_pairs())

    msg = _banner_daemon_issues(hb.build_health_state(vault_dir=None))[0].message
    assert "PID 11" in msg and "PID 21" in msg, msg
    assert "PID 10" not in msg and "PID 20" not in msg, (
        "the DANGER names a launcher stub the operator cannot act on: " + msg)


# --------------------------------------------------------------------------- #
# (c) a stub whose child has died is a stub on its way out
# --------------------------------------------------------------------------- #

def test_a_stub_whose_child_died_is_not_counted(scan, tmp_path):
    """The snapshot caught both rows; by the time they are read the child is
    gone. The child is pruned as stale, and the stub -- which is waiting on a
    process that no longer exists -- is exiting, not serving."""
    vault = _vault_with_sidecar(tmp_path, pid=4321, port=8765)
    stub = _FakeProc(1234, _argv(vault), cwd=r"C:\proj",
                     exe=r"C:\proj\.venv\Scripts\python.exe")
    dead_child = _FakeProc(4321, _argv(vault), parent=stub, alive=False)

    records = scan([stub, dead_child])

    assert records == (), (
        "a launcher stub whose child has exited was counted as a daemon: "
        + repr(records))
    assert hb._count_systemu_daemons() == 0


# --------------------------------------------------------------------------- #
# the exclusion is EVIDENCE, not a silent drop
# --------------------------------------------------------------------------- #

def test_the_excluded_launcher_pids_are_visible(scan, tmp_path):
    """D2's rule for stale records applies to launchers too: a record the scan
    refused to count must be nameable, or a support conversation has nothing to
    go on."""
    rows, _vault = _stub_and_child(tmp_path)
    scan(rows)

    assert set(hb.excluded_launcher_pids()) == {1234}, hb.excluded_launcher_pids()


def test_a_launcher_is_not_reported_as_a_stale_record(scan, tmp_path):
    """The two exclusions mean different things and must stay distinguishable:
    a pruned pid is a record that failed liveness; a launcher passed liveness
    and is simply not a daemon."""
    rows, _vault = _stub_and_child(tmp_path)
    scan(rows)

    assert 1234 not in set(hb.pruned_daemon_pids()), hb.pruned_daemon_pids()


# --------------------------------------------------------------------------- #
# the parentage probe is a probe of somebody else's process
# --------------------------------------------------------------------------- #

def test_a_process_object_that_answers_no_parent_yields_no_launchers():
    proc = _FakeProc(5, _argv("/v", 8765))
    assert hb.launcher_stub_pids([(5, proc)]) == frozenset()


def test_the_launcher_probe_never_raises_on_a_hostile_process_object():
    class _Hostile:
        pid = 7

        def parent(self):
            raise RuntimeError("access denied")

    assert hb.launcher_stub_pids([(7, _Hostile())]) == frozenset()


def test_a_parent_cycle_cannot_spin_the_launcher_probe():
    """A hostile or confused parent chain must terminate. The bound is the
    rule's, not the tree's."""
    a = _FakeProc(1, _argv("/v", 8765))
    b = _FakeProc(2, _argv("/v", 8765), parent=a)
    a._parent = b

    assert hb.launcher_stub_pids([(1, a), (2, b)]) == frozenset({1, 2})


def test_an_ancestor_reached_THROUGH_a_non_daemon_process_is_still_a_launcher():
    """The ruling says PARENT *or ancestor*, and only the walk can honour it.

    The intermediate here carries no daemon argv -- a shell, a service wrapper,
    a ``cmd /c`` -- so it is not among the rows at all. Checking ``ppid()``
    alone would find no daemon parent for the leaf and would count the launcher
    at the top as a second daemon, which is the defect wearing a longer chain.
    """
    top = _FakeProc(1, _argv("/v", 8765))
    shim = _FakeProc(2, [r"C:\Windows\system32\cmd.exe", "/c"], parent=top)
    leaf = _FakeProc(3, _argv("/v", 8765), parent=shim)

    assert hb.launcher_stub_pids([(1, top), (3, leaf)]) == frozenset({1})
