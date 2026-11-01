"""Bridge parked decisions to a chat channel (Telegram) and back.

Telegram caps ``callback_data`` at 64 bytes, so a parked decision is
referenced by a short deterministic *tag* (a base32 slice of the
decision-id hash) rather than the full id.  This module holds the pure,
side-effect-free helpers for that mapping:

    decision_tag       — deterministic 6-char base32-lower tag for an id
    disambiguate_tag   — extend to 8 chars only when the 6-char tag collides
    callback_token     — pack ``d|<tag>|<choice_key>`` for Telegram callback_data
    parse_callback     — parse that token back, tolerantly (never raises)
    classify_resolution — SEC-1 fail-closed remote-resolvability predicate
    resolve_from_channel — the RESOLUTION PATH: resolve a parked decision from a
                         chat channel through the SAME dashboard resolve fn, gated
                         by the persisted resolution_class bit (SEC-1). This is
                         the server-side gate a phone (or a forged callback) hits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # Python 3.8+: Literal lives in typing
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing_extensions import Literal  # type: ignore

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_CHOICE_KEYS = {"a1", "a2", "a3", "a4"}


# ── SEC-1: remote-resolvability classification ────────────────────────────────
#
# R-P1 lets an operator resolve a parked decision from Telegram. That is only
# safe for a NARROW set of decision shapes; everything else must stay
# dashboard-only. ``classify_resolution`` is the ALLOWLIST-style predicate that
# decides which. It is intentionally fail-closed: the DEFAULT is ``"floor"``
# and a decision only becomes ``"remotely_resolvable"`` when it POSITIVELY
# matches a recognized safe shape. "Buttons are opt-in per recognized shape,
# never the default."
#
# The value is stamped into the decision context at creation time
# (OperatorDecisionQueue.post) and PERSISTED, so the resolution-path guard reads
# a frozen classification rather than re-deriving it on the untrusted wire path.

RESOLUTION_REMOTE = "remotely_resolvable"
RESOLUTION_FLOOR = "floor"

# Gate types that CAN be resolved remotely once every floor check below passes.
# This is an ALLOWLIST — an unrecognized gate_type floors.
#
# SEC-1 (R-P1 finding 3/4/5): ONLY ``command`` and ``tool``. Two independent
# reasons, both load-bearing:
#   (a) spec §3 rule (a) scopes the remote inline buttons to command/tool (an
#       MCP-OAuth / credential handoff resolves via a URL/dashboard, not a tap).
#   (b) command/tool are the ONLY gate types whose REMOTE resolution has a
#       WORKING cross-process resume rail: ``resume_on_decision`` /
#       ``scheduler.jobs.reconcile_resolved_stuck_decisions`` re-dispatch a
#       resolved command/tool gate off ``queue.resolve()`` alone. scroll / dep /
#       forge / mcp_call need ``inbox.resolve_gate()`` (approve_pending_scroll /
#       approve_and_install / forge_tool_from_spec) to actually EXECUTE — which
#       ``resolve_from_channel`` deliberately does NOT call (running the
#       authorized action inside the messaging process is wrong). So a remotely
#       "approved" scroll/dep/forge/mcp_call would be marked resolved yet never
#       run — silently stuck. Flooring them here is the correct fix: they never
#       reach that broken second-rail path.
_REMOTE_GATE_TYPES = frozenset({
    "tool", "command",
})

# Gate types that ALWAYS floor: policy/posture changes, credential handoffs,
# raw-model sampling, and the "tools are blocked, loosen posture?" prompt.
# Resolving any of these from a phone would loosen the security posture or move
# a credential — exactly what §2.1 keeps dashboard-only.
_FLOOR_GATE_TYPES = frozenset({
    "evolution", "recovery", "tools_blocked", "sampling", "mcp_oauth",
})

# effect_tags that floor a gate regardless of gate_type: money movement and any
# explicitly-irreversible effect. This is a DENYLIST of known-dangerous classes,
# and it answers only "is this classified effect dangerous?".
#
# It deliberately does NOT contain "unknown", and adding it would close nothing:
# the check is a set INTERSECTION, and the unclassified cases that matter are an
# EMPTY list (intersects nothing) — not a list carrying the literal "unknown".
# The separate, prior question — "is there a classification AT ALL?" — is asked
# in step 3 of classify_resolution, which floors an empty / unknown-only list.
#
# (Historical note: the "an unknown tag does not by itself floor a recognized
# safe gate_type — the gate_type allowlist stays the backstop" rationale that
# used to sit here was written in c4bfae0e, when this predicate was
# exclusion-style and the allowlist held six gate types. 28119ca9 inverted the
# design to positive-proof and narrowed the allowlist to {tool, command} — i.e.
# to exactly the two types an unclassified gate rides in on — so the allowlist
# is no longer a backstop for this case. The rationale was never updated to
# match; it is not evidence that unclassified effects were meant to stay
# remotely approvable.)
_FLOOR_EFFECT_TAGS = frozenset({
    "money_move", "irreversible", "money_spend", "payment", "funds_move",
})

# The scorer's "nothing was classified" sentinel. Mirrors
# ``runtime.effect_tags.EffectTag.UNKNOWN.value``; kept as a literal so this
# module stays import-light (decision_queue imports it LAZILY to dodge an
# approval<->messaging import cycle).
_UNKNOWN_EFFECT_TAG = "unknown"

# The ONLY verdict values that permit a remote (tap-to-approve) resolution. The
# governor's HarnessDecision enum uses ``grant``/``deny``/``escalate``; the
# command/tool action gate (gate.py from_tool / action_governance.Verdict) uses
# ``require_approval``/``allow``/``deny``/``mask``. A remote tap is meaningful
# only when the action WOULD run on approval — so ``grant`` / ``require_approval``
# / ``allow``. Everything else (``deny``, ``escalate``, ``mask``, "", absent)
# floors: a deny is not a phone tap, an escalate is a posture question, and mask
# is applied automatically (no operator choice).
_REMOTE_VERDICTS = frozenset({"grant", "require_approval", "allow"})

# Keys a caller MAY set to force a typed-confirmation floor.
#
# IMPL-2: ``tool_sandbox._maybe_gate_tool`` now sets ``requires_typed_confirm`` on every
# operator-RECLASSIFIED follow-up card, so this is live, not merely defensive. (It was
# accurate when written — the DENY typed-confirm was unbuilt then, and the amend
# band-increase confirm is still derived live at dashboard resolve time rather than
# persisted.) The floor it produces is the point: an effect the governor once
# DENY-floored must never become a one-tap remote approval, and the remote lane has no
# reclassify surface to offer instead. The remaining keys stay honored defensively so a
# future persisted marker fails closed.
_TYPED_CONFIRM_KEYS = ("requires_typed_confirm", "typed_confirm",
                       "require_typed_confirm")

# Max enum options for a single-choice elicitation to stay phone-resolvable.
# Telegram inline keyboards are cramped; a long enum is a form, not a tap.
_MAX_REMOTE_ENUM = 4


def _is_elicitation(context: Dict[str, Any]) -> bool:
    """True iff the decision is a QUESTION (structured question / form), whatever
    it rides on. A harness INPUT gate is ``kind="gate"`` but carries a
    ``requested_schema``: it is a question, and its Deny/Approve options are not
    answers. One predicate for classify, render and resolve."""
    requested_schema = context.get("requested_schema")
    return bool(context.get("kind") == "structured_question"
                or isinstance(requested_schema, dict) and requested_schema)


def _elicitation_field_remote(props: Dict[str, Any]) -> bool:
    """True iff a single-field elicitation is safe to resolve remotely.

    Exactly ONE property, which is either a plain free-text string OR an enum
    with ``<= _MAX_REMOTE_ENUM`` options, AND the field is NOT a secret. Zero
    fields, multi-field, a large enum, or a secret field all floor.
    """
    if not isinstance(props, dict) or len(props) != 1:
        return False
    (name, spec), = props.items()
    if not isinstance(spec, dict):
        spec = {}
    # Secret detection reuses the elicitation module's is_secret_field, which
    # reads field["format"] and field["name"] — a properties entry has no
    # "name" key, so inject the property name before the check.
    try:
        from systemu.runtime.elicitation import is_secret_field
        if is_secret_field({**spec, "name": name}):
            return False
    except Exception:
        # If we cannot evaluate secrecy, fail closed (treat as unsafe).
        return False
    enum = spec.get("enum")
    if isinstance(enum, list):
        return 1 <= len(enum) <= _MAX_REMOTE_ENUM
    # A plain string field (free text). Anything else (object/array/number
    # without an enum, or a missing/odd type) is not a recognized remote shape.
    ftype = spec.get("type", "string")
    return ftype == "string"


def classify_resolution(context: Any) -> str:
    """Classify a decision's remote-resolvability. SEC-1, fail-closed.

    Returns ``"remotely_resolvable"`` ONLY for a positively-recognized safe
    shape; returns ``"floor"`` for everything else (the default, including any
    non-dict / malformed input). Never raises.
    """
    if not isinstance(context, dict):
        return RESOLUTION_FLOOR

    kind = context.get("kind")
    gate_type = context.get("gate_type")

    # ── ELICITATION (structured question / form) ─────────────────────────────
    # A structured_question, or anything carrying a requested_schema, is an
    # elicitation. It is remote only as a single safe field.
    requested_schema = context.get("requested_schema")
    if _is_elicitation(context):
        schema = requested_schema if isinstance(requested_schema, dict) else {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        if _elicitation_field_remote(props or {}):
            return RESOLUTION_REMOTE
        return RESOLUTION_FLOOR

    # ── GATE (a concrete "do this action?" prompt) ───────────────────────────
    # A gate is identified by kind=="gate" OR a recognized gate_type. Anything
    # else is unrecognized and floors.
    if kind == "gate" or gate_type in _REMOTE_GATE_TYPES or gate_type in _FLOOR_GATE_TYPES:
        # A gate becomes remotely_resolvable ONLY when it POSITIVELY proves it is
        # safe on EVERY axis below. This is a hard reversal of the earlier
        # "floor only on a matched exclusion" logic: the REAL gate-creation paths
        # (GateDescriptor.to_decision_context + the sandbox context_extras) did
        # not persist verdict/effect_tags, so the exclusion checks silently never
        # fired and DENY / money / destructive gates sailed through to remote.
        # ABSENCE of the safety evidence now → floor (fail-closed). Only when the
        # sandbox actually stamps a clean verdict + a clean effect_tags list (and
        # nothing else trips) does a benign command/tool gate stay remote.

        # 1. gate_type must be one of the two remotely-resumable actions.
        if gate_type not in _REMOTE_GATE_TYPES:
            return RESOLUTION_FLOOR

        # 2. verdict KEY must be PRESENT and an affirmative-ish value. Absent
        #    verdict, "deny", "escalate", "" → floor.
        if "verdict" not in context:
            return RESOLUTION_FLOOR
        verdict = str(context.get("verdict") or "").lower()
        if verdict not in _REMOTE_VERDICTS:
            return RESOLUTION_FLOOR

        # 3. effect_tags KEY must be PRESENT, be a list, carry a POSITIVE
        #    classification, and be DISJOINT from the money/irreversible floor
        #    set. Absent effect_tags → floor.
        #
        #    The POSITIVE-classification requirement is load-bearing. An EMPTY
        #    list is the ABSENCE of a classification, not a finding of "no
        #    effect" — but it is "present", "a list", and disjoint from every
        #    denylist, so it used to satisfy all three tests and go REMOTE. That
        #    inverted the whole point of the card: the governor raises a
        #    REQUIRE_APPROVAL on an untagged tool precisely BECAUSE it could not
        #    classify the effect ("unclassifiable effect — gated
        #    (dangerous-until-proven)", action_governance.evaluate_action), and
        #    that card then became one-tap approvable from a phone. Worse, the
        #    name-verb escalator means an untagged `wire_funds` scores
        #    MONEY_MOVE — so the very cards the money floor exists for were the
        #    ones sailing past it.
        #
        #    Three siblings already read empty as UNKNOWN and fail closed on it;
        #    this was the one consumer that did not:
        #      * action_governance._effective_tags  — empty ⇒ {UNKNOWN}
        #      * financial_signal._is_unknown_effect — empty OR unknown-only ⇒
        #        UNKNOWN, feeding the fail-closed money-move net
        #      * tool_dry_run._gate_skip_reason      — "empty ⇒ UNKNOWN ⇒ don't
        #        trust"
        #
        #    Cost check ("does this make remote approval useless?"): no. The
        #    population R-P1 Part C was built to keep on the phone is a BENIGN,
        #    POSITIVELY-classified tool gate — its worked example is a
        #    `net_mutate` tool — and every such gate still passes. What loses
        #    the phone lane is exactly the set of actions the system cannot
        #    describe, which fall back to the dashboard (SURFACE_DASHBOARD_ONLY),
        #    the designed route. Friction lands on the least-understood actions.
        if "effect_tags" not in context:
            return RESOLUTION_FLOOR
        effect_tags = context.get("effect_tags")
        if not isinstance(effect_tags, list):
            return RESOLUTION_FLOOR
        tags = {str(t).strip().lower() for t in effect_tags}
        tags.discard("")            # a blank entry classifies nothing
        if not tags or _UNKNOWN_EFFECT_TAG in tags:
            return RESOLUTION_FLOOR
        if tags & _FLOOR_EFFECT_TAGS:
            return RESOLUTION_FLOOR

        # 4. A destructive-parameter signal floors (defense in depth — the
        #    sandbox stamps this for a destructive command/tool call).
        if context.get("destructive"):
            return RESOLUTION_FLOOR

        # 5. Typed-confirmation marker floors (defensive: a future persisted flag
        #    fails closed rather than tapping past a required typed confirm).
        if any(context.get(k) for k in _TYPED_CONFIRM_KEYS):
            return RESOLUTION_FLOOR

        # POSITIVE: a command/tool gate that proved safe on every axis.
        return RESOLUTION_REMOTE

    # ── Everything else → floor (the default) ────────────────────────────────
    return RESOLUTION_FLOOR


# ── R-P1 Task 4: push rendering (surface hint + positional option buttons) ────
#
# ``render_options`` decides HOW a parked decision reaches the operator's phone:
# tappable inline buttons, a "/answer" reply hint, or a "needs the dashboard"
# note. It is keyed FIRST on the PERSISTED ``resolution_class`` bit — buttons are
# opt-in per recognized shape, never the default — then on the decision's shape.
#
# CRITICAL wire contract: a button's ``a{i+1}`` key means position ``i`` of the
# list the button was RENDERED from, and the inbound resolver
# (``resolve_from_channel``) maps it back against the SAME list: an option card
# against ``decision.options``, a one-field enum QUESTION against its enum
# values. Mapping an enum button onto ``decision.options`` (a question's
# placeholder "Submit", or a harness gate's Deny/Approve) records the wrong
# thing - that was the shipped defect.

# Surface hints the push layer understands.
SURFACE_BUTTONS = "buttons"          # inline tappable option buttons
SURFACE_REPLY = "reply"              # free-text: operator sends /answer <tag> <value>
SURFACE_DASHBOARD_ONLY = "dashboard_only"  # no remote resolution — open the dashboard


class DecisionPush(BaseModel):
    """The rendered push shape for a parked decision.

    Carries the short ``tag``, the chosen ``surface_hint`` (buttons / reply /
    dashboard_only), and the POSITIONAL ``options`` (``(choice_key, label)``
    pairs, ``a1``->options[0] …). The push layer turns ``options`` into inline
    buttons via ``callback_token(tag, choice_key)`` — the same positional key the
    inbound resolver maps back to ``decision.options[i]``.
    """

    tag: str
    surface_hint: Literal["buttons", "reply", "dashboard_only"]
    options: List[Tuple[str, str]] = Field(default_factory=list)


def _gate_is_button_shaped(decision: Any, context: Dict[str, Any]) -> bool:
    """True iff this remotely-resolvable decision is a gate that renders as
    buttons: a recognized gate (kind=="gate" or a remote gate_type) whose option
    list is a tappable size (1..4). A 5+-option gate is a form, not a tap."""
    kind = context.get("kind")
    gate_type = context.get("gate_type")
    is_gate = kind == "gate" or gate_type in _REMOTE_GATE_TYPES
    if not is_gate:
        return False
    options = list(getattr(decision, "options", []) or [])
    return 1 <= len(options) <= _MAX_REMOTE_ENUM


def _single_field_spec(context: Dict[str, Any]):
    """Return the (name, spec) of a single-field elicitation, or None.

    Mirrors ``classify_resolution``'s elicitation shape check so the two stay
    consistent: exactly one property, non-secret. Returns None for zero /
    multi-field / secret / non-elicitation.
    """
    requested_schema = context.get("requested_schema")
    if not _is_elicitation(context):
        return None
    schema = requested_schema if isinstance(requested_schema, dict) else {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or len(props) != 1:
        return None
    (name, spec), = props.items()
    if not isinstance(spec, dict):
        spec = {}
    # Reuse the elicitation secret detector (a secret field is never remote).
    try:
        from systemu.runtime.elicitation import is_secret_field
        if is_secret_field({**spec, "name": name}):
            return None
    except Exception:
        return None
    return (name, spec)


def render_options(decision: Any, *, tag: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Decide the push surface for a parked ``decision``.

    Returns ``(surface_hint, options)`` where ``options`` is a list of
    ``(choice_key, label)`` pairs with POSITIONAL keys into the list the labels
    came from (``decision.options`` for a gate, the enum for a question).

    Keyed FIRST on the persisted ``resolution_class`` (fail-closed: anything but
    ``"remotely_resolvable"`` — including absent — is ``dashboard_only``), then on
    shape. A QUESTION is decided before any gate shape, because a one-field
    question can ride a gate (a harness INPUT gate) whose Deny/Approve options
    are not answers:
      * single-field enum (<=4)           -> ``("buttons", positional enum values)``
      * single free-text string field     -> ``("reply", [])``  (operator /answers)
      * any other question shape          -> ``("dashboard_only", [])``
      * gate with 1..4 options            -> ``("buttons", positional options)``
      * 5+ / unknown                      -> ``("dashboard_only", [])``
    Never raises.
    """
    context = getattr(decision, "context", None) or {}
    if not isinstance(context, dict):
        return (SURFACE_DASHBOARD_ONLY, [])

    # SEC-1: buttons are opt-in per recognized shape, never the default. The
    # persisted resolution_class gates EVERYTHING — a floor (or absent) decision
    # is dashboard_only regardless of its otherwise button-shaped options.
    if context.get("resolution_class") != RESOLUTION_REMOTE:
        return (SURFACE_DASHBOARD_ONLY, [])

    # A question renders as a question - never as the gate's buttons.
    if _is_elicitation(context):
        field = _single_field_spec(context)
        if field is not None:
            _name, spec = field
            enum = spec.get("enum")
            if isinstance(enum, list) and 1 <= len(enum) <= _MAX_REMOTE_ENUM:
                return (SURFACE_BUTTONS,
                        [(f"a{i + 1}", str(v)) for i, v in enumerate(enum)])
            # A plain free-text string field → reply hint (operator /answers).
            if spec.get("type", "string") == "string":
                return (SURFACE_REPLY, [])
        return (SURFACE_DASHBOARD_ONLY, [])

    # Gate → positional buttons from the record's option list.
    if _gate_is_button_shaped(decision, context):
        options = list(getattr(decision, "options", []) or [])[:_MAX_REMOTE_ENUM]
        return (SURFACE_BUTTONS,
                [(f"a{i + 1}", label) for i, label in enumerate(options)])

    # 5+ / unrecognized → dashboard only.
    return (SURFACE_DASHBOARD_ONLY, [])


def _tag_full(decision_id: str) -> str:
    """The full base32-lowercased digest for a decision id."""
    digest = hashlib.sha256(decision_id.encode()).digest()
    return base64.b32encode(digest).decode("ascii").lower()


def decision_tag(decision_id: str) -> str:
    """Deterministic 6-char base32-lower tag for ``decision_id``."""
    return _tag_full(decision_id)[:6]


def disambiguate_tag(decision_id: str, open_tags: set[str]) -> str:
    """6-char tag, extended to 8 chars if it collides with an open tag."""
    tag = decision_tag(decision_id)
    if tag in open_tags:
        return _tag_full(decision_id)[:8]
    return tag


def callback_token(tag: str, choice_key: str) -> str:
    """Pack ``tag`` + ``choice_key`` into Telegram ``callback_data``."""
    return f"d|{tag}|{choice_key}"


def parse_callback(data) -> Optional[tuple[str, str]]:
    """Parse ``d|<tag>|<choice_key>`` back to ``(tag, choice_key)``.

    Returns ``None`` for anything that isn't exactly that shape with a
    recognised choice key.  Never raises (tolerates ``None``/non-str).
    """
    if not isinstance(data, str):
        return None
    parts = data.split("|")
    if len(parts) != 3:
        return None
    prefix, tag, choice_key = parts
    if prefix != "d" or not tag or choice_key not in _CHOICE_KEYS:
        return None
    return (tag, choice_key)


# ── The typed answer grammar: ``/answer <tag> <value>`` ───────────────────────
#
# ONE grammar, used by BOTH sides: the push prints ``answer_example(tag)`` and
# the handler parses with ``parse_answer_args`` (registered under
# ``ANSWER_COMMAND``), so the promise and the parser cannot drift apart again.
# ``<value>`` is the rest of the message: free text for a free-text question, an
# option's label (case-insensitive) or 1-based number for an enum question or an
# option card (``a1..a4`` keep working).

ANSWER_COMMAND = "answer"
ANSWER_VALUE_PLACEHOLDER = "<value>"


def answer_example(tag: str, value: str = ANSWER_VALUE_PLACEHOLDER) -> str:
    """The literal command an operator types to answer decision ``tag``."""
    return f"/{ANSWER_COMMAND} {tag} {value}"


def parse_answer_args(args) -> Optional[Tuple[str, str]]:
    """``"<tag> <value...>"`` -> ``(tag, value)``; None when either is missing.

    The value keeps its inner whitespace (a free-text answer is a sentence)."""
    if not isinstance(args, str):
        return None
    parts = args.strip().split(None, 1)
    if len(parts) != 2:
        return None
    tag, value = parts[0], parts[1].strip()
    return (tag, value) if value else None


# ── R-P1 resolution path ──────────────────────────────────────────────────────
#
# ``resolve_from_channel`` is the server-side gate that a phone (or a FORGED
# Telegram callback) hits. It resolves a parked decision through the EXACT SAME
# path the dashboard/CLI/inbox use — ``OperatorDecisionQueue.resolve(id,
# choice=...)`` — so the existing resume rail (the daemon reconciler
# ``scheduler/jobs.reconcile_resolved_stuck_decisions`` + the EventBus subscriber
# ``runtime/resume_on_decision``) dispatches the resume for free off the
# persisted resolved decision. There is NO second resume rail here.
#
# SEC-1 (load-bearing): a decision is only remotely-resolvable when its PERSISTED
# ``context["resolution_class"] == "remotely_resolvable"``. Absent / anything else
# → refuse. The refusal rests on the frozen persisted bit (stamped at post()
# time by classify_resolution), NEVER on the outbound ``surface_hint`` — a forged
# callback could fake that, so we never read it here.

ResolveOutcome = Literal[
    "OK", "EXPIRED", "UNKNOWN_TAG", "BAD_CHOICE",
    "REFUSED_TYPED_CONFIRM", "RATE_LIMITED",
]

# The choice keys, in positional order. ``a1`` -> options[0], ..., ``a4`` ->
# options[3]. Only the INDEX comes off the wire; the label is re-derived from the
# decision record (never trusted from the wire).
_CHOICE_ORDER = ("a1", "a2", "a3", "a4")

_ALLOWLIST_ENV = "SHARING_ON_TELEGRAM_ALLOWED_USER_IDS"

# Per-sender in-memory sliding-window rate limit.
_RATE_MAX_PER_MIN = 20
_RATE_WINDOW_S = 60.0
_rate_lock = threading.Lock()
_rate_hits: Dict[str, List[float]] = {}

# Mask anything that looks secret before it lands in the audit row.
_SECRET_HINT = re.compile(r"(token|secret|password|passwd|api[_-]?key|bearer|cookie)",
                          re.IGNORECASE)


def _default_queue():
    """Production default: the real vault-backed OperatorDecisionQueue.

    Lazy so the pure helpers in this module never drag in the approval/vault
    stack, and so a test that injects its own ``queue`` never touches a vault.
    Mirrors the CLI/dashboard vault-open path (``Config.from_env()`` +
    ``open_vault`` — which respects ``SYSTEMU_STORAGE`` so we resolve against the
    SAME backend the dashboard writes to, not a divergent one).
    """
    from sharing_on.config import Config
    from systemu.vault.factory import open_vault
    from systemu.approval.decision_queue import OperatorDecisionQueue

    config = Config.from_env()
    vault = open_vault(config)
    return OperatorDecisionQueue(vault)


def _allowlist() -> set:
    from systemu.messaging.gateway import allowlist_from_env
    return allowlist_from_env(_ALLOWLIST_ENV)


def _rate_ok(sender_id: str, now_s: float) -> bool:
    """Sliding-window per-sender rate check. True iff this resolve is allowed;
    records the hit when allowed."""
    with _rate_lock:
        hits = _rate_hits.setdefault(sender_id, [])
        cutoff = now_s - _RATE_WINDOW_S
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= _RATE_MAX_PER_MIN:
            return False
        hits.append(now_s)
        return True


def _mask(value: Any) -> Any:
    """Redact a value that looks like a secret before auditing it.

    Two passes. A string carrying a hint word is dropped WHOLE, as before. Every
    other string goes through the tree's shared span primitive
    (``gateway.mask_secret_spans``), so a credential that no hint word names --
    ``Authorization: Basic <b64>``, a URL's userinfo password, an ``sk-`` key
    pasted as an answer -- is masked by the same rules the push uses, and a
    forgotten hint word can no longer let it through. A value the primitive
    cannot mask is dropped (fail closed): this row is written to disk."""
    if not isinstance(value, str):
        return value
    if _SECRET_HINT.search(value):
        return "***"
    try:
        from systemu.messaging.gateway import mask_secret_spans
        return mask_secret_spans(value)
    except Exception:
        return "***"


def _audit(queue, row: Dict[str, Any]) -> None:
    """Append one audit row to ``<vault_root>/messaging/resolve_audit.jsonl``.

    Best-effort: an audit failure must never break a resolution (the decision is
    already persisted-resolved by the time we get here on the OK path, and a
    refusal has no side effect worth blocking on). No existing shared audit
    writer covers this surface, so we own a small jsonl here. Secret-looking
    values are masked.
    """
    try:
        vault = getattr(queue, "_vault", None)
        root = getattr(vault, "root", None)
        if root is None:
            return
        from pathlib import Path as _Path
        audit_dir = _Path(root) / "messaging"
        audit_dir.mkdir(parents=True, exist_ok=True)
        safe = {k: _mask(v) for k, v in row.items()}
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        with open(audit_dir / "resolve_audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.debug("[resolve_from_channel] audit write failed", exc_info=True)


def _enumerate_decisions(queue) -> List[Any]:
    """Best-effort list of every decision the queue knows (any status).

    Prefers the FULL persisted index (so an already-resolved decision is still
    findable — needed for the idempotent double-tap EXPIRED reply). Falls back to
    ``list_pending()`` when the queue has no vault index (e.g. a test stub). The
    tag is derived from the OPEN set (``disambiguate_tag`` collision handling is
    computed over pending ids, so we compute tags the same way regardless).
    """
    # Real queue: walk the vault index, load each decision.
    vault = getattr(queue, "_vault", None)
    if vault is not None and hasattr(vault, "load_index"):
        try:
            headers = vault.load_index("decisions") or []
            out = []
            for h in headers:
                try:
                    out.append(vault.get_decision(h["id"]))
                except Exception:
                    continue
            return out
        except Exception:
            logger.debug("[resolve_from_channel] load_index failed", exc_info=True)
    # Test stub / minimal queue: pending is the best we can enumerate.
    try:
        return list(queue.list_pending() or [])
    except Exception:
        logger.debug("[resolve_from_channel] list_pending failed", exc_info=True)
        return []


def _match_open_decision(queue, tag: str):
    """Find the decision whose tag matches ``tag`` (any status).

    R-P1 finding 6 (tag collision). The PUSH side computed each decision's tag as
    ``disambiguate_tag(id, {6-char tags of the OTHER pending decisions})`` — so a
    decision whose 6-char tag collides with another OPEN decision was pushed as
    its DISAMBIGUATED 8-char form. The resolver MUST apply the IDENTICAL mapping
    over the SAME open-decision set, or a tap could resolve the WRONG decision
    (the old code returned whichever bare-6 match the index yielded first).

    Algorithm — mirror the push exactly:
      * The collision namespace is the PENDING set (only open tags could collide
        on the wire).
      * For every enumerated decision, compute its push-tag =
        ``disambiguate_tag(id, {6-char tags of the OTHER pending ids})`` — the
        same value the push emitted.
      * Match the incoming ``tag`` against those push-tags. If exactly one
        decision has that push-tag, return it. If an incoming BARE-6 tag maps to
        >1 pending decision (a collided tag a legit push would NEVER have sent
        bare), it is AMBIGUOUS — return None (refuse) rather than resolve the
        wrong one.
    """
    decisions = _enumerate_decisions(queue)
    if not decisions:
        return None

    # 6-char tags of the pending set (the collision namespace).
    pending_ids = [d.id for d in decisions
                   if getattr(d, "status", None) == "pending"]
    pending_tags = [decision_tag(pid) for pid in pending_ids]

    def _push_tag(dec_id: str) -> str:
        # The tag the push side would have emitted for this id: extend to 8 iff
        # its 6-char tag collides with ANOTHER pending id's 6-char tag. Count the
        # 6-char tag's occurrences in the pending set; if this id's tag appears
        # for MORE THAN this id, it collided → 8-char form (same as the push,
        # which passes ``open_tags`` = the OTHER pending 6-char tags).
        base = decision_tag(dec_id)
        collides = sum(1 for t in pending_tags if t == base) > 1
        return disambiguate_tag(dec_id, {base}) if collides else base

    matches = [d for d in decisions if _push_tag(d.id) == tag]
    if len(matches) == 1:
        return matches[0]
    # 0 matches → unknown tag. >1 matches → an ambiguous bare-6 that a legit push
    # would never have sent (each collided decision was pushed as its 8-char
    # form). Refuse in both cases — NEVER resolve the wrong decision.
    return None


_ECHO_CAP = 200


def _echo(value: Any, cap: int = _ECHO_CAP) -> str:
    """A value as it may be echoed back to the chat: MASK first, THEN cap
    (DEC-31 - truncating first could leave the head of a secret unmasked)."""
    from systemu.messaging.gateway import mask_outbound
    text = mask_outbound(str(value))
    if len(text) > cap:
        text = text[:cap - 3] + "..."
    return text


def _title_of(decision: Any) -> str:
    return _echo(getattr(decision, "title", "") or "", cap=120).rstrip(" .?!:")


def _recorded_reply(shown: Any, decision: Any) -> str:
    title = _title_of(decision)
    if title:
        return f"Recorded: {_echo(shown)} for {title}."
    return f"Recorded: {_echo(shown)}."


def _options_reply(labels: List[Any], decision: Any, tag: str) -> str:
    listing = ", ".join(f"{i + 1} = {_echo(v, cap=60)}" for i, v in enumerate(labels))
    title = _title_of(decision)
    where = f" for {title}" if title else ""
    return (f"Nothing was recorded. Valid options{where}: {listing}. "
            f"Reply {answer_example(tag, '<number or label>')}.")


def _pick(labels: List[Any], choice: Optional[str], value: Optional[str]) -> Optional[int]:
    """Index into ``labels`` for a typed value (label, case-insensitive, then
    its 1-based number) or a button key ``a1..a4``. None when nothing matches."""
    if value is not None:
        typed = value.strip()
        for i, label in enumerate(labels):
            if str(label).strip().lower() == typed.lower():
                return i
        if typed.isdigit() and 1 <= int(typed) <= len(labels):
            return int(typed) - 1
    if choice in _CHOICE_ORDER and _CHOICE_ORDER.index(choice) < len(labels):
        return _CHOICE_ORDER.index(choice)
    return None


def resolve_from_channel(
    tag: str,
    choice: Optional[str],
    *,
    sender_id: str,
    channel: str,
    queue=None,
    resolver: Optional[Callable[[str, str], Any]] = None,
    now: Optional[Callable[[], float]] = None,
    value: Optional[str] = None,
) -> Tuple[ResolveOutcome, str]:
    """Resolve a parked decision from a chat channel (Telegram). Server-side gate.

    Args:
        tag:        the short decision tag from the callback (``d|<tag>|<key>``).
        choice:     the button key off the wire (``a1``..``a4``), or None. ONLY
                    the index is trusted; what it selects is re-derived from the
                    record (the option list, or a question's enum).
        sender_id:  the platform user id (defense-in-depth allowlist re-check).
        channel:    the channel name (e.g. ``"telegram"``) — audited.
        queue:      injectable OperatorDecisionQueue (defaults to the real one).
        resolver:   injectable resolve fn ``(decision_id, choice) -> Any`` — the
                    SAME dashboard resolve fn. Defaults to ``queue.resolve``.
        now:        injectable monotonic-ish clock ``() -> float`` for the rate
                    limiter (defaults to ``time.monotonic``).
        value:      the operator's TYPED answer (``/answer <tag> <value>``), or
                    None for a button tap. A question uses it as its answer; an
                    option card matches it against the option labels/numbers.

    The decision's kind decides what is recorded - never the wire:
      * a QUESTION (one-field form, even one riding a harness input gate)
        records the structured ``{field: value}`` JSON the dashboard form
        records: the typed text for free text, the ENUM VALUE for an enum;
      * an option card (a command/tool gate) records one of its options.
    A value that fits neither records nothing and the reply lists the options.

    Returns ``(ResolveOutcome, human_msg)``.
    """
    now_fn = now or time.monotonic
    if queue is None:
        try:
            queue = _default_queue()
        except Exception:
            logger.exception("[resolve_from_channel] could not build default queue")
            return ("UNKNOWN_TAG", "Sorry — I couldn't reach the decision queue.")

    # 1. Allowlist re-check (the gateway already gates, this is defense in depth).
    #    A miss is SILENT — return an UNKNOWN_TAG-style refusal so we never leak
    #    that a decision exists to an unauthorised sender.
    if str(sender_id) not in _allowlist():
        logger.warning("[resolve_from_channel] rejected resolve from "
                       "non-allowlisted sender %s (channel=%s)", sender_id, channel)
        return ("UNKNOWN_TAG", "I don't have anything for you to resolve.")

    # 2. Per-sender rate limit (in-memory sliding window).
    if not _rate_ok(str(sender_id), float(now_fn())):
        logger.warning("[resolve_from_channel] rate-limited sender %s", sender_id)
        return ("RATE_LIMITED",
                "You're resolving too fast — give it a minute and try again.")

    # 3. Enumerate OPEN decisions and match the tag.
    decision = _match_open_decision(queue, tag)
    if decision is None:
        return ("UNKNOWN_TAG",
                "I couldn't find that decision — it may have already been handled.")

    # 4. Status must be pending (idempotent: a double-tap gets a friendly note).
    #    IMPL-11: a card withdrawn because its run was CANCELLED is not "already
    #    resolved" - nobody resolved it. The phone gets the same notice as every
    #    other surface, and the answer is written as an operator-readable event.
    status = getattr(decision, "status", None)
    if status != "pending":
        from systemu.approval.decision_queue import (
            record_late_answer, was_expired_by_cancel)
        if was_expired_by_cancel(decision):
            return ("EXPIRED", record_late_answer(decision, channel=channel))
        return ("EXPIRED", "That decision is already resolved. Nothing to do.")

    context = getattr(decision, "context", None) or {}

    # 5. SEC-1: the persisted resolution_class bit. Absent / anything but
    #    "remotely_resolvable" → refuse. Never read surface_hint.
    if context.get("resolution_class") != RESOLUTION_REMOTE:
        logger.info("[resolve_from_channel] refused %s — resolution_class=%r "
                    "(not remotely_resolvable)", decision.id,
                    context.get("resolution_class"))
        _audit(queue, {
            "ts": time.time(), "channel": channel, "sender_id": sender_id,
            "decision_id": decision.id, "tag": tag, "choice": choice,
            "outcome": "REFUSED_TYPED_CONFIRM",
        })
        return ("REFUSED_TYPED_CONFIRM",
                "That decision can't be resolved from here — open the dashboard.")

    # 6. Decide WHAT to record from the RECORD's kind (never from the wire).
    if _is_elicitation(context):
        # A question: only a one-field, non-secret, text-or-enum form is
        # answerable here; it records the structured value, never an option
        # label ("Submit" / a gate's Deny-Approve are not answers).
        field = _single_field_spec(context)
        spec = field[1] if field is not None else {}
        enum = spec.get("enum")
        if field is not None and isinstance(enum, list) and enum:
            idx = _pick(enum, choice, value)
            if idx is None:
                return ("BAD_CHOICE", _options_reply(enum, decision, tag))
            shown = enum[idx]
        elif field is not None and spec.get("type", "string") == "string":
            if value is None or not value.strip():
                return ("BAD_CHOICE",
                        "Nothing was recorded. This question needs a typed "
                        f"answer: reply {answer_example(tag)}.")
            shown = value.strip()
        else:
            return ("REFUSED_TYPED_CONFIRM",
                    "That question can't be answered from here - open the dashboard.")
        mapped_choice = json.dumps({field[0]: shown})
    else:
        # An option card: the choice is one of the record's options.
        options = list(getattr(decision, "options", []) or [])
        idx = _pick(options, choice, value)
        if idx is None:
            return ("BAD_CHOICE", _options_reply(options, decision, tag))
        mapped_choice = shown = options[idx]

    # 7. Call the SAME dashboard resolve fn. This sets status+choice+publishes the
    #    operator_decision_resolved event; the existing reconciler / EventBus
    #    subscriber dispatch the resume. We do NOT re-implement resume here.
    from systemu.approval.decision_queue import AnswerNotNeeded, resolution_channel
    resolve_fn = resolver or queue.resolve
    try:
        # R-P3b: the card records the channel it was resolved on, in the same save.
        with resolution_channel(str(channel) if channel else ""):
            if resolver is not None:
                resolve_fn(decision.id, mapped_choice)
            else:
                queue.resolve(decision.id, choice=mapped_choice)
    except AnswerNotNeeded as late:
        # IMPL-11: the run was cancelled between the lookup above and this
        # resolve. The queue recorded nothing and already wrote the event; the
        # phone gets the same notice. Before ValueError, which it subclasses.
        return ("EXPIRED", str(late))
    except ValueError:
        # The record's option list disagreed with the queue's membership check
        # (shouldn't happen — we mapped from the same options) → treat as a bad
        # choice rather than a crash.
        logger.warning("[resolve_from_channel] resolve rejected choice %r for %s",
                       mapped_choice, decision.id)
        return ("BAD_CHOICE", "That option couldn't be applied.")
    except KeyError:
        # The decision vanished between enumeration and resolve.
        return ("EXPIRED", "That decision is already resolved. Nothing to do.")
    except Exception:
        logger.exception("[resolve_from_channel] resolve failed for %s", decision.id)
        return ("UNKNOWN_TAG", "Something went wrong resolving that. Try the dashboard.")

    # 8. Audit the successful resolution (masking any secret-looking value).
    _audit(queue, {
        "ts": time.time(), "channel": channel, "sender_id": sender_id,
        "decision_id": decision.id, "tag": tag, "choice": choice,
        "mapped_choice": mapped_choice, "outcome": "OK",
    })

    # 9. Success: a closing line naming what was recorded.
    return ("OK", _recorded_reply(shown, decision))
