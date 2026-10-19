"""T5 -- the three SURFACES that show "answered from your table" (reachability pins).

Each surface reads the DURABLE per-run record through ``table_payoff_record.find_run``
(a stat and a small file read, never a blocking call) and renders
``table_payoff.run_chip_model`` with token classes only. For each one there is a
behavioural test over a record written by the REAL recorder, and a pin that the
surface's render path still calls the chip renderer -- delete the call and a named
test goes red.

The audit data root is pointed at ``tmp_path`` (cwd + no vault env), exactly as the
production resolver would find it; nothing is written into the worktree.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from systemu.core.models import Requirement, RequirementReport
from systemu.runtime import table_payoff_record as tpr
from systemu.runtime.situational_inventory import SituationReport, compose_table
from systemu.runtime.table_store import TableItem
from systemu.runtime.workflow_tracker import WorkflowSnapshot


@pytest.fixture()
def audit_home(tmp_path, monkeypatch):
    from systemu.runtime import vault_root as vr
    monkeypatch.delenv(vr.VAULT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "data"


def _record(audit_home, *, root="exec_w1", eid=None, chat=None, name="recipient"):
    """One table-supplied requirement, through the REAL recorder."""
    item = TableItem(id="t-1", kind="service", name=name,
                     ref={"server": "https://example.invalid/t1"})
    situation = compose_table(SituationReport(), [item])
    req = Requirement(kind="decision", schema_path="recipient", state="have",
                      source="situation", value_origin="content_derived",
                      confidence=0.8, table_item_id="t-1")
    status = tpr.record_binder_pass(
        RequirementReport(per_objective={1: [req]}), situation,
        root_execution_id=root, execution_id=eid or root, site="midloop",
        chat_submission_id=chat, data_dir=audit_home)
    assert status["written"] >= 1, status


class _El:
    def __init__(self, log, kind, text=None):
        self.log, self.kind, self.text = log, kind, text

    def classes(self, cls):
        self.log.append((self.kind, self.text, cls))
        return self

    def tooltip(self, _t):
        return self

    def props(self, _p):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeUI:
    """Records what a renderer emitted: (kind, text, classes)."""

    def __init__(self):
        self.log = []

    def row(self):
        return _El(self.log, "row")

    def label(self, text):
        return _El(self.log, "label", text)

    def expansion(self, text, icon=None):
        return _El(self.log, "expansion", text)

    def card(self):
        return _El(self.log, "card")


def _calls_in(fn_node: ast.AST):
    out = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", None))
    return out


def _function(path: Path, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name}: {name} not found")


# == the shared renderer ========================================================
def test_the_renderer_emits_one_token_pill_per_in_flight_chip():
    from systemu.interface.pages.work import render_table_chips
    ui = _FakeUI()
    render_table_chips({"mode": "using", "labels": ["Using from your table: A",
                                                    "+2 more from your table"],
                        "count": 3, "tooltip": "t"}, _ui=ui)
    pills = [(t, c) for k, t, c in ui.log if k == "label"]
    assert pills == [("Using from your table: A", "s-pill s-pill--accent"),
                     ("+2 more from your table", "s-pill s-pill--accent")]


def test_the_renderer_puts_the_items_behind_the_answered_expansion():
    from systemu.interface.pages.work import render_table_chips
    ui = _FakeUI()
    render_table_chips({"mode": "answered", "label": "Answered from your table: 2",
                        "items": ["A", "B"], "count": 2, "tooltip": "t"}, _ui=ui)
    assert ("expansion", "Answered from your table: 2") in [(k, t) for k, t, _ in ui.log]
    assert [t for k, t, _ in ui.log if k == "label"] == ["A", "B"]


def test_the_renderer_renders_nothing_for_no_model():
    from systemu.interface.pages.work import render_table_chips
    ui = _FakeUI()
    render_table_chips(None, _ui=ui)
    assert ui.log == []


# == 1. the Work run card =======================================================
def _snap(**kw) -> WorkflowSnapshot:
    base = dict(workflow_id="wf_1", title="Send the report", stage="execution",
                status="running", execution_id="exec_w1")
    base.update(kw)
    return WorkflowSnapshot(**base)


def test_an_in_flight_work_card_shows_using_from_your_table(audit_home):
    from systemu.interface.pages.work import work_row_model
    _record(audit_home)
    chip = work_row_model(_snap())["table_chip"]
    assert chip["mode"] == "using"
    assert chip["labels"] == ["Using from your table: recipient"]


def test_a_finished_work_card_shows_answered_from_your_table(audit_home):
    from systemu.interface.pages.work import work_row_model
    _record(audit_home)
    chip = work_row_model(_snap(stage="done", status="completed"))["table_chip"]
    assert chip["mode"] == "answered"
    assert chip["label"] == "Answered from your table: 1"
    assert chip["items"] == ["recipient"]


def test_a_resumed_run_card_still_finds_its_root_record(audit_home):
    """After a resume the tracker holds the NEW execution id."""
    from systemu.interface.pages.work import work_row_model
    _record(audit_home, root="exec_w1", eid="exec_w2")
    chip = work_row_model(_snap(execution_id="exec_w2"))["table_chip"]
    assert chip is not None and chip["mode"] == "using"


def test_a_work_card_with_no_record_or_no_run_has_no_table_chip(audit_home):
    from systemu.interface.pages.work import work_row_model
    assert work_row_model(_snap())["table_chip"] is None
    assert work_row_model(_snap(execution_id=None))["table_chip"] is None


def test_the_work_row_renders_the_table_chip():
    """Reachability: the row renderer calls the chip renderer."""
    from systemu.interface.pages import work
    assert "render_table_chips" in _calls_in(_function(Path(work.__file__), "_render_row"))


# == 2. the workflow detail page ================================================
class _Tracker:
    def __init__(self, snap):
        self._snap = snap

    def get_workflow(self, workflow_id):
        return self._snap if self._snap and self._snap.workflow_id == workflow_id else None


def test_the_workflow_detail_panel_reads_the_runs_record(audit_home):
    from systemu.interface.pages import workflow_detail as wd
    _record(audit_home)
    model = wd.table_payoff_panel_model("wf_1", tracker=_Tracker(_snap()))
    assert model["mode"] == "using"
    assert model["labels"] == ["Using from your table: recipient"]
    done = wd.table_payoff_panel_model(
        "wf_1", tracker=_Tracker(_snap(stage="done", status="completed")))
    assert done["label"] == "Answered from your table: 1"


def test_the_workflow_detail_panel_is_empty_for_an_unknown_workflow(audit_home):
    from systemu.interface.pages import workflow_detail as wd
    _record(audit_home)
    assert wd.table_payoff_panel_model("wf_other", tracker=_Tracker(_snap())) is None


def test_the_workflow_detail_section_renders_the_chip_under_a_heading():
    from systemu.interface.pages import workflow_detail as wd
    ui = _FakeUI()
    wd._render_table_payoff_section(
        {"mode": "answered", "label": "Answered from your table: 1",
         "items": ["recipient"], "count": 1, "tooltip": "t"}, _ui=ui)
    texts = [(k, t) for k, t, _ in ui.log]
    assert ("label", "From your table") in texts
    assert ("expansion", "Answered from your table: 1") in texts


def test_the_workflow_detail_section_renders_nothing_without_a_chip():
    from systemu.interface.pages import workflow_detail as wd
    ui = _FakeUI()
    wd._render_table_payoff_section(None, _ui=ui)
    assert ui.log == []


def test_the_workflow_detail_page_builds_the_table_panel():
    """Reachability: page -> panel -> (model, section renderer)."""
    from systemu.interface.pages import workflow_detail as wd
    path = Path(wd.__file__)
    assert "_build_table_payoff_panel" in _calls_in(
        _function(path, "build_workflow_detail_page"))
    panel = _calls_in(_function(path, "_build_table_payoff_panel"))
    assert {"table_payoff_panel_model", "_render_table_payoff_section"} <= panel
    assert "render_table_chips" in _calls_in(_function(path, "_render_table_payoff_section"))


# == 3. the chat task card ======================================================
_TS = "2026-10-19T09:12:44.551200"      # a chat entry's ts IS its submission id


def test_an_in_flight_chat_card_shows_using_from_your_table(audit_home):
    """While the run is in flight the chat entry has NO execution id yet -- only its
    ts, which the runtime received as chat_submission_id."""
    from systemu.interface.pages.chat_page import chat_table_chip_model
    _record(audit_home, root="exec_c1", chat=_TS)
    chip = chat_table_chip_model({"ts": _TS, "status": "running"})
    assert chip["mode"] == "using"
    assert chip["labels"] == ["Using from your table: recipient"]


def test_a_parked_chat_run_is_still_in_flight(audit_home):
    """A run suspended on the pre-filled confirm card has not ended."""
    from systemu.interface.pages.chat_page import chat_table_chip_model
    _record(audit_home, root="exec_c1", chat=_TS)
    chip = chat_table_chip_model({"ts": _TS, "status": "suspended_harness_escalation"})
    assert chip["mode"] == "using"


def test_a_finished_chat_card_shows_answered_from_your_table(audit_home):
    from systemu.interface.pages.chat_page import chat_table_chip_model
    _record(audit_home, root="exec_c1", chat=_TS)
    chip = chat_table_chip_model({"ts": _TS, "status": "success",
                                  "execution_id": "exec_c1"})
    assert chip["mode"] == "answered"
    assert chip["label"] == "Answered from your table: 1"


def test_a_chat_card_is_found_by_its_final_execution_id(audit_home):
    """A resumed run's entry carries the LAST execution id, not the root."""
    from systemu.interface.pages.chat_page import chat_table_chip_model
    _record(audit_home, root="exec_c1", eid="exec_c2")
    chip = chat_table_chip_model({"ts": "unrelated", "status": "success",
                                  "execution_id": "exec_c2"})
    assert chip is not None and chip["mode"] == "answered"


def test_a_chat_card_with_nothing_recorded_has_no_table_chip(audit_home):
    from systemu.interface.pages.chat_page import chat_table_chip_model
    assert chat_table_chip_model({"ts": _TS, "status": "success",
                                  "lane": "quick"}) is None
    assert chat_table_chip_model("not a dict") is None


def test_the_chat_card_renders_the_table_chip():
    """Reachability: the chat history's per-entry renderer calls the chip renderer
    on the chip model."""
    from systemu.interface.pages import chat_page
    page = _function(Path(chat_page.__file__), "build_chat_page")
    entry = next(n for n in ast.walk(page)
                 if isinstance(n, ast.FunctionDef) and n.name == "_render_entry")
    assert {"render_table_chips", "chat_table_chip_model"} <= _calls_in(entry)
