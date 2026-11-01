"""R-2 -- no upgrade regression for local MCP servers.

The SSRF guard now refuses loopback / private MCP endpoints unless the operator
allows them. An operator who already runs a local MCP server must not silently
lose it, so:

  (a) Settings asks before it saves a local/private server ("This is a local or
      private address. Allow systemu to connect to <host:port>?") and, on yes,
      writes EXACTLY that host:port into the allowance; a saved server that gets
      refused shows its reason beside an "Allow <host:port>" button that does
      the same write.
  (b) every writer of the allowance store is an operator surface (Settings, the
      CLI, or the environment) -- pinned by AST here; R-4 fenced the only
      model-reachable one (promote_to_env).
  (c) SYSTEMU_MCP_SERVER_URLS entries count as an allowance for exactly their
      host:port (operator-set at process start, same trust as the allowance).
  (d) the Settings placeholder no longer suggests an address that is refused.
  (e) the refusal reason names the Settings button as well as the entry.

Hermetic: the .env the Settings writer edits lives under tmp_path; loopback
servers only; DNS faked where a name is used.
"""
from __future__ import annotations

import ast
import asyncio
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import _env_scan

_ALLOW_ENV = "SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS"
_URLS_ENV = "SYSTEMU_MCP_SERVER_URLS"
_SETTINGS = _env_scan.ROOT / "systemu" / "interface" / "pages" / "settings.py"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # setenv FIRST so monkeypatch records the original state and undoes even a
    # later direct os.environ write (the Settings grant writes the live env);
    # a bare delenv of an absent name records nothing and would leak.
    for name in (_ALLOW_ENV, _URLS_ENV):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    from systemu.interface.pages import settings
    path = tmp_path / "home" / ".env"
    path.parent.mkdir()
    path.write_text("OTHER_SETTING=1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "_env_file_path", lambda: path)
    return path


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


# ---------------------------------------------------------------------------
# (e) the refusal names the Settings button and the exact entry
# ---------------------------------------------------------------------------

class TestTheRefusalTellsTheOperatorHow:
    def test_a_local_refusal_names_the_button_and_the_entry(self):
        from systemu.runtime.mcp.sdk.egress import admit_url
        v = admit_url("http://127.0.0.1:8080/mcp")
        assert v.admitted is False and v.kind == "non_public"
        assert v.allow_entry == "127.0.0.1:8080"
        assert '"Allow 127.0.0.1:8080"' in v.reason, v.reason
        assert "Settings > MCP servers" in v.reason, v.reason
        assert "'127.0.0.1:8080'" in v.reason and _ALLOW_ENV in v.reason, v.reason
        assert v.reason.isascii()

    def test_ipv6_and_default_ports_are_exact(self):
        from systemu.runtime.mcp.sdk.egress import admit_url
        assert admit_url("http://[::1]:9000/mcp").allow_entry == "[::1]:9000"
        assert admit_url("https://10.0.0.5/mcp").allow_entry == "10.0.0.5:443"

    def test_a_dns_failure_offers_no_allow_button(self, monkeypatch):
        from systemu.runtime.mcp.sdk.egress import admit_url

        def _gai(host, *a, **k):
            raise socket.gaierror(11001, "getaddrinfo failed")

        monkeypatch.setattr(socket, "getaddrinfo", _gai)
        v = admit_url("https://nowhere.example.test/mcp")
        assert v.admitted is False and v.kind == "dns" and v.allow_entry == ""


# ---------------------------------------------------------------------------
# (c) SYSTEMU_MCP_SERVER_URLS counts as an allowance for exactly host:port
# ---------------------------------------------------------------------------

class TestEnvDeclaredServersAreAllowed:
    def test_exactly_their_host_and_port(self, monkeypatch):
        from systemu.runtime.mcp.sdk import egress
        monkeypatch.setenv(_URLS_ENV, "http://127.0.0.1:8080, https://box.local.test")
        allow = egress.operator_allowance()
        assert "127.0.0.1:8080" in allow and "box.local.test:443" in allow
        assert egress.admit_url("http://127.0.0.1:8080/mcp",
                                allowed_hosts=allow).via == "allowance"
        assert egress.admit_url("http://127.0.0.1:9090/mcp",
                                allowed_hosts=allow).admitted is False
        assert egress.admit_url("http://box.local.test/mcp",
                                allowed_hosts=allow).admitted is False   # port 80 != 443

    def test_a_declared_local_server_is_reached_through_the_manager(self, monkeypatch):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        with _recording_server() as (port, hits):
            url = f"http://127.0.0.1:{port}/mcp"
            refused = asyncio.run(ConnectionManager(default_timeout=10.0).list_tools(
                "local", {"transport": "http", "url": url}))
            assert refused.ok is False and "SSRF" in refused.reason and hits == []
            monkeypatch.setenv(_URLS_ENV, f"http://127.0.0.1:{port}")
            reached = asyncio.run(ConnectionManager(default_timeout=10.0).list_tools(
                "local", {"transport": "http", "url": url}))
        assert "SSRF" not in reached.reason
        assert hits, "the env-declared server must be reached"

    def test_an_explicit_allowance_still_admits_env_declared_servers(self, monkeypatch):
        """The runtime-connect path passes HarnessPolicy.allowed_mcp_hosts
        explicitly; the env-declared servers count there too."""
        from systemu.runtime.mcp.sdk.manager import ConnectionManager
        monkeypatch.setenv(_URLS_ENV, "http://127.0.0.1:8080")
        v = ConnectionManager()._admit({"transport": "http", "url": "http://127.0.0.1:8080/x"},
                                       allowed_hosts={"10.0.0.9"})
        assert v.admitted is True and v.via == "allowance"


# ---------------------------------------------------------------------------
# (a) the Settings grant -- exact host:port, .env + live environment
# ---------------------------------------------------------------------------

class TestTheSettingsGrant:
    def test_grant_writes_exactly_one_host_port(self, env_file):
        from systemu.interface.pages import settings
        from systemu.runtime.mcp.sdk import egress
        assert settings.grant_mcp_allowance("127.0.0.1:8080") == "127.0.0.1:8080"
        assert os.environ[_ALLOW_ENV] == "127.0.0.1:8080"
        text = env_file.read_text(encoding="utf-8")
        assert f"{_ALLOW_ENV}=127.0.0.1:8080" in text and "OTHER_SETTING=1" in text
        assert egress.admit_url("http://127.0.0.1:8080/mcp",
                                allowed_hosts=egress.operator_allowance()).via == "allowance"
        assert egress.admit_url("http://127.0.0.1:8081/mcp",
                                allowed_hosts=egress.operator_allowance()).admitted is False

    def test_grants_accumulate_without_duplicates(self, env_file):
        from systemu.interface.pages import settings
        settings.grant_mcp_allowance("127.0.0.1:8080")
        settings.grant_mcp_allowance("[::1]:9000")
        settings.grant_mcp_allowance("127.0.0.1:8080")
        assert os.environ[_ALLOW_ENV] == "127.0.0.1:8080,[::1]:9000"

    @pytest.mark.parametrize("entry", ["127.0.0.1", "localhost", "a b:1", "http://x:1/",
                                       "", "127.0.0.1:1,10.0.0.1:2", "127.0.0.1:notaport"])
    def test_anything_but_one_exact_host_port_is_refused(self, env_file, entry):
        from systemu.interface.pages import settings
        with pytest.raises(ValueError):
            settings.grant_mcp_allowance(entry)
        assert _ALLOW_ENV not in os.environ

    def test_the_local_refusal_helper(self, env_file):
        from systemu.interface.pages import settings
        r = settings.mcp_local_refusal("http://127.0.0.1:8080/mcp")
        assert r is not None and r["entry"] == "127.0.0.1:8080" and "SSRF" not in r["entry"]
        assert settings.MCP_LOCAL_PROMPT.format(entry=r["entry"]) == (
            "This is a local or private address. Allow systemu to connect to 127.0.0.1:8080?")
        settings.grant_mcp_allowance(r["entry"])
        assert settings.mcp_local_refusal("http://127.0.0.1:8080/mcp") is None
        assert settings.mcp_local_refusal("file:///etc/passwd") is None   # not a local-address case

    def test_the_settings_constant_is_the_guards(self):
        from systemu.interface.pages import settings
        from systemu.runtime.mcp.sdk import egress
        assert settings._MCP_ALLOWANCE_ENV == egress.ALLOWANCE_ENV == _ALLOW_ENV


class TestTheSettingsPageWiresIt:
    """The MCP card's handlers route through the two helpers (source pins; the
    page is a NiceGUI closure)."""

    def _section(self):
        src = _SETTINGS.read_text(encoding="utf-8")
        start = src.index('_section_header("MCP servers")')
        end = src.index("# ── Telegram reach", start)
        return src[start:end]

    @staticmethod
    def _handler(name):
        tree = ast.parse(_SETTINGS.read_text(encoding="utf-8"))
        found = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        assert len(found) == 1, f"handler {name} must exist exactly once"
        return found[0]

    @staticmethod
    def _calls(node):
        out = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                out.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        return out

    def test_adding_a_local_server_asks_and_grants(self):
        add = self._handler("_add_mcp")
        calls = self._calls(add)
        assert "mcp_local_refusal" in calls, "the Add handler must ask the guard first"
        assert "format" in calls and "MCP_LOCAL_PROMPT" in ast.unparse(add)
        yes = self._handler("_yes")
        yes_calls = self._calls(yes)
        assert yes_calls.index("_allow") < yes_calls.index("_save"), (
            "on yes: grant the allowance, THEN save the server")
        assert "grant_mcp_allowance" in self._calls(self._handler("_allow"))
        # a refused local address is never saved without the grant: the only
        # unconditional _save() in the Add handler is the stdio branch and the
        # "not refusal" branch
        src = ast.unparse(add)
        assert "if not refusal:" in src

    def test_a_refused_saved_server_offers_the_allow_button(self):
        sec = self._section()
        assert 'f"Allow {' in sec, "the button must name the exact host:port"
        assert "_allow" in self._calls(self._handler("_allow_saved"))

    def test_the_placeholder_suggests_no_refused_address(self):
        sec = self._section()
        tree = ast.parse(_SETTINGS.read_text(encoding="utf-8"))
        placeholders = [kw.value.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                        for kw in n.keywords
                        if kw.arg == "placeholder" and isinstance(kw.value, ast.Constant)
                        and "command for stdio" in str(kw.value.value)]
        assert placeholders, "the MCP URL input keeps a placeholder"
        for ph in placeholders:
            for token in ph.split():
                if token.startswith(("http://", "https://")):
                    assert "localhost" not in token and "127.0.0.1" not in token, ph
        assert "asks you to allow it" in sec


# ---------------------------------------------------------------------------
# (b) the allowance store's writer set (AST), and SYSTEMU_MCP_SERVER_URLS has none
# ---------------------------------------------------------------------------

def _writes_of(name):
    """Every site in systemu/ + sharing_on/ that writes env ``name``: an
    os.environ write, or an ``_update_env_var`` (.env) call, whose key resolves
    to ``name``."""
    sites = {a.site for a in _env_scan.scan() if a.write and a.name == name}
    for d in _env_scan.SCAN_DIRS:
        for py in (_env_scan.ROOT / d).rglob("*.py"):
            rel = py.relative_to(_env_scan.ROOT).as_posix()
            tree = ast.parse(py.read_text(encoding="utf-8"))
            consts = _env_scan._module_consts(tree)

            class V(ast.NodeVisitor):
                stack = []

                def _scope(self, node):
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

                def visit_Call(self, node):
                    f = node.func
                    fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if fname == "_update_env_var" and node.args:
                        a = node.args[0]
                        val = (a.value if isinstance(a, ast.Constant) else
                               consts.get(a.id) if isinstance(a, ast.Name) else None)
                        if val == name:
                            sites.add(f"{rel}::{'.'.join(self.stack)}::_update_env_var")
                    self.generic_visit(node)

            V().visit(tree)
    return sites


class TestTheAllowanceWriterSet:
    def test_only_the_settings_grant_writes_the_allowance(self):
        sites = _writes_of(_ALLOW_ENV)
        assert sites == {
            "systemu/interface/pages/settings.py::grant_mcp_allowance::_MCP_ALLOWANCE_ENV",
            "systemu/interface/pages/settings.py::grant_mcp_allowance::_update_env_var",
        }, sites

    def test_nothing_writes_the_env_declared_server_list(self):
        assert _writes_of(_URLS_ENV) == set()

    def test_no_dynamic_writer_table_can_name_either(self):
        from systemu.interface.command import verbs
        from systemu.interface.pages import welcome
        from systemu.runtime import costing
        from systemu.runtime.adherence import ADHERENCE_PRESETS
        tables = set(verbs._SETTING_ENV_KEYS.values()) | set(welcome.PRESET_TIER_ENV) \
            | {costing.PRICE_OVERRIDE_ENV} | {k for v in ADHERENCE_PRESETS.values() for k in v}
        assert _ALLOW_ENV not in tables and _URLS_ENV not in tables

    @pytest.mark.parametrize("key", [_ALLOW_ENV, _URLS_ENV])
    def test_the_model_reachable_writer_is_fenced(self, key, monkeypatch):
        from types import SimpleNamespace
        from systemu.runtime.credentials.resolver import CredentialResolver

        class _Store:
            def get(self, k):
                return "127.0.0.1:1"

        before = dict(os.environ)
        CredentialResolver(store=_Store()).promote_to_env([SimpleNamespace(key=key)])
        assert dict(os.environ) == before
