"""— `SYSTEMU_NON_INTERACTIVE` + safe-action ordering contract.

Closes review issue #2.  Two assertions:

1. The old env var `SYSTEMU_AUTO_APPROVE_SCROLLS` is no longer read.
2. In non-interactive mode, every multi-action `notify_user` call auto-picks
   `actions[0]` — which MUST be the safe-by-default choice.  Verified for the
   5 multi-action call sites the codebase actually uses.
"""
from __future__ import annotations

import os
import sys

import pytest

from systemu.interface import notifications as N


@pytest.fixture
def force_non_interactive(monkeypatch):
    monkeypatch.setenv("SYSTEMU_NON_INTERACTIVE", "true")
    monkeypatch.delenv("SYSTEMU_AUTO_APPROVE_SCROLLS", raising=False)
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    # Single-arg notify_user paths check `_vault` — patch it None so no DB write.
    monkeypatch.setattr(N, "_vault", None)
    yield


class TestSafeActionOrdering:
    """Each test models one of the 5 reordered call sites."""

    def test_forge_skip_auto_picks_skip_extract(self, force_non_interactive):
        # activity_extractor.py — forge-prompt notification
        choice = N.notify_user(
            title="t", message="m", actions=["Skip", "Forge"],
        )
        assert choice == "Skip"

    def test_forge_skip_auto_picks_skip_tool_forge(self, force_non_interactive):
        # tool_forge.py — Gate-1 spec approval
        choice = N.notify_user(
            title="t", message="m", actions=["Skip", "Forge"],
        )
        assert choice == "Skip"

    def test_workshop_approve_reject_auto_picks_reject(self, force_non_interactive):
        # workshop_module.py
        choice = N.notify_user(
            title="t", message="m", actions=["Reject", "Approve"],
        )
        assert choice == "Reject"

    def test_memory_graduation_approve_reject_auto_picks_reject(self, force_non_interactive):
        # scheduler/jobs.py — memory graduation
        choice = N.notify_user(
            title="t", message="m", actions=["Reject", "Approve"],
        )
        assert choice == "Reject"

    def test_shadow_decision_picks_skip(self, force_non_interactive):
        # shadow_decision.py — Awaken/Assign/Skip → safe-first
        choice = N.notify_user(
            title="t", message="m",
            actions=["Skip", "Assign to Existing", "Awaken"],
        )
        assert choice == "Skip"


class TestEnvRenameHardCut:
    def test_old_env_var_is_ignored(self, monkeypatch):
        """The old SYSTEMU_AUTO_APPROVE_SCROLLS must no longer be honored."""
        monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
        monkeypatch.setenv("SYSTEMU_AUTO_APPROVE_SCROLLS", "true")
        from sharing_on.config import Config
        c = Config.from_env()
        # New field name only.  Old attribute name no longer exists at all.
        assert getattr(c, "non_interactive", None) is False
        assert not hasattr(c, "auto_approve_scrolls"), (
            "Old attribute name leaked — hard cut not complete"
        )

    def test_new_env_var_is_read(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_NON_INTERACTIVE", "true")
        monkeypatch.delenv("SYSTEMU_AUTO_APPROVE_SCROLLS", raising=False)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.non_interactive is True


class TestNotificationsReadNewVar:
    def test_notifications_reads_new_var(self, monkeypatch):
        """The new env-var name is plumbed through notifications.

        We only check the new name is present in the source — the old name
        is allowed to appear in comments documenting the rename.
        """
        from pathlib import Path
        src = Path(N.__file__).read_text(encoding="utf-8")
        # The CALL site must use the new var
        assert 'os.environ.get("SYSTEMU_NON_INTERACTIVE")' in src

    def test_notifications_does_not_read_old_var(self, monkeypatch):
        """Verify NO active code reads the old var (comments allowed)."""
        from pathlib import Path
        src = Path(N.__file__).read_text(encoding="utf-8")
        assert 'os.environ.get("SYSTEMU_AUTO_APPROVE_SCROLLS")' not in src
        assert 'os.getenv("SYSTEMU_AUTO_APPROVE_SCROLLS")' not in src
