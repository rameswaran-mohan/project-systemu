#!/usr/bin/env python3
"""HTTP GET a JSON endpoint and return the parsed response."""
from __future__ import annotations

TOOL_META = {
    "name": "fetch_json",
    "tool_type": "web",
    "dependencies": ["requests"],
}


#: The public project page.  NEVER a source-forge URL -- a forge URL carries an
#: account name as a path segment.
PROJECT_URL = "https://pypi.org/project/systemu"


def _user_agent() -> str:
    """``systemu/<version> (+<project url>)``, derived rather than re-typed.

    This used to be a literal naming the 0.9 line, and it stayed there for the
    whole of the 0.10 line, so every third-party server the agent fetched from
    was told the wrong build was calling it.  (Written without spelling the old
    literal: ``test_no_outbound_identity_carries_a_version_literal`` reads this
    file line by line and a version-shaped string here is indistinguishable
    from the defect coming back.)

    THREE STEPS, in this order, because this file runs in the SANDBOX and is
    executed by path -- it cannot import the ``systemu.runtime.user_agent``
    helper the in-process sites use:

      1. ``systemu.__version__`` -- the source of truth.  ``pyproject.toml``
         derives the wheel's metadata from it, so the two cannot drift.
      2. ``importlib.metadata.version("systemu")`` -- only if the package
         itself is unreachable.  It reads a build artifact that goes stale the
         moment the literal changes without a re-install (measured: 0.10.25
         against a ``__version__`` of 0.10.30), so it is a fallback, not the
         answer.
      3. plain ``systemu`` -- a fetch that dies on its own User-Agent is a
         worse outcome than one that identifies the project without a version.
    """
    version = ""
    try:
        import systemu
        candidate = getattr(systemu, "__version__", None)
        if type(candidate) is str and candidate:
            version = candidate
    except Exception:
        pass
    if not version:
        try:
            from importlib.metadata import version as _dist_version
            candidate = _dist_version("systemu")
            if type(candidate) is str and candidate:
                version = candidate
        except Exception:
            pass
    if not version:
        return "systemu (+{})".format(PROJECT_URL)
    return "systemu/{} (+{})".format(version, PROJECT_URL)


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "")
    headers: dict = dict(kwargs.get("headers", {}) or {})
    params: dict = kwargs.get("params", {}) or {}

    if not url:
        return {"success": False, "data": None, "status_code": 0, "error": "url is required"}

    # v0.8.20: default a descriptive User-Agent + JSON Accept when the caller omits
    # them. The requests default ("python-requests/x") is rejected by several free
    # APIs the agent commonly reaches for — Nominatim returns 403 (per the OSM usage
    # policy) and Overpass returns 406 — which made free "nearby places" tasks fail.
    # Caller-supplied headers always win (setdefault).
    headers.setdefault("User-Agent", _user_agent())
    headers.setdefault("Accept", "application/json")

    try:
        import requests

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"success": True, "data": data, "status_code": response.status_code, "error": None}

    except Exception as exc:
        status_code = 0
        try:
            status_code = exc.response.status_code  # type: ignore[attr-defined]
        except Exception:
            pass
        return {"success": False, "data": None, "status_code": status_code, "error": str(exc)}
