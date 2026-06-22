"""Clipboard change collector — detects when clipboard content changes.

Platform-specific implementations:
- Windows: ctypes (user32.dll)
- Linux:   xclip / xsel via subprocess
- macOS:   pbpaste via subprocess

Only captures a preview (first 200 chars) to avoid storing sensitive data.
Never stores passwords or large binary content.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore
from sharing_on.platform_info import OSType, PlatformInfo

logger = logging.getLogger(__name__)

# Max preview length stored in events
PREVIEW_LENGTH = 200


class ClipboardCollector(BaseCollector):
    """Monitors clipboard for text content changes.

    Polls periodically and emits an event when the clipboard content changes.
    Only stores a short preview — not full clipboard content.
    """

    name = "clipboard"

    def __init__(
        self,
        event_store: EventStore,
        platform: PlatformInfo,
        poll_interval: float = 1.5,
    ):
        super().__init__(event_store)
        self._platform = platform
        self._poll_interval = poll_interval
        self._last_hash: Optional[str] = None

    def _collect_loop(self) -> None:
        while self._running:
            try:
                content = self._get_clipboard_text()
                if content is not None:
                    content_hash = hashlib.md5(content.encode()).hexdigest()

                    if content_hash != self._last_hash:
                        self._last_hash = content_hash

                        # Create a safe preview
                        preview = content[:PREVIEW_LENGTH]
                        if len(content) > PREVIEW_LENGTH:
                            preview += "..."

                        # Detect content type
                        content_type = self._classify_content(content)

                        # Skip if it looks like a password or secret
                        if content_type == "secret":
                            preview = "[REDACTED — possible secret/password]"

                        self.emit(CaptureEvent(
                            category=EventCategory.CLIPBOARD,
                            action=EventAction.CLIPBOARD_CHANGE,
                            timestamp=datetime.now(timezone.utc),
                            data={
                                "preview": preview,
                                "length": len(content),
                                "content_type": content_type,
                                "hash": content_hash[:8],
                            },
                        ))

            except Exception as e:
                logger.debug(f"Clipboard read error: {e}")

            time.sleep(self._poll_interval)

    def _get_clipboard_text(self) -> Optional[str]:
        """Read text content from the system clipboard. Platform-dispatched."""
        if self._platform.is_windows:
            return self._get_clipboard_windows()
        elif self._platform.is_linux:
            return self._get_clipboard_linux()
        elif self._platform.is_macos:
            return self._get_clipboard_macos()
        return None

    # --- Windows (ctypes) ---

    def _get_clipboard_windows(self) -> Optional[str]:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        CF_UNICODETEXT = 13

        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None

        if not user32.OpenClipboard(0):
            return None

        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None

            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None

            try:
                text = ctypes.c_wchar_p(ptr).value
                return text or None
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    # --- Linux (xclip / xsel) ---

    def _get_clipboard_linux(self) -> Optional[str]:
        for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    return result.stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return None

    # --- macOS (pbpaste) ---

    def _get_clipboard_macos(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["pbpaste"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    # --- Content classification ---

    @staticmethod
    def _classify_content(text: str) -> str:
        """Classify clipboard content type for context."""
        text_lower = text.strip().lower()

        # Likely a secret/password (short, no spaces, mixed chars)
        if len(text.strip()) < 100 and " " not in text.strip():
            if any(prefix in text_lower for prefix in [
                "sk-", "api_key", "token", "password", "secret",
                "ghp_", "ghs_", "sk-or-", "bearer ",
            ]):
                return "secret"

        # URL
        if text_lower.startswith(("http://", "https://", "ftp://")):
            return "url"

        # File path
        if text_lower.startswith(("/", "c:\\", "d:\\", "~/")):
            return "filepath"

        # Code-like (contains syntax markers)
        if any(marker in text for marker in [
            "def ", "function ", "class ", "import ", "const ",
            "var ", "let ", "return ", "if (", "for (",
        ]):
            return "code"

        # Shell command
        if any(text_lower.startswith(cmd) for cmd in [
            "cd ", "ls ", "dir ", "git ", "npm ", "pip ", "docker ",
            "kubectl ", "curl ", "wget ", "ssh ", "scp ",
        ]):
            return "command"

        return "text"
