"""D4 (dogfood 0.10.28): the health banner ran a BLOCKING socket connect during
page render.

THE WITNESS
-----------
Captured stack, on the shipped 0.10.28 wheel::

    _build_layout -> render_health_banner -> build_health_state
                  -> provider_status -> probe_ollama -> urllib ... sock.connect

``_build_layout`` runs on EVERY dashboard route, so every page paint waited on a
loopback TCP connect to the Ollama endpoint. On this operator's Windows box a
dual-stack ``localhost`` with nothing listening costs ~2 s, and the module's own
docstring measures 1.688 s with Ollama genuinely RUNNING. It correlated with
repeated renderer freezes -- 30 s CDP screenshot timeouts, a "Connection lost"
flash, and eaten clicks.

THE PROPERTY
------------
    NO NETWORK PROBE RUNS ON THE RENDER PATH.

    ``provider_status.all_provider_statuses`` stays THE ONE MINT; a background
    refresher spends the witness and publishes a ``HealthSnapshot``. The render
    READS that snapshot and never probes. Before any snapshot exists the render
    says "Checking providers..." -- an honest third answer, minted with the
    mint's own null witness (``unprobed``), which opens no socket and cannot
    manufacture a green verdict -- and schedules a refresh. A snapshot older
    than the refresh interval is disclosed with its age.

THE PIN
-------
``socket.create_connection``, ``socket.socket.connect`` and
``urllib.request.urlopen`` are replaced with fakes that RAISE and COUNT. A
render that probes trips the counter, whatever the probe then does with the
exception -- ``probe_ollama`` swallows everything, so counting the ATTEMPT is
the only assertion the defect cannot hide from.
"""
from __future__ import annotations

import asyncio
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from systemu.interface.components import health_banner as hb
from systemu.runtime import provider_status as ps

#: ONE event loop, built at import time. ``asyncio.run`` builds a fresh loop per
#: call and a Windows proactor loop opens a self-pipe with ``socket.socketpair``
#: -- which would trip the socket tripwire below on asyncio's own plumbing
#: rather than on a provider probe.
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


# ── the socket tripwire ─────────────────────────────────────────────────────

class _SocketTouched(RuntimeError):
    """Raised by every network primitive the probe could reach for."""


@pytest.fixture()
def no_network(monkeypatch):
    """Every socket primitive raises AND is counted. Returns the counter."""
    import socket
    import urllib.request

    touched = []

    def _trip(*a, **k):
        touched.append(a[:1])
        raise _SocketTouched("the render path opened a socket")

    monkeypatch.setattr(socket, "create_connection", _trip)
    monkeypatch.setattr(socket.socket, "connect", _trip)
    monkeypatch.setattr(urllib.request, "urlopen", _trip)
    return touched


@pytest.fixture()
def bare_machine(monkeypatch):
    """No credential anywhere, a keyless URL that would have to be probed."""
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")
    ps.clear_probe_cache()
    from systemu.runtime import provider_snapshot as snap
    # A refresher left running by an earlier test would probe in the
    # BACKGROUND and trip this test's socket tripwire from another thread.
    snap.stop_refresher()
    snap.clear()
    monkeypatch.setattr(hb, "_count_systemu_daemons", lambda *a, **k: 1)
    monkeypatch.setattr(hb, "_daemon_processes", lambda *a, **k: ())
    yield
    snap.stop_refresher()
    ps.clear_probe_cache()
    snap.clear()


def _cfg(**kw):
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base["ollama_url"] = "http://127.0.0.1:1"
    base.update({f"tier{i}_model": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update(kw)
    return SimpleNamespace(**base)


def _answering(_url, _timeout=1.0):
    return (ps.STATE_REACHABLE, "answered (test)")


def _quiet(_url, _timeout=1.0):
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _provider_issues(state):
    return [i for i in state.issues if "LLM provider" in i.message
            or "Checking providers" in i.message]


# ── a recording NiceGUI, so the REAL renderer can be executed ───────────────

class _Node:
    """Anything a page chains onto a ``ui.<fn>(...)`` call."""

    def __init__(self, rec=None):
        self._rec = rec
        self.value = ""

    def style(self, *a, **k):
        return self

    def classes(self, *a, **k):
        return self

    def props(self, *a, **k):
        return self

    def tooltip(self, *a, **k):
        return self

    def on(self, *a, **k):
        return self

    def on_value_change(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Refreshable:
    def __init__(self, fn):
        self._fn = fn

    def __call__(self, *a, **k):
        return self._fn(*a, **k)

    def refresh(self, *a, **k):
        return self._fn(*a, **k)


class _RecordingUI:
    """Records every ``ui.<fn>(...)`` a page makes, and every timer it arms."""

    def __init__(self):
        self.calls = []
        self.timers = []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def refreshable(self, fn):
        return _Refreshable(fn)

    def timer(self, _delay, callback=None, **k):
        if callback is not None:
            self.timers.append(callback)
        return _Node(self)

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self)
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]


def _render(monkeypatch, vault_dir=None) -> _RecordingUI:
    """Execute the REAL ``render_health_banner`` against a recording ``ui``.

    The refresher is recorded rather than started: a live one would probe from
    ANOTHER thread and trip this file's socket tripwire non-deterministically.
    ``started`` is what the scheduling pin below asserts on.
    """
    from systemu.interface import dashboard_state             # noqa: F401
    from systemu.runtime import provider_snapshot as snap
    rec = _RecordingUI()
    rec.started = []
    monkeypatch.setattr(snap, "start_refresher",
                        lambda *a, **k: rec.started.append((a, k)) or True)
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    hb.render_health_banner(vault_dir)
    return rec


# ── (a) THE REACHABILITY PIN: the render must not touch a socket ────────────

def test_build_health_state_opens_no_socket(no_network, bare_machine):
    """THE REPRO. Today this path reaches urllib -> sock.connect."""
    state = hb.build_health_state(vault_dir=None)
    assert no_network == [], (
        "build_health_state opened a socket on the render path: " + repr(no_network))
    assert state is not None


def test_build_health_state_says_it_is_still_checking(no_network, bare_machine):
    """No snapshot yet -> an honest third answer, not a verdict off a probe."""
    state = hb.build_health_state(vault_dir=None)
    hits = _provider_issues(state)
    assert hits, "the operator must still be told the check is running"
    assert hits[0].message.startswith("Checking providers"), hits[0].message
    assert no_network == []


def test_render_health_banner_opens_no_socket(monkeypatch, no_network,
                                              bare_machine):
    """The whole captured stack, executed: _build_layout's actual call."""
    rec = _render(monkeypatch)
    assert no_network == [], (
        "render_health_banner opened a socket: " + repr(no_network))
    assert [t for t in rec.labels() if t.startswith("Checking providers")], \
        rec.labels()


def test_the_render_schedules_the_refresh_it_declined_to_run(monkeypatch,
                                                             no_network,
                                                             bare_machine):
    """Declining to probe is only honest if something else will."""
    rec = _render(monkeypatch)
    assert rec.started, "the render never started the background refresher"


def test_an_injected_probe_is_still_honoured(no_network, bare_machine):
    """The seam non-render callers use (doctor, tests) is unchanged: an
    EXPLICIT witness is spent, and the copy carries no 'checking' hedge."""
    state = hb.build_health_state(vault_dir=None, config=_cfg(),
                                  provider_probe=_quiet)
    hits = _provider_issues(state)
    assert hits and not hits[0].message.startswith("Checking providers"), hits
    assert no_network == [], "an injected probe must not reach a real socket"


# ── (b) the refresher populates the snapshot; the next render shows it ──────

def test_the_refresher_publishes_a_snapshot_the_render_then_reads(no_network,
                                                                  bare_machine):
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    published = snap.refresh_now(cfg, probe=_answering)
    assert published.statuses["ollama"].satisfied is True
    assert snap.current_snapshot(snap.config_key(cfg)) is not None

    state = hb.build_health_state(vault_dir=None, config=cfg)
    assert _provider_issues(state) == [], (
        "a reachable keyless provider was observed; the banner must go quiet")
    assert no_network == []


def test_an_observed_negative_verdict_drops_the_checking_hedge(no_network,
                                                               bare_machine):
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    snap.refresh_now(cfg, probe=_quiet)
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=cfg))
    assert hits, hits
    assert not hits[0].message.startswith("Checking providers"), hits[0].message
    assert "No LLM provider is usable" in hits[0].message
    assert no_network == []


def test_a_snapshot_about_another_config_is_not_replayed(no_network,
                                                         bare_machine):
    """A verdict is about a config. The Settings page renders for the config it
    was handed; replaying its answer for the daemon's would be DEC-43 form (ii)
    -- a claim derived from a proxy for the thing asked about."""
    from systemu.runtime import provider_snapshot as snap
    snap.refresh_now(_cfg(ollama_url="http://elsewhere:11434"), probe=_answering)
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=_cfg()))
    assert hits and hits[0].message.startswith("Checking providers"), hits
    assert no_network == []


def test_the_snapshot_holds_no_credential(no_network, bare_machine):
    """DEC-31: the fingerprint that keys a snapshot records SET/UNSET, never
    the secret."""
    from systemu.runtime import provider_snapshot as snap
    key = snap.config_key(_cfg(openrouter_api_key="sk-or-verysecret-value"))
    assert "verysecret" not in key, key
    assert "openrouter" in key


# ── (c) the on-demand refresh trigger ("Re-check") ──────────────────────────

def test_request_refresh_updates_the_snapshot_without_blocking_the_caller(
        no_network, bare_machine, monkeypatch):
    """The Re-check button ENQUEUES; the refresher thread does the work."""
    from systemu.runtime import provider_snapshot as snap
    answers = {"state": ps.STATE_UNREACHABLE}

    def _probe(_url, _timeout=1.0):
        return (answers["state"], "test")

    monkeypatch.setitem(ps._PROBES, "ollama", _probe)
    cfg = _cfg()
    key = snap.config_key(cfg)
    assert snap.start_refresher(config_fn=lambda: cfg, interval_s=3600.0) is True
    try:
        deadline = time.monotonic() + 10.0
        while snap.current_snapshot(key) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        first = snap.current_snapshot(key)
        assert first is not None, "the refresher never took a first snapshot"
        assert first.statuses["ollama"].satisfied is False

        answers["state"] = ps.STATE_REACHABLE
        ps.clear_probe_cache()
        assert snap.request_refresh() is True
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            now = snap.current_snapshot(key)
            if now is not None and now.statuses["ollama"].satisfied:
                break
            time.sleep(0.02)
        again = snap.current_snapshot(key)
        assert again is not None and again.statuses["ollama"].satisfied is True, \
            "the on-demand refresh never reached the snapshot"
    finally:
        snap.stop_refresher()
    assert no_network == []


def test_a_fresh_snapshot_does_not_nudge_the_refresher(monkeypatch,
                                                       no_network,
                                                       bare_machine):
    """Nudging on EVERY render would have the refresher probing back to back on
    a busy dashboard -- the render would stop paying for the socket and start
    paying for it in another thread instead."""
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    snap.refresh_now(cfg, probe=_quiet)
    nudges = []
    monkeypatch.setattr(snap, "request_refresh",
                        lambda *a, **k: nudges.append(1) or False)
    hb.build_health_state(vault_dir=None, config=cfg)
    assert nudges == [], "a fresh snapshot must not trigger another probe"


def test_a_stale_snapshot_does_nudge_the_refresher(monkeypatch, no_network,
                                                   bare_machine):
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    snap.refresh_now(cfg, probe=_quiet, _now=time.monotonic() - 90.0)
    nudges = []
    monkeypatch.setattr(snap, "request_refresh",
                        lambda *a, **k: nudges.append(1) or False)
    hb.build_health_state(vault_dir=None, config=cfg)
    assert nudges, "a snapshot past its interval must ask for a fresh one"


def test_request_refresh_is_a_no_op_when_no_refresher_runs(bare_machine):
    from systemu.runtime import provider_snapshot as snap
    snap.stop_refresher()
    assert snap.request_refresh() is False


def test_the_refresher_starts_once_per_process(bare_machine, monkeypatch):
    from systemu.runtime import provider_snapshot as snap
    monkeypatch.setitem(ps._PROBES, "ollama", _quiet)
    cfg = _cfg()
    try:
        assert snap.start_refresher(config_fn=lambda: cfg, interval_s=3600.0) is True
        assert snap.start_refresher(config_fn=lambda: cfg, interval_s=3600.0) is False
    finally:
        snap.stop_refresher()


# ── (d) the age line ────────────────────────────────────────────────────────

def test_a_stale_snapshot_is_disclosed_with_its_age(no_network, bare_machine):
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    snap.refresh_now(cfg, probe=_quiet,
                     _now=time.monotonic() - 90.0)
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=cfg))
    assert hits, hits
    assert "as of" in hits[0].message and "s ago" in hits[0].message, \
        hits[0].message
    assert no_network == []


def test_a_fresh_snapshot_says_nothing_about_its_age(no_network, bare_machine):
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    snap.refresh_now(cfg, probe=_quiet)
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=cfg))
    assert hits and "as of" not in hits[0].message, hits[0].message


def test_the_snapshot_reports_its_own_age_and_staleness():
    from systemu.runtime import provider_snapshot as snap
    now = time.monotonic()
    fresh = snap.HealthSnapshot(taken_at=now, statuses={}, interval_s=30.0)
    old = snap.HealthSnapshot(taken_at=now - 90.0, statuses={}, interval_s=30.0)
    assert fresh.age_s(now) == pytest.approx(0.0, abs=0.5)
    assert fresh.stale is False
    assert old.age_s(now) == pytest.approx(90.0, abs=0.5)
    assert old.stale is True


def test_all_banner_copy_stays_ascii(no_network, bare_machine):
    """DEC-32c."""
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    snap.refresh_now(cfg, probe=_quiet, _now=time.monotonic() - 90.0)
    for issue in hb.build_health_state(vault_dir=None, config=cfg).issues:
        (issue.message + (issue.cta or "")).encode("ascii")


# ── every OTHER render-path caller of the mint ──────────────────────────────

def _render_welcome(monkeypatch, cfg, probe) -> _RecordingUI:
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design import primitives
    from systemu.interface.pages import welcome
    from systemu.interface import persona_content              # noqa: F401

    monkeypatch.setitem(ps._PROBES, "ollama", probe)
    ps.clear_probe_cache()
    monkeypatch.setattr(AppState, "_instance",
                        SimpleNamespace(vault=SimpleNamespace(), config=cfg),
                        raising=False)
    rec = _RecordingUI()
    monkeypatch.setattr(primitives, "button", lambda *a, **k: _Node(rec))
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    welcome.build_welcome_page()
    return rec


def test_the_welcome_wizard_publishes_the_snapshot_the_banner_reads(
        monkeypatch, no_network, bare_machine):
    """/welcome step 1 IS a provider verdict, so it keeps its witness -- but it
    is now the only surface on that page that pays for one. Delete the
    ``refresh_now`` call and the banner on the same page goes back to probing
    inside layout."""
    from systemu.runtime import provider_snapshot as snap
    cfg = _cfg()
    _render_welcome(monkeypatch, cfg, _answering)
    published = snap.current_snapshot(snap.config_key(ps.env_overlay(cfg)))
    assert published is not None, "the wizard observed a verdict and dropped it"
    assert published.statuses["ollama"].satisfied is True
    # ...and the banner that renders on this very page now needs no probe.
    assert _provider_issues(
        hb.build_health_state(vault_dir=None, config=cfg)) == []
    assert no_network == [], repr(no_network)


def test_the_welcome_wizard_still_names_the_usable_provider(monkeypatch,
                                                            no_network,
                                                            bare_machine):
    """The shipped behaviour of step 1 is unchanged by the reroute."""
    rec = _render_welcome(monkeypatch, _cfg(), _answering)
    ready = [t for t in rec.labels() if t.startswith("Provider ready:")]
    assert ready and "Ollama" in ready[0], rec.labels()


def test_the_settings_credentials_card_first_paint_opens_no_socket(
        monkeypatch, no_network, bare_machine):
    """The card's FIRST, synchronous paint uses the null witness already; this
    pins that it stays that way."""
    from unittest import mock
    from systemu.interface.pages import settings

    rec = _RecordingUI()
    with mock.patch.object(settings, "ui", rec):
        settings.provider_credentials_card(_cfg())
    assert no_network == [], (
        "the settings credentials card probed on its first paint: "
        + repr(no_network))


def test_the_settings_observe_pass_publishes_the_snapshot(monkeypatch,
                                                          no_network,
                                                          bare_machine):
    """Its off-loop pass spends a witness anyway -- so it publishes it, and the
    banner on the same page reads it instead of probing."""
    from unittest import mock
    from systemu.interface.pages import settings
    from systemu.runtime import provider_snapshot as snap

    monkeypatch.setitem(ps._PROBES, "ollama", _answering)
    ps.clear_probe_cache()
    cfg = _cfg()
    rec = _RecordingUI()
    with mock.patch.object(settings, "ui", rec):
        settings.provider_credentials_card(cfg)
        assert rec.timers, "the card no longer arms its observe pass"
        _run(rec.timers[0]())
    published = snap.current_snapshot(snap.config_key(cfg))
    assert published is not None, "the observed verdict was never published"
    assert published.statuses["ollama"].satisfied is True
    assert no_network == [], repr(no_network)


def test_the_trust_card_opens_no_socket(no_network, bare_machine):
    from systemu.interface import trust_card
    clause = trust_card.provider_clause(_cfg(tier1_model="openai/gpt-4o"))
    assert no_network == [], repr(no_network)
    assert clause is None or "value" in clause


# ── the mint stays the mint ─────────────────────────────────────────────────

def test_the_refresher_is_the_only_thing_that_calls_the_real_witness():
    """DEC-43: one mint, one call site for the banner. The state builder may
    reach the mint ONLY through the null witness or an injected probe."""
    import inspect
    src = inspect.getsource(hb)
    assert "unprobed" in src, (
        "the render-path mint call must pass the mint's own null witness")
    assert "cache_ttl_s" not in src, (
        "cache_ttl_s means a real reachability probe is being paid for on "
        "this path -- that is D4")
