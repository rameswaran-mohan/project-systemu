"""T5 -- the BINDER-SIDE writes of the durable table-payoff record (reachability pins).

The record (``table_payoff_record``) is worthless if nothing writes it -- this repo has
repeatedly shipped a correct helper with no production caller. These tests drive the
REAL producers:

  * the pre-loop producer ``shadow_runtime._populate_requirement_report`` (direct
    call, the way ``test_rb5_table_payoff`` drives it), and
  * the mid-loop TOOL_CALL binder inside the REAL ``ShadowRuntime.execute`` loop, with
    a REAL table item composed by the REAL survey.

Removing either write reds a named test here. Every test points the audit data root at
``tmp_path`` (cwd + no vault env), so nothing is written into the worktree.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from systemu.core.models import Objective, Tool
from systemu.runtime import table_payoff_record as tpr
from systemu.runtime.situational_inventory import SituationReport, compose_table
from systemu.runtime.table_store import TableItem


@pytest.fixture()
def audit_home(tmp_path, monkeypatch):
    """Point the production audit-root resolver at ``tmp_path/data``."""
    from systemu.runtime import vault_root as vr
    monkeypatch.delenv(vr.VAULT_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "data"


def _rows(data_dir: Path, root: str):
    p = tpr.run_record_path(root, data_dir=data_dir)
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


# == 1. the pre-loop producer ==================================================
class _Ctx:
    def __init__(self):
        self.files_produced = []


_SCHEMA = {"type": "object", "required": ["recipient"],
           "properties": {"recipient": {"type": "string"}}}


def _situation():
    item = TableItem(id="t-rcpt", kind="service", name="recipient",
                     ref={"server": "https://example.invalid/r"})
    return compose_table(SituationReport(), [item]).model_dump(mode="json")


def _producer(**ids):
    import systemu.runtime.shadow_runtime as sr
    with patch("systemu.runtime.elicitation.surface_ask_bundle_requirement",
               return_value={"action": "cancel", "content": {}}) as surface:
        sr._populate_requirement_report(
            _Ctx(), objectives=[Objective(id=1, goal="send it", success_criteria="sent")],
            capability=Tool(id="tool_t", name="t", description="d",
                            tool_type="python_function", parameters_schema=_SCHEMA),
            situation=_situation(), **ids)
    return surface


def test_the_preloop_producer_writes_the_table_payoff_record(audit_home):
    _producer(root_execution_id="exec_pre1", execution_id="exec_pre1",
              scroll_id="scroll_9", activity_id="act_9", chat_submission_id="sub-9")
    rows = _rows(audit_home, "exec_pre1")
    assert [r["event"] for r in rows] == ["consult", "supplied"], rows
    assert rows[0]["site"] == "preloop" and rows[0]["scroll_id"] == "scroll_9"
    assert rows[1]["table_item_id"] == "t-rcpt" and rows[1]["item_name"] == "recipient"
    assert rows[1]["bound_as"] == "prefilled_confirm"
    assert tpr.find_run(chat_submission_id="sub-9", data_dir=audit_home) is not None


def test_a_silent_table_bind_is_recorded_before_the_empty_ask_return(audit_home,
                                                                       monkeypatch):
    """ORDERING: a punched clamp empties the ask_bundle, and the producer returns early
    on an empty bundle. The record must be written BEFORE that return, or the one bind
    the DEC-25 tripwire exists for would never reach the durable record."""
    import systemu.runtime.requirement_binder as rb
    monkeypatch.setattr(rb, "_entry_origin", lambda entry: rb._OPERATOR)
    surface = _producer(root_execution_id="exec_pre2", execution_id="exec_pre2")
    surface.assert_not_called()                       # premise: nothing was asked
    supplied = [r for r in _rows(audit_home, "exec_pre2") if r["event"] == "supplied"]
    assert [r["bound_as"] for r in supplied] == ["silent"]


def test_a_legacy_caller_without_run_ids_writes_nothing(audit_home):
    _producer()
    assert not (audit_home / "audit" / "table_payoff").exists()


def test_a_record_failure_never_affects_the_run(audit_home, monkeypatch):
    """Observability only: a raising recorder must leave the producer's behaviour (the
    ask it surfaces) exactly as it was."""
    def _boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(tpr, "record_binder_pass", _boom)
    surface = _producer(root_execution_id="exec_pre3", execution_id="exec_pre3")
    surface.assert_called_once()


def _execute_fn() -> ast.AST:
    import systemu.runtime.shadow_runtime as sr
    src = Path(sr.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "ShadowRuntime":
            for fn in node.body:
                if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "execute":
                    return fn
    raise AssertionError("ShadowRuntime.execute not found")


def _calls(fn: ast.AST, name: str):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            callee = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if callee == name:
                out.append(node)
    return out


def test_execute_passes_the_run_ids_to_the_preloop_producer():
    """The producer writes nothing without a root id (legacy callers), so the production
    call site MUST pass one -- otherwise the pre-loop write is silently inert."""
    calls = _calls(_execute_fn(), "_populate_requirement_report")
    assert calls, "execute() no longer calls the pre-loop producer"
    for c in calls:
        kws = {k.arg for k in c.keywords}
        assert {"root_execution_id", "execution_id", "scroll_id", "activity_id",
                "chat_submission_id"} <= kws, kws


# == 2. the mid-loop TOOL_CALL binder, inside the REAL execute() loop ==========
_NESTED = {
    "type": "object",
    "properties": {
        "recipient": {"type": "string"},
        "message": {"type": "object",
                    "properties": {"body": {"type": "string"},
                                   "subject": {"type": "string"}},
                    "required": ["body", "subject"]}},
    "required": ["recipient", "message"]}


def _send_tool():
    from systemu.core.models import ToolStatus, ToolType
    return Tool(id="tool_send", name="send", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True, implementation_path="vault/tools/implementations/send.py",
                parameters_schema=_NESTED, effect_tags=["net_mutate"])


def _entities(vroot: Path, tool):
    from systemu.core.models import Activity, Scroll, Shadow, ShadowStatus
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (vroot / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"]:
        (vroot / idx / "index.json").write_text("[]", encoding="utf-8")
    (vroot / "global_memory.jsonl").write_text("", encoding="utf-8")
    vault = Vault(str(vroot))
    shadow = Shadow(id="shadow_t5", name="s", description="t", system_prompt="t",
                    status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_t5", name="s", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="send the message",
                                          success_criteria="sent")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_t5", name="a", scroll_id=scroll.id,
                        required_tool_ids=[tool.id], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


def test_the_midloop_binder_writes_the_table_payoff_record(tmp_path, audit_home,
                                                            monkeypatch):
    """The live path: the operator's table holds an item named for the leaf the LLM
    left out. The REAL survey composes it, the REAL mid-loop binder binds the leaf from
    it (a pre-filled one-click confirm), the run parks on the card -- and the durable
    record names the item, keyed by the run's root."""
    from sharing_on.config import Config
    import systemu.runtime.execution_snapshot as es
    import systemu.runtime.shadow_runtime as sr
    from systemu.interface import notifications
    from systemu.runtime.table_store import save_items

    vroot = tmp_path / "vault"
    vault, shadow, activity = _entities(vroot, _send_tool())
    save_items(vault, [TableItem(id="t-rcpt", kind="service", name="recipient",
                                 ref={"server": "https://example.invalid/r"},
                                 provenance="operator_added")])

    snap_dir = tmp_path / "snap"
    (snap_dir / "audit").mkdir(parents=True)
    rw, rr, rd = es.write_snapshot, es.read_snapshot, es.delete_snapshot
    monkeypatch.setattr(es, "write_snapshot", lambda s, **k: rw(s, data_dir=snap_dir))
    monkeypatch.setattr(es, "read_snapshot", lambda e, **k: rr(e, data_dir=snap_dir))
    monkeypatch.setattr(es, "delete_snapshot", lambda e, **k: rd(e, data_dir=snap_dir))
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "off")
    monkeypatch.setattr(notifications, "_vault", vault, raising=False)
    monkeypatch.setattr(notifications, "_decision_queue_instance", None, raising=False)

    cfg = Config()
    cfg.vault_dir = str(vroot)
    cfg.output_dir = str(tmp_path / "out")
    runtime = sr.ShadowRuntime(cfg, vault)
    monkeypatch.setattr(runtime, "_handle_tool_call",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("tool ran despite the confirm card")))
    decisions = iter([{"action": "TOOL_CALL", "tool_name": "send",
                       "completes_objective": 1,
                       "parameters": {"message": {"body": "hi", "subject": "yo"}}}])
    monkeypatch.setattr(sr, "llm_call_json",
                        lambda *a, **k: next(decisions, {"action": "COMPLETE",
                                                         "summary": "done"}),
                        raising=False)

    # NEVER an LLM: the R-A10 open-world planner runs on a fresh run whose survey is
    # non-empty, and it would otherwise reach the real router. Identity is its own
    # documented no-insert answer.
    async def _no_plan(*, objectives, **_k):
        return objectives
    monkeypatch.setattr(sr, "run_open_world_planner", _no_plan)
    import systemu.core.llm_router as lr
    llm_calls = []

    async def _no_llm(*a, **k):
        llm_calls.append(k.get("stage"))
        raise RuntimeError("no LLM in tests")
    monkeypatch.setattr(lr, "llm_call", _no_llm)
    monkeypatch.setattr(lr, "async_llm_call_json", _no_llm)

    with patch("systemu.interface.harness_review.surface_harness_request",
               return_value="did_1"):
        result = asyncio.run(runtime.execute(shadow, activity, dry_run=False))

    assert str(result.get("status", "")).startswith("suspended"), result.get("status")
    assert llm_calls == [], f"the run reached the LLM router: {llm_calls}"
    scan = tpr.scan_records(data_dir=audit_home)
    assert len(scan.records) == 1, scan
    rec = scan.records[0]
    assert rec.consulted is True
    assert [(s["table_item_id"], s["site"], s["tool"], s["bound_as"])
            for s in rec.supplied] == [("t-rcpt", "midloop", "send", "prefilled_confirm")]
    assert rec.supplied[0]["item_name"] == "recipient"
    rows = _rows(audit_home, rec.root_execution_id)
    consult = next(r for r in rows if r["event"] == "consult")
    assert consult["scroll_id"] == "scroll_t5" and consult["activity_id"] == "act_t5"
