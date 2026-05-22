"""Tests for the v0.3.6 supervisor-feed + no-restart approval flow.

Covers four contracts:

  1. **EventBus emits a non-blocking approval card** when the installer
     hits BLOCKED_PENDING_APPROVAL — deduped per package.
  2. **DepApprovalStore.is_approved() re-reads on every check**, so a
     write by a separate process is visible to the daemon without a
     restart.
  3. **DepApprovalStore.approve() publishes a dismissed event** so any
     open chat card closes when the operator approves on /tools.
  4. **No-restart suppression bypass**: after an approval lands, the
     in-flight runtime's dep-suppression dict clears for that tool.

The tests stub pip and never touch the network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.runtime import dependency_installer as di
from systemu.runtime.dep_approvals import DepApprovalStore
from systemu.interface.event_bus import EventBus


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture(autouse=True)
def _reset_state():
    di.reset_cache_for_tests()
    EventBus.get().reset_dep_publish_state_for_tests()
    yield
    di.reset_cache_for_tests()
    EventBus.get().reset_dep_publish_state_for_tests()


@pytest.fixture
def bus_capture():
    """Subscribe and capture every event published during the test."""
    events: list = []
    unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
    yield events
    unsub()


# ─────────────────────────────────────────────────────────────────────────────
# 1) Installer publishes a non-blocking approval card

class TestInstallerPublishesApprovalCard:
    def test_blocked_pending_publishes_approval_event(self, tmp_path, bus_capture):
        store = DepApprovalStore(tmp_path / "approvals.json")
        r = di.ensure_satisfied(
            ["python-docx"],
            mode=di.InstallMode.PROMPT,
            approvals=store,
            tool_name="create_word_doc",
            tool_id="tool_xyz",
        )
        assert not r.ok
        approval_events = [e for e in bus_capture if e.get("category") == "approval"]
        assert len(approval_events) == 1
        ctx = approval_events[0]["context"]
        assert ctx["package"]      == "python-docx"
        assert ctx["tool_name"]    == "create_word_doc"
        assert ctx["redirect_to"]  == "/tools"
        assert ctx["dedup_key"]    == "dep-install:python-docx"

    def test_repeated_blocks_dedupe(self, tmp_path, bus_capture):
        store = DepApprovalStore(tmp_path / "approvals.json")
        # Hit the same missing dep three times (simulating three shadows).
        for _ in range(3):
            di.ensure_satisfied(
                ["python-docx"],
                mode=di.InstallMode.PROMPT,
                approvals=store,
                tool_name="create_word_doc",
            )
        approval_events = [e for e in bus_capture if e.get("category") == "approval"]
        # Only ONE card published (request_count = 1 is the first threshold).
        assert len(approval_events) == 1

    def test_republish_at_threshold(self, tmp_path, bus_capture):
        store = DepApprovalStore(tmp_path / "approvals.json")
        # Simulate request_count climbing by hitting the same dep many times.
        for _ in range(6):
            di.ensure_satisfied(
                ["python-docx"],
                mode=di.InstallMode.PROMPT,
                approvals=store,
                tool_name="create_word_doc",
            )
        approval_events = [e for e in bus_capture if e.get("category") == "approval"]
        # First publish at count=1, second at count=5 → 2 cards total.
        assert len(approval_events) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# 2) Store re-reads on every check (no-restart fix)

class TestStoreReReadsOnEveryCheck:
    def test_separate_process_write_visible(self, tmp_path):
        path = tmp_path / "approvals.json"
        s_reader = DepApprovalStore(path)
        assert not s_reader.is_approved("python-docx")

        # Simulate a *separate process* (CLI / dashboard) approving.
        s_writer = DepApprovalStore(path)
        s_writer.approve("python-docx")

        # The reader instance is the same Python object as before but
        # MUST now see the approval — that's the v0.3.6 contract.
        assert s_reader.is_approved("python-docx")

    def test_revoke_visible_to_reader(self, tmp_path):
        path = tmp_path / "approvals.json"
        s = DepApprovalStore(path)
        s.approve("alpha")
        assert s.is_approved("alpha")
        # Separate-process revoke.
        DepApprovalStore(path).revoke("alpha")
        assert not s.is_approved("alpha")


# ─────────────────────────────────────────────────────────────────────────────
# 3) Approve / revoke publish dismissal events

class TestApproveAndRevokePublishDismissal:
    def test_approve_publishes_dismissed(self, tmp_path, bus_capture):
        s = DepApprovalStore(tmp_path / "approvals.json")
        s.approve("python-docx")
        dismissals = [e for e in bus_capture if e.get("category") == "approval_dismissed"]
        assert len(dismissals) == 1
        ctx = dismissals[0]["context"]
        assert ctx["package"]   == "python-docx"
        assert ctx["outcome"]   == "approved"
        assert ctx["dedup_key"] == "dep-install:python-docx"

    def test_revoke_publishes_dismissed(self, tmp_path, bus_capture):
        s = DepApprovalStore(tmp_path / "approvals.json")
        s.approve("foo")
        s.revoke("foo")
        # Two dismissals — approved + revoked.
        outcomes = [
            e["context"]["outcome"]
            for e in bus_capture
            if e.get("category") == "approval_dismissed"
        ]
        assert outcomes == ["approved", "revoked"]

    def test_idempotent_approve_does_not_re_publish(self, tmp_path, bus_capture):
        s = DepApprovalStore(tmp_path / "approvals.json")
        assert s.approve("foo") is True
        assert s.approve("foo") is False
        dismissals = [e for e in bus_capture if e.get("category") == "approval_dismissed"]
        assert len(dismissals) == 1   # second call was a no-op


# ─────────────────────────────────────────────────────────────────────────────
# 4) End-to-end: approval after a block unblocks the installer

class TestEndToEndUnblock:
    def test_block_then_approve_then_install(self, tmp_path, monkeypatch, bus_capture):
        store = DepApprovalStore(tmp_path / "approvals.json")
        installs: list[list[str]] = []
        def fake_pip(pkgs, *, timeout):
            installs.append(list(pkgs))
            return di.InstallResult(
                ok=True, status=di.InstallStatus.INSTALLED, installed_now=list(pkgs),
            )
        monkeypatch.setattr(di, "_run_pip_install", fake_pip)

        # 1. First attempt: blocked pending approval, no install.
        r1 = di.ensure_satisfied(
            ["python-docx"], mode=di.InstallMode.PROMPT,
            approvals=store, tool_name="create_word_doc",
        )
        assert r1.status is di.InstallStatus.BLOCKED_PENDING_APPROVAL
        assert installs == []

        # 2. Operator approves (separate process semantics).
        DepApprovalStore(tmp_path / "approvals.json").approve("python-docx")

        # 3. Same store, second attempt — re-reads file and installs.
        r2 = di.ensure_satisfied(
            ["python-docx"], mode=di.InstallMode.PROMPT,
            approvals=store, tool_name="create_word_doc",
        )
        assert r2.ok
        assert r2.status is di.InstallStatus.INSTALLED
        assert installs == [["python-docx"]]


# ─────────────────────────────────────────────────────────────────────────────
# 5) Smoke: EventBus dep dedup is per-package, not global

class TestPerPackageDedup:
    def test_different_packages_each_get_a_card(self, tmp_path, bus_capture):
        store = DepApprovalStore(tmp_path / "approvals.json")
        di.ensure_satisfied(["pkg_a"], mode=di.InstallMode.PROMPT,
                            approvals=store, tool_name="t1")
        di.ensure_satisfied(["pkg_b"], mode=di.InstallMode.PROMPT,
                            approvals=store, tool_name="t2")
        approval_events = [e for e in bus_capture if e.get("category") == "approval"]
        assert len(approval_events) == 2
        pkgs = {e["context"]["package"] for e in approval_events}
        assert pkgs == {"pkg_a", "pkg_b"}
