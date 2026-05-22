"""provider registry + dispatcher."""
from .base import BaseLLMProvider, LLMResponse  # noqa: F401
from .openrouter import OpenRouterProvider
from .google import GoogleProvider
from .openai import OpenAIProvider
from .ollama import OllamaProvider

# Anthropic is optional — import inside try so missing dep doesn't break registry
try:
    from .anthropic import AnthropicProvider
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    AnthropicProvider = None  # type: ignore[assignment]
    _ANTHROPIC_AVAILABLE = False

# Order matters for matches() — most specific first, catch-all (OpenRouter) last
_REGISTRY: list = [
    GoogleProvider,
    *([AnthropicProvider] if _ANTHROPIC_AVAILABLE else []),
    OpenAIProvider,
    OllamaProvider,
    OpenRouterProvider,
]


def _by_name() -> dict:
    """Lazy-evaluated for tests that swap registry contents via monkeypatch."""
    return {cls.__name__.replace("Provider", "").lower(): cls
            for cls in _REGISTRY}


def resolve_provider_class(
    model_name: str,
    override_name: str | None = None,
) -> type:
    """Return the provider class that should handle this model.

    If override_name is set (from env), that wins. Otherwise dispatch by
    matches() in registry order; falls back to OpenRouter as catch-all.
    """
    if override_name:
        key = override_name.lower()
        names = _by_name()
        if key not in names:
            raise ValueError(
                f"unknown provider {override_name!r}; "
                f"valid: {sorted(names.keys())}"
            )
        return names[key]
    for cls in _REGISTRY:
        if cls.matches(model_name):
            return cls
    return OpenRouterProvider  # final fallback
