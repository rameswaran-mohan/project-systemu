"""R-A13b-2i TASK 1 — the production independent-https readback client.

ProdReadbackClient is the SAFETY-CRITICAL transport for the hardened api_readback
path: a credential-less https GET that RE-READS an effect from an external host so
the deterministic matcher can confirm a submission-unique fresh token. These tests
are hermetic (an injected httpx.MockTransport — NO real network) and assert both
the happy read-back shape AND the defense-in-depth SSRF refusals (which must fire
BEFORE any connect).
"""
from __future__ import annotations

import httpx
import pytest

from systemu.runtime.readback_client import ProdReadbackClient


class _SpyTransport(httpx.MockTransport):
    """An httpx.MockTransport that COUNTS how many requests reached it — so an SSRF
    refusal can be proven to short-circuit BEFORE any connect (count stays 0)."""

    def __init__(self, handler):
        self.calls = 0

        def _counting(request):
            self.calls += 1
            return handler(request)

        super().__init__(_counting)


# ── happy path: JSON body → observed_tokens parsed + response_body carried ──
def test_readback_parses_json_tokens_and_body():
    def _handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            json={"id": "row-777", "status": "created", "token": "sub-abc-1"})

    transport = _SpyTransport(_handler)
    client = ProdReadbackClient(transport=transport)
    # a LITERAL public IP host keeps the SSRF gate hermetic (no DNS).
    out = client.readback("https://93.184.216.34/rows/777")

    assert transport.calls == 1
    assert isinstance(out, dict)
    assert "sub-abc-1" in out.get("observed_tokens", []), out
    assert "row-777" in out.get("observed_tokens", []), out
    # the raw body is carried so the verifier's substring matcher can also match.
    assert "sub-abc-1" in out.get("response_body", ""), out


# ── happy path: plain-text body → token available via response_body ──
def test_readback_plain_text_body():
    def _handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"},
                              text="row present: confirmation TOKEN-XYZ")

    transport = _SpyTransport(_handler)
    client = ProdReadbackClient(transport=transport)
    out = client.readback("https://93.184.216.34/rows/1")

    assert transport.calls == 1
    assert "TOKEN-XYZ" in out.get("response_body", ""), out


# ── SSRF: non-https refused BEFORE any connect ──
def test_refuses_http_scheme_before_connect():
    transport = _SpyTransport(lambda r: httpx.Response(200, text="should never run"))
    client = ProdReadbackClient(transport=transport)
    out = client.readback("http://93.184.216.34/rows/1")
    assert out == {}, out
    assert transport.calls == 0, "an http:// url must be refused before any connect"


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x",          # loopback
    "https://10.0.0.5/x",           # RFC1918 private
    "https://192.168.1.1/x",        # RFC1918 private
    "https://169.254.169.254/x",    # cloud-metadata (link-local)
    "https://localhost/x",          # loopback name
])
def test_refuses_private_and_metadata_hosts_before_connect(url):
    transport = _SpyTransport(lambda r: httpx.Response(200, text="should never run"))
    client = ProdReadbackClient(transport=transport)
    out = client.readback(url)
    assert out == {}, (url, out)
    assert transport.calls == 0, f"{url} must be SSRF-refused before any connect"


# ── FIX 1 (C1): NAT64-embedded / reserved / multicast literal IPs SSRF-refused ──
#
# is_url_safe rejects ONLY loopback/private/link-local/unspecified — NOT is_reserved
# or is_multicast. So a NAT64 well-known-prefix literal (64:ff9b::/96) that embeds a
# cloud-metadata / RFC1918 IPv4 (is_reserved=True) and IPv6/IPv4 multicast literals
# slipped through the literal-IP branch. In a NAT64/IPv6-only VPC the gateway
# translates 64:ff9b::169.254.169.254 → the real 169.254.169.254 (IMDS). The
# literal-IP branch must mirror _resolves_to_public (also reject reserved/multicast).
@pytest.mark.parametrize("url", [
    "https://[64:ff9b::169.254.169.254]/latest/meta-data/",  # NAT64→IMDS (is_reserved)
    "https://[64:ff9b::10.0.0.5]/x",                          # NAT64→RFC1918 (is_reserved)
    "https://[ff02::1]/x",                                    # IPv6 multicast
    "https://224.0.0.1/x",                                    # IPv4 multicast
    "https://[::ffff:169.254.169.254]/x",                     # IPv4-mapped metadata
])
def test_refuses_nat64_reserved_multicast_literals_before_connect(url):
    from systemu.runtime.readback_client import _url_is_admissible
    assert _url_is_admissible(url) is False, f"{url} must fail the SSRF gate"
    transport = _SpyTransport(lambda r: httpx.Response(200, text="should never run"))
    client = ProdReadbackClient(transport=transport)
    out = client.readback(url)
    assert out == {}, (url, out)
    assert transport.calls == 0, f"{url} must be SSRF-refused before any connect"


# ── FIX 1 no-regression: a normal public https literal IP is still ADMITTED ──
def test_public_literal_ip_still_admissible():
    from systemu.runtime.readback_client import _url_is_admissible
    assert _url_is_admissible("https://93.184.216.34/rows/1") is True


# ── HARDENING 1 (6to4): an IPv6 literal EMBEDDING an internal IPv4 SSRF-refused ──
#
# The prior C1 fix added is_reserved/is_multicast, which closed the NAT64 sibling
# (64:ff9b::/96 is_reserved) but LEFT 6to4 (2002::/16) open: a 6to4 literal
# embedding an RFC1918/metadata IPv4 classifies as is_global (none of
# is_loopback/is_private/is_link_local/is_reserved/is_multicast/is_unspecified
# set), so it PASSED. In a 6to4 tunnel the gateway translates 2002:a9fe:a9fe::
# back to the embedded 169.254.169.254 (IMDS) / 2002:a00:5:: → 10.0.0.5. The gate
# must EXTRACT the embedded IPv4 (ip.sixtofour / ipv4_mapped / teredo client) and
# reject if it is non-global — in BOTH the literal-IP branch and the resolve path.
@pytest.mark.parametrize("url", [
    "https://[2002:a9fe:a9fe::]/latest/meta-data/",  # 6to4 → 169.254.169.254 (IMDS)
    "https://[2002:a00:5::]/",                        # 6to4 → 10.0.0.5 (RFC1918)
    "https://[2002:7f00:1::]/",                       # 6to4 → 127.0.0.1 (loopback)
])
def test_refuses_6to4_embedded_internal_ipv4_before_connect(url):
    from systemu.runtime.readback_client import _url_is_admissible
    assert _url_is_admissible(url) is False, f"{url} must fail the SSRF gate (6to4 embed)"
    transport = _SpyTransport(lambda r: httpx.Response(200, text="should never run"))
    client = ProdReadbackClient(transport=transport)
    out = client.readback(url)
    assert out == {}, (url, out)
    assert transport.calls == 0, f"{url} must be SSRF-refused before any connect"


# ── HARDENING 1 no-regression: a NORMAL public IPv6 literal is still ADMITTED ──
def test_public_ipv6_literal_still_admissible():
    from systemu.runtime.readback_client import _url_is_admissible
    # 2606:4700:4700::1111 (Cloudflare) — global, NOT 6to4/mapped/teredo.
    assert _url_is_admissible("https://[2606:4700:4700::1111]/") is True
    # a 6to4 literal embedding a PUBLIC IPv4 (8.8.8.8) is a legit public target.
    assert _url_is_admissible("https://[2002:808:808::]/") is True


# ── HARDENING 1: a HOSTNAME that resolves to a 6to4-embedded internal IPv4 is
# also rejected by the resolve-then-reject guard. ──
def test_resolves_to_6to4_embedded_internal_is_rejected(monkeypatch):
    import systemu.runtime.readback_client as rc

    def _fake_getaddrinfo(host, *a, **k):
        # host resolves to a 6to4 address embedding 169.254.169.254 (IMDS).
        return [(0, 0, 0, "", ("2002:a9fe:a9fe::", 0, 0, 0))]

    monkeypatch.setattr(rc.socket, "getaddrinfo", _fake_getaddrinfo)
    assert rc._resolves_to_public("evil.example.com") is False
    # and the whole gate refuses it too.
    assert rc._url_is_admissible("https://evil.example.com/x") is False


def test_resolves_to_normal_public_ipv6_is_admitted(monkeypatch):
    import systemu.runtime.readback_client as rc

    def _fake_getaddrinfo(host, *a, **k):
        return [(0, 0, 0, "", ("2606:4700:4700::1111", 0, 0, 0))]  # Cloudflare, global

    monkeypatch.setattr(rc.socket, "getaddrinfo", _fake_getaddrinfo)
    assert rc._resolves_to_public("cloudflare-dns.example") is True


# ── I4: the client NEVER raises into a run — a transport error yields {} ──
def test_transport_error_returns_empty_never_raises():
    def _boom(request):
        raise httpx.ConnectError("connection refused")

    transport = _SpyTransport(_boom)
    client = ProdReadbackClient(transport=transport)
    out = client.readback("https://93.184.216.34/rows/1")   # a public literal IP
    assert out == {}, out


# ── follow_redirects=False — a redirect target is NOT followed / read ──
def test_redirect_is_not_followed():
    def _handler(request):
        # a 302 to a DIFFERENT (would-be-internal) host: must NOT be chased.
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})

    transport = _SpyTransport(_handler)
    client = ProdReadbackClient(transport=transport)
    out = client.readback("https://93.184.216.34/rows/1")
    # exactly ONE request (the redirect was not followed) and no observable body.
    assert transport.calls == 1, "a redirect must NOT be followed (host-pin defeat)"
    assert out == {}, out
