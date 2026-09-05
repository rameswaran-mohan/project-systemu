"""P2c world scan, Task 2 - the derived proposals.

Three deterministic extension-count rules, no LLM anywhere on this path.  Each
one is pinned on BOTH sides of its threshold, because a rule that only fires is
half a rule: the interesting failure is the folder that earns a proposal it
should not have.

Two fences ride on every proposal:

  * THE HONESTY WALL - `wishes.ready_tools`, exactly as the wishlist nudge uses
    it.  A starter may only be offered when the seed tool that would carry it
    is DEPLOYED and enabled.  Offering the expenses starter on an install with
    no CSV writer is promising a build, and this path builds nothing.
  * THE INPUT FENCE - a refused scan (DEC-32 value-style verdict) and anything
    that is not a `ScanResult` produce NO proposals.  The type is pinned with
    `type(x) is T` (DEC-36), the only check that cannot be dispatched around.

Nothing here persists.  A scan is session-transient, so its proposals are too -
there is no decline-forever machinery on this surface and no fact is written.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from systemu.interface import proposals
from systemu.interface.proposals import SCAN_MIN_FILES, derive_scan_proposals
from systemu.interface.world_scan import ScanResult


def _deployed(name):
    return {"id": name, "name": name, "description": name.replace("_", " "),
            "status": "deployed", "enabled": True}


#: every seed tool the three rules can require - the "everything installed" case
ALL_TOOLS = [_deployed("write_csv_file"), _deployed("write_markdown_file"),
             _deployed("file_list_dir")]

FOLDER = "/home/op/Documents"


def _result(counts, folder=FOLDER):
    return ScanResult(ok=True, refusal="", folder=folder, counts=dict(counts),
                      files=sum(counts.values()), entries=sum(counts.values()),
                      cap=2000, capped=False, outside=0)


def _keys(counts, tools=None):
    tools = ALL_TOOLS if tools is None else tools
    return [k for k, _t, _r in derive_scan_proposals(_result(counts), tools)]


# ---------------------------------------------------------------------------
#  The threshold is stated, not implied
# ---------------------------------------------------------------------------


def test_the_threshold_is_three_files():
    assert SCAN_MIN_FILES == 3


def test_there_are_exactly_three_rules():
    assert len(proposals.SCAN_RULES) == 3


def test_every_rule_requires_a_named_seed_tool():
    for rule in proposals.SCAN_RULES:
        assert rule.requires, f"{rule.key} names no capability"
        assert rule.extensions, f"{rule.key} matches no extension"


# ---------------------------------------------------------------------------
#  Rule 1 - CSV -> the expenses starter
# ---------------------------------------------------------------------------


class TestExpensesRule:
    def test_three_csv_files_earn_the_expenses_proposal(self):
        assert _keys({".csv": 3}) == ["scan:expenses"]

    def test_two_csv_files_do_not(self):
        assert _keys({".csv": 2}) == []

    def test_the_starter_names_the_scanned_folder(self):
        (_key, _text, route), = derive_scan_proposals(_result({".csv": 3}), ALL_TOOLS)
        assert FOLDER in unquote(route)

    def test_without_a_csv_writer_there_is_no_offer(self):
        """The honesty wall: no deployed `write_csv_file`, no expenses offer."""
        tools = [_deployed("write_markdown_file"), _deployed("file_list_dir")]
        assert _keys({".csv": 9}, tools) == []


# ---------------------------------------------------------------------------
#  Rule 2 - documents -> the summarize starter
# ---------------------------------------------------------------------------


class TestSummarizeRule:
    def test_three_documents_of_mixed_kinds_earn_the_summarize_proposal(self):
        assert _keys({".docx": 1, ".pdf": 1, ".md": 1}) == ["scan:summarize"]

    def test_two_documents_do_not(self):
        assert _keys({".docx": 1, ".pdf": 1}) == []

    def test_each_document_extension_counts_on_its_own(self):
        for ext in (".docx", ".pdf", ".md"):
            assert _keys({ext: 3}) == ["scan:summarize"], ext

    def test_without_a_markdown_writer_there_is_no_offer(self):
        tools = [_deployed("write_csv_file"), _deployed("file_list_dir")]
        assert _keys({".pdf": 9}, tools) == []


# ---------------------------------------------------------------------------
#  Rule 3 - images -> the file-index starter
# ---------------------------------------------------------------------------


class TestFileIndexRule:
    def test_three_images_earn_the_file_index_proposal(self):
        assert _keys({".png": 2, ".jpg": 1}) == ["scan:file_index"]

    def test_two_images_do_not(self):
        assert _keys({".png": 1, ".jpg": 1}) == []

    def test_without_a_directory_lister_there_is_no_offer(self):
        tools = [_deployed("write_csv_file"), _deployed("write_markdown_file")]
        assert _keys({".png": 9}, tools) == []


# ---------------------------------------------------------------------------
#  Shape - at most three, deterministic order, honest text
# ---------------------------------------------------------------------------


class TestShape:
    def test_a_folder_that_earns_everything_gets_all_three_in_rule_order(self):
        got = _keys({".csv": 3, ".pdf": 3, ".png": 3})
        assert got == ["scan:expenses", "scan:summarize", "scan:file_index"]

    def test_never_more_than_three_proposals(self):
        assert len(derive_scan_proposals(
            _result({".csv": 9, ".pdf": 9, ".png": 9, ".md": 9, ".docx": 9}),
            ALL_TOOLS)) <= 3

    def test_the_order_does_not_depend_on_the_counts(self):
        assert _keys({".png": 90, ".csv": 3}) == ["scan:expenses", "scan:file_index"]

    def test_each_proposal_is_the_engine_triple(self):
        for key, text, route in derive_scan_proposals(
                _result({".csv": 3, ".pdf": 3, ".png": 3}), ALL_TOOLS):
            assert key.startswith("scan:")
            assert text and text.isascii()
            assert route.startswith("/chat?prefill=")
            assert FOLDER in unquote(route)

    def test_the_text_states_the_count_it_found(self):
        (_k, text, _r), = derive_scan_proposals(_result({".csv": 7}), ALL_TOOLS)
        assert "7" in text

    def test_a_folder_with_odd_characters_survives_the_route_quoting(self):
        weird = "/home/op/My Docs & Stuff/q?x=1"
        got = derive_scan_proposals(
            ScanResult(ok=True, refusal="", folder=weird, counts={".csv": 3},
                       files=3, entries=3, cap=2000, capped=False, outside=0),
            ALL_TOOLS)
        (_k, _t, route), = got
        assert route.count("?") == 1, "the folder must not open a second query"
        assert weird in unquote(route)

    def test_the_route_is_built_by_the_shared_prefill_helper(self):
        """One definition of what a prefill route is - reused, not re-derived."""
        from systemu.interface.components.command_palette import prefill_target
        (_k, _t, route), = derive_scan_proposals(_result({".csv": 3}), ALL_TOOLS)
        starter = unquote(route.split("prefill=", 1)[1])
        assert route == prefill_target(starter)


# ---------------------------------------------------------------------------
#  The input fence
# ---------------------------------------------------------------------------


class TestInputFence:
    def test_a_refused_scan_proposes_nothing(self):
        refused = ScanResult(ok=False, refusal="no_such_path")
        assert derive_scan_proposals(refused, ALL_TOOLS) == []

    def test_a_value_that_is_not_a_scan_result_proposes_nothing(self):
        class Lookalike:
            ok = True
            refusal = ""
            folder = FOLDER
            counts = {".csv": 99}

        for junk in (None, {}, "ok", 7, Lookalike()):
            assert derive_scan_proposals(junk, ALL_TOOLS) == [], junk

    def test_an_empty_toolbox_proposes_nothing(self):
        assert derive_scan_proposals(_result({".csv": 9}), []) == []
        assert derive_scan_proposals(_result({".csv": 9}), None) == []

    def test_a_forged_but_undeployed_tool_is_not_a_capability(self):
        """`ready_tools`, verbatim: a plan is not something the toolbox can do."""
        tools = [{"name": "write_csv_file", "status": "forged", "enabled": True}]
        assert derive_scan_proposals(_result({".csv": 9}), tools) == []

    def test_a_disabled_tool_is_not_a_capability(self):
        tools = [{"name": "write_csv_file", "status": "deployed", "enabled": False}]
        assert derive_scan_proposals(_result({".csv": 9}), tools) == []

    def test_malformed_tool_rows_are_skipped_not_fatal(self):
        tools = ["junk", None, 7, {}, _deployed("write_csv_file")]
        assert _keys({".csv": 3}, tools) == ["scan:expenses"]

    def test_an_empty_folder_proposes_nothing(self):
        assert derive_scan_proposals(_result({}), ALL_TOOLS) == []


# ---------------------------------------------------------------------------
#  Nothing on this path persists
# ---------------------------------------------------------------------------


def test_the_scan_path_writes_no_facts_and_no_table_items():
    """A scan is session-transient, so its proposals are too.  `decline` /
    `add_fact` belong to Home's one-per-render engine, not to this one, and the
    OnTheTable store keeps its single writer (DEC-10)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(derive_scan_proposals))
    called = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)
    for forbidden in ("add_fact", "decline", "add_operator_item", "save_items",
                      "add_tombstone", "set_pin", "add_accepted"):
        assert forbidden not in called, (
            f"derive_scan_proposals calls {forbidden} - scan proposals persist "
            "nothing"
        )


def test_homes_one_per_render_engine_is_untouched(tmp_path):
    """The scan proposals render inline on the Table page.  Home's
    `derive_proposal` keeps its own candidates and its own one-per-render
    contract - this item must not have widened it."""
    import inspect

    src = inspect.getsource(proposals.derive_proposal)
    assert "scan" not in src.lower()


def test_the_module_is_ascii_only():
    assert Path(proposals.__file__).read_text(encoding="utf-8").isascii()
