"""R-UX2 / SPEC Part II §15-UX **UX-9(f)** — the "/health" system-under-load chip.

UX-9(f): "``/health`` shows a 'system under load' chip (loop-lag + CPU) so
residual slowness is **explained**."

**Every snapshot in this module is built by the real producer.** The first cut of
these tests used a hand-written ``_snap()`` helper that set ``p95_ms`` and
``under_load`` as an always-consistent PAIR — so the producer-side bug, where
those two came from *different windows*, was structurally unobservable no matter
how many chip tests were added. A fixture that can only express consistent
states cannot fail on an inconsistent one. So the snapshots here come out of
``LoopLagWatchdog.record()``, and the two readings that refuted the first cut
are asserted at the CHIP level, where an operator would have read them.

The render is pinned twice: once by executing ``build_health_page`` against a
recording ``nicegui.ui`` stub (no NiceGUI runtime needed), and once on the AST.
Deleting the four lines that put the chip on the page left all 45 of the earlier
tests green — the page's only prior coverage was ``assert callable(...)``.
"""
from __future__ import annotations

import ast
import inspect
import sys
import types
from unittest import mock

import pytest

from systemu.interface.pages import health
from systemu.runtime import loop_lag
from systemu.runtime.loop_lag import LoopLagWatchdog


_OK = dict(provider_configured=True, provider_reachable=True,
           keyring_locked=False, daemon_running=True)

# A calm, MEASURED CPU series — the state a production box is normally in.
_CALM_CPU = (22.0,) * 5


class _Clock:
    """Only used for the staleness cases. Deliberately a local 6-line helper:
    ``tests/`` has no ``__init__.py`` and no test module imports another, so
    reaching into ``test_rux2_loop_lag`` for this would invent a new (and
    fragile) cross-module test dependency to save five lines of scaffolding."""

    def __init__(self, t: float = 10_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


def _feed_calm_cpu(wd, pcts=_CALM_CPU) -> None:
    """Give ``wd`` a MEASURED CPU half, spaced at its own poll cadence.

    Needed wherever a test drives ``record()`` directly: ``load_state`` is a
    composite, and a watchdog fed only lag reports "unknown" — correctly, but it
    is not the state a production install is in.
    """
    now = wd._clock()
    pcts = list(pcts)
    for i, pct in enumerate(pcts):
        ts = now - (len(pcts) - 1 - i) * wd._cpu_poll_s
        wd._add_cpu_reading(float(pct), ts=ts, covers_from=ts - wd._cpu_poll_s)


def _produced(lags, *, interval_s: float = 0.1, cpu=None, **kw) -> dict:
    """A snapshot built the way production builds one: through ``record()``.

    The clock advances by ``interval_s + lag`` per sample, because that is how
    long the beat that observed ``lag`` actually took. Recording back-to-back
    instead would put every sample inside the 5-second window no matter how many
    there were — which would make ``recent`` and ``ring`` identical and quietly
    turn the regression tests below into tautologies. (It did: the first draft of
    this helper did exactly that, and the precondition assertion in
    ``test_a_recovered_loop_...`` is what caught it.)

    ``cpu`` defaults to a calm MEASURED series, because ``load_state`` is a
    composite of lag AND CPU and ``psutil`` is a hard dependency in
    ``pyproject.toml`` — a production install always has a CPU half, so a
    fixture without one was modelling a broken box while asserting healthy
    output. Pass ``cpu=[]`` for a genuinely unmeasured CPU.

    Note the explicit ``is None``: ``cpu or []`` would collapse "not specified"
    and "explicitly none" into one case, which is precisely the distinction
    these tests now turn on.
    """
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, **kw)
    for lag in lags:
        clock.advance(interval_s + float(lag) / 1000.0)
        wd.record(float(lag))
    pcts = [float(p) for p in (_CALM_CPU if cpu is None else cpu)]
    # Spaced at the real poll cadence and ending "now", with each reading's
    # covers_from threaded from the previous stamp — the shape `_refresh_cpu`
    # produces. Readings dropped in at one instant would not exercise expiry.
    for i, pct in enumerate(pcts):
        ts = clock() - (len(pcts) - 1 - i) * wd._cpu_poll_s
        wd._add_cpu_reading(pct, ts=ts, covers_from=ts - wd._cpu_poll_s)
    return wd.snapshot()


@pytest.fixture(autouse=True)
def _clean_watchdog():
    loop_lag.reset_watchdog()
    yield
    loop_lag.reset_watchdog()


# ── the three states ─────────────────────────────────────────────────────────

def test_a_responsive_loop_renders_a_responsive_chip_with_its_numbers():
    v = health.health_view(load=_produced([1.0] * 50, cpu=[22.0] * 5), **_OK)
    chip = v["load_chip"]
    assert chip["state"] == "normal"
    assert chip["label"] == "RESPONSIVE"
    assert "1ms" in chip["detail"]         # the p95 is shown, not just a colour
    assert "22%" in chip["detail"]         # ...and so is CPU


def test_under_load_renders_a_busy_chip_that_explains_the_slowness():
    snap = _produced([340.0] * 50, cpu=[91.0] * 5)
    v = health.health_view(load=snap, **_OK)
    chip = v["load_chip"]
    assert chip["state"] == "busy"
    assert chip["label"] == "UNDER LOAD"
    assert "340ms" in chip["detail"] and "91%" in chip["detail"]
    # the point of the chip is that residual slowness is EXPLAINED, not hidden
    assert snap["recent_breaches"] > 0
    assert f"{snap['recent_breaches']} stall(s)" in chip["detail"]
    assert chip["label"].lower() != "responsive"


def test_unmeasured_load_is_reported_as_unknown_never_as_healthy():
    """The failure class this page has already shipped once (see the
    ``_needs_you_section`` docstring): with no data we do not KNOW the UI is
    responsive, and /health is reachable precisely when the install is broken —
    exactly when a confident "responsive" would be both wrong and reassuring."""
    v = health.health_view(load=_produced([]), **_OK)
    chip = v["load_chip"]
    assert chip["state"] == "unknown"
    assert chip["label"] == "LOAD NOT MEASURED"
    assert chip["state"] != "normal"
    assert "unknown" in chip["detail"].lower()


def test_a_missing_cpu_reading_is_DECLARED_not_silently_dropped():
    """MUTATION TARGET. This test previously asserted the defect:
    ``state == "normal"`` and ``"CPU" not in detail``.

    The CPU clause was appended only ``if cpu is not None``, so an unmeasured
    CPU disappeared from the justification — the chip listed the inputs it had
    checked and looked complete, while announcing a verdict on half of them.
    Omission is not a safe default for an input to a load verdict: the operator
    cannot tell "CPU is fine" from "CPU was never read".

    Both halves are pinned: the state must not be the healthy one, and the
    missing input must be named.
    """
    v = health.health_view(load=_produced([1.0] * 50, cpu=[]), **_OK)
    chip = v["load_chip"]
    assert chip["state"] == "unknown", chip
    assert chip["state"] != "normal"
    assert "CPU not measured" in chip["detail"], chip["detail"]
    assert "None" not in chip["detail"]              # declared, not rendered "None"
    # the lag half really was measured, so its numbers are still shown
    assert "1ms" in chip["detail"], chip["detail"]


def test_a_measured_cpu_still_renders_its_number_and_a_measured_window():
    """The negative half — a chip that always said "CPU not measured" would pass
    the test above. The window printed is the MEASURED span, not a count."""
    snap = _produced([1.0] * 50, cpu=[22.0] * 5)
    chip = health._load_chip(snap)
    assert chip["state"] == "normal"
    assert "CPU 22%" in chip["detail"], chip["detail"]
    assert "CPU not measured" not in chip["detail"]
    assert snap["cpu_window_s"] == pytest.approx(5.0)
    assert snap["cpu_readings"] == 5


def test_every_chip_state_has_a_pill_class_so_the_render_cannot_keyerror():
    for state in ("normal", "busy", "unknown"):
        assert state in health._LOAD_PILL
        assert state in health._LOAD_LABEL


def test_an_unrecognised_load_state_degrades_to_unknown_not_to_responsive():
    """Defensive, and in the safe direction: a future state name this render has
    never heard of must not fall through to the healthy pill."""
    chip = health._load_chip({"load_state": "melting", "measured": True})
    assert chip["state"] == "unknown"


def test_load_chip_does_not_disturb_the_existing_status_chip():
    v = health.health_view(load=_produced([900.0] * 50), **_OK)
    assert v["load_chip"]["state"] == "busy"
    # "busy" is not "broken": a loaded box is still a healthy install.
    assert v["status_chip"] == "ok"
    assert v["ok"] is True


# ── the refuted readings, at the chip ────────────────────────────────────────

def test_a_recovered_loop_cannot_cite_the_old_spike_as_evidence_it_is_fine():
    """REGRESSION — reproduced reading:
    ``RESPONSIVE :: UI lag p95 900ms · 200 stall(s)``.

    The state came from ~5s, the p95 printed beside it came from the ~2-minute
    ring, and the stall count was all-time. This is the assertion the old
    hand-built fixture could not make, because it set the state and the p95
    together and so could never express the disagreement.
    """
    snap = _produced([900.0] * 200 + [1.0] * 50)
    # precondition: the two windows genuinely disagree, or this proves nothing
    assert snap["ring_p95_ms"] > 800.0
    assert snap["recent_p95_ms"] == pytest.approx(1.0)

    detail = health._load_chip(snap)["detail"]
    assert health._load_chip(snap)["state"] == "normal"
    assert "900" not in detail, detail
    assert "1ms" in detail
    # the all-time figure may still appear — but ONLY behind a label that says
    # which window it is from, never as part of the justification clause.
    assert "Since start" in detail
    assert detail.index("200") > detail.index("Since start"), detail


def test_a_busy_chip_cannot_display_a_healthy_number_beside_its_warning():
    """REGRESSION — the inverse reading: ``UNDER LOAD :: UI lag p95 1ms``."""
    snap = _produced([1.0] * 1200 + [900.0] * 5)
    chip = health._load_chip(snap)
    assert chip["state"] == "busy"
    assert "900ms" in chip["detail"], chip["detail"]


def test_the_justification_clause_reads_only_recent_window_numbers():
    """The invariant, stated directly rather than via two examples: whatever the
    chip prints before "Since start" is computed from ``recent_*``.

    Driven so that the recent p95, the ring p95, the recent breach count and the
    session breach count are FOUR distinct numbers — a leak from any of the
    wrong three is then unmistakable rather than coincidentally equal.
    """
    snap = _produced([777.0] * 300 + [300.0] * 60, cpu=[11.0] * 4)
    # preconditions: the four figures really do differ
    assert snap["ring_p95_ms"] == pytest.approx(777.0)
    assert snap["recent_p95_ms"] == pytest.approx(300.0)
    assert 0 < snap["recent_breaches"] < snap["session_breaches"]

    chip = health._load_chip(snap)
    justification = chip["detail"].split("Since start")[0]
    assert "300ms" in justification
    assert "777" not in justification, justification
    assert f"{snap['recent_breaches']} stall(s)" in justification
    assert str(snap["session_breaches"]) not in justification, justification
    # ...and the window it covers is named in the text, not left to be guessed
    assert "Last 5s" in justification
    assert f"{snap['recent_samples']} samples" in justification


def test_a_stale_meter_renders_unknown_and_says_it_might_be_a_live_stall():
    """The chip must not silently swap one unknown for another: "never started"
    and "stopped reporting" are different facts and the operator is told which."""
    clock = _Clock()
    wd = LoopLagWatchdog(clock=clock, stale_after_s=2.0)
    for _ in range(30):
        wd.record(3.0)
    _feed_calm_cpu(wd)
    assert health._load_chip(wd.snapshot())["state"] == "normal"

    clock.advance(600.0)
    chip = health._load_chip(wd.snapshot())
    assert chip["state"] == "unknown"
    assert "stopped" in chip["detail"] or "blocked" in chip["detail"], chip["detail"]
    assert "3ms" not in chip["detail"], "a frozen ring leaked into the chip"


# ── wiring (not just the injected-override path) ─────────────────────────────

def test_health_view_reads_the_LIVE_watchdog_when_no_override_is_given():
    """Without this pin, ``load`` could be a parameter nothing in production
    ever populates — the dropped-argument class this repo keeps hitting: every
    override-driven test above would still pass while /health showed nothing.
    """
    wd = loop_lag.get_watchdog()
    for _ in range(30):
        wd.record(3.0)
    _feed_calm_cpu(wd)

    v = health.health_view(**_OK)           # NO load= override
    assert v["load"]["measured"] is True
    assert v["load"]["ring_samples"] == 30
    assert v["load_chip"]["state"] == "normal"


def test_a_fresh_install_with_no_watchdog_samples_reads_unknown():
    v = health.health_view(**_OK)           # watchdog reset, never started
    assert v["load"]["measured"] is False
    assert v["load_chip"]["state"] == "unknown"


# ── the render call site itself ──────────────────────────────────────────────

class _Node:
    """A chainable, context-manager-able stand-in for a nicegui element."""

    def __init__(self, rec, kind):
        self._rec, self._kind = rec, kind

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
    """Records every ``ui.<fn>(...)`` the page makes."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def texts(self, fn=None):
        return [str(a[0]) for n, a, _k in self.calls
                if a and (fn is None or n == fn)]


def _render(load_snap):
    """Execute the REAL ``build_health_page`` against a recording ui stub.

    No NiceGUI runtime is involved — ``build_health_page`` does ``from nicegui
    import ui`` at call time, so swapping the module in ``sys.modules`` is
    enough. This is what makes the four chip lines a covered call site rather
    than an AST-shaped assertion about them.
    """
    ui = _RecordingUI()
    fake = types.ModuleType("nicegui")
    fake.ui = ui
    real_view = health.health_view

    def _view(**kw):
        return real_view(load=load_snap, **_OK)

    with mock.patch.dict(sys.modules, {"nicegui": fake}), \
            mock.patch.object(health, "health_view", _view):
        health.build_health_page()
    return ui


def test_build_health_page_actually_puts_the_chip_on_the_page():
    """MUTATION TARGET. Replacing the four render lines with ``pass`` left all
    45 tests of the first cut green, because the page's only coverage was
    ``assert callable(build_health_page)`` — structural emptiness as data."""
    snap = _produced([340.0] * 50, cpu=[91.0] * 5)
    ui = _render(snap)
    html = " ".join(ui.texts("html"))
    assert "UNDER LOAD" in html, html
    assert health._LOAD_PILL["busy"] in html, html
    # ...and the explanation, which is the whole point of UX-9(f)
    detail = health._load_chip(snap)["detail"]
    assert detail in ui.texts("label"), ui.texts("label")


def test_the_page_renders_the_unknown_chip_too_not_only_the_happy_one():
    ui = _render(_produced([]))
    html = " ".join(ui.texts("html"))
    assert "LOAD NOT MEASURED" in html, html
    assert health._LOAD_PILL["unknown"] in html, html


def test_the_status_chip_still_renders_beside_it():
    """Guards the mutation where the load chip REPLACES the health chip rather
    than joining it — both must be on the page, on their own axes."""
    ui = _render(_produced([1.0] * 50))
    html = " ".join(ui.texts("html"))
    assert "HEALTHY" in html and "RESPONSIVE" in html, html


def test_the_render_reads_LOAD_PILL_and_load_chip_on_the_AST():
    """A second, independent pin on the same call site.

    The stub render above proves the chip reaches the page for the inputs it was
    given; this proves the *code path* exists at all, so a future conditional
    that happens to skip the chip for the stub's inputs cannot pass silently.
    Asserted on real ``Subscript``/``Name`` nodes — a substring search would also
    match the name appearing in the comment directly above those lines, and this
    repo has shipped a pin that passed on the strength of a comment.
    """
    tree = ast.parse(inspect.getsource(health.build_health_page))
    pill_reads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
        and n.value.id == "_LOAD_PILL"
    ]
    assert pill_reads, "nothing in build_health_page reads _LOAD_PILL"
    chip_reads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
        and n.slice.value == "load_chip"
    ]
    assert chip_reads, "nothing in build_health_page reads view['load_chip']"
