"""R-A11 — the load-bearing fix: web_access egress is now SSRF-gated.

Before: web_access._http_get/_http_post reached urllib.request.urlopen with NO
guard — a live SSRF/confused-deputy hole reachable from web_read / web_search /
find_places / geocode. These tests prove a blocked destination REFUSES without
ever calling urlopen, a public destination still fetches, and the operator escape
hatch works.
"""
from __future__ import annotations

import pytest

from systemu.runtime import net_safety, web_access


class _UrlopenReached(Exception):
    pass


def _forbid_urlopen(monkeypatch):
    def _boom(*a, **k):
        raise _UrlopenReached("egress must NOT be reached for a blocked URL")
    monkeypatch.setattr(web_access, "_urlopen", _boom)


def _patch_resolve(monkeypatch, addrs):
    monkeypatch.setattr(net_safety.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in addrs])


class _Resp:
    status = 200
    def read(self):
        return b"ok"
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata (IMDS) literal
    "http://[64:ff9b::169.254.169.254]/",          # NAT64 → IMDS
    "http://127.0.0.1:8765/",                      # loopback (the dashboard!)
    "http://[::1]/",
    "file:///etc/passwd",
    "gopher://evil/",
    "ftp://evil/",
])
def test_http_get_refuses_blocked_without_reaching_urlopen(monkeypatch, url):
    _forbid_urlopen(monkeypatch)
    status, text, err = web_access._http_get(url)          # must NOT raise _UrlopenReached
    assert status is None and text == "" and "blocked" in err.lower()


def test_http_get_refuses_private_resolving_host(monkeypatch):
    _patch_resolve(monkeypatch, ["10.0.0.5"])              # a name that resolves RFC-1918
    _forbid_urlopen(monkeypatch)
    status, text, err = web_access._http_get("http://intranet.corp/")
    assert status is None and "blocked" in err.lower()


def test_http_post_refuses_imds_without_urlopen(monkeypatch):
    _forbid_urlopen(monkeypatch)
    status, text, err = web_access._http_post("http://169.254.169.254/", b"x")
    assert status is None and "blocked" in err.lower()


def test_http_get_allows_public_host(monkeypatch):
    _patch_resolve(monkeypatch, ["93.184.216.34"])
    monkeypatch.setattr(web_access, "_urlopen", lambda *a, **k: _Resp())
    status, text, err = web_access._http_get("https://example.com/")
    assert status == 200 and text == "ok" and err == ""


def test_escape_hatch_admits_operator_allowlisted_private(monkeypatch):
    monkeypatch.setenv("SYSTEMU_ALLOWED_OUTBOUND_HOSTS", "mybox.local, other.local")
    _patch_resolve(monkeypatch, ["10.0.0.5"])              # resolves private…
    monkeypatch.setattr(web_access, "_urlopen", lambda *a, **k: _Resp())
    status, _text, _err = web_access._http_get("http://mybox.local/")
    assert status == 200                                   # …but is explicitly allowlisted


def test_escape_hatch_does_not_admit_unlisted(monkeypatch):
    monkeypatch.setenv("SYSTEMU_ALLOWED_OUTBOUND_HOSTS", "mybox.local")
    _patch_resolve(monkeypatch, ["10.0.0.5"])
    _forbid_urlopen(monkeypatch)
    status, _text, err = web_access._http_get("http://sneaky.local/")
    assert status is None and "blocked" in err.lower()


# ── the render bypass (direct Chromium egress) is gated too ──────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # IMDS via a local render
    "http://127.0.0.1:8765/",                      # the dashboard via a local render
])
def test_browser_render_refuses_blocked_url_before_any_render(url):
    # render=True reaches the network through a LOCAL Chromium, not _http_get —
    # a blocked URL must return None BEFORE importing/using BrowserPool (so this
    # holds even in an env with no Playwright installed).
    assert web_access._browser_render(url) is None


# ── redirect-following is re-gated (the classic resolve-then-reject bypass) ──

@pytest.mark.parametrize("newurl", [
    "http://169.254.169.254/latest/meta-data/",   # 30x → IMDS
    "http://127.0.0.1/",                           # 30x → loopback
    "file:///etc/passwd",                          # 30x → non-http scheme
])
def test_redirect_to_blocked_target_is_refused(newurl):
    # A public URL that 302-redirects to an internal/metadata host: the guarded
    # redirect handler REFUSES the hop (raises), so urlopen never follows it.
    h = web_access._SSRFGuardedRedirectHandler()
    req = web_access.urllib.request.Request("https://public.example/")
    with pytest.raises(web_access.urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, newurl)


def test_redirect_to_public_target_is_followed(monkeypatch):
    _patch_resolve(monkeypatch, ["93.184.216.34"])
    h = web_access._SSRFGuardedRedirectHandler()
    req = web_access.urllib.request.Request("https://public.example/")
    out = h.redirect_request(req, None, 302, "Found", {}, "https://example.com/next")
    assert out is not None          # a public redirect target is followed, not blocked


# ── the socket-pin: dial the VETTED IP literal, closing the rebind TOCTOU ─────
# url_is_admissible / the redirect re-check VET the host, but urllib would resolve
# AGAIN at connect — a rebinding DNS answer could swap in an internal IP in that
# window. The pin resolves ONCE to a vetted-public literal and dials THAT, so the
# kernel connects to the exact address the gate approved. SNI + Host stay the name.

def test_pin_dials_the_vetted_ip_literal_not_the_hostname(monkeypatch):
    monkeypatch.setattr(net_safety, "resolve_pinned_ip", lambda h: "93.184.216.34")
    seen = {}
    def _cap(address, *a, **k):
        seen["addr"] = address
        raise _UrlopenReached("stop before a real connect")
    monkeypatch.setattr(web_access._socket, "create_connection", _cap)
    with pytest.raises(_UrlopenReached):
        web_access._pinned_connect(("example.com", 443), 30, None)
    assert seen["addr"] == ("93.184.216.34", 443)   # the LITERAL, not "example.com"


def test_pin_refuses_private_resolving_host_before_dialing(monkeypatch):
    monkeypatch.setattr(net_safety, "resolve_pinned_ip", lambda h: None)  # non-global/failed
    def _boom(*a, **k):
        raise AssertionError("must not dial a host that failed the pin")
    monkeypatch.setattr(web_access._socket, "create_connection", _boom)
    with pytest.raises(OSError):
        web_access._pinned_connect(("intranet.corp", 80), 30, None)


def test_pin_respects_operator_allowlist_dialing_host_as_is(monkeypatch):
    # An operator-widened host may be internal BY DESIGN — the pin must NOT re-vet it
    # (resolve_pinned_ip fail-closes on private), else the escape hatch would break.
    monkeypatch.setenv("SYSTEMU_ALLOWED_OUTBOUND_HOSTS", "mybox.local")
    def _no_resolve(h):
        raise AssertionError("an allowlisted host must not be pinned/re-vetted")
    monkeypatch.setattr(net_safety, "resolve_pinned_ip", _no_resolve)
    seen = {}
    monkeypatch.setattr(web_access._socket, "create_connection",
                        lambda address, *a, **k: seen.setdefault("addr", address))
    web_access._pinned_connect(("mybox.local", 8765), 30, None)
    assert seen["addr"] == ("mybox.local", 8765)    # dialed as-is (operator-widened)


def test_pinned_https_connection_pins_creator_but_keeps_hostname():
    c = web_access._PinnedHTTPSConnection("example.com", 443)
    assert c._create_connection is web_access._pinned_connect   # the socket is pinned…
    assert c.host == "example.com"          # …but SNI + Host header stay the hostname


def test_opener_wires_the_pinned_handlers():
    kinds = {type(h).__name__ for h in web_access._SSRF_OPENER.handlers}
    assert "_PinnedHTTPSHandler" in kinds and "_PinnedHTTPHandler" in kinds


def test_pin_exempts_configured_proxy_host_so_egress_still_works(monkeypatch):
    # REGRESSION (adversarial review F1): urllib routes the connection to the PROXY
    # (self.host becomes the proxy), so a private/localhost proxy — corporate 10.x or
    # a localhost debug proxy — must still be DIALED, not fail-closed by the pin. The
    # real TARGET is still SSRF-pre-checked upstream; the proxy resolves the target.
    monkeypatch.setenv("HTTP_PROXY", "http://10.20.30.40:3128")
    def _no_resolve(h):
        raise AssertionError("a configured proxy host must not be pinned/re-vetted")
    monkeypatch.setattr(net_safety, "resolve_pinned_ip", _no_resolve)
    seen = {}
    monkeypatch.setattr(web_access._socket, "create_connection",
                        lambda address, *a, **k: seen.setdefault("addr", address))
    web_access._pinned_connect(("10.20.30.40", 3128), 30, None)
    assert seen["addr"] == ("10.20.30.40", 3128)   # dialed as-is (operator proxy infra)


def test_no_proxy_key_is_not_mistaken_for_a_proxy_host(monkeypatch):
    # getproxies() surfaces no_proxy under key 'no' with a comma-list value — it must
    # NOT be parsed into a dialable proxy host (would silently widen the exemption).
    monkeypatch.setenv("NO_PROXY", "example.com,10.0.0.0/8")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    assert "example.com" not in web_access._proxy_hosts()


def test_http_get_maps_a_pin_rebind_block_to_the_canonical_refusal(monkeypatch):
    # F2: a genuine rebind caught at CONNECT (the pin) must surface as the clean,
    # greppable `blocked: … (SSRF guard)` tuple — not a raw URLError repr.
    monkeypatch.setattr(net_safety, "resolve_pinned_ip", lambda h: "93.184.216.34")  # pre-check passes
    def _raise(*a, **k):
        raise web_access.urllib.error.URLError(web_access._PinBlocked("rebind at connect"))
    monkeypatch.setattr(web_access, "_urlopen", _raise)
    status, text, err = web_access._http_get("https://example.com/")
    assert status is None and text == "" and "blocked" in err.lower()
