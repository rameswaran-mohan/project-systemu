"""R-A11 — the legacy web stack (SYSTEMU_WEB_STACK_V2=false) is SSRF-gated too.

The adversarial review found that `web/fetch_core.fetch_url` (httpx, follow_redirects=
True, no guard) and `web/browser_pool.render_html` (domain policy only, no IP check)
were an env-flip away from being the active egress with zero SSRF protection. Both now
route through `net_safety`.
"""
from __future__ import annotations

import pytest

from systemu.runtime import net_safety
from systemu.runtime.web import browser_pool, fetch_core


def _patch_resolve(monkeypatch, addrs):
    monkeypatch.setattr(net_safety.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in addrs])


# ── fetch_core (httpx) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # IMDS literal
    "http://127.0.0.1:8765/",                      # loopback (dashboard)
    "http://[64:ff9b::169.254.169.254]/",          # NAT64 → IMDS
    "file:///etc/passwd",
    "ftp://10.0.0.1/",
])
def test_fetch_url_refuses_blocked_before_httpx(url):
    # The pre-check refuses (no httpx.Client is ever created — no network).
    res = fetch_core.fetch_url(url)
    assert res.ok is False and "ssrf" in (res.error or "").lower()


def test_fetch_url_refuses_private_resolving_host(monkeypatch):
    _patch_resolve(monkeypatch, ["10.0.0.5"])
    res = fetch_core.fetch_url("http://intranet.corp/")
    assert res.ok is False and "ssrf" in (res.error or "").lower()


# ── browser_pool render (Playwright) ─────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8765/",
    "http://[64:ff9b::169.254.169.254]/",
])
def test_render_html_ssrf_gate_raises_before_browser(url):
    # The IP-level gate is the FIRST thing render_html does — it raises before any
    # browser/semaphore work, so a bare instance (no __init__) is enough to prove it.
    bp = browser_pool.BrowserPool.__new__(browser_pool.BrowserPool)
    with pytest.raises(PermissionError):
        bp.render_html(url)


def test_render_html_ssrf_gate_blocks_private_resolving(monkeypatch):
    _patch_resolve(monkeypatch, ["192.168.1.9"])
    bp = browser_pool.BrowserPool.__new__(browser_pool.BrowserPool)
    with pytest.raises(PermissionError):
        bp.render_html("http://nas.local/")
