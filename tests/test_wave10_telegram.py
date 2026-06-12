"""W10.1 — Telegram reach: the dormant gateway wired into the daemon.

messaging/telegram_gateway.py (long-poll + push + allowlist) and
event_pusher.py (EventBus→gateway bridge with rate limits) both shipped
complete and were started by NOTHING. This slice wires them into the daemon
boot and teaches the translator the modern event shapes (W5.3's
operator_decision_posted, W8's task_outcome) so needs-you items and task
outcomes reach the operator's phone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from systemu.messaging.event_pusher import EventPusher, translate_event


class TestTranslatorModernEvents:
    def test_decision_posted_pushes_needs_you(self):
        msg = translate_event({
            "category": "operator_decision_posted", "level": "WARNING",
            "message": "Needs you: Stuck on Objective 2: 'Search for cheap spas'",
            "context": {"decision_id": "dec_1",
                        "title": "Stuck on Objective 2: 'Search for cheap spas'"},
        })
        assert msg is not None
        assert "Needs you" in msg.text and "cheap spas" in msg.text
        assert msg.category == "approval"   # unlimited rate bucket

    def test_task_outcome_success_pushes_with_summary(self):
        msg = translate_event({
            "category": "task_outcome", "level": "SUCCESS",
            "message": "Task success: compile the weekly report",
            "details": {"summary": "Report saved with 12 rows.",
                        "files": ["C:/out/report.xlsx"]},
        })
        assert msg is not None
        assert "✅" in msg.text and "weekly report" in msg.text
        assert "Report saved" in msg.text

    def test_task_outcome_failure_pushes_warning(self):
        msg = translate_event({
            "category": "task_outcome", "level": "ERROR",
            "message": "Task failed: scrape the portal",
            "details": {"summary": ""},
        })
        assert msg is not None and "⚠️" in msg.text

    def test_quick_task_iteration_noise_is_dropped(self):
        assert translate_event({
            "category": "quick_task", "level": "INFO",
            "message": "[3/12] web_search → ok",
        }) is None

    def test_decision_resolved_is_dropped(self):
        assert translate_event({
            "category": "operator_decision_resolved", "level": "INFO",
            "message": "Resolved: Stuck on Objective 2",
        }) is None


class TestPusherIntegration:
    def test_decision_event_reaches_the_gateway(self):
        class FakeGateway:
            def __init__(self):
                self.pushed = []

            def push(self, message):
                self.pushed.append(message)

        class FakeBus:
            def __init__(self):
                self.cb = None

            def subscribe(self, cb, replay=False):
                self.cb = cb
                return lambda: None

        gw, bus = FakeGateway(), FakeBus()
        pusher = EventPusher(gw)
        pusher.subscribe(bus)
        bus.cb({"category": "operator_decision_posted", "level": "WARNING",
                "message": "Needs you: Approve scroll X", "context": {}})
        bus.cb({"category": "quick_task", "level": "INFO",
                "message": "[1/12] noise"})
        assert len(gw.pushed) == 1
        assert "Approve scroll X" in gw.pushed[0].text


class TestStatusHandler:
    def test_reports_pending_and_recent(self, tmp_path: Path):
        from systemu.vault.vault import Vault
        from systemu.messaging.handlers import build_status_handler
        (tmp_path / "elder").mkdir(parents=True, exist_ok=True)
        vault = Vault(str(tmp_path))
        vault.append_chat_history({
            "ts": "2026-06-12T10:00:00", "prompt": "weekly report",
            "status": "success", "summary": "done",
        })
        handler = build_status_handler(vault)
        reply = handler(object())
        assert "0" in reply              # nothing needs you
        assert "weekly report" in reply  # recent task listed

    def test_never_raises(self):
        from systemu.messaging.handlers import build_status_handler
        handler = build_status_handler(object())
        assert isinstance(handler(object()), str)


class TestDaemonWiring:
    def test_daemon_starts_gateway_and_pusher(self):
        import inspect
        from systemu.scheduler import daemon
        src = inspect.getsource(daemon)
        assert "build_from_env" in src and "EventPusher" in src, \
            "the daemon must start the gateway + pusher when configured"

    def test_settings_shows_telegram_status(self):
        import inspect
        from systemu.interface.pages import settings
        src = inspect.getsource(settings)
        assert "SHARING_ON_TELEGRAM_BOT_TOKEN" in src, \
            "Settings must show how to configure Telegram (status only)"