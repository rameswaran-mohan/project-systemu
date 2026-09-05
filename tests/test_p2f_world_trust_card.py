"""Phase 2f - the "What Systemu can touch" trust card.

EVERY clause consumes an EXISTING mint. A clause that cannot be backed by a
mint is OMITTED, never approximated - so these tests pin BOTH halves: the
clauses that must be present AND the omissions that must stay omissions.

The transmission clause ("what leaves this machine") is the important
omission. The pinned wording proposed for it ("Nothing else is sent
anywhere.") is CONTRADICTED by the tree - `web_access.read_url` relays every
fetch through the third-party `r.jina.ai`, `search_web` sends the operator's
QUERY there too, `find_places` reaches Nominatim/Overpass, and the shipped
`/privacy` mint (`runtime.privacy.privacy_report`) already says so. Until the
wording is ruled, the card carries no transmission claim at all and points at
`/privacy`, which is the surface that already tells the truth.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.interface import trust_card as tc


# -- fakes --------------------------------------------------------------------

class FakeVault:
    def __init__(self, rows):
        self._rows = rows

    def list_tools(self):
        return self._rows


class AngryVault:
    def list_tools(self):
        raise RuntimeError("backend is down")


class MutelessVault:
    """A vault backend that does not implement the API at all."""


def _config(**kw):
    base = dict(
        tier1_model="anthropic/claude-3", tier2_model="anthropic/claude-3",
        tier3_model="anthropic/claude-3",
        tier1_provider="", tier2_provider="", tier3_provider="",
        openrouter_api_key="", google_api_key="",
        anthropic_api_key="sk-ant-not-a-real-key", openai_api_key="",
        ollama_url="http://localhost:11434",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _by_id(model):
    return {c["id"]: c for c in model["clauses"]}


def _omitted_ids(model):
    return {o["id"] for o in model["omitted"]}


# -- shape --------------------------------------------------------------------

def test_model_returns_clauses_and_omissions(tmp_path):
    m = tc.trust_card_model(FakeVault([]), _config(),
                            vault_verdict=_fake_root(tmp_path))
    assert type(m) is dict
    assert type(m["clauses"]) is list
    assert type(m["omitted"]) is list
    for c in m["clauses"]:
        assert set(c) >= {"id", "label", "value"}
        for k in ("label", "value", "detail"):
            assert type(c.get(k, "")) is str
    for o in m["omitted"]:
        assert set(o) >= {"id", "reason"}


def test_every_rendered_string_is_ascii(tmp_path):
    m = tc.trust_card_model(FakeVault([{"enabled": True}]), _config(),
                            vault_verdict=_fake_root(tmp_path))
    for c in m["clauses"]:
        for k in ("label", "value", "detail"):
            assert str(c.get(k, "")).isascii(), c
    for o in m["omitted"]:
        assert o["reason"].isascii()
    assert m["title"].isascii()


def test_the_model_is_pure_no_nicegui_and_no_network():
    src = inspect.getsource(tc)
    for forbidden in ("nicegui", "ui.label", "urllib", "socket", "requests"):
        assert forbidden not in src, forbidden


def test_the_module_source_is_ascii():
    assert Path(tc.__file__).read_text(encoding="utf-8").isascii()


# -- clause: provider (provider_status mint) ---------------------------------

def _fake_root(tmp_path, refused=False):
    from systemu.runtime.vault_root import VaultRootVerdict
    return VaultRootVerdict(
        root=str(tmp_path / "systemu" / "vault"), home=str(tmp_path),
        source="default", package_dir=str(tmp_path / "pkg"),
        inside_package=refused, refused=refused,
        reason="REFUSED: test" if refused else "")


def test_provider_clause_names_the_routed_provider(tmp_path):
    """The mint is `provider_status.routed_tier_providers` - the SAME
    derivation the Settings credentials banner and /welcome step 1 use."""
    from systemu.runtime import provider_status as _ps
    cfg = _config()
    routed = _ps.routed_tier_providers(cfg)
    assert "anthropic" in routed, routed          # guards the fixture itself

    m = tc.trust_card_model(FakeVault([]), cfg, vault_verdict=_fake_root(tmp_path))
    c = _by_id(m)[tc.CLAUSE_PROVIDER]
    assert "Anthropic" in c["value"]


def test_provider_clause_follows_the_mint_when_the_route_changes(tmp_path):
    """A machine with only an OpenRouter key routes an anthropic model id
    THROUGH OpenRouter - the card must say what the router will really do,
    not what the model id looks like."""
    cfg = _config(anthropic_api_key="", openrouter_api_key="sk-or-not-real")
    m = tc.trust_card_model(FakeVault([]), cfg, vault_verdict=_fake_root(tmp_path))
    c = _by_id(m)[tc.CLAUSE_PROVIDER]
    assert "OpenRouter" in c["value"]
    assert "Anthropic" not in c["value"]


def test_provider_clause_is_omitted_when_the_mint_can_score_nothing(tmp_path):
    """No approximation: a route the mint cannot score yields NO clause."""
    m = tc.trust_card_model(
        FakeVault([]), _config(), vault_verdict=_fake_root(tmp_path),
        routed=["", "", ""])
    assert tc.CLAUSE_PROVIDER not in _by_id(m)
    assert tc.CLAUSE_PROVIDER in _omitted_ids(m)


def test_provider_clause_spends_no_probe_on_the_render_path():
    """DEFAULT_PROBE_TIMEOUT is real wall-clock and settings.py deliberately
    keeps it off the render path. The card must not reintroduce it."""
    src = inspect.getsource(tc.provider_clause)
    assert "all_provider_statuses" not in src
    assert "probe" not in src


# -- clause: vault root (vault_root mint) ------------------------------------

def test_vault_root_clause_renders_the_minted_path(monkeypatch, tmp_path):
    """Pinned against the MINT, from a foreign cwd with a relative env var -
    the exact shape of the vault-root defect - so a package-tree constant
    cannot pass."""
    from systemu.runtime import vault_root as vr
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(vr.VAULT_DIR_ENV, "my_vault")
    expected = vr.resolve_vault_root().root
    assert str(tmp_path) in expected                # guards the fixture

    m = tc.trust_card_model(FakeVault([]), _config())
    c = _by_id(m)[tc.CLAUSE_VAULT_ROOT]
    assert c["value"] == expected


def test_vault_root_clause_carries_the_refusal_bit(tmp_path):
    """DEC-32: the fence is a VALUE. The card reads `refused` in the same
    frame it renders the path, so it can never show a refused root as if it
    were the operating vault."""
    ok = tc.trust_card_model(FakeVault([]), _config(),
                             vault_verdict=_fake_root(tmp_path))
    assert _by_id(ok)[tc.CLAUSE_VAULT_ROOT]["refused"] is False

    bad = tc.trust_card_model(FakeVault([]), _config(),
                              vault_verdict=_fake_root(tmp_path, refused=True))
    c = _by_id(bad)[tc.CLAUSE_VAULT_ROOT]
    assert c["refused"] is True
    assert "not in use" in c["detail"].lower() or "refused" in c["detail"].lower()


# -- clause: tools (vault.list_tools aggregation) ----------------------------

def test_tools_clause_counts_enabled_and_not_enabled(tmp_path):
    rows = [{"enabled": True}, {"enabled": True}, {"enabled": False},
            {"enabled": None}]
    m = tc.trust_card_model(FakeVault(rows), _config(),
                            vault_verdict=_fake_root(tmp_path))
    c = _by_id(m)[tc.CLAUSE_TOOLS]
    assert c["total"] == 4
    assert c["enabled"] == 2
    assert c["not_enabled"] == 2
    assert "4" in c["value"] and "2" in c["value"]


def test_tools_clause_does_not_invent_an_enabled_tool(tmp_path):
    """A header without the key is NOT enabled. `enabled` is the header field
    both backends write (`vault._tool_header` / `sqlite._tool_header`); a
    missing one is unknown, and unknown is never counted as usable."""
    m = tc.trust_card_model(FakeVault([{}, {"enabled": "yes"}]), _config(),
                            vault_verdict=_fake_root(tmp_path))
    c = _by_id(m)[tc.CLAUSE_TOOLS]
    assert c["total"] == 2
    assert c["enabled"] == 0


@pytest.mark.parametrize("vault", [AngryVault(), MutelessVault(), None])
def test_tools_clause_is_omitted_when_the_store_cannot_answer(vault, tmp_path):
    m = tc.trust_card_model(vault, _config(), vault_verdict=_fake_root(tmp_path))
    assert tc.CLAUSE_TOOLS not in _by_id(m)
    assert tc.CLAUSE_TOOLS in _omitted_ids(m)


# -- clause: capture (platform_profile mint) ---------------------------------

def test_capture_clause_reads_the_platform_profile(tmp_path):
    from systemu.runtime.platform_profile import platform_profile
    native = platform_profile(platform_str="win32", in_container=False,
                              provider_configured=False)
    boxed = platform_profile(platform_str="linux", in_container=True,
                             provider_configured=False)
    assert native["capture_available"] is True      # guards the fixture
    assert boxed["capture_available"] is False

    on = tc.trust_card_model(FakeVault([]), _config(), profile=native,
                             vault_verdict=_fake_root(tmp_path))
    off = tc.trust_card_model(FakeVault([]), _config(), profile=boxed,
                              vault_verdict=_fake_root(tmp_path))
    assert _by_id(on)[tc.CLAUSE_CAPTURE]["available"] is True
    assert _by_id(off)[tc.CLAUSE_CAPTURE]["available"] is False
    assert "Host Companion" in _by_id(off)[tc.CLAUSE_CAPTURE]["detail"]


def test_capture_clause_is_omitted_when_the_profile_lacks_the_key(tmp_path):
    m = tc.trust_card_model(FakeVault([]), _config(), profile={},
                            vault_verdict=_fake_root(tmp_path))
    assert tc.CLAUSE_CAPTURE not in _by_id(m)
    assert tc.CLAUSE_CAPTURE in _omitted_ids(m)


# -- the transmission clause: OMITTED, and it stays omitted ------------------

def test_the_transmission_clause_is_omitted_with_its_reason(tmp_path):
    m = tc.trust_card_model(FakeVault([]), _config(),
                            vault_verdict=_fake_root(tmp_path))
    assert tc.CLAUSE_TRANSMISSION not in _by_id(m)
    o = [x for x in m["omitted"] if x["id"] == tc.CLAUSE_TRANSMISSION]
    assert len(o) == 1
    assert "r.jina.ai" in o[0]["reason"]


def test_the_card_makes_no_nothing_else_is_sent_claim(tmp_path):
    """The false sentence must not appear anywhere the operator can read it -
    not in the model, not in the renderer's source."""
    m = tc.trust_card_model(FakeVault([{"enabled": True}]), _config(),
                            vault_verdict=_fake_root(tmp_path))
    blob = " ".join(str(c.get(k, "")) for c in m["clauses"]
                    for k in ("label", "value", "detail"))
    for lie in ("Nothing else is sent anywhere",
                "nothing else is sent",
                "reach the sites they name"):
        assert lie.lower() not in blob.lower()

    from systemu.interface.pages import settings
    ssrc = inspect.getsource(settings.world_trust_card)
    assert "Nothing else is sent anywhere" not in ssrc


def test_the_privacy_page_still_contradicts_the_proposed_sentence():
    """The evidence, pinned. If a future change makes the relay untrue, THIS
    test goes red and the omission can be revisited on purpose rather than by
    someone re-reading a stale comment."""
    from systemu.runtime import web_access
    src = inspect.getsource(web_access.read_url)
    assert "r.jina.ai" in src

    ssrc = inspect.getsource(web_access.search_web)
    assert "r.jina.ai" in ssrc


# -- reachability pins (AST; mutation-checked) -------------------------------

def _calls_in(module, func_name: str) -> set:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        names.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        names.add(f.attr)
            return names
    raise AssertionError(f"{func_name} not found in {module.__name__}")


def test_settings_page_renders_the_card():
    """Reachability pin. Delete the `world_trust_card()` call from
    `build_settings_page` and this goes red - a model nothing consumes is the
    half-built shape this repo keeps shipping."""
    from systemu.interface.pages import settings
    assert "world_trust_card" in _calls_in(settings, "build_settings_page")


def test_the_renderer_consumes_the_model_function():
    from systemu.interface.pages import settings
    assert "trust_card_model" in _calls_in(settings, "world_trust_card")


def test_the_card_points_at_the_page_that_tells_the_egress_truth(tmp_path):
    """The route literal lives in ONE place (trust_card.PRIVACY_ROUTE) and the
    renderer consumes it, so the card cannot drift onto a second link."""
    assert tc.PRIVACY_ROUTE == "/privacy"
    m = tc.trust_card_model(FakeVault([]), _config(),
                            vault_verdict=_fake_root(tmp_path))
    assert m["privacy_route"] == "/privacy"

    from systemu.interface.pages import settings
    src = inspect.getsource(settings.world_trust_card)
    assert "privacy_route" in src


def test_the_card_section_does_not_touch_the_persona_switcher():
    """Merge hygiene: the card is its own section."""
    from systemu.interface.pages import settings
    src = inspect.getsource(settings.world_trust_card)
    assert "persona" not in src.lower()
