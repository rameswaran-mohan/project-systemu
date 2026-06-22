"""v0.9.5 T4 — mixture_of_agents tool tests."""
from unittest.mock import patch
import pytest


class TestMixtureOfAgents:
    def test_returns_majority_vote(self, monkeypatch):
        from systemu.runtime.tools.mixture_of_agents import mixture_of_agents
        # Mock LLM to return predictable verdicts: 2 yes, 1 no -> majority yes
        responses = iter([
            {"verdict": "yes", "reason": "looks good"},
            {"verdict": "yes", "reason": "passes check"},
            {"verdict": "no", "reason": "missing field"},
        ])
        monkeypatch.setattr(
            "systemu.runtime.tools.mixture_of_agents.llm_call_json",
            lambda **kw: next(responses),
        )
        from sharing_on.config import Config
        result = mixture_of_agents(
            query="is this complete?",
            config=Config(),
            n_peers=3,
        )
        assert result["majority_verdict"] == "yes"
        assert result["yes_count"] == 2
        assert result["no_count"] == 1
        assert len(result["per_peer_verdicts"]) == 3

    def test_tie_returns_no(self, monkeypatch):
        """On a tie, default to no (conservative — never credit on doubt)."""
        from systemu.runtime.tools.mixture_of_agents import mixture_of_agents
        responses = iter([
            {"verdict": "yes", "reason": "ok"},
            {"verdict": "no", "reason": "nope"},
        ])
        monkeypatch.setattr(
            "systemu.runtime.tools.mixture_of_agents.llm_call_json",
            lambda **kw: next(responses),
        )
        from sharing_on.config import Config
        result = mixture_of_agents(
            query="is this complete?",
            config=Config(),
            n_peers=2,
        )
        assert result["majority_verdict"] == "no"

    def test_lenses_are_threaded_to_llm(self, monkeypatch):
        """Each peer call must receive its assigned lens in the prompt."""
        from systemu.runtime.tools.mixture_of_agents import mixture_of_agents
        captured = []
        def fake_call(**kw):
            captured.append(kw.get("user", ""))
            return {"verdict": "yes", "reason": "ok"}
        monkeypatch.setattr(
            "systemu.runtime.tools.mixture_of_agents.llm_call_json",
            fake_call,
        )
        from sharing_on.config import Config
        mixture_of_agents(
            query="test query",
            config=Config(),
            n_peers=3,
            lenses=["correctness", "security", "completeness"],
        )
        joined = " | ".join(captured)
        assert "correctness" in joined
        assert "security" in joined
        assert "completeness" in joined

    def test_llm_exception_counts_as_no_verdict(self, monkeypatch):
        """When a peer LLM call fails, count it as 'no' (conservative)."""
        from systemu.runtime.tools.mixture_of_agents import mixture_of_agents
        responses = iter([
            {"verdict": "yes", "reason": "ok"},
            None,  # signal exception below
            {"verdict": "yes", "reason": "ok"},
        ])

        def fake_call(**kw):
            r = next(responses)
            if r is None:
                raise RuntimeError("LLM down")
            return r
        monkeypatch.setattr(
            "systemu.runtime.tools.mixture_of_agents.llm_call_json",
            fake_call,
        )
        from sharing_on.config import Config
        result = mixture_of_agents(
            query="test", config=Config(), n_peers=3,
        )
        assert result["yes_count"] == 2
        assert result["no_count"] == 1  # the exception counts as no
        assert result["majority_verdict"] == "yes"


class TestMixtureRegistered:
    def test_registered_in_v2_registry(self):
        from systemu.runtime.tool_registry_v2 import registry as singleton
        import systemu.runtime.tools.mixture_of_agents  # noqa: F401
        entry = singleton.get("mixture_of_agents")
        assert entry is not None
        assert entry.toolset == "verification"
