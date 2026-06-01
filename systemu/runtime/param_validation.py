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
