"""Channel convergence (pro <- public): three functional fixes the public tree
carried that the private tree never received, plus one operator-facing string
that leaked the name of a private document.

1. STALE HARDCODED MODEL DEFAULTS.
   `sharing_on.analyzer.generator.generate_instructions` and
   `sharing_on.analyzer.intent_extractor.extract_intent` each carried a frozen
   model id as their `model=` default. Production callers pass
   `config.tier3_model` / `config.tier2_model`, so the default only bites
   standalone, fallback and test calls -- and it pinned those to ids the
   tier/preset system had already moved off (`openai/gpt-4o-mini` and
   `google/gemini-2.0-flash-exp:free`; the latter is the same dead-id class as
   the `z-ai/glm-4.5-air:free` that W11.7 had to rip out of the preset table
   after OpenRouter started 404-ing it). The fix: `model=None` routes the
   default through `sharing_on.model_presets.resolve_preset(os.environ)`.

2. PRIVATE URL ON THE WIRE.
   The `fetch_json` default User-Agent named the PRIVATE repository URL and
   sent it to every third-party server the agent fetches from. It now names
   the public PyPI project.

3. PRIVATE DOCUMENT NAME IN AN OPERATOR-FACING REFUSAL.
   `MODALITY_NOT_ADMISSIBLE` is the reason string an operator READS when an MCP
   effect is refused. It cited an untracked internal planning document by name, one no
   operator can open. The citation is now "spec". (Comments in that module keep
   their internal refs -- a comment is not an operator surface.)

SEAM (no network, no product stub)
    Both analyzer entry points expose the RESOLVED model at their LLM call:
    each does a module-level `from openai import OpenAI`, so patching
    `<module>.OpenAI` -- the third-party network client, never product code --
    with a recorder yields the exact `model=` the function resolved. This is
    the seam `tests/test_v060a_intent_extractor.py` already uses. The preset
    side is exercised for REAL: the env var is set, `resolve_preset` itself is
    never replaced.

WITNESS / REACHABILITY PIN
    * Restore either hardcoded default -> the matching
      `test_*_default_model_routes_through_preset` goes red. No entry in
      `model_presets.PRESETS` resolves to either stale id, in any preset, so
      the red is structural and not a coincidence of today's table (that is
      what `test_no_preset_resolves_to_either_stale_hardcoded_id` guards).
    * Restore the private-repo User-Agent -> the two
      `test_fetch_json_default_user_agent_*` tests go red.
    * Restore the internal document name in the refusal string ->
      `test_modality_refusal_names_no_private_document` goes red.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from sharing_on.model_presets import PRESETS, resolve_preset

# The ids that were hardcoded before convergence. Named here once so the
# mutation witness is explicit, and so the "no preset resolves to these"
# structural check below has something concrete to check.
_STALE_TIER3_DEFAULT = "openai/gpt-4o-mini"
_STALE_TIER2_DEFAULT = "google/gemini-2.0-flash-exp:free"

# "" means: no SYSTEMU_MODEL_PRESET at all (the shipped budget defaults).
_PRESET_NAMES = ("", "budget", "balanced", "quality")


def _set_preset(monkeypatch, name):
    if name:
        monkeypatch.setenv("SYSTEMU_MODEL_PRESET", name)
    else:
        monkeypatch.delenv("SYSTEMU_MODEL_PRESET", raising=False)


# ---------------------------------------------------------------------------
# 1. analyzer default-model routing
# ---------------------------------------------------------------------------

def _fake_step():
    s = MagicMock()
    s.step_number = 1
    s.label = None
    s.primary_app = "Chrome"
    s.start_time = None
    s.duration_seconds = 1.0
    s.events = []
    s.event_summary = {"file": 1}
    return s


def _fake_event():
    ev = MagicMock()
    ev.application = "Chrome"
    ev.action = None
    ev.file_path = None
    ev.category = None
    ev.url = None
    ev.data = {}
    return ev


def _model_reaching_generator_llm(**call_kwargs):
    """Return the `model=` that `generate_instructions` handed the client."""
    from sharing_on.analyzer.generator import generate_instructions

    seen = {}
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "# mocked instructions"
    resp.usage = None

    with patch("sharing_on.analyzer.generator.OpenAI") as OpenAI_mock:
        client = MagicMock()

        def _create(**kw):
            seen["model"] = kw.get("model")
            return resp

        client.chat.completions.create.side_effect = _create
        OpenAI_mock.return_value = client

        generate_instructions(
            steps=[_fake_step()],
            session_name="s",
            platform_info="p",
            duration_seconds=1.0,
            api_key="k",
            **call_kwargs,
        )

    assert "model" in seen, (
        "generate_instructions never reached the LLM call seam -- the test "
        "cannot observe the resolved model"
    )
    return seen["model"]


def _model_reaching_extractor_llm(**call_kwargs):
    """Return the `model=` that `extract_intent` handed the client."""
    from sharing_on.analyzer.intent_extractor import extract_intent

    seen = {}
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps(
        {
            "intent": "document the weather",
            "expected_outcome": "a file exists",
            "success_signal": "file exists",
            "abstracted_steps": ["look up the weather"],
            "confidence": "high",
        }
    )

    with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
        client = MagicMock()

        def _create(**kw):
            seen["model"] = kw.get("model")
            return resp

        client.chat.completions.create.side_effect = _create
        OpenAI_mock.return_value = client

        extract_intent(
            steps=[_fake_step()],
            events=[_fake_event()],
            session_name="s",
            platform_info="p",
            api_key="k",
            **call_kwargs,
        )

    assert "model" in seen, (
        "extract_intent never reached the LLM call seam -- the test cannot "
        "observe the resolved model"
    )
    return seen["model"]


def test_no_preset_resolves_to_either_stale_hardcoded_id():
    """Structural backing for the mutation witness.

    The routing tests assert the resolved model EQUALS the preset value. That
    assertion only detects a RESTORED hardcoded default while no preset happens
    to carry the same id. Pin that here, so a future preset-table edit which
    would silence the witness fails loudly instead of quietly.
    """
    for name, table in PRESETS.items():
        for tier, model in table.items():
            assert model != _STALE_TIER3_DEFAULT, (name, tier)
            assert model != _STALE_TIER2_DEFAULT, (name, tier)
    # ...and the no-preset (budget) path, resolved the way production does.
    budget = resolve_preset({})
    assert budget["tier3"] != _STALE_TIER3_DEFAULT
    assert budget["tier2"] != _STALE_TIER2_DEFAULT


@pytest.mark.parametrize("preset", _PRESET_NAMES)
def test_generator_default_model_routes_through_preset(monkeypatch, preset):
    """`model=None` (the default) resolves to the tier-3 preset model."""
    _set_preset(monkeypatch, preset)
    expected = resolve_preset(os.environ)["tier3"]

    assert _model_reaching_generator_llm() == expected
    assert _model_reaching_generator_llm(model=None) == expected


@pytest.mark.parametrize("preset", _PRESET_NAMES)
def test_extractor_default_model_routes_through_preset(monkeypatch, preset):
    """`model=None` (the default) resolves to the tier-2 preset model."""
    _set_preset(monkeypatch, preset)
    expected = resolve_preset(os.environ)["tier2"]

    assert _model_reaching_extractor_llm() == expected
    assert _model_reaching_extractor_llm(model=None) == expected


def test_generator_explicit_model_still_wins(monkeypatch):
    """Production passes config.tier3_model -- routing must not override it."""
    _set_preset(monkeypatch, "quality")
    assert _model_reaching_generator_llm(model="vendor/explicit-3") == "vendor/explicit-3"


def test_extractor_explicit_model_still_wins(monkeypatch):
    """Production passes config.tier2_model -- routing must not override it."""
    _set_preset(monkeypatch, "quality")
    assert _model_reaching_extractor_llm(model="vendor/explicit-2") == "vendor/explicit-2"


# ---------------------------------------------------------------------------
# 2. fetch_json default User-Agent
# ---------------------------------------------------------------------------

class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True}


def _load_fetch_json():
    import systemu

    p = (
        pathlib.Path(systemu.__file__).parent
        / "vault"
        / "tools"
        / "implementations"
        / "fetch_json.py"
    )
    spec = importlib.util.spec_from_file_location("fetch_json_convergence_uut", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_user_agent(monkeypatch):
    """The UA the module ACTUALLY defaults, read by RUNNING it -- not by regex."""
    import requests

    captured = {}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["headers"] = dict(headers or {})
        return _Resp()

    monkeypatch.setattr(requests, "get", _fake_get)
    out = _load_fetch_json().run(url="https://example.invalid/api")

    assert out["success"] is True, out
    return captured["headers"].get("User-Agent", "")


#: The one User-Agent the fetcher is allowed to default to.
#:
#: Pinned as an EQUALITY, not as the absence of a particular private string.
#: Two reasons, and the second is why this changed:
#:   * an allowlist of one is strictly stronger -- "not the private repository"
#:     passed for every other leak, including a personal account name;
#:   * this file ships inside the sdist, so an assertion written as
#:     ``assert "<private name>" not in ua`` publishes the very name it exists
#:     to forbid.  A property about a private string must not be stated by
#:     spelling it.
_EXPECTED_DEFAULT_USER_AGENT = "systemu/0.9 (+https://pypi.org/project/systemu)"


def test_fetch_json_default_user_agent_names_no_private_repository(monkeypatch):
    ua = _default_user_agent(monkeypatch)
    assert ua == _EXPECTED_DEFAULT_USER_AGENT, (
        "the default User-Agent is sent to every third-party server the agent "
        "fetches from, so it may name the public project and nothing else; "
        "expected " + repr(_EXPECTED_DEFAULT_USER_AGENT) + ", got " + repr(ua)
    )
    # A source host is where the private names live; the public project is
    # named by its index page, never by a repository URL.
    assert "github.com" not in ua, repr(ua)


def test_fetch_json_default_user_agent_names_the_public_project(monkeypatch):
    ua = _default_user_agent(monkeypatch)
    assert "pypi.org/project/systemu" in ua, repr(ua)


# ---------------------------------------------------------------------------
# 3. operator-facing MCP refusal string
# ---------------------------------------------------------------------------

def test_modality_refusal_names_no_private_document():
    from systemu.runtime.mcp.dispatch import MODALITY_NOT_ADMISSIBLE

    assert "MASTER-" not in MODALITY_NOT_ADMISSIBLE, (
        "the refusal an operator reads cites an untracked internal document: "
        + repr(MODALITY_NOT_ADMISSIBLE)
    )
    # The citation must survive, only its name changes -- a refusal that cites
    # nothing is not an improvement over one that cites the wrong thing.
    assert "15.1(b)" in MODALITY_NOT_ADMISSIBLE
