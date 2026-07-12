"""R-A14a slice 4 — Step 1 micro-test: the github reference server emits a FLAT
``structuredContent`` over the REAL SDK stdio path.

This is the de-risking test for the riskiest external unknown of the G-DEMO v0
acceptance fixture: does the INSTALLED FastMCP actually surface ``html_url`` at
the top level of the call envelope for a ``create_issue`` dict return? If it
nests it (``{"result": {...}}``) or drops it to unstructured ``content``, the
whole receipt path (``_synthesize_directive`` → readback) silently degrades to
CLAIMED. We spawn the server exactly as ``test_v0936_mcp_client.py:96-103`` does
(``StdioServerParameters(command=sys.executable, args=[REF_SERVER])``), route
through the ConnectionManager, and assert ``html_url`` contains ``/issues/``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REF_SERVER = str(Path(__file__).resolve().parent / "_github_reference_server.py")


def _stdio_spec():
    return {"transport": "stdio", "command": sys.executable,
            "args": [REF_SERVER], "env": {}}


@pytest.mark.asyncio
async def test_create_issue_structured_content_exposes_html_url():
    """A create_issue dict return surfaces a FLAT structuredContent whose
    ``html_url`` carries ``/issues/`` — proving FastMCP emits structured output
    for the TypedDict return against the installed SDK version."""
    from systemu.runtime.mcp.sdk.manager import ConnectionManager
    mgr = ConnectionManager()
    try:
        out = await mgr.call_tool(
            "github", _stdio_spec(), "create_issue",
            {"repo": "octocat/hello", "title": "login button 500s"})
    finally:
        await mgr.disconnect_all()

    assert out.get("success") is True, (
        f"the create_issue stdio round-trip must succeed; got {out}")
    resp = out.get("response")
    assert isinstance(resp, dict), (
        f"FastMCP must emit a structured (dict) response, not unstructured "
        f"content; got {type(resp).__name__}: {resp}")
    # THE de-risking assertion: html_url is at the TOP level (flat structured
    # output), NOT nested under a "result" key. A bare `-> dict` return would
    # nest it and this would fail — forcing the TypedDict annotation fix.
    html_url = resp.get("html_url")
    assert isinstance(html_url, str) and "/issues/" in html_url, (
        "the structured envelope must carry a top-level html_url containing "
        f"'/issues/' (the signal _synthesize_directive reads); got response={resp}")
    # the id/number tokens the readback will match are present + consistent.
    assert str(resp.get("number")) in html_url
    assert resp.get("state") == "open"


def test_reference_server_create_issue_returns_dict():
    """The tool function itself returns a plain dict (a TypedDict IS a dict) with
    an https public-IP-literal html_url — the SSRF-safe, DNS-free readback host."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_gh_ref", REF_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.create_issue("octocat/hello", "bug", body="")
    assert isinstance(out, dict)
    assert out["html_url"].startswith("https://93.184.216.34/repos/")
    assert "/issues/" in out["html_url"]
    # id is a DISTINCTIVE large opaque int (issue_id_for(number)), NOT the small
    # human number — this is what makes the readback token match load-bearing (a
    # small numeric token would substring-match the IP host by coincidence).
    assert out["id"] == mod.issue_id_for(out["number"])
    assert out["id"] != out["number"] and out["id"] > 4_000_000_000
