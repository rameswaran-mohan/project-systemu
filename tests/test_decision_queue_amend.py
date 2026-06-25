"""Tests for OperatorDecisionQueue.resolve_with_context_patch (amend-then-approve).

Mirrors the filesystem-Vault construction used by
``tests/test_harness_grant_reconciler.py::_make_vault`` so the round-trip
(post -> resolve_with_context_patch -> get_decision) runs against a real Vault.
"""
import pytest

from systemu.approval.decision_queue import OperatorDecisionQueue


def _make_vault(tmp_path):
    """Build a filesystem Vault with the dir layout the resume tests use."""
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def test_resolve_with_context_patch_persists_patch_and_choice(tmp_path):
    vlt = _make_vault(tmp_path)
    q = OperatorDecisionQueue(vlt)
    did = q.post(title="Harness request: tool", body="?",
                 options=["Deny", "Approve"],
                 context={"kind": "gate", "gate_type": "harness",
                          "harness_kind": "tool", "spec": {"name": "t"}},
                 dedup_key="harness:e1:hreq_1")
    q.resolve_with_context_patch(did, choice="Approve",
                                 context_patch={"amended_spec": {"name": "t2"}})
    got = vlt.get_decision(did)
    assert got.status == "resolved"
    assert got.choice == "Approve"
    assert got.context["amended_spec"] == {"name": "t2"}
    # original context survives the merge
    assert got.context["harness_kind"] == "tool"
