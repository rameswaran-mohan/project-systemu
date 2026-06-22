# systemu/runtime/param_resolution.py
"""v0.9.35 (Phase 3) — resolve Scroll.parameters at run time.

PURE module (no I/O, no NiceGUI). Two responsibilities:

  1. Turn a recorded scroll's ``parameters`` (List[ScrollParameter]) into an
     MCP form-mode ``requested_schema`` whose every slot is in ``required[]``
     with the CAPTURED value as the editable ``default`` — so the existing
     ASK_OPERATOR / render_decision_card rail asks the operator and pre-fills
     the captured value (pinned KEY CONSTRAINT: required + absent + default).
  2. Substitute the operator's chosen answers back into the objectives /
     constraints / intent context the agent sees (slot substitution).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from systemu.runtime.elicitation import elicitation_schema_from_fields


def _param_to_field(p: Any) -> Dict[str, Any]:
    """ScrollParameter (or its model_dump dict) → elicitation field descriptor.

    The captured value (``default``) is ALWAYS placed under the ``default`` key
    so the schema declares it and the form pre-fills it. ``required`` is implied
    (the builder treats every field as required — they are the asked gap)."""
    d = p if isinstance(p, dict) else p.model_dump(mode="json")
    field: Dict[str, Any] = {
        "name": d.get("name", ""),
        "type": d.get("type", "string"),
        "description": d.get("description", "") or "",
        "default": d.get("default"),
    }
    if isinstance(d.get("enum"), list) and d["enum"]:
        field["enum"] = list(d["enum"])
    if d.get("format"):
        field["format"] = d["format"]
    return field


def slot_schema_from_parameters(parameters: List[Any]) -> Dict[str, Any]:
    """Build the form-mode requested_schema for a scroll's captured parameters.

    Each slot is required[] (the gap) and carries the captured value as the
    schema ``default`` (pre-filled, editable). Empty parameters ⇒ an empty
    schema (caller treats that as a no-op)."""
    fields = [_param_to_field(p) for p in (parameters or [])
              if (p if isinstance(p, dict) else p.model_dump()).get("name")]
    return elicitation_schema_from_fields(fields)


def _replace_in_obj(obj: Any, old: str, new: str) -> Any:
    """Recursively replace literal ``old`` with ``new`` in str leaves of a
    JSON-shaped object (dict/list/str). Other scalars pass through."""
    if isinstance(obj, str):
        return obj.replace(old, new) if old else obj
    if isinstance(obj, list):
        return [_replace_in_obj(v, old, new) for v in obj]
    if isinstance(obj, dict):
        return {k: _replace_in_obj(v, old, new) for k, v in obj.items()}
    return obj


def substitute_parameters(
    parameters: List[Any],
    answers: Dict[str, Any],
    *,
    scroll_json: List[Dict[str, Any]],
    intent: str,
    constraints: Dict[str, Any],
):
    """Substitute operator-chosen parameter values into the scroll context.

    For each parameter, the RESOLVED value is the operator's answer when given,
    else the captured ``default`` (operator left the pre-fill untouched). Every
    literal occurrence of the captured default string in the objectives / intent
    / constraints is replaced with the resolved value (slot substitution).

    Returns ``(scroll_json, intent, constraints, resolved)`` — all copies; the
    inputs are never mutated. Empty ``parameters`` ⇒ identity (no-op)."""
    new_json = copy.deepcopy(scroll_json or [])
    new_intent = intent or ""
    new_constraints = copy.deepcopy(constraints or {})
    resolved: Dict[str, Any] = {}
    for p in (parameters or []):
        d = p if isinstance(p, dict) else p.model_dump(mode="json")
        name = d.get("name")
        if not name:
            continue
        captured = d.get("default")
        chosen = answers.get(name, captured) if answers else captured
        resolved[name] = chosen
        # Slot substitution: only meaningful when both sides stringify and differ.
        old_s = "" if captured is None else str(captured)
        new_s = "" if chosen is None else str(chosen)
        if old_s and old_s != new_s:
            new_json = _replace_in_obj(new_json, old_s, new_s)
            new_intent = new_intent.replace(old_s, new_s)
            new_constraints = _replace_in_obj(new_constraints, old_s, new_s)
    return new_json, new_intent, new_constraints, resolved
