"""PLAN-11 / DEC-11 -- the approval fatigue the system records is SHOWN, and its
latency series is BOUNDED.

Two defects closed here:

* ``metrics_store.record_resolution`` appended to ``resolution_latency_ms`` on
  every resolution, forever. The series now keeps the last
  ``LATENCY_WINDOW`` samples and carries a running count / sum / min / max, so
  the long-run aggregates stay EXACT after the window rotates.
* ``MetricsStore.snapshot()`` had no reader outside tests: the counters were
  written on every gate decision and shown nowhere. ``/health`` now has an
  "Approvals" section, rendered from a PUBLISHED render snapshot (the page never
  reads the file on the event loop), where every measure whose denominator is 0
  reads NOT MEASURED -- never "0%".
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest

from systemu.interface.pages import health
from systemu.runtime import metrics_store as ms
from systemu.runtime.metrics_store import MetricsStore


# -- A. the bounded series keeps exact long-run aggregates -------------------

def test_the_ruled_window_is_500_samples():
    assert ms.LATENCY_WINDOW == 500


def test_the_latency_series_is_bounded_and_the_aggregates_stay_exact(tmp_path,
                                                                     monkeypatch):
    # A small window keeps this fast (every resolution rewrites the file); the
    # writer reads the module constant at call time, so this is the real path.
    monkeypatch.setattr(ms, "LATENCY_WINDOW", 25)
    store = MetricsStore(tmp_path)
    n = ms.LATENCY_WINDOW + 23
    # The extremes sit at the very START, so they are rotated OUT of the window
    # long before the end -- only an exact running min/max can still name them.
    lat = [1.0, 99_000.0] + [float(100 + i) for i in range(n - 2)]
    for i, v in enumerate(lat):
        store.record_resolution(v, ts=10.0 * i)

    snap = store.snapshot()
    assert len(snap["resolution_latency_ms"]) == ms.LATENCY_WINDOW
    assert snap["resolution_latency_ms"] == lat[-ms.LATENCY_WINDOW:]
    assert snap["resolution_latency_count"] == n
    assert snap["resolution_latency_sum_ms"] == pytest.approx(sum(lat))
    assert snap["resolution_latency_min_ms"] == 1.0
    assert snap["resolution_latency_max_ms"] == 99_000.0
    assert snap["gate_cards_resolved"] == n

    # ...and the bound is on DISK, not just in the snapshot view.
    raw = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert len(raw["resolution_latency_ms"]) == ms.LATENCY_WINDOW


def test_a_legacy_unbounded_file_seeds_exact_aggregates_then_rotates(tmp_path):
    """A store written before the bound holds EVERY sample ever taken, so its
    aggregates are exactly derivable once -- and must be, before the rotation
    throws the oldest samples away."""
    legacy = [float(i) for i in range(1, 801)]          # 800 samples, no aggregates
    (tmp_path / "metrics.json").write_text(json.dumps({
        "gate_cards_resolved": 800, "resolution_latency_ms": legacy,
    }), encoding="utf-8")

    snap = MetricsStore(tmp_path).snapshot()
    assert snap["resolution_latency_count"] == 800
    assert snap["resolution_latency_sum_ms"] == pytest.approx(sum(legacy))
    assert snap["resolution_latency_min_ms"] == 1.0
    assert snap["resolution_latency_max_ms"] == 800.0
    assert snap["resolution_latency_ms"] == legacy[-ms.LATENCY_WINDOW:]

    MetricsStore(tmp_path).record_resolution(5000.0, ts=1.0)
    snap = MetricsStore(tmp_path).snapshot()
    assert snap["resolution_latency_count"] == 801
    assert snap["resolution_latency_sum_ms"] == pytest.approx(sum(legacy) + 5000.0)
    assert snap["resolution_latency_max_ms"] == 5000.0
    raw = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert len(raw["resolution_latency_ms"]) == ms.LATENCY_WINDOW


def test_a_non_numeric_sample_never_poisons_the_aggregates(tmp_path):
    (tmp_path / "metrics.json").write_text(json.dumps({
        "resolution_latency_ms": [10.0, "x", None, True, 30.0],
    }), encoding="utf-8")
    snap = MetricsStore(tmp_path).snapshot()
    assert snap["resolution_latency_ms"] == [10.0, 30.0]
    assert snap["resolution_latency_count"] == 2
    assert snap["resolution_latency_sum_ms"] == pytest.approx(40.0)


def test_approvals_under_the_fast_threshold_are_counted_and_denies_are_not(tmp_path):
    store = MetricsStore(tmp_path)
    fast_ms = ms.FAST_APPROVAL_S * 1000.0 - 1.0
    slow_ms = ms.FAST_APPROVAL_S * 1000.0 + 1.0
    store.record_resolution(fast_ms, ts=100.0, choice="Approve once")
    store.record_resolution(fast_ms, ts=200.0, choice="Always allow")
    store.record_resolution(slow_ms, ts=300.0, choice="Approve")
    store.record_resolution(fast_ms, ts=400.0, choice="Deny")     # not an approval
    snap = store.snapshot()
    assert snap["approvals_timed"] == 3
    assert snap["approvals_unread"] == 2


def test_concurrent_resolutions_lose_no_update(tmp_path):
    """The rotation is a read-modify-write of the whole file. Unlocked, two
    threads that load the same state each write back their own +1 and one is
    lost. The store's lock makes the count exact."""
    store_dir = tmp_path / "m"
    errors = []

    def worker(k):
        try:
            for i in range(40):
                MetricsStore(store_dir).record_resolution(
                    float(i), ts=1000.0 * k + 10.0 * i, choice="Approve once")
        except Exception as exc:          # pragma: no cover - surfaced below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    snap = MetricsStore(store_dir).snapshot()
    assert snap["gate_cards_resolved"] == 240
    assert snap["resolution_latency_count"] == 240
    assert snap["approvals_timed"] == 240


# -- B. the /health "Approvals" section, as render data ----------------------

def _metrics(**over):
    snap = dict(ms.empty_snapshot())
    snap.update(over)
    return snap


def _row(view, starts):
    hits = [v for (label, v) in view["rows"] if label.startswith(starts)]
    assert len(hits) == 1, (starts, view["rows"])
    return hits[0]


def _assert_ascii(view):
    for label, value in view["rows"]:
        assert label.isascii() and value.isascii(), (label, value)
    for note in view["notes"]:
        assert note.isascii(), note


def test_the_section_renders_every_measure_from_a_fake_snapshot():
    window = [1000.0, 2000.0, 3000.0, 4000.0, 5000.0,
              6000.0, 7000.0, 8000.0, 9000.0, 10000.0]
    view = health.approvals_view(_metrics(
        gate_cards_resolved=12,
        resolution_latency_ms=window,
        resolution_latency_count=12,
        resolution_latency_sum_ms=60000.0,
        resolution_latency_min_ms=500.0,
        resolution_latency_max_ms=10000.0,
        approvals_timed=8, approvals_unread=2,
        always_allow_grants=3, bulk_approve_events=4,
    ))
    assert view["measured"] is True
    assert _row(view, "Gate cards resolved") == "12"
    assert _row(view, "Median time to decision").startswith("5.5s")
    assert "last 10 decisions" in _row(view, "Median time to decision")
    assert _row(view, "90th percentile time to decision").startswith("9.0s")
    assert _row(view, "Approved without reading").startswith("25% (2 of 8 approvals)")
    assert _row(view, "Always allow grants") == "3"
    # "decisions", not "approvals": the detector counts ANY two resolutions
    # under 2s apart, denies included -- the label must not overstate it.
    assert _row(view, "Bulk decisions") == "4"
    assert "12 decisions" in _row(view, "All-time time to decision")
    assert "avg 5.0s" in _row(view, "All-time time to decision")     # 60000 / 12
    # N is stated on the page, not only in a constant.
    assert any("under 3s" in label for (label, _v) in view["rows"]
               if label.startswith("Approved without reading"))
    _assert_ascii(view)


def test_an_empty_store_renders_NOT_MEASURED_never_zero_percent(tmp_path):
    view = health.approvals_view(MetricsStore(tmp_path).snapshot())
    assert view["rows"], "the section must still list what it measures"
    for label, value in view["rows"]:
        assert value.startswith("NOT MEASURED"), (label, value)
        assert "0%" not in value
    _assert_ascii(view)


def test_a_rate_with_a_zero_denominator_is_NOT_MEASURED_even_when_others_are():
    """Only denies so far: cards were resolved and timed, but there is no
    approval to take a share OF."""
    view = health.approvals_view(_metrics(
        gate_cards_resolved=2, resolution_latency_ms=[4000.0, 6000.0],
        resolution_latency_count=2, resolution_latency_sum_ms=10000.0,
        resolution_latency_min_ms=4000.0, resolution_latency_max_ms=6000.0,
        denies=2, approvals_timed=0, approvals_unread=0,
    ))
    assert _row(view, "Median time to decision").startswith("5.0s")
    share = _row(view, "Approved without reading")
    assert share.startswith("NOT MEASURED") and "0%" not in share
    assert _row(view, "Always allow grants") == "0"   # a measured zero IS a zero


def test_an_unread_snapshot_says_so_rather_than_inventing_a_number():
    view = health.approvals_view(None, unread_reason="not read yet")
    assert view["measured"] is False
    assert any("not read yet" in n for n in view["notes"])
    for _label, value in view["rows"]:
        assert value.startswith("NOT MEASURED")


# -- C. reachability: the /health builder consumes the PUBLISHED snapshot ----

class _Node:
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

    def clear(self):
        return self


class _RecordingUI:
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


@pytest.fixture
def _published(monkeypatch, tmp_path):
    """A fake open vault + a way to PUBLISH an approval-metrics snapshot for it,
    with the refresher neutralised so no background producer can overwrite it."""
    from systemu.interface.dashboard_state import AppState
    from systemu.runtime import render_snapshots as rs

    vault = types.SimpleNamespace(root=str(tmp_path))
    monkeypatch.setattr(AppState, "_instance",
                        types.SimpleNamespace(vault=vault), raising=False)
    monkeypatch.setattr(rs, "_producers", {})
    monkeypatch.setattr(rs, "start_refresher", lambda *a, **k: False)
    monkeypatch.setattr(rs, "request_refresh", lambda: False)
    rs.clear(ms.APPROVAL_METRICS_SNAPSHOT)

    def _publish(metrics):
        rs.publish(ms.APPROVAL_METRICS_SNAPSHOT, ms.ApprovalMetricsSnapshot(
            taken_at=time.monotonic(), interval_s=30.0,
            store_dir=str(Path(tmp_path) / "metrics"), metrics=metrics))

    yield _publish
    rs.clear(ms.APPROVAL_METRICS_SNAPSHOT)


def _render_page():
    ui = _RecordingUI()
    fake = types.ModuleType("nicegui")
    fake.ui = ui
    canned = health.health_view(provider_configured=True, provider_reachable=True,
                                keyring_locked=False, daemon_running=True)
    with mock.patch.dict(sys.modules, {"nicegui": fake}), \
            mock.patch.object(health, "health_view", lambda **kw: canned):
        health.build_health_page()
    return ui


def test_build_health_page_renders_the_published_approval_numbers(_published, monkeypatch):
    """MUTATION TARGET: delete the Approvals call in ``build_health_page`` and
    this goes red. The numbers are distinctive so they can only have come from
    the published snapshot -- and the store's file reader is booby-trapped, so
    they cannot have come from a render-time read of the file either."""
    _published(_metrics(
        gate_cards_resolved=4321, resolution_latency_ms=[2000.0, 4000.0],
        resolution_latency_count=4321, resolution_latency_sum_ms=4321 * 3000.0,
        resolution_latency_min_ms=10.0, resolution_latency_max_ms=4000.0,
        approvals_timed=40, approvals_unread=10,
        always_allow_grants=777, bulk_approve_events=55,
    ))

    def _boom(self):
        raise AssertionError("the /health render read metrics.json on the loop")

    monkeypatch.setattr(MetricsStore, "_load", _boom)
    labels = _render_page().texts("label")
    assert "Approvals" in labels
    assert "4321" in labels
    assert "777" in labels
    assert "55" in labels
    assert any(t.startswith("25% (10 of 40 approvals)") for t in labels), labels


def test_build_health_page_without_a_snapshot_says_NOT_MEASURED(_published):
    labels = _render_page().texts("label")
    assert "Approvals" in labels
    assert any(t.startswith("NOT MEASURED") for t in labels), labels
    assert not any(t.endswith("%") for t in labels)


def test_a_snapshot_for_a_different_vault_is_not_shown(_published, tmp_path):
    """The snapshot slot is process-wide; a dashboard on another vault must not
    render the first vault's numbers as its own."""
    from systemu.runtime import render_snapshots as rs
    rs.publish(ms.APPROVAL_METRICS_SNAPSHOT, ms.ApprovalMetricsSnapshot(
        taken_at=time.monotonic(), interval_s=30.0,
        store_dir=str(tmp_path / "some-other-vault" / "metrics"),
        metrics=_metrics(gate_cards_resolved=9999, always_allow_grants=8888)))
    labels = _render_page().texts("label")
    assert "8888" not in labels and "9999" not in labels


def test_the_render_registers_the_producer_for_this_vault(_published, tmp_path):
    from systemu.runtime import render_snapshots as rs
    _render_page()
    assert ms.APPROVAL_METRICS_SNAPSHOT in rs._producers
    # ...and the producer, run OFF the render path, publishes what is on disk.
    MetricsStore(tmp_path / "metrics").record_resolution(1500.0, ts=1.0,
                                                         choice="Approve once")
    rs._producers[ms.APPROVAL_METRICS_SNAPSHOT]()
    snap = rs.current(ms.APPROVAL_METRICS_SNAPSHOT)
    assert type(snap) is ms.ApprovalMetricsSnapshot
    assert snap.metrics["gate_cards_resolved"] == 1
    assert snap.store_dir == str(Path(tmp_path) / "metrics")


def test_the_builder_calls_the_approvals_section_on_the_AST():
    """Second, independent pin on the call site (a future conditional that skips
    the section for the stub's inputs cannot pass silently)."""
    tree = ast.parse(inspect.getsource(health.build_health_page))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_paint_approvals"]
    assert calls, "build_health_page no longer paints the Approvals section"
