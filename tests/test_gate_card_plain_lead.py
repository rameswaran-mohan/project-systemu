"""The bulk first-gate card must OPEN in plain English (persona onboarding, Task 3).

THE DEFECT this pins shut:

The one-time bulk review card is the FIRST security decision a new operator is ever
asked to make, and it opened cold on the generated consent record -- a 1200-character
sentence enumerating ``browser_actuate, clipboard_read, input_synthesis, local_read,
local_write, net_read, no_effect, screen_capture`` and two dated operator rulings with
their risks. That sentence is exactly right and completely unreadable to someone who
has never seen an effect class. An operator who cannot parse the first line of a
one-click, inventory-wide grant either clicks it blind or abandons the product; both
are failures of the same consent.

THE PROPERTY:

    The card's FIRST line is a plain-English lead that says what the review is and
    what batch approval does and does not cover -- and every safety claim the lead
    makes is TRUE of the machine's own allowlist, not merely reassuring.

    AND the lead is display copy, so it participates in the renderer's clip loop
    exactly like the tool listing does. The F17 consent record still never yields:
    under an inventory large enough to force the clip, ``batch_rule_sentence()`` is
    still present in full and the truncation is still disclosed.

Construction fixtures copied from ``tests/test_f9_batch_approval_effects.py``:
``_entry`` verbatim, ``_partition`` from
``test_the_card_still_offers_the_batch_when_something_DOES_qualify`` (a band with a
real ELIGIBLE member has to be built directly -- a ``local_read`` tool does not gate,
so ``partition_entries`` drops it from both bands), and the 400-tool ``partition_entries``
inventory from ``test_the_excluded_listing_with_reasons_stays_inside_the_dashboard_clip``
for the real-path clip case.

The lead is transcribed here VERBATIM and independently -- it is deliberately NOT
imported from the module under test, because a pin that reads its expectation out of
the code it is checking asserts nothing about the words an operator reads.
"""
from __future__ import annotations

import pytest


LEAD = (
    "In plain English: this one-time review lists what Systemu may do without "
    "asking every time. Batch approval covers only safe, reversible kinds of "
    "action. Anything that moves money, uses a credential, or reaches the "
    "network in new ways always asks you first, one card at a time."
)


# -- helpers (copied from tests/test_f9_batch_approval_effects.py) -------------

def _entry(name, tags, *, tool_id="t1", signature=None):
    from systemu.runtime.first_gate_review import build_entry
    return build_entry(tool_id=tool_id, name=name, effect_tags=list(tags),
                       signature=signature or f"sig::{name}")


def _partition(eligible_rows, excluded_rows):
    from systemu.runtime.first_gate_review import BulkReviewPartition
    return BulkReviewPartition(
        eligible=tuple(_entry(n, t, signature=f"e::{n}") for n, t in eligible_rows),
        excluded=tuple(_entry(n, t, signature=f"x::{n}") for n, t in excluded_rows))


def _card(eligible_rows, excluded_rows):
    from systemu.interface.command.gate import GateDescriptor
    return GateDescriptor.from_first_gate_bulk(
        _partition(eligible_rows, excluded_rows), version="0.10.22")


_ELIGIBLE = [("discovered_reader", ["local_read"])]
_EXCLUDED = [("run_command", ["shell_exec"]), ("file_delete", ["local_delete"])]


# -- A. the card opens in plain English, on every shape of the card -----------

@pytest.mark.parametrize("eligible,excluded", [
    (_ELIGIBLE, _EXCLUDED),   # both bands populated
    ([], _EXCLUDED),          # nothing qualifies -> no affirmative option offered
    (_ELIGIBLE, []),          # nothing excluded
], ids=["both-bands", "nothing-qualifies", "nothing-excluded"])
def test_bulk_card_opens_in_plain_english_and_stays_true(eligible, excluded):
    card = _card(eligible, excluded)

    head = card.inspect.splitlines()[0].lower()
    assert "plain english" in head, (
        "the card still opens on the consent record: "
        f"{card.inspect.splitlines()[0][:120]!r}")

    # The lead's safety claims must match the machine truth:
    assert "money" in card.inspect and "credential" in card.inspect

    assert LEAD in card.inspect, "the lead is not the text the plan approved, verbatim"
    assert card.inspect.startswith(LEAD), "the lead must lead"


def test_the_card_reached_through_the_REAL_partition_opens_the_same_way():
    """Not a hand-built partition: the path ``surface_bulk_first_gate`` actually walks."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries

    part = partition_entries([_entry(n, t, signature=f"sig::{n}")
                              for n, t in _EXCLUDED])
    assert part.excluded, "precondition: these tools really do gate"
    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.22")
    assert d.inspect.startswith(LEAD)


def test_the_lead_is_ascii_only():
    """The card carries an em dash and a real ellipsis elsewhere; the lead may not.

    Onboarding copy stays ASCII so a console that cannot encode the operator's codepage
    does not mangle the first line they read.
    """
    card = _card(_ELIGIBLE, _EXCLUDED)
    lead_line = card.inspect.splitlines()[0]
    lead_line.encode("ascii")  # raises if the lead ever picks up a smart character
    assert LEAD.isascii()


# -- B. the lead's claims are TRUE of the allowlist, not merely reassuring ----

def test_the_leads_three_promises_hold_against_the_allowlist():
    """"moves money", "uses a credential", "reaches the network in new ways".

    The lead is a claim about what one click can grant. If any of these classes ever
    enters ``BATCH_APPROVABLE``, the first line of the card becomes a lie and this
    goes red -- the same failure mode as the F9 promise, one surface earlier.

    Note ``net_read`` IS batch-approvable (the 2026-08-09 operator ruling), which is
    why the lead says "reaches the network in NEW ways" rather than "uses the
    network": the claim is scoped to ``net_mutate``, and it is exact.
    """
    from systemu.runtime.effect_tags import BATCH_APPROVABLE, EffectTag

    for claim, tag in (("moves money", EffectTag.MONEY_MOVE),
                       ("uses a credential", EffectTag.OAUTH_CALL),
                       ("reaches the network in new ways", EffectTag.NET_MUTATE)):
        assert tag not in BATCH_APPROVABLE, (
            f"the card's lead promises that anything that {claim} always asks first, "
            f"but {tag.value} is batch-approvable")


# -- C. the lead yields to the clip; the consent record still never does ------

def test_the_lead_participates_in_the_clip_and_the_record_still_never_yields():
    """F17's invariant, re-pinned with the lead in front of it.

    The lead is display copy added INSIDE ``_render``, so it is inside the loop that
    shrinks the card. What may not change: under an inventory big enough to force the
    clip, the whole consent record is still on the card, the truncation is still
    disclosed, and the card still fits the dashboard's silent 8000-char detail clip.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (MAX_CARD_CHARS,
                                                   batch_rule_sentence,
                                                   partition_entries)

    many = [_entry(f"file_scan_directory_variant_{i:03d}", [], signature=f"s{i}")
            for i in range(400)]
    part = partition_entries(many)
    assert len(part.excluded) == 400, "precondition: a large EXCLUDED band"

    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.22")

    assert len(d.inspect) <= MAX_CARD_CHARS, (
        f"card is {len(d.inspect)} chars -- the renderer would cut the consent record")
    assert batch_rule_sentence() in d.inspect, (
        "the consent record yielded to the clip; only the display listing may")
    assert "more (open the Inbox" in d.inspect, "truncation must stay disclosed"
    assert LEAD in d.inspect, "the lead was clipped out of a card that still had room"
    assert "400" in d.what_approve_does, "the COUNT must stay exact and un-truncated"


def test_the_consent_record_follows_the_lead_and_is_unabridged():
    """The lead ANNOTATES the record; it does not replace, summarize or reorder it."""
    from systemu.runtime.first_gate_review import batch_rule_sentence

    card = _card(_ELIGIBLE, _EXCLUDED)
    rule = batch_rule_sentence()
    assert rule in card.inspect
    assert card.inspect.index(LEAD) < card.inspect.index(rule)
    # and the record is still what the affirmative copy quotes, byte for byte
    assert rule in card.what_approve_does


def test_the_card_title_is_unchanged():
    """The lead goes in ``inspect`` only -- the title is a separate pinned surface."""
    card = _card(_ELIGIBLE, _EXCLUDED)
    assert card.title == "Review 3 tool(s) before the action gate turns on"
    assert "plain English" not in card.title
