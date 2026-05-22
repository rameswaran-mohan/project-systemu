"""Web Extension Collector — runs a local server to receive browser DOM events.

Instead of writing complex Native Messaging host installers that manipulate the Windows Registry,
we run a simple HTTP server on localhost. The Chrome Extension directly POSTs
DOM events (clicks, input changes) to this server as long as `sharing_on record` is running.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)

PORT = 49494

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

    def handle_extension_event(self, payload: dict) -> None:
        """Called by the HTTP Request Handler when data arrives."""
        ts = datetime.now(timezone.utc)
        
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
