"""W5.2 — the header Status dropdown: task list with status, outcome, links.

Pins the pure row model (build_status_rows over the vault chat history), the
outcome plumbing (terminal chat-history writes now persist the run's
final_summary), and the header wiring.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    (tmp_path / "elder").mkdir(parents=True, exist_ok=True)
    return Vault(str(tmp_path))


class TestBuildStatusRows:
    def _seed(self, vault):
        vault.append_chat_history({
            "ts": "2026-06-12T10:00:00", "prompt": "find the nearest salon",
            "scroll_id": "scroll_1", "status": "success",
            "summary": "Found 3 salons near T Nagar; list saved.",
        })
        vault.append_chat_history({
            "ts": "2026-06-12T11:00:00", "prompt": "scrape the news",
            "scroll_id": "scroll_2", "status": "failed",
            "error": "extraction returned no activity",
        })
        vault.append_chat_history({
            "ts": "2026-06-12T12:00:00", "prompt": "book parking",
            "scroll_id": "scroll_3", "status": "pending_decision",
            "decision_id": "dec_9",
        })

    def test_rows_newest_first_with_outcome_and_target(self, vault):
        from systemu.interface.components.status_menu import build_status_rows
        self._seed(vault)
        rows = build_status_rows(vault)
        assert [r["name"] for r in rows] == [
            "book parking", "scrape the news", "find the nearest salon"]
        # Outcome priority: summary, then error, then status fallback copy.
        assert rows[2]["outcome"] == "Found 3 salons near T Nagar; list saved."
        assert rows[1]["outcome"] == "extraction returned no activity"
        assert "Inbox" in rows[0]["outcome"]          # pending_decision fallback
        # Workflow click-through.
        assert rows[0]["target"] == "/workflow/scroll_3"
        assert rows[2]["target"] == "/workflow/scroll_1"
        # Attention flag drives the warn tint.
        assert rows[0]["attention"] is True
        assert rows[2]["attention"] is False
        # Compact timestamp.
        assert rows[2]["ts_display"] == "2026-06-12 10:00"

    def test_entry_without_scroll_has_no_target(self, vault):
        from systemu.interface.components.status_menu import build_status_rows
        vault.append_chat_history({
            "ts": "2026-06-12T09:00:00", "prompt": "x", "status": "failed",
            "error": "early pipeline failure",
        })
        rows = build_status_rows(vault)
        assert rows[0]["target"] is None

    def test_defensive_on_broken_vault(self):
        from systemu.interface.components.status_menu import build_status_rows
        assert build_status_rows(object()) == []


class TestOutcomePlumbing:
    def test_sync_terminal_write_persists_summary(self):
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task)
        assert '"summary":      result.get("final_summary")' in src, \
            "sync terminal chat-history write must persist the outcome summary"

    def test_queued_terminal_write_persists_summary(self):
        from systemu.pipelines.direct_task import _wire_chat_history_completion
        src = inspect.getsource(_wire_chat_history_completion)
        assert "final_summary" in src, \
            "queued-path completion write must persist the outcome summary"


class TestHeaderWiring:
    def test_status_menu_rendered_in_header(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard._build_layout)
        assert "render_status_menu" in src, \
            "the Status dropdown must sit in the header next to Needs you"
