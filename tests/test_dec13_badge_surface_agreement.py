"""DEC-13 / DEC-43 -- SURFACE AGREEMENT: every registered surface renders the
one mint's state.

``verification_badge.SURFACES`` is the committed surface list. This test walks
every surface in it over the fixture matrix (verified / claimed / no evidence /
measured / failed / handoff / unreadable) through the surface's own production
entry point, with the receipts on disk where the production default reads them,
and asserts the surface renders exactly what the mint minted. A surface added to
the registry without an adapter here fails; a surface that re-derives the badge
(a proxy, a recipe, a dropped badge) fails on the case where its answer and the
mint's diverge.

The lint (``tools/lint_verification_badge.py``) checks STRUCTURE; this checks
BEHAVIOUR. Different currencies -- the DEC-43 two-layer shape.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import _dec13_fixtures as fx
from systemu.runtime import verification_badge as vb

SURFACE_IDS = sorted({sid for sid, _ in vb.SURFACES})
REPO = Path(__file__).resolve().parent.parent


def _snap(status, eid, stage="done"):
    from systemu.runtime.workflow_tracker import WorkflowSnapshot
    return WorkflowSnapshot(workflow_id="wf_1", title="Weekly report", stage=stage,
                            status=status, scroll_id="wf_1", execution_id=eid)


def _assert_ui_badge(value, minted):
    """A UI surface's badge: the minted VALUE (not a recipe), rendered by the one
    component, with the filled check iff verified."""
    from systemu.interface.components.receipt_panel import render_badge
    assert type(value) is vb.RunBadge, f"the surface holds {type(value).__name__}, not the minted value"
    assert value.state == minted.state
    ui = fx.FakeUI()
    render_badge(value, _ui=ui)
    (btn,) = ui.buttons()
    v = vb.view(minted)
    assert btn["text"] == v.label and btn["tooltip"] == v.tooltip
    assert (btn["icon"] == vb.VERIFIED_ICON) is (minted.state == vb.VERIFIED)


# ── run-level surfaces ───────────────────────────────────────────────────────

def _work_row(ctx):
    from systemu.interface.pages.work import work_row_model
    _assert_ui_badge(work_row_model(_snap(ctx.status, ctx.eid))["badge"], ctx.minted)


def _workflow_detail(ctx):
    from systemu.interface.pages.workflow_detail import (
        workflow_badge_model, workflow_header_icon,
    )
    b = workflow_badge_model(_snap(ctx.status, ctx.eid))
    _assert_ui_badge(b, ctx.minted)
    assert (workflow_header_icon("done", b) == vb.VERIFIED_ICON) is \
        (ctx.minted.state == vb.VERIFIED)


def _receipt_panel(ctx):
    from systemu.interface.components.receipt_panel import (
        receipt_panel_model, render_receipt_panel,
    )
    m = receipt_panel_model(ctx.minted)
    assert m["state"] == ctx.minted.state and m["summary"] == vb.view(ctx.minted).tooltip
    ui = fx.FakeUI()
    render_receipt_panel(m, _ui=ui)
    assert m["title"] in ui.texts()


def _chat_card(ctx):
    from systemu.interface.pages.chat_page import chat_badge_model
    _assert_ui_badge(chat_badge_model({"ts": "t1", "status": ctx.status,
                                       "execution_id": ctx.eid}), ctx.minted)


def _home_recent(ctx):
    from systemu.interface.pages.console import home_recent_workflows
    (row,) = home_recent_workflows([_snap(ctx.status, ctx.eid)])
    _assert_ui_badge(row["badge"], ctx.minted)


class _HistoryVault:
    def __init__(self, entries):
        self._entries = entries

    def load_chat_history(self, limit=25):
        return list(self._entries)


def _entry(ctx):
    return {"ts": "2026-10-01T10:00:00", "prompt": "weekly report",
            "status": ctx.status, "execution_id": ctx.eid, "scroll_id": "s1",
            "shadow_id": "shadow_1", "summary": "done"}


def _status_menu(ctx):
    from systemu.interface.components.status_menu import build_status_rows
    (row,) = build_status_rows(_HistoryVault([_entry(ctx)]))
    _assert_ui_badge(row["badge"], ctx.minted)


def _telegram_status(ctx):
    from systemu.messaging.handlers import build_status_handler
    from systemu.vault.vault import Vault
    root = ctx.tmp_path / "vault"
    (root / "elder").mkdir(parents=True, exist_ok=True)
    vault = Vault(str(root))
    vault.append_chat_history(_entry(ctx))
    reply = build_status_handler(vault)(object())
    assert f"weekly report -- {vb.view(ctx.minted).label}" in reply, reply


def _telegram_push(ctx):
    from systemu.messaging.event_pusher import translate_event
    msg = translate_event({
        "category": "task_outcome",
        "level": "SUCCESS" if ctx.status == "success" else "WARNING",
        "message": f"Task {ctx.status}: weekly report",
        "context": {"status": ctx.status, "execution_id": ctx.eid},
        "details": {"summary": "Report saved."}})
    assert vb.badge_text(ctx.minted) in msg.text, msg.text
    if ctx.minted.state in (vb.HANDOFF, vb.FAILED):
        for line in vb.effect_lines(ctx.minted):
            assert line in msg.text


def _record_console(monkeypatch):
    from rich.console import Console
    from systemu.interface import cli_commands as cc
    rec = Console(record=True, width=400, color_system=None)
    monkeypatch.setattr(cc, "console", rec)
    return cc, rec


def _cli_chat_submit(ctx):
    from click.testing import CliRunner
    import systemu.pipelines.direct_task as dt
    cc, rec = _record_console(ctx.monkeypatch)
    vault = _HistoryVault([_entry(ctx)])
    ctx.monkeypatch.setattr(cc, "_get_vault_and_config",
                            lambda _c: (SimpleNamespace(), vault))
    ctx.monkeypatch.setattr(dt, "run_direct_task",
                            lambda p, c, v: SimpleNamespace(name="act", scroll_id="s1"))
    res = CliRunner().invoke(cc.chat_group, ["submit", "weekly report"],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    out = rec.export_text()
    assert "Verification: " + vb.badge_text(ctx.minted) in out, out
    if ctx.minted.state in (vb.HANDOFF, vb.FAILED):
        for line in vb.effect_lines(ctx.minted):
            assert line.strip() in out


def _cli_chat_history(ctx):
    from click.testing import CliRunner
    cc, rec = _record_console(ctx.monkeypatch)
    vault = _HistoryVault([_entry(ctx)])
    ctx.monkeypatch.setattr(cc, "_get_vault_and_config",
                            lambda _c: (SimpleNamespace(), vault))
    res = CliRunner().invoke(cc.chat_group, ["history"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    row = fx.first(rec.export_text().splitlines(), lambda ln: "weekly report" in ln)
    assert row is not None
    cells = [c.strip() for c in row.split("│")]
    assert vb.view(ctx.minted).label in cells, row


def _cli_army_execute(ctx):
    from click.testing import CliRunner
    from systemu.runtime import shadow_runtime
    cc, rec = _record_console(ctx.monkeypatch)
    shadow = SimpleNamespace(name="sh", id="sh1", available_tool_ids=[], skill_ids=[])
    scroll = SimpleNamespace(name="sc", id="sc1", activity_id=None, action_blocks=[])
    vault = SimpleNamespace(get_shadow=lambda i: shadow, get_scroll=lambda i: scroll)
    ctx.monkeypatch.setattr(cc, "_get_vault_and_config",
                            lambda _c: (SimpleNamespace(), vault))
    ctx.monkeypatch.setattr(cc, "_maybe_install_bridge_writer", lambda: None)
    status, eid = ctx.status, ctx.eid

    class _Runtime:
        def __init__(self, config=None, vault=None):
            pass

        async def execute(self, shadow, activity, dry_run=False, origin="manual"):
            return {"status": status, "summary": "s", "execution_id": eid}

    ctx.monkeypatch.setattr(shadow_runtime, "ShadowRuntime", _Runtime)
    res = CliRunner().invoke(cc.army_group, ["execute", "sh1", "sc1"],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "Verification: " + vb.badge_text(ctx.minted) in rec.export_text()


def _task_api(ctx):
    from systemu.interface.task_api import project_task
    got = project_task({"ts": "t1", "status": ctx.status, "execution_id": ctx.eid})
    assert got["verification"] == vb.view(ctx.minted).as_json()


# ── per-effect surfaces ──────────────────────────────────────────────────────

def _minted_effects(receipts):
    if type(receipts) is dict:
        return [vb.mint_effect(k, v) for k, v in sorted(receipts.items())]
    return []


def _ledger_export(ctx):
    from systemu.runtime import ledger
    root = ctx.tmp_path / "v"
    (root / "audit").mkdir(parents=True)
    (root / "audit" / "actions.jsonl").write_text(json.dumps({
        "ts": "2026-10-01T09:59:00Z", "execution_id": ctx.eid, "objective_id": 1,
        "action": "create_row", "params": {"target": "x"}, "success": True}) + "\n",
        encoding="utf-8")
    rows = ledger.project(SimpleNamespace(root=root))
    (eff,) = [r for r in rows if r.event_kind == "effect"]
    cell = dict(zip(ledger._COLUMNS, ledger._csv_cells(eff)))["outcome_verification"]
    if ctx.receipts is None:
        # No receipt: the cell is the mint's claimed -- never blank, never verified.
        assert cell == ctx.minted.state == vb.CLAIMED == eff.outcome["verification"]
    elif type(ctx.receipts) is bytes:
        assert cell == ledger.NOT_RECORDED
    else:
        want = vb.mint_effect("1", ctx.receipts["1"]).state
        assert cell == want == eff.outcome["verification"]
        committed = [r for r in rows if r.event_kind == "committed_effect"]
        assert [r.outcome["verification"] for r in committed] == \
            [e.state for e in _minted_effects(ctx.receipts)
             if e.state == vb.VERIFIED and e.live_marked]


def _committed_effects(ctx):
    from systemu.runtime.committed_effects import committed_effect_details
    store = ctx.receipts if type(ctx.receipts) is dict else (
        {"1": "garbage"} if type(ctx.receipts) is bytes else {})
    want = [e.detail for e in _minted_effects(store) if e.state == vb.VERIFIED]
    assert committed_effect_details(store) == want


def _receipt_badges(ctx):
    from systemu.runtime.receipts_store import receipt_badges_for
    got = receipt_badges_for(ctx.eid)
    if type(ctx.receipts) is bytes:
        assert [b["state"] for b in got] == [vb.NOT_RECORDED]
    else:
        assert [(b["state"], b["label"]) for b in got] == \
            [(e.state, vb.view(e).label) for e in ctx.minted.effects]
        assert [b["verified"] for b in got] == \
            [e.state == vb.VERIFIED for e in ctx.minted.effects]


ADAPTERS = {
    "work_row": _work_row,
    "workflow_detail": _workflow_detail,
    "receipt_panel": _receipt_panel,
    "chat_card": _chat_card,
    "home_recent": _home_recent,
    "status_menu": _status_menu,
    "telegram_status": _telegram_status,
    "telegram_push": _telegram_push,
    "cli_chat_submit": _cli_chat_submit,
    "cli_chat_history": _cli_chat_history,
    "cli_army_execute": _cli_army_execute,
    "task_api": _task_api,
    "ledger_export": _ledger_export,
    "committed_effects": _committed_effects,
    "receipt_badges": _receipt_badges,
}


def test_every_registered_surface_has_an_adapter():
    """The committed list and this walk are the same set: a surface added to the
    registry without an agreement adapter, or an adapter for a surface dropped
    from it, fails here."""
    assert set(ADAPTERS) == set(SURFACE_IDS)


def test_every_registered_surface_exists():
    for sid, spec in vb.SURFACES:
        path, _, fn = spec.partition("::")
        p = REPO / path
        assert p.is_file(), sid
        if fn:
            names = {n.name for n in ast.parse(p.read_text(encoding="utf-8")).body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert fn in names, sid


@pytest.mark.parametrize("case", sorted(fx.CASES))
@pytest.mark.parametrize("sid", SURFACE_IDS)
def test_the_surface_renders_the_mints_state(sid, case, tmp_path, monkeypatch):
    status, receipts = fx.CASES[case]
    eid = f"exec_{case}"
    fx.point_audit_root(monkeypatch, tmp_path)
    fx.plant(tmp_path, eid, receipts)
    minted = vb.run_badge(status, eid)
    # the mint itself answers DEC-13 correctly for this case ...
    assert minted.state == fx.EXPECTED_RUN_STATE[case]
    # ... and the surface renders exactly that answer.
    ADAPTERS[sid](SimpleNamespace(status=status, eid=eid, receipts=receipts,
                                  minted=minted, tmp_path=tmp_path,
                                  monkeypatch=monkeypatch))


# ── the page renderers hand the minted value to the one component ────────────

def _calls(path: str, function: str):
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            out = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    f = n.func
                    out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", None))
            return out
    raise AssertionError(f"{path}: {function} not found")


@pytest.mark.parametrize("path,function", [
    ("systemu/interface/pages/work.py", "_render_row"),
    ("systemu/interface/pages/console.py", "_build_recent_activity_card"),
    ("systemu/interface/components/status_menu.py", "render_status_menu"),
    ("systemu/interface/pages/chat_page.py", "_render_entry"),
    ("systemu/interface/pages/workflow_detail.py", "_build_verification_panel"),
])
def test_the_page_renders_through_the_shared_badge(path, function):
    assert "render_badge" in _calls(path, function)


@pytest.mark.parametrize("path,function", [
    ("systemu/interface/pages/console.py", "_build_recent_activity_card"),
    ("systemu/interface/components/status_menu.py", "render_status_menu"),
])
def test_a_ticking_list_with_a_badge_repaints_only_on_change(path, function):
    """W12: a badge is a button; an unconditional 2s repaint would eat its click."""
    assert "gated_refresh" in _calls(path, function)


def test_the_detail_page_and_chat_card_render_the_handoff_state():
    assert "render_receipt_panel" in _calls(
        "systemu/interface/pages/workflow_detail.py", "_build_verification_panel")
    assert "render_committed_effects_list" in _calls(
        "systemu/interface/pages/chat_page.py", "_render_entry")
