"""F8 — the Settings page may not assert a provider capability it never checked.

THE DEFECT (witnessed live on 2026-08-07): the "Provider credentials" table
decided every row with ``bool(getattr(config, attr, "").strip())``. Four of the
five providers are key-based, so a non-empty string really is the credential.
The fifth, **Ollama, is keyless** — its config attribute is a *base URL* that
``Config`` defaults to ``http://localhost:11434`` UNCONDITIONALLY. A sandbox
``.env`` holding only ``OPENROUTER_API_KEY`` still rendered "Ollama URL: OK Set"
on a box with no Ollama at all.

  * DEC-40 — a disclosure must ride a LIVENESS WITNESS, not a declaration.
  * DEC-43 (ii) — deriving from a PROXY ("a string is present") instead of the
    authority ("something answers as Ollama") is wrong from birth.
  * DEC-43 (i) — the same file also held TWO copies of the provider->attribute
    recipe (``_CRED_ROWS`` and ``_prov_to_attr``, 15 lines apart).

PROPERTY (quantified over every provider the Settings surface discloses):
    a provider is shown as configured ONLY IF the thing that makes it usable
    has actually been OBSERVED — a credential string for key-based providers,
    a live answer from the endpoint for a keyless one — and the per-provider
    satisfaction rule is declared in exactly ONE place.

FENCE: ``systemu.runtime.provider_status`` — one frozen ``PROVIDER_SPECS``
table carrying the rule per provider, and one mint (``provider_status``) that
is the only thing allowed to say "satisfied". ``ProviderStatus.satisfied`` is a
DERIVED property of ``state`` (a whitelist), so no probe and no caller can mint
a green verdict out of band.

WITNESS: ``test_live_url_string_alone_never_satisfies_the_keyless_provider``
and the two render pins below. Re-introduce ``satisfied = bool(value)`` for
Ollama tomorrow and they go red — the URL in those tests is non-empty and
well-formed, exactly like the shipped default, but nothing is listening on it.
"""
from __future__ import annotations

import ast
import inspect
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

from sharing_on.config import Config
from systemu.interface.pages import settings
from systemu.runtime import provider_status as ps


# ─────────────────────────────────────────────────────────────────────────────
#  Harness — a really-closed port, and a really-answering Ollama-shaped server.
#  Both are REAL sockets: the point of this module is that a *declaration* is
#  not evidence, so its own fixtures may not be declarations either.
# ─────────────────────────────────────────────────────────────────────────────

def _closed_port() -> int:
    """A port number nothing is listening on (bound, read, then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class _Ollamaish(BaseHTTPRequestHandler):
    payload = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]}
    status = 200

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        body = json.dumps(self.payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep pytest output clean
        return


class _NotOllama(_Ollamaish):
    """An HTTP server that answers — but is not an Ollama API."""
    payload = {"hello": "i am nginx"}


@pytest.fixture
def fake_ollama():
    """Yields a base URL served by an Ollama-shaped /api/tags."""
    srv = HTTPServer(("127.0.0.1", 0), _Ollamaish)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def wrong_server():
    srv = HTTPServer(("127.0.0.1", 0), _NotOllama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _cfg(**kw) -> Config:
    """A Config with the four keys empty unless a test sets one."""
    base = dict(openrouter_api_key="", google_api_key="", anthropic_api_key="",
                openai_api_key="")
    base.update(kw)
    return Config(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  THE WITNESS — a keyless provider is never satisfied by a string
# ─────────────────────────────────────────────────────────────────────────────

def test_live_url_string_alone_never_satisfies_the_keyless_provider():
    """MUTATION TARGET. ``bool(cfg.ollama_url)`` is True here — the URL is
    non-empty and well-formed, exactly like the shipped default. Nothing is
    listening on it. Anything that decides this row from the string goes red."""
    url = f"http://127.0.0.1:{_closed_port()}"
    cfg = _cfg(openrouter_api_key="sk-or-present", ollama_url=url)

    assert bool(cfg.ollama_url.strip()) is True, "the old proxy must still be 'true'"

    st = ps.all_provider_statuses(cfg, timeout=0.4)["ollama"]
    assert st.satisfied is False, st
    assert st.state == ps.STATE_UNREACHABLE, st


def test_the_shipped_default_url_is_not_evidence_of_anything():
    """``Config()`` invents ``http://localhost:11434`` with no operator input
    at all — the exact reason 'a string is present' is a meaningless proxy for
    this provider. Redirected to a dead port so the assertion is about the
    RULE, not about whether this machine happens to run Ollama."""
    assert Config().ollama_url == "http://localhost:11434"
    cfg = _cfg(ollama_url=f"http://127.0.0.1:{_closed_port()}")
    assert ps.all_provider_statuses(cfg, timeout=0.4)["ollama"].satisfied is False


def test_keyless_provider_is_satisfied_once_a_live_endpoint_answers(fake_ollama):
    cfg = _cfg(ollama_url=fake_ollama)
    st = ps.all_provider_statuses(cfg)["ollama"]
    assert st.state == ps.STATE_REACHABLE, st
    assert st.satisfied is True
    assert "2 model" in st.detail, st.detail


def test_an_http_server_that_is_not_ollama_does_not_count(wrong_server):
    """Something listening on the port is not the same fact as 'Ollama is
    usable' — the daemon-readiness mint refuses a squatter for the same
    reason (``daemon.probe_readiness``)."""
    cfg = _cfg(ollama_url=wrong_server)
    st = ps.all_provider_statuses(cfg)["ollama"]
    assert st.satisfied is False, st
    assert st.state == ps.STATE_UNREACHABLE, st


# ─────────────────────────────────────────────────────────────────────────────
#  The third state is honest, and it is NOT "configured"
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["", "   ", "localhost:11434", "file:///etc/passwd",
                                 "not a url at all", "ftp://x/y"])
def test_unprobeable_urls_read_unknown_and_are_not_satisfied(url):
    """UNKNOWN is 'we could not even ask'. Still not configured."""
    st = ps.all_provider_statuses(_cfg(ollama_url=url))["ollama"]
    assert st.state == ps.STATE_UNKNOWN, (url, st)
    assert st.satisfied is False, (url, st)


def test_a_silent_endpoint_reads_not_reachable_within_the_bound():
    """Measured on this box: a CLOSED loopback port is silently DROPPED, not
    refused (1.0 s for 127.0.0.1, 2.0 s for dual-stack `localhost`). Filing a
    timeout under `unknown` would push every machine without Ollama into the
    muted third state instead of telling the operator it is down."""
    url = f"http://127.0.0.1:{_closed_port()}"
    st = ps.all_provider_statuses(_cfg(ollama_url=url), timeout=0.4)["ollama"]
    assert st.state == ps.STATE_UNREACHABLE, st
    assert st.satisfied is False
    assert "ollama serve" in st.detail or "nothing is serving" in st.detail, st.detail


def test_a_probe_that_raises_can_never_reach_the_page():
    """CARE: a probe failure must not raise into the render."""
    def _boom(url, timeout):
        raise RuntimeError("kaboom")

    st = ps.provider_status(ps.SPEC_BY_PROVIDER["ollama"],
                            _cfg(ollama_url="http://127.0.0.1:11434"),
                            probe=_boom)
    assert st.state == ps.STATE_UNKNOWN and st.satisfied is False


def test_a_probe_cannot_mint_a_credential_state():
    """DEC-34: the verifier may not trust an operand the verified party
    controls. A probe returning the *credential* verdict ('set') — or any
    unrecognised token — is discarded, not honoured."""
    for bogus in ("set", "SATISFIED", "reachable ", "", None, 1):
        st = ps.provider_status(ps.SPEC_BY_PROVIDER["ollama"],
                                _cfg(ollama_url="http://127.0.0.1:11434"),
                                probe=lambda u, t, _b=bogus: (_b, "claimed"))
        assert st.satisfied is False, bogus
        assert st.state == ps.STATE_UNKNOWN, bogus


def test_satisfied_is_derived_from_state_not_stored_alongside_it():
    """A stored ``satisfied`` could drift out of step with ``state``; making it
    a read-only property over a whitelist means it structurally cannot."""
    assert isinstance(ps.ProviderStatus.__dict__["satisfied"], property)
    assert "satisfied" not in getattr(ps.ProviderStatus, "__dataclass_fields__", {})
    assert ps.SATISFIED_STATES == frozenset({ps.STATE_SET, ps.STATE_REACHABLE})


# ─────────────────────────────────────────────────────────────────────────────
#  Key-based providers keep the rule that IS right for them
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,attr,env", [
    ("openrouter", "openrouter_api_key", "OPENROUTER_API_KEY"),
    ("google", "google_api_key", "GOOGLE_API_KEY"),
    ("anthropic", "anthropic_api_key", "ANTHROPIC_API_KEY"),
    ("openai", "openai_api_key", "OPENAI_API_KEY"),
])
def test_key_providers_are_decided_by_the_credential(provider, attr, env):
    spec = ps.SPEC_BY_PROVIDER[provider]
    assert (spec.attr, spec.env, spec.rule) == (attr, env, ps.RULE_CREDENTIAL)
    _quiet = lambda _u, _t: (ps.STATE_UNKNOWN, "not probed")  # noqa: E731

    absent = ps.all_provider_statuses(_cfg(**{attr: "   "}), probe=_quiet)[provider]
    assert absent.satisfied is False and absent.state == ps.STATE_MISSING
    assert env in absent.detail

    present = ps.all_provider_statuses(_cfg(**{attr: "a-key"}), probe=_quiet)[provider]
    assert present.satisfied is True and present.state == ps.STATE_SET


def test_probing_is_never_done_for_a_key_provider():
    """A key-based row must not pay for (or be gated on) the network."""
    called = []
    ps.all_provider_statuses(_cfg(openrouter_api_key="k",
                                  ollama_url="http://127.0.0.1:11434"),
                             probe=lambda u, t: called.append(u) or ("unknown", "x"))
    assert called == ["http://127.0.0.1:11434"], called


def test_every_declared_provider_carries_a_satisfaction_rule():
    assert {s.provider for s in ps.PROVIDER_SPECS} == {
        "openrouter", "google", "anthropic", "openai", "ollama"}
    for s in ps.PROVIDER_SPECS:
        assert s.rule in (ps.RULE_CREDENTIAL, ps.RULE_REACHABILITY), s
        assert s.display and s.attr and s.env


def test_a_spec_with_no_rule_fails_closed():
    rogue = ps.ProviderSpec("mystery", "Mystery", "ollama_url", "MYSTERY_URL", "vibes")
    st = ps.provider_status(rogue, _cfg(ollama_url="http://127.0.0.1:11434"))
    assert st.satisfied is False and st.state == ps.STATE_UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
#  DEC-43 (i) — ONE table. The duplicate that was going to drift is gone.
# ─────────────────────────────────────────────────────────────────────────────

_SETTINGS_SRC = Path(inspect.getfile(settings)).read_text(encoding="utf-8")


def test_settings_no_longer_holds_its_own_provider_map():
    assert not hasattr(settings, "_CRED_ROWS")
    assert not hasattr(settings, "_prov_to_attr")


@pytest.mark.parametrize("literal", [
    "openrouter_api_key", "google_api_key", "anthropic_api_key",
    "openai_api_key", "ollama_url",
    "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "OLLAMA_URL",
])
def test_the_provider_recipe_appears_nowhere_in_the_settings_module(literal):
    """The drift fence. Every one of these lives in exactly one place now —
    ``provider_status.PROVIDER_SPECS`` — so a fifth provider, a renamed env var
    or a changed rule cannot be half-applied."""
    assert literal not in _SETTINGS_SRC, (
        f"{literal!r} is back in settings.py — that is the second copy of the "
        "recipe DEC-43 (i) forbids")


def test_the_recipe_appears_exactly_once_in_the_mint():
    src = Path(inspect.getfile(ps)).read_text(encoding="utf-8")
    for spec in ps.PROVIDER_SPECS:
        assert src.count(f'"{spec.attr}"') == 1, spec.attr
        assert src.count(f'"{spec.env}"') == 1, spec.env


def test_the_tier_dropdown_derives_from_the_same_table():
    assert set(settings._PROVIDER_OPTIONS) == {""} | {
        s.provider for s in ps.PROVIDER_SPECS}
    for s in ps.PROVIDER_SPECS:
        assert settings._PROVIDER_OPTIONS[s.provider] == s.display


# ─────────────────────────────────────────────────────────────────────────────
#  RENDER PINS — remove the production call site and these go red (the
#  standing rule: a reachability pin, not just a unit test).
# ─────────────────────────────────────────────────────────────────────────────

class _Node:
    def __init__(self, rec, kind):
        self._rec, self._kind = rec, kind

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def classes(self, *a, **k):
        if a:
            self._rec.classes.append(str(a[0]))
        return self

    def style(self, *a, **k):
        return self

    def props(self, *a, **k):
        return self

    def tooltip(self, *a, **k):
        return self

    def on(self, *a, **k):
        return self


class _Refreshable:
    """Stand-in for ``@ui.refreshable``: callable, and ``.refresh()`` re-runs
    it — which is what makes the SECOND (observed) paint a real call site."""

    def __init__(self, rec, fn):
        self._rec, self._fn = rec, fn

    def __call__(self, *a, **k):
        return self._fn(*a, **k)

    def refresh(self, *a, **k):
        self._rec.refreshes += 1
        return self._fn(*a, **k)


class _RecordingUI:
    """Records every ``ui.<fn>(...)`` the card makes. settings.py binds
    ``ui`` at module import, so we patch the attribute, not sys.modules."""

    def __init__(self):
        self.calls, self.classes, self.timers = [], [], []
        self.refreshes = 0

    # explicit members win over __getattr__
    def refreshable(self, fn):
        return _Refreshable(self, fn)

    def timer(self, interval, callback, **k):
        self.timers.append((interval, callback, k))
        return _Node(self, "timer")

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def reset(self):
        self.calls, self.classes = [], []

    def texts(self, fn=None):
        return [str(a[0]) for n, a, _k in self.calls
                if a and (fn is None or n == fn)]


def _render_card(cfg, *, observe: bool = True) -> _RecordingUI:
    """Execute the REAL card against a recording ``ui``.

    With ``observe`` the scheduled probe callback is then run and the recording
    reset, so the assertions see exactly the paint an operator ends up looking
    at — the whole production path, timer and refresh included.
    """
    import asyncio

    rec = _RecordingUI()
    with mock.patch.object(settings, "ui", rec):
        settings.provider_credentials_card(cfg)
        if observe:
            assert rec.timers, "the card never scheduled the reachability probe"
            _interval, cb, kw = rec.timers[0]
            assert kw.get("once") is True, kw
            rec.reset()
            asyncio.run(cb())
            assert rec.refreshes == 1, "the observed verdicts never re-rendered"
    return rec


def test_nothing_is_green_on_the_first_paint_before_the_probe_runs(fake_ollama):
    """The probe is bounded but not instant (a dual-stack ``localhost`` with
    nothing listening costs ~2 s here), so it runs OFF the render path. The
    consequence has to be fail-closed: even a genuinely reachable Ollama is
    not green until it has actually been observed."""
    rec = _render_card(_cfg(ollama_url=fake_ollama), observe=False)
    joined = " | ".join(rec.texts("label"))
    assert "Reachable" not in joined, joined
    assert not any("s-pill--success" in c for c in rec.classes), rec.classes


def test_the_card_renders_no_success_pill_for_an_unobserved_ollama():
    """THE RENDER PIN for the reported defect: a .env carrying only
    OPENROUTER_API_KEY, on a box with no Ollama. OpenRouter goes green,
    Ollama does not."""
    cfg = _cfg(openrouter_api_key="sk-or-present",
               ollama_url=f"http://127.0.0.1:{_closed_port()}")
    rec = _render_card(cfg)
    joined = " | ".join(rec.texts("label"))

    assert "Ollama" in joined, joined
    assert "Not reachable" in joined, joined
    # green appears exactly once on this card — for OpenRouter's key
    assert sum("s-pill--success" in c for c in rec.classes) == 1, rec.classes
    # ...and the operator is told WHY, naming the address that was probed
    assert any("127.0.0.1" in t for t in rec.texts("label")), joined


def test_the_card_renders_a_success_pill_once_ollama_answers(fake_ollama):
    cfg = _cfg(ollama_url=fake_ollama)
    rec = _render_card(cfg)
    joined = " | ".join(rec.texts("label"))
    assert "Reachable" in joined, joined
    assert sum("s-pill--success" in c for c in rec.classes) == 1, rec.classes


def test_the_card_flags_a_tier_pointed_at_an_unusable_provider():
    """The 'selected for a tier but not usable' banner now consumes the SAME
    mint — before, it re-derived from its own copy of the map and so called a
    dead Ollama 'set'."""
    cfg = _cfg(tier1_provider="ollama",
               ollama_url=f"http://127.0.0.1:{_closed_port()}")
    rec = _render_card(cfg)
    banner = [t for t in rec.texts("label") if "Selected for a tier" in t]
    assert banner, rec.texts("label")
    assert "ollama" in banner[0].lower(), banner


def test_the_card_stays_quiet_when_every_selected_tier_is_usable(fake_ollama):
    """EVERY tier, which is what this test always claimed and did not build.

    It used to pin tier 1 alone and expect silence, because the banner scored
    the explicit ``tier{N}_provider`` OVERRIDES and tiers 2-3 had none. Those
    tiers were never unselected, though: their MODELS are the shipped
    OpenRouter-served defaults and there is no OpenRouter key here, so they
    would have failed at call time while this card said nothing. That gap is
    the DEC-43 split closed in tests/test_provider_selection_parity.py -- the
    banner now scores what the ROUTER will really call for all three, so the
    quiet machine is the one where all three genuinely run.
    """
    cfg = _cfg(tier1_provider="ollama", tier2_provider="ollama",
               tier3_provider="ollama", ollama_url=fake_ollama)
    rec = _render_card(cfg)
    assert not [t for t in rec.texts("label") if "Selected for a tier" in t]


def test_the_unusable_tier_banner_waits_for_a_real_verdict(fake_ollama):
    """It must not shout 'those tiers will fail' off a placeholder — that
    would be the mirror image of the bug: a claim without an observation."""
    rec = _render_card(_cfg(tier1_provider="ollama", ollama_url=fake_ollama),
                       observe=False)
    assert not [t for t in rec.texts("label") if "Selected for a tier" in t]


def test_a_probe_blowing_up_never_raises_into_the_render():
    """CARE: the page must render even if the whole mint explodes."""
    import asyncio

    rec = _RecordingUI()
    with mock.patch.object(settings, "ui", rec), \
            mock.patch.object(settings._ps, "all_provider_statuses",
                              side_effect=RuntimeError("boom")):
        settings.provider_credentials_card(_cfg())   # first paint must survive
        assert rec.timers
        asyncio.run(rec.timers[0][1]())              # ...and so must the probe


def test_the_settings_page_actually_calls_the_card():
    """Delete the call site and this fails — the card being correct is worth
    nothing if the page stops rendering it."""
    tree = ast.parse(inspect.getsource(settings.build_settings_page))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "provider_credentials_card" in called, sorted(called)


def test_the_openrouter_key_section_agrees_with_the_table():
    """GATE-7a corollary: settings.py disclosed this one fact on TWO surfaces
    (the credentials table and the 'OpenRouter API Key' section). Both derive
    from the mint now, so they cannot contradict each other."""
    src = inspect.getsource(settings.build_settings_page)
    assert "config.openrouter_api_key" not in src, (
        "the API-key section re-derives instead of consuming the mint")
    assert "openrouter_key_status(" in src
    assert settings.openrouter_key_status(_cfg(openrouter_api_key="k"))[0].startswith("✓")
    assert not settings.openrouter_key_status(_cfg())[0].startswith("✓")
