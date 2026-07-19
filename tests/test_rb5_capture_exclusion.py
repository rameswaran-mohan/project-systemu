"""R-B5 / T5 §5.10.b#6 + §5.10.e AC4b — capture exclusion of systemu's own surfaces.

    "`/table` (especially the Keys zone) and the chat strip are excluded from
     evidence/attest/`web_act`/screenshot capture."

Two independent things are pinned here, and they must stay independent:

  1. the exclusion itself (``capture_exclusion``), which is an ORIGIN rule about
     systemu's own UI, and
  2. the R-A11 SSRF gate, which is an IP rule about where a browser may go.

On a default install the SSRF gate happens to also block the dashboard (loopback),
which makes it very easy to "verify" the exclusion against a refusal the SSRF gate
actually produced. ``test_the_exclusion_is_not_just_the_ssrf_guard`` deliberately
configures a NON-loopback, allow-listed dashboard host — SSRF-clean — and pins that
the capture is still refused.

Also pinned: the R-A11 parity gap this milestone closed. ``BrowserPool.screenshot``
and ``web_act`` drove a real browser behind only the IP-blind domain policy, while
the otherwise-identical ``render_html`` carried the SSRF gate.
"""
from __future__ import annotations

import pytest

from systemu.runtime import capture_exclusion, net_safety


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("SYSTEMU_DASHBOARD_ORIGIN", "SYSTEMU_DASHBOARD_HOST",
                "SYSTEMU_DASHBOARD_PORT", "SYSTEMU_WEB_ALLOW_DOMAINS",
                "SYSTEMU_WEB_DENY_DOMAINS", "SYSTEMU_ALLOWED_OUTBOUND_HOSTS"):
        monkeypatch.delenv(var, raising=False)


# ── the predicate ───────────────────────────────────────────────────────────
def test_the_table_page_is_an_own_surface(monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    assert capture_exclusion.is_own_surface("http://localhost:8765/table") is True


def test_the_exclusion_is_origin_scoped_not_path_scoped(monkeypatch):
    """A path check would be defeated by a query string, a redirect, or a deep link,
    and the neighbouring pages leak the same inventory."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    for path in ("/table", "/table?zone=keys", "/", "/inbox", "/tools", "/settings"):
        assert capture_exclusion.is_own_surface(f"http://localhost:8765{path}") is True


def test_loopback_spellings_are_one_host(monkeypatch):
    """A 127.0.0.1-stamped origin must match a tab the browser opened as localhost."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://127.0.0.1:8765")
    assert capture_exclusion.is_own_surface("http://localhost:8765/table") is True
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    assert capture_exclusion.is_own_surface("http://127.0.0.1:8765/table") is True


def test_a_different_port_is_not_our_surface(monkeypatch):
    """The rule must be precise: another local app is not systemu."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    assert capture_exclusion.is_own_surface("http://localhost:9999/table") is False


def test_an_ordinary_site_is_not_excluded(monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    assert capture_exclusion.is_own_surface("https://example.com/table") is False
    assert capture_exclusion.refusal_reason("https://example.com/table") is None


def test_an_unparseable_url_fails_closed(monkeypatch):
    """If we cannot tell what we are about to capture, refuse."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")

    class _Boom(str):
        pass

    monkeypatch.setattr(capture_exclusion, "urlsplit",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad")))
    assert capture_exclusion.is_own_surface("http://anything/") is True


def test_a_bare_file_path_is_not_an_own_surface(monkeypatch):
    """A local output path is not a URL and must not trip the origin rule."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    assert capture_exclusion.is_own_surface("C:/tmp/shot.png") is False
    assert capture_exclusion.is_own_surface("") is False


def test_the_refusal_names_the_rule(monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    reason = capture_exclusion.refusal_reason("http://localhost:8765/table")
    assert reason and "5.10.b#6" in reason


def test_dispatch_delegates_to_the_canonical_origin(monkeypatch):
    """The recorder self-filter and this exclusion must never hold two different
    notions of 'our own UI'."""
    from systemu.interface.command import dispatch
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:4321")
    assert dispatch._dashboard_origin() == capture_exclusion.dashboard_origin()


# ── independence from the SSRF gate (the load-bearing one) ──────────────────
def test_the_exclusion_is_not_just_the_ssrf_guard(monkeypatch):
    """A NON-loopback, allow-listed dashboard host is SSRF-admissible — and must
    STILL be refused for capture.

    Without this the whole AC4b claim could rest on the SSRF gate's loopback
    rejection, and any deployment that binds the dashboard to a LAN address or
    whitelists its host would silently start capturing `/table`.
    """
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://systemu.internal:8765")
    url = "http://systemu.internal:8765/table"
    monkeypatch.setattr(net_safety, "url_is_admissible", lambda *_a, **_k: True)
    monkeypatch.setattr(net_safety, "allowed_outbound_hosts", lambda: {"systemu.internal"})

    assert capture_exclusion.refusal_reason(url) is not None

    from systemu.runtime.web import browser_pool
    with pytest.raises(PermissionError) as exc:
        browser_pool.BrowserPool.get().screenshot(url, "out.png")
    assert "5.10.b#6" in str(exc.value), (
        "an SSRF-clean dashboard origin was not refused by the capture exclusion"
    )


# ── the three capture paths ────────────────────────────────────────────────
def test_screenshot_refuses_the_dashboard_origin(monkeypatch):
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    from systemu.runtime.web import browser_pool
    with pytest.raises(PermissionError):
        browser_pool.BrowserPool.get().screenshot("http://localhost:8765/table", "o.png")


def test_web_act_refuses_the_dashboard_origin(monkeypatch):
    """`web_act` is the sharpest path — it can OPERATE the board's controls, not
    merely read them."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://localhost:8765")
    from systemu.vault.tools.implementations import web_act
    out = web_act.run(url="http://localhost:8765/table", instruction="read the keys")
    assert out["success"] is False
    assert "5.10.b#6" in out["error"] or "SSRF" in out["error"]


def test_web_act_refusal_is_the_exclusion_when_ssrf_would_pass(monkeypatch):
    """Same independence check as above, on the web_act path."""
    monkeypatch.setenv("SYSTEMU_DASHBOARD_ORIGIN", "http://systemu.internal:8765")
    monkeypatch.setattr(net_safety, "url_is_admissible", lambda *_a, **_k: True)
    monkeypatch.setattr(net_safety, "allowed_outbound_hosts", lambda: {"systemu.internal"})
    from systemu.vault.tools.implementations import web_act
    out = web_act.run(url="http://systemu.internal:8765/table", instruction="x")
    assert out["success"] is False and "5.10.b#6" in out["error"]


# ── the R-A11 parity gap this milestone closed ─────────────────────────────
def test_screenshot_now_carries_the_ssrf_gate(monkeypatch):
    """Before R-B5 this path had ONLY the IP-blind domain policy, so a local
    Chromium could be pointed at IMDS and the result written to a PNG."""
    from systemu.runtime.web import browser_pool
    with pytest.raises(PermissionError) as exc:
        browser_pool.BrowserPool.get().screenshot(
            "http://169.254.169.254/latest/meta-data/", "o.png")
    assert "SSRF" in str(exc.value)


def test_web_act_now_carries_the_ssrf_gate(monkeypatch):
    from systemu.vault.tools.implementations import web_act
    out = web_act.run(url="http://169.254.169.254/latest/meta-data/", instruction="x")
    assert out["success"] is False and "SSRF" in out["error"]


def test_render_html_still_carries_its_ssrf_gate():
    """Coverage hole found by mutating this milestone's own work: deleting the
    SSRF gate from ``render_html`` — the R-A11 control ``screenshot`` was compared
    against — left the whole suite green, so that gate had no test at all. Pinned
    here because R-B5 now depends on it as the reference implementation.
    """
    from systemu.runtime.web import browser_pool
    with pytest.raises(PermissionError) as exc:
        browser_pool.BrowserPool.get().render_html("http://169.254.169.254/latest/meta-data/")
    assert "SSRF" in str(exc.value)


def test_the_domain_policy_alone_would_have_allowed_it():
    """Pins WHY the gates above are needed rather than redundant: the pre-existing
    guard returns True for both the dashboard and IMDS."""
    from systemu.runtime.web.browser_pool import is_url_allowed
    assert is_url_allowed("http://localhost:8765/table") is True
    assert is_url_allowed("http://169.254.169.254/latest/meta-data/") is True


def test_an_ordinary_url_still_reaches_the_browser(monkeypatch):
    """The gates must not have broken the normal path: an ordinary public URL gets
    past all three checks and reaches the browser launch.

    A sentinel is raised AT the launch rather than letting the real browser run —
    an earlier version of this test drove a live network fetch of example.com,
    which passed for the right reason but made the suite depend on the internet.
    Reaching the sentinel proves all three gates allowed the URL through.
    """
    from systemu.runtime.web import browser_pool

    class _Reached(Exception):
        pass

    def _boom(self):
        raise _Reached()

    monkeypatch.setattr(browser_pool.BrowserPool, "_ensure_browser", _boom)
    with pytest.raises(_Reached):
        browser_pool.BrowserPool.get().screenshot("https://example.com/", "o.png")
