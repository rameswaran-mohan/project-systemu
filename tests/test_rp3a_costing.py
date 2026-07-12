"""R-P3a — cost visibility: the price table + cost_of/record_usage/daily_total.

Phase A step 1 tests (the pure write-path + pricing math), written FIRST.

The money-honesty invariant is the load-bearing property: an UNKNOWN model
shows tokens + "unknown", NEVER a guessed number; money is ``Decimal``, never
float. These tests pin AC2 (reconciliation), AC4 (unknown → Unknown), AC5
(zero-usage → 0 tokens/zero cost), and the operator-override path (AC3 backend).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from systemu.runtime import costing


@pytest.fixture(autouse=True)
def _clean_ledger_and_overrides(monkeypatch):
    """Every test starts with an empty ledger and no operator price overrides."""
    costing.reset_ledger()
    monkeypatch.delenv(costing.PRICE_OVERRIDE_ENV, raising=False)
    yield
    costing.reset_ledger()


# ─────────────────────────────────────────────────────────────────────────────
#  Pricing math (pure)


def test_known_model_is_priced_with_decimal_money():
    model = next(iter(costing.shipped_prices()))
    price = costing.shipped_prices()[model]
    rows = [{"model": model, "tokens_in": 1000, "tokens_out": 2000}]
    summary = costing.cost_of(rows)

    assert summary.tokens_in == 1000
    assert summary.tokens_out == 2000
    assert summary.total_known is True
    assert summary.total is not None
    # Money is Decimal — 1k in + 2k out priced exactly, no float drift.
    expected = price["in_per_1k"] * Decimal(1) + price["out_per_1k"] * Decimal(2)
    assert isinstance(summary.total.amount, Decimal)
    assert summary.total.amount == expected
    assert summary.total.currency == price["currency"]
    # And exactly one by-model line, priced.
    assert len(summary.by_model) == 1
    line = summary.by_model[0]
    assert line.priced is True
    assert line.cost is not None and line.cost.amount == expected


def test_unknown_model_yields_unknown_total_never_a_number():
    """AC4: an unknown model → that line + the total are Unknown (tokens still
    shown, money never fabricated)."""
    rows = [{"model": "who/is-this-model", "tokens_in": 500, "tokens_out": 500}]
    summary = costing.cost_of(rows)

    assert summary.tokens_in == 500          # tokens ARE shown
    assert summary.tokens_out == 500
    assert summary.total_known is False      # money is NOT
    assert summary.total is None             # never a guessed number
    assert summary.by_model[0].priced is False
    assert summary.by_model[0].cost is None


def test_any_unknown_line_poisons_the_total_to_unknown():
    """A run mixing a known + an unknown model → total Unknown (can't honestly
    claim a number when part is unpriced)."""
    known = next(iter(costing.shipped_prices()))
    rows = [
        {"model": known, "tokens_in": 100, "tokens_out": 100},
        {"model": "mystery/model", "tokens_in": 100, "tokens_out": 100},
    ]
    summary = costing.cost_of(rows)
    assert summary.total_known is False
    assert summary.total is None
    # The known line is still individually priced (honest per-line detail).
    known_line = next(l for l in summary.by_model if l.model == known)
    assert known_line.priced is True


def test_zero_usage_run_is_zero_tokens_and_zero_cost():
    """AC5: a deterministic zero-LLM run → 0 tokens, zero cost (known, not Unknown)."""
    summary = costing.cost_of([])
    assert summary.tokens_in == 0
    assert summary.tokens_out == 0
    assert summary.by_model == []
    assert summary.total_known is True
    assert summary.total is not None
    assert summary.total.amount == Decimal(0)


def test_reconciliation_sum_of_by_model_equals_total():
    """AC2 property: the total is exactly the sum of the priced by-model lines."""
    prices = costing.shipped_prices()
    models = list(prices)[:2] if len(prices) >= 2 else list(prices) * 2
    rows = [
        {"model": models[0], "tokens_in": 1200, "tokens_out": 800},
        {"model": models[1], "tokens_in": 300, "tokens_out": 700},
        {"model": models[0], "tokens_in": 500, "tokens_out": 0},
    ]
    summary = costing.cost_of(rows)
    assert summary.total_known is True
    line_sum = sum((l.cost.amount for l in summary.by_model), Decimal(0))
    assert summary.total.amount == line_sum
    # Tokens reconcile too.
    assert summary.tokens_in == 2000
    assert summary.tokens_out == 1500


def test_same_model_across_rows_aggregates_into_one_line():
    model = next(iter(costing.shipped_prices()))
    rows = [
        {"model": model, "tokens_in": 10, "tokens_out": 20},
        {"model": model, "tokens_in": 30, "tokens_out": 40},
    ]
    summary = costing.cost_of(rows)
    assert len(summary.by_model) == 1
    assert summary.by_model[0].tokens_in == 40
    assert summary.by_model[0].tokens_out == 60


# ─────────────────────────────────────────────────────────────────────────────
#  Operator override (AC3 backend — the render-side immediacy is a surface test)


def test_operator_override_changes_the_price_immediately(monkeypatch):
    """AC3: a price override read on each cost_of — no restart, no cache."""
    import json
    model = next(iter(costing.shipped_prices()))
    rows = [{"model": model, "tokens_in": 1000, "tokens_out": 1000}]

    before = costing.cost_of(rows)
    monkeypatch.setenv(
        costing.PRICE_OVERRIDE_ENV,
        json.dumps({model: {"in_per_1k": "9.99", "out_per_1k": "0", "currency": "USD"}}),
    )
    after = costing.cost_of(rows)

    assert after.total.amount == Decimal("9.99")   # 1k * 9.99 + 1k * 0
    assert after.total.amount != before.total.amount


def test_override_can_price_a_previously_unknown_model(monkeypatch):
    import json
    rows = [{"model": "brand/new-model", "tokens_in": 1000, "tokens_out": 0}]
    assert costing.cost_of(rows).total_known is False   # unknown by default
    monkeypatch.setenv(
        costing.PRICE_OVERRIDE_ENV,
        json.dumps({"brand/new-model": {"in_per_1k": "1.5", "out_per_1k": "3",
                                        "currency": "INR"}}),
    )
    summary = costing.cost_of(rows)
    assert summary.total_known is True
    assert summary.total.amount == Decimal("1.5")
    assert summary.total.currency == "INR"


def test_malformed_override_is_ignored_not_fatal(monkeypatch):
    """A hand-corrupted override JSON must never crash costing — it degrades to
    the shipped table (never blocks a run on bad pricing config)."""
    model = next(iter(costing.shipped_prices()))
    monkeypatch.setenv(costing.PRICE_OVERRIDE_ENV, "{not valid json")
    summary = costing.cost_of([{"model": model, "tokens_in": 1000, "tokens_out": 0}])
    assert summary.total_known is True   # fell back to shipped price


# ─────────────────────────────────────────────────────────────────────────────
#  The ledger write path (record_usage / usage_rows) + daily total


def test_record_usage_attributes_rows_to_the_run_ref():
    costing.record_usage("run-A", "m/x", 10, 5)
    costing.record_usage("run-A", "m/x", 3, 7)
    costing.record_usage("run-B", "m/y", 1, 1)

    rows_a = costing.usage_rows("run-A")
    assert len(rows_a) == 2
    assert sum(r["tokens_in"] for r in rows_a) == 13
    assert sum(r["tokens_out"] for r in rows_a) == 12
    assert len(costing.usage_rows("run-B")) == 1


def test_record_usage_none_run_ref_is_a_noop_never_orphans():
    """A call outside any run (execution_id is None) must NOT write a phantom
    orphan row and must NOT crash."""
    costing.record_usage(None, "m/x", 10, 5)
    costing.record_usage("", "m/x", 10, 5)
    # Nothing recorded under a falsy key.
    assert costing.usage_rows(None) == []
    assert costing.usage_rows("") == []


def test_cost_of_accepts_an_execution_id_string():
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("run-C", model, 1000, 0)
    summary = costing.cost_of("run-C")
    assert summary.tokens_in == 1000
    assert summary.total_known is True


def test_cost_of_accepts_a_record_with_a_cost_field():
    """The durable path: a run record carrying its own usage rows in a ``cost``
    field prices without touching the live ledger."""
    model = next(iter(costing.shipped_prices()))
    record = {"execution_id": "run-D",
              "cost": [{"model": model, "tokens_in": 2000, "tokens_out": 0}]}
    summary = costing.cost_of(record)
    assert summary.tokens_in == 2000
    assert summary.total_known is True


def test_daily_total_reconciles_with_the_per_run_sum():
    """AC2 (home): the daily total equals the sum of the per-run cost_of over the
    same runs (both lanes)."""
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("quick_1", model, 1000, 500)   # quick lane
    costing.record_usage("exec_2", model, 2000, 1000)   # workflow lane

    per_run = [costing.cost_of("quick_1"), costing.cost_of("exec_2")]
    total = costing.daily_total(["quick_1", "exec_2"])

    assert total.tokens_in == 3000
    assert total.tokens_out == 1500
    assert total.total_known is True
    assert total.total.amount == sum((s.total.amount for s in per_run), Decimal(0))


def test_daily_total_over_all_todays_ledger_runs_when_runs_none():
    model = next(iter(costing.shipped_prices()))
    costing.record_usage("r1", model, 100, 0)
    costing.record_usage("r2", model, 400, 0)
    total = costing.daily_total()
    assert total.tokens_in == 500


def test_negative_price_override_is_rejected_never_a_negative_total(monkeypatch):
    """Money honesty: a NEGATIVE override price ("-5" is a valid Decimal) must be
    REJECTED, not accepted into a fabricated negative total. The model falls back
    to its shipped default (or Unknown) — never a negative number."""
    known = next(iter(costing.shipped_prices()))
    monkeypatch.setenv(
        costing.PRICE_OVERRIDE_ENV,
        f'{{"{known}": {{"in_per_1k": "-5", "out_per_1k": "-5", "currency": "USD"}}}}')
    rows = [{"model": known, "tokens_in": 1000, "tokens_out": 1000}]
    summary = costing.cost_of(rows)
    # the negative override was dropped → priced by the (positive) shipped default.
    if summary.total is not None:
        assert summary.total.amount >= 0, "a negative price must never fabricate a negative total"
    assert all((l.cost is None or l.cost.amount >= 0) for l in summary.by_model)


def test_currency_override_is_case_insensitive_single_currency(monkeypatch):
    """A currency override of "usd"/" USD " must normalize to the shipped "USD" so
    a single-currency run is NOT wrongly rendered Unknown on a case mismatch."""
    known = next(iter(costing.shipped_prices()))
    monkeypatch.setenv(
        costing.PRICE_OVERRIDE_ENV,
        f'{{"{known}": {{"in_per_1k": "0.001", "out_per_1k": "0.002", "currency": " usd "}}}}')
    rows = [{"model": known, "tokens_in": 1000, "tokens_out": 1000}]
    summary = costing.cost_of(rows)
    assert summary.total_known is True, "a single (case-different) currency must still price"
    assert summary.total is not None and summary.total.currency == "USD"


def test_cost_chip_for_helper():
    """cost_chip_for: a run with usage → a chip string; a zero-usage run → None
    (no chip); an unknown model → tokens + '—' (never a fabricated number)."""
    known = next(iter(costing.shipped_prices()))
    assert costing.cost_chip_for([]) is None                       # zero usage → no chip
    assert costing.cost_chip_for(
        [{"model": known, "tokens_in": 1000, "tokens_out": 1000}]) is not None
    unknown_chip = costing.cost_chip_for(
        [{"model": "who/unknown", "tokens_in": 500, "tokens_out": 500}])
    assert unknown_chip is not None and unknown_chip.startswith("—")   # tokens shown, cost —


def test_cost_survives_restart_via_durable_fallback(tmp_path, monkeypatch):
    """After a daemon restart (the in-process ledger is wiped), cost_of(eid) still
    shows the run's cost by falling back to the durable per-run cost file."""
    monkeypatch.setattr(costing, "_DEFAULT_DATA_DIR", str(tmp_path))
    known = next(iter(costing.shipped_prices()))
    costing.reset_ledger()
    costing.record_usage("exec-restart", known, 1000, 500)
    costing.persist_run_cost("exec-restart")            # durable write

    costing.reset_ledger()                              # simulate the restart
    assert costing.usage_rows("exec-restart") == []     # ledger cleared

    s = costing.cost_of("exec-restart")                 # falls back to the durable file
    assert s.tokens_in == 1000 and s.tokens_out == 500
    assert s.total_known is True                        # priced from the durable rows


def test_persist_run_cost_is_a_noop_without_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(costing, "_DEFAULT_DATA_DIR", str(tmp_path))
    costing.reset_ledger()
    costing.persist_run_cost("exec-empty")              # no usage → no orphan file
    assert costing._read_durable_cost("exec-empty") == []
    costing.persist_run_cost(None)                      # no eid → no-op


def test_live_ledger_preferred_over_durable(tmp_path, monkeypatch):
    """A LIVE run reads the fresh ledger, not a stale durable file."""
    monkeypatch.setattr(costing, "_DEFAULT_DATA_DIR", str(tmp_path))
    known = next(iter(costing.shipped_prices()))
    costing.reset_ledger()
    costing.record_usage("exec-live", known, 100, 50)
    costing.persist_run_cost("exec-live")               # durable = 100/50
    costing.record_usage("exec-live", known, 900, 450)  # ledger now 1000/500 (fresher)
    s = costing.cost_of("exec-live")
    assert s.tokens_in == 1000 and s.tokens_out == 500  # live ledger wins
