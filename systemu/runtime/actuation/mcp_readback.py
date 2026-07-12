"""Curated pre-submit readback templates for money-move MCP tools.

A money-move MCP effect can only be CREDITED via a hardened, independent,
provably-FRESH api_readback (never self-report). Freshness for a create-once
resource with a SERVER-assigned id is unprovable pre-submit (you don't know the id
before you create it) — so those money-moves stay fail-closed/uncredited.

But a money-move whose target is knowable PRE-SUBMIT — a CLIENT-provided
idempotency key, read back at a deterministic URL — CAN be proven fresh: probe the
URL before the mutation (token ABSENT), submit, then read it back (token PRESENT).
This module curates, per money-move MCP tool, how to build that pre-submit readback
directive from the call's params. It is deliberately EMPTY by default (no real
money-move MCP tool is connected yet); an operator/integration adds an entry, and
the mechanism (McpActuationModality.probe_presubmit) is validated by tests today so
it is ready — not speculative — the moment a real tool lands.

Absent an entry ⇒ no pre-submit probe ⇒ the money-move stays fail-closed (safe).
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

#: tool name (the namespaced ``mcp__<server>__<tool>`` or the bare tool name) →
#: {"readback_url_template": "...{idempotency_key}...", "idempotency_param": "..."}.
#: The template is str.format()'d with the call's params; the idempotency_param
#: names the param carrying the client-generated, provably-fresh token.
_TEMPLATES: Dict[str, Dict[str, str]] = {}


def register_template(tool_name: str, *, readback_url_template: str,
                      idempotency_param: str) -> None:
    """Curate (or, in tests, inject) a money-move MCP tool's pre-submit readback
    template. Idempotent overwrite."""
    _TEMPLATES[str(tool_name)] = {
        "readback_url_template": str(readback_url_template),
        "idempotency_param": str(idempotency_param),
    }


def template_for(tool_name: Optional[str]) -> Optional[Dict[str, str]]:
    """The curated template for a tool (by its namespaced or bare name), else None."""
    if not tool_name:
        return None
    name = str(tool_name)
    if name in _TEMPLATES:
        return _TEMPLATES[name]
    # also match the bare tool name (mcp__server__tool → tool)
    bare = name.rsplit("__", 1)[-1]
    return _TEMPLATES.get(bare)


def presubmit_directive_from_params(tool_name: Optional[str],
                                    params: Optional[Dict[str, Any]]) -> Optional[dict]:
    """Build a PRE-SUBMIT api_readback directive from a money-move MCP call's params
    + the curated template, or None when: no template is curated, the idempotency
    param is missing, the filled URL is not https, or the template is malformed.

    The ``expected_tokens`` is the CLIENT-provided idempotency token — provably fresh
    when the pre-submit probe finds it ABSENT and the post-submit readback PRESENT.
    """
    tpl = template_for(tool_name)
    if not tpl:
        return None
    params = params or {}
    idem_param = tpl.get("idempotency_param")
    token = params.get(idem_param) if idem_param else None
    if not token:
        return None
    try:
        url = str(tpl["readback_url_template"]).format(**params)
    except Exception:
        return None
    if not url.lower().startswith("https://"):
        return None   # readback must be https (same admissibility the hardened path enforces)
    host = (urlparse(url).hostname or "").lower().strip()
    if not host:
        return None
    return {"strategy": "api_readback", "readback_url": url,
            "expected_tokens": [str(token)], "submit_host": host}
