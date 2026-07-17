"""R-A11 SSRF-dedupe — the canonical fail-closed net-safety module.

`net_safety` lifts the adversarially-hardened SSRF logic that lived only in
`readback_client.py` (IMDS 169.254.169.254, RFC-1918, link-local, NAT64/6to4/
ipv4-mapped/teredo embedded-IPv4, mixed-resolution fail-closed) into ONE module so
every outbound surface routes through the same gate — closing the confirmed-live
unguarded `web_access` egress. These tests pin the fail-closed contract; the
readback adversarial suite is the byte-identical regression floor (separate file).
"""
from __future__ import annotations

import ipaddress

import pytest

from systemu.runtime import net_safety


# ── literal-IP classification ────────────────────────────────────────────────

@pytest.mark.parametrize("ip,nonglobal", [
    ("127.0.0.1", True), ("10.0.0.5", True), ("192.168.1.1", True),
    ("172.16.0.1", True), ("169.254.169.254", True),          # IMDS
    ("::1", True), ("fe80::1", True), ("224.0.0.1", True),
    ("0.0.0.0", True),
    ("8.8.8.8", False), ("1.1.1.1", False), ("93.184.216.34", False),
])
def test_ip_is_non_global(ip, nonglobal):
    assert net_safety.ip_is_non_global(ipaddress.ip_address(ip)) is nonglobal


@pytest.mark.parametrize("ip6", [
    "2002:a9fe:a9fe::",           # 6to4 → 169.254.169.254 (is_global on the OUTER v6)
    "2002:a00:5::",               # 6to4 → 10.0.0.5
    "::ffff:10.0.0.5",            # ipv4-mapped private
])
def test_embedded_ipv4_non_global(ip6):
    # These OUTER v6 addresses are is_global; only the embedded-IPv4 extraction
    # catches them — the load-bearing 6to4 hardening.
    assert net_safety.embedded_ipv4_is_non_global(ipaddress.ip_address(ip6)) is True


def test_embedded_ipv4_public_is_false():
    assert net_safety.embedded_ipv4_is_non_global(
        ipaddress.ip_address("2001:4860:4860::8888")) is False


@pytest.mark.parametrize("addr", [
    "64:ff9b::169.254.169.254",   # NAT64 → IMDS (is_global True, caught by is_reserved)
    "2002:a9fe:a9fe::",           # 6to4 → IMDS (is_global True, caught by embedded-IPv4)
    "2002:a00:5::",               # 6to4 → 10.0.0.5
    "::ffff:10.0.0.5",            # ipv4-mapped
    "169.254.169.254", "10.0.0.5", "127.0.0.1",
])
def test_combined_gate_catches_all_ssrf_classes(addr):
    # The gate that the surfaces actually use is (ip_is_non_global OR embedded).
    # A naive is_global check would MISS NAT64/6to4 (both report is_global True).
    ip = ipaddress.ip_address(addr)
    assert net_safety.ip_is_non_global(ip) or net_safety.embedded_ipv4_is_non_global(ip)


# ── resolve_pinned_ip (fail-closed over getaddrinfo) ─────────────────────────

def _patch_resolve(monkeypatch, addrs):
    def _gai(host, *a, **k):
        if not addrs:
            raise OSError("no resolution")
        return [(2, 1, 6, "", (ip, 0)) for ip in addrs]
    monkeypatch.setattr(net_safety.socket, "getaddrinfo", _gai)


def test_resolve_pinned_public(monkeypatch):
    _patch_resolve(monkeypatch, ["93.184.216.34"])
    assert net_safety.resolve_pinned_ip("example.com") == "93.184.216.34"


def test_resolve_pinned_private_rejected(monkeypatch):
    _patch_resolve(monkeypatch, ["10.0.0.5"])
    assert net_safety.resolve_pinned_ip("evil.internal") is None


def test_resolve_pinned_imds_rejected(monkeypatch):
    _patch_resolve(monkeypatch, ["169.254.169.254"])
    assert net_safety.resolve_pinned_ip("metadata.evil") is None


def test_resolve_pinned_mixed_fails_closed(monkeypatch):
    # one public + one private (a rebind set) → reject the WHOLE set.
    _patch_resolve(monkeypatch, ["93.184.216.34", "10.0.0.5"])
    assert net_safety.resolve_pinned_ip("rebind.evil") is None


def test_resolve_pinned_resolution_failure(monkeypatch):
    _patch_resolve(monkeypatch, [])
    assert net_safety.resolve_pinned_ip("nxdomain.evil") is None
    assert net_safety.host_resolves_public("nxdomain.evil") is False


# ── url_is_admissible (the surface gate) ─────────────────────────────────────

def test_url_admissible_public(monkeypatch):
    _patch_resolve(monkeypatch, ["93.184.216.34"])
    assert net_safety.url_is_admissible("https://example.com/x") is True


def test_url_reject_non_http_schemes():
    assert net_safety.url_is_admissible("file:///etc/passwd") is False
    assert net_safety.url_is_admissible("gopher://x/") is False
    assert net_safety.url_is_admissible("ftp://x/") is False
    assert net_safety.url_is_admissible("") is False


def test_url_reject_imds_literal():
    # No DNS needed — literal IP is validated directly (NAT64 sibling too).
    assert net_safety.url_is_admissible("http://169.254.169.254/latest/meta-data/") is False
    assert net_safety.url_is_admissible("http://[64:ff9b::169.254.169.254]/") is False


def test_url_reject_localhost_and_private(monkeypatch):
    _patch_resolve(monkeypatch, ["127.0.0.1"])
    assert net_safety.url_is_admissible("http://localhost/") is False
    _patch_resolve(monkeypatch, ["10.0.0.5"])
    assert net_safety.url_is_admissible("http://intranet.corp/") is False


def test_url_require_https(monkeypatch):
    _patch_resolve(monkeypatch, ["93.184.216.34"])
    assert net_safety.url_is_admissible("http://example.com/") is True          # default: http ok
    assert net_safety.url_is_admissible("http://example.com/", require_https=True) is False


def test_url_allowed_hosts_escape_hatch(monkeypatch):
    # Operator-configured widening: an allowlisted host is admitted even if it
    # resolves private (explicit opt-in only — never the default).
    _patch_resolve(monkeypatch, ["10.0.0.5"])
    assert net_safety.url_is_admissible("http://mybox.local/", allowed_hosts={"mybox.local"}) is True
    assert net_safety.url_is_admissible("http://other.local/", allowed_hosts={"mybox.local"}) is False


def test_url_missing_host_rejected():
    assert net_safety.url_is_admissible("https:///nohost") is False
