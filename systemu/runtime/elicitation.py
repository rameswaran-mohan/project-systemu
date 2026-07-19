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

#: R-B4/F3 — the reserved key carrying "which fields did the operator explicitly
#: PICK the suggestion for", alongside the answer values.
#:
#: Why it exists. Until now "did the operator take the binder's candidate or type
#: their own?" was INFERRED downstream by comparing a keyed digest of the answer
#: against a digest of the candidate (`replay_metrics.record_ask_avoidable`, and
#: the origin decision in the `ask_promotion` module). Byte-equality works for
#: short identifiers and is effectively dead for path-shaped values:
#: ``out/x.md``, ``out\x.md`` and ``OUT/X.MD`` all compare unequal.
#:
#: NB the deliberate circumlocution above: a source pin asserts the promoter is
#: not WIRED into this rail (it has no idempotency stamp, so two resumes would
#: promote twice) and enforces that by substring. Naming the function here, even
#: in prose, trips it. The pin is right to be that blunt — this comment bends.
#:
#: The failure is not neutral. A mismatch is read as "the operator typed this",
#: i.e. the TRUSTED axis — so a confirm the normalizer failed to fold silently
#: LAUNDERS a content_derived value into `operator`. An explicit marker records
#: what the UI actually knows at the moment of the click, and fails toward taint.
#:
#: It is NOT a schema property and must never reach a tool. `param_answers_from_
#: choice` drops it unconditionally — before the schema check, so a schema that
#: declared a property of this name could not smuggle it through either.
PICK_MARKER_KEY = "__picked__"

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


def free_text_input_schema(question: str) -> Dict[str, Any]:
    """v0.9.45: a one-field free-text ``requested_schema`` for a plain
    ASK_OPERATOR question that carries no structured fields.

    A bare ASK_OPERATOR ("What number?") used to surface as a generic harness
    gate (Deny/Approve/Edit spec) with NO answer field, so the operator could
    only click a button — whose label was then injected as the agent's "answer",
    and the agent re-asked forever. Synthesizing this schema makes
    render_decision_card draw a real answer BOX (the same form widget the
    missing-param / MCP-elicitation paths use), and the reconciler extracts the
    operator's typed value cleanly.
    """
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string",
                       "description": (question or "Your answer")[:200]},
        },
        "required": ["answer"],
    }


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
        if name == PICK_MARKER_KEY:
            # R-B4/F3 — provenance metadata, never a tool parameter. Dropped BEFORE
            # the schema lookup on purpose: a schema declaring a property of this
            # name would otherwise let the marker through as a real argument.
            continue
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


# ─────────────────────────────────────────────────────────────────────────────
#  R-A10 B10 — a RequirementReport.ask_bundle Requirement → the elicitation rail
# ─────────────────────────────────────────────────────────────────────────────

def _schema_path_leaf(schema_path: str) -> str:
    """The trailing leaf of a Requirement.schema_path (e.g. ``"auth/api_key"`` →
    ``"api_key"``). Array-item markers (the literal ``"[]"``) and empty segments
    are skipped so the field name is a real leaf, never ``""`` or ``"[]"``."""
    segs = [s for s in str(schema_path or "").split("/") if s and s != "[]"]
    return segs[-1] if segs else (str(schema_path) or "field")


def requirement_to_field(req: Any) -> Dict[str, Any]:
    """Map ONE ``Requirement`` (B3 binder output) to an elicitation FIELD dict —
    the ``{name, type, description, [format], [default]}`` shape
    :func:`elicitation_schema_from_fields` consumes.

    Mapping (spec §5.6 / the B10 plan):
      * ``name``        = the LEAF of ``schema_path``.
      * ``type``        = ``"string"`` for every kind (the rail's primitive default;
                          a richer type is inferred from the enum below when present).
      * ``credential``  additionally carries ``format="password"`` so
                          :func:`is_secret_field` routes it URL-mode (never the form).
      * ``description`` = the requirement's ``rationale`` (WHY it's asked).
      * ``default``     = the pre-filled non-secret bound value from
                          ``bound_value_ref`` — a one-click confirm. A SECRET's
                          bound_value_ref is a REFERENCE (never the plaintext), so a
                          credential is NEVER pre-filled: no secret value in ``default``.

    A decision requirement that carries options (``enum`` on a future extension)
    would render a choice; today Requirement has no options field, so a decision is
    a plain string field (still a usable ask).
    """
    kind = _get_attr(req, "kind") or "input"
    schema_path = _get_attr(req, "schema_path") or ""
    rationale = _get_attr(req, "rationale") or ""
    bound_ref = _get_attr(req, "bound_value_ref")

    field: Dict[str, Any] = {
        "name": _schema_path_leaf(schema_path),
        "type": "string",
        "description": rationale,
    }
    if kind == "credential":
        # URL-mode marker → is_secret_field True; NEVER pre-fill a secret default.
        field["format"] = "password"
        return field

    # Non-secret: pre-fill the bound value as the schema default (a one-click
    # confirm). Guard on the name too — a name that reads as a secret must NOT get
    # a pre-filled default even if the kind is not "credential" (defense-in-depth).
    if bound_ref is not None and not is_secret_field(field):
        field["default"] = bound_ref
    return field


def _get_attr(obj, name):
    """Read ``name`` off a pydantic model OR a plain dict, tolerantly (None on miss)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def surface_ask_bundle_requirement(req: Any, *, vault=None, config=None) -> Dict[str, Any]:
    """Render ONE ``Requirement`` through the park/ask/resume rail and return the
    accept/decline/cancel envelope.

    Builds a SINGLE-field ``requested_schema`` from
    :func:`requirement_to_field`, then drives :func:`resolve_structured_input`
    with a clear ask ``message`` that carries the requirement's rationale. The
    suspend IS the rail: a ``PendingChoiceRequest`` raised while awaiting the
    operator PROPAGATES (it is deliberately NOT caught here — the caller sits in
    the resume-aware spine). Headless / no-queue ⇒ ``resolve_structured_input``
    returns a fail-closed ``cancel`` (never hangs, never fabricates).

    SINGLE requirement ONLY. The batched multi-requirement scope card (one card,
    N requirements) + re-plan-on-resume is deferred to **R-A12**; B10 surfaces the
    FIRST ask_bundle requirement so the producer has a live consumer.
    """
    field = requirement_to_field(req)
    schema = elicitation_schema_from_fields([field])
    rationale = _get_attr(req, "rationale") or ""
    leaf = field.get("name") or "input"
    message = f"Input needed for '{leaf}'."
    if rationale:
        message = f"{message} {rationale}"
    envelope = resolve_structured_input(
        message=message,
        requested_schema=schema,
        vault=vault,
        config=config,
    )
    # R-A16 / G-LEARN §5.9 — the ANSWER-LINKED avoidable-ask signal. This is the ONE
    # frame where the full Requirement (confidence / value_origin / bound_value_ref)
    # and the operator's answer coexist, so it is the natural producer. Only an
    # ``accept`` is an answer. OBSERVABILITY-ONLY: wrapped so a recorder hiccup can
    # never change what this rail returns (the credential/secret exclusion lives in
    # ``requirement_snapshot``, which refuses to snapshot a secret-mode requirement).
    try:
        if vault is not None and isinstance(envelope, dict) \
                and envelope.get("action") == "accept":
            from systemu.runtime import replay_metrics as _rm
            _rm.record_ask_avoidable(
                vault,
                ask_id=_elicitation_request_id(message, schema),
                snapshot=_rm.requirement_snapshot(req),
                answer=(envelope.get("content") or {}).get(leaf),
            )
    except Exception:
        pass
    return envelope


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
