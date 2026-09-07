"""P9(b) -- the bulk FIRST-GATE card, the one exemption the ASCII pass left open.

THE DEFECT
    ``tests/test_dogfood28_p9_gate_card_corpus_is_ascii.py`` made every card in
    `gate.py` ASCII except one: the bulk first-gate card. Its exact bytes are
    frozen by two golden SHAs in ``tests/test_impl4_bulk_first_gate.py``, on the
    operator's ruling that "the timing moved and NOTHING else". Cleaning the
    card up is a legitimate change AND a deliberate, visible re-freeze -- so it
    was carved out rather than slipped through the pin that exists to catch
    exactly that.

    This is that re-freeze. The card is now ASCII and both goldens were
    recomputed in the same commit, with the old and new digests stated in the
    commit message.

    It matters more here than on the other cards, not less. This is the FIRST
    security decision a new operator ever sees, it is posted on a Windows
    daemon's cp1252 console, and an unencodable character there is a
    UnicodeEncodeError in place of the card (DEC-32c) -- i.e. a consent surface
    that renders as a traceback.

THREE SOURCES, TWO MODULES
    `gate.from_first_gate_bulk` wrote most of the em dashes, but not all of
    them: the card quotes ``first_gate_review.batch_rule_sentence()`` (the
    CONSENT RECORD) whole, and appends ``batch_exclusion_reason(entry)`` after
    each ``[excluded:``. The second of those is itself re-derived from
    ``action_governance.migration_batch_approvable``, which is where two of the
    em dashes actually lived. All three are fixed; the property below is stated
    on the RENDERED CARD, so it holds regardless of which module wrote a given
    character.

WHAT IS EXERCISED
    The REAL seed catalogue -- migrated into a tmp vault by the real migrator,
    never the worktree's own ``systemu/vault/`` -- plus the two synthetic
    branches the real catalogue cannot reach today: an eligible batch (so the
    affirmative option's copy is rendered) and a listing long enough to force
    BOTH bounded renders, the per-tool reason clip and the "...and N more"
    disclosure.

NO PRODUCT STUB. The eligible branch is built from a genuinely batch-approvable
effect class (``screen_capture``, admitted by the 2026-08-07 operator ruling),
not by monkeypatching the predicate that decides eligibility.
"""
from __future__ import annotations

import pytest

from systemu.interface.command.gate import GateDescriptor


#: The descriptor fields an operator actually reads.
_OPERATOR_FIELDS = ("title", "inspect", "what_approve_does", "safe_default")


def _non_ascii(text):
    return sorted({c for c in text if ord(c) > 127})


def _assert_ascii(label, text):
    bad = _non_ascii(text)
    assert not bad, (
        "{}: the bulk first-gate card carries non-ASCII {} -- this is the first "
        "security decision an operator ever reads and it is posted to a cp1252 "
        "console, where an unencodable character is a UnicodeEncodeError in "
        "place of the card (DEC-32c). Use '--' for an em dash and '...' for an "
        "ellipsis.\n  {!r}".format(label, [hex(ord(c)) for c in bad], text))


def _assert_card_ascii(label, card):
    for field in _OPERATOR_FIELDS:
        _assert_ascii("{}.{}".format(label, field), getattr(card, field))
    for i, option in enumerate(card.options):
        _assert_ascii("{}.options[{}]".format(label, i), option)


def _entry(name, tags, *, tool_id=None, signature=None):
    from systemu.runtime.first_gate_review import build_entry

    return build_entry(tool_id=tool_id or ("t::" + name), name=name,
                       effect_tags=list(tags), signature=signature or ("sig::" + name))


# --------------------------------------------------------------------------- #
# the three partitions
# --------------------------------------------------------------------------- #

def _real_catalogue_partition(tmp_path):
    """The tools the product ACTUALLY ships, scored by the real migrator.

    The seed catalogue is deployed into a fresh tmp vault -- the worktree's own
    ``systemu/vault/`` is never written to. The vault dir must be named
    ``vault``: a relative implementation_path anchors at the vault root's
    parent, which is how the runtime resolves it.
    """
    from systemu.runtime.first_gate_review import (collect_backfilled_entries,
                                                   partition_entries)
    from systemu.runtime.vault_migrator import run as migrate

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    migrate(vault_dir)

    entries = collect_backfilled_entries(vault_dir)
    assert len(entries) >= 40, (
        "the seed catalogue did not deploy ({}); every assertion below would be "
        "vacuous".format(len(entries)))
    return entries, partition_entries(entries)


def _eligible_partition():
    """A partition with a REAL affirmative option, so the "what Approve does"
    branch that quotes ``OPT_BULK_ALLOW`` is rendered.

    ``screen_capture`` is genuinely batch-approvable (the 2026-08-07 operator
    ruling admits it) and genuinely REQUIRE_APPROVAL-band, so both conjuncts are
    satisfied for real. Nothing is stubbed -- a fixture that forced eligibility
    by replacing ``batch_exclusion_reason`` would also erase the excluded
    reasons this test is here to check.
    """
    from systemu.runtime.first_gate_review import partition_entries

    return partition_entries([
        _entry("take_screenshot", ["screen_capture"]),
        _entry("run_command", ["shell_exec"]),
        _entry("mystery_tool", []),
    ])


def _truncated_partition():
    """Enough excluded tools to force BOTH bounded renders: the per-tool reason
    clip (these names infer a class, which pushes the reason past
    ``MAX_REASON_CHARS``) and the "...and N more" disclosure."""
    from systemu.runtime.first_gate_review import partition_entries

    return partition_entries([
        _entry("file_scan_directory_variant_{:03d}".format(i), [])
        for i in range(120)])


def _card(partition):
    return GateDescriptor.from_first_gate_bulk(partition, version="0.10.30")


# --------------------------------------------------------------------------- #
# 1. the two sentences the OTHER module writes into the card
# --------------------------------------------------------------------------- #

def test_the_batch_rule_sentence_is_ascii():
    """The CONSENT RECORD. It states the rule actually applied and attributes
    every dated operator ruling, it is quoted WHOLE into both `inspect` and
    `what_approve_does`, and F17 rules that it never yields to the card's clip
    -- so it is the one fragment guaranteed to be on screen."""
    from systemu.runtime.first_gate_review import batch_rule_sentence

    _assert_ascii("batch_rule_sentence()", batch_rule_sentence())


def test_every_exclusion_reason_for_the_real_catalogue_is_ascii(tmp_path):
    """The per-tool "why it was excluded" clause, for every tool the product
    ships. Re-derived by the same predicate that made the decision, so this is
    the text an operator actually reads next to a refused tool."""
    from systemu.runtime.first_gate_review import batch_exclusion_reason

    entries, partition = _real_catalogue_partition(tmp_path)
    assert partition.excluded, (
        "'0 excluded' again -- the partition is not excluding anything, so no "
        "reason is rendered and this test is vacuous")

    for entry in entries:
        _assert_ascii("batch_exclusion_reason({!r})".format(entry.name),
                      batch_exclusion_reason(entry))


def test_the_exclusion_reasons_that_carried_the_em_dashes_are_ascii():
    """The two reason shapes that actually held the em dashes, named so a
    failure says which one came back. Both are minted in
    ``action_governance.migration_batch_approvable`` and reach the card through
    ``batch_exclusion_reason``:

      * the no-declaration headline ("declares no effects ... nothing was
        classified"), the COMMONEST backfill outcome;
      * the name-inferred qualifier on a class the tool did not declare.
    """
    from systemu.runtime.first_gate_review import batch_exclusion_reason

    headline = batch_exclusion_reason(_entry("mystery_tool", []))
    _assert_ascii("no-declaration headline", headline)
    assert "declares no effects" in headline, headline

    inferred = batch_exclusion_reason(_entry("delete_and_email_the_file", []))
    _assert_ascii("name-inferred qualifier", inferred)
    assert "name-inferred, not declared" in inferred, inferred


# --------------------------------------------------------------------------- #
# 2. THE CARD, on all three partitions
# --------------------------------------------------------------------------- #

def test_the_bulk_card_is_ascii_for_the_real_seed_catalogue(tmp_path):
    """THE PIN. Not a synthesized partition: the inventory the operator is
    actually shown the first time the action gate turns on."""
    entries, partition = _real_catalogue_partition(tmp_path)
    card = _card(partition)

    _assert_card_ascii("real catalogue", card)
    assert "excluded:" in card.inspect, (
        "the real catalogue rendered no exclusion clause; the reasons this "
        "test is about never reached the card")


def test_the_bulk_card_is_ascii_when_a_batch_is_offered():
    """The affirmative branch. The real catalogue may or may not offer one on
    any given day, and the copy that quotes ``OPT_BULK_ALLOW`` and promises
    what an approval covers only exists on this branch."""
    partition = _eligible_partition()
    assert partition.eligible and partition.excluded, partition

    card = _card(partition)
    _assert_card_ascii("eligible batch", card)
    assert "remembers 1 tool(s)" in card.what_approve_does, card.what_approve_does


def test_the_bulk_card_is_ascii_when_nothing_is_eligible():
    """The other affirmative branch: with 0 eligible there is no batch option
    at all, and a DIFFERENT sentence is rendered."""
    from systemu.runtime.first_gate_review import partition_entries

    partition = partition_entries([_entry("run_command", ["shell_exec"])])
    card = _card(partition)

    _assert_card_ascii("nothing eligible", card)
    assert "acknowledges this list" in card.what_approve_does
    assert len(card.options) == 1, card.options


def test_the_bulk_card_is_ascii_when_the_listing_is_clipped():
    """Both bounded renders at once -- the per-tool reason clip and the
    "...and N more" disclosure. The old ellipsis character lived in exactly
    these two places, and neither is reachable on a short card."""
    import re

    card = _card(_truncated_partition())

    _assert_card_ascii("clipped listing", card)
    # The COUNT is deliberately not pinned. How many tools survive the clip is
    # a function of MAX_CARD_CHARS and the rendered width, and "--"/"..." are
    # wider than the characters they replace -- so pinning a number here would
    # be pinning the defect's byte count. The DISCLOSURE is what matters: an
    # operator who cannot see the list was clipped reads a short list as the
    # whole inventory.
    disclosure = re.search(r"^  \.\.\.and (\d+) more \(open the Inbox",
                           card.inspect, re.MULTILINE)
    assert disclosure, (
        "the truncation disclosure did not render; the character that used to "
        "sit there was never exercised:\n" + card.inspect[-400:])
    assert int(disclosure.group(1)) > 0


# --------------------------------------------------------------------------- #
# 3. the repaired clip still behaves like a clip
# --------------------------------------------------------------------------- #

def test_the_clipped_reason_still_respects_its_cap_and_says_it_was_clipped():
    """A one-character ellipsis became three, so the clip's own arithmetic had
    to move with it. If it did not, the bounded field silently grew by two
    characters per excluded tool -- on a card that is clipped again, whole, at
    ``MAX_CARD_CHARS``.

    Pinned as a PROPERTY of the rendered line, not as an offset in the source.
    """
    from systemu.runtime.first_gate_review import MAX_REASON_CHARS

    card = _card(_truncated_partition())
    clipped = [line for line in card.inspect.splitlines() if "[excluded:" in line]
    assert clipped, "no excluded line rendered; nothing is pinned"

    saw_a_clip = False
    for line in clipped:
        why = line.split("[excluded:", 1)[1].rstrip("]").strip()
        assert len(why) <= MAX_REASON_CHARS, (
            "an exclusion reason overran its cap ({} > {}): {!r}".format(
                len(why), MAX_REASON_CHARS, why))
        if why.endswith("..."):
            saw_a_clip = True

    assert saw_a_clip, (
        "no reason was long enough to clip, so the clip branch -- where the "
        "ellipsis character lives -- was never rendered")


def test_the_repaired_bulk_lines_still_read_as_sentences():
    """`--` where an em dash was, not a deleted clause. A typography pass that
    quietly drops half a sentence is a copy defect, not a fix, and these are
    the sentences that tell an operator what a one-click approval costs."""
    eligible = _card(_eligible_partition())
    nothing = _card(_truncated_partition())

    assert "re-forging one re-gates it -- and a remembered tool is still gated" \
        in eligible.what_approve_does
    assert "just acknowledges this list -- every one of these" \
        in nothing.what_approve_does
    assert "showing the actual arguments -- and an effect that could not be " \
        "classified is reclassified there" in eligible.inspect

    listing = [line for line in eligible.inspect.splitlines()
               if line.startswith("  take_screenshot ")]
    assert listing == ["  take_screenshot -- screen_capture"], listing
