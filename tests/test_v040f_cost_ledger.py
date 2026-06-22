"""Tests for v0.4.0-f cost governance.

Covers SupervisorCostLedger:
  * Fresh ledger allows spending up to the per-hour cap.
  * Breaching the per-hour cap trips the hour kill switch.
  * Breaching the per-day cap trips the day kill switch.
  * State persists across reload (process restart).
  * operator reset_kill_switch clears the day disable.
  * Hour rollover clears the hour kill switch automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from systemu.runtime.supervisor_cost_ledger import SupervisorCostLedger


@pytest.fixture
def ledger(tmp_path):
    return SupervisorCostLedger(
        path=tmp_path / "cost.json",
        max_per_hour_usd=1.0,
        max_per_day_usd=5.0,
    )


class TestSpendAndRecord:
    def test_fresh_ledger_can_spend(self, ledger):
        assert ledger.can_spend(0.5) is True

    def test_below_cap_records_ok(self, ledger):
        ledger.record(0.25)
        ledger.record(0.25)
        snap = ledger.snapshot()
        assert snap["hour_spent_usd"] == 0.5
        assert snap["day_spent_usd"] == 0.5
        assert snap["hour_disabled_until"] == ""

    def test_hour_cap_breach_trips_kill_switch(self, ledger):
        ledger.record(0.6)
        ledger.record(0.5)   # total 1.1 > 1.0
        assert ledger.snapshot()["hour_disabled_until"] != ""
        # Future calls denied
        assert ledger.can_spend(0.01) is False

    def test_day_cap_breach_trips_day_disable(self, tmp_path):
        # Set hour cap above the day cap so we exercise the day path alone
        led = SupervisorCostLedger(
            path=tmp_path / "cost.json",
            max_per_hour_usd=100.0,
            max_per_day_usd=2.0,
        )
        led.record(1.0)
        led.record(1.5)   # total 2.5 > 2.0
        snap = led.snapshot()
        assert snap["day_disabled_until"] != ""
        assert led.can_spend(0.01) is False


class TestPersistence:
    def test_state_round_trips_across_instances(self, tmp_path):
        path = tmp_path / "cost.json"
        a = SupervisorCostLedger(path=path, max_per_hour_usd=1.0, max_per_day_usd=5.0)
        a.record(0.7)
        a.record(0.5)   # trips hour
        # Re-load: same on-disk state
        b = SupervisorCostLedger(path=path, max_per_hour_usd=1.0, max_per_day_usd=5.0)
        snap = b.snapshot()
        assert snap["hour_disabled_until"] != ""
        assert b.can_spend(0.01) is False


class TestOperatorReset:
    def test_reset_clears_day_disable(self, tmp_path):
        led = SupervisorCostLedger(
            path=tmp_path / "cost.json",
            max_per_hour_usd=100.0,
            max_per_day_usd=2.0,
        )
        led.record(3.0)
        assert led.can_spend(0.01) is False
        led.reset_kill_switch()
        assert led.can_spend(0.01) is True


class TestRollover:
    def test_disabled_caps_zero_skips_checks(self, tmp_path):
        led = SupervisorCostLedger(
            path=tmp_path / "cost.json",
            max_per_hour_usd=0,
            max_per_day_usd=0,
        )
        for _ in range(50):
            led.record(100.0)
        assert led.can_spend(1000.0) is True
        assert led.snapshot()["hour_disabled_until"] == ""

    def test_hour_rollover_clears_hour_trip(self, tmp_path, monkeypatch):
        """When the hour bucket rolls over, the hour disable should clear
        as long as the disabled-until timestamp is in the past."""
        from systemu.runtime import supervisor_cost_ledger as scl

        # Trip the hour with a frozen 'now' a long time ago.
        past = datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(scl, "_now", lambda: past)
        led = SupervisorCostLedger(
            path=tmp_path / "cost.json",
            max_per_hour_usd=1.0, max_per_day_usd=100.0,
        )
        led.record(1.5)   # trips
        assert led.snapshot()["hour_disabled_until"] != ""

        # Jump forward many hours; the bucket rolls over AND the past
        # disabled-until elapses.
        future = past + timedelta(hours=3)
        monkeypatch.setattr(scl, "_now", lambda: future)
        assert led.can_spend(0.01) is True
