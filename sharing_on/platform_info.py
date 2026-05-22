"""Platform detection — determine OS and available capabilities at runtime."""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class OSType(Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    UNKNOWN = "unknown"


@dataclass
class PlatformInfo:
    """Detected platform capabilities."""

    os_type: OSType
    os_version: str
    hostname: str
    architecture: str
    capabilities: List[str] = field(default_factory=list)

    @property
    def is_windows(self) -> bool:
        return self.os_type == OSType.WINDOWS

    @property
    def is_linux(self) -> bool:
        return self.os_type == OSType.LINUX

    @property
    def is_macos(self) -> bool:
        return self.os_type == OSType.MACOS

    def summary(self) -> str:
        return (
            f"{self.os_type.value} {self.os_version} "
            f"({self.architecture}) on {self.hostname}"
        )


def detect_platform() -> PlatformInfo:
    """Detect the current OS and available capabilities."""
    system = platform.system().lower()

    if system == "windows":
        os_type = OSType.WINDOWS
    elif system == "linux":
        os_type = OSType.LINUX
    elif system == "darwin":
        os_type = OSType.MACOS
    else:
        os_type = OSType.UNKNOWN

    info = PlatformInfo(
        os_type=os_type,
        os_version=platform.version(),
        hostname=platform.node(),
        architecture=platform.machine(),
    )

    # Detect available capabilities
    info.capabilities = _detect_capabilities(os_type)

    return info


def _detect_capabilities(os_type: OSType) -> List[str]:
    """Check which capture capabilities are available on this platform."""
    caps = []

    # Screenshots — mss is always available (pure Python)
    caps.append("screenshots")

    # Process monitoring — psutil is always available
    caps.append("process_monitor")

    # File watching — watchdog is always available
    caps.append("file_watcher")

    # Active window and UI Introspection — platform-specific checks
    if os_type == OSType.WINDOWS:
        # ctypes is always available on Windows Python
        caps.append("window_tracker")
        caps.append("clipboard")
        # uiautomation is the Windows introspector
        try:
            import uiautomation  # noqa: F401
            caps.append("ui_introspection")
        except ImportError:
            pass

    elif os_type == OSType.LINUX:
        if shutil.which("xdotool"):
            caps.append("window_tracker")
        elif shutil.which("xprop"):
            caps.append("window_tracker")
        if shutil.which("xclip") or shutil.which("xsel"):
            caps.append("clipboard")
        # Check if we're in a Wayland session
        import os
        if os.getenv("WAYLAND_DISPLAY"):
            caps.append("wayland_session")
        # pyatspi is the Linux introspector
        try:
            import pyatspi  # noqa: F401
            caps.append("ui_introspection")
        except ImportError:
            pass

    elif os_type == OSType.MACOS:
        # osascript is always available on macOS
        caps.append("window_tracker")
        caps.append("clipboard")  # pbpaste is always available
        # pyobjc ApplicationServices is the macOS introspector
        try:
            import ApplicationServices  # noqa: F401
            caps.append("ui_introspection")
        except ImportError:
            pass

    return caps

def check_dependencies() -> List[str]:
    """Return list of missing optional dependencies with install hints."""
    missing = []

    try:
        import mss  # noqa: F401
    except ImportError:
        missing.append("mss (pip install mss)")

    try:
        import psutil  # noqa: F401
    except ImportError:
        missing.append("psutil (pip install psutil)")

    try:
        import watchdog  # noqa: F401
    except ImportError:
        missing.append("watchdog (pip install watchdog)")

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("Pillow (pip install Pillow)")

    try:
        import openai  # noqa: F401
    except ImportError:
        missing.append("openai (pip install openai)")

    return missing
