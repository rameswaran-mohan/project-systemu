import pytest
from dataclasses import is_dataclass
from systemu.llm.providers.base import BaseLLMProvider, LLMResponse


def test_llm_response_is_dataclass():
    assert is_dataclass(LLMResponse)
    r = LLMResponse(content="hi", model="x", usage={"input": 1, "output": 1})
    assert r.content == "hi"
    assert r.model == "x"
    assert r.usage == {"input": 1, "output": 1}


def test_llm_response_default_usage_and_raw():
    r = LLMResponse(content="hi", model="x")
    assert r.usage == {}
    assert r.raw is None


def test_base_provider_is_abstract():
    with pytest.raises(TypeError):
        BaseLLMProvider()  # type: ignore[abstract]


def test_base_provider_matches_classmethod_exists():
    assert hasattr(BaseLLMProvider, "matches")
    assert callable(BaseLLMProvider.matches)
