"""S4 perf+fix — the episodic-capture LLM key-guard (root cause of the ~380s
end-of-run stall).

``episodic_memory.capture`` ends a run with a Tier-1 ``llm_call_json`` summarize.
Gated ONLY on ``episodic_memory_enabled`` (default True) with NO provider-key
guard, a keyless/offline run (and the whole hermetic test suite — bare ``Config()``
+ empty keys, and the ``execute()`` tests patch only ``shadow_runtime``'s
``llm_call_json`` binding, not episodic_memory's OWN import) hit the router's real
network ladder for ~380s before failing. ``capture`` already degrades to ``None``
on that failure, so the ``_has_llm_provider`` short-circuit is behavior-equivalent
— just fast.

These are FAST unit tests (no execute()): they assert the guard SKIPS the LLM call
when no provider key is configured (returning the same degraded ``None``), and that
a configured key still lets the call through.
"""
from __future__ import annotations

import pytest

from systemu.runtime import episodic_memory


class _FakeVault:
    """A minimal vault stand-in for capture(): the idempotency read returns no
    existing summaries, and append is a no-op recorder."""

    def __init__(self):
        self.appended = []

    def query_session_summaries(self, limit=None):
        return []

    def append_session_summary(self, summary):
        self.appended.append(summary)


def _capture(vault, config):
    return episodic_memory.capture(
        vault=vault,
        session_id="sess_guard",
        intent="do a thing",
        chat_result="did the thing",
        files_produced=[],
        status="success",
        config=config,
    )


def test_no_provider_key_skips_llm_call_and_returns_none(monkeypatch):
    """A no-key Config() → capture SKIPS the LLM call entirely and returns the
    SAME degraded value (None) it already returns on a failed call. We patch the
    module's OWN llm_call_json binding to RAISE if invoked — asserting it is never
    called proves the guard fires before the doomed ~380s network ladder."""
    from sharing_on.config import Config

    def _boom(*a, **k):
        raise AssertionError(
            "llm_call_json must NOT be called when no provider key is configured "
            "— the key-guard short-circuit failed")

    monkeypatch.setattr(episodic_memory, "llm_call_json", _boom, raising=True)

    cfg = Config()  # all provider keys default to "" (no OPENROUTER/GOOGLE/etc.)
    assert episodic_memory._has_llm_provider(cfg) is False

    vault = _FakeVault()
    result = _capture(vault, cfg)

    # Behavior-equivalent to the existing degraded path: None, nothing persisted.
    assert result is None
    assert vault.appended == []


def test_provider_key_present_does_attempt_the_llm_call(monkeypatch):
    """With a provider key configured, the guard passes and capture DOES attempt
    the Tier-1 summarize call. We stub llm_call_json to a valid summary dict and
    assert it was invoked (and a SessionSummary is produced + persisted)."""
    from sharing_on.config import Config

    called = {"n": 0}

    def _stub(*a, **k):
        called["n"] += 1
        return {
            "outcome_summary": "summarized ok",
            "key_facts_learned": ["fact-1"],
            "tags": ["Alpha", "beta"],
        }

    monkeypatch.setattr(episodic_memory, "llm_call_json", _stub, raising=True)

    cfg = Config()
    cfg.openrouter_api_key = "sk-test-key"  # a configured provider key
    assert episodic_memory._has_llm_provider(cfg) is True

    vault = _FakeVault()
    result = _capture(vault, cfg)

    assert called["n"] == 1, "capture must attempt the LLM call when a key is present"
    assert result is not None
    assert result.session_id == "sess_guard"
    assert result.outcome_summary == "summarized ok"
    assert len(vault.appended) == 1


def test_has_llm_provider_detects_any_of_the_four_keys():
    """The guard is satisfied by ANY of the four provider keys (openrouter /
    google / anthropic / openai), and is defensive against a missing attr."""
    from sharing_on.config import Config

    assert episodic_memory._has_llm_provider(Config()) is False

    for attr in ("openrouter_api_key", "google_api_key",
                 "anthropic_api_key", "openai_api_key"):
        cfg = Config()
        setattr(cfg, attr, "some-value")
        assert episodic_memory._has_llm_provider(cfg) is True, attr

    # Whitespace-only is treated as unset.
    cfg = Config()
    cfg.openrouter_api_key = "   "
    assert episodic_memory._has_llm_provider(cfg) is False
