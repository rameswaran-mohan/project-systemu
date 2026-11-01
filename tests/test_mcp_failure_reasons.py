"""Defect 2 -- an MCP failure names its innermost meaningful cause.

Witnessed defect: an unreachable server surfaced as "unhandled errors in a
TaskGroup (1 sub-exception)" -- the str() of the anyio exception group, which
names nothing the operator can act on.

Ruling: unwrap exception groups at the manager boundary and report the
innermost meaningful cause:
  * connection refused  -> the host and port;
  * DNS failure         -> the name;
  * process exit        -> the exit code and the last stderr lines, secrets
                           redacted BEFORE any truncation (DEC-31).
Verdict text is ASCII-only.

Hermetic: a closed loopback port, faked DNS at the resolver seam
(``socket.getaddrinfo``), stdio children spawned from this interpreter. Remote
specs carry an explicit operator allowance for the loopback port so the SSRF
guard admits exactly that host:port and nothing else.
"""
from __future__ import annotations

import socket
import sys

import pytest

_REFUSED_HOST = "127.0.0.1"
_UNRESOLVABLE = "mcp-server.unresolvable.test"


def _closed_port() -> int:
    s = socket.socket()
    s.bind((_REFUSED_HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _allow(monkeypatch, entry: str) -> None:
    """The operator's explicit MCP allowance (HarnessPolicy.allowed_mcp_hosts)."""
    monkeypatch.setenv("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", entry)


def _fake_dns(monkeypatch, *, fail: str) -> None:
    real = socket.getaddrinfo

    def _gai(host, *a, **k):
        name = host.decode("idna") if isinstance(host, bytes) else str(host)
        if name.lower() == fail:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _gai)


def _stdio(code: str, env=None):
    return {"transport": "stdio", "command": sys.executable, "args": ["-c", code],
            "env": dict(env or {})}


def _assert_no_taskgroup(text: str) -> None:
    assert "TaskGroup" not in text, text
    assert "sub-exception" not in text, text
    assert text.isascii(), text


# ---------------------------------------------------------------------------
# Real connects.
# ---------------------------------------------------------------------------

class TestConnectionRefused:
    def test_list_names_refused_host_and_port(self, monkeypatch):
        from systemu.runtime.mcp import client as mc
        port = _closed_port()
        _allow(monkeypatch, f"{_REFUSED_HOST}:{port}")
        out = mc.mcp_list_tools(server=f"http://{_REFUSED_HOST}:{port}/mcp")
        assert out["success"] is False, out
        _assert_no_taskgroup(out["error"])
        assert f"connection refused by {_REFUSED_HOST}:{port}" in out["error"], out

    def test_call_names_refused_host_and_port(self, monkeypatch):
        from systemu.runtime.mcp import client as mc
        port = _closed_port()
        _allow(monkeypatch, f"{_REFUSED_HOST}:{port}")
        out = mc.mcp_call_tool(server=f"http://{_REFUSED_HOST}:{port}/mcp",
                               name="echo", params={}, config=None)
        assert out["success"] is False, out
        assert out["error"].startswith("connection/call failed:"), out
        _assert_no_taskgroup(out["error"])
        assert f"connection refused by {_REFUSED_HOST}:{port}" in out["error"], out

    @pytest.mark.asyncio
    async def test_sse_names_refused_host_and_port(self, monkeypatch):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        port = _closed_port()
        _allow(monkeypatch, f"{_REFUSED_HOST}:{port}")
        listing = await ConnectionManager(default_timeout=15.0).list_tools(
            "sse-srv", {"transport": "sse", "url": f"http://{_REFUSED_HOST}:{port}/sse"})
        assert listing.ok is False
        _assert_no_taskgroup(listing.reason)
        assert f"connection refused by {_REFUSED_HOST}:{port}" in listing.reason


class TestDnsFailure:
    def test_list_names_the_unresolvable_host(self, monkeypatch):
        from systemu.runtime.mcp import client as mc
        _fake_dns(monkeypatch, fail=_UNRESOLVABLE)
        out = mc.mcp_list_tools(server=f"https://{_UNRESOLVABLE}/mcp")
        assert out["success"] is False, out
        _assert_no_taskgroup(out["error"])
        assert "DNS lookup failed" in out["error"], out
        assert _UNRESOLVABLE in out["error"], out


@pytest.mark.asyncio
class TestProcessExit:
    async def test_exit_code_and_last_stderr_lines(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        code = ("import sys\n"
                "for i in range(12): sys.stderr.write(f'boot line {i}\\n')\n"
                "sys.stderr.write('fatal: config file missing\\n')\n"
                "sys.exit(3)\n")
        listing = await ConnectionManager(default_timeout=20.0).list_tools(
            "stdio:dies", _stdio(code))
        assert listing.ok is False
        reason = listing.reason
        _assert_no_taskgroup(reason)
        assert "exited with code 3" in reason, reason
        assert "fatal: config file missing" in reason, reason
        # only the LAST lines -- the early boot noise is not dumped wholesale
        assert "boot line 0" not in reason, reason

    async def test_call_tool_reports_the_exit_too(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        out = await ConnectionManager(default_timeout=20.0).call_tool(
            "stdio:dies", _stdio("import sys; sys.stderr.write('nope\\n'); sys.exit(7)"),
            "echo", {})
        assert out["success"] is False
        assert out["error"].startswith("connection/call failed:"), out
        assert "exited with code 7" in out["error"], out
        _assert_no_taskgroup(out["error"])

    async def test_command_not_found(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        listing = await ConnectionManager(default_timeout=20.0).list_tools(
            "stdio:missing", {"transport": "stdio", "command": "no_such_mcp_server_cmd_q7",
                              "args": [], "env": {}})
        assert listing.ok is False
        _assert_no_taskgroup(listing.reason)
        assert "could not start the server process" in listing.reason, listing.reason
        assert "no_such_mcp_server_cmd_q7" in listing.reason


# ---------------------------------------------------------------------------
# DEC-31: redact BEFORE truncation. Each secret is LONGER than the per-line cap,
# and the shape-rule secret is longer than the whole stderr read window too.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStderrRedaction:
    async def test_secrets_in_stderr_never_reach_the_reason(self):
        from systemu.runtime.mcp.sdk import failure
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        line_cap = failure.STDERR_LINE_CAP
        window = failure.STDERR_WINDOW_BYTES
        # (1) a shapeless secret the child received in its env: only the
        #     known-value fence can catch it (no shape rule knows it).
        env_secret = ("Qv7" + "mZ" * (line_cap)) [: line_cap + 60]
        # (2) a shape-rule secret on a key=value line, longer than the line cap.
        kv_secret = "Rk" * (line_cap // 2 + 40)
        # (3) a shape-rule token longer than the WHOLE read window.
        huge_token = "sk-" + "A" * (window + 4096)
        # (4) a JWT longer than the line cap: its shape needs all three
        #     segments, so a cap applied first would leave a clear fragment.
        jwt = "eyJ" + "J" * (line_cap + 50) + ".eyJ" + "p" * 40 + "." + "s" * 40
        assert len(env_secret) > line_cap and len(kv_secret) > line_cap
        assert len(jwt) > line_cap and len(huge_token) > window
        code = (
            "import os, sys\n"
            "w = sys.stderr.write\n"
            f"w('token=' + {huge_token!r} + '\\n')\n"
            "w('starting\\n')\n"
            "w('env says ' + os.environ['MCP_TEST_SECRET'] + '\\n')\n"
            f"w('api_key=' + {kv_secret!r} + '\\n')\n"
            f"w('sent header ' + {jwt!r} + '\\n')\n"
            "w('giving up\\n')\n"
            "sys.exit(5)\n")
        listing = await ConnectionManager(default_timeout=20.0).list_tools(
            "stdio:leaky", _stdio(code, env={"MCP_TEST_SECRET": env_secret}))
        assert listing.ok is False
        reason = listing.reason
        assert "exited with code 5" in reason, reason
        assert "giving up" in reason, reason
        for name, secret in (("env", env_secret), ("kv", kv_secret),
                             ("huge", huge_token[3:]), ("jwt", jwt)):
            for i in range(0, len(secret) - 16, 7):
                chunk = secret[i:i + 16]
                assert chunk not in reason, f"{name} secret fragment leaked: {chunk!r}"
        assert reason.isascii()


class TestStderrTail:
    """The tail builder on its own (synchronous)."""

    def test_tail_is_redacted_before_it_is_capped(self):
        """Unit pin on the tail builder: secrets longer than the line cap are
        masked whole -- the cap applies to the MASKED line. The JWT is the
        load-bearing case: its shape needs all three segments, so capping first
        would cut it into a clear fragment the matcher no longer recognises."""
        from systemu.runtime.mcp.sdk import failure
        cap = failure.STDERR_LINE_CAP
        kv_secret = "x9" * cap
        jwt = "eyJ" + "h" * (cap + 50) + ".eyJ" + "p" * 40 + "." + "s" * 40
        assert len(jwt) > cap and len(kv_secret) > cap
        raw = ("ok\n" + "password=" + kv_secret + "\n"
               + "auth header was " + jwt + "\n").encode("utf-8")
        lines = failure.stderr_tail(raw, truncated=False, known_values=())
        joined = " | ".join(lines)
        assert kv_secret[:16] not in joined, joined
        assert "h" * 16 not in joined, joined
        assert all(len(line) <= cap for line in lines)

    def test_a_cut_first_line_is_dropped_not_shown(self):
        from systemu.runtime.mcp.sdk import failure
        raw = ("Zm9vYmFyYmF6cXV4" * 10 + "\nlast words\n").encode("utf-8")
        lines = failure.stderr_tail(raw, truncated=True, known_values=())
        assert lines == ["last words"], lines


# ---------------------------------------------------------------------------
# The unwrap itself, on exception trees shaped exactly like the SDK's.
# ---------------------------------------------------------------------------

class TestUnwrap:
    def _refused_tree(self):
        refused = ConnectionRefusedError(10061, "refused")
        inner = OSError("All connection attempts failed")
        inner.__cause__ = refused
        try:
            try:
                raise inner
            except OSError:
                raise ConnectionError("All connection attempts failed") from None
        except ConnectionError as wrapped:
            leaf = wrapped
        return ExceptionGroup("unhandled errors in a TaskGroup",
                              [ExceptionGroup("unhandled errors in a TaskGroup", [leaf])])

    def test_nested_groups_unwrap_to_refused(self):
        from systemu.runtime.mcp.sdk import failure
        reason = failure.describe_failure(
            self._refused_tree(), {"transport": "http", "url": "http://10.9.8.7:4444/mcp"})
        assert reason == "connection refused by 10.9.8.7:4444", reason

    def test_default_port_is_named(self):
        from systemu.runtime.mcp.sdk import failure
        reason = failure.describe_failure(
            self._refused_tree(), {"transport": "http", "url": "https://h.example/mcp"})
        assert reason == "connection refused by h.example:443", reason

    def test_dns_failure_names_the_host(self):
        from systemu.runtime.mcp.sdk import failure
        exc = ExceptionGroup("unhandled errors in a TaskGroup",
                             [socket.gaierror(11001, "getaddrinfo failed")])
        reason = failure.describe_failure(
            exc, {"transport": "sse", "url": "http://nowhere.test:8080/sse"})
        assert reason == "DNS lookup failed for 'nowhere.test'", reason

    def test_unknown_leaf_is_named_not_the_group(self):
        from systemu.runtime.mcp.sdk import failure
        exc = ExceptionGroup("unhandled errors in a TaskGroup",
                             [ValueError("schema mismatch on initialize")])
        reason = failure.describe_failure(exc, {"transport": "stdio", "command": "x"})
        assert "ValueError: schema mismatch on initialize" in reason, reason
        _assert_no_taskgroup(reason)

    def test_self_referential_chain_terminates(self):
        from systemu.runtime.mcp.sdk import failure
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__context__ = b
        b.__context__ = a
        reason = failure.describe_failure(a, {"transport": "stdio", "command": "x"})
        assert "RuntimeError" in reason


# ---------------------------------------------------------------------------
# An INCIDENTAL exception context is never the reason (found on mcp 2.3: the
# sync runner's "is a loop running?" probe leaked RuntimeError("no running
# event loop") as the __context__ of every failure, and became the reason).
# ---------------------------------------------------------------------------

class TestIncidentalContextIsNotTheReason:
    def test_the_sync_runner_leaks_no_context(self):
        from systemu.runtime.mcp.client import _run_async

        async def _boom():
            raise ValueError("the real failure")

        with pytest.raises(ValueError) as err:
            _run_async(_boom())
        assert err.value.__context__ is None, repr(err.value.__context__)

    def test_an_unsuppressed_context_is_not_the_leaf(self):
        from systemu.runtime.mcp.sdk import failure
        try:
            try:
                raise RuntimeError("no running event loop")
            except RuntimeError:
                raise LookupError("Server returned an error response")
        except LookupError as real:
            exc = ExceptionGroup("unhandled errors in a TaskGroup", [real])
        reason = failure.describe_failure(exc, {"transport": "http",
                                                "url": "http://h.example.test/mcp"})
        assert reason == "LookupError: Server returned an error response", reason
