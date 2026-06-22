"""Plan 0 Build 2 Task 2.5 — pure-logic tests for the Tools registry
``forged_by_systemu`` ("Agent-built") filter + the row-model flag surfacing.

Only the headless / pure-data layer is covered (no NiceGUI runtime):
  * ``_filter_tools`` with each ``forged_by_systemu_filter`` value (yes/no/all),
    plus a check that existing query/status behaviour is untouched.
  * ``tool_row_model`` surfacing ``forged_by_systemu`` as a bool.
"""
from systemu.interface.pages.tools import _filter_tools
from systemu.interface.components.entity_rows import tool_row_model


# A small registry of dict rows (the shape of vault.load_index('tools') items).
_ROWS = [
    {"id": "t1", "name": "alpha", "status": "forged", "forged_by_systemu": True},
    {"id": "t2", "name": "beta", "status": "deployed", "forged_by_systemu": False},
    {"id": "t3", "name": "gamma", "status": "forged"},  # missing key → falsy
    {"id": "t4", "name": "delta", "status": "deployed", "forged_by_systemu": True},
]


def _ids(rows):
    return [r["id"] for r in rows]


# ── _filter_tools: forged_by_systemu_filter ───────────────────────────────────

def test_filter_forged_by_systemu_yes_keeps_only_agent_built():
    out = _filter_tools(_ROWS, "", "all", forged_by_systemu_filter="yes")
    assert _ids(out) == ["t1", "t4"]


def test_filter_forged_by_systemu_no_keeps_only_the_rest():
    out = _filter_tools(_ROWS, "", "all", forged_by_systemu_filter="no")
    # t2 (explicit False) and t3 (missing key) are NOT agent-built.
    assert _ids(out) == ["t2", "t3"]


def test_filter_forged_by_systemu_all_is_no_filter():
    out = _filter_tools(_ROWS, "", "all", forged_by_systemu_filter="all")
    assert _ids(out) == ["t1", "t2", "t3", "t4"]


def test_filter_forged_by_systemu_empty_string_is_no_filter():
    out = _filter_tools(_ROWS, "", "all", forged_by_systemu_filter="")
    assert _ids(out) == ["t1", "t2", "t3", "t4"]


def test_filter_forged_by_systemu_defaults_to_all():
    # The new param is optional — omitting it must not change existing behaviour.
    out = _filter_tools(_ROWS, "", "all")
    assert _ids(out) == ["t1", "t2", "t3", "t4"]


# ── _filter_tools: existing query/status behaviour is preserved ───────────────

def test_filter_query_still_matches_name():
    out = _filter_tools(_ROWS, "alph", "all")
    assert _ids(out) == ["t1"]


def test_filter_status_still_matches_exactly():
    out = _filter_tools(_ROWS, "", "forged")
    assert _ids(out) == ["t1", "t3"]


def test_filter_combines_status_and_forged_by_systemu():
    # forged status AND agent-built → only t1 (t3 is forged but not agent-built).
    out = _filter_tools(_ROWS, "", "forged", forged_by_systemu_filter="yes")
    assert _ids(out) == ["t1"]


def test_filter_combines_query_and_forged_by_systemu_no():
    out = _filter_tools(_ROWS, "delta", "all", forged_by_systemu_filter="yes")
    assert _ids(out) == ["t4"]
    out = _filter_tools(_ROWS, "delta", "all", forged_by_systemu_filter="no")
    assert _ids(out) == []


# ── tool_row_model surfaces the flag ──────────────────────────────────────────

def test_tool_row_model_surfaces_forged_by_systemu_true():
    m = tool_row_model({"id": "t1", "name": "alpha", "forged_by_systemu": True})
    assert m["forged_by_systemu"] is True


def test_tool_row_model_forged_by_systemu_false_when_explicit():
    m = tool_row_model({"id": "t2", "name": "beta", "forged_by_systemu": False})
    assert m["forged_by_systemu"] is False


def test_tool_row_model_forged_by_systemu_false_when_missing():
    m = tool_row_model({"id": "t3", "name": "gamma"})
    assert m["forged_by_systemu"] is False


def test_tool_row_model_forged_by_systemu_is_a_real_bool():
    # Truthy non-bool values (e.g. an execution-id string) must coerce to bool.
    m = tool_row_model({"id": "t5", "name": "eps", "forged_by_systemu": "exec_123"})
    assert m["forged_by_systemu"] is True
    assert isinstance(m["forged_by_systemu"], bool)
