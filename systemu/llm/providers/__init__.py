"""v0.7-e: provider registry + dispatcher.

WHO KNOWS WHETHER A PROVIDER'S SDK IS THERE (e2e31)
---------------------------------------------------
Three of the five providers ride the CORE ``openai`` client and a fourth is
core ``httpx``; ``AnthropicProvider`` is the only class that imports an
OPTIONAL SDK, and it does so inside ``__init__`` so the module still imports on
an interpreter that has never seen the package.

That used to be guarded here by

    try:
        from .anthropic import AnthropicProvider
        _ANTHROPIC_AVAILABLE = True
    except ImportError:
        ...

which could NEVER FIRE -- nothing at import time of ``.anthropic`` touches the
SDK.  So the class was always registered, the router always picked it, the
provider mint always called the key satisfied, and the tier died with a bare
``ModuleNotFoundError`` at the first real call.  A guard that cannot fire is
worse than none: every surface downstream believes the question was asked.

The question is now asked for real, once, HERE, and the answer is a VALUE
(:func:`sdk_importable`) rather than an exception crossing the boundary -- a
raise is no fence when any frame between it and the decision may catch it
(DEC-32).  ``systemu.runtime.provider_status`` consumes it, which is what makes
"Provider ready" and the health banner honest on that machine.

THE CLASS STAYS IN THE REGISTRY.  Dropping it when the SDK is absent would
reroute ``anthropic/*`` to the OpenRouter catch-all, and every operator-facing
warning would then be about the wrong provider -- OpenRouter's missing key
instead of the Anthropic SDK the operator actually needs.  Selection and
usability are different facts and the mint scores the second one.
"""
import importlib.util

from .base import BaseLLMProvider, LLMResponse  # noqa: F401
from .openrouter import OpenRouterProvider
from .google import GoogleProvider
from .openai import OpenAIProvider
from .ollama import OllamaProvider
from .anthropic import AnthropicProvider

#: Provider id -> the pip DISTRIBUTION whose SDK that provider's class imports
#: at CONSTRUCTION time.  A provider absent from this table needs no optional
#: package of its own, and :func:`sdk_importable` says so without asking the
#: filesystem -- declaring one that does not need an SDK would refuse a
#: provider that works.
#:
#: The distribution name is what ``pip install`` and ``pyproject`` spell, so it
#: is also the key ``systemu.runtime.optional_deps`` uses to look up the
#: remedy.  The IMPORT name is tracked separately because the two differ for
#: plenty of packages (``python-docx`` -> ``docx``); they happen to coincide
#: for the only entry today, and writing one field would have hidden that.
_PROVIDER_SDK: dict = {
    # provider id -> (distribution name, top-level import name)
    "anthropic": ("anthropic", "anthropic"),
}

#: RETAINED NAME, NOT A GATE.  It used to mean "the try/except above did not
#: fire", which was true on every machine ever.  The registry below no longer
#: consults it and neither does anything else in the tree; the live answer to
#: "can this install actually use the Anthropic provider" is
#: :func:`sdk_importable`.  Kept only because ``tests/test_v070_provider_
#: registry.py`` binds to it while rebuilding ``_REGISTRY`` by hand.
_ANTHROPIC_AVAILABLE = True

# Order matters for matches() — most specific first, catch-all (OpenRouter) last
_REGISTRY: list = [
    GoogleProvider,
    AnthropicProvider,
    OpenAIProvider,
    OllamaProvider,
    OpenRouterProvider,
]


def _find_spec(module_name: str):
    """THE SEAM.  The one place this package asks the interpreter for a module.

    Module-level so a test can flip the world in both directions without
    touching ``sys.modules`` or the developer's site-packages, and so there is
    exactly one thing to fake.  ``importlib.util.find_spec`` does not IMPORT
    the module -- it only resolves it -- which is what makes it safe to call
    from a render path and from the boot gate.
    """
    return importlib.util.find_spec(module_name)


def sdk_package(provider: str) -> str:
    """The pip distribution ``provider``'s class imports, or "" if it needs none.

    A VALUE, never a raise.  "" is the answer for four of the five providers
    and means "there is nothing optional here to be missing".
    """
    pid = provider.strip().lower() if type(provider) is str else ""
    req = _PROVIDER_SDK.get(pid)
    if type(req) is not tuple:
        return ""
    return req[0] if type(req[0]) is str else ""


def sdk_importable(provider: str) -> bool:
    """Can THIS interpreter import the SDK ``provider``'s class needs?

    ``True`` for a provider that needs no optional SDK -- it has nothing to
    miss.  Otherwise the seam is asked and the answer is a plain bool.

    FAILS CLOSED.  ``find_spec`` raises for a partially installed or broken
    namespace package, and an id we cannot even normalise is not a provider we
    can vouch for; both read as NOT importable, because "we could not tell"
    must never render as ready.  Never raises: the caller is a disclosure
    surface, and an exception here would either take a dashboard down or be
    swallowed somewhere upstream into the very "looks fine" this closes.
    """
    pid = provider.strip().lower() if type(provider) is str else ""
    if not pid:
        return False
    req = _PROVIDER_SDK.get(pid)
    if req is None:
        return True
    if type(req) is not tuple or type(req[1]) is not str:
        return False
    try:
        return _find_spec(req[1]) is not None
    except Exception:
        return False


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
