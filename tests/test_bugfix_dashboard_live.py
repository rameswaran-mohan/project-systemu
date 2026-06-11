"""Regression tests for the four dashboard bugs reported post-v0.9.13.

BUG-4 (the load-bearing one): the dashboard is served by uvicorn, which on
Windows runs on a SelectorEventLoop — where asyncio.create_subprocess_exec
raises NotImplementedError. W2.2 routed forged tools through that subprocess
path, so every forged-tool call died silently → "task stuck after
activities". The LocalBackend now runs the child via a thread + blocking
subprocess.run, which is loop- and platform-agnostic.
"""
import asyncio
import os
import sys
import tempfile

import pytest

from systemu.runtime.tool_sandbox import ToolSandbox


def _make_echo_tool() -> str:
    d = tempfile.mkdtemp(prefix="bugfix_tools_")
    p = os.path.join(d, "echo_tool.py")
    open(p, "w", encoding="utf-8").write(
        "import json\nprint(json.dumps({'success': True, 'echo': 'ok'}))\n"
    )
    return p


def _run_tool_on(loop, impl: str):
    sandbox = ToolSandbox(os.path.dirname(impl))
    return loop.run_until_complete(
        sandbox.execute_tool(impl, {}, force_subprocess=True, timeout=30)
    )


class TestBug4SubprocessLoopAgnostic:
    def test_subprocess_tool_succeeds_on_proactor_loop(self):
        impl = _make_echo_tool()
        loop = (asyncio.ProactorEventLoop()
                if sys.platform == "win32" else asyncio.new_event_loop())
        try:
            res = _run_tool_on(loop, impl)
        finally:
            loop.close()
        assert res.success and res.parsed.get("echo") == "ok"

    @pytest.mark.skipif(sys.platform != "win32", reason="Selector-loop subprocess gap is Windows-specific")
    def test_subprocess_tool_succeeds_on_selector_loop(self):
        # THE regression: this is the loop uvicorn serves the dashboard on.
        impl = _make_echo_tool()
        loop = asyncio.SelectorEventLoop()
        try:
            res = _run_tool_on(loop, impl)
        finally:
            loop.close()
        assert res.success, f"subprocess tool failed on SelectorEventLoop: {res.error!r}"
        assert res.parsed.get("echo") == "ok"

    def test_local_backend_uses_thread_not_create_subprocess_exec(self):
        import inspect
        from systemu.runtime.backend import local
        src = inspect.getsource(local)
        assert "asyncio.to_thread" in src
        # the loop-fragile CALL is gone (the name survives only in a comment)
        assert "await asyncio.create_subprocess_exec(" not in src


class TestBug2DetailsRendererShared:
    def test_details_body_renderer_is_module_level(self):
        from systemu.interface.components.live_events_pane import (
            render_event_details_body, _has_details,
        )
        assert callable(render_event_details_body)
        assert _has_details({"details": {"reasoning": "x"}}) is True
        assert _has_details({"details": {}}) is False
        assert _has_details({}) is False

    def test_chat_feed_renders_expand_arrow(self):
        import inspect
        from systemu.interface.pages import systemu_chat
        src = inspect.getsource(systemu_chat)
        assert "render_event_details_body" in src and "ui.expansion" in src


class TestBug1LiveTimerNotBuildGated:
    def test_pane_schedules_timer_unconditionally(self):
        # The build-time has_socket_connection gate (which is always False during
        # the initial render) is gone; the pane uses safe_timer directly.
        import inspect
        from systemu.interface.components import live_events_pane
        src = inspect.getsource(live_events_pane.build_supervisor_events_pane)
        # timer scheduled directly (no build-time has_socket_connection gate,
        # which is always False during the initial render)
        assert "safe_timer(0.5, _tick)" in src
        assert "if client is None or _should_schedule_refresh" not in src

    def test_home_restores_live_panes(self):
        import inspect
        from systemu.interface.pages import console
        src = inspect.getsource(console)
        assert "build_supervisor_events_pane" in src
