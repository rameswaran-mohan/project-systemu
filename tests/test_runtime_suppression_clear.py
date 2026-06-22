"""Test the v0.3.6 dep-suppression bypass in ShadowRuntime.

The key invariant: once the operator approves a package that previously
blocked a tool, the runtime must drop the tool from its
``_dep_failed_tools`` map so the next call attempts the tool again
(rather than short-circuiting with the cached "permanently unavailable"
message).

We exercise ``_maybe_clear_dep_suppression`` directly using a fake
sandbox that wraps a real :class:`DepApprovalStore`.  Avoids the
heavy setup of a full Shadow execution.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime.dep_approvals import DepApprovalStore


def _make_runtime_stub(approvals):
    """Build the minimal ShadowRuntime surface that _maybe_clear_dep_suppression touches."""
    # The method only touches self.sandbox._approvals and self._dep_failed_tools.
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.sandbox = SimpleNamespace(_approvals=approvals)
    rt._dep_failed_tools = {}
    return rt


def test_clears_when_all_blocking_pkgs_approved(tmp_path):
    store = DepApprovalStore(tmp_path / "approvals.json")
    store.approve("python-docx")
    rt = _make_runtime_stub(store)
    rt._dep_failed_tools["create_word_doc"] = ["python-docx"]

    cleared = rt._maybe_clear_dep_suppression(
        "create_word_doc", ["python-docx"]
    )
    assert cleared is True
    assert "create_word_doc" not in rt._dep_failed_tools


def test_does_not_clear_when_any_pkg_unapproved(tmp_path):
    store = DepApprovalStore(tmp_path / "approvals.json")
    store.approve("python-docx")
    # Pillow NOT approved → suppression must remain.
    rt = _make_runtime_stub(store)
    rt._dep_failed_tools["multi_dep_tool"] = ["python-docx", "Pillow"]

    cleared = rt._maybe_clear_dep_suppression(
        "multi_dep_tool", ["python-docx", "Pillow"]
    )
    assert cleared is False
    assert "multi_dep_tool" in rt._dep_failed_tools


def test_empty_blocking_list_clears(tmp_path):
    """Defensive: a tool with no recorded blocking packages should clear
    on next check (data is incomplete; safer to retry than to suppress
    forever)."""
    store = DepApprovalStore(tmp_path / "approvals.json")
    rt = _make_runtime_stub(store)
    rt._dep_failed_tools["mystery_tool"] = []
    cleared = rt._maybe_clear_dep_suppression("mystery_tool", [])
    assert cleared is True
    assert "mystery_tool" not in rt._dep_failed_tools


def test_no_approvals_means_no_clear(tmp_path):
    """Sandbox without an approvals store can't validate — returns False."""
    rt = _make_runtime_stub(approvals=None)
    rt._dep_failed_tools["x"] = ["python-docx"]
    assert rt._maybe_clear_dep_suppression("x", ["python-docx"]) is False
    assert "x" in rt._dep_failed_tools


def test_separate_process_approval_picked_up(tmp_path):
    """The whole point of the no-restart fix: a separate-process approval
    must be visible to the in-flight runtime without restarting it.
    """
    path = tmp_path / "approvals.json"
    store_in_runtime = DepApprovalStore(path)
    rt = _make_runtime_stub(store_in_runtime)
    rt._dep_failed_tools["create_word_doc"] = ["python-docx"]

    # First check: not approved → suppression stays.
    assert not rt._maybe_clear_dep_suppression("create_word_doc", ["python-docx"])

    # Operator approves out-of-band (separate Python process semantics).
    DepApprovalStore(path).approve("python-docx")

    # Same runtime instance, no restart — must now see the approval.
    assert rt._maybe_clear_dep_suppression("create_word_doc", ["python-docx"])
    assert "create_word_doc" not in rt._dep_failed_tools
