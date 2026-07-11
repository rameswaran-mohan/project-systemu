"""R-A13b-2iii — the pure Stage-3 ARM-GATE verdict function.

`s4_shadow_arm_verdict(snapshot, *, min_runs) -> (ready, reasons)` reads the existing
`s4_shadow` park-surface bucket and decides whether the external-verification net is
ready to ARM (flip ENFORCE). The criterion is NOT `would_park==0` (that never passes on
genuinely-unverifiable effects and falsely passes when nothing stamped). It is:

  1. COVERAGE      — Σ would_stamp >= min_runs
  2. NO BENIGN     — no class outside the DEC-24 stamp set (and not "unknown") stamps
     OVER-STAMP      (a benign class stamping ⇒ effect_tags mis-wired ⇒ spurious park)
  3. LIVE CHANNEL  — every STAMPED class (stamp set ∪ {"unknown"}) with would_stamp>0
                     also has would_credit>0 (a stamped class that only parks ⇒ dead
                     evidence channel ⇒ spurious park)

Pure function, never raises, defensive on malformed cells.
"""
from __future__ import annotations

from systemu.runtime.s4_activation import s4_shadow_arm_verdict


def _cell(stamp, credit, park):
    return {"would_stamp": stamp, "would_credit": credit, "would_park": park}


def test_empty_snapshot_is_not_ready_insufficient_data():
    ready, reasons = s4_shadow_arm_verdict({}, min_runs=20)
    assert ready is False
    assert any("insufficient data" in r for r in reasons)
    assert any("/20" in r for r in reasons)


def test_benign_class_stamping_is_over_stamp_not_ready():
    # net_read is a benign class — it must NEVER stamp. This is TODAY's failure shape
    # when effect_tags leak a non-stamp tag onto the meter. Coverage is met so the ONLY
    # failing check is the benign over-stamp.
    snap = {"net_read": _cell(25, 25, 0)}
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=20)
    assert ready is False
    assert any("benign class net_read still stamping" in r for r in reasons)
    # NOT flagged as insufficient (coverage is met)
    assert not any("insufficient data" in r for r in reasons)


def test_stamped_class_with_zero_credit_is_dead_channel_not_ready():
    # money_move IS in the stamp set; it stamped 25× but never credited ⇒ dead channel.
    snap = {"money_move": _cell(25, 0, 25)}
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=20)
    assert ready is False
    assert any("stamped class money_move has 0 would_credit" in r for r in reasons)
    assert any("dead channel" in r for r in reasons)


def test_ready_shape_all_checks_pass():
    # coverage met (30 >= 20); the only class that stamps is a stamp-set class with a
    # live credit channel; a benign class is PRESENT but never stamps (would_stamp 0).
    snap = {
        "money_move": _cell(20, 15, 5),
        "send_message": _cell(10, 8, 2),
        "net_read": _cell(0, 0, 0),
    }
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=20)
    assert ready is True
    assert reasons == []


def test_unknown_is_stamped_until_classified_not_a_benign_violation():
    # "unknown" stamps AND credits — it is stamped-until-classified, so it is NOT a
    # benign-over-stamp violation and it passes the live-channel check ⇒ READY.
    snap = {"unknown": _cell(25, 10, 15)}
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=20)
    assert ready is True
    assert reasons == []
    assert not any("benign" in r for r in reasons)


def test_unknown_with_zero_credit_is_a_dead_channel():
    # "unknown" that only parks is the honest TODAY state (no tool emits evidence) ⇒
    # dead channel, NOT a benign over-stamp.
    snap = {"unknown": _cell(30, 0, 30)}
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=20)
    assert ready is False
    assert any("stamped class unknown has 0 would_credit" in r for r in reasons)
    assert not any("benign" in r for r in reasons)


def test_malformed_cells_never_raise():
    snap = {
        "money_move": "not a dict",
        "net_read": {"would_stamp": None},
        "unknown": {},
    }
    # must not raise; returns a bool + list
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=5)
    assert isinstance(ready, bool)
    assert isinstance(reasons, list)


def test_reasons_are_deterministic_and_ordered():
    # coverage fails AND two benign classes over-stamp AND a stamped class is dead:
    # order = coverage, benign(sorted), dead(sorted).
    snap = {
        "net_read": _cell(1, 0, 1),
        "local_write": _cell(1, 0, 1),
        "oauth_call": _cell(1, 0, 1),
    }
    ready, reasons = s4_shadow_arm_verdict(snap, min_runs=100)
    assert ready is False
    assert reasons[0].startswith("insufficient data")
    # benign classes sorted alphabetically: local_write before net_read
    benign = [r for r in reasons if "benign class" in r]
    assert benign == [
        "benign class local_write still stamping (1)",
        "benign class net_read still stamping (1)",
    ]
