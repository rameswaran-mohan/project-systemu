"""R-A13b-2iii — the `debug s4-shadow-meter` CLI report reader.

A READ-ONLY operator surface: it resolves the vault the same way the other vault-scoped
debug commands do, reads the existing ``s4_shadow`` bucket via
``MetricsStore(...).shadow_meter_snapshot()``, renders a per-effect-class table, and prints
the pure Stage-3 arm-gate verdict. It writes nothing.

Tests mirror the in-repo CLI precedent (``test_cli_tools_recalibrate_show.py``): monkeypatch
``cli_commands._get_vault_and_config`` to a fake vault whose ``root`` is a tmp dir, seed the
store via the REAL meter writer (``incr_s4_shadow_meter``), and invoke the group via
``click.testing.CliRunner``.
"""
from __future__ import annotations

from click.testing import CliRunner

from systemu.interface import cli_commands
from systemu.runtime.metrics_store import MetricsStore
from systemu.runtime.s4_activation import format_shadow_meter_rows


class _FakeVault:
    def __init__(self, root):
        self.root = root


def _seed(metrics_dir, entries):
    """entries: list of (effect_class, would_credit) — one incr per tuple."""
    store = MetricsStore(metrics_dir)
    for ec, credit in entries:
        store.incr_s4_shadow_meter(ec, would_credit=credit)


def test_report_shows_classes_and_not_ready_verdict(tmp_path, monkeypatch):
    metrics_dir = tmp_path / "metrics"
    # money_move stamps but never credits ⇒ dead channel ⇒ NOT_READY.
    _seed(metrics_dir, [("money_move", False)] * 3 + [("net_mutate", True)] * 2)
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), _FakeVault(tmp_path)))
    runner = CliRunner()
    res = runner.invoke(cli_commands.debug_group, ["s4-shadow-meter", "--min-runs", "2"])
    assert res.exit_code == 0, res.output
    assert "money_move" in res.output
    assert "net_mutate" in res.output
    # the arm-verdict footer line (printed unwrapped via click.echo)
    assert "ARM VERDICT: NOT_READY" in res.output
    assert "dead channel" in res.output


def test_report_ready_verdict_when_channel_live(tmp_path, monkeypatch):
    metrics_dir = tmp_path / "metrics"
    # a stamp-set class that both stamps AND credits, coverage met ⇒ READY.
    _seed(metrics_dir, [("net_mutate", True)] * 3)
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), _FakeVault(tmp_path)))
    runner = CliRunner()
    res = runner.invoke(cli_commands.debug_group, ["s4-shadow-meter", "--min-runs", "2"])
    assert res.exit_code == 0, res.output
    assert "ARM VERDICT: READY" in res.output


def test_report_empty_store_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), _FakeVault(tmp_path)))
    runner = CliRunner()
    res = runner.invoke(cli_commands.debug_group, ["s4-shadow-meter"])
    assert res.exit_code == 0, res.output
    assert "no shadow-meter data yet" in res.output


def test_report_default_min_runs_is_20(tmp_path, monkeypatch):
    metrics_dir = tmp_path / "metrics"
    # 5 credits < default 20 ⇒ insufficient-data NOT_READY without passing --min-runs.
    _seed(metrics_dir, [("net_mutate", True)] * 5)
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), _FakeVault(tmp_path)))
    runner = CliRunner()
    res = runner.invoke(cli_commands.debug_group, ["s4-shadow-meter"])
    assert res.exit_code == 0, res.output
    assert "insufficient data (5/20)" in res.output


# --- pure formatting helper (no console needed) -------------------------------

def test_format_rows_computes_park_rate():
    snap = {"money_move": {"would_stamp": 4, "would_credit": 1, "would_park": 3}}
    rows = format_shadow_meter_rows(snap)
    assert rows == [{
        "effect_class": "money_move", "would_stamp": 4, "would_credit": 1,
        "would_park": 3, "park_rate": 0.75,
    }]


def test_format_rows_defensive_on_zero_stamp_and_garbage():
    snap = {"unknown": {}, "net_read": "garbage"}
    rows = format_shadow_meter_rows(snap)
    # both classes present, park_rate 0.0 (no divide-by-zero), sorted by class
    assert [r["effect_class"] for r in rows] == ["net_read", "unknown"]
    assert all(r["park_rate"] == 0.0 for r in rows)
