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


# ── R-A14a §15.1(a) / IMPL-13 / DEC-1 — the forged-network HARD-DENY ──────────
# The network egress effect classes. A forged/untrusted tool carrying ANY of
# these EGRESSES; pre-S2 there is NO OS-kernel egress jail, so it would run with
# UNRESTRICTED network access (the hole S2 closes). Broader than MUST_ISOLATE:
# net_read is included — a forged tool that merely READS the network still
# egresses (exfiltration), and there is no jail to bound it. IMPL-13: "no kernel
# enforcer ⇒ forged-network DENY; the capability is absent, never silently
# ungated."
NET_EFFECTS = frozenset({
    EffectTag.NET_READ.value,
    EffectTag.NET_MUTATE.value,
    EffectTag.SEND_MESSAGE.value,
    EffectTag.MONEY_MOVE.value,
    EffectTag.OAUTH_CALL.value,
})


def has_network_egress(effect_tags: Iterable) -> bool:
    """True iff any effect tag is a network-egress class (NET_EFFECTS)."""
    return any(coerce(t) in NET_EFFECTS for t in (effect_tags or ()))


def _egress_enforcer_available() -> bool:
    """The S2 seam. There is NO OS-kernel egress jail today ⇒ False.

    When S2 (R-A8 Phase-2) lands the jailed spawn path in ``backend/local.py``,
    this flips to probe the enforcer's real availability; until then a forged
    network tool is DENIED (IMPL-13). The R-A8 Phase-1 spike already proved the
    zero-capability AppContainer blocks egress at zero privilege, so this DENY is
    the honest posture until that enforcer is wired — never a silent un-gate."""
    return False


# The honest, matchable BLOCKED reasons (an ``egress_enforcer_unavailable``-class
# refusal, never a rubber-stampable approval card).
EGRESS_ENFORCER_UNAVAILABLE = (
    "egress_enforcer_unavailable: refusing to run a forged/untrusted network "
    "tool — no OS-kernel egress jail (S2) exists yet, so it would run with "
    "unrestricted network access. This capability is absent until S2 ships, "
    "never silently ungated (IMPL-13 / DEC-1)."
)
EGRESS_ENFORCER_UNAVAILABLE_STDIO = (
    "egress_enforcer_unavailable: refusing to LAUNCH a registry/untrusted stdio "
    "MCP server — no OS-kernel egress jail (S2) exists yet, so its subprocess "
    "would egress unrestricted. Only operator-connected servers may launch until "
    "S2 ships (IMPL-13 / DEC-1)."
)


def _forged_source_has_network_egress(impl_path) -> bool:
    """Structurally scan a forged tool's ON-DISK source for a network-egress
    effect, INDEPENDENT of its (unreliable) stored/declared ``effect_tags``.

    The forged ``effect_tags`` CANNOT be trusted for this gate: a runtime-forged
    tool ships with ``effect_tags=[]`` (never stamped — the once-per-version boot
    backfill already ran before it was forged), and even a backfilled forged tool
    can DECLARE-AWAY its net tags via a self-authored ``TOOL_META`` (the backfill
    prefers the declaration and only floors ``money_move``). Both let a
    net-exfiltrating forged tool reach a rubber-stampable approval card instead of
    the DENY. So the gate re-derives net-egress from the source the backend is
    about to execute — mirroring ``tool_dry_run``'s empty-tag re-derivation — which
    an attacker-authored declaration cannot suppress.

    Returns True iff the source structurally egresses. A read/parse failure yields
    False: the tag check already ran (no net there either), the codebase governs a
    truly unclassifiable forged tool via REQUIRE_APPROVAL, and an unreadable /
    unparseable source cannot execute — so this never SILENTLY ungates a *known*
    net egress, and it never over-DENYs a legitimate local tool on a transient
    read hiccup. (The residual — an egress via a sink ``classify_source`` does not
    recognise — lands in REQUIRE_APPROVAL, the documented curation-bounded bound.)"""
    if not impl_path:
        return False
    try:
        from pathlib import Path
        src = Path(impl_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        from systemu.runtime.effect_tags import classify_source
        return has_network_egress(classify_source(src))
    except Exception:
        return False


def forged_network_denied(tool, *, impl_path=None) -> Optional[str]:
    """§15.1(a) hard-DENY predicate. Returns the honest BLOCKED reason iff *tool*
    is a forged/untrusted NETWORK actuator that must be refused pre-S2, else None.

    Fires ONLY for a tool that is (i) ``forged_by_systemu`` AND (ii) has a
    network-egress effect — established from its effect tags OR, because forged
    tags are UNRELIABLE (empty for runtime-forged tools; declare-away-able via a
    self-authored ``TOOL_META``), from a fresh STRUCTURAL scan of the source the
    backend is about to run — AND (iii) has no egress enforcer available today.
    ``impl_path`` (the resolved on-disk path the caller is about to execute) is
    scanned when provided; otherwise the tool's own ``implementation_path`` is used.

    Returns None for:
      * a non-forged BUILT-IN net tool (vetted repo code — gated, not denied),
        and by extension an operator-connected MCP tool (never ``forged``, and
        actuated via ``call_mcp_tool``, never the forged spawn);
      * a forged LOCAL-only tool (no network egress in tags OR source → unchanged
        REQUIRE_APPROVAL).

    Fail-closed: any error resolving the signals DENIES — a forged tool we cannot
    clear is refused, never launched-then-denied."""
    if tool is None:
        return None  # no Tool context — the None-isolation default governs elsewhere
    try:
        if not bool(getattr(tool, "forged_by_systemu", False)):
            return None  # built-in / operator-connected MCP → not this DENY
        net = has_network_egress(getattr(tool, "effect_tags", None) or ())
        if not net:
            # Tags say local-only, but forged tags are untrustworthy — re-derive
            # net-egress from the actual source (closes the empty-tag and the
            # declare-away holes). Scan the exact path the caller will execute.
            src_path = impl_path or getattr(tool, "implementation_path", None)
            net = _forged_source_has_network_egress(src_path)
        if not net:
            return None  # forged but local-only (tags AND source) → REQUIRE_APPROVAL
        if _egress_enforcer_available():
            return None  # S2 jail present → the jailed spawn path governs (future)
        return EGRESS_ENFORCER_UNAVAILABLE
    except Exception:
        # never let a classification hiccup weaken the DENY — fail toward refusal.
        return EGRESS_ENFORCER_UNAVAILABLE


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
    # IMPL-2: an effect class the OPERATOR assigned to a DENY-floored action via
    # typed-confirm (provenance `operator`, logged at the call site). Defeats ONLY the
    # UNKNOWN conjunct — never clears an independently-computed escalator, and never
    # makes the action frictionless. Absent on every ordinary call.
    operator_assigned_class: Optional[str] = None

    # A mistyped field name on a SECURITY context must be a loud error, not a silent
    # no-op that scores the call as though the signal were never supplied.
    # ``validate_assignment`` because the gate SETS ``operator_assigned_class`` after
    # construction — without it a wrong-typed value would reach the scorer unchecked.
    model_config = {"extra": "forbid", "validate_assignment": True}
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


def _assigned_class(ctx: ActionContext) -> Optional[str]:
    """The operator's assigned effect class, or None if there isn't a usable one.

    A value that does not coerce to a REAL tag classifies nothing, so it is not a
    reclassification at all — it must not defeat the UNKNOWN conjunct. Without this,
    ``coerce("garbage")`` returns ``unknown``, which would be added and then discarded:
    net effect, UNKNOWN silently stripped and nothing put in its place. Whitespace is
    normalised so a blank submission reads the same as no submission."""
    raw = (ctx.operator_assigned_class or "").strip()
    if not raw:
        return None
    assigned = coerce(raw)
    return None if assigned == EffectTag.UNKNOWN.value else assigned


def _score_known(ctx: ActionContext, tags: Set[str]) -> Tuple["Verdict", str]:
    """The known-effects ladder: what the gate does once the effect IS classified."""
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
    return Verdict.ALLOW, "reversible/local or read-only effect"


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

    # IMPL-2: an operator-assigned effect class is a POSITIVE classification, so it
    # defeats the UNKNOWN conjunct. It is strictly ADDITIVE, never subtractive —
    # independently-derived tags (the name verb map, a network target) still stand.
    # That is what stops "reclassify a wire_funds tool as local_read" from stripping its
    # money escalator: the operator's class is never the SOLE severity input.
    assigned = _assigned_class(ctx)
    if assigned:
        tags.add(assigned)
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

    # known effects — either natively classified, or classified BY THE OPERATOR (IMPL-2).
    verdict, why = _score_known(ctx, tags)

    # IMPL-2 re-arbitration. A DENY is operator-remediable but never rubber-stampable:
    # the operator assigns the real effect class (typed-confirm, logged), and the gate
    # re-runs this SAME ladder over the reclassified tags plus the UNTOUCHED raw signals.
    # So the remedy genuinely works — a refusal becomes an honest approval card — while
    # the operator's label can never erase an independently-computed signal: destructive
    # parameters and irreversibility are facts about the CALL, and a name-derived money
    # or network escalation survives because the assigned class is additive, not a
    # replacement. The one thing reclassification may never buy is silence: an action
    # that was refused once does not become frictionless, so ALLOW is floored to an
    # approval card (spec AC-d: never ALLOW).
    if _assigned_class(ctx) and verdict is Verdict.ALLOW:
        return (Verdict.REQUIRE_APPROVAL,
                "operator-reclassified effect — approvable on the new classification")
    return verdict, why


def should_mask(ctx: ActionContext) -> bool:
    """MASK is orthogonal to the action verdict: it flags secret-bearing args for
    redaction in logs/cards/evidence (wired in the evidence pipeline, §4.4)."""
    preview = " ".join(str(v) for v in ctx.args_preview.values()).lower()
    return any(k in preview for k in ("token", "password", "secret", "api_key", "authorization"))
