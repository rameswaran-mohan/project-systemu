"""v0.8.19 — validate a tool call against its Tool.parameters_schema (R4)."""
from __future__ import annotations

from typing import Any, Dict, List

_JSON_PY = {"string": str, "integer": int, "number": (int, float),
            "boolean": bool, "array": list, "object": dict}


def validate_params(schema: Dict[str, Any], params: Dict[str, Any]) -> List[str]:
    """Return human-readable errors (empty list = ok).

    • TYPE: every PROVIDED arg must match its JSON 'type'.
    • REQUIRED: flagged ONLY when an entry explicitly declares ``required: true``
      (we do NOT infer required-ness from a missing 'default' — a legacy tool may
      have an optional param without a default; genuinely-missing args fall through
      to execute()'s existing TypeError backstop). Unknown params allowed.
      Empty schema => no checks (back-compat).
    """
    errs: List[str] = []
    for name, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        if name not in (params or {}):
            if spec.get("required") is True:
                errs.append(f"missing required parameter '{name}' (type {spec.get('type', 'any')})")
            continue
        expected = spec.get("type")
        py = _JSON_PY.get(expected)
        if py is None:
            continue
        val = params[name]
        if expected in ("integer", "number") and isinstance(val, bool):
            errs.append(f"parameter '{name}' must be {expected}, got boolean")
        elif not isinstance(val, py):
            errs.append(f"parameter '{name}' must be {expected}, got {type(val).__name__}")
    return errs


def _resolve_schema(tool_name: str, *, tools, v2_registry) -> Dict[str, Any]:
    """Resolve a tool's JSON-Schema parameters definition.

    v1 tools carry it on ``tool.parameters_schema``; v2/MCP tools on
    ``registry.get(name).schema``. Either may be the JSON-Schema object shape
    ``{"type":"object","properties":{...},"required":[...]}``. Returns ``{}``
    when no schema is found (⇒ empty gap ⇒ zero behavior change).
    """
    for t in (tools or []):
        if getattr(t, "name", None) == tool_name:
            sch = getattr(t, "parameters_schema", None)
            if isinstance(sch, dict) and sch:
                return sch
            break
    if v2_registry is not None:
        try:
            entry = v2_registry.get(tool_name)
        except Exception:
            entry = None
        sch = getattr(entry, "schema", None) if entry is not None else None
        if isinstance(sch, dict) and sch:
            return sch
    return {}


def missing_required(
    tool_name: str,
    parameters: Dict[str, Any],
    *,
    tools,
    v2_registry,
) -> List[Dict[str, Any]]:
    """Return descriptors for required JSON-Schema params absent from ``parameters``.

    A param is *missing* when it is in the schema's top-level ``required[]`` AND
    its value in ``parameters`` is absent / ``None`` / ``""``. Each descriptor is
    ``{name, type, description, enum?, format?}`` (enum/format only when declared)
    — the shape the elicitation form renders.

    Empty / absent schema ⇒ ``[]`` ⇒ zero behavior change for legacy tools.
    Never raises (a malformed schema yields ``[]``).
    """
    schema = _resolve_schema(tool_name, tools=tools, v2_registry=v2_registry)
    if not isinstance(schema, dict):
        return []
    required = schema.get("required") or []
    if not isinstance(required, list) or not required:
        return []
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    params = parameters or {}
    out: List[Dict[str, Any]] = []
    for name in required:
        if not isinstance(name, str):
            continue
        val = params.get(name)
        if val is not None and val != "":
            continue  # present
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        field: Dict[str, Any] = {
            "name": name,
            "type": spec.get("type", "string"),
            "description": spec.get("description", ""),
        }
        if isinstance(spec.get("enum"), list):
            field["enum"] = list(spec["enum"])
        if spec.get("format"):
            field["format"] = spec["format"]
        if "default" in spec:
            field["default"] = spec["default"]
        out.append(field)
    return out
