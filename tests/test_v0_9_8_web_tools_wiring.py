"""v0.9.8 Phase 1 Task 7 — the three web vault-tools delegate to the keyless
web_access layer, gated on SYSTEMU_WEB_STACK_V2 (default on), and a new
find_places tool is registered.

No live HTTP: web_access.read_url/search_web/find_places are monkeypatched to
return canned dicts. Two flavours of test:
  * getsource guards — the run() bodies reference web_access + the env gate;
    web_read uses render=True.
  * behavioural — with the env unset (default on), each tool returns the mapped
    content; find_places surfaces the ODbL attribution.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from systemu.runtime import web_access
from systemu.vault.tools.implementations import (
    find_places as t_find_places,
    web_extract as t_web_extract,
    web_read as t_web_read,
    web_search as t_web_search,
)


@pytest.fixture(autouse=True)
def _default_on(monkeypatch):
    """Ensure the V2 stack is on by default (env unset → default 'true')."""
    monkeypatch.delenv("SYSTEMU_WEB_STACK_V2", raising=False)
    yield


# ── getsource guards ─────────────────────────────────────────────────────────

def test_web_extract_run_references_web_access_and_gate():
    src = inspect.getsource(t_web_extract.run)
    assert "web_access" in src
    assert "SYSTEMU_WEB_STACK_V2" in src


def test_web_read_run_references_web_access_gate_and_render_true():
    src = inspect.getsource(t_web_read.run)
    assert "web_access" in src
    assert "SYSTEMU_WEB_STACK_V2" in src
    # web_read must preserve the JS/anti-bot escalation via render=True.
    assert "render=True" in src


def test_web_search_run_references_web_access_and_gate():
    src = inspect.getsource(t_web_search.run)
    assert "web_access" in src
    assert "SYSTEMU_WEB_STACK_V2" in src


def test_find_places_run_references_web_access():
    src = inspect.getsource(t_find_places.run)
    assert "web_access" in src
    assert "find_places" in src


# ── behavioural: web_extract delegates the FETCH to read_url ──────────────────

def test_web_extract_delegates_to_read_url(monkeypatch):
    called = {}

    def fake_read_url(url, *, render=False, timeout=45, config=None):
        called["url"] = url
        return {"content": "<html><body>"
                           "<li><a href='/a'>Alpha Cafe</a> tasty spot here ok</li>"
                           "<li><a href='/b'>Bravo Bistro</a> another nice place ok</li>"
                           "</body></html>",
                "status": 200, "source": "jina", "url": url, "error": ""}

    monkeypatch.setattr(web_access, "read_url", fake_read_url)
    out = t_web_extract.run(url="https://example.com/places")
    assert called["url"] == "https://example.com/places"
    assert out["success"] is True
    # Heuristic mode (no schema/fields) still produces records from the mapped content.
    assert out["count"] >= 1
    names = [r.get("name") for r in out["records"]]
    assert "Alpha Cafe" in names


def test_web_extract_v2_blocked_surfaces_http_error(monkeypatch):
    def fake_read_url(url, *, render=False, timeout=45, config=None):
        return {"content": "", "status": None, "source": "none", "url": url,
                "error": "all backends failed"}

    monkeypatch.setattr(web_access, "read_url", fake_read_url)
    out = t_web_extract.run(url="https://blocked.example/x")
    assert out["success"] is False
    # status None → mapped to a 403 anti-bot shape by the adapter.
    assert out.get("error_type") in ("anti_bot_blocked", "http_error", "empty_or_blocked")


# ── behavioural: web_read delegates to read_url(render=True) ──────────────────

def test_web_read_delegates_to_read_url_render(monkeypatch):
    captured = {}

    def fake_read_url(url, *, render=False, timeout=45, config=None):
        captured["render"] = render
        captured["url"] = url
        return {"content": "<html><head><title>Hello</title></head>"
                           "<body><p>Readable body text here.</p></body></html>",
                "status": 200, "source": "browser", "url": url, "error": ""}

    monkeypatch.setattr(web_access, "read_url", fake_read_url)
    out = t_web_read.run(url="https://example.com/page")
    assert captured["render"] is True  # JS/anti-bot escalation preserved
    assert out["success"] is True
    assert "Readable body" in out["text"]
    assert out["tier_used"] == "browser"


def test_web_read_v2_failure_maps_error(monkeypatch):
    def fake_read_url(url, *, render=False, timeout=45, config=None):
        return {"content": "", "status": None, "source": "none", "url": url,
                "error": "all backends failed"}

    monkeypatch.setattr(web_access, "read_url", fake_read_url)
    out = t_web_read.run(url="https://blocked.example/x")
    assert out["success"] is False
    assert "all backends failed" in (out.get("error") or "")


# ── behavioural: web_search delegates to search_web ──────────────────────────

def test_web_search_delegates_to_search_web(monkeypatch):
    def fake_search_web(query, *, max_results=8, config=None):
        return {"results": [
                    {"title": "Top 10 Gyms", "url": "https://x.com/gyms", "snippet": ""},
                    {"title": "Cult.fit", "url": "https://cult.fit/chennai", "snippet": ""},
                ],
                "provider": "jina+ddg", "query": query, "error": ""}

    monkeypatch.setattr(web_access, "search_web", fake_search_web)
    out = t_web_search.run(query="best gyms in Chennai", max_results=5)
    assert out["success"] is True
    assert len(out["results"]) == 2
    assert out["results"][0]["url"] == "https://x.com/gyms"
    assert out["provider"] == "jina+ddg"
    assert out["degraded"] is True  # keyless free floor


def test_web_search_v2_no_results_has_note(monkeypatch):
    def fake_search_web(query, *, max_results=8, config=None):
        return {"results": [], "provider": "jina+ddg", "query": query,
                "error": "no results"}

    monkeypatch.setattr(web_access, "search_web", fake_search_web)
    out = t_web_search.run(query="zzz nothing here")
    assert out["success"] is False
    assert out["results"] == []
    assert "note" in out and "Do NOT retry" in out["note"]


# ── behavioural: find_places returns places + ODbL attribution ───────────────

def test_find_places_returns_places_and_attribution(monkeypatch):
    captured = {}

    def fake_find_places(query, *, near=None, lat=None, lon=None, limit=10, config=None):
        captured.update(query=query, near=near, lat=lat, lon=lon, limit=limit)
        return {"places": [
                    {"name": "G Force Gym", "opening_hours": "06:00-22:00",
                     "address": "12 Main St", "phone": None, "lat": 13.1, "lon": 80.2},
                ],
                "attribution": "© OpenStreetMap contributors (ODbL)",
                "center": {"lat": 13.0827, "lon": 80.2707},
                "query": query, "error": ""}

    monkeypatch.setattr(web_access, "find_places", fake_find_places)
    out = t_find_places.run(query="gym", near="Chennai", limit=5)
    assert captured["query"] == "gym" and captured["near"] == "Chennai"
    assert captured["limit"] == 5
    assert out["success"] is True
    assert out["places"][0]["name"] == "G Force Gym"
    # ODbL attribution must be surfaced.
    assert out["attribution"] == "© OpenStreetMap contributors (ODbL)"


def test_find_places_requires_query():
    out = t_find_places.run(query="")
    assert out["success"] is False
    assert "query is required" in out["error"]


# ── registration: find_places is present in the vault index ──────────────────

def test_find_places_registered_in_index():
    idx_path = Path(__file__).resolve().parent.parent / "systemu/vault/tools/index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    entry = next((e for e in idx if e["name"] == "find_places"), None)
    assert entry is not None, "find_places must be registered in tools/index.json"
    assert entry["enabled"] is True
    assert entry["status"] == "deployed"
    for p in ("query", "near", "limit"):
        assert p in entry["parameter_names"]


def test_find_places_record_disambiguated():
    rec_path = Path(__file__).resolve().parent.parent / \
        "systemu/vault/tools/tool_tool_find_places.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    desc = rec["description"].lower()
    assert "openstreetmap" in desc
    assert "not for general web search" in desc
    assert rec["enabled"] is True and rec["status"] == "deployed"
    schema = rec["parameters_schema"]
    for p in ("query", "near", "limit"):
        assert p in schema
