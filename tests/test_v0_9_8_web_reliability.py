"""v0.9.8 — keyless web layer reliability hardening (B3a/B3b/B3c).

Covers the failure mode that killed the live burrito task: free Overpass mirrors
returning 504 Gateway Timeout / read-timeouts, and a transient Nominatim timeout.
All HTTP is monkeypatched; ``time.sleep`` is a no-op so tests are fast. Everything
is best-effort — the functions must degrade to their normal "error" return shapes,
never raise.
"""
import json

from systemu.runtime import web_access as wa


# ── B3a: more Overpass mirrors ──────────────────────────────────────────────

def test_overpass_host_list_has_at_least_four_mirrors():
    assert len(wa._OVERPASS_HOSTS) >= 4
    # Original two kept FIRST, in order.
    assert wa._OVERPASS_HOSTS[0] == "https://overpass-api.de/api/interpreter"
    assert wa._OVERPASS_HOSTS[1] == "https://overpass.kumi.systems/api/interpreter"
    # All entries are real http(s) interpreter endpoints, no duplicates.
    assert all(h.startswith("https://") and h.endswith("/interpreter") for h in wa._OVERPASS_HOSTS)
    assert len(set(wa._OVERPASS_HOSTS)) == len(wa._OVERPASS_HOSTS)


# ── B3b: find_places retries the host list a second time after a 504 pass ────

def test_find_places_retries_second_pass_after_all_hosts_504(monkeypatch):
    """First full pass: every host 504s (transient overload). After a backoff,
    the second pass succeeds and returns the POI. Asserts find_places retried."""
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)   # no rate-limit sleeps
    sleeps = []
    monkeypatch.setattr(wa.time, "sleep", lambda s: sleeps.append(s))  # backoff = no-op

    n_hosts = len(wa._OVERPASS_HOSTS)
    calls = {"n": 0}
    overpass_json = json.dumps({"elements": [
        {"type": "node", "lat": 13.1, "lon": 80.2,
         "tags": {"name": "El Burrito Loco", "amenity": "restaurant",
                  "opening_hours": "11:00-23:00"}},
    ]})

    def fake_post(url, data, timeout=40, headers=None):
        calls["n"] += 1
        # Whole first pass (one call per host) returns 504; everything after 200.
        if calls["n"] <= n_hosts:
            return 504, "", "HTTP 504"
        return 200, overpass_json, ""
    monkeypatch.setattr(wa, "_http_post", fake_post)

    res = wa.find_places("burrito", lat=13.0827, lon=80.2707)

    names = [p["name"] for p in res["places"]]
    assert "El Burrito Loco" in names                 # POI returned on retry
    assert res["error"] == ""
    assert res["attribution"] == wa.OSM_ATTRIBUTION    # ODbL attribution preserved
    # Made a second pass: hit at least the full first pass + one more call.
    assert calls["n"] >= n_hosts + 1
    assert len(sleeps) == 1                             # exactly one backoff between passes


def test_find_places_timeout_first_pass_then_success(monkeypatch):
    """Read-timeout (status None) on the first host of pass 1 is treated as
    retryable; a later attempt returns a named POI."""
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    n_hosts = len(wa._OVERPASS_HOSTS)
    calls = {"n": 0}
    overpass_json = json.dumps({"elements": [
        {"type": "node", "lat": 1.0, "lon": 2.0,
         "tags": {"name": "Taqueria Uno", "amenity": "restaurant"}},
    ]})

    def fake_post(url, data, timeout=40, headers=None):
        calls["n"] += 1
        if calls["n"] <= n_hosts:           # entire first pass times out
            return None, "", "TimeoutError"
        return 200, overpass_json, ""
    monkeypatch.setattr(wa, "_http_post", fake_post)

    res = wa.find_places("burrito", lat=1.0, lon=2.0)
    assert [p["name"] for p in res["places"]] == ["Taqueria Uno"]
    assert res["error"] == ""


def test_find_places_caps_at_two_passes(monkeypatch):
    """If every host fails on BOTH passes, find_places does not loop forever:
    at most 2 passes total, then degrades to the 'no places found' return."""
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    n_hosts = len(wa._OVERPASS_HOSTS)
    calls = {"n": 0}

    def fake_post(url, data, timeout=40, headers=None):
        calls["n"] += 1
        return 504, "", "HTTP 504"
    monkeypatch.setattr(wa, "_http_post", fake_post)

    res = wa.find_places("burrito", lat=1.0, lon=2.0)
    assert res["places"] == []
    assert res["error"]                                 # non-empty error string
    assert res["attribution"] == wa.OSM_ATTRIBUTION
    assert calls["n"] == n_hosts * 2                    # exactly 2 passes, no more


# ── B3c: geocode retries once on a transient failure ────────────────────────

def test_geocode_retries_once_after_timeout(monkeypatch):
    """Nominatim times out once (status None), then returns a coordinate.
    geocode must retry and return the (lat, lon)."""
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    sleeps = []
    monkeypatch.setattr(wa.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_get(url, timeout=30, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "", "TimeoutError"            # transient timeout
        return 200, json.dumps([{"lat": "13.0827", "lon": "80.2707"}]), ""
    monkeypatch.setattr(wa, "_http_get", fake_get)

    coord = wa.geocode("Chennai")
    assert coord == (13.0827, 80.2707)
    assert calls["n"] == 2                              # original + one retry
    assert len(sleeps) == 1                             # one backoff between attempts


def test_geocode_retries_on_5xx_then_succeeds(monkeypatch):
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def fake_get(url, timeout=30, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return 504, "", "HTTP 504"
        return 200, json.dumps([{"lat": "1.5", "lon": "2.5"}]), ""
    monkeypatch.setattr(wa, "_http_get", fake_get)

    assert wa.geocode("Somewhere") == (1.5, 2.5)
    assert calls["n"] == 2


def test_geocode_does_not_retry_on_clean_empty_match(monkeypatch):
    """A clean 200 with valid-but-empty JSON is a real 'no match', not transient
    — geocode should NOT retry and should return None after a single call."""
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def fake_get(url, timeout=30, headers=None):
        calls["n"] += 1
        return 200, "[]", ""
    monkeypatch.setattr(wa, "_http_get", fake_get)

    assert wa.geocode("Nowheresville XYZ") is None
    assert calls["n"] == 1                              # no wasted retry


def test_geocode_gives_up_after_two_attempts(monkeypatch):
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def fake_get(url, timeout=30, headers=None):
        calls["n"] += 1
        return None, "", "TimeoutError"
    monkeypatch.setattr(wa, "_http_get", fake_get)

    assert wa.geocode("Chennai") is None
    assert calls["n"] == 2                              # original + one retry, then stop
