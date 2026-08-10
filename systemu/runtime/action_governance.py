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
  * **`shell_exec` is an APPROVAL-band effect, and STAYS in `_LOCAL_TAGS`.**
    Those are not in tension — the two sets answer different questions.
    `_APPROVAL_TAGS` asks "must the operator see this?": yes, because a shell can
    run ANYTHING. "Local" describes a shell's EGRESS class (it does not itself
    reach the network), not its BLAST RADIUS (it can `curl`, `rm`, or install).
    `_LOCAL_TAGS` asks "may the NAME verb-map escalate this tool?": no, because
    the tool-side tags already classify it, so a shell tool called
    `run_deploy_script` must not silently acquire NET_MUTATE from the token
    "deploy" (the same no-false-positive rule that protects
    `send_summary_to_log`). `local_delete` is in BOTH sets for exactly this
    reason and is the precedent being mirrored. Removing `shell_exec` from
    `_LOCAL_TAGS` would not tighten anything — it would only manufacture phantom
    network/message tags from tool names.

    **A third question the verdict does NOT answer (F9): may this effect class be
    BATCH-approved?** REQUIRE_APPROVAL means "the operator sees this call, with its
    arguments". A batch removes the card AND the arguments, permanently, so the
    REQUIRE_APPROVAL band is not a safe batch — reading it as one is how
    `run_command` and `file_delete` came to be offered under a single click on the
    first-run review card. :func:`batch_approvable` answers that question against
    the `effect_tags.BATCH_APPROVABLE` allowlist.

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
    # F14 — driving a browser IS network egress, and saying so here is what keeps
    # the pre-S2 hard-DENY firing on a FORGED tool that reaches the net through
    # playwright/BrowserPool rather than through `requests`. It is deliberately
    # recorded as egressing even though the operator ruling of 2026-08-07 also
    # admits it to `effect_tags.BATCH_APPROVABLE`: those answer different
    # questions, and falsifying this one to keep a set-disjointness assertion
    # green would be the F9 defect in a new costume.
    EffectTag.BROWSER_ACTUATE.value,
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
               EffectTag.LOCAL_DELETE.value, EffectTag.SHELL_EXEC.value,
               # F14: a body PROVED to have no effects must not acquire one from
               # its name — the `send_summary_to_log` rule, at its limit. The
               # capture/actuation classes are deliberately NOT here: they are not
               # local, so the name verb map may still escalate them.
               EffectTag.NO_EFFECT.value}
_APPROVAL_TAGS = {EffectTag.NET_MUTATE.value, EffectTag.SEND_MESSAGE.value,
                  EffectTag.MONEY_MOVE.value,
                  EffectTag.LOCAL_DELETE.value, EffectTag.OAUTH_CALL.value,
                  EffectTag.SHELL_EXEC.value,
                  # F14 — "must the operator SEE this call?" Yes, for every one of
                  # these: a screenshot and a clipboard read travel to the model
                  # provider, synthetic input lands in whatever window has focus, a
                  # browser action carries that browser's live session, and a toast
                  # can impersonate a system dialog. The operator ruling of
                  # 2026-08-07 lets FOUR of them be waived in one deliberate batch
                  # (`effect_tags.OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07`); it
                  # does not make any individual call frictionless by default.
                  EffectTag.SCREEN_CAPTURE.value, EffectTag.CLIPBOARD_READ.value,
                  EffectTag.CLIPBOARD_WRITE.value, EffectTag.INPUT_SYNTHESIS.value,
                  EffectTag.BROWSER_ACTUATE.value, EffectTag.DESKTOP_NOTIFY.value}

# ── F14 — which tool-side classifications SUPPRESS the name verb-map ─────────
#
# The set `_effective_tags` actually consults. Its question is the one `_LOCAL_TAGS`
# has always answered — "may the NAME verb-map escalate this tool?" — and the answer
# is no whenever the tool-side tags ALREADY classify it, because the map is a
# fallback for tools that are unclassified, not a second opinion about ones that are
# not. That is the `send_summary_to_log` / `run_deploy_script` rule.
#
# WHY THE DESKTOP CAPTURE/ACTUATION CLASSES JOIN IT. Measured on the shipped catalog:
# `type_text`, tagged `input_synthesis` from its `pynput` body, was scored
# `{input_synthesis, send_message}` because the token "text" is in `_MESSAGE_VERBS`
# ("text the customer"). That is a phantom effect — the tool sends no message — and
# it excluded a tool the operator explicitly ruled batch-approvable, on a reason the
# card would have printed as messaging. Same shape for `notify_desktop` and the
# token "notify".
#
# WHY `browser_actuate` IS DELIBERATELY NOT HERE. The restriction to non-network
# classes is the load-bearing half of the original rule. For a tool that reaches the
# network, the structural scan can see THAT it egresses but not WHAT for, and the
# name is the only signal separating a fetch from a payment from a message — so the
# map must keep escalating it. `browser_actuate` is a network class (it is in
# NET_EFFECTS), so `submit_expense_via_browser` still picks up NET_MUTATE and still
# leaves the batch. The desktop classes carry no such ambiguity: there is no network
# dimension for the name to disambiguate.
#
# The residual is the one `_LOCAL_TAGS` has always carried: a capture tool that ALSO
# egresses is protected by the SCAN finding the egress sink (requests/urlopen/smtplib
# /the curated `effect_signals` map), not by its name — pinned in
# `test_a_capture_tool_that_also_egresses_is_caught_by_the_SCAN_not_the_name`.
_NAME_ESCALATION_EXEMPT = _LOCAL_TAGS | {
    EffectTag.SCREEN_CAPTURE.value,
    EffectTag.CLIPBOARD_READ.value,
    EffectTag.CLIPBOARD_WRITE.value,
    EffectTag.INPUT_SYNTHESIS.value,
    EffectTag.DESKTOP_NOTIFY.value,
}


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
                "external mutation / delete / money / message / shell-exec effect")
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

    # A tool whose tool-side classification is already complete and carries no
    # network dimension (see `_NAME_ESCALATION_EXEMPT`) is NOT escalated by its
    # NAME — this is what keeps a local `send_summary_to_log` from being mis-read
    # as SEND_MESSAGE, and `type_text` from being mis-read as one off the token
    # "text".
    local_only = (
        EffectTag.UNKNOWN.value not in tags
        and tags <= _NAME_ESCALATION_EXEMPT
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


def effective_tags(ctx: ActionContext) -> Set[str]:
    """PUBLIC: the tag set ``evaluate_action`` ACTUALLY scored for *ctx*.

    Callers that need to disclose or re-check a verdict's effect basis must use
    THIS, never ``ctx.effect_tags`` (the raw DECLARED tags). The two differ
    whenever the scorer escalated: an untagged ``wire_funds`` declares nothing
    but scores ``money_move`` off the name verb map, and a tool declaring only
    ``net_read`` scores ``{net_read, money_move}``. A consumer that reads the
    declared set therefore sees a benign classification for a call the governor
    judged dangerous — which is exactly how a money-move gate reached one-tap
    remote approval (``messaging.decision_bridge.classify_resolution``).

    Deliberately NOT the tool signature's input: ``command_approvals.tool_signature``
    keys on the DECLARED tags, and re-keying it on the effective set would
    invalidate every stored approval.
    """
    return _effective_tags(ctx)


# Operator-facing phrases for the effect classes a batch may not contain. Used ONLY to
# say WHY a tool was excluded; an unlisted class falls back to its own tag value, so a
# new effect class still produces an honest reason instead of a blank one.
_EXCLUSION_PHRASE = {
    EffectTag.SHELL_EXEC.value: "shell execution",
    EffectTag.LOCAL_DELETE.value: "deletion",
    EffectTag.NET_READ.value: "network egress",
    EffectTag.NET_MUTATE.value: "network mutation",
    EffectTag.SEND_MESSAGE.value: "messaging",
    EffectTag.MONEY_MOVE.value: "money movement",
    EffectTag.OAUTH_CALL.value: "OAuth / credential use",
    # F14 — the capture/actuation classes. Four of these are batch-approvable by
    # the 2026-08-07 ruling and so never reach this map in practice; the two that
    # are NOT (clipboard_write, desktop_notify) need an honest phrase, and all six
    # can still be named when the batch is refused for a DIFFERENT reason.
    EffectTag.SCREEN_CAPTURE.value: "screen capture",
    EffectTag.CLIPBOARD_READ.value: "clipboard read",
    EffectTag.CLIPBOARD_WRITE.value: "clipboard replacement",
    EffectTag.INPUT_SYNTHESIS.value: "synthetic keyboard / mouse input",
    EffectTag.BROWSER_ACTUATE.value: "browser actuation",
    EffectTag.DESKTOP_NOTIFY.value: "desktop notification",
}


def batch_approvable(ctx: ActionContext) -> Tuple[bool, str]:
    """F9 — may this action class receive a STANDING, UNATTENDED, BLANKET allow?

    Returns ``(ok, reason)``. This is a STRICTLY NARROWER question than the verdict:
    ``evaluate_action`` asks "must the operator see THIS call?", which a shell tool
    answers with REQUIRE_APPROVAL — an approvable card, per call, with the arguments in
    front of the operator. Batch approval removes both the card AND the arguments,
    forever, so the REQUIRE_APPROVAL band is not a safe batch. It was being used as one:
    the first-run review card offered `run_command` and `file_delete` in a single click.

    Two rules, in this order:

      1. an UNKNOWN effect is refused — we cannot grant blanket permission for something
         we could not classify, and empty ``effect_tags`` (the commonest backfill
         outcome, and what every desktop actuator in the seed catalog carries) reaches
         here as UNKNOWN via ``_effective_tags``;
      2. every remaining class must be in the ``BATCH_APPROVABLE`` ALLOWLIST.

    Scored on :func:`effective_tags` — the ESCALATED set — NEVER on ``ctx.effect_tags``.
    An untagged ``wire_payout`` declares nothing and scores ``money_move`` off the name
    verb map; reading the declared set is how a money-move gate once reached one-tap
    approval (``messaging.decision_bridge.classify_resolution``). A tool's own
    declaration may add information here, never remove an escalation.
    """
    from systemu.runtime.effect_tags import (batch_approvable_tags, coerce as _coerce,
                                             is_batch_approvable_tag)

    tags = _effective_tags(ctx)
    declared = {_coerce(t) for t in (ctx.effect_tags or ())}
    declared.discard(EffectTag.UNKNOWN.value)

    def _name(t: str) -> str:
        """Name one offending class — and say whether the TOOL declared it.

        A tag the scorer ADDED is a governor INFERENCE, not a statement by the tool, and
        the card must not present the two as the same kind of fact. Unqualified, the
        rendered line read ``file_list_dir — unclassified [excluded: network mutation]``:
        self-contradictory on its face, and a false assertion about what that tool does
        (the name verb map fires on the token "file"). A reason that overstates is the
        same defect class as a promise that overstates — which is what F9 was.

        Kept SHORT on purpose: this string is rendered once per excluded tool on a card
        that is persisted in the decision context and drawn in the dashboard, where an
        oversized detail block is clipped at 8000 chars (R-UX2). The full rule is stated
        ONCE at the top of the card; these are labels, not explanations.
        """
        phrase = _EXCLUSION_PHRASE.get(t, "high-authority effect")
        return f"{phrase} ({t})" if t in declared else (
            f"{phrase} ({t} — name-inferred, not declared)")

    outside = sorted(t for t in tags if not is_batch_approvable_tag(t))

    if not declared:
        # Lead with the honest headline: this tool told us NOTHING. Whatever the
        # governor inferred is secondary and is labelled as an inference.
        why = "declares no effects — nothing was classified"
        inferred = [t for t in outside if t != EffectTag.UNKNOWN.value]
        if inferred:
            why += "; name suggests " + ", ".join(_name(t) for t in inferred)
        return False, why

    if EffectTag.UNKNOWN.value in tags:
        return False, "carries an unclassified effect alongside its declared ones"
    if outside:
        return False, "; ".join(_name(t) for t in outside)
    # NOT "reversible local effect" any more. That phrase was accurate while the
    # allowlist held only `local_read`/`local_write`; after the 2026-08-07 ruling it
    # would describe a screen capture and a browser action as reversible and local,
    # which is neither true nor the basis on which they were admitted. The set is
    # named instead of characterised, so the sentence cannot go stale the next time
    # the set changes.
    return True, ("fully classified, and every class is in the batch-approvable "
                  "allowlist (" + ", ".join(batch_approvable_tags()) + ")")


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
