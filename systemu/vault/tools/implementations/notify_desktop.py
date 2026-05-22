#!/usr/bin/env python3
"""Show a desktop notification/toast message."""
from __future__ import annotations

TOOL_META = {
    "name": "notify_desktop",
    "tool_type": "system",
    "dependencies": ["plyer"],
}


def run(**kwargs) -> dict:
    title: str = kwargs.get("title", "")
    message: str = kwargs.get("message", "")
    timeout: int = int(kwargs.get("timeout", 5))

    try:
        from plyer import notification

        notification.notify(
            title=title,
            message=message,
            timeout=timeout,
        )
        return {"success": True, "error": None}

    except ImportError:
        print(f"[notify_desktop] {title}: {message}")
        return {"success": True, "error": None}

    except Exception as exc:
        return {"success": False, "error": str(exc)}
