"""/privacy must be true on a default install (orchestrator ruling 2026-10-17).

Every destination the page names is derived from the SAME config/state the
product reads:

  * web search + page reads -> the switch the shipped web tools read at call time
    (``SYSTEMU_WEB_STACK_V2``) and the reader backend that actually reaches
    ``web_access.read_url`` on the tool path. The route the page describes is
    pinned against what the real ``web_access`` code contacts (``_http_get``
    stubbed - no network);
  * MCP servers -> the MCP connections store (``runtime/mcp/connections``) plus
    the env-declared servers it merges;
  * Telegram -> the env the gateway itself reads, present only when a bot token
    is set;
  * the ledger export -> a pointer to Settings -> Compliance export.

Hermetic: every test uses a tmp vault, monkeypatched env, and stubbed HTTP.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from systemu.runtime import privacy

_REMOTE = "deepseek/deepseek-v4-flash"

_ENV_VARS = (
    "SYSTEMU_WEB_STACK_V2",
    "SYSTEMU_WEB_READER_BACKEND",
    "SYSTEMU_MCP_SERVER_URLS",
    "SHARING_ON_TELEGRAM_BOT_TOKEN",
    "SHARING_ON_TELEGRAM_ALLOWED_USER_IDS",
    "SHARING_ON_MESSAGING_PUSH_DETAIL",
    "SHARING_ON_MESSAGING_DECISION_RESOLUTION",
    "SYSTEMU_TAVILY_API_KEY",
    "SYSTEMU_EXA_API_KEY",
    "SYSTEMU_BRAVE_API_KEY",
    "SYSTEMU_SERPER_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    yield


def _profile(**over):
    base = dict(os="win32", os_family="windows", arch="x86_64",
                python_version="3.12.0", capture_available=True,
                keyring_backend="dpapi", forged_net_jail="absent",
                docker_mode=False, provider_configured=True, host_capabilities=[])
    base.update(over)
    return base


def _tiers():
    return {"tier1": _REMOTE, "tier2": _REMOTE, "tier3": _REMOTE}


def _vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir(exist_ok=True)
    return SimpleNamespace(root=str(root))


def _config(backend="auto"):
    return SimpleNamespace(web_reader_backend=backend)


def _report(tmp_path, *, backend="auto", vault=None, **kw):
    return privacy.privacy_report(
        profile=_profile(), tier_models=_tiers(),
        vault=vault if vault is not None else _vault(tmp_path),
        config=_config(backend), **kw)


def _section(report, key):
    found = [s for s in report["sections"] if s["key"] == key]
    assert found, f"no {key!r} section in {[s['key'] for s in report['sections']]}"
    return found[0]


def _has_section(report, key):
    return any(s["key"] == key for s in report["sections"])


def _item(section, key):
    found = [i for i in section.get("items", []) if i["key"] == key]
    assert found, f"no {key!r} item in {[i['key'] for i in section.get('items', [])]}"
    return found[0]


def _page_text(report):
    from systemu.interface.pages import privacy as page
    return page.page_text(page.privacy_page_model(report=report))


# =============================================================================
#  1. Web search + page reads: the relay is named from the values the tools read
# =============================================================================

def test_default_search_names_the_relay_before_the_search_engine(tmp_path):
    r = _report(tmp_path)
    s = _item(_section(r, "tools"), "search")["text"]
    low = s.lower()
    assert "r.jina.ai" in low and "duckduckgo" in low
    # The query reaches r.jina.ai FIRST - the order is the disclosure.
    assert low.index("r.jina.ai") < low.index("duckduckgo")
    assert r["web"]["search_route"][0] == "r.jina.ai"
    # The false sentence the page used to render must never come back.
    assert "web search goes to duckduckgo" not in _page_text(r).lower()


def test_default_page_reads_name_the_relay(tmp_path):
    r = _report(tmp_path)
    s = _item(_section(r, "tools"), "read")["text"].lower()
    assert "r.jina.ai" in s
    assert r["web"]["read_route"][0] == "r.jina.ai"
    assert r["web"]["reader_backend_effective"] == "auto"


def test_a_direct_reader_setting_is_reported_as_not_applied_by_the_tools(tmp_path):
    """The operator sets web_reader_backend=raw. The shipped tools call
    read_url WITHOUT a config, so the setting never reaches them: claiming a
    direct route here would be the false-assurance defect (DEC-34). The page
    names the setting, says it is not applied, and keeps naming the relay."""
    r = _report(tmp_path, backend="raw")
    tools = _section(r, "tools")
    note = _item(tools, "reader_setting")["text"].lower()
    assert "raw" in note and "not" in note
    assert "r.jina.ai" in _item(tools, "read")["text"].lower()
    assert "r.jina.ai" in _item(tools, "search")["text"].lower()
    assert r["web"]["reader_backend_configured"] == "raw"
    assert r["web"]["reader_backend_effective"] == "auto"


def test_the_default_reader_setting_adds_no_note(tmp_path):
    r = _report(tmp_path, backend="auto")
    assert not [i for i in _section(r, "tools")["items"] if i["key"] == "reader_setting"]


def test_web_stack_off_routes_directly_and_names_no_relay(tmp_path, monkeypatch):
    """SYSTEMU_WEB_STACK_V2=false is the switch the shipped tools really read:
    then nothing goes through r.jina.ai and the page names the direct route."""
    monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", "false")
    r = _report(tmp_path)
    tools = _section(r, "tools")
    search = _item(tools, "search")["text"].lower()
    read = _item(tools, "read")["text"].lower()
    assert "r.jina.ai" not in search and "r.jina.ai" not in read
    assert "directly" in read
    assert "ddgs" in search
    assert r["web"]["stack_v2"] is False
    assert "r.jina.ai" not in r["web"]["search_route"]
    assert "r.jina.ai" not in r["web"]["read_route"]


def test_web_stack_off_names_a_keyed_search_provider_when_its_key_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", "false")
    monkeypatch.setenv("SYSTEMU_BRAVE_API_KEY", "k-not-a-real-key-value")
    r = _report(tmp_path)
    search = _item(_section(r, "tools"), "search")["text"]
    assert "brave" in search.lower()
    assert "k-not-a-real-key-value" not in _page_text(r)
    assert r["web"]["search_route"] == ["brave", "ddgs"]


def test_place_lookups_name_nominatim_and_every_overpass_server(tmp_path):
    from systemu.runtime import web_access
    r = _report(tmp_path)
    places = _item(_section(r, "tools"), "places")["text"].lower()
    assert "nominatim.openstreetmap.org" in places
    hosts = [urlparse(u).hostname for u in web_access._OVERPASS_HOSTS]
    for h in hosts:
        assert h in places
    assert r["web"]["places_hosts"] == ["nominatim.openstreetmap.org"] + hosts


# ---- the route the page describes == what the real web_access code contacts --

class _HttpSpy:
    def __init__(self):
        self.urls = []

    def __call__(self, url, timeout=30, headers=None):
        self.urls.append(url)
        return None, "", "stubbed: no network in tests"


@pytest.fixture
def http_spy(monkeypatch):
    from systemu.runtime import web_access
    spy = _HttpSpy()
    monkeypatch.setattr(web_access, "_http_get", spy)
    monkeypatch.setattr(web_access._RL, "wait", lambda host: None)
    monkeypatch.setattr(web_access, "_sleep_backoff", lambda seconds: None)
    return spy


def test_read_route_matches_what_read_url_contacts_on_the_tool_path(tmp_path, http_spy):
    from systemu.runtime import web_access
    url = "https://example.test/privacy-route-pin-read"
    # The tool path: no config reaches read_url (pinned below).
    web_access.read_url(url)
    contacted = [urlparse(u).hostname for u in http_spy.urls]
    assert contacted == ["r.jina.ai", "example.test"]
    # ... and the jina hop carries the target URL - r.jina.ai sees the address.
    assert http_spy.urls[0].endswith(url)

    r = _report(tmp_path)
    described = [("example.test" if h == privacy.SITE else h)
                 for h in r["web"]["read_route"]]
    assert described == contacted


def test_search_route_matches_what_search_web_contacts(tmp_path, http_spy):
    from systemu.runtime import web_access
    q = "privacy route pin search"
    web_access.search_web(q)
    contacted = [urlparse(u).hostname for u in http_spy.urls]
    assert contacted == ["r.jina.ai", "lite.duckduckgo.com"]
    # The relayed request embeds the DuckDuckGo results URL WITH the query.
    assert "html.duckduckgo.com" in http_spy.urls[0]
    assert "privacy" in http_spy.urls[0]

    r = _report(tmp_path)
    assert r["web"]["search_route"] == contacted
    assert r["web"]["search_relays_to"] == "html.duckduckgo.com"


def test_geocode_contacts_the_host_the_page_names(tmp_path, http_spy):
    from systemu.runtime import web_access
    web_access._geocode_one("Privacy Pin Place 12345")
    contacted = {urlparse(u).hostname for u in http_spy.urls}
    assert contacted == {"nominatim.openstreetmap.org"}
    r = _report(tmp_path)
    assert r["web"]["places_hosts"][0] in contacted


def test_the_shipped_web_tools_pass_no_config_to_the_reader(monkeypatch):
    """Pins reader_backend_effective == 'auto' on the tool path. If a tool ever
    starts passing a config, the operator's web_reader_backend becomes live and
    THIS goes red, so the page is changed on purpose, not by accident."""
    from systemu.runtime import web_access
    from systemu.vault.tools.implementations import (
        web_extract as t_extract, web_read as t_read, web_search as t_search)

    seen = []

    def _read(url, **kw):
        seen.append(("read", kw))
        return {"content": "", "status": None, "source": "none", "url": url, "error": "x"}

    def _search(query, **kw):
        seen.append(("search", kw))
        return {"results": [], "provider": "none", "query": query, "error": "x"}

    monkeypatch.setattr(web_access, "read_url", _read)
    monkeypatch.setattr(web_access, "search_web", _search)
    t_read.run(url="https://example.test/a")
    t_extract._fetch("https://example.test/b", {}, {}, 10)
    t_search.run(query="q")
    assert [k for k, _ in seen] == ["read", "read", "search"]
    for _, kw in seen:
        assert "config" not in kw
    assert privacy.TOOL_READER_BACKEND == "auto"


@pytest.mark.parametrize("raw", [None, "", "true", "TRUE", "1", "false", "FALSE", "0", "off"])
def test_the_stack_switch_is_parsed_exactly_as_the_web_search_tool_parses_it(monkeypatch, raw):
    from systemu.runtime import web_access
    from systemu.runtime.web import search_providers
    from systemu.vault.tools.implementations import web_search as t_search

    if raw is None:
        monkeypatch.delenv("SYSTEMU_WEB_STACK_V2", raising=False)
    else:
        monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", raw)
    called = []
    monkeypatch.setattr(web_access, "search_web",
                        lambda q, **kw: called.append("v2") or {"results": []})
    monkeypatch.setattr(search_providers, "search",
                        lambda q, n=5: called.append("legacy") or {"results": []})
    t_search.run(query="q")
    assert called == (["v2"] if privacy.web_stack_v2_on() else ["legacy"])


# =============================================================================
#  2. MCP servers - from the connections store the product reads
# =============================================================================

def test_two_mcp_connections_are_listed_remote_with_host_stdio_as_local(tmp_path):
    from systemu.runtime.mcp import connections
    v = _vault(tmp_path)
    connections.add_server(v, "https://mcp.example.com/mcp")
    connections.add_server(v, "files", transport={
        "transport": "stdio", "command": "python", "args": ["-m", "files_server"]})

    r = _report(tmp_path, vault=v)
    mcp = _section(r, "mcp")
    assert mcp["status"] == "connected"
    remote = _item(mcp, "https://mcp.example.com/mcp")
    local = _item(mcp, "files")
    assert remote["remote"] is True and remote["host"] == "mcp.example.com"
    assert "mcp.example.com" in remote["text"] and "remote" in remote["text"].lower()
    assert "argument" in remote["text"].lower()
    assert local["remote"] is False and local["host"] is None
    assert "runs locally" in local["text"].lower()
    assert "argument" in local["text"].lower()
    # The command line is NOT rendered (it can carry a home path).
    assert "files_server" not in _page_text(r)


def test_no_mcp_connections_renders_an_explicit_none_line(tmp_path):
    r = _report(tmp_path)
    mcp = _section(r, "mcp")
    assert mcp["status"] == "none"
    assert "no mcp servers connected" in mcp["detail"].lower()
    assert "no mcp servers connected" in _page_text(r).lower()


def test_an_env_declared_mcp_server_is_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMU_MCP_SERVER_URLS", "https://env.example.org/mcp")
    r = _report(tmp_path)
    mcp = _section(r, "mcp")
    it = _item(mcp, "https://env.example.org/mcp")
    assert it["host"] == "env.example.org" and it["origin"] == "env"
    assert "SYSTEMU_MCP_SERVER_URLS" in it["text"]


def test_an_unreadable_store_is_never_reported_as_none(tmp_path):
    v = _vault(tmp_path)
    p = Path(v.root) / "connections" / "mcp.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    r = _report(tmp_path, vault=v)
    mcp = _section(r, "mcp")
    assert mcp["status"] == "unknown"
    assert "no mcp servers connected" not in _page_text(r).lower()


def test_no_vault_handle_is_unknown_not_none(tmp_path):
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers(),
                               vault=None, config=_config())
    assert _section(r, "mcp")["status"] == "unknown"


def test_a_secret_in_a_remote_server_url_is_not_rendered(tmp_path):
    from systemu.runtime.mcp import connections
    v = _vault(tmp_path)
    secret = "s3cr3t-query-token-value-0123456789abcdef"
    connections.add_server(v, f"https://user:pw-{secret}@mcp.example.com/sse?token={secret}")
    r = _report(tmp_path, vault=v)
    text = _page_text(r)
    assert "mcp.example.com" in text
    assert secret not in text


# =============================================================================
#  3. Telegram - present only when a bot token is configured
# =============================================================================

_TOKEN = "123456789:telegram-token-value-that-must-never-render-0000"


def test_telegram_line_present_with_what_is_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")
    r = _report(tmp_path)
    tg = _section(r, "telegram")
    d = tg["detail"].lower()
    assert tg["status"] == "on"
    assert "api.telegram.org" in d
    assert "decision card" in d
    assert "task outcome" in d
    assert _TOKEN not in _page_text(r)


def test_telegram_full_push_detail_discloses_the_card_body(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "full")
    full = _section(_report(tmp_path), "telegram")["detail"].lower()
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "summary")
    summary = _section(_report(tmp_path), "telegram")["detail"].lower()
    assert "body" in full and "body" not in summary


def test_no_telegram_token_no_telegram_line(tmp_path):
    r = _report(tmp_path)
    assert not _has_section(r, "telegram")
    assert "telegram" not in _page_text(r).lower()


def test_a_token_without_an_allowlist_says_nothing_is_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", _TOKEN)
    tg = _section(_report(tmp_path), "telegram")
    assert tg["status"] == "off"
    assert "nothing is sent" in tg["detail"].lower()


@pytest.mark.parametrize("token,allow", [("", ""), (_TOKEN, ""), (_TOKEN, "111,222"),
                                         ("", "111")])
def test_the_telegram_line_agrees_with_the_gateway_builder(tmp_path, monkeypatch, token, allow):
    """No second table: the page is 'on' exactly when the gateway's own
    build_from_env would start a gateway."""
    from systemu.messaging import telegram_gateway
    for k, v in (("SHARING_ON_TELEGRAM_BOT_TOKEN", token),
                 ("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", allow)):
        if v:
            monkeypatch.setenv(k, v)
    starts = telegram_gateway.build_from_env() is not None
    r = _report(tmp_path)
    on = _has_section(r, "telegram") and _section(r, "telegram")["status"] == "on"
    assert on is starts


# =============================================================================
#  4. The ledger-export pointer
# =============================================================================

def test_the_ledger_export_pointer_is_present(tmp_path):
    r = _report(tmp_path)
    led = _section(r, "ledger")
    d = led["detail"]
    assert "Settings" in d and "Compliance export" in d
    assert led["route"] == "/settings"
    from systemu.runtime import ledger_export
    for fmt in ledger_export.FORMATS:
        assert fmt in d
    assert "compliance export" in _page_text(r).lower()


def _calls_in(module, func_name):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            out = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
            return out
    raise AssertionError(func_name)


def test_the_pointer_target_exists_on_settings():
    """Reachability: the section the pointer names is really rendered."""
    from systemu.interface.pages import settings
    assert "compliance_export_card" in _calls_in(settings, "build_settings_page")
    assert '_section_header("Compliance export")' in inspect.getsource(settings)


# =============================================================================
#  5. The forged-network sentence matches the hard-DENY that ships
# =============================================================================

def test_the_page_no_longer_says_a_forged_network_tool_can_be_approved(tmp_path):
    """The hard-DENY fires BEFORE the approval gates (tool_sandbox) - it is a
    refusal, never an approvable card. The old sentence ('refused unless you
    approve it') was false."""
    r = _report(tmp_path)
    text = _page_text(r).lower()
    assert "unless you approve" not in text
    forged = _item(_section(r, "tools"), "forged")["text"].lower()
    assert "refused outright" in forged and "cannot approve" in forged


# =============================================================================
#  6. The page model: pure, ASCII, and actually rendered
# =============================================================================

def test_every_rendered_string_is_ascii(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")
    v = _vault(tmp_path)
    connections.add_server(v, "https://mcp.example.com/mcp")
    from systemu.interface.pages import privacy as page
    for prof in (_profile(), _profile(docker_mode=True, keyring_backend="plaintext_fallback")):
        rep = privacy.privacy_report(profile=prof, tier_models=_tiers(), vault=v,
                                     config=_config("raw"))
        text = page.page_text(page.privacy_page_model(report=rep))
        assert text.isascii(), [c for c in text if not c.isascii()][:5]


def test_the_page_renders_the_model():
    from systemu.interface.pages import privacy as page
    assert "privacy_page_model" in _calls_in(page, "live_page_model")
    assert "privacy_report" in _calls_in(page, "privacy_page_model")


def test_the_page_model_reads_the_vault_it_is_given(tmp_path):
    """The page model forwards its vault to the mint - a model that dropped it
    would render "not available" on every install with servers connected."""
    from systemu.interface.pages import privacy as page
    from systemu.runtime.mcp import connections
    v = _vault(tmp_path)
    connections.add_server(v, "https://mcp.example.com/mcp")
    text = page.page_text(page.privacy_page_model(vault=v, config=_config()))
    assert "mcp.example.com" in text


def test_the_page_renders_from_the_live_inputs(monkeypatch, tmp_path):
    """build_privacy_page renders live_page_model(), which feeds THIS process's
    vault + config to the mint (behavioural: the live vault's MCP server shows)."""
    from systemu.interface import dashboard_state
    from systemu.interface.pages import privacy as page
    from systemu.runtime.mcp import connections
    assert "live_page_model" in _calls_in(page, "build_privacy_page")
    v = _vault(tmp_path)
    connections.add_server(v, "https://live.example.net/mcp")
    monkeypatch.setattr(dashboard_state.AppState, "get",
                        classmethod(lambda cls: SimpleNamespace(vault=v, config=_config())))
    assert "live.example.net" in page.page_text(page.live_page_model())


def test_the_page_feeds_the_live_vault_and_config(monkeypatch, tmp_path):
    from systemu.interface import dashboard_state
    from systemu.interface.pages import privacy as page
    v, c = _vault(tmp_path), _config()
    monkeypatch.setattr(dashboard_state.AppState, "get",
                        classmethod(lambda cls: SimpleNamespace(vault=v, config=c)))
    assert page.live_inputs() == (v, c)


def test_every_section_reaches_the_page_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111")
    from systemu.interface.pages import privacy as page
    rep = _report(tmp_path)
    model = page.privacy_page_model(report=rep)
    assert [s["key"] for s in model["sections"]] == [s["key"] for s in rep["sections"]]
    for k in ("tools", "mcp", "telegram", "ledger"):
        assert k in [s["key"] for s in model["sections"]]


# =============================================================================
#  7. The trust card defers to this page - its named parties are disclosed here
# =============================================================================

def test_every_third_party_the_trust_card_names_is_disclosed_by_the_page(tmp_path):
    """Ruled 2026-08-14: the card carries NO transmission sentence and links to
    /privacy instead. Its omission reason names the third parties that made
    every candidate sentence false; each must be on the page it points at."""
    from systemu.interface import trust_card as tc
    reason = tc.TRANSMISSION_OMISSION_REASON.lower()
    text = _page_text(_report(tmp_path)).lower()
    for party in ("r.jina.ai", "nominatim", "overpass"):
        assert party in reason
        assert party in text
    assert tc.PRIVACY_ROUTE == "/privacy"
