"""Phase 6 Batch 2 (6d) — legacy top-level URLs are TRUE server redirects.

Before 6d the five legacy routes were ``@ui.page`` stubs that returned a 200
HTML page whose only job was a client-side ``ui.navigate.to(...)`` hop.  6d
swaps those for real HTTP 3xx redirects registered on the Starlette/FastAPI
``app`` via ``app.add_route``, so curl / bots / prefetch all land correctly
without executing JS.

We assert two things, both without a live server:
  - the pure mapping ``_legacy_redirect_routes()`` lists every legacy path with
    its correct target (the single source of truth ``register_routes`` feeds
    into ``app.add_route``);
  - each registered route, when invoked, returns a ``RedirectResponse`` with
    status 307 (temporary, method-preserving) and the right ``Location``.

6h flipped the Shadows rename: ``/army`` -> ``/shadows`` (army is now the
legacy alias; /shadows is the canonical route).
"""
from starlette.responses import RedirectResponse

from systemu.interface.dashboard import _legacy_redirect_routes


EXPECTED = {
    "/systemu-chat": "/chat?tab=live",
    "/memory":       "/insights?tab=memory",
    "/flywheel":     "/insights?tab=flywheel",
    "/notifications": "/insights?tab=events",
    "/army":         "/shadows",
}


def test_mapping_covers_every_legacy_path_with_correct_target():
    mapping = dict((path, target) for path, target in _legacy_redirect_routes())
    assert mapping == EXPECTED


def test_no_duplicate_legacy_paths():
    paths = [path for path, _target in _legacy_redirect_routes()]
    assert len(paths) == len(set(paths))


def test_each_route_returns_a_307_redirect_to_its_target():
    for path, target in _legacy_redirect_routes():
        handler = _redirect_handler(target)
        resp = handler(request=None)
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 307, f"{path} must be a 307 temporary redirect"
        assert resp.headers["location"] == target


# Mirror the handler factory used by register_routes so we test the real shape
# (a callable taking the Starlette request and returning a RedirectResponse).
def _redirect_handler(target: str):
    from systemu.interface.dashboard import _make_redirect

    return _make_redirect(target)
