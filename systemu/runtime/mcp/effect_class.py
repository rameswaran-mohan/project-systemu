"""P21 -- an MCP tool call, classified by the SAME effect-class machinery as a native tool.

Before this module the MCP action gate (``dispatch._gate_mcp_call``) read two server
hints into a three-way risk tier and nothing else: an MCP gate card carried NO effect
classes, so a money-moving or destructive MCP tool still offered "Always allow" and a
one-click approve -- the spec section 10 ruling R1 never reached it.

:func:`classify_mcp_call` closes that by building the ``ActionContext`` a native tool
gets and scoring it with ``action_governance`` itself -- ``evaluate_action`` for the
verdict, ``effective_tags`` for the scored classes (so the native NAME VERB MAP runs on
an MCP tool exactly as it runs on a forged one), and ``irreversible_effects`` for the
R1 answer. What differs is only WHERE the declared classes come from, because an MCP
tool has no source the codebase can scan and no tags the codebase authored.

The rules, and whose word each one rests on:

  * DEC-34 -- the SERVER is the verified party. Its annotations are hints it controls,
    so they may only RAISE severity, never lower it:

      - ``destructiveHint: true`` (a real bool) is irreversible: it sets the context's
        ``irreversible`` signal and stamps the literal ``irreversible`` marker, which
        ``irreversible_effects`` reads (and the phone floor already names);
      - ``readOnlyHint: true`` keeps a call ungated ONLY when nothing else -- the name
        verb map, a money signal, a destructive parameter, the destructive hint --
        classifies it. It never suppresses another signal: a ``delete_*`` tool that
        claims read-only is a delete;
      - a MISSING or UNPARSEABLE annotation (absent, or not a plain ``bool``) is the
        UNKNOWN band (IMPL-10): gated, at least require-approval, never auto-allowed,
        and no session trust;
      - ``openWorldHint`` / ``idempotentHint`` are not consulted: neither can raise a
        class this vocabulary has, and reading either could only lower one.

  * MONEY -- an MCP tool is ``money_move`` when the OPERATOR declared it so (below), or
    when its name or its input-schema property names match the money heuristics native
    code already uses: the gate's own name verb map (``action_governance``, via the
    context) and the money-move net's whole-token detector
    (``financial_signal.is_financial_signal``). No second list. Names are split at
    camelCase boundaries first (``schema_map._snake``) so ``createPayment`` reads as
    ``create payment`` -- a normalisation, never a new vocabulary.

  * THE OPERATOR -- ``connections.get_operator_effect_classes`` is a declaration in the
    MCP connections file the operator controls, written only by
    ``connections.set_operator_effect_classes`` (never by anything a server sends).
    When present it IS the tool's declared class set, exactly as a native tool's
    ``effect_tags``: it may raise a tool's class, and it may lower it -- the server's
    hints and the schema money signal are then not consulted. The native name verb map
    still escalates it unless the declared set is local-only (the
    ``send_summary_to_log`` rule), and a declared R1 marker (``irreversible``,
    ``payment`` ...) is kept, never coerced away to UNKNOWN.

Fail-closed throughout: a value of the wrong concrete type is UNKNOWN, a classification
that raises is the UNKNOWN band on the per-call tier, and nothing in here can produce
the read-only tier on its own.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: A hint that is present but is not a plain ``bool`` -- UNKNOWN band, never a clear.
_UNPARSEABLE = object()

#: The marker ``irreversible_effects`` reads for a server-declared destructive tool.
IRREVERSIBLE_MARKER = "irreversible"

#: Where the declared classes came from (stamped on the card).
BASIS_OPERATOR = "operator"
BASIS_SERVER = "server"
BASIS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class McpEffectClass:
    """One MCP call's classification. Every field is a plain value (DEC-32: the fence
    rides in the value the gate decides on)."""

    effect_tags: Tuple[str, ...]   # the SCORED classes (+ the irreversible marker)
    verdict: str                   # ``evaluate_action`` verdict value
    reason: str                    # ``evaluate_action`` reason
    irreversible: Tuple[str, ...]  # ``irreversible_effects(effect_tags)`` -- R1 iff non-empty
    tier: str                      # "R" ungated-if-pinned / "A" action / "D" per-call
    basis: str                     # operator / server / unknown


def _hint(annotations: Any, wire_name: str) -> Any:
    """``True`` / ``False`` / ``None`` (absent) / :data:`_UNPARSEABLE`.

    Read through ``schema_map.wire_field`` so a camelCase (mcp 1.x) and a snake_case
    (mcp 2.x) annotation both count. Type-pinned (DEC-36): only a plain ``bool`` is a
    hint; anything else present is unparseable."""
    if annotations is None:
        return None
    if type(annotations) is not dict:
        return _UNPARSEABLE
    from systemu.runtime.mcp.sdk.schema_map import wire_field
    value = wire_field(annotations, wire_name)
    if value is None:
        return None
    if type(value) is bool:
        return value
    return _UNPARSEABLE


def hint_tier(annotations: Any) -> str:
    """The risk tier the server's hints ALONE would earn: "R" / "A" / "D".

    Destructive dominates read-only; a missing or unparseable hint is "D" (the UNKNOWN
    band). This is a FLOOR the classification may only raise, never the answer."""
    read_only = _hint(annotations, "readOnlyHint")
    destructive = _hint(annotations, "destructiveHint")
    if destructive is True:
        return "D"
    if read_only is _UNPARSEABLE or destructive is _UNPARSEABLE:
        return "D"
    if read_only is True:
        return "R"
    if read_only is None and destructive is None:
        return "D"
    return "A"


def _snake(name: Any) -> str:
    if type(name) is not str:
        return ""
    from systemu.runtime.mcp.sdk.schema_map import _snake as _wire_snake
    return _wire_snake(name)


def _schema_property_names(schema: Any) -> Tuple[str, ...]:
    """The top-level input-schema property names, type-pinned. ``()`` for anything
    that is not a JSON-schema object with a ``properties`` dict."""
    if type(schema) is not dict:
        return ()
    props = schema.get("properties")
    if type(props) is not dict:
        return ()
    return tuple(k for k in props if type(k) is str)


def money_signal(tool: Any, schema: Any) -> bool:
    """True when the tool's NAME or its input-schema PROPERTY NAMES carry the money-move
    net's whole-token financial signal (``financial_signal.is_financial_signal``).
    Positive-only and escalate-never-clear; never raises (a failure is not a clear,
    so it answers True)."""
    try:
        from systemu.runtime.financial_signal import is_financial_signal
        words = [_snake(tool)] + [_snake(p) for p in _schema_property_names(schema)]
        return bool(is_financial_signal(" ".join(w for w in words if w), None))
    except Exception:
        logger.debug("[McpEffectClass] money signal failed -- treating as money",
                     exc_info=True)
        return True


def operator_declared_classes(raw: Any) -> Optional[Tuple[set, bool]]:
    """Normalise an operator declaration to ``(declared_tags, irreversible_marker)``,
    or ``None`` when there is no usable declaration (absent or empty).

    Each entry is type-pinned to ``str`` (DEC-36); a non-string entry is UNKNOWN. A
    canonical class is kept as itself; an R1 money marker (``payment`` ...) becomes
    ``money_move``; the ``irreversible`` marker is carried as the context's
    irreversible signal; anything else is UNKNOWN -- so an operator's raise is never
    coerced into a lower band."""
    if type(raw) not in (list, tuple):
        return None
    entries = list(raw)
    if not entries:
        return None
    from systemu.runtime.action_governance import MONEY_EFFECTS
    from systemu.runtime.effect_tags import EffectTag, coerce
    declared: set = set()
    irreversible = False
    for entry in entries:
        if type(entry) is not str:
            declared.add(EffectTag.UNKNOWN.value)
            continue
        norm = entry.strip().lower()
        if norm == IRREVERSIBLE_MARKER:
            irreversible = True
            continue
        if norm in MONEY_EFFECTS:
            declared.add(EffectTag.MONEY_MOVE.value)
            continue
        declared.add(coerce(norm))
    return declared, irreversible


def _destructive_param(tool: Any, params: Any) -> bool:
    """The native destructive-call heuristic (``ToolSandbox.is_destructive_call``).
    Fail-closed: a check that raises is a destructive signal."""
    try:
        from systemu.runtime.tool_sandbox import ToolSandbox
        return bool(ToolSandbox.is_destructive_call(
            tool if type(tool) is str else "",
            params if type(params) is dict else {}))
    except Exception:
        return True


def classify_mcp_call(*, tool: str, params: Optional[Dict[str, Any]] = None,
                      annotations: Any = None, schema: Any = None,
                      operator_classes: Any = None,
                      operator_assigned: Any = None,
                      classification_trusted: bool = False) -> McpEffectClass:
    """Classify ONE MCP call with the native gate's machinery. Never raises: a
    failure is the UNKNOWN band on the per-call tier (fail-closed).

    ``operator_assigned`` is the class the operator assigned on an MCP DENY card
    (``connections.get_operator_assigned_class``). It enters exactly as a native
    IMPL-2 reclassification does -- ``ActionContext.operator_assigned_class`` --
    so it defeats only the UNKNOWN conjunct, is additive to every other signal,
    and can never make the call frictionless."""
    try:
        return _classify(tool=tool, params=params, annotations=annotations,
                         schema=schema, operator_classes=operator_classes,
                         operator_assigned=operator_assigned,
                         classification_trusted=classification_trusted)
    except Exception:
        logger.warning("[McpEffectClass] classification failed for %r -- UNKNOWN band, "
                       "per-call gate (fail-closed)", tool, exc_info=True)
        return McpEffectClass(effect_tags=("unknown",), verdict="require_approval",
                              reason="the effect could not be classified",
                              irreversible=(), tier="D", basis=BASIS_UNKNOWN)


def _classify(*, tool, params, annotations, schema, operator_classes,
              operator_assigned, classification_trusted) -> McpEffectClass:
    from systemu.runtime.action_governance import (ActionContext, Verdict,
                                                   effective_tags, evaluate_action,
                                                   irreversible_effects)
    from systemu.runtime.effect_tags import EffectTag
    from systemu.runtime.tool_sandbox import _small_args_preview

    name = tool if type(tool) is str else ""
    declared_op = operator_declared_classes(operator_classes)
    if declared_op is not None:
        # The operator's declaration IS the class set (it may raise or lower); the
        # server's hints and the schema money signal are not consulted.
        declared, irreversible = declared_op
        basis = BASIS_OPERATOR
        base_tier = None
    else:
        declared = set()
        # DEC-34: only a raise -- destructiveHint is the only hint that can raise.
        irreversible = _hint(annotations, "destructiveHint") is True
        if money_signal(name, schema):
            declared.add(EffectTag.MONEY_MOVE.value)
        base_tier = hint_tier(annotations)
        hinted = (_hint(annotations, "readOnlyHint") is not None
                  or _hint(annotations, "destructiveHint") is not None)
        basis = BASIS_SERVER if hinted else BASIS_UNKNOWN

    ctx = ActionContext(
        tool=_snake(name),
        effect_tags=declared,
        is_destructive_param=_destructive_param(name, params),
        target=None,
        target_is_network=False,
        irreversible=irreversible,
        classification_trusted=bool(classification_trusted),
        args_preview=_small_args_preview(params if type(params) is dict else {}),
        operator_assigned_class=(operator_assigned.strip()
                                 if type(operator_assigned) is str
                                 and operator_assigned.strip() else None),
    )
    verdict, reason = evaluate_action(ctx)
    scored = set(effective_tags(ctx))
    if irreversible:
        scored.add(IRREVERSIBLE_MARKER)
    tags = tuple(sorted(scored))
    irrev = irreversible_effects(tags)

    if irrev or verdict == Verdict.DENY:
        tier = "D"
    elif base_tier is None:                       # operator-declared
        tier = "R" if verdict == Verdict.ALLOW else "A"
    elif base_tier == "R":
        # readOnlyHint never suppresses another signal: it keeps the call ungated
        # only when NOTHING else classified it.
        unclassified = scored == {EffectTag.UNKNOWN.value}
        tier = "R" if (unclassified and not ctx.is_destructive_param) else "A"
    else:
        tier = base_tier
    return McpEffectClass(effect_tags=tags,
                          verdict=getattr(verdict, "value", str(verdict)),
                          reason=reason, irreversible=tuple(irrev), tier=tier,
                          basis=basis)
