"""v0.9.7 Phase 0 — bundled tool descriptions must disambiguate IP-geolocation.

Regression guard for the tool-selection regression: the agent must be steered to
fetch_json (IP-geolocation via an API) rather than web_search for "find my
location" tasks. This is encoded in the tool descriptions the LLM sees.
"""
import json
from pathlib import Path


def _load(name):
    p = Path("systemu/vault/tools") / name
    return json.loads(p.read_text(encoding="utf-8"))


def test_fetch_json_description_steers_ip_geolocation():
    rec = _load("tool_tool_a1f69543.json")
    desc = rec["description"].lower()
    assert "ip" in desc and ("geolocat" in desc or "ip-api" in desc or "location" in desc)
    assert "web_search" in desc, "must disambiguate against web_search"


def test_web_search_description_warns_against_ip_lookups():
    rec = _load("tool_tool_web_search.json")
    desc = rec["description"].lower()
    assert "fetch_json" in desc and ("ip" in desc and "location" in desc)
    assert "do not" in desc or "not for" in desc


def test_index_matches_records_for_disambiguation():
    index = {t["name"]: t for t in _load("index.json")}
    assert "fetch_json" in index and "web_search" in index
    assert "ip-api" in index["fetch_json"]["description"].lower() or "geolocat" in index["fetch_json"]["description"].lower()
    assert "fetch_json" in index["web_search"]["description"].lower()
