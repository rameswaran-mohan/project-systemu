"""D-3 (e2e 0.10.31): the Ctrl+K palette rebuilt its whole index on every page
render.

THE WITNESS
-----------
Profiled ``_build_layout("Home", "/")`` three times against a 40-tool vault,
psutil TTL pinned warm so the daemon scan is not in frame::

    3  0.002  0.931  interface/dashboard.py:327(_build_layout)
    3  0.000  0.861  components/command_palette.py:360(build_command_palette)
    3  0.000  0.858  components/command_palette.py:276(build_index)
    3  0.005  0.438  runtime/capability_index.py:263(derive_index)
    3  0.001  0.393  runtime/table_reconciler.py:163(project)

286 ms of the event loop on EVERY route, for an overlay the operator has not
opened. Measured per source: Tools 46-60 ms, Table 56-57 ms, and the whole
``build_index`` 118-147 ms even after the availability probe was memoised.
Unlike the daemon scan it had no memo at all -- every paint paid it.

THE PROPERTY
------------
    THE RENDER MATCHES AGAINST A PUBLISHED INDEX; IT NEVER BUILDS ONE.

    ``build_index`` is unchanged and stays THE builder -- it just runs on the
    shared refresher thread (``render_snapshots``) and publishes a
    ``PaletteIndexSnapshot``. The mounted overlay reads that snapshot at MATCH
    time, not at mount time, which is strictly fresher than what it did before:
    the palette is opened long after the paint that mounted it.

    With no snapshot the palette offers the STATIC navigation rows and nudges a
    refresh. That is a smaller palette, never a wrong one: this side cannot
    invent a tool, and the rows it does show are the ones that were never
    vault-derived in the first place.

Both projections are read-only (``derive_index`` and ``table_reconciler.project``
are documented deterministic + idempotent and load only), so running them on the
refresher thread races nothing.
"""
from __future__ import annotations

import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from systemu.interface.components import command_palette as cp
from systemu.runtime import render_snapshots as rs


@pytest.fixture()
def quiet_snapshots():
    rs.stop_refresher()
    rs.clear()
    yield
    rs.stop_refresher()
    rs.clear()
    rs._producers.pop(rs.PALETTE_INDEX, None)


@pytest.fixture()
def no_index_build(monkeypatch):
    """``build_index`` COUNTS and RAISES. ``build_command_palette`` swallows a
    failed build, so counting the ATTEMPT is the only assertion the defect
    cannot hide from."""
    touched = []

    def _trip(*a, **k):
        touched.append(a)
        raise RuntimeError("the render path rebuilt the palette index")

    monkeypatch.setattr(cp, "build_index", _trip)
    return touched


# ── a recording NiceGUI ─────────────────────────────────────────────────────

class _Node:
    def __init__(self, rec=None, kind=""):
        self._rec = rec
        self.kind = kind
        self.value = ""

    def __getattr__(self, name):
        def _m(*a, **k):
            return self
        return _m

    def on(self, *a, **k):
        return self

    def clear(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingUI:
    def __init__(self):
        self.calls = []
        self.navigate = SimpleNamespace(to=lambda *a, **k: None)

    def timer(self, _delay, callback=None, **k):
        return _Node(self)

    def refreshable(self, fn):
        return fn

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def button_labels(self):
        return [str(a[0]) for n, a, _k in self.calls if n == "button" and a]


def _mount(monkeypatch, vault) -> _RecordingUI:
    """Mount the REAL overlay against a recording ``ui``."""
    rec = _RecordingUI()
    rec.started = []
    monkeypatch.setattr(rs, "start_refresher",
                        lambda *a, **k: rec.started.append(1) or True)
    fake = ModuleType("nicegui")
    fake.ui = rec
    monkeypatch.setitem(sys.modules, "nicegui", fake)
    cp.build_command_palette(vault)
    return rec


class _Vault:
    """A vault whose every read is a tripwire: the render must not touch it."""

    def __init__(self, touched):
        self._touched = touched

    def __getattr__(self, name):
        def _m(*a, **k):
            self._touched.append(name)
            raise RuntimeError("the render path read the vault")
        return _m


def _entry(label, group="Tools"):
    return cp.PaletteEntry(group=group, label=label, intent=cp.NAVIGATE,
                           target="/tools", detail="")


def _publish(entries, *, age_s=0.0, interval_s=30.0):
    rs.publish(rs.PALETTE_INDEX, rs.PaletteIndexSnapshot(
        taken_at=time.monotonic() - age_s, interval_s=interval_s,
        entries=tuple(entries)))


# ── (a) THE REACHABILITY PIN ────────────────────────────────────────────────

def test_mounting_the_palette_never_builds_the_index(monkeypatch,
                                                     no_index_build,
                                                     quiet_snapshots):
    """THE REPRO. Before the fix this is 1 call, 286 ms, on every route."""
    _mount(monkeypatch, _Vault([]))
    assert no_index_build == [], (
        "build_command_palette rebuilt the index on the render path: "
        + repr(len(no_index_build)) + " calls")


def test_mounting_the_palette_never_reads_the_vault(monkeypatch,
                                                    quiet_snapshots):
    """The stronger form: not just 'not via build_index'. Every vault read is
    a tripwire, so a second path to the same cost cannot slip in later."""
    touched = []
    _mount(monkeypatch, _Vault(touched))
    assert touched == [], "the render path read the vault: " + repr(touched)


def test_matching_never_builds_the_index(monkeypatch, no_index_build,
                                         quiet_snapshots):
    """Typing in the palette is a per-keystroke handler. It must read the
    published entries, not rebuild them."""
    _publish([_entry("web_act")])
    assert [e.label for e in cp.palette_entries() if e.group == "Tools"] == \
        ["web_act"]
    assert no_index_build == []


# ── (b) with no snapshot the palette SHRINKS, it does not invent ───────────

def test_no_snapshot_leaves_the_static_navigation_rows(quiet_snapshots):
    rs.clear(rs.PALETTE_INDEX)
    labels = [e.label for e in cp.palette_entries()]
    static = [e.label for e in cp._static_entries()]
    assert labels == static, labels
    assert static, "the static rows are the floor; there must be some"


def test_no_snapshot_invents_no_tool(quiet_snapshots):
    rs.clear(rs.PALETTE_INDEX)
    assert [e for e in cp.palette_entries() if e.group == "Tools"] == []


def test_the_render_nudges_the_build_it_declined_to_run(monkeypatch,
                                                        quiet_snapshots):
    rs.clear(rs.PALETTE_INDEX)
    nudges = []
    monkeypatch.setattr(rs, "request_refresh",
                        lambda *a, **k: nudges.append(1) or False)
    cp.palette_entries()
    assert nudges, "the palette found no index and nudged nobody"


def test_mounting_starts_the_refresher_that_will_build(monkeypatch,
                                                       quiet_snapshots):
    rec = _mount(monkeypatch, _Vault([]))
    assert rec.started, "the mount never started the refresher"


# ── (c) the published snapshot IS what the palette matches ─────────────────

def test_a_published_index_is_what_the_palette_offers(quiet_snapshots):
    _publish([_entry("web_act"), _entry("read_file")])
    labels = [e.label for e in cp.palette_entries()]
    assert "web_act" in labels and "read_file" in labels, labels


def test_the_mounted_overlay_paints_the_published_rows(monkeypatch,
                                                       no_index_build,
                                                       quiet_snapshots):
    """End to end through the real renderer: a published entry reaches a row."""
    _publish([_entry("web_act")])
    rec = _mount(monkeypatch, _Vault([]))
    assert any("web_act" in label for label in rec.button_labels()), \
        rec.button_labels()


def test_a_stale_index_is_still_offered_and_nudged(monkeypatch,
                                                   quiet_snapshots):
    nudges = []
    monkeypatch.setattr(rs, "request_refresh",
                        lambda *a, **k: nudges.append(1) or False)
    _publish([_entry("web_act")], age_s=45.0, interval_s=30.0)
    assert "web_act" in [e.label for e in cp.palette_entries()]
    assert nudges, "a stale index must ask to be refreshed"


def test_an_index_older_than_the_replay_bound_is_withheld(quiet_snapshots):
    _publish([_entry("web_act")], age_s=rs.MAX_REPLAY_AGE_S + 1.0)
    assert "web_act" not in [e.label for e in cp.palette_entries()]


# ── (d) the producer still builds the same index, on the refresher ─────────

def test_the_registered_producer_publishes_what_build_index_returns(
        monkeypatch, quiet_snapshots):
    built = [_entry("forged_thing")]
    monkeypatch.setattr(cp, "build_index", lambda v: list(built))
    _mount(monkeypatch, _Vault([]))
    assert rs.palette_index() is None, "the mount must not have built anything"
    assert rs.refresh_now(rs.PALETTE_INDEX) == 1
    snap = rs.palette_index()
    assert snap is not None
    assert [e.label for e in snap.entries] == ["forged_thing"], snap


def test_the_producer_is_registered_against_the_vault_it_was_handed(
        monkeypatch, quiet_snapshots):
    seen = []
    monkeypatch.setattr(cp, "build_index", lambda v: seen.append(v) or [])
    sentinel = _Vault([])
    _mount(monkeypatch, sentinel)
    rs.refresh_now(rs.PALETTE_INDEX)
    assert seen == [sentinel], seen


def test_a_build_that_blows_up_leaves_the_static_rows_standing(
        monkeypatch, quiet_snapshots):
    """Fail CLOSED and VISIBLY: a projection that cannot be built must not
    leave a previous index standing as if it had just been rebuilt, and must
    not blank the palette either."""
    def _boom(_v):
        raise RuntimeError("vault unreadable")

    monkeypatch.setattr(cp, "build_index", _boom)
    _mount(monkeypatch, _Vault([]))
    rs.refresh_now(rs.PALETTE_INDEX)
    assert rs.palette_index() is None
    assert [e.label for e in cp.palette_entries()] == \
        [e.label for e in cp._static_entries()]
