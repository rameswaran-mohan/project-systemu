"""R-P3a — the cost surfaces, at the render-DATA level (Phase B).

A full NiceGUI render is impractical in a unit test, so we assert the row/summary
DATA the page would render (the pure model-builders + model_dump(mode="json") at
the boundary). Money in the dumped payload is a STRING (Decimal in json mode),
never a float — the honesty invariant carried to the wire.

Cost is separate chrome from verified/claimed badges — these tests assert cost
never fabricates money for an unknown model, and never implies success.
"""
from __future__ import annotations

import pytest

from systemu.runtime import costing
from systemu.runtime.workflow_tracker import WorkflowSnapshot


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    costing.reset_ledger()
    monkeypatch.delenv(costing.PRICE_OVERRIDE_ENV, raising=False)
    yield
    costing.reset_ledger()


def _snap(**kw) -> WorkflowSnapshot:
    base = dict(workflow_id="w1", title="Do a thing", stage="done", status="completed")
    base.update(kw)
    return WorkflowSnapshot(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  Work-row chip (step 5)


def test_work_row_shows_cost_chip_for_a_priced_run():
    from systemu.interface.pages.work import work_row_model
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("exec-1", model, 30000, 8000)
    row = work_row_model(_snap(execution_id="exec-1"))

    assert row["has_cost"] is True
    assert row["cost"]["tokens_in"] == 30000
    assert row["cost"]["total_known"] is True
    # Money on the wire is a string, not a float.
    assert isinstance(row["cost"]["total"]["amount"], str)
    assert "38k tok" in row["cost_chip"]


def test_work_row_unknown_model_shows_tokens_only_no_fabricated_money():
    from systemu.interface.pages.work import work_row_model
    costing.record_usage("exec-2", "mystery/model", 1000, 0)
    row = work_row_model(_snap(execution_id="exec-2"))

    assert row["has_cost"] is True
    assert row["cost"]["total_known"] is False
    assert row["cost"]["total"] is None            # never a guessed number
    assert row["cost_chip"].startswith("—")        # tokens shown, money withheld


def test_work_row_without_execution_id_has_no_cost():
    from systemu.interface.pages.work import work_row_model
    row = work_row_model(_snap(execution_id=None))
    assert row["has_cost"] is False


def test_work_row_price_override_re_renders_immediately(monkeypatch):
    """AC3: an override changes the row's chip on the next build (no restart)."""
    import json
    from systemu.interface.pages.work import work_row_model
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("exec-3", model, 1000, 0)

    before = work_row_model(_snap(execution_id="exec-3"))["cost"]["total"]["amount"]
    monkeypatch.setenv(costing.PRICE_OVERRIDE_ENV,
                       json.dumps({model: {"in_per_1k": "5", "out_per_1k": "0",
                                           "currency": "USD"}}))
    after = work_row_model(_snap(execution_id="exec-3"))["cost"]["total"]["amount"]
    assert after == "5.00000" or after.startswith("5")   # 1k * 5
    assert after != before


# ─────────────────────────────────────────────────────────────────────────────
#  Task-outcome by-model breakdown (step 6)


def test_workflow_detail_by_model_breakdown():
    from systemu.interface.pages.workflow_detail import cost_detail_view
    prices = list(costing.shipped_prices())
    costing.record_usage("exec-5", prices[0], 1000, 500)
    if len(prices) > 1:
        costing.record_usage("exec-5", prices[1], 200, 0)

    view = cost_detail_view("exec-5")
    assert view["has_usage"] is True
    assert view["total_known"] is True
    assert len(view["by_model"]) == (2 if len(prices) > 1 else 1)
    assert all(r["priced"] for r in view["by_model"])
    assert view["total"] != "unknown"


def test_workflow_detail_unknown_model_line_shows_unknown_cost():
    from systemu.interface.pages.workflow_detail import cost_detail_view
    costing.record_usage("exec-6", "who/knows", 1000, 0)
    view = cost_detail_view("exec-6")
    assert view["by_model"][0]["cost"] == "unknown"   # no fabricated number
    assert view["total"] == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
#  Home daily total (step 7) — both lanes, reconciliation (AC2)


def test_home_daily_total_sums_both_lanes():
    from systemu.interface.pages.console import home_daily_cost_summary
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("quick_a", model, 1000, 0)    # quick lane
    costing.record_usage("exec_b", model, 2000, 0)     # workflow lane

    view = home_daily_cost_summary()
    assert view["has_usage"] is True
    assert view["tokens_total"] == 3000
    assert view["total_known"] is True


def test_home_daily_total_reconciles_with_per_run_sum():
    """AC2: the Home daily total equals the sum of the per-run costs."""
    from systemu.interface.pages.console import home_daily_cost_summary
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("quick_a", model, 1000, 500)
    costing.record_usage("exec_b", model, 2000, 1000)

    per_run = [costing.cost_of("quick_a"), costing.cost_of("exec_b")]
    from decimal import Decimal
    expected = sum((s.total.amount for s in per_run), Decimal(0))
    view = home_daily_cost_summary(["quick_a", "exec_b"])
    # The formatted total reflects the reconciled sum.
    assert view["total"] == costing.format_money(
        costing.Money(amount=expected, currency=per_run[0].total.currency))


# ─────────────────────────────────────────────────────────────────────────────
#  Settings price editor (step 8)


def test_settings_get_price_rows_lists_shipped_models():
    from systemu.interface.pages.settings import get_price_rows
    rows = get_price_rows()
    models = {r["model"] for r in rows}
    assert models >= set(costing.shipped_prices())
    assert all(not r["overridden"] for r in rows)   # nothing tuned yet


def test_settings_save_price_override_takes_effect_immediately(monkeypatch, tmp_path):
    """AC3 end-to-end at the write side: save_price_overrides → costing prices
    change on the very next cost_of (no restart)."""
    # Redirect the .env write into a temp cwd so we never touch the repo's .env.
    monkeypatch.chdir(tmp_path)
    from systemu.interface.pages.settings import save_price_overrides
    model = next(iter(costing.shipped_prices()))
    rows = [{"model": model, "tokens_in": 1000, "tokens_out": 0}]

    before = costing.cost_of(rows).total.amount
    save_price_overrides({model: {"in_per_1k": "7.5", "out_per_1k": "0",
                                  "currency": "USD"}})
    after = costing.cost_of(rows).total.amount
    from decimal import Decimal
    assert after == Decimal("7.5")
    assert after != before


def test_settings_save_rejects_non_numeric_price():
    from systemu.interface.pages.settings import save_price_overrides
    with pytest.raises(ValueError):
        save_price_overrides({"m/x": {"in_per_1k": "abc", "out_per_1k": "1"}})
