"""v0.9.35 (P1) — shared structured-input (elicitation) model.

The lingua franca for systemu's three structured-input sources (missing
required tool params, MCP-server ``elicitation/create``, ``ASK_OPERATOR``)
is the MCP elicitation FORM-MODE schema shape:

  * flat object (no nesting),
  * primitive fields only: string / number / integer / boolean / enum,
  * ``format`` ∈ {email, uri, date, date-time, password},
  * per-field ``default``,
  * accept / decline / cancel response model (handled by the gate's options).

This module is PURE (no I/O, no NiceGUI, no vault). It builds the schema,
type-coerces operator answers, validates them client-side, and identifies
secret fields that must go URL-mode (never the form / LLM / logs).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_ALLOWED_TYPES = {"string", "number", "integer", "boolean"}
_ALLOWED_FORMATS = {"email", "uri", "date", "date-time", "password"}

# Field names / formats that indicate a credential → URL-mode, never the form.
_SECRET_NAME_TOKENS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_key", "private_key", "client_secret", "credential", "auth",
    "card", "cvv", "ssn", "pin",
)
_SECRET_FORMATS = {"password"}


def elicitation_schema_from_fields(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build an MCP form-mode ``requested_schema`` from field descriptors.

    ``fields`` are the ``{name, type, description, enum?, format?, default?}``
    dicts produced by :func:`param_validation.missing_required`. All fields are
    treated as required (they are the detected gap). Unknown types fall back to
    ``string``; disallowed formats are dropped.
    """
    props: Dict[str, Any] = {}
    required: List[str] = []
    for f in (fields or []):
        name = f.get("name")
        if not isinstance(name, str) or not name:
            continue
        ftype = f.get("type") if f.get("type") in _ALLOWED_TYPES else "string"
        spec: Dict[str, Any] = {"type": ftype}
        if f.get("description"):
            spec["description"] = f["description"]
        if isinstance(f.get("enum"), list):
            spec["enum"] = list(f["enum"])
        if f.get("format") in _ALLOWED_FORMATS:
            spec["format"] = f["format"]
        if "default" in f:
            spec["default"] = f["default"]
        props[name] = spec
        required.append(name)
    return {"type": "object", "properties": props, "required": required}


def coerce_field_value(ftype: str, raw: Any) -> Any:
    """Coerce a raw (usually string) operator answer to the field's type.

    Returns ``None`` when a numeric/integer value cannot be parsed (caller
    treats that as still-missing → re-ask, never a fabricated value).
    """
    if raw is None:
        return None
    if ftype == "string":
        return raw if isinstance(raw, str) else str(raw)
    if ftype == "boolean":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "yes", "y", "1", "on"}
    if ftype in ("number", "integer"):
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return None
        return int(num) if ftype == "integer" else num
    return raw


def validate_against_schema(schema: Dict[str, Any], values: Dict[str, Any]) -> List[str]:
    """Client-side validation. Returns the list of field names that fail.

    A field fails when it is required and absent/None/"" OR (for enum fields)
    its value is not one of the declared options. Type mismatches are NOT a
    failure here — coercion happens in :func:`param_answers_from_choice`; a
    non-coercible numeric surfaces as a still-missing re-ask downstream.
    """
    bad: List[str] = []
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    vals = values or {}
    for name in required:
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        val = vals.get(name)
        if val is None or val == "":
            bad.append(name)
            continue
        enum = spec.get("enum")
        if isinstance(enum, list) and val not in enum:
            bad.append(name)
    return bad


def is_secret_field(field: Dict[str, Any]) -> bool:
    """True when a field is a credential/secret → must go URL-mode."""
    if (field.get("format") or "").lower() in _SECRET_FORMATS:
        return True
    name = (field.get("name") or "").lower()
    return any(tok in name for tok in _SECRET_NAME_TOKENS)


def param_answers_from_choice(
    schema: Dict[str, Any], raw_values: Dict[str, Any]
) -> Dict[str, Any]:
    """Type-coerce a raw answer dict (form output) into typed param answers.

    Secret fields are never present here (they go URL-mode), so no secret ever
    flows through this function. A field whose coercion yields ``None`` is
    OMITTED (so the re-dispatched call re-validates as still-missing).
    """
    props = schema.get("properties") or {}
    out: Dict[str, Any] = {}
    for name, raw in (raw_values or {}).items():
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        if not isinstance(spec, dict) or not spec:
            # Field not declared in the schema (e.g. a leaked secret) — omit it.
            continue
        coerced = coerce_field_value(spec.get("type", "string"), raw)
        if coerced is None:
            continue
        out[name] = coerced
    return out


def split_secret_fields(
    fields: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Partition fields into (form_fields, secret_fields). Secret fields are
    rendered as URL-mode links, never typed inputs."""
    form: List[Dict[str, Any]] = []
    secret: List[Dict[str, Any]] = []
    for f in (fields or []):
        (secret if is_secret_field(f) else form).append(f)
    return form, secret


def _elicitation_request_id(message: str, requested_schema: Dict[str, Any]) -> str:
    """Stable id for an elicitation prompt (so a re-entry returns the SAME
    pending card — the dedup_key the poster keys resumption on)."""
    import hashlib
    import json as _json
    blob = _json.dumps(
        {"m": message or "", "s": requested_schema or {}},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def resolve_structured_input(
    *,
    message: str,
    requested_schema: Dict[str, Any],
    vault=None,
    config=None,
) -> Dict[str, Any]:
    """Resolve a structured-input request through the SAME park/ask/resume rail
    P1 builds for missing tool params, returning the MCP accept/decline/cancel
    response envelope.

    This is the EXPORTED entry point **P2 Task 13** registers as the SDK
    elicitation callback (``set_elicitation_callback``) so a connected MCP
    server's ``elicitation/create`` is serviced by the operator form rail —
    one card, N typed fields, secrets URL-mode — instead of a parallel prompt.

    Behaviour (pinned by ``docs/superpowers/specs/2026-06-19-mcp-pinned-contracts.md``):

      * Posts an INPUT gate via :func:`notifications.request_choice` (the same
        structured-question poster :func:`render_decision_card` renders), passing
        ``requested_schema`` so the typed multi-field form (one card) is shown and
        secret fields go URL-mode. While awaiting, ``request_choice`` raises
        ``PendingChoiceRequest`` — this function lets it PROPAGATE (the suspend is
        the rail; the caller is inside the resume-aware spine).
      * No operator queue / non-interactive ⇒ ``{"action": "cancel", ...}``
        (fail-closed: never hang, never fabricate a value).
      * On resolve: a coerced non-empty answer ⇒ ``accept`` with type-coerced
        ``content`` (same coercion as the reconciler); an explicit decline /
        safe-default ⇒ ``decline``; empty / non-coercible ⇒ ``cancel``.

    Returns ``{"action": "accept"|"decline"|"cancel", "content": {...}}``.
    """
    from systemu.interface import notifications

    schema = requested_schema if isinstance(requested_schema, dict) else {}
    req_id = _elicitation_request_id(message, schema)
    dedup_key = f"elicit:{req_id}"

    # One question whose prompt is the server's message; options come from the
    # form's Submit/Decline (the typed widgets live in render_decision_card,
    # keyed on requested_schema).
    questions = [{
        "id": "elicitation",
        "prompt": message or "Input needed",
        "options": [],
        "allow_free_text": False,
    }]
    extra_context = {"request_id": req_id, "elicitation": True}

    answer = notifications.request_choice(
        questions,
        dedup_key=dedup_key,
        extra_context=extra_context,
        requested_schema=schema,
    )
    # No queue / headless ⇒ fail-closed cancel (PendingChoiceRequest propagates
    # on its own while awaiting — it is NOT caught here).
    if answer is None:
        return {"action": "cancel", "content": {}}

    # Explicit decline / safe-default: the poster hands back a {"_raw": <label>}
    # when the choice was not JSON form output (e.g. the "Deny" button).
    if isinstance(answer, dict) and set(answer.keys()) == {"_raw"}:
        return {"action": "decline", "content": {}}

    content = param_answers_from_choice(schema, answer if isinstance(answer, dict) else {})
    if not content:
        # Nothing coerced through (empty / non-coercible) — treat as cancel, never
        # an accept with a fabricated value.
        return {"action": "cancel", "content": {}}
    return {"action": "accept", "content": content}
