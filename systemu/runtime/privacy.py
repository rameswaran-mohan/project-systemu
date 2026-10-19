"""R-P3b - the "What leaves this machine" privacy report (section 15.7 interim honesty rule).

PURE data over the platform profile + the configured LLM tiers + the config/state
the product itself reads (the page just renders it). It states PLAINLY the
machine's actual egress reality: until a private-compute mode exists,
"local-first" here means CUSTODY (your vault), VERIFICATION (receipts), and the
boundary - NOT zero egress. Truthful pre- AND post-S2 (the OS egress jail) - AC6.
Never prints a key, only reasons about presence/locality.

THE MINT RULE (orchestrator ruling 2026-10-17)
    /privacy is the mint and must be true on a DEFAULT install. Every destination
    is derived from the SAME config/state the product reads - no second table:

      * web search / page reads -> the switch the shipped web tools read at call
        time (``SYSTEMU_WEB_STACK_V2``, parsed exactly as they parse it) and the
        reader backend that actually reaches ``web_access.read_url`` on the tool
        path. The tools call it WITHOUT a config, so the operator's
        ``web_reader_backend`` never reaches them: the backend in effect is
        ``read_url``'s own default. A configured non-default value is reported as
        NOT APPLIED rather than rendered as a direct route - claiming a direct
        route there would be a false assurance (DEC-34). The route descriptions
        below are pinned against what the real ``web_access`` code contacts
        (tests/test_privacy_disclosure.py, ``_http_get`` stubbed);
      * MCP servers -> the MCP connections store plus the env-declared servers it
        merges (``mcp.connections.all_servers`` / ``transport_for``);
      * Telegram -> the env the gateway reads; present only when a bot token is
        set; "on" exactly when ``telegram_gateway.build_from_env`` would start;
      * the egress enforcer -> ``action_governance._egress_enforcer_available``,
        the seam the forged-network hard-DENY itself consults;
      * the ledger export -> a pointer to Settings -> Compliance export.

    Render-time reads are cheap local reads only: no network, no probes. The
    profile is built with ``provider_configured=False`` because this report never
    reads that key and the default would spend the provider reachability probe on
    every render.

Accuracy notes (from the truthfulness audit - a privacy page that over-claims is a
trust failure):
  * Locality is PER-TIER. The agent runs three model tiers; "nothing leaves for the
    LLM" is asserted ONLY when EVERY tier is a local (ollama) model - a local tier-1
    with a remote tool-forge/formatting tier still egresses.
  * We name the actual network DESTINATION (openrouter.ai for the flash/catch-all
    ids, anthropic for native claude), not the model vendor.
  * "Encrypted" secrets is read off the profile's ``secrets_at_rest`` row - the
    one derived from a WORKING envelope round trip (S5 / R-A3) - never off
    ``keyring_backend``. A plaintext row, a keyring that leaves secret files
    plaintext, and an unreadable row are all flagged, never assumed safe.
  * Every operator-visible string is ASCII (DEC-32c).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from systemu.runtime.platform_profile import (
    KEYRING_DPAPI, KEYRING_KEYCHAIN, KEYRING_SECRETSERVICE,
    SECRETS_DPAPI, SECRETS_KEYRING, SECRETS_PLAINTEXT,
)

# Whitelist: ONLY these are genuine OS secret stores. Anything else (the
# plaintext_fallback 0600 file, unknown) is flagged - never assumed encrypted.
_ENCRYPTED_BACKENDS = frozenset({KEYRING_DPAPI, KEYRING_KEYCHAIN, KEYRING_SECRETSERVICE})

# -- the web routes (descriptions of web_access's code, pinned by observation) --

#: The reader backend in effect on the TOOL path. ``web_read`` / ``web_extract``
#: call ``web_access.read_url`` with no ``config``, so ``read_url`` falls back to
#: its own built-in default. Pinned by
#: ``test_the_shipped_web_tools_pass_no_config_to_the_reader``.
TOOL_READER_BACKEND = "auto"

#: Sentinel in ``read_route`` for "the site the address names", contacted directly.
SITE = "<the site itself>"

JINA_HOST = "r.jina.ai"
DDG_HTML_HOST = "html.duckduckgo.com"
DDG_LITE_HOST = "lite.duckduckgo.com"
NOMINATIM_HOST = "nominatim.openstreetmap.org"

#: ``web_access.read_url``'s backend -> the hosts it contacts, in order.
_READER_ROUTES = {
    "auto": [JINA_HOST, SITE],      # relay first, raw GET on failure
    "jina": [JINA_HOST],
    "raw": [SITE],
}

# The env the Telegram gateway reads (``telegram_gateway.build_from_env``).
# Agreement is pinned by ``test_the_telegram_line_agrees_with_the_gateway_builder``.
_TELEGRAM_TOKEN_ENV = "SHARING_ON_TELEGRAM_BOT_TOKEN"
_TELEGRAM_ALLOWLIST_ENV = "SHARING_ON_TELEGRAM_ALLOWED_USER_IDS"

_SETTINGS_ROUTE = "/settings"


def _ascii(text: Any) -> str:
    """One ASCII spelling of an operator-visible string (DEC-32c)."""
    s = text if type(text) is str else str(text)
    return s.encode("ascii", "backslashreplace").decode("ascii")


def _short(text: Any, cap: int = 80) -> str:
    s = _ascii(text)
    return s if len(s) <= cap else s[:cap - 3] + "..."


def _tier_models(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The resolved {tier1,tier2,tier3} model ids (per-tier env override wins over
    the active preset). Deterministic; never reads a key value."""
    env = env if env is not None else dict(os.environ)
    try:
        from sharing_on import model_presets  # type: ignore
        base = model_presets.resolve_preset(env)
    except Exception:
        base = {}
    out = {}
    for i in (1, 2, 3):
        out[f"tier{i}"] = (env.get(f"SYSTEMU_TIER{i}_MODEL")
                           or base.get(f"tier{i}") or "")
    return out


def _is_local(model_id: str) -> bool:
    """True iff this model runs on THIS machine (no LLM egress) - the MODEL-MATRIX
    ``local_capable`` class (ollama/*)."""
    try:
        from sharing_on import model_presets  # type: ignore
        return model_presets.locality_of(model_id) == "local_capable"
    except Exception:
        return str(model_id or "").lower().startswith("ollama/")


def _destination(model_id: str) -> Optional[str]:
    """The network party that RECEIVES this tier's prompts - None if local. Native
    claude -> 'anthropic'; everything else (deepseek/google/openai/... flash + catch-all)
    routes through OpenRouter."""
    m = str(model_id or "").lower()
    if _is_local(m):
        return None
    if m.startswith("anthropic/"):
        return "anthropic"
    return "openrouter.ai"


# -- web: what search and page reads really contact ---------------------------

def web_stack_v2_on() -> bool:
    """The switch the shipped web tools read AT CALL TIME, parsed exactly as they
    parse it (``web_search`` / ``web_read`` / ``web_extract`` ``run``): anything
    but a case-insensitive "false" is ON. NOT ``Config.web_stack_v2_enabled``,
    whose parse differs ("1" is off there and on in the tools)."""
    return os.getenv("SYSTEMU_WEB_STACK_V2", "true").lower() != "false"


def _configured_reader_backend(config) -> Optional[str]:
    """The operator's ``web_reader_backend`` value, or None if it cannot be read."""
    if config is None:
        try:
            from sharing_on.config import Config
            config = Config.from_env()
        except Exception:
            return None
    try:
        if isinstance(config, dict):
            value = config.get("web_reader_backend")
        else:
            value = getattr(config, "web_reader_backend", None)
    except Exception:
        return None
    if type(value) is not str:
        return None
    return value.strip().lower()


def _keyed_search_providers() -> List[str]:
    """Legacy (stack-off) search chain: the keyed providers whose key is set, in
    the order ``search_providers.search`` tries them. The keyless floor is not
    included here."""
    try:
        from systemu.runtime.web import search_providers as _sp
        names = []
        for cls in _sp._CHAIN:
            prov = cls()
            if prov.name == "ddgs":
                continue
            if prov.available() is True:
                names.append(str(prov.name))
        return names
    except Exception:
        return []


def _overpass_hosts() -> List[str]:
    try:
        from systemu.runtime import web_access as _wa
        hosts = []
        for u in _wa._OVERPASS_HOSTS:
            h = urlparse(u).hostname
            if h and h not in hosts:
                hosts.append(h)
        return hosts
    except Exception:
        return []


def web_routes(config=None) -> Dict[str, Any]:
    """Where web search, page reads and place lookups go, from the values the
    shipped tools read. Never raises."""
    v2 = web_stack_v2_on()
    configured = _configured_reader_backend(config)
    overpass = _overpass_hosts()
    out: Dict[str, Any] = {
        "stack_v2": v2,
        "reader_backend_configured": configured,
        "places_hosts": [NOMINATIM_HOST] + overpass,
    }
    if v2:
        effective = TOOL_READER_BACKEND
        out.update({
            "reader_backend_effective": effective,
            "read_route": list(_READER_ROUTES.get(effective, [])),
            "search_route": [JINA_HOST, DDG_LITE_HOST],
            "search_relays_to": DDG_HTML_HOST,
        })
    else:
        out.update({
            "reader_backend_effective": None,     # read_url is not on this path
            "read_route": [SITE],
            "search_route": _keyed_search_providers() + ["ddgs"],
            "search_relays_to": None,
        })
    return out


def _web_items(web: Dict[str, Any], *, enforcer: bool) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []

    # -- search
    if web["stack_v2"]:
        search = ("Web search (web_search): your search query goes first to "
                  f"{JINA_HOST}, a third-party reader run by Jina AI, which fetches "
                  f"DuckDuckGo's results page ({DDG_HTML_HOST}) for it, so both "
                  f"{JINA_HOST} and DuckDuckGo receive the query. If that returns no "
                  "results, this machine sends the query directly to DuckDuckGo "
                  f"({DDG_LITE_HOST}).")
    else:
        keyed = [n for n in web["search_route"] if n != "ddgs"]
        floor = "the keyless ddgs library, which sends it on to public search engines"
        if keyed:
            chain = (f"the first of these that returns results: {', '.join(keyed)} "
                     f"(API key set), then {floor}")
        else:
            chain = floor
        search = ("Web search (web_search): your search query goes directly from "
                  f"this machine to {chain}. No third-party relay is used, because "
                  "SYSTEMU_WEB_STACK_V2 is off.")
    items.append({"key": "search", "text": search})

    # -- page reads
    route = web["read_route"]
    if web["stack_v2"] and route and route[0] == JINA_HOST:
        read = ("Reading a web page (web_read, web_extract): the page's address "
                f"goes first to {JINA_HOST}, which fetches the page and returns its "
                f"text, so {JINA_HOST} sees both the address and the page.")
        if SITE in route:
            read += (" If that fails, this machine fetches the page directly from "
                     "its site, and web_read may also open it in a local browser, "
                     "which contacts the site directly.")
    elif web["stack_v2"]:
        read = ("Reading a web page (web_read, web_extract): this machine fetches "
                "the page directly from its site, and web_read may also open it in "
                "a local browser, which contacts the site directly.")
    else:
        read = ("Reading a web page (web_read, web_extract): this machine fetches "
                "the page directly from its site, and web_read may also open it in "
                "a local browser, which contacts the site directly. No third-party "
                "relay is used, because SYSTEMU_WEB_STACK_V2 is off.")
    items.append({"key": "read", "text": read})

    # -- the operator's reader setting, when it differs from what is in effect
    configured = web["reader_backend_configured"]
    effective = web["reader_backend_effective"]
    if web["stack_v2"] and configured is not None and configured != effective:
        items.append({"key": "reader_setting", "text": (
            f"Your web_reader_backend setting is '{_short(configured, 24)}', but the "
            "built-in web tools do not pass that setting to the page reader, so it "
            "does not change the route above. Web search does not read it either.")})

    # -- place lookups
    overpass = [h for h in web["places_hosts"] if h != NOMINATIM_HOST]
    places = (f"Place lookups (find_places): the place name goes to {NOMINATIM_HOST}, "
              "and the kind of place with the area around it goes to public Overpass "
              "servers")
    places += (": " + ", ".join(overpass) + ".") if overpass else "."
    items.append({"key": "places", "text": places})

    # -- everything else that fetches
    items.append({"key": "other", "text": (
        "Other built-in tools that fetch or open an address (fetch_json, fetch_html, "
        "download_file, api_call_get, web_act, web_screenshot) contact the address "
        "the call names directly.")})

    items.append({"key": "mcp", "text": (
        "MCP servers you connected receive the arguments of their tools when a run "
        "calls them; they are listed under MCP servers below.")})

    if enforcer:
        forged = ("A tool the agent wrote for itself that reaches the network is not "
                  "refused outright while the egress enforcer reports available; it "
                  "goes through the approval gates instead.")
    else:
        forged = ("A tool the agent wrote for itself that reaches the network is "
                  "refused outright: you cannot approve it to run while the egress "
                  "enforcer is unavailable.")
    items.append({"key": "forged", "text": forged})
    return items


# -- the egress enforcer (the seam the hard-DENY consults) -------------------

def _egress_enforcer_available() -> bool:
    """Read the SAME seam ``action_governance.forged_network_denied`` reads. Any
    failure reads as unavailable - the direction the gate itself fails toward
    (a raising classification DENIES)."""
    try:
        from systemu.runtime import action_governance as _ag
        return _ag._egress_enforcer_available() is True
    except Exception:
        return False


def _sandbox_section(profile: Dict[str, Any], enforcer: bool) -> Dict[str, Any]:
    jail = _short(profile.get("forged_net_jail") or "absent", 40)
    jailed = jail != "absent"
    if jailed and enforcer:
        detail = (f"Network access by tools the agent wrote for itself is OS-sandboxed "
                  f"({jail}), and the egress enforcer is available.")
    elif jailed:
        detail = (f"This machine's profile reports an OS egress jail ({jail}), but the "
                  "egress enforcer the tool gate checks is unavailable, so a tool the "
                  "agent wrote for itself that reaches the network is still refused "
                  "outright.")
    elif enforcer:
        detail = ("The tool gate reports an egress enforcer, but this machine's "
                  "profile reports no OS-level egress jail, so this page does not "
                  "call outbound network sandboxed.")
    else:
        detail = ("There is no OS-level egress jail yet, and the egress enforcer is "
                  "unavailable on this machine. That means nothing at the OS level "
                  "limits where a program started by Systemu connects, so the "
                  "boundary is the forged-network hard-DENY: a tool the agent wrote "
                  "for itself that reaches the network is refused outright, and you "
                  "cannot approve it to run. Built-in tools and the MCP servers you "
                  "connected are not covered by that refusal; what they reach is "
                  "listed below. This is an honest interim posture, not a sandbox.")
    return {
        "key": "sandbox", "title": "Outbound network",
        "status": "sandboxed" if jailed else "unsandboxed",
        "severity": "ok" if (jailed and enforcer) else "info",
        "enforcer": "available" if enforcer else "unavailable",
        "detail": detail,
    }


# -- MCP servers (the connections store the product reads) -------------------

def _mcp_store_readable(vault) -> Tuple[bool, str]:
    """Witness the store, so an unreadable file is never reported as "none"
    (``connections.get_state`` deliberately returns empty state on a bad file)."""
    if vault is None:
        return False, "was not available to this page"
    try:
        from systemu.runtime.mcp import connections as _mc
        path = _mc._path(vault)
        if not path.exists():
            return True, ""
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data) is not dict:
            return False, "could not be read"
        return True, ""
    except Exception:
        return False, "could not be read"


def _host_of(url: str) -> Tuple[Optional[str], str]:
    """(hostname, host[:port]) of a remote server URL - never the userinfo, path
    or query (they can carry a credential)."""
    try:
        u = urlparse(url)
        host = u.hostname
        if not host:
            return None, ""
        try:
            port = u.port
        except ValueError:
            port = None
        return host, host + (f":{port}" if port else "")
    except Exception:
        return None, ""


def _mcp_section(vault, *, jailed: bool) -> Dict[str, Any]:
    readable, why = _mcp_store_readable(vault)
    try:
        from systemu.runtime.mcp import connections as _mc
        state = _mc.get_state(vault) if readable else {"servers": [], "servers_meta": {}}
        merged = _mc.all_servers(vault)
        store = set(state.get("servers") or [])
        meta = state.get("servers_meta") or {}
    except Exception:
        readable, why = False, "could not be read"
        _mc, merged, store, meta = None, [], set(), {}

    items: List[Dict[str, Any]] = []
    if not readable:
        items.append({"key": "store", "remote": None, "host": None, "origin": None,
                      "text": (f"The MCP connections store {why}, so this page "
                               "cannot list every connected server; Settings -> "
                               "MCP servers shows them.")})

    any_remote = False
    for server in merged:
        if type(server) is not str or not server:
            continue
        try:
            spec = _mc.transport_for(vault, server) if _mc else {}
        except Exception:
            spec = {}
        kind = str((spec or {}).get("transport") or "http").lower()
        origin = "settings" if server in store else "env"
        m = meta.get(server) if type(meta) is dict else None
        label = m.get("label") if type(m) is dict else None
        named = (f"{_short(label, 60)}: " if type(label) is str and label
                 and label != server and "://" not in label else "")
        tail = (" Declared in SYSTEMU_MCP_SERVER_URLS." if origin == "env" else "")
        if kind == "stdio":
            text = (f"{named or _short(server, 60) + ': '}runs locally as a program on "
                    "this machine. It receives the arguments of its tools when a run "
                    "calls them")
            text += ("." if jailed else
                     "; with no OS egress jail, what it sends onward is up to that "
                     "program.")
            items.append({"key": server, "remote": False, "host": None,
                          "origin": origin, "text": text + tail})
            continue
        any_remote = True
        host, hostport = _host_of(str((spec or {}).get("url") or server))
        where = (f"Remote server at {hostport} ({_short(kind, 20)})" if host else
                 "Remote server (its address could not be read from the store)")
        items.append({"key": server, "remote": True, "host": host, "origin": origin,
                      "text": (f"{named}{where}. It receives the arguments of its "
                               "tools when a run calls them." + tail)})

    servers = [i for i in items if i["key"] != "store"]
    if not readable:
        status, severity = "unknown", "warn"
    elif not servers:
        status, severity = "none", "ok"
    else:
        status, severity = "connected", ("warn" if any_remote else "info")

    detail = ("No MCP servers connected." if status == "none"
              else " ".join(i["text"] for i in items))
    section = {"key": "mcp", "title": "MCP servers you connected",
               "status": status, "severity": severity, "detail": detail}
    if items:
        section["items"] = items
    return section


# -- Telegram (present only when a bot token is configured) ------------------

def _telegram_section() -> Optional[Dict[str, Any]]:
    if not (os.environ.get(_TELEGRAM_TOKEN_ENV) or "").strip():
        return None
    try:
        from systemu.messaging.gateway import allowlist_from_env
        allowed = bool(allowlist_from_env(_TELEGRAM_ALLOWLIST_ENV))
    except Exception:
        allowed = True          # cannot tell -> disclose, never under-disclose

    if not allowed:
        return {"key": "telegram", "title": "Telegram", "status": "off",
                "severity": "info",
                "detail": ("A Telegram bot token is set, but no allowed Telegram user "
                           "IDs are, so the Telegram channel does not start and "
                           "nothing is sent to Telegram.")}

    # What a decision card carries depends on the pusher's own two switches.
    try:
        from systemu.messaging import event_pusher as _ep
        options = (", and their answer options as buttons where the card allows it"
                   if _ep._decision_resolution_on() else "")
        body = ", and the card's body" if _ep._push_detail() == "full" else ""
        card = f"their title{options}{body}"
    except Exception:
        card = "their title, and possibly their answer options and body"

    return {"key": "telegram", "title": "Telegram", "status": "on", "severity": "warn",
            "detail": (
                "Telegram is configured, so these leave this machine for Telegram's "
                "servers (api.telegram.org) and reach your allowlisted Telegram "
                f"accounts: decision cards that need you ({card}), task outcomes with "
                "the start of each result summary, run finished or failed notices, "
                "watchdog notices, tool proposals, and the replies to commands you "
                "send from Telegram, such as /status. Secret-looking text in these "
                "notices is masked before they are sent; replies to your commands "
                "are not masked.")}


# -- the ledger-export pointer -----------------------------------------------

def _ledger_section() -> Dict[str, Any]:
    try:
        from systemu.runtime import ledger_export as _le
        fmts = " or ".join(str(f) for f in _le.FORMATS)
    except Exception:
        fmts = ""
    how = f" as {fmts}" if fmts else ""
    return {"key": "ledger", "title": "Record of what ran", "status": "local",
            "severity": "info", "route": _SETTINGS_ROUTE,
            "detail": ("The action ledger on this machine keeps a record of the "
                       "actions the agent's runs take. To review it, export it from "
                       f"Settings -> Compliance export{how}; the file is written to "
                       "this machine only, and nothing is uploaded.")}


def _secrets_row(profile: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """``(backend, label, reason)`` of the profile's ``secrets_at_rest`` row, or
    None when the row is not one the profile mints (type-pinned, DEC-36)."""
    row = profile.get("secrets_at_rest")
    if type(row) is not dict:
        return None
    backend, label, reason = row.get("backend"), row.get("label"), row.get("reason")
    if type(backend) is not str or type(label) is not str or type(reason) is not str:
        return None
    if backend not in (SECRETS_DPAPI, SECRETS_KEYRING, SECRETS_PLAINTEXT):
        return None
    return backend, label, reason


def _secrets_section(profile: Dict[str, Any]) -> Dict[str, Any]:
    """The "Secrets at rest" line, from the TRUTHFUL row (S5 / R-A3).

    Status, severity and claim come from ``profile["secrets_at_rest"]`` -- the
    row derived from a WORKING envelope round trip -- never from
    ``keyring_backend``, which once read ``dpapi`` whenever any keyring existed.
    The keyring row is only NAMED alongside, as what holds the credentials.

    A row that is present but unreadable is flagged, never assumed safe. A
    profile with NO row at all (a hand-built profile from before the row
    existed; the live profile always carries one) keeps the old whitelist over
    the keyring row, which since R-A3 reads ``dpapi`` only off a working
    envelope itself.
    """
    keyring = _short(str(profile.get("keyring_backend") or "none").lower(), 40)
    row = _secrets_row(profile)
    if row is None and "secrets_at_rest" not in profile:
        encrypted = keyring in _ENCRYPTED_BACKENDS
        return {
            "key": "secrets", "title": "Secrets at rest",
            "status": "encrypted" if encrypted else "plaintext",
            "severity": "ok" if encrypted else "warn",
            "detail": (f"API keys and credentials are held in the OS secret store ({keyring})."
                       if encrypted else
                       f"Credentials fall back to a local file ({keyring}), NOT OS-encrypted. "
                       "Flagged."),
        }
    if row is None:
        status, severity = "unknown", "warn"
        detail = ("What protects secrets at rest could not be read from the platform "
                  f"profile, so they are NOT assumed encrypted (OS keyring: {keyring}). "
                  "Flagged.")
    else:
        backend, label, reason = row
        if backend == SECRETS_DPAPI:
            status, severity = "encrypted", "ok"
            detail = (f"Secrets are encrypted at rest ({label}): secret files are sealed "
                      "with Windows DPAPI under your user account "
                      f"(OS keyring: {keyring}).")
        elif backend == SECRETS_KEYRING and not reason:
            status, severity = "encrypted", "ok"
            detail = ("API keys and credentials are held in the OS secret store "
                      f"(OS keyring: {keyring}).")
        elif backend == SECRETS_KEYRING:
            status, severity = "partial", "warn"
            detail = (f"Not every secret is encrypted at rest: {reason} "
                      f"(OS keyring: {keyring}). Flagged.")
        else:
            status, severity = "plaintext", "warn"
            detail = (f"Secrets are NOT encrypted at rest: {label} "
                      f"(OS keyring: {keyring}). Flagged.")
    return {"key": "secrets", "title": "Secrets at rest",
            "status": status, "severity": severity, "detail": detail}


def privacy_report(*, profile: Optional[Dict[str, Any]] = None,
                   tier_models: Optional[Dict[str, str]] = None,
                   vault: Any = None, config: Any = None) -> Dict[str, Any]:
    """The 'what leaves this machine' report. ``profile`` defaults to the live
    platform profile; ``tier_models`` to the resolved per-tier model ids;
    ``vault`` is the vault whose MCP connections store is read (None -> the
    store is reported as not available, never as "none"); ``config`` supplies
    ``web_reader_backend`` (None -> ``Config.from_env()``).
    Deterministic + hermetic - safe to unit-test with fixtures (AC6)."""
    if profile is None:
        from systemu.runtime.platform_profile import platform_profile
        # provider_configured is never read here; passing it declines the
        # provider reachability probe the default would spend on every render.
        profile = platform_profile(provider_configured=False)
    if tier_models is None:
        tier_models = _tier_models()

    all_local = bool(tier_models) and all(_is_local(m) for m in tier_models.values())
    any_local = any(_is_local(m) for m in tier_models.values())
    destinations = sorted({d for m in tier_models.values() if (d := _destination(m))})
    dest_txt = ", ".join(destinations) or "the configured provider"

    jail = str(profile.get("forged_net_jail") or "absent")
    jailed = jail != "absent"
    enforcer = _egress_enforcer_available()

    sections = []

    # 1. Model calls - the headline "what leaves" (per-tier honest).
    if all_local:
        sections.append({
            "key": "llm", "title": "Model calls", "status": "local", "severity": "ok",
            "detail": ("Every model tier runs on a LOCAL model (ollama); the prompt text and "
                       "any file excerpts you share do NOT leave this machine for the LLM."),
        })
    elif any_local:
        sections.append({
            "key": "llm", "title": "Model calls", "status": "partial", "severity": "warn",
            "detail": (f"Some stages run on a local model, but others (e.g. the tool-forge / "
                       f"formatting tiers) send your prompts and any file excerpts to {dest_txt} "
                       "to be processed, so data still leaves this machine."),
        })
    else:
        sections.append({
            "key": "llm", "title": "Model calls", "status": "leaves", "severity": "warn",
            "detail": (f"Your prompts AND any file excerpts you give the agent transit {dest_txt} "
                       "to be processed. Local-first here means custody + verification, "
                       "not zero egress."),
        })

    # 2. Secrets at rest - from the TRUTHFUL row (S5 / R-A3), never the keyring row.
    sections.append(_secrets_section(profile))

    # 3. Outbound network sandbox (S2) - truthful pre AND post, read from the
    #    profile AND the enforcer seam the hard-DENY consults.
    sections.append(_sandbox_section(profile, enforcer))

    # 4. Tool network access - search, page reads, places, MCP, forged tools.
    web = web_routes(config)
    items = _web_items(web, enforcer=enforcer)
    sections.append({
        "key": "tools", "title": "What the agent's tools reach",
        "status": "gated", "severity": "info", "items": items,
        "detail": " ".join(i["text"] for i in items),
    })

    # 5. The MCP servers the operator connected.
    sections.append(_mcp_section(vault, jailed=jailed))

    # 6. Telegram, only when a bot token is configured.
    tg = _telegram_section()
    if tg is not None:
        sections.append(tg)

    # 7. Where to see what ran.
    sections.append(_ledger_section())

    # 8. Custody - what stays.
    sections.append({
        "key": "custody", "title": "Your data + custody", "status": "local", "severity": "ok",
        "detail": ("Your vault (activities, tools, outcomes, and receipts) lives locally on "
                   "this machine. A money-move is credited only via independent verification "
                   "(a hardened read-back), never the tool's self-report."),
    })

    # 9. Container / host boundary.
    if profile.get("docker_mode"):
        sections.append({
            "key": "docker", "title": "Container", "status": "container", "severity": "info",
            "detail": ("Running in a container: desktop / host capture reaches your machine "
                       "only via the Host Companion pairing (typed-confirm), never directly."),
        })

    headline = ("Your prompts stay on local models; your vault and credentials stay local."
                if all_local else
                f"Prompts + file excerpts you share transit {dest_txt}; your vault and credentials "
                "stay local.")
    headline += " What the tools and connected services reach is listed below."

    for s in sections:
        s["detail"] = _ascii(s["detail"])
        for it in s.get("items", []):
            it["text"] = _ascii(it["text"])

    return {
        "destinations": destinations, "local_llm": all_local,
        "os": profile.get("os_family"), "docker": bool(profile.get("docker_mode")),
        "enforcer": "available" if enforcer else "unavailable",
        "web": web,
        "headline": _ascii(headline), "sections": sections,
    }
