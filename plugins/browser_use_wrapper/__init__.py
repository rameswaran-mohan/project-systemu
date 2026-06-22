"""Browser-Use as a Systemu tool plugin.

Wraps the browser-use library's primary API surface so it's invocable
as ordinary Systemu tools.  Operator opts in via:

    pip install systemu[browser-use]

# deps: browser-use, playwright

The deps comment above is read by the v0.7-d wizard's `scan_tool_deps`
helper so the operator's reqirements-tools.txt picks them up at install
time (idempotent against v0.6.8-d's allow-list seed).
"""
from __future__ import annotations
from typing import Any


def _run_browser_use(action: str, **kwargs) -> dict:
    """Inner adapter that actually invokes browser-use.

    Isolated so tests can mock without hitting the live library.  The
    real implementation imports browser-use locally (extras gate) and
    routes the named action through its Agent / Browser surface.
    """
    from browser_use import Browser, Agent  # local import — extras gate
    # Minimal sync wrapper around browser-use's async API.  Real
    # implementation runs the agent in an asyncio.run for sync tools.
    raise NotImplementedError(
        "wire up browser_use here — left as a sketch for the operator to "
        "complete against the live browser-use API surface they're using"
    )


def web_navigate(*, url: str, **kwargs) -> dict:
    """Navigate to a URL and return the page title + final URL."""
    try:
        result = _run_browser_use("navigate", url=url, **kwargs)
        return {
            "success": True,
            "title": result.get("title"),
            "url": result.get("url"),
            "output_path": "",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "output_path": ""}


def web_extract_text(*, url: str, selector: str = "body", **kwargs) -> dict:
    try:
        result = _run_browser_use("extract", url=url, selector=selector, **kwargs)
        return {
            "success": True,
            "text": result.get("text", ""),
            "output_path": "",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "output_path": ""}


def web_click(*, url: str, selector: str, **kwargs) -> dict:
    try:
        result = _run_browser_use("click", url=url, selector=selector, **kwargs)
        return {
            "success": True,
            "output_path": "",
            "after_url": result.get("url"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "output_path": ""}


def web_fill_form(*, url: str, fields: dict, submit_selector: str | None = None, **kwargs) -> dict:
    try:
        result = _run_browser_use(
            "fill_form", url=url, fields=fields,
            submit_selector=submit_selector, **kwargs,
        )
        return {
            "success": True,
            "output_path": "",
            "after_url": result.get("url"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "output_path": ""}


def register_tools(registry) -> None:
    """Called by the v0.7-f plugin loader at registry init."""
    for fn, name in [
        (web_navigate, "browser_use_wrapper.web_navigate"),
        (web_extract_text, "browser_use_wrapper.web_extract_text"),
        (web_click, "browser_use_wrapper.web_click"),
        (web_fill_form, "browser_use_wrapper.web_fill_form"),
    ]:
        registry.register({"name": name, "fn": fn})
