"""P2c world scan, Task 3 - the "Scan a folder" card on the Table page.

Reachability pins in the GTM sense: delete a production call site and a NAMED
test here goes red.  A scan model nobody renders is the half-built shape this
project keeps paying for, and a privacy card whose copy outruns its code is
worse than no card at all.

What is pinned:

  * THE TWO CLAIMS render verbatim.  `SCAN_BLURB` is compared character for
    character against the sentence the model's source-purity tests make true.
  * THE PAGE REACHES the model and the rule engine (`scan_folder`,
    `derive_scan_proposals`) and the two minted sentences (`summary_line`,
    `refusal_message`) - so a refusal and a cap can never render as silence.
  * THE CARD PERSISTS NOTHING.  No table-store writer, no `add_fact`, no
    `decline`: the scan is session-transient and so is a dismissal of one of
    its proposals.  `table_reconciler.project()` stays the sole writer of the
    projected inventory (DEC-10).
"""
from __future__ import annotations

import ast
import inspect

from systemu.interface.pages import table


def _page_src() -> str:
    return inspect.getsource(table.build_table_page)


def _called_names(src: str) -> set:
    tree = ast.parse(src)
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _handler_calls(name: str) -> set:
    """Every callee (bare name or attribute) inside a nested handler of the
    page function."""
    tree = ast.parse(_page_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            out = set()
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                if isinstance(f, ast.Attribute):
                    out.add(f.attr)
                elif isinstance(f, ast.Name):
                    out.add(f.id)
            return out
    raise AssertionError(f"{name}() is not defined inside build_table_page")


# ---------------------------------------------------------------------------
#  Reachability
# ---------------------------------------------------------------------------


def test_the_table_page_reaches_the_scan_model():
    """Delete the `scan_folder` call site and this test reds."""
    assert "scan_folder" in _called_names(_page_src())


def test_the_table_page_reaches_the_scan_proposal_engine():
    """Delete the `derive_scan_proposals` call site and this test reds."""
    assert "derive_scan_proposals" in _called_names(_page_src())


def test_the_page_renders_both_minted_sentences():
    """A refusal and a capped count are the two things an operator MUST see;
    minting them and never rendering them is the half-built shape."""
    called = _called_names(_page_src())
    assert {"summary_line", "refusal_message"} <= called, sorted(called)


def test_the_page_takes_the_scan_helpers_from_the_model_module():
    """Imported, not re-implemented - one definition of what a scan is."""
    assert "from systemu.interface.world_scan import" in inspect.getsource(table)


def test_the_page_takes_the_rules_from_the_proposals_engine():
    assert "from systemu.interface.proposals import" in inspect.getsource(table)


def test_the_scan_card_sits_between_the_board_and_the_wishlist():
    src = _page_src()
    assert (src.rindex("_board()")
            < src.rindex("_scan_card()")
            < src.rindex("_wishlist()"))


def test_the_path_box_is_outside_the_refreshable_result_region():
    """A refreshable rebuilds everything it owns.  With the path box inside
    `_scan`, every completed scan would blank the field the operator just
    typed into - and steal focus if they were mid-typing, the same trap the
    consult's rename handler documents.  Found by driving the real card."""
    assert "input" not in _handler_calls("_scan"), (
        "the path box is inside `_scan`, so a result refresh wipes it"
    )
    assert "input" in _handler_calls("_scan_card"), (
        "`_scan_card` no longer owns the path box"
    )


def test_the_scan_reads_the_toolbox_so_the_honesty_wall_has_input():
    """`derive_scan_proposals` can only refuse to over-promise if it is handed
    the real tool index; passing it nothing would silence every proposal and
    passing it a guess would resurrect the promise."""
    assert "list_tools" in _handler_calls("_scan")


# ---------------------------------------------------------------------------
#  The card persists nothing
# ---------------------------------------------------------------------------


_TABLE_WRITERS = {
    "add_operator_item", "make_operator_item", "add_accepted", "add_tombstone",
    "remove_tombstone", "set_pin", "save_items", "project", "commit",
}

_PERSISTERS = _TABLE_WRITERS | {"add_fact", "decline", "add_wish",
                                "dismiss_wish", "forget_fact"}


def test_no_scan_handler_persists_anything():
    """The scan itself is transient, so decline-forever machinery does not
    apply here: a dismissal is a card dismissal for this render only."""
    for handler in ("_on_scan", "_on_dismiss_scan", "_scan", "_scan_card"):
        offenders = _handler_calls(handler) & _PERSISTERS
        assert not offenders, (
            f"{handler} calls {sorted(offenders)} - a folder scan and its "
            "proposals are session-transient and persist nothing"
        )


def test_the_scan_never_becomes_a_second_writer_of_the_table():
    offenders = _handler_calls("_scan") & _TABLE_WRITERS
    assert not offenders, sorted(offenders)


# ---------------------------------------------------------------------------
#  The copy - exactly the two claims the model makes true
# ---------------------------------------------------------------------------


def test_the_privacy_claim_is_the_planned_copy_verbatim():
    assert table.SCAN_BLURB == (
        "Reads file names and extensions only - never file contents. "
        "Nothing leaves this machine."
    )


def test_the_card_is_titled_scan_a_folder():
    assert table.SCAN_TITLE == "Scan a folder"


def test_the_scan_needs_an_explicit_button_press():
    """Consent-first: nothing is scanned until the operator asks for it."""
    assert table.SCAN_BUTTON == "Scan"
    src = _page_src()
    assert "_on_scan" in src


def test_the_card_says_the_result_is_not_kept():
    assert "not saved" in table.SCAN_TRANSIENT.lower()


def test_every_scan_string_constant_is_ascii():
    for name in dir(table):
        if not name.startswith("SCAN"):
            continue
        val = getattr(table, name)
        assert isinstance(val, str) and val.isascii(), name


def test_the_scan_copy_promises_nothing_it_cannot_do():
    """No upload, no cloud, no remembering - the card must not say otherwise."""
    blob = " ".join(str(getattr(table, n)) for n in dir(table)
                    if n.startswith("SCAN")).lower()
    for lie in ("upload", "cloud", "we store", "saved to your", "sent to",
                "remembers", "history"):
        assert lie not in blob, f"scan copy promises {lie!r}"
