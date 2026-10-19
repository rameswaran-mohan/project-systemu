"""ONE Telegram gateway per daemon process, owned by the daemon.

Witnessed defect: the daemon started a gateway at boot AND ``run_dashboard`` -
which the daemon launches inside the SAME process - started a second one on the
same bot token. Two ``getUpdates`` pollers on one token conflict (Telegram
answers one of them 409 Conflict) and every needs-you push went out twice, once
per gateway's EventPusher.

Ruling pinned here:
  * the daemon owns the process's one gateway; the dashboard never starts its
    own (it may read the daemon's handle for status);
  * a process-level singleton guard keyed on the bot token makes a second
    ``start()`` return the existing instance (and say so at INFO, once), so no
    future call site can reintroduce the pair.

No network, no real daemon: ``telegram.ext`` is replaced in ``sys.modules`` by
a fake whose ``Application.run_polling`` records itself and blocks until
``stop_running`` - i.e. the poller is counted at the HTTP-client seam.
"""
from __future__ import annotations

import ast
import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_TOKEN = "123456:FAKE-test-token-not-real"
_OTHER_TOKEN = "654321:FAKE-other-token-not-real"
_SYSTEMU = Path(__file__).resolve().parent.parent / "systemu"


# -- fake python-telegram-bot (the HTTP-client seam) --------------------------

class _FakeFilter:
    def __and__(self, other):
        return self

    def __invert__(self):
        return self


class _FakeApp:
    def __init__(self, registry, token):
        self._registry = registry
        self.token = token
        self.handlers = []
        self._stopped = threading.Event()
        self.bot = types.SimpleNamespace(_loop=None)

    def add_handler(self, handler):
        self.handlers.append(handler)

    def run_polling(self, stop_signals=None):
        # One call == one getUpdates long-poller on ``self.token``.
        with self._registry["lock"]:
            self._registry["polling"].append(self)
        self._stopped.wait(timeout=30)

    def stop_running(self):
        self._stopped.set()


def _fake_telegram_modules(registry):
    class _Builder:
        def token(self, t):
            self._t = t
            return self

        def build(self):
            app = _FakeApp(registry, self._t)
            with registry["lock"]:
                registry["apps"].append(app)
            return app

    class Application:
        @staticmethod
        def builder():
            return _Builder()

    ext = types.ModuleType("telegram.ext")
    ext.Application = Application
    ext.CommandHandler = lambda name, cb: ("command", name, cb)
    ext.CallbackQueryHandler = lambda cb: ("callback", cb)
    ext.MessageHandler = lambda flt, cb: ("message", cb)
    ext.filters = types.SimpleNamespace(TEXT=_FakeFilter(), COMMAND=_FakeFilter())
    pkg = types.ModuleType("telegram")
    pkg.ext = ext
    return pkg, ext


class _FakeBus:
    """Stands in for the process EventBus; records every subscriber."""

    def __init__(self):
        self.subscribers = []

    def subscribe(self, cb, replay=False):
        self.subscribers.append(cb)

        def _unsub():
            if cb in self.subscribers:
                self.subscribers.remove(cb)
        return _unsub

    def publish(self, event):
        for cb in list(self.subscribers):
            cb(event)


def _gateway_threads():
    return [t for t in threading.enumerate()
            if t.name == "telegram-gateway" and t.is_alive()]


@pytest.fixture
def tg_env(monkeypatch):
    """Configured env + fake telegram + fake bus; tears every poller down."""
    from systemu.messaging import telegram_gateway as tgmod
    from systemu.interface.event_bus import EventBus

    registry = {"lock": threading.Lock(), "polling": [], "apps": []}
    pkg, ext = _fake_telegram_modules(registry)
    monkeypatch.setitem(sys.modules, "telegram", pkg)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")

    bus = _FakeBus()
    monkeypatch.setattr(EventBus, "get", classmethod(lambda cls: bus))

    pushes = []
    monkeypatch.setattr(tgmod.TelegramGateway, "push",
                        lambda self, message: pushes.append((self, message)))

    before = set(_gateway_threads())
    yield types.SimpleNamespace(registry=registry, bus=bus, pushes=pushes,
                                before=before, tgmod=tgmod)

    for app in list(registry["apps"]):
        app.stop_running()
    for t in _gateway_threads():
        if t not in before:
            t.join(timeout=5)
    running = getattr(tgmod, "_RUNNING", None)
    if isinstance(running, dict):
        running.clear()


def _settle(registry, expected_min=1, grace=0.3):
    """Wait until at least ``expected_min`` pollers entered, then a grace
    period so a would-be SECOND poller has time to show up and be counted."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with registry["lock"]:
            if len(registry["polling"]) >= expected_min:
                break
        time.sleep(0.01)
    time.sleep(grace)
    with registry["lock"]:
        return list(registry["polling"])


class _State:
    """Minimal AppState stand-in for the dashboard's messaging seam."""
    messaging_gateway = None
    messaging_pusher = None


# -- the witnessed defect: daemon boot + in-process dashboard -----------------

def test_daemon_then_dashboard_yields_exactly_one_poller(tg_env):
    from systemu.scheduler import daemon
    from systemu.interface import dashboard

    gw = daemon._start_messaging(object())
    state = _State()
    dashboard._attach_messaging(state)

    pollers = _settle(tg_env.registry)
    new_threads = [t for t in _gateway_threads() if t not in tg_env.before]
    assert len(pollers) == 1, (
        f"{len(pollers)} getUpdates pollers on one bot token -- Telegram 409s "
        f"all but one; the daemon must own the ONE gateway")
    assert len(new_threads) == 1
    assert gw is not None
    # The dashboard may READ the daemon's handle for status - never its own.
    assert state.messaging_gateway is gw


def test_one_decision_event_is_pushed_once(tg_env):
    from systemu.scheduler import daemon
    from systemu.interface import dashboard

    daemon._start_messaging(object())
    dashboard._attach_messaging(_State())
    _settle(tg_env.registry)

    tg_env.bus.publish({
        "category": "operator_decision_posted", "level": "WARNING",
        "message": "Needs you: approve the report",
        "context": {"decision_id": "dec_1", "title": "approve the report"},
    })
    assert len(tg_env.pushes) == 1, (
        f"one needs-you event produced {len(tg_env.pushes)} pushes")


def test_daemon_start_twice_reuses_and_pushes_once(tg_env):
    from systemu.scheduler import daemon

    first = daemon._start_messaging(object())
    second = daemon._start_messaging(object())
    assert second is first
    assert len(_settle(tg_env.registry)) == 1
    tg_env.bus.publish({
        "category": "operator_decision_posted", "level": "WARNING",
        "message": "Needs you: approve the report",
        "context": {"decision_id": "dec_1", "title": "approve the report"},
    })
    assert len(tg_env.pushes) == 1


def test_dashboard_path_starts_nothing(tg_env, monkeypatch):
    from systemu.interface import dashboard

    starts = []
    real_start = tg_env.tgmod.TelegramGateway.start

    def _spy(self):
        starts.append(self)
        return real_start(self)

    monkeypatch.setattr(tg_env.tgmod.TelegramGateway, "start", _spy)
    state = _State()
    dashboard._attach_messaging(state)
    assert starts == [], "the dashboard must never start a Telegram gateway"
    assert tg_env.registry["apps"] == []
    # No daemon gateway in this process -> nothing to show, nothing started.
    assert state.messaging_gateway is None


# -- the singleton guard ------------------------------------------------------

def test_second_explicit_start_returns_the_same_instance(tg_env, caplog):
    TG = tg_env.tgmod.TelegramGateway
    first = TG(bot_token=_TOKEN, allowlist={"111"})
    second = TG(bot_token=_TOKEN, allowlist={"111"})

    assert first.start() is first
    with caplog.at_level(logging.INFO, logger=tg_env.tgmod.__name__):
        assert second.start() is first
        assert second.start() is first
    pollers = _settle(tg_env.registry)
    assert len(pollers) == 1
    assert len([t for t in _gateway_threads() if t not in tg_env.before]) == 1
    dup_lines = [r for r in caplog.records
                 if r.levelno == logging.INFO and "already polling" in r.getMessage()]
    assert len(dup_lines) == 1, [r.getMessage() for r in caplog.records]
    assert all(ord(c) < 128 for c in dup_lines[0].getMessage())
    assert _TOKEN not in dup_lines[0].getMessage()
    assert tg_env.tgmod.running_gateway() is first


def test_guard_is_keyed_on_the_bot_token(tg_env):
    TG = tg_env.tgmod.TelegramGateway
    a = TG(bot_token=_TOKEN, allowlist={"111"})
    b = TG(bot_token=_OTHER_TOKEN, allowlist={"111"})
    assert a.start() is a
    assert b.start() is b
    pollers = _settle(tg_env.registry, expected_min=2)
    assert sorted(p.token for p in pollers) == sorted([_TOKEN, _OTHER_TOKEN])


def test_stopped_owner_releases_the_token(tg_env):
    TG = tg_env.tgmod.TelegramGateway
    first = TG(bot_token=_TOKEN, allowlist={"111"})
    first.start()
    _settle(tg_env.registry)
    first.stop()
    assert tg_env.tgmod.running_gateway() is None
    second = TG(bot_token=_TOKEN, allowlist={"111"})
    assert second.start() is second


# -- reachability: the daemon is the ONLY production starter ------------------

_STARTER_NAMES = {"build_from_env", "TelegramGateway"}


def _starter_files() -> set:
    """Every systemu/ file outside messaging/ that can construct a gateway:
    imports ``build_from_env``/``TelegramGateway`` from the telegram module
    (aliased or not) or reaches them as an attribute of it."""
    out: set = set()
    for py in _SYSTEMU.rglob("*.py"):
        rel = py.relative_to(_SYSTEMU).as_posix()
        if rel.startswith("messaging/"):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and (node.module or "").endswith("telegram_gateway")
                    and any(a.name in _STARTER_NAMES for a in node.names)):
                out.add(rel)
            elif (isinstance(node, ast.Attribute)
                  and node.attr in _STARTER_NAMES
                  and isinstance(node.value, ast.Name)
                  and "telegram" in node.value.id):
                out.add(rel)
    return out


def test_only_the_daemon_starts_a_telegram_gateway():
    assert _starter_files() == {"scheduler/daemon.py"}


def test_daemon_boot_reaches_the_messaging_seam():
    import inspect
    from systemu.scheduler import daemon
    assert "_start_messaging(vault)" in inspect.getsource(daemon._run_daemon_loop)


def test_dashboard_boot_reaches_the_messaging_seam():
    import inspect
    from systemu.interface import dashboard
    assert "_attach_messaging(state)" in inspect.getsource(dashboard.run_dashboard)
