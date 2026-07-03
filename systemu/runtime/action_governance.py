"""S1 — the universal action-governance gate (spec UNIFIED-v2 §5.7).

`evaluate_action(ActionContext) -> (Verdict, reason)` is the ONE deterministic
policy over every effectful call. It is the *net*, not the *driver*: the planner
proposes boldly; this gate decides ALLOW / DENY / REQUIRE_APPROVAL / MASK.

Load-bearing rules (all under test in ``tests/test_action_governance.py``):

  * **Effect is derived PRIMARILY from the tool's EffectTags** (G0) + the target
    host. The **name verb-map** and **is_destructive_param** are POSITIVE-ONLY
    escalators (they can add danger, never clear it), and a **self-declared HTTP
    method never clears** — a network-reachable target is a mutation unless the
    operator confirmed read-only.
  * **Open vocabulary, two-band UNKNOWN.** An unclassifiable effect is
    REQUIRE_APPROVAL (gated, dangerous-until-proven) — never a refusal — EXCEPT
    the narrow **DENY floor**: UNKNOWN ∩ a high-severity signal (irreversible /
    destructive-param / money) fails closed to DENY with an honest handoff, not a
    rubber-stampable card. This bounds *blast radius*, not *world breadth*.
  * **No false positive.** The name verb-map does not escalate a tool whose
    tool-side tags say it is local-only (so `send_summary_to_log` stays ALLOW).

This is the evaluator only. Wiring the live gates (`_maybe_gate_command`,
`_gate_mcp_call`, the forged execute path) to delegate through it — and closing
the `trusted_inprocess` bypass — is S1b.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from pydantic import BaseModel, Field

from systemu.runtime.effect_tags import EffectTag, coerce, is_high_severity


# §13.3 — effect classes that make a tool ineligible for the in-process fast path.
# Such a tool egresses / mutates externally / handles money-or-tokens, so it must
# NEVER run in-daemon at full privilege with an ambient secret — it runs isolated
# (and, later, in the S2 OS-kernel jail). This closes the `trusted_inprocess`
# bypass: a *speed* grant can never become a *governance* grant.
MUST_ISOLATE = frozenset({
    EffectTag.NET_MUTATE.value,
    EffectTag.SEND_MESSAGE.value,
    EffectTag.MONEY_MOVE.value,
    EffectTag.OAUTH_CALL.value,
})


def requires_isolation(effect_tags: Iterable) -> bool:
    """True iff any effect tag forces out-of-process isolation (§13.3)."""
    return any(coerce(t) in MUST_ISOLATE for t in (effect_tags or ()))


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    MASK = "mask"


class ActionContext(BaseModel):
    """Everything the gate needs to score one effectful call. Populated by the
    call sites in S1b (forged tool / MCP / shell); constructed directly in tests."""

    tool: str
    effect_tags: Set[str] = Field(default_factory=set)   # EffectTag values from G0 (may be empty ⇒ UNKNOWN)
    is_destructive_param: bool = False                    # from is_destructive_call — POSITIVE-ONLY
    http_method: Optional[str] = None                     # ENFORCEMENT key at the proxy, NEVER a trust/clear input
    target: Optional[str] = None                          # host / target identifier
    target_is_network: bool = False                       # is `target` a network-reachable host?
    irreversible: bool = False                            # system-of-record / no-undo (a high-severity signal)
    risk_band: str = "low"
    classification_trusted: bool = True                   # False ⇒ discovered/registry/first-use MCP
    operator_confirmed_read_only: bool = False            # the ONLY thing that clears a network target
    denied_by_policy: bool = False                        # explicit denylist / policy violation
    args_preview: Dict[str, Any] = Field(default_factory=dict)


# whole-token verb categories (tokenized on any non-alphanumeric boundary)
_MONEY_VERBS = {"charge", "pay", "purchase", "transfer", "wire", "refund",
                "remit", "withdraw", "deposit", "invoice", "bill"}
_MESSAGE_VERBS = {"send", "email", "message", "dm", "notify", "reply", "text"}
_MUTATE_VERBS = {"submit", "post", "upload", "file", "issue", "publish", "deploy",
                 "create", "update", "rsvp", "cancel", "approve", "order", "book"}
_DELETE_VERBS = {"delete", "remove", "drop", "truncate", "wipe", "purge",
                 "destroy", "erase"}

_LOCAL_TAGS = {EffectTag.LOCAL_READ.value, EffectTag.LOCAL_WRITE.value,
               EffectTag.LOCAL_DELETE.value, EffectTag.SHELL_EXEC.value}
_APPROVAL_TAGS = {EffectTag.NET_MUTATE.value, EffectTag.SEND_MESSAGE.value,
                  EffectTag.MONEY_MOVE.value, EffectTag.MONEY_MOVE.value,
                  EffectTag.LOCAL_DELETE.value, EffectTag.OAUTH_CALL.value}


def _tokens(name: str) -> Set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t}


def _name_categories(name: str) -> Set[str]:
    toks = _tokens(name)
    cats: Set[str] = set()
    if toks & _MONEY_VERBS:
        cats.add("money")
    if toks & _MESSAGE_VERBS:
        cats.add("message")
    if toks & _MUTATE_VERBS:
        cats.add("mutate")
    if toks & _DELETE_VERBS:
        cats.add("delete")
    return cats


def _effective_tags(ctx: ActionContext) -> Set[str]:
    """The tool-side EffectTags (primary), positive-only escalated by the target
    host and — only when NOT tool-side-local-only — the name verb map."""
    tags: Set[str] = {coerce(t) for t in ctx.effect_tags} or {EffectTag.UNKNOWN.value}

    network = ctx.target_is_network and not ctx.operator_confirmed_read_only

    # A tool whose tool-side classification is purely local (and not network /
    # unknown) is NOT escalated by its NAME — this is what keeps a local
    # `send_summary_to_log` from being mis-read as SEND_MESSAGE.
    local_only = (
        EffectTag.UNKNOWN.value not in tags
        and tags <= _LOCAL_TAGS
        and not network
    )

    if network:
        # a network-reachable target is a POSITIVE classification (known external
        # mutation) — no longer UNKNOWN, so it gates as REQUIRE_APPROVAL not DENY.
        tags.add(EffectTag.NET_MUTATE.value)
        tags.discard(EffectTag.UNKNOWN.value)

    if not local_only:
        cats = _name_categories(ctx.tool)
        if cats:
            if "money" in cats:
                tags.add(EffectTag.MONEY_MOVE.value)
            if "message" in cats:
                tags.add(EffectTag.SEND_MESSAGE.value)
            if "mutate" in cats:
                tags.add(EffectTag.NET_MUTATE.value)
            if "delete" in cats:
                # a delete on a network target is a remote mutation; otherwise local
                tags.add(EffectTag.NET_MUTATE.value if network else EffectTag.LOCAL_DELETE.value)
            # the name gave us a positive classification ⇒ no longer UNKNOWN
            tags.discard(EffectTag.UNKNOWN.value)

    return tags


def _high_severity_signal(ctx: ActionContext, tags: Set[str]) -> bool:
    """The escalators that make an UNKNOWN effect fail closed to DENY."""
    return (
        ctx.irreversible
        or ctx.is_destructive_param
        or any(is_high_severity(t) for t in tags)
    )


def evaluate_action(ctx: ActionContext) -> Tuple[Verdict, str]:
    """Score one effectful call. Deterministic; never consults an LLM."""
    if ctx.denied_by_policy:
        return Verdict.DENY, "explicit policy denial"

    tags = _effective_tags(ctx)
    unknown = EffectTag.UNKNOWN.value in tags

    if unknown:
        # two-band UNKNOWN rule
        if _high_severity_signal(ctx, tags):
            return (Verdict.DENY,
                    "unclassifiable effect with a high-severity signal "
                    "(irreversible/destructive/financial) — refusing rather than "
                    "posting a rubber-stampable approval")
        return (Verdict.REQUIRE_APPROVAL,
                "unclassifiable effect — gated (dangerous-until-proven)")

    # known effects
    if ctx.is_destructive_param:
        return Verdict.REQUIRE_APPROVAL, "destructive parameter signal"
    if ctx.irreversible:
        return Verdict.REQUIRE_APPROVAL, "irreversible action"
    if tags & _APPROVAL_TAGS:
        return (Verdict.REQUIRE_APPROVAL,
                "external mutation / delete / money / message effect")
    if not ctx.classification_trusted:
        # a discovered/registry/first-use tool making any effectful call is gated
        # regardless of a self-declared read-only hint
        return (Verdict.REQUIRE_APPROVAL,
                "unconfirmed discovered/registry tool — gated on first effectful use")

    # reversible local read/write or a plain net-read ⇒ frictionless majority
    return Verdict.ALLOW, "reversible/local or read-only effect"


def should_mask(ctx: ActionContext) -> bool:
    """MASK is orthogonal to the action verdict: it flags secret-bearing args for
    redaction in logs/cards/evidence (wired in the evidence pipeline, §4.4)."""
    preview = " ".join(str(v) for v in ctx.args_preview.values()).lower()
    return any(k in preview for k in ("token", "password", "secret", "api_key", "authorization"))
