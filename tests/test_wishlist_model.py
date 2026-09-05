"""Capability Wishlist v1, Task 1 - the wish model.

A wish is an operator-AUTHORED user fact (`tags=["wish"]`, `source="table"`) in
the existing append-only fact store.  There is no new store and nothing here
ever writes OnTheTable - `table_reconciler.project()` stays its sole writer.

These tests pin four things:

  * THE FACT CONTRACT - prefix, tags, source, blank refusal, newest-first read.
  * DISMISSAL IS A SUPERSEDE - it goes through the existing `forget_fact` API,
    so the wish stays in the log (append-only) and simply stops being open.
  * MATCHING IS DUMB AND DECIDABLE - >= 2 significant words (len > 3, lowercased,
    stopword-stripped) must appear in a tool's name+description.  Both a hit and
    a near-miss are pinned, so the threshold cannot drift silently.
  * THE HONESTY WALL - `ready_tools` is what stops a nudge naming a tool that is
    not actually usable today; a `proposed`/`forged` or disabled tool is not a
    fulfilled wish, it is a promise.
"""
from __future__ import annotations

from pathlib import Path

from systemu.interface import wishes
from systemu.interface.wishes import (
    add_wish,
    dismiss_wish,
    match_wish,
    open_wishes,
    ready_tools,
)
from systemu.runtime.user_profile import add_fact, get_facts


class FakeVault:
    """Facts live at `Path(vault.root)/user_facts.jsonl` - that is the whole
    surface the model uses."""

    def __init__(self, root):
        self.root = str(root)


class BrokenVault:
    """Every read of `root` raises.  The wishlist must degrade, never crash a
    page that merely wanted to show a list."""

    @property
    def root(self):
        raise RuntimeError("vault down")


def _deployed(name, description):
    return {"id": name, "name": name, "description": description,
            "status": "deployed", "enabled": True}


# ---------------------------------------------------------------------------
#  add_wish - the fact contract
# ---------------------------------------------------------------------------


class TestAddWish:
    def test_a_wish_is_a_prefixed_tagged_fact_sourced_from_the_table(self, tmp_path):
        v = FakeVault(tmp_path)
        fid = add_wish(v, "send my weekly invoice reminders by email")

        facts = get_facts(v, tags=["wish"])
        assert len(facts) == 1
        f = facts[0]
        assert f.id == fid
        assert f.fact.startswith("wish:")
        assert "send my weekly invoice reminders by email" in f.fact
        assert f.source == "table"
        assert "wish" in f.tags

    def test_surrounding_whitespace_is_stripped(self, tmp_path):
        v = FakeVault(tmp_path)
        add_wish(v, "   tidy my downloads folder   ")
        assert open_wishes(v)[0][1] == "tidy my downloads folder"

    def test_a_blank_wish_is_refused_and_writes_nothing(self, tmp_path):
        v = FakeVault(tmp_path)
        for blank in ("", "   ", "\t\n", None):
            assert add_wish(v, blank) is None
        assert get_facts(v, tags=["wish"]) == []
        assert not (Path(tmp_path) / "user_facts.jsonl").exists()

    def test_a_wish_containing_a_colon_survives_the_round_trip(self, tmp_path):
        v = FakeVault(tmp_path)
        add_wish(v, "remind me at 09:00 about standup")
        assert open_wishes(v)[0][1] == "remind me at 09:00 about standup"

    def test_a_broken_vault_refuses_rather_than_raises(self):
        assert add_wish(BrokenVault(), "anything at all") is None


# ---------------------------------------------------------------------------
#  open_wishes - newest first, superseded excluded, wish facts only
# ---------------------------------------------------------------------------


class TestOpenWishes:
    def test_a_cold_vault_has_no_wishes(self, tmp_path):
        assert open_wishes(FakeVault(tmp_path)) == []

    def test_wishes_come_back_newest_first_as_id_text_pairs(self, tmp_path):
        v = FakeVault(tmp_path)
        first = add_wish(v, "file my receipts")
        second = add_wish(v, "draft my monday status note")

        got = open_wishes(v)
        assert got == [(second, "draft my monday status note"),
                       (first, "file my receipts")]

    def test_other_facts_are_not_mistaken_for_wishes(self, tmp_path):
        v = FakeVault(tmp_path)
        add_fact(v, "Usage persona: Freelance", source="settings", tags=["persona"])
        add_wish(v, "file my receipts")
        assert [t for _i, t in open_wishes(v)] == ["file my receipts"]

    def test_a_broken_vault_reads_as_no_wishes(self):
        assert open_wishes(BrokenVault()) == []


# ---------------------------------------------------------------------------
#  dismiss_wish - the existing supersede API, nothing new
# ---------------------------------------------------------------------------


class TestDismissWish:
    def test_a_dismissed_wish_stops_being_open_but_stays_in_the_log(self, tmp_path):
        v = FakeVault(tmp_path)
        keep = add_wish(v, "file my receipts")
        drop = add_wish(v, "draft my monday status note")

        assert dismiss_wish(v, drop) is True
        assert open_wishes(v) == [(keep, "file my receipts")]
        # append-only: the fact is superseded, not deleted
        assert len(get_facts(v, tags=["wish"], include_superseded=True)) == 2

    def test_dismissing_an_unknown_id_is_a_false_no_op(self, tmp_path):
        v = FakeVault(tmp_path)
        keep = add_wish(v, "file my receipts")
        assert dismiss_wish(v, "fact_deadbeef") is False
        assert open_wishes(v) == [(keep, "file my receipts")]

    def test_a_blank_id_is_refused(self, tmp_path):
        v = FakeVault(tmp_path)
        add_wish(v, "file my receipts")
        assert dismiss_wish(v, "") is False
        assert dismiss_wish(v, None) is False
        assert len(open_wishes(v)) == 1

    def test_a_broken_vault_reports_failure_rather_than_raising(self):
        assert dismiss_wish(BrokenVault(), "fact_1234abcd") is False


# ---------------------------------------------------------------------------
#  ready_tools - the honesty wall
# ---------------------------------------------------------------------------


class TestReadyTools:
    def test_only_deployed_or_upgraded_and_enabled_tools_are_ready(self):
        tools = [
            {"name": "a", "status": "deployed", "enabled": True},
            {"name": "b", "status": "upgraded", "enabled": True},
            {"name": "c", "status": "proposed", "enabled": True},
            {"name": "d", "status": "forged", "enabled": True},
            {"name": "e", "status": "tested", "enabled": True},
            {"name": "f", "status": "deployed", "enabled": False},
            {"name": "g"},
        ]
        assert [t["name"] for t in ready_tools(tools)] == ["a", "b"]

    def test_junk_input_reads_as_nothing_ready(self):
        assert ready_tools(None) == []
        assert ready_tools([]) == []
        assert ready_tools(["not-a-dict", 7, None]) == []


# ---------------------------------------------------------------------------
#  match_wish - >= 2 significant words, deterministic, dumb on purpose
# ---------------------------------------------------------------------------


_INVOICE_WISH = "send my weekly invoice reminders by email"


class TestMatchWish:
    def test_two_significant_words_is_a_hit(self):
        # "invoice" and "email" both land; that is exactly the threshold.
        tool = _deployed("invoice_reminder",
                         "Email a reminder for an unpaid invoice.")
        assert match_wish(_INVOICE_WISH, [tool]) is tool

    def test_one_significant_word_is_a_near_miss(self):
        # only "invoice" lands -> below the threshold -> no nudge at all.
        tool = _deployed("invoice_lookup", "Look up a customer record.")
        assert match_wish(_INVOICE_WISH, [tool]) is None

    def test_short_words_and_stopwords_never_count(self):
        # "my", "by" are too short; "send"/"weekly"/"reminders" are absent.
        # The tool text is stuffed with stopwords and 3-letter words only.
        tool = _deployed("noop", "I want to have some of my things by the way.")
        assert match_wish(_INVOICE_WISH, [tool]) is None

    def test_the_best_overlap_wins(self):
        weak = _deployed("invoice_email", "Email an invoice.")
        strong = _deployed("weekly_invoice_email",
                           "Email a weekly invoice reminders digest.")
        assert match_wish(_INVOICE_WISH, [weak, strong]) is strong
        assert match_wish(_INVOICE_WISH, [strong, weak]) is strong

    def test_a_tie_resolves_to_the_first_tool_in_the_list(self):
        one = _deployed("alpha", "Email an invoice.")
        two = _deployed("beta", "Email an invoice.")
        assert match_wish(_INVOICE_WISH, [one, two]) is one

    def test_an_empty_wish_or_an_empty_toolbox_matches_nothing(self):
        assert match_wish("", [_deployed("a", "Email an invoice.")]) is None
        assert match_wish(None, [_deployed("a", "Email an invoice.")]) is None
        assert match_wish(_INVOICE_WISH, []) is None
        assert match_wish(_INVOICE_WISH, None) is None

    def test_malformed_tool_entries_are_skipped_not_fatal(self):
        good = _deployed("invoice_reminder",
                         "Email a reminder for an unpaid invoice.")
        assert match_wish(_INVOICE_WISH, ["junk", None, 7, {}, good]) is good


# ---------------------------------------------------------------------------
#  Module-level locks
# ---------------------------------------------------------------------------


def test_the_module_is_ascii_only():
    assert Path(wishes.__file__).read_text(encoding="utf-8").isascii()


def test_the_model_never_reaches_for_the_onthetable_store():
    """Wishes are facts.  Nothing here may write the projected inventory - its
    sole writer stays the reconciler."""
    src = Path(wishes.__file__).read_text(encoding="utf-8")
    for forbidden in ("table_store", "TableItem", "save_items"):
        assert forbidden not in src, (
            f"wishes.py must not touch {forbidden} - a wish is a user fact, "
            "never a projected table item"
        )


def test_the_match_threshold_is_stated_not_implied():
    assert wishes.MATCH_MIN_WORDS == 2


# ---------------------------------------------------------------------------
#  The BEST match, deterministically - whatever order the index arrived in
# ---------------------------------------------------------------------------


#: The live wish that exposed the defect.  Its significant words are
#: ("write", "csv", "file", "expenses") - "csv" is three characters and clears
#: the length floor only because it is listed in `_SHORT_SIGNIFICANT`, and it
#: is the single most discriminating word the operator typed.
_CSV_WISH = "write a csv file for my expenses"


class TestTheBestMatchIsDeterministic:
    """Live defect: this wish named `file_write` over `write_csv_file`.  Two
    things were wrong.  The wish's sharpest word, "csv", was thrown away by the
    length floor, so the pair scored 2 all square; and the square was then
    settled by whichever tool the index happened to list first.  A tool index
    is assembled from a listing and carries no ordering contract, so a
    position-decided winner can change between two renders of the same wish.
    Count the format vocabulary; rank by hit count; break a tie by tool name,
    never by position.
    """

    def test_the_csv_wish_resolves_to_the_csv_writer_in_both_orders(self):
        file_write = _deployed(
            "file_write", "Write text content to a file at a given path.")
        write_csv_file = _deployed(
            "write_csv_file", "Create a CSV spreadsheet from rows of data.")
        # 3 hits ("write", "csv", "file") against 2: dropping "csv" was what
        # flattened these two into a tie and handed the answer to list order.
        assert (match_wish(_CSV_WISH, [file_write, write_csv_file])
                is write_csv_file)
        assert (match_wish(_CSV_WISH, [write_csv_file, file_write])
                is write_csv_file)

    def test_more_hits_beat_an_alphabetically_earlier_name(self):
        file_write = _deployed(
            "file_write", "Write text content to a file at a given path.")
        write_csv_file = _deployed(
            "write_csv_file",
            "Write rows of data to a CSV file, such as an expenses ledger.")
        # 3 hits ("write", "file", "expenses") against 2.  The count is the
        # first key, so the name tie-break may never outrank a better overlap
        # even though "file_write" sorts earlier.
        assert (match_wish(_CSV_WISH, [file_write, write_csv_file])
                is write_csv_file)
        assert (match_wish(_CSV_WISH, [write_csv_file, file_write])
                is write_csv_file)

    def test_a_missing_or_unstringy_name_ranks_instead_of_raising(self):
        # The tie-break reads a field the index may simply not carry.  A
        # wishlist that raised would take down the page it renders on.
        nameless = {"description": "Write rows to a file of expenses.",
                    "status": "deployed", "enabled": True}
        numbered = {"name": 7, "description": "Write a file of expenses.",
                    "status": "deployed", "enabled": True}
        named = _deployed("zzz_last", "Write a file of expenses.")
        picked = match_wish(_CSV_WISH, [named, nameless, numbered])
        assert picked is nameless          # "" sorts before "7" and "zzz_last"
        assert match_wish(_CSV_WISH, [nameless, numbered, named]) is nameless

    def test_a_genuine_tie_is_broken_by_name_not_by_index_order(self):
        """The tie-break itself, held on words the short-token list does not
        reach - so no later edit to that list can quietly stop this from being
        a tie and leave the guarantee untested."""
        wish = "summarize my quarterly board minutes"
        digest = _deployed("board_minutes_digest",
                           "Digest a meeting into a short recap.")
        summarize = _deployed("minutes_summarize",
                              "Summarize a set of meeting notes.")
        # Each lands exactly two of ("summarize", "quarterly", "board",
        # "minutes"); the name, not the position, settles it.
        assert match_wish(wish, [digest, summarize]) is digest
        assert match_wish(wish, [summarize, digest]) is digest


# ---------------------------------------------------------------------------
#  The short high-signal tokens - a CLOSED exception, not a lower floor
# ---------------------------------------------------------------------------


class TestShortSignificantTokens:
    """The length floor exists to keep noise out, and lowering it globally
    would have loosened every match on the page.  The exception is enumerated
    instead: a closed set of format and protocol vocabulary, which is exactly
    the three-letter word an operator uses to say what they actually want.
    """

    def test_the_list_is_closed_and_stated(self):
        # Pinned by value: growing it is a deliberate edit with a test change,
        # never a quiet widening of what counts as a capability word.
        assert wishes._SHORT_SIGNIFICANT == frozenset({
            "csv", "pdf", "zip", "png", "jpg", "gif", "svg", "xml", "sql",
            "api", "url", "ocr",
        })
        assert type(wishes._SHORT_SIGNIFICANT) is frozenset

    def test_every_listed_token_actually_needs_the_exception(self):
        """A member long enough to clear the floor on its own, or one that the
        stopword filter would drop anyway, would be dead weight pretending to
        be a rule."""
        for tok in wishes._SHORT_SIGNIFICANT:
            assert tok == tok.lower(), tok
            assert len(tok) < wishes._MIN_WORD_LEN, tok
            assert tok not in wishes._STOPWORDS, tok

    def test_every_listed_token_counts_as_significant(self):
        for tok in wishes._SHORT_SIGNIFICANT:
            assert tok in wishes.significant_words(f"export a {tok} report"), tok

    def test_an_unlisted_three_letter_word_still_counts_for_nothing(self):
        # "the", "tax", "box", "bob" are all three letters and none are listed.
        assert wishes.significant_words("send the tax box to bob") == ["send"]

    def test_the_exception_does_not_lower_the_two_word_threshold(self):
        # One significant word is one significant word, whatever its length:
        # a lone format token is still a near miss, not a "you wished for this".
        tool = _deployed("pdf_merge", "Merge several PDF documents into one.")
        assert match_wish("a pdf", [tool]) is None
        assert match_wish("merge a pdf", [tool]) is tool
