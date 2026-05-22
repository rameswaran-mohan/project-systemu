"""Active window tracker — detects which application the user is interacting with.

Platform-specific implementations:
- Windows: ctypes (user32.dll) — no extra dependencies
- Linux:   xdotool / xprop via subprocess
- macOS:   osascript via subprocess
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore
from sharing_on.platform_info import OSType, PlatformInfo

logger = logging.getLogger(__name__)


class WindowCollector(BaseCollector):
    """Tracks the currently focused window / application.

    Emits an event whenever the user switches to a different window.
    """

    name = "window"

    def __init__(
        self,
        event_store: EventStore,
        platform: PlatformInfo,
        poll_interval: float = 1.0,
    ):
        super().__init__(event_store)
        self._platform = platform
        self._poll_interval = poll_interval
        self._last_window: Optional[str] = None
        self._last_app: Optional[str] = None

    def _collect_loop(self) -> None:
        while self._running:
            try:
                app_name, window_title = self._get_active_window()

                # Only emit when the window actually changes
                if window_title != self._last_window or app_name != self._last_app:
                    self.emit(CaptureEvent(
                        category=EventCategory.WINDOW,
                        action=EventAction.WINDOW_FOCUS,
                        timestamp=datetime.now(timezone.utc),
                        application=app_name,
                        window_title=window_title,
                        process_name=app_name,
                        data={
                            "previous_app": self._last_app,
                            "previous_title": self._last_window,
                        },
                    ))
                    self._last_window = window_title
                    self._last_app = app_name

            except Exception as e:
                logger.debug(f"Window tracking error: {e}")

            time.sleep(self._poll_interval)

    def _get_active_window(self) -> Tuple[str, str]:
        """Get the active window (app_name, window_title). Platform-dispatched."""
        if self._platform.is_windows:
            return self._get_active_window_windows()
        elif self._platform.is_linux:
            return self._get_active_window_linux()
        elif self._platform.is_macos:
            return self._get_active_window_macos()
        return ("Unknown", "Unknown")

    # --- Windows (ctypes, zero dependencies) ---

    def _get_active_window_windows(self) -> Tuple[str, str]:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Get foreground window handle
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ("", "")

        # Get window title
        length = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        window_title = buf.value

        # Get process name from window handle
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app_name = self._get_process_name_windows(pid.value)

        return (app_name, window_title)

    @staticmethod
    def _get_process_name_windows(pid: int) -> str:
        """Get process executable name from PID using ctypes."""
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return "Unknown"

        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_uint(260)
            # QueryFullProcessImageNameW
            success = kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            )
            if success:
                # Extract just the filename
                import os
                return os.path.basename(buf.value)
            return "Unknown"
        finally:
            kernel32.CloseHandle(handle)

    # --- Linux (xdotool / xprop) ---

    def _get_active_window_linux(self) -> Tuple[str, str]:
        try:
            # Get active window ID
            wid = subprocess.check_output(
                ["xdotool", "getactivewindow"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()

            # Get window name
            title = subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowname"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()

            # Get PID and process name
            pid_str = subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowpid"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()

            app_name = "Unknown"
            if pid_str:
                try:
                    import psutil
                    p = psutil.Process(int(pid_str))
                    app_name = p.name()
                except Exception:
                    pass

            return (app_name, title)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return ("Unknown", "Unknown")

    # --- macOS (osascript) ---

    def _get_active_window_macos(self) -> Tuple[str, str]:
        try:
            # Get frontmost application name
            app_script = (
                'tell application "System Events" to get name of '
                "first application process whose frontmost is true"
            )
            app_name = subprocess.check_output(
                ["osascript", "-e", app_script],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()

            # Get window title
            title_script = (
                f'tell application "System Events" to get name of '
                f"front window of application process \"{app_name}\""
            )
            try:
                window_title = subprocess.check_output(
                    ["osascript", "-e", title_script],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                ).decode().strip()
            except subprocess.CalledProcessError:
                window_title = app_name  # Some apps don't report window titles

            return (app_name, window_title)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return ("Unknown", "Unknown")
