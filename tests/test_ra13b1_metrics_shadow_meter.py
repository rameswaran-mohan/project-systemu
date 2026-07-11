"""R-A13b-1 — metrics_store per-effect-class SHADOW park-surface counters.

The cross-run aggregation the meter writes at the credit seam: a single pinnable
writer ``incr_s4_shadow_meter(effect_class, would_credit=...)`` that records, per
effect-class, ``{would_stamp, would_credit, would_park}``; ``shadow_meter_snapshot``
reads it back. Atomic + defensive like the rest of the store.
"""
from __future__ import annotations


def _store(tmp_path):
    from systemu.runtime.metrics_store import MetricsStore
    return MetricsStore(tmp_path / "metrics")


def test_would_credit_increments_stamp_and_credit(tmp_path):
    s = _store(tmp_path)
    s.incr_s4_shadow_meter("net_mutate", would_credit=True)
    snap = s.shadow_meter_snapshot()
    assert snap["net_mutate"] == {"would_stamp": 1, "would_credit": 1, "would_park": 0}


def test_would_park_increments_stamp_and_park(tmp_path):
    s = _store(tmp_path)
    s.incr_s4_shadow_meter("money_move", would_credit=False)
    snap = s.shadow_meter_snapshot()
    assert snap["money_move"] == {"would_stamp": 1, "would_credit": 0, "would_park": 1}


def test_per_effect_class_and_accumulation(tmp_path):
    s = _store(tmp_path)
    s.incr_s4_shadow_meter("net_mutate", would_credit=True)
    s.incr_s4_shadow_meter("net_mutate", would_credit=False)
    s.incr_s4_shadow_meter("send_message", would_credit=True)
    snap = s.shadow_meter_snapshot()
    assert snap["net_mutate"] == {"would_stamp": 2, "would_credit": 1, "would_park": 1}
    assert snap["send_message"] == {"would_stamp": 1, "would_credit": 1, "would_park": 0}


def test_none_effect_class_buckets_as_unknown(tmp_path):
    s = _store(tmp_path)
    s.incr_s4_shadow_meter(None, would_credit=False)
    snap = s.shadow_meter_snapshot()
    assert snap["unknown"] == {"would_stamp": 1, "would_credit": 0, "would_park": 1}


def test_meter_counters_persist_across_instances(tmp_path):
    _store(tmp_path).incr_s4_shadow_meter("oauth_call", would_credit=True)
    # a fresh store over the SAME dir reads the persisted bucket (atomic round-trip)
    snap = _store(tmp_path).shadow_meter_snapshot()
    assert snap["oauth_call"] == {"would_stamp": 1, "would_credit": 1, "would_park": 0}


def test_meter_does_not_disturb_the_fatigue_counters(tmp_path):
    s = _store(tmp_path)
    s.incr("gate_cards_created")
    s.incr_s4_shadow_meter("net_mutate", would_credit=True)
    # the shadow bucket lives alongside the fatigue snapshot, not on top of it
    assert s.snapshot()["gate_cards_created"] == 1
    assert s.shadow_meter_snapshot()["net_mutate"]["would_stamp"] == 1


def test_empty_snapshot_is_a_dict(tmp_path):
    assert _store(tmp_path).shadow_meter_snapshot() == {}
