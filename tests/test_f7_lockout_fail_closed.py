"""F7 -- brute-force protection must never become a silent no-op.

Live defect this pins (v0.10.22): ``LockoutStore._save`` caught EVERY exception
and only ``logger.warning``-ed it (the handler was even marked
``# pragma: no cover``), and ``_load`` returned ``{}`` on any error.  With an
unwritable store path, five consecutive ``record_failure`` calls left
``is_locked()`` False -- every failed login forgotten, brute-force protection
gone, recorded only as a buried warning in the daemon log.  That is FAIL-OPEN,
against the project's fail-closed doctrine (DEC-32).

DESIGN CHOSEN -- bounded in-memory fallback + a loud operator-visible health
issue, in preference to either silent-open or hard-deny:

  * silent-open is the defect;
  * hard-deny turns a transient disk hiccup into a PERMANENT self-lockout, and
    worse, locks the operator out of the very dashboard they would use to fix
    the disk;
  * the fallback keeps the counter running for the life of the process, so
    protection is genuinely ENFORCED (not merely claimed), and the loss is
    reduced to durability across a restart -- which is exactly what the health
    issue tells the operator about.

Refusing a *successful* login while degraded was rejected for the same
self-lockout reason: a failed attempt is still denied and still counted, which
is the part brute-force cares about.

PROPERTY
    Brute-force protection is never silently absent.  For every way the store
    can fail to persist or to be read, (a) failures are still counted and the
    threshold still locks, and (b) the degradation is reported on the
    operator's health surface -- never only in a log line.

FENCE
    ``test_failures_are_never_forgotten`` and
    ``test_degradation_is_always_reported`` below, both parametrised over every
    store-failure mode (unwritable directory, write error, corrupt file), plus
    ``test_health_banner_surfaces_the_degradation`` which pins the operator
    surface rather than the internal flag.

WITNESS
    Restore the old ``_save``/``_load`` (swallow and return ``{}``) and both
    fences go red: is_locked() stays False after the threshold, and the health
    surface reports nothing.

The pre-existing guarantees (per-IP lockout at 5, global lockout at 20 across
distinct IPs, success clearing the per-IP counter, window-expiry reset) are
re-asserted here against a DEGRADED store, and are unchanged for a healthy one
(tests/test_rsec1_dashboard_auth.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime import dashboard_auth as da


@pytest.fixture(autouse=True)
def _clean_lockout_state():
    da.reset_lockout_state()
    yield
    da.reset_lockout_state()


# --------------------------------------------------------------------------- #
# every way the store can fail
# --------------------------------------------------------------------------- #

def _unwritable_dir(tmp_path, monkeypatch):
    """The store's parent directory is occupied by a regular file, so
    ``mkdir(parents=True, exist_ok=True)`` raises and nothing can be written.
    This is the exact shape observed live."""
    blocker = tmp_path / "secrets"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    return blocker / "dashboard_lockout.json"


def _write_error(tmp_path, monkeypatch):
    """The directory is fine; the atomic replace fails (disk full, AV lock,
    read-only mount)."""
    def _boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(da.os, "replace", _boom)
    return tmp_path / "secrets" / "dashboard_lockout.json"


def _corrupt_file(tmp_path, monkeypatch):
    """The file exists but cannot be parsed -- the read side of the fail-open."""
    p = tmp_path / "secrets" / "dashboard_lockout.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json at all", encoding="utf-8")
    return p


#: every way the store can fail.  Used for the "no failure is ever forgotten"
#: half of the property, which holds for all of them.
_FAILURE_MODES = pytest.mark.parametrize(
    "make_path", [_unwritable_dir, _write_error, _corrupt_file],
    ids=["unwritable-dir", "write-error", "corrupt-file"],
)

#: the modes where persistence stays broken.  A corrupt file SELF-HEALS -- the
#: next successful write replaces it with valid JSON -- so it is degraded only
#: until then (pinned separately in
#: ``test_a_corrupt_store_is_reported_while_it_is_unreadable``).
_PERSISTENT_FAILURE_MODES = pytest.mark.parametrize(
    "make_path", [_unwritable_dir, _write_error],
    ids=["unwritable-dir", "write-error"],
)


# --------------------------------------------------------------------------- #
# FENCE (a) -- the counter keeps counting
# --------------------------------------------------------------------------- #

@_FAILURE_MODES
def test_failures_are_never_forgotten(tmp_path, monkeypatch, make_path):
    """5 consecutive failures must lock the IP whatever the store is doing.

    Observed live BEFORE this fix: is_locked() == False after 5 failures.
    """
    store = da.LockoutStore(make_path(tmp_path, monkeypatch))
    ip = "203.0.113.7"
    for _ in range(da.FAILURE_THRESHOLD):
        assert store.is_locked(ip) is False
        store.record_failure(ip)
    assert store.is_locked(ip) is True


@_FAILURE_MODES
def test_global_spray_still_locks_when_the_store_is_broken(tmp_path, monkeypatch, make_path):
    """The distributed-spray defence must not evaporate either."""
    store = da.LockoutStore(make_path(tmp_path, monkeypatch))
    assert store.is_globally_locked() is False
    for i in range(da.GLOBAL_FAILURE_THRESHOLD):
        store.record_failure(f"198.51.100.{i}")
    assert store.is_globally_locked() is True


@_FAILURE_MODES
def test_login_attempt_is_refused_once_the_degraded_counter_trips(tmp_path, monkeypatch, make_path):
    """End-to-end through the real decision core, not just the store."""
    from systemu.interface.pages.login import _attempt_login
    stored_hash = da.hash_passphrase("correct horse battery staple")
    store = da.LockoutStore(make_path(tmp_path, monkeypatch))
    ip = "203.0.113.9"
    for _ in range(da.FAILURE_THRESHOLD):
        ok, reason = _attempt_login("wrong", stored_hash, store, ip)
        assert (ok, reason) == (False, "incorrect")
    # the NEXT attempt is refused without even checking the passphrase --
    # and that holds even for the correct one
    ok, reason = _attempt_login("correct horse battery staple", stored_hash, store, ip)
    assert (ok, reason) == (False, "locked")


@_FAILURE_MODES
def test_the_fallback_counter_is_shared_across_store_instances(tmp_path, monkeypatch, make_path):
    """systemu/interface/pages/login.py builds a NEW LockoutStore on EVERY
    render of ``/login`` (``lockout = _lockout_store(vault)`` inside the page
    function).

    So a fallback counter kept on the INSTANCE would be reset by reloading the
    login page between guesses -- worthless against precisely the attack it
    defends against.  The fallback is therefore keyed by store PATH and shared
    process-wide.
    """
    path = make_path(tmp_path, monkeypatch)
    ip = "203.0.113.59"
    for _ in range(da.FAILURE_THRESHOLD):
        assert da.LockoutStore(path).is_locked(ip) is False   # fresh instance
        da.LockoutStore(path).record_failure(ip)              # fresh instance
    assert da.LockoutStore(path).is_locked(ip) is True


@_PERSISTENT_FAILURE_MODES
def test_a_fresh_instance_sees_the_degradation(tmp_path, monkeypatch, make_path):
    """The health surface must not depend on holding the object that failed."""
    path = make_path(tmp_path, monkeypatch)
    da.LockoutStore(path).record_failure("203.0.113.61")
    assert da.LockoutStore(path).health().durable is False
    assert da.lockout_degradations()


def test_a_counter_already_at_the_threshold_is_not_forgotten_when_the_file_rots(tmp_path):
    """Failures counted while healthy survive the file going unreadable."""
    p = tmp_path / "secrets" / "dashboard_lockout.json"
    store = da.LockoutStore(p)
    ip = "203.0.113.11"
    for _ in range(da.FAILURE_THRESHOLD):
        store.record_failure(ip)
    assert store.is_locked(ip) is True
    p.write_text("{ corrupted by something else", encoding="utf-8")
    assert store.is_locked(ip) is True          # was False before this fix


# --------------------------------------------------------------------------- #
# FENCE (b) -- the operator is told, on a surface they look at
# --------------------------------------------------------------------------- #

@_PERSISTENT_FAILURE_MODES
def test_degradation_is_always_reported(tmp_path, monkeypatch, make_path):
    store = da.LockoutStore(make_path(tmp_path, monkeypatch))
    store.record_failure("203.0.113.13")

    health = store.health()
    assert health.enforced is True          # still counting ...
    assert health.durable is False          # ... but not across a restart
    assert health.reason                    # and it says why

    reported = da.lockout_degradations()
    assert len(reported) == 1, reported
    assert reported[0]["path"] == str(store.path)
    assert reported[0]["reason"]


def test_a_corrupt_store_is_reported_while_it_is_unreadable(tmp_path):
    """An unreadable file degrades the store, and self-heals on the next write.

    Both halves matter: the read-side fail-open (``_load`` returning ``{}``) is
    reported rather than silent, and a one-off corruption does not leave a
    permanent scary banner once the file has been rewritten.
    """
    p = _corrupt_file(tmp_path, None)
    store = da.LockoutStore(p)

    assert store.is_locked("203.0.113.53") is False   # the unreadable read
    assert store.health().durable is False
    assert da.lockout_degradations()

    store.record_failure("203.0.113.53")              # rewrites valid JSON
    assert store.health().durable is True
    assert da.lockout_degradations() == ()
    assert json.loads(p.read_text(encoding="utf-8"))["203.0.113.53"]["fails"] == 1


@_PERSISTENT_FAILURE_MODES
def test_health_banner_surfaces_the_degradation(tmp_path, monkeypatch, make_path):
    """The operator-visible surface, not the internal flag.

    A silent no-op recorded only in a log line is the defect; this pins that it
    reaches the dashboard banner as a DANGER issue with a remediation.
    """
    from systemu.interface.components import health_banner as hb
    store = da.LockoutStore(make_path(tmp_path, monkeypatch))
    store.record_failure("203.0.113.17")

    state = hb.build_health_state(vault_dir=None)
    hits = [i for i in state.issues if "brute-force" in i.message.lower()]
    assert hits, [i.message for i in state.issues]
    assert hits[0].severity == "danger"
    assert hits[0].cta


def test_the_health_banner_has_a_production_call_site():
    """Reachability pin: the surface this fix relies on must be RENDERED.

    ``build_health_state`` returning the right issue is worthless if nothing
    paints it.  Delete the dashboard's ``render_health_banner(...)`` call and
    this test goes red, which is the whole point -- a half-built control that
    computes a warning nobody sees is the failure mode this repo keeps hitting.
    """
    import inspect
    from systemu.interface import dashboard
    src = inspect.getsource(dashboard)
    assert "render_health_banner(" in src, (
        "no production call site paints the health banner -- the lockout "
        "degradation issue would be computed and never shown"
    )
    # and the call must be a real invocation, not just the import
    assert src.count("render_health_banner") >= 2, src.count("render_health_banner")


def test_a_healthy_store_reports_no_degradation(tmp_path):
    from systemu.interface.components import health_banner as hb
    store = da.LockoutStore(tmp_path / "secrets" / "dashboard_lockout.json")
    for _ in range(da.FAILURE_THRESHOLD):
        store.record_failure("203.0.113.19")
    assert store.is_locked("203.0.113.19") is True

    health = store.health()
    assert (health.enforced, health.durable) == (True, True)
    assert da.lockout_degradations() == ()
    state = hb.build_health_state(vault_dir=None)
    assert not [i for i in state.issues if "brute-force" in i.message.lower()]


def test_degradation_clears_once_the_store_can_persist_again(tmp_path, monkeypatch):
    """A transient hiccup must not leave a permanent scary banner."""
    p = tmp_path / "secrets" / "dashboard_lockout.json"
    store = da.LockoutStore(p)
    boom = {"on": True}

    real_replace = da.os.replace

    def _maybe(*a, **k):
        if boom["on"]:
            raise OSError(28, "No space left on device")
        return real_replace(*a, **k)

    monkeypatch.setattr(da.os, "replace", _maybe)
    store.record_failure("203.0.113.23")
    assert store.health().durable is False
    assert da.lockout_degradations()

    boom["on"] = False
    store.record_failure("203.0.113.23")
    assert store.health().durable is True
    assert da.lockout_degradations() == ()
    assert json.loads(p.read_text(encoding="utf-8"))["203.0.113.23"]["fails"] == 2


# --------------------------------------------------------------------------- #
# the guarantees that ALREADY worked must keep working (healthy store)
# --------------------------------------------------------------------------- #

def test_success_still_clears_the_per_ip_counter_on_a_healthy_store(tmp_path):
    store = da.LockoutStore(tmp_path / "lock.json")
    ip = "203.0.113.29"
    for _ in range(3):
        store.record_failure(ip)
    store.record_success(ip)
    for _ in range(da.FAILURE_THRESHOLD - 1):
        store.record_failure(ip)
    assert store.is_locked(ip) is False


def test_success_still_clears_the_per_ip_counter_on_a_degraded_store(tmp_path, monkeypatch):
    store = da.LockoutStore(_write_error(tmp_path, monkeypatch))
    ip = "203.0.113.31"
    for _ in range(3):
        store.record_failure(ip)
    store.record_success(ip)
    for _ in range(da.FAILURE_THRESHOLD - 1):
        store.record_failure(ip)
    assert store.is_locked(ip) is False         # counter really was cleared
    store.record_failure(ip)
    assert store.is_locked(ip) is True          # ... and really is still counting


def test_window_expiry_still_resets_on_a_degraded_store(tmp_path, monkeypatch):
    store = da.LockoutStore(_write_error(tmp_path, monkeypatch))
    ip = "203.0.113.37"
    base = 1_000_000.0
    monkeypatch.setattr(da.time, "time", lambda: base)
    for _ in range(da.FAILURE_THRESHOLD):
        store.record_failure(ip)
    assert store.is_locked(ip) is True

    monkeypatch.setattr(da.time, "time", lambda: base + da.LOCKOUT_SECONDS + 1)
    assert store.is_locked(ip) is False
    store.record_failure(ip)
    assert store.is_locked(ip) is False         # fresh window, not instant re-lock


def test_lockout_is_still_per_ip_on_a_degraded_store(tmp_path, monkeypatch):
    store = da.LockoutStore(_write_error(tmp_path, monkeypatch))
    for _ in range(da.FAILURE_THRESHOLD):
        store.record_failure("203.0.113.41")
    assert store.is_locked("203.0.113.41") is True
    assert store.is_locked("203.0.113.43") is False


def test_persistence_across_instances_is_unchanged_when_healthy(tmp_path):
    p = tmp_path / "lock.json"
    s1 = da.LockoutStore(p)
    for _ in range(da.FAILURE_THRESHOLD):
        s1.record_failure("203.0.113.47")
    assert da.LockoutStore(p).is_locked("203.0.113.47") is True
