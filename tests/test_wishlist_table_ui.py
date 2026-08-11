"""Capability Wishlist v1, Task 2 - capture + list on the Table page.

The wishlist section is a fact READER with two operator-driven writes
(`add_wish`, `dismiss_wish`).  It must never become a second writer of the
projected inventory: `table_reconciler.project()` stays the sole writer of
`items.json`, and a wish is a user fact that merely renders next to it.

These are mostly reachability pins in the GTM sense - delete the production
call site and a NAMED test goes red.  A correct model nobody renders is the
half-built shape this project keeps paying for.
"""
from __future__ import annotations

import ast
import inspect

from systemu.interface.pages import table
from systemu.interface.wishes import add_wish, dismiss_wish, open_wishes


class FakeVault:
    def __init__(self, root):
        self.root = str(root)


def _page_src() -> str:
    return inspect.getsource(table.build_table_page)


def _called_names(src: str) -> set:
    tree = ast.parse(src)
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


# ---------------------------------------------------------------------------
#  Reachability - the page actually reaches every helper
# ---------------------------------------------------------------------------


def test_the_table_page_reaches_every_wishlist_helper():
    """Delete any of these call sites and this test reds."""
    called = _called_names(_page_src())
    assert {"add_wish", "open_wishes", "dismiss_wish"} <= called, sorted(called)


def test_the_page_takes_the_helpers_from_the_model_module():
    """Imported, not re-implemented - one definition of what a wish is."""
    assert "from systemu.interface.wishes import" in inspect.getsource(table)


def test_the_wishlist_sits_under_the_board():
    src = _page_src()
    assert src.rindex("_board()") < src.rindex("_wishlist()")


# ---------------------------------------------------------------------------
#  The section is a READER of the table, never a writer of it
# ---------------------------------------------------------------------------


_TABLE_WRITERS = {
    "add_operator_item", "make_operator_item", "add_accepted", "add_tombstone",
    "remove_tombstone", "set_pin", "save_items", "project", "commit",
}


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


def test_the_wish_handlers_never_touch_the_onthetable_store():
    """DEC-10 single-writer: a wish is a user fact.  If a wish handler ever
    calls a table-store writer, the wishlist has quietly become a second
    writer of the projected inventory."""
    for handler in ("_on_add_wish", "_on_dismiss_wish"):
        offenders = _handler_calls(handler) & _TABLE_WRITERS
        assert not offenders, (
            f"{handler} calls table writer(s) {sorted(offenders)} - the wishlist "
            "section reads facts only"
        )


def test_the_wishlist_renderer_reads_facts_only():
    called = _handler_calls("_wishlist")
    assert "open_wishes" in called
    assert not (called & _TABLE_WRITERS)


# ---------------------------------------------------------------------------
#  Copy - the plan's wording, ASCII, and honest about what a wish is
# ---------------------------------------------------------------------------


def test_the_capture_prompt_is_the_planned_question():
    assert "What do you wish Systemu could do?" in inspect.getsource(table)


def test_the_empty_state_is_the_planned_copy():
    assert table.WISHLIST_EMPTY == (
        "Nothing wished yet - write down what you want Systemu to learn to do, "
        "and it will tell you when it can."
    )


def test_every_wishlist_string_constant_is_ascii():
    for name in dir(table):
        if not name.startswith("WISHLIST"):
            continue
        val = getattr(table, name)
        assert isinstance(val, str) and val.isascii(), name


def test_the_wishlist_copy_promises_nothing_it_cannot_do():
    """v1 has no forge trigger: writing a wish down does NOT queue any build.
    The copy must not say it does."""
    src = inspect.getsource(table)
    start = src.index("WISHLIST_EMPTY")
    blob = src[start:start + 4000].lower()
    for lie in ("i will build", "we will build", "queued", "coming soon"):
        assert lie not in blob, f"wishlist copy promises {lie!r}"


# ---------------------------------------------------------------------------
#  Behaviour through the helpers the section calls
# ---------------------------------------------------------------------------


def test_capture_then_list_then_dismiss_round_trips(tmp_path):
    v = FakeVault(tmp_path)
    first = add_wish(v, "tidy my downloads folder")
    second = add_wish(v, "send my weekly invoice reminders by email")

    # what the section renders: newest first
    assert [t for _i, t in open_wishes(v)] == [
        "send my weekly invoice reminders by email",
        "tidy my downloads folder",
    ]

    assert dismiss_wish(v, second) is True
    assert open_wishes(v) == [(first, "tidy my downloads folder")]


def test_a_blank_capture_adds_nothing(tmp_path):
    v = FakeVault(tmp_path)
    assert add_wish(v, "   ") is None
    assert open_wishes(v) == []
