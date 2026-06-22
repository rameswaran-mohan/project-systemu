"""W7 — dashboard responsiveness + liveness field fixes.

Five operator reports from live 0.9.16 use, root-caused:

1. FREEZE ON APPROVE — resolve_gate EXECUTES (LLM extraction, pip installs,
   dry-runs) synchronously inside the NiceGUI click handler, starving the
   websocket → "brief connection loss and screen freeze". Handlers are now
   async and run the resolve chain via asyncio.to_thread.

2. LIVE PANES DIE UNTIL MANUAL REFRESH — cleanup was registered on
   ``app.on_disconnect``, which in NiceGUI is GLOBAL (fires for EVERY
   client's disconnect) — so any navigation/transient drop anywhere killed
   every pane's EventBus subscription process-wide, and NiceGUI reconnects
   the same page without rebuilding. Cleanup now uses the per-client
   ``client.on_delete`` (true deletion only).

3. CHAT TASKS SERIALIZED — the Compose submit button was disabled for the
   entire duration of a sync run; concurrent submissions were impossible
   from the UI even though each run threads independently.

4. Supervisor concurrency is env-tunable (SYSTEMU_MAX_CONCURRENT_SHADOWS).
"""
from __future__ import annotations

import inspect


# ── W7.1: approve handlers must not block the UI event loop ─────────────────
class TestApproveOffTheLoop:
    def test_inbox_card_resolver_is_async_threaded(self):
        from systemu.interface.pages import inbox_page
        src = inspect.getsource(inbox_page._render_unified_card)
        assert "async def _click" in src, \
            "the unified card's resolve handler must be async (non-blocking)"
        assert "asyncio.to_thread" in src, \
            "resolve_gate EXECUTES (LLM/pip/dry-run) — it must run off the loop"

    def test_rail_quick_approve_is_async_threaded(self):
        from systemu.interface.components import inbox_rail
        src = inspect.getsource(inbox_rail.build_inbox_rail_section)
        assert "async def _on_approve" in src
        assert "asyncio.to_thread" in src

    def test_blocking_work_extracted_pure(self):
        """The thread target is a pure function — resolve + execute, no UI."""
        from systemu.interface.pages.inbox_page import _resolve_and_execute_gate
        sig = inspect.signature(_resolve_and_execute_gate)
        assert list(sig.parameters) == ["dec_id", "choice", "vault"]


# ── W7.2: cleanup on true client deletion, not global/transient disconnect ──
class TestLivenessSurvivesTransientDisconnect:
    def test_no_global_on_disconnect_cleanup_remains(self):
        """app.on_disconnect is GLOBAL — one client's drop killed every pane."""
        import systemu.interface.components.live_events_pane as lep
        import systemu.interface.components.right_rail as rr
        import systemu.interface.components.live_objectives_pane as lop
        import systemu.interface.pages.chat_page as cp
        for mod in (lep, rr, lop, cp):
            src = inspect.getsource(mod)
            assert "app.on_disconnect(" not in src, \
                f"{mod.__name__} still registers a GLOBAL disconnect handler"

    def test_panes_clean_up_on_client_delete(self):
        import systemu.interface.components.live_events_pane as lep
        import systemu.interface.components.right_rail as rr
        import systemu.interface.components.live_objectives_pane as lop
        for mod in (lep, rr, lop):
            src = inspect.getsource(mod)
            assert "on_delete" in src, \
                f"{mod.__name__} must unsubscribe when the client is truly deleted"

    def test_systemu_chat_uses_on_delete_too(self):
        import systemu.interface.pages.systemu_chat as sc
        src = inspect.getsource(sc)
        assert "client.on_delete(_cleanup)" in src
        assert "client.on_disconnect(_cleanup)" not in src, \
            "per-client on_disconnect still kills the feed on transient drops"


# ── W7.4: parallel task execution ────────────────────────────────────────────
class TestParallelSubmission:
    def test_chat_submit_not_disabled_during_run(self):
        """The Compose button was disabled until the sync run finished —
        the UI itself serialized chat tasks."""
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page.build_chat_page)
        assert "set_enabled(False)" not in src, \
            "submit must stay enabled — each submission runs in its own thread"

    def test_supervisor_concurrency_env_tunable(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_MAX_CONCURRENT_SHADOWS", "5")
        from systemu.runtime.supervisor import _resolve_max_concurrent
        assert _resolve_max_concurrent() == 5
        monkeypatch.delenv("SYSTEMU_MAX_CONCURRENT_SHADOWS")
        assert _resolve_max_concurrent() == 3  # default unchanged

    def test_supervisor_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_MAX_CONCURRENT_SHADOWS", "zero")
        from systemu.runtime.supervisor import _resolve_max_concurrent
        assert _resolve_max_concurrent() == 3
