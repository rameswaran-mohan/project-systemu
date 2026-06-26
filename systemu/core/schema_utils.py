"""Shared helpers for normalizing a tool's ``parameters_schema``.

The forge LLM sometimes emits a tool's ``parameters_schema`` in the *wrapped*
JSON-Schema object form ``{"type": "object", "properties": {...}, "required":
[...]}`` and sometimes in the canonical *unwrapped* vault form
``{param_name: {type, ...}}``.  Every consumer that derives the parameter list
must agree on one shape, or it ends up iterating the wrapper keys
(``["type", "properties", "required"]``) instead of the real parameter names.

These helpers are stdlib-only and idempotent so they can be called at forge
construction time *and* on every read/save without churn.
"""
from __future__ import annotations

from typing import Any, Dict, List


def normalize_parameters_schema(schema: Any) -> Dict[str, Any]:
    """Return the UNWRAPPED ``{param_name: spec}`` form of ``schema``.

    If ``schema`` is a wrapped JSON-Schema object (it has a ``properties``
    dict), return that ``properties`` dict with the wrapper-level ``required``
    list folded into each property spec as ``"required": True`` (the unwrapped
    convention the vault TOOL.md render and the dry-run param generator already
    read).  Idempotent on an already-unwrapped schema; returns ``{}`` for
    empty / non-dict input.
    """
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if isinstance(props, dict):
        required = set(schema.get("required") or [])
        out: Dict[str, Any] = {}
        for name, spec in props.items():
            spec = dict(spec) if isinstance(spec, dict) else {}
            if name in required:
                spec["required"] = True
            out[str(name)] = spec
        return out
    return schema


def schema_param_names(schema: Any) -> List[str]:
    """Return the ordered list of real parameter names for ``schema``."""
    return list(normalize_parameters_schema(schema).keys())
