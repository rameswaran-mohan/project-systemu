"""Web Extension Collector — runs a local server to receive browser DOM events.

Instead of writing complex Native Messaging host installers that manipulate the Windows Registry,
we run a simple HTTP server on localhost. The Chrome Extension directly POSTs
DOM events (clicks, input changes) to this server as long as `sharing_on record` is running.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlsplit

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)

PORT = 49494


# v0.9.32 FIX 2A: the dashboard binds 127.0.0.1 and stamps the origin as
# http://127.0.0.1:<port>, but operators open http://localhost:<port>. A raw
# hostname compare ('localhost' != '127.0.0.1') made Layer 1 dead in the common
# case. Collapse every loopback alias to one canonical bucket before comparing.
_LOOPBACK_ALIASES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", None})


def _canon_host(host: Optional[str]) -> Optional[str]:
    """Map any loopback alias to a single canonical host; pass others through."""
    if host in _LOOPBACK_ALIASES:
        return "\x00loopback"
    return host


def _same_origin(url: str, origin: str) -> bool:
    """True if `url`'s scheme://host:port equals `origin`. Path/query ignored.

    Loopback aliases ({localhost, 127.0.0.1, ::1, 0.0.0.0}) are treated as one
    canonical host so a 127.0.0.1-stamped origin matches a localhost-opened tab.
    Non-loopback hosts are compared by hostname as before.
    """
    if not url or not origin:
        return False
    u, o = urlsplit(url), urlsplit(origin)
    return (
        (u.scheme, _canon_host(u.hostname), u.port)
        == (o.scheme, _canon_host(o.hostname), o.port)
    )


def _is_origin_target(target: str) -> bool:
    """True if a single-source capture target looks like a browser origin (has a
    scheme), e.g. 'https://github.com'. App-process targets ('chrome.exe')
    return False so the origin allow-list does not engage for them."""
    return "://" in (target or "")


class ExtensionRequestHandler(BaseHTTPRequestHandler):
    """Handles POST requests from the Chrome Extension."""
    
    # We will inject the collector reference onto the server object so the handler can access it
    def do_POST(self):
        if self.path == "/event":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                payload = json.loads(post_data.decode('utf-8'))
                
                # Forward to the collector via the server object
                if hasattr(self.server, 'collector_ref'):
                    self.server.collector_ref.handle_extension_event(payload)
                    
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            except Exception as e:
                logger.error(f"Failed to parse extension payload: {e}")
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
            
    def do_OPTIONS(self):
        # Reject pre-flight requests from normal webpages.
        # Only the extension background script (which bypasses CORS) should hit this.
        self.send_response(403)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress standard HTTP server logging to keep CLI clean
        pass


class WebExtensionCollector(BaseCollector):
    name = "web_extension"

    def __init__(self, event_store: EventStore):
        super().__init__(event_store)
        self._httpd: Optional[HTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
            
        self._running = True
        self._error = None

        try:
            self._httpd = HTTPServer(('127.0.0.1', PORT), ExtensionRequestHandler)
            self._httpd.collector_ref = self  # type: ignore
            
            self._server_thread = threading.Thread(
                target=self._httpd.serve_forever,
                daemon=True,
                name="extension-http-server"
            )
            self._server_thread.start()
            logger.info(f"Collector '{self.name}' listening on 127.0.0.1:{PORT}")
        except Exception as e:
            logger.error(f"Failed to start Extension server: {e}")
            self._error = e

    def stop(self) -> None:
        self._running = False
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2.0)
            
        logger.info(f"Collector '{self.name}' stopped")

    def _collect_loop(self) -> None:
        # Overridden because HTTPServer handles the loop natively
        pass

    def should_capture(self, event) -> bool:
        """Web events do their OWN sources filtering in handle_extension_event (the
        single-mode origin allow-list below); the base process-name match would
        wrongly drop them because their application is the generic
        "Chrome/Edge Web Browser", never a process/origin token. Always allow at
        the base hook — single-source narrowing already happened before emit()."""
        return True

    def handle_extension_event(self, payload: dict) -> None:
        """Called by the HTTP Request Handler when data arrives."""
        ts = datetime.now(timezone.utc)

        # v0.9.32 Item 2, Layer 1: default-drop captures of systemu's OWN
        # dashboard UI. The dashboard is a browser tab, so a PID/window filter
        # can't discriminate it from legit browser tasks — only the URL origin
        # can. SYSTEMU_DASHBOARD_ORIGIN is set by dispatch at spawn time.
        dashboard_origin = os.environ.get("SYSTEMU_DASHBOARD_ORIGIN", "")
        if dashboard_origin and _same_origin(payload.get("url", ""), dashboard_origin):
            logger.debug("Dropping dashboard-origin DOM event: %s", payload.get("url"))
            return

        # v0.9.35 Phase 0: single-mode origin allow-list. When the single source
        # is a URL origin (not a browser process), keep ONLY events whose URL
        # matches that origin — a generalization of the dashboard drop above.
        sources = getattr(self, "_sources", None)
        if sources is not None and sources.is_single and _is_origin_target(sources.source_app):
            if not _same_origin(payload.get("url", ""), sources.source_app):
                logger.debug("Single source dropped off-origin DOM event: %s",
                             payload.get("url"))
                return

        # Determine specific action
        action_str = payload.get("action", "mouse_click")
        if "input" in action_str:
            action = EventAction.KEY_PRESS if hasattr(EventAction, "KEY_PRESS") else EventAction.STEP_MARKER
        else:
            action = EventAction.MOUSE_CLICK if hasattr(EventAction, "MOUSE_CLICK") else EventAction.STEP_MARKER

        # Map to CaptureEvent model
        event = CaptureEvent(
            category=EventCategory.INTERACTION,
            action=action,
            timestamp=ts,
            application="Chrome/Edge Web Browser",  # Override in LLM or merge phase
            window_title=payload.get("tab_title", "Unknown Webpage"),
            data={
                "url": payload.get("url", ""),
                "element_tag": payload.get("element_tag", ""),
                "element_type": payload.get("element_type", ""),
                "element_text": payload.get("element_text", ""),
                "element_xpath": payload.get("element_xpath", ""),
                "value": payload.get("value", "")
            }
        )
        self.emit(event)
