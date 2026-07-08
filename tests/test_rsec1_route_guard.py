"""R-SEC1 — dashboard route-guard middleware + login page.

The central authentication-enforcement task: when a passphrase is configured
(``state.dashboard_auth_active is True``) EVERY dashboard route requires an
authenticated session. Unauthenticated browser navigation → 302 /login; an
unauthenticated XHR/API request → 401. When no passphrase is configured
(``dashboard_auth_active`` False — the loopback default) the guard is a STRICT
pass-through no-op so the existing UI regression floor stays green untouched.

The security core is the pure decision function ``_guard_decision`` — exercised
exhaustively here. On top of it:
  * an integration test drives the real Starlette middleware end-to-end with a
    ``fastapi.testclient.TestClient`` for the deny paths (redirect + 401);
  * AC-SEC1 is the live-route-walk: every registered ``@ui.page`` route that is
    NOT on the allowlist MUST be guarded when active + not-authed — so a NEW
    page can never ship unguarded;
  * verify+lockout wiring is proven against the real ``LockoutStore`` +
    ``dashboard_auth.verify``: 5 wrong → locked → a correct passphrase during
    the lockout window is still refused.

The pure-function approach is deliberate: rendering a live ``@ui.page`` under a
bare TestClient requires NiceGUI's ``ui.run()`` config bootstrap (absent in a
unit test), so the PASS path is proven by the decision function + allowlist
enumeration rather than by rendering the page. The DENY paths (redirect/401)
short-circuit before the page handler, so those ARE driven through the real
middleware.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# The security core — the pure decision function, tested exhaustively.
# --------------------------------------------------------------------------- #

from systemu.interface.dashboard import _guard_decision


class TestGuardDecisionNoOp:
    """active=False → the guard is a strict pass-through no-op (regression floor)."""

    @pytest.mark.parametrize("path", ["/", "/data", "/settings", "/login", "/_nicegui/x"])
    @pytest.mark.parametrize("authed", [True, False])
    @pytest.mark.parametrize("accept", ["text/html", "application/json", ""])
    def test_inactive_always_passes(self, path, authed, accept):
        assert _guard_decision(path, accept, authed=authed, active=False) == "pass"


class TestGuardDecisionActive:
    """active=True → enforce. Unauthenticated non-allowlisted routes are denied."""

    def test_unauthed_html_navigation_redirects(self):
        assert _guard_decision("/data", "text/html", authed=False, active=True) == "redirect"

    def test_unauthed_html_with_charset_still_redirects(self):
        # Browsers send a long Accept header; substring match must still fire.
        accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        assert _guard_decision("/data", accept, authed=False, active=True) == "redirect"

    def test_unauthed_xhr_json_gets_401(self):
        assert _guard_decision("/data", "application/json", authed=False, active=True) == "401"

    def test_unauthed_no_accept_gets_401(self):
        # No Accept header (curl/bot/XHR) is treated as a non-navigation → 401,
        # never a redirect-loop, never an open.
        assert _guard_decision("/data", "", authed=False, active=True) == "401"

    def test_authed_passes(self):
        assert _guard_decision("/data", "text/html", authed=True, active=True) == "pass"


class TestAllowlistSegmentBoundary:
    """FINDING B — the prefix allowlist must match on a SEGMENT boundary, not a
    raw ``startswith``. Without a boundary, ``/login-history``, ``/assets-export``,
    ``/staticdata`` and ``/_nicegui_admin`` were treated as allowlisted and served
    unauthenticated — a latent auth bypass."""

    @pytest.mark.parametrize("path", [
        "/loginX",
        "/login-history",
        "/assets-export",
        "/assets-secret",
        "/staticdata",
        "/static-report",
        "/_nicegui_admin",
        "/_nicegui_wsx",
    ])
    def test_prefix_collisions_are_NOT_allowlisted(self, path):
        from systemu.interface.dashboard import _is_auth_allowlisted
        assert _is_auth_allowlisted(path) is False, f"{path} must be GUARDED"

    @pytest.mark.parametrize("path", [
        "/login",
        "/favicon.ico",
        "/assets/fonts/inter.woff2",
        "/_nicegui/3.11.1/static/x.js",
        "/_nicegui_ws/socket.io/xhr",
        "/static/anything",
    ])
    def test_real_infra_paths_stay_allowlisted(self, path):
        from systemu.interface.dashboard import _is_auth_allowlisted
        assert _is_auth_allowlisted(path) is True, f"{path} must be allowlisted"

    def test_collision_path_is_denied_via_guard_decision(self):
        """End-to-end at the decision layer: a prefix-collision path must DENY
        (redirect for HTML) when active + not-authed — never pass."""
        from systemu.interface.dashboard import _guard_decision
        assert _guard_decision("/assets-export", "text/html",
                               authed=False, active=True) == "redirect"


class TestGuardDecisionAllowlist:
    """Allowlisted paths pass even when active + not-authed (login must be reachable)."""

    def test_login_exact_passes(self):
        assert _guard_decision("/login", "text/html", authed=False, active=True) == "pass"

    @pytest.mark.parametrize("path", [
        "/login",
        "/login?next=/data",
        "/_nicegui/3.11.1/static/foo.js",
        "/_nicegui_ws/socket",
        "/assets/fonts/inter.woff2",
        "/static/anything",
        "/favicon.ico",
    ])
    def test_infra_paths_pass_unauthenticated(self, path):
        # Strip any query string the way the middleware sees request.url.path;
        # the decision function receives the bare path.
        bare = path.split("?", 1)[0]
        assert _guard_decision(bare, "text/html", authed=False, active=True) == "pass"

    def test_nicegui_internal_must_be_exempt(self):
        # If /_nicegui/* were guarded, the login page's own JS + websocket would
        # 302 and the page could never load — a self-inflicted lockout.
        assert _guard_decision("/_nicegui/3.11.1/static/nicegui.js", "text/html",
                               authed=False, active=True) == "pass"


# --------------------------------------------------------------------------- #
# Integration — drive the real Starlette middleware through a TestClient.
# Only the DENY paths are exercised end-to-end (they short-circuit before the
# NiceGUI page handler, which needs ui.run()'s config bootstrap to render).
# --------------------------------------------------------------------------- #

class _GuardState:
    """Tiny stand-in for AppState — only the flag the middleware reads."""
    dashboard_auth_active = False


def _bootstrap_nicegui_config(core):
    """Populate NiceGUI's AppConfig for a bare TestClient (no ui.run()).

    Version-tolerant: fills every unset dataclass field with a benign default
    (bool→False, int→0, float→0.0, str→"", else None) so the response builder
    (which reads markdown / prod_js / tailwind / … ) can render a page or 404
    without an AttributeError."""
    import dataclasses
    cfg = core.app.config
    try:
        fields = dataclasses.fields(cfg)
    except Exception:
        return
    for f in fields:
        # A missing (never-set) field raises on getattr; treat that as "unset".
        try:
            current = getattr(cfg, f.name)
            missing = current is None
        except Exception:
            missing = True
        if not missing:
            continue
        anno = str(f.type)
        if "bool" in anno:
            default = False
        elif "int" in anno and "float" not in anno:
            default = 0
        elif "float" in anno:
            default = 0.0
        elif "str" in anno:
            default = ""
        else:
            default = None
        try:
            setattr(cfg, f.name, default)
        except Exception:
            pass


@pytest.fixture(scope="module")
def guarded_app():
    """The real NiceGUI ASGI app with the route guard installed + a probe page.

    MODULE-SCOPED on purpose: Starlette locks the middleware stack once the app
    has started serving (a TestClient lifespan), so the guard can be added
    exactly ONCE — and a NiceGUI @ui.page path can be registered exactly once.
    Each test toggles the shared ``state.dashboard_auth_active`` flag, which the
    middleware re-reads on every request, so per-test posture control is retained
    without re-registering anything.

    Yields (app, state, testclient).
    """
    from nicegui import app as ng_app, ui, core
    from fastapi.testclient import TestClient
    from systemu.interface import dashboard as dash

    # NiceGUI populates AppConfig only inside ui.run(); a bare TestClient never
    # calls it, so the render/404 path trips over unset config flags (markdown,
    # prod_js, …). Bootstrap the run config once — via NiceGUI's own
    # ``add_run_config`` when its signature is available, else by filling in any
    # missing dataclass fields — so a PASSED-THROUGH request resolves to a real
    # status instead of crashing the render/404 handler. (The guard's own
    # 302/401 short-circuit before this, so it is unaffected.)
    _bootstrap_nicegui_config(core)

    state = _GuardState()

    # Register the guard against our stand-in state, exactly as run_dashboard does.
    dash._install_route_guard(ng_app, state)

    # A probe page proves live @ui.page routes are subject to the guard.
    @ui.page("/probe_rsec1")
    def _probe():
        ui.label("secret")

    # raise_server_exceptions=False: rendering a live @ui.page needs NiceGUI's
    # ui.run() config bootstrap (absent in a unit test), so a PASSED-THROUGH
    # request 500s at the render layer. We want that surfaced as a 500 RESPONSE
    # (not a propagated exception) so "the guard did NOT emit 302/401" is a clean
    # assertion. The guard's own 302/401 responses are unaffected.
    client = TestClient(ng_app, raise_server_exceptions=False)
    yield ng_app, state, client


def test_middleware_noop_when_inactive_reaches_page_layer(guarded_app):
    """active=False → the guard must not intercept; the request flows past it.

    We assert the guard did NOT emit a 302/401 (it may fail later in the
    NiceGUI render for lack of ui.run(), but that is NOT our redirect/401 — a
    500 here still proves the guard was a no-op)."""
    _app, state, client = guarded_app
    state.dashboard_auth_active = False
    r = client.get("/probe_rsec1", headers={"accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code not in (302, 401)  # guard did not intercept


def test_middleware_redirects_unauthed_html(guarded_app):
    _app, state, client = guarded_app
    state.dashboard_auth_active = True
    r = client.get("/probe_rsec1", headers={"accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_middleware_401s_unauthed_xhr(guarded_app):
    _app, state, client = guarded_app
    state.dashboard_auth_active = True
    r = client.get("/probe_rsec1", headers={"accept": "application/json"},
                   follow_redirects=False)
    assert r.status_code == 401
    assert r.json() == {"detail": "authentication required"}


def test_middleware_allows_login_page_unauthenticated(guarded_app):
    _app, state, client = guarded_app
    state.dashboard_auth_active = True
    # /login is allowlisted → the guard passes; the request reaches the page
    # layer (which, absent ui.run(), may error — but NOT with our 302/401).
    r = client.get("/login", headers={"accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code not in (302, 401)


def test_middleware_allows_nicegui_internal_unauthenticated(guarded_app):
    _app, state, client = guarded_app
    state.dashboard_auth_active = True
    r = client.get("/_nicegui/3.11.1/static/nicegui.js",
                   headers={"accept": "*/*"}, follow_redirects=False)
    # Whatever the static layer returns (200/404), the guard must not 302/401 it.
    assert r.status_code not in (302, 401)


def test_middleware_fails_closed_on_guard_exception(guarded_app, monkeypatch):
    """If the decision itself raises, the guard must DENY (401/redirect), never open."""
    _app, state, client = guarded_app
    state.dashboard_auth_active = True
    from systemu.interface import dashboard as dash

    def _boom(*a, **k):
        raise RuntimeError("guard internals blew up")

    monkeypatch.setattr(dash, "_guard_decision", _boom)
    r = client.get("/probe_rsec1", headers={"accept": "application/json"},
                   follow_redirects=False)
    assert r.status_code in (302, 401)  # fail CLOSED, never 200/pass


# --------------------------------------------------------------------------- #
# AC-SEC1 — the live-route-walk. Enumerate the app's real page routes and assert
# each non-allowlisted one is guarded when active + not-authed. This is the
# guarantee that a NEW page can never ship unguarded.
# --------------------------------------------------------------------------- #

def _enumerate_page_paths(ng_app):
    """Concrete GET page paths registered on the app (skip param/mount/ws)."""
    paths = []
    for route in ng_app.routes:
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/"):
            continue
        if "{" in path:          # parameterised — represented by a sample below
            continue
        if "GET" not in methods:  # mounts/websockets have no GET method set
            continue
        paths.append(path)
    return paths


def test_ac_sec1_every_non_allowlisted_page_is_guarded():
    """Register the real dashboard routes, then assert each concrete page path
    is either allowlisted OR denied to an unauthenticated caller when active."""
    from systemu.interface import dashboard as dash

    # Build the real route table (idempotent import-time registration).
    try:
        dash.register_routes()
    except Exception as exc:  # pragma: no cover - only if nicegui import breaks
        pytest.skip(f"register_routes unavailable: {exc}")

    from nicegui import app as ng_app
    page_paths = _enumerate_page_paths(ng_app)
    assert page_paths, "expected at least the '/' page route to be registered"

    # Also throw in representative deep/param paths a new page might claim.
    sample_paths = page_paths + ["/data", "/api/anything", "/workflow/abc123",
                                 "/memory/shadow1", "/recover/tool/t1"]

    offenders = []
    for path in sample_paths:
        decision = dash._guard_decision(path, "text/html", authed=False, active=True)
        allowlisted = dash._is_auth_allowlisted(path)
        if decision == "pass" and not allowlisted:
            offenders.append(path)
    assert not offenders, f"UNGUARDED non-allowlisted routes: {offenders}"


def test_ac_sec1_allowlist_is_minimal():
    """The allowlist must cover ONLY the login page + framework infra — not any
    real content page. A regression that widened it would let '/' leak."""
    from systemu.interface import dashboard as dash
    for leaky in ["/", "/settings", "/tools", "/work", "/insights", "/data"]:
        assert not dash._is_auth_allowlisted(leaky), f"{leaky} must NOT be allowlisted"
    for infra in ["/login", "/_nicegui/x", "/assets/y", "/static/z", "/favicon.ico"]:
        assert dash._is_auth_allowlisted(infra), f"{infra} MUST be allowlisted"


def test_ac_sec1_prefix_collision_decoy_page_is_denied(guarded_app):
    """FINDING B — STRENGTHEN AC-SEC1: register a decoy page at a prefix-collision
    name (``/assets-export`` — collides with the ``/assets`` allow-prefix) and
    drive it through the REAL guard middleware. When active + not-authed it MUST
    be denied (redirect for HTML), proving the raw-startswith bypass class is
    caught going forward — the live-route-walk alone did not exercise this."""
    from nicegui import app as ng_app, ui
    _app, state, client = guarded_app
    state.dashboard_auth_active = True

    # A real @ui.page whose path prefix-collides with the /assets allow-prefix.
    # Registered here (module-scoped app), guaranteed on the route table.
    @ui.page("/assets-export")
    def _decoy_assets_export():
        ui.label("secret-inventory")

    r = client.get("/assets-export", headers={"accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code == 302, "prefix-collision decoy must be GUARDED, not served"
    assert r.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# verify + lockout wiring — the login page's real auth logic, against the real
# LockoutStore + dashboard_auth.verify. 5 wrong → locked → correct-during-lockout
# is still refused.
# --------------------------------------------------------------------------- #

def test_verify_and_lockout_wiring(tmp_path):
    from systemu.runtime import dashboard_auth as da
    from systemu.interface.pages.login import _attempt_login

    vault = tmp_path / "vault"
    (vault / "secrets").mkdir(parents=True)
    da.set_passphrase(vault, "s3cret-pass")
    stored = da.get_passphrase_hash_vault(vault)
    assert stored is not None

    lockout = da.LockoutStore(vault / "secrets" / "dashboard_lockout.json")
    ip = "203.0.113.7"

    # 5 wrong attempts → the IP is locked out.
    for _ in range(da.FAILURE_THRESHOLD):
        ok, reason = _attempt_login("wrong-pass", stored, lockout, ip)
        assert ok is False
        assert reason == "incorrect"
    assert lockout.is_locked(ip) is True

    # A CORRECT passphrase during the lockout window is STILL refused — the
    # lockout is checked before verify, so brute-force can't slip a hit in.
    ok, reason = _attempt_login("s3cret-pass", stored, lockout, ip)
    assert ok is False
    assert reason == "locked"


def test_successful_login_clears_lockout(tmp_path):
    from systemu.runtime import dashboard_auth as da
    from systemu.interface.pages.login import _attempt_login

    vault = tmp_path / "vault"
    (vault / "secrets").mkdir(parents=True)
    da.set_passphrase(vault, "open-sesame")
    stored = da.get_passphrase_hash_vault(vault)
    lockout = da.LockoutStore(vault / "secrets" / "dashboard_lockout.json")
    ip = "198.51.100.9"

    # A couple of failures (below threshold) then a success clears the counter.
    _attempt_login("nope", stored, lockout, ip)
    _attempt_login("nope", stored, lockout, ip)
    ok, reason = _attempt_login("open-sesame", stored, lockout, ip)
    assert ok is True
    assert reason == "ok"
    assert lockout.is_locked(ip) is False


def test_global_lockout_blocks_login_from_fresh_ip(tmp_path):
    """A distributed spray trips the GLOBAL lockout; a fresh IP that is NOT
    per-IP locked, with the CORRECT passphrase, is still refused (Finding 2)."""
    from systemu.runtime import dashboard_auth as da
    from systemu.interface.pages.login import _attempt_login

    vault = tmp_path / "vault"
    (vault / "secrets").mkdir(parents=True)
    da.set_passphrase(vault, "correct-pass")
    stored = da.get_passphrase_hash_vault(vault)
    lockout = da.LockoutStore(vault / "secrets" / "dashboard_lockout.json")

    # One failed guess each from many distinct IPs — no single IP is per-IP
    # locked, but the global counter crosses its threshold (a couple extra to be
    # robust to >= vs > boundary semantics; all IPs distinct).
    for i in range(da.GLOBAL_FAILURE_THRESHOLD + 2):
        _attempt_login("wrong", stored, lockout, f"203.0.113.{i}")
    assert lockout.is_globally_locked() is True

    fresh_ip = "198.51.100.250"
    assert lockout.is_locked(fresh_ip) is False          # not per-IP locked
    ok, reason = _attempt_login("correct-pass", stored, lockout, fresh_ip)
    assert ok is False                                    # global lockout wins
    assert reason == "locked"
