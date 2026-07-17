"""R-P3b — the "What leaves this machine" privacy report (§15.7 interim honesty rule).

PURE data over the platform profile + the configured LLM tiers (the page just renders
it). It states PLAINLY the machine's actual egress reality: until a private-compute
mode exists, "local-first" here means CUSTODY (your vault), VERIFICATION (receipts),
and the boundary — NOT zero egress. Truthful pre- AND post-S2 (the OS egress jail) —
AC6. Never prints a key, only reasons about presence/locality.

Accuracy notes (from the truthfulness audit — a privacy page that over-claims is a
trust failure):
  * Locality is PER-TIER. The agent runs three model tiers; "nothing leaves for the
    LLM" is asserted ONLY when EVERY tier is a local (ollama) model — a local tier-1
    with a remote tool-forge/formatting tier still egresses.
  * We name the actual network DESTINATION (openrouter.ai for the flash/catch-all
    ids, anthropic for native claude), not the model vendor.
  * "Encrypted" secrets is a WHITELIST of the known OS stores — the plaintext-file
    fallback (``plaintext_fallback``) and anything unknown is flagged, never assumed
    safe.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from systemu.runtime.platform_profile import (
    KEYRING_DPAPI, KEYRING_KEYCHAIN, KEYRING_SECRETSERVICE,
)

# Whitelist: ONLY these are genuine OS secret stores. Anything else (the
# plaintext_fallback 0600 file, unknown) is flagged — never assumed encrypted.
_ENCRYPTED_BACKENDS = frozenset({KEYRING_DPAPI, KEYRING_KEYCHAIN, KEYRING_SECRETSERVICE})


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
    """True iff this model runs on THIS machine (no LLM egress) — the MODEL-MATRIX
    ``local_capable`` class (ollama/*)."""
    try:
        from sharing_on import model_presets  # type: ignore
        return model_presets.locality_of(model_id) == "local_capable"
    except Exception:
        return str(model_id or "").lower().startswith("ollama/")


def _destination(model_id: str) -> Optional[str]:
    """The network party that RECEIVES this tier's prompts — None if local. Native
    claude → 'anthropic'; everything else (deepseek/google/openai/… flash + catch-all)
    routes through OpenRouter."""
    m = str(model_id or "").lower()
    if _is_local(m):
        return None
    if m.startswith("anthropic/"):
        return "anthropic"
    return "openrouter.ai"


def privacy_report(*, profile: Optional[Dict[str, Any]] = None,
                   tier_models: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The 'what leaves this machine' report. ``profile`` defaults to the live
    platform profile; ``tier_models`` to the resolved per-tier model ids.
    Deterministic + hermetic — safe to unit-test with fixtures (AC6)."""
    if profile is None:
        from systemu.runtime.platform_profile import platform_profile
        profile = platform_profile()
    if tier_models is None:
        tier_models = _tier_models()

    all_local = bool(tier_models) and all(_is_local(m) for m in tier_models.values())
    any_local = any(_is_local(m) for m in tier_models.values())
    destinations = sorted({d for m in tier_models.values() if (d := _destination(m))})
    dest_txt = ", ".join(destinations) or "the configured provider"

    jail = str(profile.get("forged_net_jail") or "absent")
    jailed = jail != "absent"
    backend = str(profile.get("keyring_backend") or "none").lower()
    encrypted_secrets = backend in _ENCRYPTED_BACKENDS

    sections = []

    # 1. Model calls — the headline "what leaves" (per-tier honest).
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
                       "to be processed — so data still leaves this machine."),
        })
    else:
        sections.append({
            "key": "llm", "title": "Model calls", "status": "leaves", "severity": "warn",
            "detail": (f"Your prompts AND any file excerpts you give the agent transit {dest_txt} "
                       "to be processed — this is the main thing that leaves your machine. "
                       "Local-first here means custody + verification, not zero egress."),
        })

    # 2. Secrets at rest (whitelist — plaintext/unknown flagged).
    sections.append({
        "key": "secrets", "title": "Secrets at rest",
        "status": "encrypted" if encrypted_secrets else "plaintext",
        "severity": "ok" if encrypted_secrets else "warn",
        "detail": (f"API keys and credentials are held in the OS secret store ({backend})."
                   if encrypted_secrets else
                   f"Credentials fall back to a local file ({backend}) — NOT OS-encrypted. Flagged."),
    })

    # 3. Outbound network sandbox (S2) — truthful pre AND post.
    sections.append({
        "key": "sandbox", "title": "Outbound network",
        "status": "sandboxed" if jailed else "unsandboxed",
        "severity": "ok" if jailed else "info",
        "detail": (f"Agent-forged network access is OS-sandboxed ({jail})."
                   if jailed else
                   "There is no OS-level egress jail yet; the boundary is the forged-network "
                   "hard-DENY — a forged tool that declares network access is refused unless you "
                   "approve it. An honest interim posture, not a sandbox."),
    })

    # 4. Tool network access — the OTHER egress, with the DEFAULT third parties named.
    sections.append({
        "key": "tools", "title": "What the agent's tools reach", "status": "gated", "severity": "info",
        "detail": ("Beyond the model, tools you run reach the network. By DEFAULT a web fetch is "
                   "relayed through r.jina.ai (a third-party reader that sees the target URL and the "
                   "page content), web search goes to DuckDuckGo, and place lookups go to "
                   "OpenStreetMap (Nominatim / Overpass); any connected MCP servers reach their own "
                   "hosts. A forged tool that declares network access is refused unless you approve it."),
    })

    # 5. Custody — what stays.
    sections.append({
        "key": "custody", "title": "Your data + custody", "status": "local", "severity": "ok",
        "detail": ("Your vault — activities, tools, outcomes, and receipts — lives locally on this "
                   "machine. A money-move is credited only via independent verification (a hardened "
                   "read-back), never the tool's self-report."),
    })

    # 6. Container / host boundary.
    if profile.get("docker_mode"):
        sections.append({
            "key": "docker", "title": "Container", "status": "container", "severity": "info",
            "detail": ("Running in a container — desktop / host capture reaches your machine only "
                       "via the Host Companion pairing (typed-confirm), never directly."),
        })

    headline = ("Your prompts stay on local models; your vault and credentials stay local."
                if all_local else
                f"Prompts + file excerpts you share transit {dest_txt}; your vault and credentials "
                "stay local.")

    return {
        "destinations": destinations, "local_llm": all_local,
        "os": profile.get("os_family"), "docker": bool(profile.get("docker_mode")),
        "headline": headline, "sections": sections,
    }
