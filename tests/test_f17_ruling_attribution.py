"""F17 — the first-gate card must attribute EVERY dated operator ruling, not one.

THE DEFECT (read off the live card on 2026-08-09):

``first_gate_review.batch_rule_sentence()`` closed with

    "browser_actuate, clipboard_read, input_synthesis, screen_capture are in that
     list by an explicit operator decision of 2026-08-07, taken with the risks
     stated: ..."

generated from ``OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07`` ALONE. Since F16 there
is a SECOND ruling, ``OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09``, which admitted
``net_read``. ``net_read`` therefore appeared in the allowlist sentence with NO
attribution — the card read as though it had always been allowed rather than
decided on a day, with risks shown. The card is the operator-facing RECORD of what
they agreed to; an unattributed member is a hole in that record.

THE PROPERTY (quantified over ALL rulings, including ones made tomorrow):

    Every member of ``effect_tags.OPERATOR_RULED_BATCH_APPROVABLE`` is named in the
    RENDERED card text inside a clause carrying the DATE of the ruling that admitted
    it — and the attribution is GENERATED from the full registry of dated rulings,
    so a third ruling is attributed automatically with nobody editing a sentence.

THE FENCE has two committed parts:

  1. ``effect_tags.OPERATOR_RULINGS`` is THE registry: one row per dated decision,
     carrying its date, its tag set and the risks the operator was shown.
     ``OPERATOR_RULED_BATCH_APPROVABLE`` is DERIVED from it, so a new dated constant
     that is not registered does not become batch-approvable at all — fail-closed,
     not merely unattributed.
  2. ``first_gate_review.batch_rule_sentence()`` emits one attribution clause per
     registry row, each led by ``RULING_ATTRIBUTION_MARKER`` + its own date, and the
     tests below read the clauses back out of the text a GateDescriptor renders.
"""
from __future__ import annotations

import pytest


# ── helpers (same shape as tests/test_f9_batch_approval_effects.py) ──────────

def _entry(name: str, tags, *, signature: str = "sig"):
    from systemu.runtime.first_gate_review import build_entry
    return build_entry(tool_id=f"t::{name}", name=name,
                       effect_tags=list(tags), signature=signature)


def _card_text() -> str:
    """The text an operator actually reads on the first-gate bulk card."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries
    entries = [_entry("run_command", ["shell_exec"], signature="sig::run_command"),
               _entry("file_read", ["local_read"], signature="sig::file_read")]
    part = partition_entries(entries)
    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.23")
    return f"{d.what_approve_does}\n{d.inspect}"


def _clauses_from(text: str) -> dict:
    """date -> the attribution clause that date leads, read out of the TEXT.

    The marker leads the clause and the DATE is the first thing after it, so each
    split segment is exactly one ruling's clause: it cannot borrow tag names from
    the allowlist enumeration earlier in the sentence, nor from a sibling ruling.
    """
    from systemu.runtime.first_gate_review import RULING_ATTRIBUTION_MARKER
    out = {}
    for seg in text.split(RULING_ATTRIBUTION_MARKER)[1:]:
        out[seg[:10]] = seg
    return out


# ── A. the registry is THE source, and it is complete ────────────────────────

def test_the_ruled_set_is_derived_from_the_dated_registry():
    """DEC-43: one mint. A dated constant that is not registered is not ruled.

    This is what makes an unattributed future ruling impossible rather than merely
    caught: the registry is the only door into ``OPERATOR_RULED_BATCH_APPROVABLE``.
    """
    from systemu.runtime.effect_tags import (OPERATOR_RULED_BATCH_APPROVABLE,
                                             OPERATOR_RULINGS)

    assert OPERATOR_RULINGS, "there is at least one dated operator ruling"
    union = frozenset().union(*(r.tags for r in OPERATOR_RULINGS))
    assert union == OPERATOR_RULED_BATCH_APPROVABLE, (
        "every ruled member must trace to a dated registry row, and every registry "
        "row must be in the ruled set")


def test_every_registry_row_carries_a_date_and_the_risks_shown():
    from systemu.runtime.effect_tags import OPERATOR_RULINGS
    import re
    for r in OPERATOR_RULINGS:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.date), r
        assert r.tags, f"ruling {r.date} admitted nothing"
        assert len(r.risks) > 40, (
            f"ruling {r.date} must state the risks the operator was shown")


def test_the_two_shipped_rulings_are_registered_by_date():
    """The concrete F17 facts: both dated constants, each under its own date."""
    from systemu.runtime.effect_tags import (
        OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07,
        OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09, OPERATOR_RULINGS)
    by_date = {r.date: r.tags for r in OPERATOR_RULINGS}
    assert by_date.get("2026-08-07") == OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07
    assert by_date.get("2026-08-09") == OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09


# ── B. the RENDERED CARD attributes every ruled member to a date ─────────────

def test_every_ruled_member_is_attributed_to_a_date_on_the_card():
    """THE F17 PROPERTY. Quantified over the whole ruled set, on the rendered text."""
    from systemu.runtime.effect_tags import (OPERATOR_RULED_BATCH_APPROVABLE,
                                             OPERATOR_RULINGS)

    text = _card_text()
    clauses = _clauses_from(text)

    for r in OPERATOR_RULINGS:
        assert r.date in clauses, (
            f"the ruling of {r.date} is not attributed anywhere on the card")
        clause = clauses[r.date]
        for t in r.tags:
            assert t.value in clause, (
                f"{t.value} was admitted by the {r.date} ruling but that date's "
                f"clause does not name it")

    attributed = set()
    for clause in clauses.values():
        for t in OPERATOR_RULED_BATCH_APPROVABLE:
            if t.value in clause:
                attributed.add(t.value)
    assert attributed == {t.value for t in OPERATOR_RULED_BATCH_APPROVABLE}, (
        "a ruled effect class appears in the allowlist with no dated attribution — "
        "the card reads as though it was always allowed rather than decided")


def test_net_read_is_attributed_to_the_08_09_ruling_not_the_08_07_one():
    """The exact hole F17 names: net_read was in the list, silently."""
    clauses = _clauses_from(_card_text())
    assert "net_read" in clauses.get("2026-08-09", "")
    assert "net_read" not in clauses.get("2026-08-07", "")


def test_each_clause_states_the_risks_that_ruling_was_taken_with():
    from systemu.runtime.effect_tags import OPERATOR_RULINGS
    clauses = _clauses_from(_card_text())
    for r in OPERATOR_RULINGS:
        assert r.risks in clauses[r.date], (
            f"the {r.date} clause does not carry the risks the operator was shown")


# ── C. a THIRD ruling tomorrow is attributed with nobody editing a sentence ──

def test_a_ruling_added_tomorrow_is_attributed_automatically(monkeypatch):
    """The generation property, not just today's output.

    A new registry row must show up on the card by itself. If the attribution is
    ever hard-coded to a fixed set of dates again, this goes red.
    """
    from systemu.runtime import effect_tags as et
    from systemu.runtime.first_gate_review import batch_rule_sentence

    future = et.OperatorRuling(
        date="2027-01-31",
        tags=frozenset({et.EffectTag.DESKTOP_NOTIFY}),
        risks=("a notification is drawn on the operator's own screen and can "
               "imitate a system prompt, so it is a phishing surface"),
    )
    monkeypatch.setattr(et, "OPERATOR_RULINGS",
                        tuple(et.OPERATOR_RULINGS) + (future,), raising=True)

    clauses = _clauses_from(batch_rule_sentence())
    assert "2027-01-31" in clauses, (
        "a newly registered ruling was not attributed — the sentence is not "
        "generated from the full registry")
    assert "desktop_notify" in clauses["2027-01-31"]
    assert future.risks in clauses["2027-01-31"]


def test_a_third_ruling_does_not_push_the_card_past_the_clip(monkeypatch):
    """The attribution grows by a clause per ruling; the dashboard clip does not.

    The consent record may not be the part that gets cut — the renderer's clip
    (``live_events_pane.clip_detail``) is silent, so an operator would read a
    truncated record with nothing on screen saying so. The display-only listing
    yields instead, and discloses that it did.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime import effect_tags as et
    from systemu.runtime.first_gate_review import (MAX_CARD_CHARS,
                                                   partition_entries)

    extra = tuple(
        et.OperatorRuling(
            date=f"2027-0{i}-01",
            tags=frozenset({et.EffectTag.DESKTOP_NOTIFY}),
            risks=("a notification is drawn on the operator's own screen and can "
                   "imitate a system prompt, so it is a phishing surface" + " x" * 60),
        )
        for i in (1, 2, 3)
    )
    monkeypatch.setattr(et, "OPERATOR_RULINGS", tuple(et.OPERATOR_RULINGS) + extra)

    many = [_entry(f"file_scan_directory_variant_{i:03d}", [], signature=f"s{i}")
            for i in range(400)]
    part = partition_entries(many)
    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.23")

    assert len(d.inspect) <= MAX_CARD_CHARS, (
        f"card is {len(d.inspect)} chars — the renderer would cut the consent record")
    assert "more (open the Inbox" in d.inspect, "truncation must stay disclosed"
    clauses = _clauses_from(d.inspect)
    for r in et.OPERATOR_RULINGS:
        assert r.date in clauses, f"{r.date} lost to the clip"


def test_the_attribution_survives_a_registry_with_no_rulings(monkeypatch):
    """Degenerate but real: with nothing ruled, the card must not invent a clause."""
    from systemu.runtime import effect_tags as et
    from systemu.runtime.first_gate_review import batch_rule_sentence

    monkeypatch.setattr(et, "OPERATOR_RULINGS", (), raising=True)
    sentence = batch_rule_sentence()
    assert _clauses_from(sentence) == {}
    assert sentence.strip(), "the rule sentence itself must still be rendered"
