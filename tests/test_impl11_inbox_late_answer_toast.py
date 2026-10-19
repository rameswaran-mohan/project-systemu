"""IMPL-11 - the Inbox shows the late-answer notice as itself.

Witnessed gap: an Inbox click on a card whose run was cancelled reached the
page's generic failure handler and toasted "Resolve failed: That run was
cancelled; this answer was not needed." in red - a FAILURE framing for an answer
that simply was not needed (nothing failed; nothing was recorded).

Ruling under test: the page catches ``AnswerNotNeeded`` specifically and toasts
its message unprefixed as an informational line; every other failure keeps the
"Resolve failed: ..." negative toast.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from systemu.approval import decision_queue as dq
from systemu.approval.decision_queue import OperatorDecisionQueue

NOTICE = "That run was cancelled; this answer was not needed."


def _calls_in(func) -> set:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_the_late_answer_toast_is_the_notice_itself_and_informational():
    from systemu.interface.pages.inbox_page import resolve_failure_notice
    msg, kind = resolve_failure_notice(dq.AnswerNotNeeded("dec_x"))
    assert msg == NOTICE, "MUTATION PIN: the notice is shown behind a failure prefix"
    assert kind == "info"
    msg.encode("ascii")


def test_any_other_failure_keeps_the_failure_toast():
    from systemu.interface.pages.inbox_page import resolve_failure_notice
    assert resolve_failure_notice(ValueError("choice 'x' not in options")) == (
        "Resolve failed: choice 'x' not in options", "negative")


def test_a_real_late_click_lands_on_the_informational_toast(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    from systemu.interface.pages.inbox_page import (
        _resolve_and_execute_confirmed_gate, resolve_failure_notice)

    vault = FileVault(Vault(str(tmp_path / "v")))
    q = OperatorDecisionQueue(vault)
    did = q.post(title="Run tool: thing", body="b", options=["Deny", "Approve once"],
                 context={"kind": "gate", "gate_type": "tool", "tool_name": "thing",
                          "execution_id": "exec_toast", "activity_id": "act_toast"},
                 dedup_key="tool:toast")
    q.expire_cards_for_run(execution_id="exec_toast")

    with pytest.raises(dq.AnswerNotNeeded) as ei:
        _resolve_and_execute_confirmed_gate(did, "Approve once", vault, None)
    assert resolve_failure_notice(ei.value) == (NOTICE, "info")
    assert vault.get_decision(did).status == "expired"


def test_the_card_click_handler_routes_failures_through_it():
    from systemu.interface.pages import inbox_page
    calls = _calls_in(inbox_page._render_unified_card)
    assert "resolve_failure_notice" in calls
    src = inspect.getsource(inbox_page._render_unified_card)
    assert 'f"Resolve failed: {exc}"' not in src, (
        "the click handler still formats the failure toast inline")
