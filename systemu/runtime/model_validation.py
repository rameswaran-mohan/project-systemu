"""W14 S4 — boundary validation + the validated-models trust anchor.

Validate at the boundary; record what passed so the runtime can tell a
validated-model-that-drifted (degrade + flag) from a never-validated model
(block). Native providers have no enumerable catalog, so validation differs
per provider (catalog / ping / ollama_tags) — recorded as ``validated_via``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Set, Tuple



def key_fingerprint(key: str) -> str:
    """Stable, non-reversible 12-char id for a key (NOT the key itself)."""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]


def validated_via_for(provider: str) -> str:
    p = (provider or "").lower()
    if p == "ollama":
        return "ollama_tags"
    if p in ("openrouter", ""):
        return "catalog"
    return "ping"


def _load(path: Path) -> list:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else []
    except Exception:
        return []


def record_validated(path: Path, *, tier: int, provider: str, model: str,
                     validated_via: str, key_fingerprint: str = "") -> None:
    """Append/replace the (provider, model) row in the trust anchor."""
    from datetime import datetime, timezone
    rows = [r for r in _load(path)
            if not (r.get("provider") == provider and r.get("model") == model)]
    rows.append({
        "tier": tier, "provider": provider, "model": model,
        "validated_via": validated_via, "key_fingerprint": key_fingerprint,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    })
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(rows, indent=2), encoding="utf-8")


def is_validated(path: Path, *, provider: str, model: str) -> bool:
    return any(r.get("provider") == provider and r.get("model") == model
               for r in _load(path))


def _prefix_mismatch(provider: str, model: str) -> bool:
    """W14 S8 seed: a NATIVE provider paired with a namespaced model id whose
    vendor prefix names a different vendor (e.g. provider=openai,
    model=deepseek/... or anthropic/...). 'auto'/'openrouter' never mismatch
    — OpenRouter serves vendor-namespaced ids. A bare native id (no '/',
    e.g. gpt-4, claude-sonnet-4.5) is fine."""
    p = (provider or "").lower()
    if p in ("", "auto", "openrouter"):
        return False
    if "/" in model:
        vendor = model.split("/", 1)[0].lower()
        return vendor != p
    return False


def _anthropic_importable() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _fetch_openrouter_catalog(key: str, *, timeout: int = 10) -> Set[str]:
    import urllib.request
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "systemu-validate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {m.get("id") for m in data.get("data", []) if m.get("id")}


def validate_model(*, provider: str, model: str, credential: str,
                   timeout: int = 10) -> Tuple[bool, str]:
    """Boundary check for one (provider, model). Returns (ok, reason).
    Never raises."""
    prov = (provider or "").lower() or "openrouter"
    if _prefix_mismatch(prov, model):
        return False, (f"Provider/model mismatch: '{model}' looks like a "
                       f"different provider than '{prov}'.")
    if prov in ("openrouter", "google", "openai", "anthropic") and not (credential or "").strip():
        return False, f"No API key configured for {prov}."
    if prov == "anthropic" and not _anthropic_importable():
        return False, ("The 'anthropic' provider needs the optional package — "
                       "install it with: pip install 'systemu[anthropic]'.")
    try:
        if prov == "openrouter":
            catalog = _fetch_openrouter_catalog(credential, timeout=timeout)
            return (True, "") if model in catalog else \
                (False, f"Model '{model}' is not available on OpenRouter.")
        if prov == "ollama":
            return _validate_ollama(model, credential or "http://localhost:11434", timeout)
        return _ping(prov, model, credential, timeout)
    except Exception as exc:
        return False, f"Could not validate {prov}/{model}: {exc}"


def _validate_ollama(model: str, base_url: str, timeout: int) -> Tuple[bool, str]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=timeout) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = {m.get("name") for m in tags.get("models", [])}
        bare = model.split("ollama/", 1)[-1]
        return (True, "") if (model in names or bare in names) else \
            (False, f"Ollama model '{bare}' is not pulled (run: ollama pull {bare}).")
    except Exception as exc:
        return False, f"Ollama unreachable at {base_url} ({exc}) — is `ollama serve` running?"


def _ping(provider: str, model: str, credential: str, timeout: int) -> Tuple[bool, str]:
    """1-token live completion against the chosen model (native providers
    have no catalog to list). Delegates to the router's ping helper; absent
    that, reports honestly rather than guessing."""
    try:
        from systemu.core.llm_router import _ping_model
        return _ping_model(provider=provider, model=model,
                           credential=credential, timeout=timeout)
    except ImportError:
        return False, (f"{provider} validation needs a live ping but the "
                       f"ping helper is unavailable.")
    except Exception as exc:
        return False, f"{provider} ping failed: {exc}"
