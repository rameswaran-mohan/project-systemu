"""R-A11b-1 -- ONE resolve-then-check SSRF guard for every MCP connect and every
url_safety check.

Witnessed defects:
  * ``sdk/manager._ssrf_precheck`` was a weaker private copy of the canonical
    guard (no embedded-IPv4 check: a name resolving to the 6to4 form of the
    metadata address 2002:a9fe:a9fe:: was ADMITTED);
  * ``remote_policy.mcp_host_allowed`` and ``tool_hygiene.url_safety.is_url_safe``
    admitted ANY hostname without resolving it;
  * the Settings "Discover tools" path (discover_and_pin -> mcp_list_tools ->
    ConnectionManager.list_tools) had no SSRF check at all.

Ruling: everything delegates to ``runtime/net_safety`` (resolve once, check every
resolved address); the operator's explicit allowance (``allowed_mcp_hosts``)
is checked by host:port (a bare host keeps matching any port); the server
addresses saved in ``<vault>/connections/mcp.json`` are NOT an allowance (the
file has model-reachable writers -- DEC-34).

Hermetic: DNS is faked at the resolver seam (``socket.getaddrinfo``) for every
name; numeric literals and ``localhost`` resolve locally; connects only ever go
to loopback servers this file starts. Nothing touches the real network.
"""
from __future__ import annotations

import ast
import asyncio
import ipaddress
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SYSTEMU = _ROOT / "systemu"

PUBLIC_IP = "93.184.216.34"          # a public documentation-style address (faked DNS)
PUBLIC_NAME = "mcp.public-example.test"
PRIVATE_NAME = "intranet.example.test"
SIXTOFOUR_IMDS_NAME = "rebind-6to4.example.test"


# ---------------------------------------------------------------------------
# Fixtures: the resolver seam, an open-session spy, a recording loopback server.
# ---------------------------------------------------------------------------

class _Dns:
    """``socket.getaddrinfo`` replacement. Table names -> faked addresses;
    numeric literals and ``localhost`` -> the real (local, no network) resolver;
    anything else -> gaierror. Counts every lookup by name."""

    def __init__(self, table):
        self.table = {k.lower(): list(v) for k, v in table.items()}
        self.calls = []
        self._real = socket.getaddrinfo

    def __call__(self, host, *a, **k):
        # anyio hands the resolver IDNA BYTES; the guard hands it str.
        name = (host.decode("idna") if isinstance(host, bytes) else str(host)).lower()
        self.calls.append(name)
        if name in self.table:
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
                     6, "", (ip, 0)) for ip in self.table[name]]
        try:
            ipaddress.ip_address(name)
            return self._real(host, *a, **k)
        except ValueError:
            pass
        if name == "localhost":
            return self._real(host, *a, **k)
        raise socket.gaierror(11001, "getaddrinfo failed")


@pytest.fixture
def dns(monkeypatch):
    fake = _Dns({
        PUBLIC_NAME: [PUBLIC_IP],
        PRIVATE_NAME: ["10.0.0.5"],
        SIXTOFOUR_IMDS_NAME: ["2002:a9fe:a9fe::"],
        "mixed.example.test": [PUBLIC_IP, "10.0.0.5"],
        "two.example.test": [PUBLIC_IP, "93.184.216.35"],
    })
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.delenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", raising=False)
    return fake


@pytest.fixture
def open_spy(monkeypatch):
    """Replace the ONE session opener; record every attempt, connect nothing."""
    from systemu.runtime.mcp.sdk import transports
    seen = []

    def _spy(spec, **kwargs):
        seen.append({"spec": dict(spec), **kwargs})
        raise ConnectionRefusedError(10061, "spy: no connect")

    monkeypatch.setattr(transports, "open_session", _spy)
    return seen


@contextmanager
def _recording_server():
    hits = []

    class _H(BaseHTTPRequestHandler):
        def _record(self):
            hits.append(self.path)
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_DELETE = _record

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv.server_address[1], hits
    finally:
        srv.shutdown()
        srv.server_close()


class _Vault:
    def __init__(self, root):
        self.root = str(root)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A. net_safety.vet_host -- the canonical primitive.
# ---------------------------------------------------------------------------

class TestVetHost:
    def test_public_literal_admitted_without_dns(self, dns):
        from systemu.runtime import net_safety
        v = net_safety.vet_host(PUBLIC_IP)
        assert v.admitted is True and v.addresses == (PUBLIC_IP,)
        assert dns.calls == []

    @pytest.mark.parametrize("literal", ["169.254.169.254", "127.0.0.1", "::1", "10.0.0.5",
                                         "fd00:ec2::254", "::ffff:169.254.169.254",
                                         "2002:a9fe:a9fe::", "64:ff9b::a9fe:a9fe"])
    def test_non_public_literal_refused_without_dns(self, dns, literal):
        from systemu.runtime import net_safety
        v = net_safety.vet_host(literal)
        assert v.admitted is False and v.addresses == ()
        assert dns.calls == []

    @pytest.mark.parametrize("spelling", ["2130706433", "0177.0.0.1", "0x7f.0.0.1", "127.1",
                                          "0x7f000001", "017700000001", "0251.0376.0251.0376"])
    def test_non_canonical_numeric_spelling_refused_without_any_resolver(self, dns, spelling):
        """Decimal / octal / hex / short IPv4 spellings: every OS resolver maps
        them somewhere (on this platform all of the 127-ones are 127.0.0.1), so
        they are refused structurally -- the resolver is never asked."""
        from systemu.runtime import net_safety
        v = net_safety.vet_host(spelling)
        assert v.admitted is False, v
        assert "non-canonical" in v.reason
        assert dns.calls == []

    def test_name_resolving_private_refused(self, dns):
        from systemu.runtime import net_safety
        v = net_safety.vet_host(PRIVATE_NAME)
        assert v.admitted is False and "10.0.0.5" in v.reason

    def test_name_resolving_to_6to4_metadata_refused(self, dns):
        from systemu.runtime import net_safety
        assert net_safety.vet_host(SIXTOFOUR_IMDS_NAME).admitted is False

    def test_mixed_set_refused_whole(self, dns):
        from systemu.runtime import net_safety
        assert net_safety.vet_host("mixed.example.test").admitted is False

    def test_every_checked_address_is_returned_and_resolved_once(self, dns):
        from systemu.runtime import net_safety
        v = net_safety.vet_host("two.example.test")
        assert v.admitted is True
        assert v.addresses == (PUBLIC_IP, "93.184.216.35")
        assert dns.calls == ["two.example.test"]

    def test_dns_failure_names_the_host(self, dns):
        from systemu.runtime import net_safety
        v = net_safety.vet_host("nowhere.example.test")
        assert v.admitted is False
        assert "DNS lookup failed" in v.reason and "nowhere.example.test" in v.reason

    def test_resolve_pinned_ip_is_vet_host(self, dns):
        from systemu.runtime import net_safety
        assert net_safety.resolve_pinned_ip("two.example.test") == PUBLIC_IP
        assert net_safety.resolve_pinned_ip(PRIVATE_NAME) is None
        assert net_safety.resolve_pinned_ip("2130706433") is None


# ---------------------------------------------------------------------------
# B. The MCP verdict and the operator's allowance.
# ---------------------------------------------------------------------------

class TestAdmitUrl:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8080/mcp",
        "http://[::1]:8080/mcp",
        f"http://{PRIVATE_NAME}/mcp",
        f"https://{SIXTOFOUR_IMDS_NAME}/mcp",
        "http://2130706433/mcp",
        "http://0177.0.0.1:8080/mcp",
        "http://[fd00:ec2::254]/mcp",
        "http://[::ffff:169.254.169.254]/mcp",
        "file:///etc/passwd",
        "gopher://127.0.0.1/",
        "http:///no-host",
        "http://127.0.0.1:notaport/mcp",
    ])
    def test_refused(self, dns, url):
        from systemu.runtime.mcp.sdk.egress import admit_url
        v = admit_url(url)
        assert v.admitted is False, v
        assert v.via == "refused" and v.reason

    def test_public_name_admitted_with_its_checked_addresses(self, dns):
        from systemu.runtime.mcp.sdk.egress import admit_url
        v = admit_url(f"https://{PUBLIC_NAME}/mcp")
        assert v.admitted is True and v.via == "public"
        assert v.addresses == (PUBLIC_IP,)
        assert (v.host, v.port) == (PUBLIC_NAME, 443)

    def test_allowance_is_checked_by_host_and_port(self, dns):
        from systemu.runtime.mcp.sdk.egress import admit_url
        assert admit_url("http://127.0.0.1:8080/mcp",
                         allowed_hosts={"127.0.0.1:8080"}).via == "allowance"
        assert admit_url("http://127.0.0.1:9090/mcp",
                         allowed_hosts={"127.0.0.1:8080"}).admitted is False
        assert admit_url("http://[::1]:8080/mcp",
                         allowed_hosts={"[::1]:8080"}).via == "allowance"
        assert admit_url("https://box.local.test/mcp",
                         allowed_hosts={"box.local.test:443"}).via == "allowance"
        assert admit_url("http://box.local.test/mcp",
                         allowed_hosts={"box.local.test:443"}).admitted is False

    def test_allowance_matches_the_name_not_an_address(self, dns):
        from systemu.runtime.mcp.sdk.egress import admit_url
        assert admit_url("http://localhost:8080/mcp",
                         allowed_hosts={"localhost:8080"}).admitted is True
        assert admit_url("http://127.0.0.1:8080/mcp",
                         allowed_hosts={"localhost:8080"}).admitted is False

    def test_a_bare_host_entry_keeps_matching_any_port(self, dns):
        """Existing operator configs ("127.0.0.1", "localhost") must not
        silently start refusing."""
        from systemu.runtime.mcp.sdk.egress import admit_url
        for url in ("http://127.0.0.1:1/mcp", "http://127.0.0.1:65000/mcp"):
            assert admit_url(url, allowed_hosts={"127.0.0.1"}).via == "allowance"
        assert admit_url("http://[::1]:5/mcp", allowed_hosts={"::1"}).via == "allowance"

    def test_malformed_entries_only_narrow(self, dns):
        from systemu.runtime.mcp.sdk.egress import admit_url, parse_allowance
        bad = {"127.0.0.1:notaport", "http://127.0.0.1/", "127.0.0.1:8080/path", "", "  "}
        assert parse_allowance(bad) == ()
        assert admit_url("http://127.0.0.1:8080/mcp", allowed_hosts=bad).admitted is False

    def test_a_verdict_cannot_be_constructed_outside_the_guard(self):
        from systemu.runtime.mcp.sdk.egress import EgressVerdict
        with pytest.raises(TypeError):
            EgressVerdict(url="http://x/", admitted=True, via="public", scheme="http",
                          host="x", port=80, addresses=("1.2.3.4",), reason="")


# ---------------------------------------------------------------------------
# C. Delegation: url_safety, remote_policy, the manager's precheck, readback.
# ---------------------------------------------------------------------------

class TestDelegatesToNetSafety:
    def test_url_safety_resolves_the_name(self, dns):
        from systemu.runtime.tool_hygiene.url_safety import is_url_safe
        assert is_url_safe(f"https://{PUBLIC_NAME}/x") is True
        assert is_url_safe(f"https://{PRIVATE_NAME}/x") is False
        assert is_url_safe(f"https://{SIXTOFOUR_IMDS_NAME}/x") is False
        assert is_url_safe("http://2130706433/") is False
        assert is_url_safe("https://unknown-name.example.test/") is False

    def test_remote_policy_resolves_the_name(self, dns):
        from systemu.runtime.mcp.sdk import remote_policy
        assert remote_policy.mcp_host_allowed(f"https://{PUBLIC_NAME}/mcp",
                                              allowed_hosts=set()) is True
        assert remote_policy.mcp_host_allowed(f"https://{PRIVATE_NAME}/mcp",
                                              allowed_hosts=set()) is False
        assert remote_policy.mcp_host_allowed("https://127.0.0.1:8443/mcp",
                                              allowed_hosts={"127.0.0.1:8443"}) is True

    def test_remote_policy_tls_accepts_a_host_port_allowance(self):
        from systemu.runtime.mcp.sdk import remote_policy
        remote_policy.enforce_tls("http://127.0.0.1:8080/mcp", allowed_hosts={"127.0.0.1:8080"})
        with pytest.raises(remote_policy.InsecureTransportError):
            remote_policy.enforce_tls("http://127.0.0.1:9090/mcp",
                                      allowed_hosts={"127.0.0.1:8080"})

    def test_manager_precheck_is_the_canonical_guard(self, dns):
        """The weak private copy admitted a name resolving to the 6to4 form of
        the metadata address (it never extracted embedded IPv4)."""
        from systemu.runtime.mcp.sdk.manager import _ssrf_precheck
        ok, why = _ssrf_precheck({"transport": "http",
                                  "url": f"https://{SIXTOFOUR_IMDS_NAME}/mcp"})
        assert ok is False, why
        ok, _ = _ssrf_precheck({"transport": "http", "url": "http://0177.0.0.1/"})
        assert ok is False

    def test_readback_pins_through_net_safety(self, monkeypatch, dns):
        import httpx
        from systemu.runtime import net_safety
        import systemu.runtime.readback_client as rc
        calls = []
        real = net_safety.resolve_pinned_ip

        def _spy(host):
            calls.append(host)
            return real(host)

        monkeypatch.setattr(net_safety, "resolve_pinned_ip", _spy)
        seen = []
        transport = httpx.MockTransport(
            lambda r: (seen.append(str(r.url.host)), httpx.Response(200, text="ok"))[1])
        out = rc.ProdReadbackClient(transport=transport).readback(f"https://{PUBLIC_NAME}/x")
        assert out.get("response_body") == "ok", out
        assert calls == [PUBLIC_NAME]
        assert seen == [PUBLIC_IP]
        assert dns.calls == [PUBLIC_NAME], "the readback path must resolve exactly once"

    def test_readback_no_longer_uses_the_check_only_helper(self):
        tree = ast.parse((_SYSTEMU / "runtime" / "readback_client.py").read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                  for a in n.names}
        assert "is_url_safe" not in names


# ---------------------------------------------------------------------------
# D. Every guarded connect path refuses BEFORE any session is opened.
# ---------------------------------------------------------------------------

_REFUSED_URLS = [
    "http://169.254.169.254/latest/meta-data",
    "http://127.0.0.1:8080/mcp",
    "http://[::1]:8080/mcp",
    f"http://{PRIVATE_NAME}/mcp",
    f"https://{SIXTOFOUR_IMDS_NAME}/mcp",
    "http://2130706433/mcp",
    "http://0177.0.0.1/mcp",
]


@pytest.mark.parametrize("url", _REFUSED_URLS)
class TestEveryConnectPathRefuses:
    def test_manager_list_tools(self, dns, open_spy, url):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        listing = _run(ConnectionManager().list_tools(url, {"transport": "http", "url": url}))
        assert listing.ok is False and "SSRF" in listing.reason, listing
        assert open_spy == []

    def test_manager_list_tools_over_sse(self, dns, open_spy, url):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        listing = _run(ConnectionManager().list_tools(url, {"transport": "sse", "url": url}))
        assert listing.ok is False and "SSRF" in listing.reason
        assert open_spy == []

    def test_settings_discover_path(self, dns, open_spy, url, tmp_path):
        from systemu.runtime.mcp import client as mc
        from systemu.runtime.mcp import connections as c
        v = _Vault(tmp_path)
        c.add_server(v, url)
        out = mc.discover_and_pin(v, url.rstrip("/"))
        assert out["success"] is False and "SSRF" in out["error"], out
        assert open_spy == []
        assert c.get_state(v)["hashes"] == {}

    def test_connect_and_discover(self, dns, open_spy, url):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        out = ConnectionManager().connect_and_discover_sync(
            "srv", {"transport": "http", "url": url})
        assert out["connected"] is False and "SSRF" in out["error"], out
        assert open_spy == []

    def test_connect_remote_sync(self, dns, open_spy, url):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with pytest.raises((PermissionError, Exception)) as err:
            ConnectionManager().connect_remote_sync(url, allowed_hosts=set(), require_tls=False)
        assert isinstance(err.value, PermissionError), err.value
        assert open_spy == []

    def test_manager_call_tool(self, dns, open_spy, url):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        for transport in ("http", "sse"):
            out = _run(ConnectionManager().call_tool(
                url, {"transport": transport, "url": url}, "echo", {}))
            assert out["success"] is False and "SSRF" in out["error"], out
            assert out["error"].startswith("connection/call failed:"), out
        assert open_spy == []

    def test_client_mcp_call_tool(self, dns, open_spy, url):
        from systemu.runtime.mcp import client as mc
        out = mc.mcp_call_tool(server=url, name="echo", params={}, config=None)
        assert out["success"] is False and "SSRF" in out["error"], out
        assert open_spy == []


class TestAdmittedPathsReachTheSeam:
    def test_public_name_reaches_the_session_opener(self, dns, open_spy):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        url = f"https://{PUBLIC_NAME}/mcp"
        listing = _run(ConnectionManager().list_tools(url, {"transport": "http", "url": url}))
        assert listing.ok is False            # the spy refuses the connect itself
        assert len(open_spy) == 1

    def test_allowance_by_host_and_port_against_a_real_loopback_server(self, dns, monkeypatch):
        """Non-vacuity: the exact allowance reaches the server; the same host on
        a different allowed port does not."""
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (port, hits):
            url = f"http://127.0.0.1:{port}/mcp"
            spec = {"transport": "http", "url": url}
            monkeypatch.setenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", f"127.0.0.1:{port + 1}")
            refused = _run(ConnectionManager(default_timeout=10.0).list_tools("loop", spec))
            assert refused.ok is False and "SSRF" in refused.reason
            assert hits == []
            monkeypatch.setenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", f"127.0.0.1:{port}")
            reached = _run(ConnectionManager(default_timeout=10.0).list_tools("loop", spec))
            assert reached.ok is False            # 503: not an MCP server
            assert "SSRF" not in reached.reason
            assert hits, "the allowed host:port must actually be reached"


@pytest.mark.asyncio
class TestTheSeamChecksTheVerdict:
    """transports.open_session refuses a remote session whose verdict does not
    admit THIS url -- before any SDK import or socket."""

    async def test_a_verdict_for_another_url_is_refused(self, dns):
        from systemu.runtime.mcp.sdk import transports
        from systemu.runtime.mcp.sdk.egress import admit_url
        verdict = admit_url(f"https://{PUBLIC_NAME}/mcp")
        assert verdict.admitted is True
        with pytest.raises(PermissionError):
            async with transports.open_session(
                    {"transport": "http", "url": f"https://{PUBLIC_NAME}/elsewhere"},
                    egress=verdict):
                pass

    async def test_a_refusing_verdict_is_refused(self, dns):
        from systemu.runtime.mcp.sdk import transports
        from systemu.runtime.mcp.sdk.egress import admit_url
        url = "http://169.254.169.254/mcp"
        with pytest.raises(PermissionError):
            async with transports.open_session({"transport": "http", "url": url},
                                               egress=admit_url(url)):
                pass

    async def test_something_that_only_looks_like_a_verdict_is_refused(self):
        from types import SimpleNamespace
        from systemu.runtime.mcp.sdk import transports
        url = "http://169.254.169.254/mcp"
        fake = SimpleNamespace(admitted=True, url=url, via="public", addresses=("1.1.1.1",))
        with pytest.raises(PermissionError):
            async with transports.open_session({"transport": "sse", "url": url}, egress=fake):
                pass

    @pytest.mark.parametrize("transport", ["http", "streamable-http", "sse"])
    async def test_a_remote_session_without_any_verdict_is_refused(self, dns, transport):
        """Every remote session needs a verdict: there is no unguarded default."""
        from systemu.runtime.mcp.sdk import transports
        with pytest.raises(PermissionError):
            async with transports.open_session(
                    {"transport": transport, "url": f"https://{PUBLIC_NAME}/mcp"}):
                pass


# ---------------------------------------------------------------------------
# E. The saved server address is NOT an allowance (DEC-34) -- and why.
# ---------------------------------------------------------------------------

# Every systemu/ call site of a writer of <vault>/connections/mcp.json.
_CONNECTIONS_WRITERS = {"add_server", "remove_server", "set_tool_enabled", "set_transport",
                        "set_server_meta", "set_tool_hash", "check_and_pin_hash"}
# Found 2026-10: the file's writer set. Two of them are MODEL-REACHABLE, which is
# why the saved address may never widen the SSRF guard:
#   runtime/governor.py      Governor._provision_mcp -- runs on an AGENT
#                            HarnessRequest(kind=mcp) whose url/server_id the
#                            agent supplies (operator-approved OR auto-granted on
#                            re-attach / allowed_mcp_servers), and persists the
#                            transport spec (set_transport) + pins + enables.
#   runtime/mcp/dispatch.py  call_mcp_tool's rug-pull re-pin (check_and_pin_hash),
#                            reached by every agent MCP tool call.
# Operator surfaces: interface/pages/settings.py (Add / Remove / enable switch),
# runtime/mcp/client.py discover_and_pin (the Settings "Discover tools" button).
# No CLI verb writes the file. A NEW writer changes this set and must be reviewed
# against DEC-34 before it lands.
_EXPECTED_WRITER_FILES = {
    "interface/pages/settings.py",
    "runtime/governor.py",
    "runtime/mcp/client.py",
    "runtime/mcp/dispatch.py",
}
_MODEL_REACHABLE_WRITER_FILES = {"runtime/governor.py", "runtime/mcp/dispatch.py"}


def _writer_call_sites():
    sites = {}
    for py in _SYSTEMU.rglob("*.py"):
        rel = py.relative_to(_SYSTEMU).as_posix()
        if rel == "runtime/mcp/connections.py":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            raise AssertionError(f"unparseable source in the writer scan: {rel}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in _CONNECTIONS_WRITERS:
                    sites.setdefault(rel, set()).add(name)
    return sites


class TestSavedAddressIsNotAnAllowance:
    def test_the_connections_file_writer_set_is_pinned(self):
        sites = _writer_call_sites()
        assert set(sites) == _EXPECTED_WRITER_FILES, sites
        assert _MODEL_REACHABLE_WRITER_FILES <= set(sites)
        assert "set_transport" in sites["runtime/governor.py"]
        assert "check_and_pin_hash" in sites["runtime/mcp/dispatch.py"]

    def test_the_guard_never_reads_the_connections_store(self):
        for rel in ("runtime/mcp/sdk/egress.py", "runtime/net_safety.py"):
            tree = ast.parse((_SYSTEMU / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    assert "connections" not in mod, (rel, mod)
                    assert all(a.name != "connections" for a in node.names), rel
                if isinstance(node, ast.Import):
                    assert all("connections" not in a.name for a in node.names), rel

    def test_a_saved_loopback_server_is_refused_and_never_reached(self, dns, tmp_path):
        from systemu.runtime.mcp import client as mc
        from systemu.runtime.mcp import connections as c
        with _recording_server() as (port, hits):
            server = f"http://127.0.0.1:{port}"
            v = _Vault(tmp_path)
            c.add_server(v, server, transport={"transport": "http", "url": server + "/mcp"})
            out = mc.discover_and_pin(v, server)
            assert out["success"] is False and "SSRF" in out["error"], out
            assert hits == []


# ---------------------------------------------------------------------------
# F. Reachability pin: every manager entry point that opens a session asks the
#    guard first (AST over the manager's methods).
# ---------------------------------------------------------------------------

# Methods that NEVER open a connection.
_NON_CONNECTING = {"set_elicitation_callback", "build_elicitation_callback",
                   "set_sampling_callback", "disconnect_all"}
# A connecting method satisfies the pin by calling the guard itself or by
# delegating to a method that does.
_GUARD_CALLS = {"_admit"}
# Committed violation baseline (DEC-32c). It held {"call_tool"} until the
# GATE-6 ruling that let call_tool be guarded; it is now EMPTY and may only
# stay empty -- both mismatch directions are fatal.
_PENDING_RULING = set()


def _manager_methods():
    tree = ast.parse((_SYSTEMU / "runtime" / "mcp" / "sdk" / "manager.py").read_text(
        encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "ConnectionManager")
    methods = {}
    for fn in cls.body:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    calls.add(node.func.attr)
            methods[fn.name] = calls
    return methods


class TestEveryEntryPointCallsTheGuard:
    def test_the_session_opener_is_reached_only_through_the_guard(self):
        methods = _manager_methods()
        openers = {name for name, calls in methods.items() if "_session" in calls}
        assert openers, "the manager must open sessions through self._session"

        def guarded(name, seen=()):
            calls = methods.get(name, set())
            if calls & _GUARD_CALLS:
                return True
            return any(guarded(c, seen + (name,)) for c in calls
                       if c in methods and c not in seen and c != "_session")

        unguarded = {m for m in openers if not guarded(m)}
        assert unguarded == _PENDING_RULING, unguarded

    def test_every_public_entry_point_is_classified(self):
        methods = _manager_methods()
        public = {m for m in methods if not m.startswith("_")}

        def connects(name, seen=()):
            calls = methods.get(name, set())
            return "_session" in calls or any(
                connects(c, seen + (name,)) for c in calls if c in methods and c not in seen)

        for m in sorted(public - _NON_CONNECTING):
            assert connects(m), f"{m} neither connects nor is declared non-connecting"
        for m in _NON_CONNECTING:
            assert not connects(m), f"{m} is declared non-connecting but opens a session"
