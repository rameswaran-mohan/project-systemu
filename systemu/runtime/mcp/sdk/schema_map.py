"""v0.9.36 P2 — pure mapping/hash/sanitisation helpers for MCP tool defs.

No SDK import: operates on plain dicts the transports layer extracts from SDK
objects. Jobs:
  * mcp_schema_to_parameters  — MCP inputSchema -> systemu parameters_schema
                                with a REAL required[] (the join point for the
                                tool-budget and the param-gap detector).
  * to_systemu_schema         — CONTRACT export (pinned doc §sdk/schema_map):
                                full MCP tool object/dict -> {description,
                                parameters_schema, annotations}; description is
                                sanitised, required[] is real. P3 builds grant
                                tool dicts from THIS.
  * tool_def_hash             — canonical hash of (name+description+schema) for
                                rug-pull (definition-drift) detection. The ONLY
                                def-hash (contract).
  * sanitize_description      — treat discovered descriptions as UNTRUSTED
                                external content (tool-poisoning defence) before
                                they enter available_tools / the LLM context.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional

_UNTRUSTED_LABEL = "[untrusted MCP tool description]"

# Role/markup frames an injected description might use to impersonate the
# conversation. Stripped before the text reaches the model.
_ROLE_TAG_RE = re.compile(r"</?\s*(system|assistant|user|tool)\s*>", re.IGNORECASE)
_ROLE_PREFIX_RE = re.compile(r"(?im)^\s*(system|assistant|user|tool)\s*:")

# Annotation hint fields we surface (action-tier + display).
# v0.9.38 (CC-3): also capture WebMCP/MCP `untrustedContentHint` so it persists
# through set_tool_enabled/get_enabled_meta (the L4 output guard already labels
# ALL MCP output untrusted; capturing the hint satisfies the addendum + future use).
_ANNOTATION_KEYS = (
    "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint",
    "untrustedContentHint", "title",
)


def mcp_schema_to_parameters(input_schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map an MCP tool ``inputSchema`` to systemu's ``parameters_schema`` shape.

    Guarantees a dict with ``type``/``properties``/``required`` keys so the
    param-gap detector and the catalog never KeyError. Preserves the real
    ``required`` list (the first real consumer of required[] beyond cosmetics).
    """
    if not isinstance(input_schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    props = input_schema.get("properties")
    required = input_schema.get("required")
    return {
        "type": input_schema.get("type", "object"),
        "properties": dict(props) if isinstance(props, dict) else {},
        "required": list(required) if isinstance(required, list) else [],
    }


def _extract_annotations(ann: Any) -> Dict[str, Any]:
    """Pull the known annotation hint fields off an SDK annotations object OR a
    plain dict into a plain dict (drops Nones)."""
    out: Dict[str, Any] = {}
    if ann is None:
        return out
    if isinstance(ann, dict):
        for k in _ANNOTATION_KEYS:
            v = ann.get(k)
            if v is not None:
                out[k] = v
        return out
    for k in _ANNOTATION_KEYS:
        v = getattr(ann, k, None)
        if v is not None:
            out[k] = v
    return out


def to_systemu_schema(mcp_tool: Any) -> Dict[str, Any]:
    """CONTRACT export: full MCP tool (SDK object OR plain dict) -> systemu shape.

    Returns ``{description, parameters_schema, annotations}`` where the
    description is sanitised (untrusted-labelled), the parameters_schema carries
    the real required[], and annotations are flattened to a plain dict. P3's
    grant tool dicts are built from this (so register_server_tools gets the
    right schema + action tier)."""
    if isinstance(mcp_tool, dict):
        description = mcp_tool.get("description", "")
        input_schema = mcp_tool.get("inputSchema")
        if input_schema is None:
            input_schema = mcp_tool.get("input_schema")
        ann = mcp_tool.get("annotations")
    else:
        description = getattr(mcp_tool, "description", "")
        input_schema = getattr(mcp_tool, "inputSchema", None)
        if input_schema is None:
            input_schema = getattr(mcp_tool, "input_schema", None)
        ann = getattr(mcp_tool, "annotations", None)
    return {
        "description": sanitize_description(str(description or "")),
        "parameters_schema": mcp_schema_to_parameters(
            dict(input_schema) if isinstance(input_schema, dict) else input_schema),
        "annotations": _extract_annotations(ann),
    }


def _canonical(obj: Any) -> str:
    # Order-insensitive, stable serialisation for hashing.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def tool_def_hash(*, name: str, description: str,
                  input_schema: Optional[Dict[str, Any]]) -> str:
    """SHA-256 over the FULL tool definition. Any drift in name, description, or
    schema flips the hash — the rug-pull trigger. The ONLY def-hash (contract)."""
    payload = _canonical({
        "name": name or "",
        "description": description or "",
        "schema": input_schema or {},
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sanitize_description(text: str, *, max_chars: int = 2000) -> str:
    """Neutralise a discovered tool description before it enters the LLM context.

    Strips role/markup tags, defuses leading role prefixes, size-caps, and
    prepends an untrusted-content label. NEVER framed as instructions.
    """
    raw = str(text or "")
    raw = _ROLE_TAG_RE.sub("", raw)
    raw = _ROLE_PREFIX_RE.sub(lambda m: m.group(0).replace(":", "ː"), raw)
    raw = raw.strip()
    if len(raw) > max_chars:
        raw = raw[:max_chars].rstrip() + "…"
    return f"{_UNTRUSTED_LABEL} {raw}".rstrip()
