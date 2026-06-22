"""v0.8.20 — fetch_json defaults a descriptive User-Agent + JSON Accept.

Root cause (confirmed by live POC): the seed fetch_json sent requests' default
"python-requests/x" User-Agent, which Nominatim rejects with 403 (OSM usage
policy) and Overpass with 406 — breaking the free "nearby places" path the agent
relies on. The fix defaults a descriptive UA + Accept unless the caller supplies them.
"""
import importlib.util
import pathlib

import pytest


def _load_fetch_json():
    import systemu
    p = pathlib.Path(systemu.__file__).parent / "vault" / "tools" / "implementations" / "fetch_json.py"
    spec = importlib.util.spec_from_file_location("fetch_json_uut", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return {"ok": True}


class TestFetchJsonHeaders:
    def test_defaults_ua_and_accept_when_caller_omits(self, monkeypatch):
        import requests
        captured = {}
        def _fake_get(url, headers=None, params=None, timeout=None):
            captured["headers"] = headers or {}
            return _Resp()
        monkeypatch.setattr(requests, "get", _fake_get)

        out = _load_fetch_json().run(url="https://nominatim.openstreetmap.org/search")
        assert out["success"] is True
        ua = captured["headers"].get("User-Agent", "")
        assert ua and "python-requests" not in ua, f"expected a descriptive UA, got {ua!r}"
        assert captured["headers"].get("Accept") == "application/json"

    def test_caller_headers_win(self, monkeypatch):
        import requests
        captured = {}
        def _fake_get(url, headers=None, params=None, timeout=None):
            captured["headers"] = headers or {}
            return _Resp()
        monkeypatch.setattr(requests, "get", _fake_get)

        _load_fetch_json().run(url="https://x.example",
                               headers={"User-Agent": "my-ua", "Accept": "text/csv"})
        assert captured["headers"]["User-Agent"] == "my-ua"   # caller override preserved
        assert captured["headers"]["Accept"] == "text/csv"

    def test_url_required_guard_unchanged(self):
        out = _load_fetch_json().run(url="")
        assert out["success"] is False and "url is required" in out["error"]
