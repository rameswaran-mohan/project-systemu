"""Keyless, layered web access for systemu (v0.9.8).

Capabilities
------------
- ``read_url(url, render=False)``  — Jina Reader (r.jina.ai) → raw GET → (optional)
  Chromium-stealth render. Beats most anti-bot 403s without a key.
- ``search_web(query)``           — Jina-on-DuckDuckGo (render DDG's results page via
  the reader) → raw DDG-lite fallback. Returns parsed {title,url,snippet}.
- ``find_places(query, near=...)``— OSM Nominatim geocode + Overpass POIs (named local
  businesses), ODbL-attributed.

Keyless by default with guardrails: a per-host rate-limiter (honors Jina ~20 RPM and
Nominatim 1 req/s), a TTL cache (cuts call volume — also required by Nominatim policy),
a descriptive User-Agent, OSM attribution, and a Chromium concurrency cap. An optional
key/self-host path (Brave/Tavily/SearXNG) is config-gated and OFF by default.

Phase-0 verified (2026-06-09): raw GET 403s where Jina Reader returns clean text;
Jina-on-DDG returns real keyless results; Overpass returns named local POIs.
"""
from __future__ import annotations

import http.client as _httplib
import json
import logging
import re
import socket as _socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

USER_AGENT = "systemu/0.9 (+https://pypi.org/project/systemu)"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
_CTX = ssl.create_default_context()
_OVERPASS_HOSTS = [
    # Keep the original two FIRST (verified primary mirrors), then additional
    # reputable public Overpass instances so one overloaded host (504/timeout)
    # doesn't doom the whole call.
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# HTTP statuses worth retrying (transient overload / gateway / throttling).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Short backoff between full retry passes (overridable; monkeypatched in tests).
_RETRY_BACKOFF_S = 1.5


# ── Guardrails: rate-limiter + TTL cache ────────────────────────────────────
class _RateLimiter:
    """Per-host minimum-spacing limiter. ``per_sec`` is the default rate; specific
    hosts can be slower via ``host_overrides`` (e.g. Jina keyless ~20/min)."""

    def __init__(self, per_sec: float = 2.0, host_overrides: Optional[Dict[str, float]] = None):
        self._default_gap = 1.0 / max(per_sec, 0.001)
        self._gaps = {h: 1.0 / max(ps, 0.001) for h, ps in (host_overrides or {}).items()}
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        need = self._gaps.get(host, self._default_gap)
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last.get(host, 0.0)
            if elapsed < need:
                time.sleep(need - elapsed)
            self._last[host] = time.monotonic()


class _TTLCache:
    def __init__(self, ttl: float = 900.0):
        self._ttl = float(ttl)
        self._d: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            v = self._d.get(key)
            if not v:
                return None
            ts, val = v
            if self._ttl <= 0 or (time.monotonic() - ts) > self._ttl:
                self._d.pop(key, None)
                return None
            return val

    def set(self, key: str, val: Any) -> None:
        with self._lock:
            self._d[key] = (time.monotonic(), val)


# Module singletons (per-host limits honor the verified service limits).
_RL = _RateLimiter(per_sec=1.0, host_overrides={
    "r.jina.ai": 0.3,                       # Jina keyless ~20 RPM → ~3.3s gap
    "nominatim.openstreetmap.org": 1.0,     # Nominatim hard 1 req/s
})
_CACHE = _TTLCache(ttl=900.0)
_BROWSER_SEM = threading.Semaphore(2)       # cap concurrent Chromium instances


# ── R-A11 SSRF gate (the canonical fail-closed check; closes the live hole) ──
from systemu.runtime import net_safety


def _allowed_outbound_hosts() -> "set[str]":
    """Operator escape hatch (delegates to the canonical net_safety source)."""
    return net_safety.allowed_outbound_hosts()


#: the refusal tuple — the SAME error shape callers already handle (status None,
#: no body, an error string). NEVER a partial fetch.
_SSRF_REFUSAL: "Tuple[Optional[int], str, str]" = (
    None, "", "blocked: destination is not an allowed public address (SSRF guard)")


def _ssrf_admissible(url: str) -> bool:
    """True iff the URL may be fetched. Fail-CLOSED: any gate error refuses (a
    broken security control must never open the egress hole)."""
    try:
        return net_safety.url_is_admissible(url, allowed_hosts=_allowed_outbound_hosts())
    except Exception:  # noqa: BLE001
        return False


_BLOCKED_REDIRECT_REASON = "blocked redirect to a non-public address (SSRF guard)"


class _SSRFGuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF gate on EVERY 30x redirect target. Without this, a public URL
    that 302-redirects to an internal / metadata host would be followed unguarded —
    the classic resolve-then-reject SSRF bypass (the gate only saw the ORIGINAL url)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _ssrf_admissible(newurl):
            raise urllib.error.HTTPError(newurl, code, _BLOCKED_REDIRECT_REASON, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ── the socket-pin: dial the VETTED IP, closing the resolve-then-reconnect TOCTOU ─
# ``url_is_admissible`` (and the redirect re-check) VET the host, but urllib would
# then resolve it AGAIN at connect time — a rebinding DNS answer could swap in an
# internal IP in that narrow window. We PIN: resolve ONCE to a vetted-public IP
# literal and dial THAT, so the kernel connects to the exact address the gate
# approved (a literal never re-resolves). TLS SNI + the Host header still use the
# hostname — ``self.host`` is untouched, only the socket creator is swapped — so cert
# validation is unchanged. This brings web_access up to the money-move readback path's
# posture (which already pins via ``net_safety.resolve_pinned_ip``).
_PIN_REFUSAL = "SSRF guard: host %r did not resolve to a public address"


class _PinBlocked(OSError):
    """Raised by the socket-pin when a host resolves to a non-public address at
    CONNECT (the exact rebind TOCTOU the pin closes). A distinct type so ``_http_get``
    can map it back to the canonical ``blocked: … (SSRF guard)`` refusal tuple."""


def _proxy_hosts() -> "set[str]":
    """Hosts of any configured HTTP(S) proxy. urllib routes the connection to the
    PROXY (``self.host`` becomes the proxy), so the proxy hop must dial as-is: it is
    operator-configured infra (like the allowlist) and MAY be internal — a corporate
    10.x or a localhost debug proxy. The real TARGET host is still SSRF-pre-checked
    upstream (``url_is_admissible`` on the original URL) and the proxy — not our
    kernel — resolves the target, so pinning the proxy adds no protection, only
    breakage. Never raises (a parse quirk must not disable egress).

    Residual (LOW, operator-scoped, reviewed): the exemption is scheme-blind, so a
    DIRECT fetch of ``https://<proxy-hostname>/`` when only an HTTP proxy is set would
    dial that one host as-is (re-resolving) rather than pinned. NOT attacker-injectable
    (only operator/system-configured hosts land here) and the precondition — attacker
    DNS control over the operator's own proxy hostname — already MITMs all proxied
    traffic, so incremental risk is negligible. Documented rather than closed: a
    literal-IP-only exemption would break legitimate hostname proxies (the very
    regression this exemption exists to fix)."""
    hosts: "set[str]" = set()
    try:
        for key, spec in urllib.request.getproxies().items():
            if key == "no":                       # no_proxy list, not a proxy endpoint
                continue
            h = urllib.parse.urlsplit(spec if "://" in spec else "//" + spec).hostname
            if h:
                hosts.add(h.lower())
    except Exception:  # noqa: BLE001 — never let proxy parsing break egress
        pass
    return hosts


def _pinned_connect(address, *args, **kwargs):
    """Drop-in for ``HTTPConnection._create_connection`` that dials the VETTED IP
    literal instead of re-resolving the hostname (closes the rebind TOCTOU). An
    operator-allowlisted host OR a configured proxy dials as-is (either may be
    internal by design — the pin must not re-vet it, since ``resolve_pinned_ip``
    fail-closes on private); any other host that does not resolve entirely-public
    RAISES ``_PinBlocked`` — it never dials."""
    host, port = address[0], address[1]
    h = str(host).lower()
    if h in _allowed_outbound_hosts() or h in _proxy_hosts():
        return _socket.create_connection((host, port), *args, **kwargs)
    pinned = net_safety.resolve_pinned_ip(host)
    if pinned is None:
        raise _PinBlocked(_PIN_REFUSAL % host)
    return _socket.create_connection((pinned, port), *args, **kwargs)


class _PinnedHTTPConnection(_httplib.HTTPConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = _pinned_connect


class _PinnedHTTPSConnection(_httplib.HTTPSConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = _pinned_connect


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # mirror stdlib https_open (3.12: passes only context), swapping the pinned class.
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


#: a guarded opener: redirect re-check + pinned http/https connections (dial the
#: vetted IP) + the module SSL context. Passing pinned HTTP(S)Handler subclasses
#: suppresses build_opener's default handlers, so EVERY hop goes through the pin.
#: Callers go through ``_urlopen`` so redirects are re-gated; tests patch ``_urlopen``.
_SSRF_OPENER = urllib.request.build_opener(
    _SSRFGuardedRedirectHandler(),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(context=_CTX),
)


def _urlopen(req, *, timeout: int):
    """The single egress call — a guarded opener that re-checks each redirect hop."""
    return _SSRF_OPENER.open(req, timeout=timeout)


# ── HTTP seams (monkeypatched in tests) ─────────────────────────────────────
def _http_get(url: str, timeout: int = 30, headers: Optional[dict] = None) -> Tuple[Optional[int], str, str]:
    if not _ssrf_admissible(url):
        return _SSRF_REFUSAL
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        if getattr(e, "reason", None) == _BLOCKED_REDIRECT_REASON:
            return _SSRF_REFUSAL          # a redirect to a non-public host was refused
        return e.code, "", "HTTP %s" % e.code
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), _PinBlocked):
            return _SSRF_REFUSAL          # a rebind caught at connect (the socket-pin)
        return None, "", repr(e)[:120]
    except Exception as e:  # noqa: BLE001
        return None, "", repr(e)[:120]


def _http_post(url: str, data: bytes, timeout: int = 40, headers: Optional[dict] = None) -> Tuple[Optional[int], str, str]:
    if not _ssrf_admissible(url):
        return _SSRF_REFUSAL
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": USER_AGENT})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        if getattr(e, "reason", None) == _BLOCKED_REDIRECT_REASON:
            return _SSRF_REFUSAL
        return e.code, "", "HTTP %s" % e.code
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), _PinBlocked):
            return _SSRF_REFUSAL          # a rebind caught at connect (the socket-pin)
        return None, "", repr(e)[:120]
    except Exception as e:  # noqa: BLE001
        return None, "", repr(e)[:120]


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc or "?"
    except Exception:
        return "?"


def _sleep_backoff(seconds: float) -> None:
    """Best-effort backoff sleep (own seam so tests can monkeypatch time.sleep)."""
    try:
        time.sleep(max(0.0, float(seconds)))
    except Exception:  # noqa: BLE001 — never let a sleep failure propagate
        pass


def _cfg(config: Any, attr: str, default: Any) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(attr, default)
    return getattr(config, attr, default)


# ── read_url ────────────────────────────────────────────────────────────────
def read_url(url: str, *, render: bool = False, timeout: int = 45, config: Any = None) -> Dict[str, Any]:
    """Fetch a URL's main content. Jina Reader first (beats most 403s), raw GET
    fallback, optional Chromium-stealth render for JS/hard-anti-bot pages."""
    if not url:
        return {"content": "", "status": None, "source": "none", "url": url, "error": "empty url"}
    ck = "read:" + url
    cached = _CACHE.get(ck)
    if cached is not None:
        return {**cached, "cached": True}

    backend = str(_cfg(config, "web_reader_backend", "auto")).lower()
    last_err = ""

    if backend in ("auto", "jina"):
        _RL.wait("r.jina.ai")
        st, body, err = _http_get("https://r.jina.ai/" + url, timeout=timeout)
        # Jina returns 200 even when the *target* was blocked; detect that.
        if st == 200 and body and "Target URL returned error" not in body[:400]:
            res = {"content": body, "status": 200, "source": "jina", "url": url, "error": ""}
            _CACHE.set(ck, res)
            return res
        last_err = err or "jina blocked/empty"

    if backend in ("auto", "raw"):
        _RL.wait(_host(url))
        st, body, err = _http_get(url, timeout=min(timeout, 30))
        if st == 200 and body:
            res = {"content": body, "status": 200, "source": "raw", "url": url, "error": ""}
            _CACHE.set(ck, res)
            return res
        last_err = err or last_err or ("HTTP %s" % st)

    if render:
        html = _browser_render(url)
        if html:
            res = {"content": html, "status": 200, "source": "browser", "url": url, "error": ""}
            _CACHE.set(ck, res)
            return res

    return {"content": "", "status": None, "source": "none", "url": url, "error": last_err or "all backends failed"}


def _browser_render(url: str) -> Optional[str]:
    """Last-resort Chromium render via the existing BrowserPool, capped + fail-safe."""
    # R-A11: the render path reaches the network through a local Chromium, NOT through
    # _http_get — so it must be SSRF-gated in its own right, else render=True would
    # bypass the guard and reach IMDS / loopback / internal hosts.
    if not _ssrf_admissible(url):
        logger.info("[web_access] browser render blocked — SSRF guard (%s)", _host(url))
        return None
    if not _BROWSER_SEM.acquire(blocking=False):
        logger.info("[web_access] browser render skipped — concurrency cap reached")
        return None
    try:
        from systemu.runtime.browser_pool import BrowserPool  # type: ignore
        return BrowserPool.get().render_html(url)
    except Exception:
        logger.debug("[web_access] browser render unavailable", exc_info=True)
        return None
    finally:
        _BROWSER_SEM.release()


# ── search_web ──────────────────────────────────────────────────────────────
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _decode_ddg_url(url: str) -> str:
    """DDG result links are redirects: …/l/?uddg=<encoded-real-url>."""
    if "duckduckgo.com/l/" in url and "uddg=" in url:
        try:
            qs = urllib.parse.urlsplit(url).query
            uddg = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
            if uddg:
                return urllib.parse.unquote(uddg)
        except Exception:
            pass
    return url


def _parse_ddg_results(markdown_or_html: str, max_results: int) -> List[Dict[str, str]]:
    """Parse Jina-on-DDG markdown into {title, url, snippet}.

    Jina renders ~3 links per result, ALL pointing at the same uddg redirect:
    the title, the display URL, and the snippet paragraph. Group by the decoded
    target (first-seen order) and surface the longest PROSE anchor as the
    snippet — the bare display URL (no spaces) and the title are excluded.
    Lite/older renderings with a single link per result just get snippet="".
    """
    groups: Dict[str, Dict[str, Any]] = {}
    order = 0
    for m in _MD_LINK.finditer(markdown_or_html or ""):
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip().strip("*").strip()
        real = _decode_ddg_url(m.group(2))
        if not real.startswith("http") or "duckduckgo.com" in _host(real):
            continue                          # skip DDG nav / favicon / self links
        if not text or len(text) < 2:
            continue
        g = groups.get(real)
        if g is None:
            groups[real] = {"title": text, "texts": [text], "order": order}
            order += 1
        else:
            g["texts"].append(text)

    out: List[Dict[str, str]] = []
    for real, g in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        title = g["title"]
        # snippet = the longest prose anchor (has a space, >=20 chars, not the
        # title) — excludes the no-space display URL and the title itself.
        prose = [t for t in g["texts"] if t != title and " " in t and len(t) >= 20]
        snippet = max(prose, key=len) if prose else ""
        out.append({"title": title[:200], "url": real, "snippet": snippet[:500]})
        if len(out) >= max_results:
            break
    return out


def search_web(query: str, *, max_results: int = 8, config: Any = None) -> Dict[str, Any]:
    """Keyless web search via Jina-on-DuckDuckGo; raw DDG-lite fallback."""
    if not query:
        return {"results": [], "provider": "none", "query": query, "error": "empty query"}
    ck = "search:%d:%s" % (max_results, query)
    cached = _CACHE.get(ck)
    if cached is not None:
        return {**cached, "cached": True}

    ddg = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    _RL.wait("r.jina.ai")
    st, body, err = _http_get("https://r.jina.ai/" + ddg, timeout=45)
    results = _parse_ddg_results(body, max_results) if (st == 200 and body) else []
    provider = "jina+ddg"

    if not results:  # fallback: raw DDG-lite
        _RL.wait("lite.duckduckgo.com")
        st2, body2, err2 = _http_get(
            "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query), timeout=20)
        results = _parse_ddg_results(body2, max_results) if body2 else []
        provider = "ddg-lite" if results else provider
        err = err or err2

    res = {"results": results, "provider": provider, "query": query,
           "error": "" if results else (err or "no results")}
    _CACHE.set(ck, res)
    return res


# ── find_places (OSM Nominatim + Overpass, ODbL) ────────────────────────────
_TAG_MAP = {
    "gym": '["leisure"="fitness_centre"]', "fitness": '["leisure"="fitness_centre"]',
    "burrito": '["amenity"="restaurant"]', "restaurant": '["amenity"="restaurant"]',
    "coffee": '["amenity"="cafe"]', "cafe": '["amenity"="cafe"]', "café": '["amenity"="cafe"]',
    "pharmacy": '["amenity"="pharmacy"]', "chemist": '["amenity"="pharmacy"]',
    "hospital": '["amenity"="hospital"]', "clinic": '["amenity"="clinic"]',
    "dentist": '["amenity"="dentist"]', "atm": '["amenity"="atm"]', "bank": '["amenity"="bank"]',
    "fuel": '["amenity"="fuel"]', "gas": '["amenity"="fuel"]', "petrol": '["amenity"="fuel"]',
    "supermarket": '["shop"="supermarket"]', "grocery": '["shop"="supermarket"]',
    "hotel": '["tourism"="hotel"]', "school": '["amenity"="school"]', "bar": '["amenity"="bar"]',
}


def _osm_tag_for(query: str) -> Optional[str]:
    q = (query or "").lower()
    for k, v in _TAG_MAP.items():
        if k in q:
            return v
    return None


def _osm_addr(tags: Dict[str, Any]) -> str:
    parts = [tags.get("addr:housenumber"), tags.get("addr:street"),
             tags.get("addr:suburb"), tags.get("addr:city")]
    return ", ".join(p for p in parts if p)


# Cap the live Nominatim lookups for a many-comma string (full + coarser fallbacks).
_GEOCODE_MAX_CANDIDATES = 4


def _geocode_candidates(place: str) -> List[str]:
    """Coarsening candidates for a free-text place: the full string first, then
    drop the most-specific (leading) comma segment each step so a finicky
    'neighborhood, city' that Nominatim can't match as one string degrades to
    the city. 'Santhoshpuram, Chennai' -> ['Santhoshpuram, Chennai', 'Chennai'].
    Comma-free input yields exactly one candidate (behavior unchanged)."""
    full = place.strip()
    cands: List[str] = [full] if full else []
    parts = [p.strip() for p in place.split(",") if p.strip()]
    for i in range(1, len(parts)):                 # drop leading (most specific) segments
        cand = ", ".join(parts[i:])
        if cand and cand not in cands:
            cands.append(cand)
    return cands[:_GEOCODE_MAX_CANDIDATES]


def _geocode_one(place: str) -> Optional[Tuple[float, float]]:
    """One Nominatim free-text lookup. Retries ONCE with a short backoff on a
    transient timeout / 5xx / empty body, then returns None. A clean 200 with
    valid JSON but no match is NOT retryable. Honors the 1 req/s rate limit."""
    url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(place)
    # Up to 2 attempts total (1 original + 1 retry on transient failure).
    for attempt in range(2):
        try:
            _RL.wait("nominatim.openstreetmap.org")
            st, body, _ = _http_get(url, timeout=20)
            if st == 200 and body:
                try:
                    arr = json.loads(body)
                    if arr:
                        return float(arr[0]["lat"]), float(arr[0]["lon"])
                except Exception:
                    pass
            # Retry only on transient conditions: timeout (st is None), 5xx/429,
            # or empty body on an otherwise-OK response. A clean 200 with valid
            # JSON but no match is NOT retryable (returns None below).
            retryable = (st is None) or (st in _RETRYABLE_STATUS) or (st == 200 and not body)
            if attempt == 0 and retryable:
                _sleep_backoff(_RETRY_BACKOFF_S)
                continue
            break
        except Exception:  # noqa: BLE001 — never propagate; degrade to None
            logger.debug("[web_access] geocode attempt failed", exc_info=True)
            break
    return None


def geocode(place: str, *, config: Any = None) -> Optional[Tuple[float, float]]:
    """Resolve a place name to (lat, lon) via Nominatim. Best-effort: tries the
    full string, then progressively coarser comma-trimmed fallbacks (so a
    'neighborhood, city' string Nominatim can't match as one degrades to the
    city), each with a single transient retry. Degrades to None. Honors the
    Nominatim 1 req/s rate limit on every attempt."""
    if not place:
        return None
    for cand in _geocode_candidates(place):
        coord = _geocode_one(cand)
        if coord:
            return coord
    return None


def find_places(query: str, *, near: Optional[str] = None, lat: Optional[float] = None,
                lon: Optional[float] = None, limit: int = 10, radius_m: int = 9000,
                config: Any = None) -> Dict[str, Any]:
    """Structured local business/POI lookup near a location (OSM, ODbL-attributed)."""
    ck = "places:%s:%s:%s:%s:%s" % (query, near, lat, lon, radius_m)
    cached = _CACHE.get(ck)
    if cached is not None:
        return {**cached, "cached": True}

    if (lat is None or lon is None) and near:
        geo = geocode(near, config=config)
        if geo:
            lat, lon = geo
    if lat is None or lon is None:
        return {"places": [], "error": "could not resolve location", "query": query,
                "attribution": OSM_ATTRIBUTION}

    tag = _osm_tag_for(query)
    if tag:
        sel = ("node%s(around:%d,%s,%s);way%s(around:%d,%s,%s);"
               % (tag, radius_m, lat, lon, tag, radius_m, lat, lon))
    else:
        safe = re.escape(query.split()[0]) if query else ""
        sel = 'node["name"~"%s",i](around:%d,%s,%s);' % (safe, radius_m, lat, lon)
    oq = "[out:json][timeout:25];(%s);out tags %d;" % (sel, max(limit * 2, 20))
    data = ("data=" + urllib.parse.quote(oq)).encode()

    places: List[Dict[str, Any]] = []
    err = ""
    # Up to 2 full passes over the host list. A single transient overload (504/
    # timeout) on every mirror in one pass shouldn't doom the whole call, so on a
    # fruitless first pass we back off briefly and retry the hosts ONCE more.
    for attempt in range(2):
        for host in _OVERPASS_HOSTS:
            try:
                _RL.wait(_host(host))
                st, body, e = _http_post(host, data, timeout=40)
            except Exception as ex:  # noqa: BLE001 — never propagate; treat as failure
                err = "request %r" % ex
                continue
            if st == 200 and body:
                try:
                    for el in json.loads(body).get("elements", []):
                        tags = el.get("tags", {}) or {}
                        nm = tags.get("name")
                        if not nm:
                            continue
                        center = el.get("center") or {}
                        places.append({
                            "name": nm,
                            "opening_hours": tags.get("opening_hours"),
                            "address": _osm_addr(tags),
                            "phone": tags.get("phone") or tags.get("contact:phone"),
                            "lat": el.get("lat") or center.get("lat"),
                            "lon": el.get("lon") or center.get("lon"),
                        })
                    if places:
                        break
                except Exception as ex:  # noqa: BLE001
                    err = "parse %r" % ex
            else:
                # 504/502/503/429 and timeouts (st is None) are transient → eligible
                # for the second pass; non-retryable statuses just record the error.
                err = e or ("HTTP %s" % st)
        if places:
            break
        # First pass yielded nothing across all hosts — back off, then retry once.
        if attempt == 0:
            _sleep_backoff(_RETRY_BACKOFF_S)

    res = {"places": places[:limit], "query": query, "attribution": OSM_ATTRIBUTION,
           "center": {"lat": lat, "lon": lon}, "error": "" if places else (err or "no places found")}
    _CACHE.set(ck, res)
    return res
