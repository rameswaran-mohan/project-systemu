"""T5 -- the "answered from your table" chips, over the DURABLE per-run record.

``table_payoff.using_from_table`` / ``answered_from_table`` were fully unit-tested and
had ZERO production callers, so the operator never saw the payoff. The chip model
pinned here is what every surface (Work card, workflow detail, chat task card) renders:

  * while the run is in flight: one "Using from your table: <item>" chip per item the
    table supplied (deduped by item, capped, overflow counted -- never dropped);
  * after it ends: "Answered from your table: N", with the items on expand.

It is built ON the two existing builders (pinned by spy below) and reads only the
record -- it never writes the novelty ledger, so no page refresh can burn an item's
novelty. Every string it emits is ASCII.
"""
from __future__ import annotations

import pytest

from systemu.runtime import table_payoff
from systemu.runtime.table_payoff_record import RunRecord


def _row(item, name=None, path=None, bound="prefilled_confirm", oid=1):
    return {"event": "supplied", "table_item_id": item, "item_name": name or item,
            "schema_path": path or item, "objective_id": oid, "tool": "send",
            "state": "have", "value_origin": "content_derived", "bound_as": bound}


def _rec(*rows, consulted=True) -> RunRecord:
    return RunRecord(root_execution_id="exec_r", consulted=consulted,
                     supplied=tuple(rows), executions=frozenset({"exec_r"}))


# == in flight =================================================================
def test_in_flight_names_each_item_the_table_supplied():
    m = table_payoff.run_chip_model(_rec(_row("t-1", "Work mail"), _row("t-2", "Reports")),
                                    in_flight=True)
    assert m["mode"] == "using"
    assert m["labels"] == ["Using from your table: Work mail",
                           "Using from your table: Reports"]
    assert m["count"] == 2


def test_in_flight_dedupes_by_item():
    """Two requirements drawn from ONE item are one chip, not two."""
    m = table_payoff.run_chip_model(
        _rec(_row("t-1", "Work mail", path="to"), _row("t-1", "Work mail", path="cc")),
        in_flight=True)
    assert m["labels"] == ["Using from your table: Work mail"]
    assert m["count"] == 2


def test_in_flight_caps_the_strip_and_counts_the_rest():
    rows = [_row(f"t-{i}", f"item {i}") for i in range(6)]
    m = table_payoff.run_chip_model(_rec(*rows), in_flight=True)
    assert len(m["labels"]) == table_payoff.MAX_USING_CHIPS + 1
    assert m["labels"][-1] == "+2 more from your table"


# == after the run ends ========================================================
def test_after_the_run_it_is_answered_from_your_table_with_the_items_on_expand():
    m = table_payoff.run_chip_model(
        _rec(_row("t-1", "Work mail", path="to"), _row("t-1", "Work mail", path="cc"),
             _row("t-2", "Reports")),
        in_flight=False)
    assert m["mode"] == "answered"
    assert m["label"] == "Answered from your table: 3"
    assert m["items"] == ["Work mail", "Reports"]


# == nothing to show ===========================================================
@pytest.mark.parametrize("in_flight", [True, False])
def test_no_record_or_no_supplied_requirement_shows_no_chip(in_flight):
    assert table_payoff.run_chip_model(None, in_flight=in_flight) is None
    assert table_payoff.run_chip_model(_rec(), in_flight=in_flight) is None


# == ASCII =====================================================================
def test_every_string_is_ascii_even_for_a_non_ascii_item_name():
    name = "Café résumés — 文件"
    for in_flight in (True, False):
        m = table_payoff.run_chip_model(_rec(_row("t-1", name)), in_flight=in_flight)
        for s in [m.get("label") or "", m["tooltip"], *m.get("labels", []),
                  *m.get("items", [])]:
            assert s.isascii(), s
    m = table_payoff.run_chip_model(_rec(_row("t-1", name)), in_flight=True)
    assert m["labels"][0].startswith("Using from your table: Cafe resumes")


def test_a_missing_name_falls_back_to_the_item_id():
    m = table_payoff.run_chip_model(_rec(_row("t-9", name="")), in_flight=True)
    assert m["labels"] == ["Using from your table: t-9"]


# == the existing builders are now the production path =========================
def test_the_chip_model_routes_through_the_existing_builders(monkeypatch):
    """The audit fact this packet closes: the builders had no production caller.
    The model must route THROUGH them, so a change to either one shows on every
    surface -- and it must hand the completion builder NO vault, so rendering can
    never advance (burn) the novelty ledger."""
    seen = {}
    real_using, real_answered = (table_payoff.using_from_table,
                                 table_payoff.answered_from_table)

    def _using(report, items=None):
        seen["using"] = True
        return real_using(report, items)

    def _answered(report, vault=None, *, record=True):
        seen["answered_vault"] = vault
        return real_answered(report, vault, record=record)

    monkeypatch.setattr(table_payoff, "using_from_table", _using)
    monkeypatch.setattr(table_payoff, "answered_from_table", _answered)
    table_payoff.run_chip_model(_rec(_row("t-1")), in_flight=True)
    table_payoff.run_chip_model(_rec(_row("t-1")), in_flight=False)
    assert seen == {"using": True, "answered_vault": None}


def test_the_answered_count_is_the_completion_builders_raw_count(monkeypatch):
    """N on the chip is answered_from_table's RAW count (computed before any novelty
    gate), not a local recount that could drift from it."""
    monkeypatch.setattr(table_payoff, "answered_from_table",
                        lambda report, vault=None, *, record=True:
                        {"count": 7, "chip": "x", "suppressed": False})
    m = table_payoff.run_chip_model(_rec(_row("t-1")), in_flight=False)
    assert m["label"] == "Answered from your table: 7"
