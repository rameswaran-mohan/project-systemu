"""v0.9.8 Phase 1 — web_access keyless layer. All HTTP is monkeypatched; no live
calls. Tests the guardrails (rate-limiter, TTL cache, UA) + read_url/search_web/
find_places fallback + parsing + OSM attribution."""
import json
import time

from systemu.runtime import web_access as wa


# ── Task 1: rate-limiter + cache + UA ───────────────────────────────────────

def test_rate_limiter_spaces_calls():
    rl = wa._RateLimiter(per_sec=5)  # 0.2s min spacing
    t0 = time.time()
    rl.wait("host"); rl.wait("host"); rl.wait("host")
    assert time.time() - t0 >= 0.4 - 0.05  # two ~0.2s gaps


def test_rate_limiter_is_per_host():
    rl = wa._RateLimiter(per_sec=2)  # 0.5s spacing
    t0 = time.time()
    rl.wait("a"); rl.wait("b")  # different hosts → no wait between them
    assert time.time() - t0 < 0.4


def test_ttl_cache_roundtrip_and_expiry():
    c = wa._TTLCache(ttl=10)
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    c0 = wa._TTLCache(ttl=0)
    c0.set("k", 1)
    assert c0.get("k") is None  # expired immediately
    assert c.get("missing") is None


def test_user_agent_is_descriptive():
    assert wa.USER_AGENT and "systemu" in wa.USER_AGENT.lower()


# ── Task 2: read_url (Jina bypass + cache) ──────────────────────────────────

def _patch_get(monkeypatch, table, counter=None):
    """table: dict url-substring -> (status, body). counter: list to count calls."""
    def fake_get(url, timeout=30, headers=None):
        if counter is not None:
            counter.append(url)
        for frag, (st, body) in table.items():
            if frag in url:
                return st, body, ("" if st == 200 else "HTTP %s" % st)
        return 403, "", "HTTP 403"
    monkeypatch.setattr(wa, "_http_get", fake_get)
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)  # no sleeps in tests


def test_read_url_jina_bypasses_403_and_caches(monkeypatch):
    wa._CACHE._d.clear()
    calls = []
    _patch_get(monkeypatch, {
        "r.jina.ai": (200, "Title: X\nMarkdown Content: clean body here"),
        # raw moneycontrol would 403 (default branch), but Jina wins first
    }, counter=calls)
    r = wa.read_url("https://www.moneycontrol.com/news/")
    assert r["source"] == "jina" and "clean body" in r["content"]
    n1 = len(calls)
    r2 = wa.read_url("https://www.moneycontrol.com/news/")  # cached → no new fetch
    assert r2.get("cached") is True
    assert len(calls) == n1


def test_read_url_detects_jina_target_block_falls_to_raw(monkeypatch):
    wa._CACHE._d.clear()
    _patch_get(monkeypatch, {
        "r.jina.ai": (200, "Title: Access Denied\nWarning: Target URL returned error 403: Forbidden"),
        "justdial.com": (200, "<html>raw justdial body</html>"),
    })
    r = wa.read_url("https://www.justdial.com/Chennai/Gyms")
    assert r["source"] == "raw" and "raw justdial" in r["content"]


# ── Task 3: search_web (Jina-on-DDG parsing) ────────────────────────────────

def test_search_web_parses_jina_ddg(monkeypatch):
    wa._CACHE._d.clear()
    md = (
        "# best gyms in Chennai at DuckDuckGo\n"
        "[](https://duckduckgo.com/html/?q=best%20gyms)\n"
        "## [Top 10 Gyms in Chennai | Chennaitop10](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.chennaitop10.com%2Fgyms)\n"
        "Discover the top 10 gyms in Chennai...\n"
        "## [Cult.fit Gyms Chennai](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.cult.fit%2Fchennai)\n"
        "## [Best Gyms Chennai - magicpin](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fmagicpin.in%2Fchennai)\n"
    )
    _patch_get(monkeypatch, {"r.jina.ai": (200, md)})
    out = wa.search_web("best gyms in Chennai")
    urls = [r["url"] for r in out["results"]]
    assert len(out["results"]) >= 3
    assert any("chennaitop10.com" in u for u in urls)
    assert all(u.startswith("http") and "duckduckgo.com" not in u for u in urls)


def test_search_web_extracts_snippets_from_grouped_jina_links(monkeypatch):
    """Real DDG-via-Jina renders ~3 same-URL links per result: the title, the
    display URL, and the snippet paragraph. The parser must group by target URL
    and surface the prose snippet (the longest anchor), not drop it to ""."""
    wa._CACHE._d.clear()
    u1 = ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fhungryforever.net"
          "%2Fbest%2F&rut=abc")
    u2 = ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.crazymasalafood.com"
          "%2Ftop20%2F&rut=def")
    md = (
        f"[10 Best Places For Punjabi Food In Chennai]({u1})\n\n"
        f"[![Image 1](https://external-content.duckduckgo.com/ip3/x.ico)]({u1})"
        f"[hungryforever.net/best/]({u1})\n\n"
        f"[Here is our list of the best Punjabi places in Chennai including "
        f"Jamavar at The Leela Palace.]({u1})\n\n"
        f"[Top 20 Punjabi Restaurants In Chennai - Crazy Masala Food]({u2})\n\n"
        f"[Here is a list of the top 20 Punjabi cuisine restaurants in Chennai, "
        f"ranked by taste.]({u2})\n"
    )
    _patch_get(monkeypatch, {"r.jina.ai": (200, md)})
    out = wa.search_web("best punjabi restaurant chennai")
    assert len(out["results"]) == 2                      # one row per real URL
    h = next(r for r in out["results"] if "hungryforever.net" in r["url"])
    assert "10 Best Places" in h["title"]                # title is the first anchor
    assert "Jamavar" in h["snippet"] and len(h["snippet"]) > 20   # prose snippet kept
    assert "hungryforever.net/best" not in h["snippet"]  # not the bare display URL
    c = next(r for r in out["results"] if "crazymasalafood.com" in r["url"])
    assert "top 20" in c["snippet"].lower()


# ── Task 4: find_places (Overpass + retry + ODbL attribution) ───────────────

def test_find_places_overpass_named_pois_retry_and_attribution(monkeypatch):
    wa._CACHE._d.clear()
    monkeypatch.setattr(wa._RL, "wait", lambda host: None)
    hosts_hit = []
    overpass_json = json.dumps({"elements": [
        {"type": "node", "lat": 13.1, "lon": 80.2,
         "tags": {"name": "G Force Gym", "leisure": "fitness_centre", "opening_hours": "06:00-22:00"}},
        {"type": "node", "tags": {"name": "Muscle Factory", "leisure": "fitness_centre"}},
        {"type": "node", "tags": {"leisure": "fitness_centre"}},  # unnamed → skipped
    ]})

    def fake_post(url, data, timeout=40, headers=None):
        hosts_hit.append(url)
        if "overpass-api.de" in url:       # first host 504s → must retry
            return 504, "", "HTTP 504"
        return 200, overpass_json, ""
    monkeypatch.setattr(wa, "_http_post", fake_post)

    res = wa.find_places("gym", lat=13.0827, lon=80.2707)
    names = [p["name"] for p in res["places"]]
    assert "G Force Gym" in names and "Muscle Factory" in names
    assert len(res["places"]) == 2                       # unnamed dropped
    assert res["attribution"] == wa.OSM_ATTRIBUTION       # ODbL attribution present
    assert len(hosts_hit) == 2                            # retried the 2nd Overpass host
