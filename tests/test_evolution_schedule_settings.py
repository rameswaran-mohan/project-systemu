"""Phase 5 Slice 3 Batch 1 (3f) — evolution cadence in Settings.

The daily evolution-check hour is no longer hard-coded in daemon.py; it reads
``SYSTEMU_EVOLUTION_HOUR`` (default "3", validated 0-23).  Settings exposes
``get_evolution_schedule`` / ``save_evolution_schedule`` mirroring the
get/save_stuck_settings pattern (env read + _update_env_var + live os.environ
patch).  Pure model round-trip — no .env writes leak (we point cwd at tmp_path).
"""
from __future__ import annotations

import os

import pytest


def test_get_evolution_schedule_default(monkeypatch):
    from systemu.interface.pages.settings import get_evolution_schedule
    monkeypatch.delenv("SYSTEMU_EVOLUTION_HOUR", raising=False)
    assert get_evolution_schedule() == {"hour": 3}


def test_get_evolution_schedule_reads_env(monkeypatch):
    from systemu.interface.pages.settings import get_evolution_schedule
    monkeypatch.setenv("SYSTEMU_EVOLUTION_HOUR", "7")
    assert get_evolution_schedule() == {"hour": 7}


def test_get_evolution_schedule_bad_env_falls_back(monkeypatch):
    from systemu.interface.pages.settings import get_evolution_schedule
    monkeypatch.setenv("SYSTEMU_EVOLUTION_HOUR", "not-a-number")
    assert get_evolution_schedule() == {"hour": 3}
    # Out-of-range stored value also falls back to the default.
    monkeypatch.setenv("SYSTEMU_EVOLUTION_HOUR", "99")
    assert get_evolution_schedule() == {"hour": 3}


def test_save_evolution_schedule_round_trips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # .env writes land in tmp_path
    monkeypatch.delenv("SYSTEMU_EVOLUTION_HOUR", raising=False)
    from systemu.interface.pages.settings import (
        get_evolution_schedule, save_evolution_schedule,
    )

    save_evolution_schedule(hour=6)
    # Live os.environ is patched immediately…
    assert os.environ["SYSTEMU_EVOLUTION_HOUR"] == "6"
    assert get_evolution_schedule() == {"hour": 6}
    # …and persisted to .env.
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SYSTEMU_EVOLUTION_HOUR=6" in env_text


@pytest.mark.parametrize("bad", [-1, 24, 25, 100])
def test_save_evolution_schedule_rejects_out_of_range(tmp_path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    from systemu.interface.pages.settings import save_evolution_schedule
    with pytest.raises(ValueError):
        save_evolution_schedule(hour=bad)


def test_save_evolution_schedule_accepts_boundaries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from systemu.interface.pages.settings import save_evolution_schedule, get_evolution_schedule
    save_evolution_schedule(hour=0)
    assert get_evolution_schedule() == {"hour": 0}
    save_evolution_schedule(hour=23)
    assert get_evolution_schedule() == {"hour": 23}


def test_evolution_schedule_card_importable():
    from systemu.interface.pages.settings import evolution_schedule_card
    assert callable(evolution_schedule_card)
