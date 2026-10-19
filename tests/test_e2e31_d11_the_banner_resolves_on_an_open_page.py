"""D-11 (e2e 0.10.31): the health banner was render-time only, so a page opened
before the first snapshot said "Checking providers..." until it was reloaded --
and while it waited it also contradicted the page it sat on top of.

THE WITNESS
-----------
Two halves of one finding.

* D4 moved the provider verdict onto a background refresher, and the banner
  reads that snapshot ON RENDER. A page painted before the refresher's first
  round therefore renders the "we have not asked yet" copy and then never
  changes: nothing on an already-painted page ever looks again. The operator's
  only remedy is F5, which is exactly the "is it broken or is it working?"
  moment the banner exists to remove.

* The copy it froze on was not neutral. It read::

      Checking providers... No LLM provider is usable from the daemon's
      environment so far: no credential is set, and whether the keyless
      provider answers has not been checked yet. ...

  On /welcome that sentence sat directly above step 1's "Provider ready: ..."
  line, which is minted from the same ``provider_status``. Two sentences, one
  screen, opposite claims -- and the banner's was the one derived from an
  observation nobody had taken yet.

THE RULING
----------
    A HEDGE MAY NOT CARRY A VERDICT, AND AN OPEN PAGE MUST RESOLVE ITSELF.

    With no snapshot the banner says it is checking and stops. It states no
    finding and offers no remedy, because it has established neither. The
    verdict sentence and its CTA are reachable only from an OBSERVED negative.

    A timer compares the published snapshots' ``taken_at`` and refreshes the
    banner when they change -- and only then. So the page an operator already
    has open resolves within one interval, without a reload.

THE PIN
-------
The builder/timer seam, driven through the real ``render_health_banner``
against a recording ``ui``: the timer callback it arms is captured and called
by hand, and ``build_health_state`` is counted, so "it re-rendered" and "it did
not re-render" are both assertions rather than absences.
"""
from __future__ import annotations

import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from systemu.interface.components import health_banner as hb
from systemu.runtime import provider_snapshot as psnap
from systemu.runtime import provider_status as ps
from systemu.runtime import render_snapshots as rs


#: The sentence that must never appear beside "Checking providers...".
_VERDICT = "No LLM provider is usable"


@pytest.fixture()
def bare_machine(monkeypatch):
    """No credential, no snapshot, no refresher, no daemon records."""
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")
    ps.clear_probe_cache()
    psnap.stop_refresher()
    psnap.clear()
    rs.stop_refresher()
    rs.clear()
    monkeypatch.setattr(hb, "_daemon_processes", lambda *a, **k: ())
    monkeypatch.setattr(hb, "_count_systemu_daemons", lambda *a, **k: 1)
    monkeypatch.setattr(hb, "_lockout_degraded", lambda: ())
    monkeypatch.setattr(hb, "_storage_degraded", lambda: None)
    yield
    psnap.stop_refresher()
    psnap.clear()
    rs.stop_refresher()
    rs.clear()
    ps.clear_probe_cache()


def _cfg(**kw):
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base["ollama_url"] = "http://127.0.0.1:1"
    base.update({f"tier{i}_model": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update(kw)
    return SimpleNamespace(**base)


def _quiet(_url, _timeout=1.0):
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _answering(_url, _timeout=1.0):
    return (ps.STATE_REACHABLE, "answered (test)")


def _provider_issues(state):
    return [i for i in state.issues
            if "LLM provider" in i.message or "Checking providers" in i.message]


# ── a recording NiceGUI that keeps its refreshables and timers ──────────────

class _Node:
    def __init__(self):
        self.value = ""

    def __getattr__(self, name):
        def _m(*a, **k):
            return self
        return _m

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
    def __init__(self):
        self.calls = []
        self.timers = []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def refreshable(self, fn):
        return _Refreshable(fn)

    def timer(self, delay, callback=None, **k):
        if callback is not None:
            self.timers.append((delay, callback))
        return _Node()

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node()
        return _call

    def labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "label" and a]

    def tick(self):
        for _delay, cb in self.timers:
            cb()


def _render(monkeypatch, vault_dir=None) -> _RecordingUI:
    rec = _RecordingUI()
    monkeypatch.setattr(psnap, "start_refresher", lambda *a, **k: True)
    monkeypatch.setattr(rs, "start_refresher", lambda *a, **k: True)
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    hb.render_health_banner(vault_dir)
    return rec


#: Captured at import, before any test can replace it.
_REAL_PROVIDER_VIEW = hb._provider_view


def _pin_provider_view(monkeypatch, config):
    """Make the render score the config THIS test named.

    ``render_health_banner`` takes no config -- it is about the daemon's own
    environment -- so without this a test would be asserting on whatever .env
    the developer's box happens to have. The seam is ``_provider_view``, the
    same one D4's file drives, and the REAL one still runs behind it.
    """
    monkeypatch.setattr(hb, "_provider_view",
                        lambda *a, **k: _REAL_PROVIDER_VIEW(config, None))


# ── (a) a hedge carries no verdict ─────────────────────────────────────────

def test_with_no_snapshot_the_banner_states_no_verdict(bare_machine):
    """THE REPRO's copy half. Before the fix this message both hedged AND
    asserted, in one sentence."""
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=_cfg()))
    assert hits, "the operator must still be told a check is running"
    assert hits[0].message.startswith(hb.PROVIDER_CHECKING_PREFIX.strip()), \
        hits[0].message
    assert _VERDICT not in hits[0].message, hits[0].message
    assert "not usable" not in hits[0].message, hits[0].message


def test_with_no_snapshot_the_banner_offers_no_remedy(bare_machine):
    """A remedy IS a verdict: 'set a key and restart' says the thing is broken.
    Nothing has been established, so nothing is prescribed."""
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=_cfg()))
    assert hits[0].cta is None, hits[0].cta


def test_the_first_paint_does_not_contradict_the_welcome_step(monkeypatch,
                                                              bare_machine):
    """The screen the finding was seen on: /welcome step 1 mints 'Provider
    ready: ...' from provider_status. The banner above it must not be asserting
    the opposite from an observation nobody took."""
    _pin_provider_view(monkeypatch, _cfg())
    rec = _render(monkeypatch)
    painted = " ".join(rec.labels())
    assert hb.PROVIDER_CHECKING_PREFIX.strip() in painted, rec.labels()
    assert _VERDICT not in painted, rec.labels()


def test_an_observed_negative_still_states_the_verdict_and_the_remedy(
        bare_machine):
    """The other side of the line, unchanged: once the witness HAS been spent,
    the banner says what it found and how to fix it."""
    cfg = _cfg()
    psnap.refresh_now(cfg, probe=_quiet)
    hits = _provider_issues(hb.build_health_state(vault_dir=None, config=cfg))
    assert hits, hits
    assert _VERDICT in hits[0].message, hits[0].message
    assert hits[0].cta, "an established finding must carry its remedy"


# ── (b) an open page resolves itself ───────────────────────────────────────

def test_the_render_arms_a_timer(monkeypatch, bare_machine):
    """THE REPRO's other half. Before the fix nothing on an already-painted
    page ever looked again."""
    rec = _render(monkeypatch)
    assert rec.timers, "the banner armed no timer; an open page can never resolve"
    delay = rec.timers[0][0]
    assert type(delay) is float or type(delay) is int, delay
    assert 0 < float(delay) <= 10.0, (
        "a banner that resolves in %s s has not resolved" % delay)


def test_the_timer_re_renders_when_a_snapshot_is_taken(monkeypatch,
                                                       bare_machine):
    rec = _render(monkeypatch)
    builds = []
    real = hb.build_health_state
    monkeypatch.setattr(hb, "build_health_state",
                        lambda *a, **k: builds.append(1) or real(*a, **k))
    rec.tick()
    assert builds == [], "nothing changed, yet the banner re-rendered"

    psnap.refresh_now(_cfg(), probe=_quiet)
    rec.tick()
    assert builds == [1], "a fresh snapshot did not re-render the open banner"


def test_the_timer_does_not_re_render_when_nothing_changed(monkeypatch,
                                                           bare_machine):
    """The other half of the same property. A timer that repaints every tick
    destroys and rebuilds the DOM twice a second for nothing -- the defect
    class ui_helpers.RepaintGate exists for."""
    psnap.refresh_now(_cfg(), probe=_quiet)
    rec = _render(monkeypatch)
    builds = []
    real = hb.build_health_state
    monkeypatch.setattr(hb, "build_health_state",
                        lambda *a, **k: builds.append(1) or real(*a, **k))
    for _ in range(5):
        rec.tick()
    assert builds == [], "the banner repainted %d times with nothing new" % len(builds)


def test_a_daemon_scan_landing_also_re_renders(monkeypatch, bare_machine):
    """The change detector watches EVERY published observation, not just the
    provider one -- a second daemon appearing must reach an open page too."""
    rec = _render(monkeypatch)
    builds = []
    real = hb.build_health_state
    monkeypatch.setattr(hb, "build_health_state",
                        lambda *a, **k: builds.append(1) or real(*a, **k))
    rs.publish(rs.DAEMON_SCAN, rs.DaemonScanSnapshot(taken_at=time.monotonic()))
    rec.tick()
    assert builds == [1], "a fresh daemon scan did not re-render the open banner"


def test_the_open_page_goes_quiet_when_the_verdict_turns_good(monkeypatch,
                                                              bare_machine):
    """End to end: the operator starts Ollama, the refresher observes it, and
    the banner on the page they already have open stops nagging -- no reload."""
    cfg = _cfg()
    _pin_provider_view(monkeypatch, cfg)
    rec = _render(monkeypatch)
    assert any(t.startswith(hb.PROVIDER_CHECKING_PREFIX.strip())
               for t in rec.labels()), rec.labels()

    psnap.refresh_now(cfg, probe=_answering)
    before = len(rec.calls)
    rec.tick()
    painted = " ".join(str(a[0]) for n, a, _k in rec.calls[before:]
                       if n == "label" and a)
    assert hb.PROVIDER_CHECKING_PREFIX.strip() not in painted, painted
    assert _VERDICT not in painted, painted


def test_the_banner_is_mounted_even_when_the_machine_is_healthy(monkeypatch,
                                                                bare_machine):
    """A banner that returns early when it has nothing to say cannot APPEAR
    later. The container and the timer are armed either way; only the contents
    are conditional."""
    monkeypatch.setattr(hb, "build_health_state", lambda *a, **k: hb.HealthState())
    rec = _render(monkeypatch)
    assert rec.timers, "a healthy first paint armed no timer"
    assert rec.labels() == [], (
        "a healthy banner must still paint nothing: " + repr(rec.labels()))
