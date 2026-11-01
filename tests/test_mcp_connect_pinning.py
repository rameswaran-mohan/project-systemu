"""R-A11b-1 (connect-time pinning) -- a remote MCP session connects to the
addresses the SSRF guard CHECKED, never to a fresh resolution of the name.

Without pinning the guard is resolve-then-check, and the SDK's HTTP client
resolves the name AGAIN at connect time: a DNS answer that changes between the
two (rebinding) reaches whatever the second answer says. The seam now hands the
SDK an HTTP client whose transport connects to the vetted address while the
URL, the Host header and TLS (SNI + certificate) keep the original name, and
which vets every other host a request goes to (redirects included). It ignores
environment proxies: a proxy resolves the name itself, which would void the pin.

Hermetic: the resolver seam (``socket.getaddrinfo``) is faked for every name;
to let a LOOPBACK test server stand in for "a public address", the canonical
classifier is told that 127.0.0.1 is public -- the test is about WHICH address
is dialled, not about classification. Nothing touches the real network: the
"rebound" second answer is 127.0.0.2 (loopback; nothing listens there).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

NAME = "pinned.example.test"
INTERNAL_NAME = "internal.example.test"


class _RebindingDns:
    """First answer for NAME is 127.0.0.1 (what the guard checks); any LATER
    lookup of NAME answers 127.0.0.2 (what a rebinding DNS would say at connect
    time). Numeric literals resolve locally; unknown names fail."""

    def __init__(self):
        self.calls = []
        self._real = socket.getaddrinfo

    def __call__(self, host, *a, **k):
        # anyio hands the resolver IDNA BYTES; the guard hands it str.
        name = (host.decode("idna") if isinstance(host, bytes) else str(host)).lower()
        self.calls.append(name)
        if name == NAME:
            ip = "127.0.0.1" if self.calls.count(NAME) == 1 else "127.0.0.2"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
        if name == INTERNAL_NAME:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        try:
            ipaddress.ip_address(name)
            return self._real(host, *a, **k)
        except ValueError:
            raise socket.gaierror(11001, "getaddrinfo failed")


@pytest.fixture
def dns(monkeypatch):
    fake = _RebindingDns()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    for var in ("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    return fake


@pytest.fixture
def loopback_counts_as_public(monkeypatch):
    """Classification seam: 127.0.0.1 stands in for a public address."""
    from systemu.runtime import net_safety
    real = net_safety.ip_is_non_global

    def _classify(ip):
        if str(ip) == "127.0.0.1":
            return False
        return real(ip)

    monkeypatch.setattr(net_safety, "ip_is_non_global", _classify)


@contextmanager
def _recording_server(status=503, location=None):
    seen = []

    class _H(BaseHTTPRequestHandler):
        def _record(self):
            seen.append({"path": self.path, "host": self.headers.get("Host")})
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_DELETE = _record

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv.server_address[1], seen
    finally:
        srv.shutdown()
        srv.server_close()


def _sdk_http_library():
    from systemu.runtime.mcp.sdk import compat
    return compat.sdk_http_library()


# ---------------------------------------------------------------------------
# End to end through the manager, on the installed SDK line.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("transport,path", [("http", "/mcp"), ("sse", "/sse")])
class TestSessionsDialTheCheckedAddress:
    def test_list_tools_resolves_once_and_keeps_the_name(
            self, dns, loopback_counts_as_public, transport, path):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (port, seen):
            url = f"http://{NAME}:{port}{path}"
            listing = asyncio.run(ConnectionManager(default_timeout=10.0).list_tools(
                "pinned", {"transport": transport, "url": url}))
        assert listing.ok is False               # 503: not an MCP server
        assert seen, f"the vetted address was never dialled: {listing.reason}"
        assert dns.calls.count(NAME) == 1, (
            f"the name was resolved again after the check: {dns.calls}")
        assert {s["host"] for s in seen} == {f"{NAME}:{port}"}, seen

    def test_connect_and_discover_is_pinned_too(
            self, dns, loopback_counts_as_public, transport, path):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (port, seen):
            out = ConnectionManager(default_timeout=10.0).connect_and_discover_sync(
                "pinned", {"transport": transport, "url": f"http://{NAME}:{port}{path}"})
        assert out["connected"] is False
        assert seen, out
        assert dns.calls.count(NAME) == 1, dns.calls

    def test_call_tool_is_pinned_too(
            self, dns, loopback_counts_as_public, transport, path):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (port, seen):
            out = asyncio.run(ConnectionManager(default_timeout=10.0).call_tool(
                "pinned", {"transport": transport, "url": f"http://{NAME}:{port}{path}"},
                "echo", {"text": "x"}))
        assert out["success"] is False           # 503: not an MCP server
        assert seen, f"the vetted address was never dialled: {out}"
        assert dns.calls.count(NAME) == 1, dns.calls
        assert {s["host"] for s in seen} == {f"{NAME}:{port}"}, seen

    def test_an_environment_proxy_cannot_take_the_connection(
            self, dns, loopback_counts_as_public, monkeypatch, transport, path):
        """A proxy resolves the name itself, voiding the pin: env proxies are
        not honoured for a pinned MCP session."""
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (proxy_port, proxy_seen), \
                _recording_server() as (port, seen):
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
            asyncio.run(ConnectionManager(default_timeout=10.0).list_tools(
                "pinned", {"transport": transport, "url": f"http://{NAME}:{port}{path}"}))
        assert proxy_seen == [], proxy_seen
        assert seen


# ---------------------------------------------------------------------------
# The pinned transport itself (the SDK's own HTTP library on this line).
# ---------------------------------------------------------------------------

def _routes_for(url, allowed=()):
    from systemu.runtime.mcp.sdk.egress import PinnedRoutes, admit_url
    verdict = admit_url(url, allowed_hosts=allowed)
    assert verdict.admitted is True, verdict
    return PinnedRoutes(verdict)


class TestPinnedTransport:
    def test_https_keeps_name_for_host_header_and_tls(self, dns, monkeypatch):
        from systemu.runtime import net_safety
        from systemu.runtime.mcp.sdk import compat
        lib = _sdk_http_library()
        monkeypatch.setattr(net_safety, "ip_is_non_global",
                            lambda ip: False if str(ip) == "127.0.0.1" else True)
        captured = []

        def handler(request):
            captured.append(request)
            return lib.Response(200, text="ok")

        routes = _routes_for(f"https://{NAME}:8443/mcp")
        transport = compat.make_pinned_transport(lib, routes, inner=lib.MockTransport(handler))

        async def go():
            async with lib.AsyncClient(transport=transport) as client:
                return await client.post(f"https://{NAME}:8443/mcp", content=b"{}")

        resp = asyncio.run(go())
        assert resp.status_code == 200
        req = captured[0]
        assert req.url.host == "127.0.0.1"                    # dialled: the vetted address
        assert req.headers["host"] == f"{NAME}:8443"         # vhost: the name
        assert req.extensions.get("sni_hostname") == NAME    # TLS SNI + cert check: the name
        assert str(resp.request.url) == f"https://{NAME}:8443/mcp"   # caller still sees the name

    def test_a_request_to_another_unvetted_host_is_refused(self, dns, monkeypatch):
        from systemu.runtime.mcp.sdk import compat
        lib = _sdk_http_library()
        hit = []
        routes = _routes_for("http://127.0.0.1:1/mcp", allowed={"127.0.0.1:1"})
        transport = compat.make_pinned_transport(
            lib, routes, inner=lib.MockTransport(lambda r: (hit.append(r), lib.Response(200))[1]))

        async def go(url):
            async with lib.AsyncClient(transport=transport) as client:
                return await client.get(url)

        for url in ("http://169.254.169.254/latest/meta-data", f"http://{INTERNAL_NAME}/",
                    "http://127.0.0.1:2/", "http://2130706433/"):
            with pytest.raises(lib.ConnectError) as err:
                asyncio.run(go(url))
            assert "SSRF" in str(err.value), err.value
        assert hit == []

    @pytest.mark.parametrize("target", ["loopback-other-port", "name-resolving-internal"])
    def test_a_followed_redirect_to_a_private_address_is_refused(self, dns, target):
        """Some SDK lines build their client with follow_redirects=True (mcp
        1.28 does; 1.30 / 2.3 follow only within the origin). Whatever the line,
        every hop goes through the pinned transport, so the redirect target is
        vetted like any other request."""
        from systemu.runtime.mcp.sdk import compat
        lib = _sdk_http_library()
        with _recording_server() as (port2, seen2):
            dest = (f"http://127.0.0.1:{port2}/x" if target == "loopback-other-port"
                    else f"http://{INTERNAL_NAME}:{port2}/x")
            with _recording_server(status=307, location=dest) as (port1, seen1):
                routes = _routes_for(f"http://127.0.0.1:{port1}/mcp",
                                     allowed={f"127.0.0.1:{port1}"})
                transport = compat.make_pinned_transport(lib, routes)

                async def go():
                    async with lib.AsyncClient(transport=transport,
                                               follow_redirects=True) as client:
                        return await client.get(f"http://127.0.0.1:{port1}/mcp")

                with pytest.raises(lib.ConnectError) as err:
                    asyncio.run(go())
        assert seen1, "the allowed first hop must be reached"
        assert seen2 == [], "the redirect target must never be reached"
        assert "SSRF" in str(err.value)

    def test_a_redirect_through_the_manager_never_reaches_the_target(self, dns, monkeypatch):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (port2, seen2):
            with _recording_server(status=307,
                                   location=f"http://127.0.0.1:{port2}/mcp") as (port1, seen1):
                monkeypatch.setenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", f"127.0.0.1:{port1}")
                listing = asyncio.run(ConnectionManager(default_timeout=10.0).list_tools(
                    "redir", {"transport": "http", "url": f"http://127.0.0.1:{port1}/mcp"}))
        assert listing.ok is False
        assert seen1
        assert seen2 == []

    def test_routes_resolve_each_other_host_once(self, dns, monkeypatch):
        from systemu.runtime import net_safety
        monkeypatch.setattr(net_safety, "ip_is_non_global",
                            lambda ip: False if str(ip) == "127.0.0.1" else True)
        routes = _routes_for("http://127.0.0.1:1/mcp", allowed={"127.0.0.1:1"})
        assert routes.route("http", NAME, 80) == ("127.0.0.1",)
        assert routes.route("http", NAME, 80) == ("127.0.0.1",)   # cached: no 2nd lookup
        assert dns.calls.count(NAME) == 1
        with pytest.raises(PermissionError):
            routes.route("ftp", NAME, 21)

    def test_routes_need_an_admitting_verdict(self, dns):
        from systemu.runtime.mcp.sdk.egress import PinnedRoutes, admit_url
        with pytest.raises(PermissionError):
            PinnedRoutes(admit_url("http://169.254.169.254/"))
        with pytest.raises(PermissionError):
            PinnedRoutes(object())


class TestEveryRemoteSessionIsPinned:
    def test_open_session_requires_routes_for_the_pinned_seam(self):
        """The seam builds the pinned client from the verdict it was handed;
        ``transports.open_session`` passes ``pin=`` for both remote kinds."""
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "systemu" / "runtime" / "mcp"
               / "sdk" / "transports.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("open_streamable_http", "open_sse"):
                calls.setdefault(node.func.attr, []).append(
                    {kw.arg for kw in node.keywords})
        assert set(calls) == {"open_streamable_http", "open_sse"}, calls
        for name, kwsets in calls.items():
            for kws in kwsets:
                assert "pin" in kws, f"{name} is called without pin=: {kws}"
