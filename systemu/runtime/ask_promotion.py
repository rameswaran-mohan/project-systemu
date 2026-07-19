"""G-LEARN slice 3 (spec §5.9) — PROMOTION of an accepted ask answer.

When the operator answers a bundled scope-card ask, §5.9 says: promote the answer into
the operator profile so the next identical situation RESOLVES instead of ASKING, and
materialize a corresponding `TableItem` (provenance ``learned``) so the world visibly
grew. This module is that promoter.

WHY THIS MODULE IS WRITTEN DEFENSIVELY
--------------------------------------
This is the hop that would INTRODUCE the laundering bug. The consumer half (bind-side
taint carriage, IMPL-5) is finished and correct; there were ZERO promotion writers
before this slice. Every default along the path points at laundering:

  * ``user_profile.add_fact``'s ``origin_class`` parameter defaults to ABSENT, and
  * ABSENT grandfathers to ``operator`` in ``requirement_binder._fact_origin``,
  * and ``operator`` at ``state="have"`` binds SILENTLY (``_needs_ask`` returns False).

So a promoter that forgets ONE kwarg converts a page-derived value into a trusted,
silently-bound one on the very next run. Four independent defences, in depth:

  1. ONE chokepoint (:func:`_promote_fact`) may reach ``add_fact``, and its
     ``origin_class`` parameter has NO default — omitting it is a ``TypeError``, not a
     silent grandfather. Pinned structurally (source pins module).
  2. ``PROMOTION_SOURCE`` is carved OUT of the ``_fact_origin`` grandfather, so even a
     dropped stamp reads ``content_derived``: the failure mode is an extra confirm
     (safe), never a silent trusted bind (unsafe).
  3. Fail-closed at the decision: no candidate digest, or an undigestable answer, ⇒
     promote NOTHING. Stamping ``operator`` there IS the bug; stamping
     ``content_derived`` over-taints the operator's own typing and destroys the payoff.
  4. Anything non-canonical clamps to ``content_derived`` (fail-untrusted).

THE ORIGIN DECISION (§5.9's "picked vs typed", by VALUE-EQUALITY)
-----------------------------------------------------------------
§5.9 words the rule as "picked from a candidate list vs freshly typed", but no
requirement-ask producer ever sets ``enum``: every slot renders as one free-text input
and the answer envelope carries no provenance marker, so there is no "picked" signal to
read. The equivalent observable is VALUE EQUALITY against the binder's own candidate,
which already crosses the suspend as a keyed digest (``candidate_ref``):

    answer digest == a candidate digest  ⇒ the operator ACCEPTED that candidate
                                           ⇒ promote with THAT candidate's origin
    answer digest != every candidate     ⇒ the operator TYPED something new
                                           ⇒ promote as ``operator``

A multi-candidate match resolves to the MOST-TAINTED origin. Deliberately NOT the
highest-confidence collapse ``replay_metrics`` uses for its own metric: confidence is
the wrong axis here, and picking by it is itself a laundering vector (a high-confidence
``content_derived`` candidate would win and be stamped trusted).

A candidate only participates in that comparison if it is a well-formed ``value_ref``
signed by THIS vault's key (see the guards at the decision site). Anything else is not
evidence in either direction and reaches the fail-closed branch.

DOCUMENTED RESIDUAL. A candidate whose MAC is altered but whose shape and key-id are
intact reads as "the operator typed something new" ⇒ ``operator``. That is not closable:
it is byte-for-byte the same signal as a genuine override, and the promoter holds
digests, never values. Nor is it an escalation — producing it needs write access to the
persisted card spec, and anyone holding that can author an ``operator``-stamped fact in
``user_facts.jsonl`` directly. The guards exist for the shapes that arise WITHOUT vault
write access: a non-conforming producer, and a vault-key rotation (which needs no
attacker at all and otherwise launders an entire card at once).

SCOPE LIMITS (each one closes a proven hole — see the pins)
-----------------------------------------------------------
  * **user_facts ONLY, never the UserProfile spine.** The spine has ``extra="forbid"``
    and no ``origin_class`` field, so it structurally cannot carry taint, and
    ``_bind_profile`` hard-codes ``operator`` for every spine hit. The spine loop also
    runs BEFORE the user_facts loop, so when the profile HOLDS a spine value that
    value wins outright: the bind returns ``profile:<spine_field>`` with the spine's
    OWN value and an ``operator`` origin, and the promoted fact is never consulted.
    That is SUBSTITUTION, not laundering (the promoted value does not travel), and it
    makes the promotion useless rather than unsafe. The refusal is kept anyway, and it
    is deliberately WIDER than the binder's predicate: ``_collides_with_profile_spine``
    drops the binder's ``val and`` half, so it refuses whether or not the profile holds
    that spine value today. It has to — the profile is mutable between this promotion
    and the next bind, so "will the spine shadow this leaf at bind time?" is not
    answerable here. The cost is over-exclusion of leaves that merely share a token
    with a spine field (``service_name``, ``output_format``, ``output_path``,
    ``body_text``); the benefit is that no promotion can land in a slot whose bind
    outcome the promoter cannot predict. See ``_collides_with_profile_spine``.
  * **Dedupe on ``schema_path``, never ``ask_id``.** Mid-loop the ask id is
    ``"hreq_" + uuid4()`` (zero cross-run protection); the pre-loop one is a content
    hash over rationale prose, so a reworded prompt re-promotes. ``ask_id`` is NOT
    persisted anywhere: it names the ask only in the refusal/audit LOG line. It is
    deliberately not stuffed into ``UserFact.tags`` — ``tags`` is the join key
    ``_bind_profile`` matches leaves against, so an id there is bind surface, not
    provenance — and ``UserFact`` forbids extra fields, so there is no other slot.
  * **Learned cards only for single-field ref-keys the projector keys identically**
    (``service``/``mcp_server``/``preference``/``device``). ``tool`` is the proven
    trap: ``ref_key("tool", …)`` prefers ``tool_id``, so the operator's removal
    tombstones ``tool:<tool_id>`` while an answer-derived card knows only the name →
    ``tool:<name>``. The keys never meet and a deleted tool is re-suggested forever.
  * **A card is keyed and NAMED by the LEAF, never by the answer value.** ``name`` is
    rendered on the /table surface, so the raw answer there publishes every promoted
    value; and keying on the value minted a NEW card per answer, so one leaf answered
    three times left three cards, all projected forever and removable only one by one.
    The value rides ``usage`` (carried by the projector, rendered by nothing).
  * **No secrets, ever** — at BOTH levels. The field NAME goes through the codebase's
    canonical marker (``replay_metrics`` → ``elicitation.is_secret_field``). The
    VALUE goes through ``messaging.gateway.mask_outbound``, the shipped outbound
    secret chokepoint, plus the two shapes it provably does not cover (see
    :func:`_value_is_secret`). A name-only fence is not enough: the promoted fact is
    read VERBATIM into a system prompt (``shadow_runtime._build_user_context_block``,
    5 most-recent facts) and into a tier-1 LLM payload (``scroll_refiner``, recent 20)
    on later, unrelated runs — so a credential parked under a neutral leaf egresses.

OBSERVABILITY + SAFETY CONTRACT. Bounded and never-raises: this runs inside a daemon
reconciler tick AFTER a real resume has already been dispatched, so it must never take
the tick down.

"Bounded" needs THREE bounds, not two. ``MAX_PROMOTIONS_PER_BATCH`` and
``MAX_PROMOTIONS_PER_CLASS`` are call-local, and the reconciler loops over every
resolved decision in the tick — so N answered cards multiplied the bound by N (measured:
5 cards ⇒ 20 promotions against a batch cap of 8). :class:`PromotionBudget` is the
cross-call bound: the caller builds ONE and passes it to every call in the tick.

EVERY refusal is LOGGED at INFO through the single ``capped`` list — including the
secret and spine refusals, which used to log at DEBUG only and were therefore invisible
at the daemon's default level, making this module's own auditability claim false. The
one deliberate exception is an unanswered/blank slot: that is a non-event, not a
refusal, and logging it would drown the real signal on any partly-filled card.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The ``UserFact.source`` string this module writes — a NEW, sole-writer value.
#: ``requirement_binder._fact_origin`` carves it OUT of the absent⇒``operator``
#: grandfather, so a dropped origin stamp reads ``content_derived`` (over-ask, safe)
#: instead of ``operator`` (silent trusted bind, unsafe). Reusing an existing source
#: string (``auto_extract``) would both misattribute promotions in the audit trail and
#: couple two unrelated writers to one carve-out.
PROMOTION_SOURCE = "ask_promotion"

#: Bounds (§5.9 "capped per class"). One answered card cannot flood the profile.
MAX_PROMOTIONS_PER_BATCH = 8
MAX_PROMOTIONS_PER_CLASS = 4
#: The CROSS-CALL bound. The two caps above are per call, so a tick that answers
#: several cards multiplies them; this one is shared by every call in the tick.
MAX_PROMOTIONS_PER_TICK = 12


class PromotionBudget:
    """A promotion allowance SHARED across every card promoted in one reconciler tick.

    Deliberately a small mutable object rather than a module-level counter: the
    reconciler is the only producer, a counter would leak state between ticks (and
    between tests), and an explicit object makes the sharing visible at the call site.

    Chosen over the alternative — bounding the total number of live
    ``ask_promotion``-sourced facts at write time — because that alternative caps
    LIFETIME learning, not per-tick flooding. It would eventually refuse every new
    promotion on a long-lived vault and quietly turn the slice off, which is a worse
    failure than the one it fixes. The invariant §5.9 actually wants is "one tick
    cannot flood the profile", and that is what this expresses."""

    __slots__ = ("remaining",)

    def __init__(self, limit: Optional[int] = None):
        # read the module global at CONSTRUCTION so a test/operator override applies
        self.remaining = int(MAX_PROMOTIONS_PER_TICK if limit is None else limit)

    def take(self) -> bool:
        """Consume one promotion. False when the tick's allowance is spent."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

_OPERATOR = "operator"
_SYSTEMU = "systemu_authored"
_CONTENT_DERIVED = "content_derived"
#: Taint ordering for a multi-candidate match — HIGHER wins (most-tainted).
_TAINT_RANK = {_OPERATOR: 0, _SYSTEMU: 1, _CONTENT_DERIVED: 2}


def _coerce_origin(origin: Any) -> str:
    """Canonical taint value, failing UNTRUSTED. Mirrors ``requirement_binder``'s
    clamp: an absent/typo'd/poisoned stamp becomes ``content_derived``, never a
    trusted axis.

    The ``isinstance`` guard is not redundant. ``value_origin`` reaches here from a
    card spec persisted in PLAINTEXT and rehydrated from JSON, so it can be ANY type —
    and ``origin in <frozenset>`` RAISES ``TypeError`` on an unhashable value (a
    list/dict). That raise would be absorbed by the entry point's outer handler and
    abort the WHOLE batch, so one poisoned entry would suppress every other promotion
    on the card: fail-closed, but a self-inflicted denial. ``requirement_binder``
    ``_fact_origin`` carries the same guard for the same reason."""
    if not isinstance(origin, str):
        return _CONTENT_DERIVED
    return origin if origin in _TAINT_RANK else _CONTENT_DERIVED


def _most_tainted(origins) -> str:
    """The most-tainted of several matched candidate origins (see module docstring)."""
    return max((_coerce_origin(o) for o in origins),
               key=lambda o: _TAINT_RANK[o], default=_CONTENT_DERIVED)


# ── exclusions ───────────────────────────────────────────────────────────────
def _leaf_of(schema_path: str) -> str:
    """The bind KEY for a schema path. ``_bind_profile`` matches on the leaf key (the
    last walk segment), so tags must be built from the leaf even though dedupe uses the
    full path."""
    return str(schema_path or "").rsplit("/", 1)[-1].strip().lower()


def _collides_with_profile_spine(leaf: str) -> bool:
    """True when a leaf COULD be bound by the UserProfile SPINE rather than by our
    promoted fact.

    Reuses ``requirement_binder``'s spine field set and its ``_key_token_overlap``
    helper rather than restating them, so the name-matching half cannot drift from the
    binder (``output_dir`` colliding with ``default_output_dir`` is exactly the case a
    naive equality check would miss).

    It is NOT the binder's full predicate, and the difference is deliberate. The
    binder's condition is ``val and (f in kl or kl in f or _key_token_overlap(kl, f))``
    — it only takes the spine branch when the profile actually HOLDS that spine value.
    This one drops the ``val and`` half and refuses either way, because the profile is
    mutable between this promotion and the next bind: whether the spine will shadow
    this leaf THEN is not knowable HERE. Refusing only on today's profile state would
    promote a fact that a later profile write silently makes unreachable.

    What the refusal prevents is a promotion whose bind outcome is unpredictable, NOT
    laundering: when the spine does hold a value it binds ``profile:<field>`` with its
    OWN value and an ``operator`` origin, so the promoted value is substituted, never
    carried. The cost is over-exclusion of leaves that merely share a token with a
    spine field — ``service_name``, ``output_format``, ``output_path``, ``body_text``,
    some of which the card mapping would otherwise target. Accepted as the conservative
    direction: a refused promotion costs one extra confirm, a mis-predicted one is a
    fact that never binds."""
    try:
        from systemu.runtime.requirement_binder import (
            _PROFILE_SPINE, _key_token_overlap,
        )
    except Exception:
        return True                      # cannot prove it is safe ⇒ refuse (fail-closed)
    kl = leaf or ""
    if not kl:
        return True
    for f in _PROFILE_SPINE:
        if f in kl or kl in f or _key_token_overlap(kl, f):
            return True
    return False


def _is_secret(schema_path: str, klass: str) -> bool:
    """Belt-and-braces secret refusal. ``requirement_snapshot`` already refuses to
    snapshot a secret-mode requirement, so the promoter should never see one — but a
    stamp is persisted plaintext and re-read here, so the fence is re-asserted at the
    write. Import failure ⇒ treat as secret (fail-closed)."""
    try:
        from systemu.runtime.replay_metrics import _is_secret_path
        return bool(_is_secret_path(schema_path, klass))
    except Exception:
        return True


#: The two secret shapes ``mask_outbound`` provably does not cover (verified against it
#: directly — see the pins). Everything else it already handles: ``Bearer …``, ``sk-…``,
#: ``ghp_…``, ``AKIA…``, JWTs, Slack tokens, long hex runs, and ``<secret-name>=<value>``
#: / ``<secret-name>: <value>`` pairs.
#:
#: 1. URI userinfo — ``scheme://user:pass@host``. No token NAME and no known token
#:    SHAPE, so nothing in the shipped detector fires; this is the exact value that
#:    reached ``user_facts.jsonl``, the learned sidecar and ``items.json`` in the
#:    reproduction. The pattern needs a colon-separated userinfo pair AND a terminating
#:    ``@``, so ``http://host:8080/path`` (no ``@``) and ``https://user@host`` (no
#:    password) do not match.
#: 2. A credential flag separated by SPACE — ``--token VALUE``, ``--password VALUE``.
#:    ``mask_outbound``'s kv pattern requires ``=`` or ``:``, so it catches
#:    ``--token=VALUE`` and misses the space form, which is how a shell command is
#:    actually written.
_URI_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_CRED_FLAG_RE = re.compile(
    r"(?i)(?:^|\s)--?(?:token|password|passwd|secret|api[-_]?key|apikey|auth|"
    r"credential|access[-_]?key)[\s=:]+\S+")


def _value_is_secret(value: Any) -> bool:
    """True when the ANSWER ITSELF looks like a credential, whatever the field is named.

    The name-level fence (:func:`_is_secret`) inspects field NAMES only, so a secret
    parked under a neutral leaf (``service_endpoint is postgres://admin:pw@db/prod``)
    sailed straight through it — and a promoted fact is read verbatim into a system
    prompt and into a tier-1 LLM payload on later, unrelated runs. This is the value
    half of that fence.

    REUSES the shipped detector rather than inventing a second one:
    ``messaging.gateway.mask_outbound`` is the codebase's outbound secret chokepoint
    (every gateway push goes through it), and it is used here as a DETECTOR — if
    masking changes the text, the text contained something the codebase already
    considers a secret. Keeping one vocabulary means a token shape added there is
    picked up here for free. Two shapes it does not cover are supplemented above.

    Verified NOT to fire on ordinary answers (paths, emails, service names, URLs,
    timezones) — a blanket refusal here would silently disable the whole slice, so the
    negative control is pinned as hard as the positive ones.

    Import failure ⇒ treat as secret (fail-closed), matching the other two guards."""
    try:
        s = str(value)
        if not s:
            return False
        if _URI_USERINFO_RE.search(s) or _CRED_FLAG_RE.search(s):
            return True
        from systemu.messaging.gateway import mask_outbound
        return mask_outbound(s) != s
    except Exception:
        logger.debug("[S3] value-secret check failed — refusing", exc_info=True)
        return True


# ── the learned-card mapping (§5.10 item semantics; §5.9 owns only the trigger) ──
#: leaf TOKEN → TableItem kind. Restricted to kinds whose ``ref_key`` is SINGLE-FIELD
#: and keyed the same way the projector keys it, so a learned card and an operator
#: removal can actually meet. Order matters: ``mcp`` is checked before the generic
#: service tokens so an ``mcp_server`` leaf does not land as a plain service.
_CARD_KIND_TOKENS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("mcp_server", ("mcp",)),
    ("service", ("service", "provider", "platform", "vendor")),
    ("device", ("device", "printer", "machine")),
    ("preference", ("preference", "format", "style", "tone", "locale", "units")),
)

#: §5.10.b#5 — an approval/autonomy POSTURE preference may only ever be proposed
#: through the explicit Governor surface; it can NEVER arrive as ``suggested``/
#: ``learned``. A friction-DECREASING posture change is danger-gated, so a learned card
#: must not become a one-click path to it.
_POSTURE_TOKENS = frozenset({
    "approval", "autonomy", "posture", "confirm", "risk", "band", "permission",
    "trust", "allow", "grant",
})


def _tokens(leaf: str) -> set:
    import re
    return {t for t in re.split(r"[^a-z0-9]+", leaf or "") if t}


def _card_kind_for(leaf: str) -> Optional[str]:
    """The TableItem kind a learned card should take for this leaf, or None for
    fact-only promotion (the common case — an unmapped leaf gets no card)."""
    toks = _tokens(leaf)
    if toks & _POSTURE_TOKENS:
        return None                      # §5.10.b#5 — never learned
    for kind, markers in _CARD_KIND_TOKENS:
        if toks & set(markers):
            return kind
    return None


# NOTE: there is no ``_card_ref`` helper here. The ref shape is built by
# ``table_store._operator_ref`` inside ``make_learned_item``, which is the SAME
# constructor the projector's keys derive from — a second local copy of that mapping
# existed, was never called, and claimed in its docstring to be the thing that keeps a
# learned card tombstonable. Two sources of truth for one ref shape is precisely how a
# learned card and an operator removal stop meeting, so the copy is gone rather than
# wired up.


# ── the write chokepoints ────────────────────────────────────────────────────
def _promote_fact(vault, *, schema_path: str, leaf: str, answer: str,
                  origin_class: str):
    """THE ONE call site that may reach ``user_profile.add_fact``.

    ``origin_class`` is keyword-only WITH NO DEFAULT on purpose: a caller that forgets
    it gets a ``TypeError``, not the silent absent⇒``operator`` grandfather that would
    launder the value. Pinned by the source-pin module, which also asserts this remains
    the sole ``add_fact`` call site in the module."""
    from systemu.runtime.user_profile import add_fact

    # NO clamp here on purpose. The origin is already canonical by construction (the
    # decision site returns either `_most_tainted`, which coerces, or `_OPERATOR`), and
    # `UserFact.origin_class` is a CLOSED vocabulary that rejects anything else at
    # construction. A third clamp here would be unkillable — deleting it would fail no
    # test — and would be actively worse than the model's validator: it would silently
    # downgrade a poisoned value to `content_derived` where the model fails LOUD.
    # The single clamp lives in `_coerce_origin`, pinned by the poisoned-snapshot test.
    # The fact is a SENTENCE (that is what user_facts holds and what `_bind_profile`
    # text-matches); the tag is the LEAF, which is what the bind actually matches on.
    text = f"{leaf} is {answer}"
    return add_fact(
        vault, text,
        source=PROMOTION_SOURCE,
        tags=[leaf],
        source_ref=schema_path,          # the DEDUPE key (never ask_id — see docstring)
        confidence=1.0,
        origin_class=origin_class,
    )


def _existing_promotions(vault, schema_path: str) -> List[Any]:
    """EVERY live (non-superseded) promotion for this ``schema_path``, oldest FIRST.

    Plural on purpose. ``get_facts`` returns newest-LAST while ``_bind_profile`` returns
    the FIRST tag match — the OLDEST row. So if a supersede write ever fails (a Windows
    file lock, or two ticks racing the ``harness_grant_dispatched`` check-then-stamp
    window) the STALE value is what binds, silently, forever: the newer row exists but
    is never consulted, and a promoter that superseded only ``prior.id`` would go on
    retiring the wrong row every time. The caller supersedes ALL of these, which makes
    the next promotion REPAIR that state instead of compounding it."""
    try:
        from systemu.runtime.user_profile import get_facts
        return [f for f in get_facts(vault)
                if f.source == PROMOTION_SOURCE and f.source_ref == schema_path]
    except Exception:
        logger.debug("[S3] could not read prior promotions", exc_info=True)
        return []


def _supersede_all(vault, facts, new_id: str) -> None:
    """Mark every one of ``facts`` superseded, chained to ``new_id`` so the audit trail
    stays traversable. Best-effort per row: one failure must not skip the rest."""
    from systemu.runtime.user_profile import forget_fact
    for f in facts:
        try:
            forget_fact(vault, f.id, reason=new_id)
        except Exception:
            logger.debug("[S3] could not supersede prior promotion %s", f.id,
                         exc_info=True)


def _materialize_learned_card(vault, *, leaf: str, answer: str, origin_class: str) -> bool:
    """Best-effort learned TableItem. Returns True when a card was written.

    The card is NAMED AND KEYED BY THE LEAF, and carries the answer in ``usage``.
    ``name`` is what the /table surface renders, so the raw answer there published every
    promoted value; and keying on the value made the card identity change with the
    answer, so one leaf answered three times left three cards — all projected forever,
    each removable only on its own. Keyed by the leaf there is exactly one card per
    slot, and a changed answer heals it in place. ``usage`` is carried forward by the
    reconciler and rendered by nothing, which is what makes it the right carrier.

    A card failure NEVER fails the promotion: the fact is the durable half of §5.9, the
    card is the visible half."""
    kind = _card_kind_for(leaf)
    if kind is None:
        return False
    try:
        from systemu.runtime import table_store as ts

        item = ts.make_learned_item(kind, leaf, origin_class=origin_class)
        item.usage = {"promoted_value": str(answer)}
        # The tombstone check lives in ``add_learned_item`` (a STORE-level invariant
        # that protects every caller) and is deliberately NOT repeated here. A second
        # copy would make the store's own guard unkillable — delete it and no test
        # fails — which is the failure mode `test_the_key_id_parser_does_not_silently_
        # duplicate_guard_3` already exists to prevent elsewhere in this codebase.
        written = ts.add_learned_item(vault, item)
        if not written:
            logger.info("[S3] learned card withheld (removed by the operator, or "
                        "already present): %s", ts.ref_key(item.kind, item.ref))
        return written
    except Exception:
        logger.debug("[S3] learned card materialization skipped", exc_info=True)
        return False


def _drop_stale_learned_card(vault, *, leaf: str) -> None:
    """Retire the learned card for ``leaf`` so a superseded answer does not leave a card
    advertising the old value.

    Drop-then-re-add rather than edit-in-place, so the re-add goes back through
    ``add_learned_item`` and is still subject to the tombstone check — a changed answer
    must not become a back door that resurrects a card the operator removed."""
    try:
        from systemu.runtime import table_store as ts

        kind = _card_kind_for(leaf)
        if kind is None:
            return
        key = ts.ref_key(kind, ts._operator_ref(kind, leaf))
        rows = ts.load_learned_items(vault)
        keep = [r for r in rows if ts.ref_key(r.kind, r.ref) != key]
        if len(keep) != len(rows):
            ts.save_learned_items(vault, keep)
    except Exception:
        logger.debug("[S3] could not retire the prior learned card", exc_info=True)


# ── the public entry ─────────────────────────────────────────────────────────
def promote_answered_asks(vault, dctx, answers,
                          budget: Optional[PromotionBudget] = None) -> int:
    """Promote the accepted answers on ONE bundled scope card. Returns the number of
    promotions written. Never raises.

    ``budget`` is the CROSS-CALL bound (:class:`PromotionBudget`). The reconciler builds
    one per tick and passes the same object to every card, because the caps below are
    call-local and a tick that answers several cards would otherwise multiply them.
    Optional so a direct caller need not build one — the per-call caps still bind on
    their own, so omitting it narrows the bound rather than removing it.

    Consumes the snapshot dict ``replay_metrics.requirement_snapshot`` emits VERBATIM
    (``schema_path``/``class``/``state``/``source``/``value_origin``/``confidence``/
    ``candidate_ref``) — the shape is not re-derived here.

    Grouped by ``schema_path`` first, exactly as the sibling recorder does: one stamp
    can carry SEVERAL snapshots for the same path (``build_requirement_report`` keeps
    same-path/different-``bound_value_ref`` asks distinct) while the operator sees ONE
    form slot and answers it once. Per-path, not per-snapshot, or one answer would
    promote twice."""
    try:
        snaps = ((dctx or {}).get("spec") or {}).get("requirement_snapshot")
        if not isinstance(snaps, list) or not snaps:
            return 0
        if not isinstance(answers, dict) or not answers:
            return 0

        from systemu.runtime import replay_metrics as rm

        ask_id = str((dctx or {}).get("request_id", "") or "")

        by_path: Dict[str, List[dict]] = {}
        for snap in snaps:
            if not isinstance(snap, dict):
                continue
            path = str(snap.get("schema_path", "") or "")
            if not path:
                continue
            by_path.setdefault(path, []).append(snap)

        promoted = 0
        per_class: Dict[str, int] = {}
        capped: List[str] = []

        for path, group in by_path.items():
            answer = answers.get(path)
            if answer is None or str(answer) == "":
                continue                  # unanswered slot — promotion is answer-linked

            klass = str((group[0] or {}).get("class", "") or "")
            leaf = _leaf_of(path)

            # ── the three refusals. All of them land in `capped`, so they are visible
            #    at INFO: a fail-closed refusal is an audit signal, not a non-event. ──
            if _is_secret(path, klass):
                capped.append(f"{path} (secret-mode field)")
                continue
            if _collides_with_profile_spine(leaf):
                capped.append(f"{path} (collides with the UserProfile spine)")
                continue
            if _value_is_secret(answer):
                # NB: the path only — never the value, and never a hint of its shape.
                capped.append(f"{path} (answer looks like a credential)")
                continue

            # ── the origin decision, fail-closed on any unusable digest ──
            ans_ref = rm.value_ref(answer, vault)
            if ans_ref is None:
                capped.append(f"{path} (answer not digestable)")
                continue
            # `candidate_ref` rides a persisted card spec across a suspend, so it is
            # attacker-shaped input here — the sibling reader of the SAME field
            # (`replay_metrics.record_ask_avoidable`) says exactly that and applies
            # these two guards. Truthiness alone is NOT enough: a present-but-
            # incomparable candidate (malformed, truncated, a list/dict, a raw value,
            # or a digest signed under a DIFFERENT vault key) would pass the old
            # `if s.get("candidate_ref")` test, fail the equality below, and be read as
            # "the operator typed something new" ⇒ `operator`, the TRUSTED axis. One
            # flipped hex character laundered the value; so did a routine vault-key
            # rotation, which makes every in-flight candidate incomparable at once and
            # needs no attacker at all. An unusable digest is not evidence of anything,
            # so it must reach the fail-closed branch below instead.
            key_id = rm._ref_key_id(ans_ref)
            cands = [(s.get("candidate_ref"), s.get("value_origin")) for s in group
                     if rm._is_value_ref(s.get("candidate_ref"))
                     and rm._ref_key_id(s.get("candidate_ref")) == key_id]
            if not cands:
                capped.append(f"{path} (no comparable candidate digest)")
                continue
            matched = [origin for ref, origin in cands if ref == ans_ref]
            origin = _most_tainted(matched) if matched else _OPERATOR

            # ── bounds: per-call, per-class, and the shared per-TICK budget ──
            if promoted >= MAX_PROMOTIONS_PER_BATCH:
                capped.append(f"{path} (batch cap)")
                continue
            if per_class.get(klass, 0) >= MAX_PROMOTIONS_PER_CLASS:
                capped.append(f"{path} ({klass} class cap)")
                continue

            # ── dedupe on schema_path; supersede a CHANGED answer ──
            # ALL live rows for the path, oldest first — `_bind_profile` binds the
            # oldest, so a stale row left behind by a failed supersede is the one that
            # actually takes effect (see `_existing_promotions`).
            priors = _existing_promotions(vault, path)
            if priors and priors[-1].fact == f"{leaf} is {answer}":
                # Identical value already promoted ⇒ no new row. Still HEAL any stale
                # duplicates: re-confirming the same answer is the common case, so if
                # the healing lived only on the write path below, a duplicate left by a
                # failed supersede would survive every tick that re-confirmed it.
                _supersede_all(vault, priors[:-1], priors[-1].id)
                continue

            # the tick budget is taken LAST, immediately before the write, so a refused
            # or deduped path never spends another card's allowance
            if budget is not None and not budget.take():
                capped.append(f"{path} (tick budget)")
                continue

            try:
                new = _promote_fact(vault, schema_path=path, leaf=leaf,
                                    answer=str(answer), origin_class=origin)
            except Exception:
                logger.debug("[S3] promotion write failed for %s", path, exc_info=True)
                continue

            _supersede_all(vault, priors, new.id)
            if priors:
                # the card advertises a value that just went stale — retire it so the
                # fresh one below replaces it rather than sitting alongside it
                _drop_stale_learned_card(vault, leaf=leaf)

            _materialize_learned_card(vault, leaf=leaf, answer=str(answer),
                                      origin_class=origin)
            promoted += 1
            per_class[klass] = per_class.get(klass, 0) + 1
            logger.debug("[S3] promoted %s (origin=%s, ask=%s)", path, origin, ask_id)

        if capped:
            # §5.9 "bounded + auditable": what was withheld is LOGGED, never silently
            # dropped — a fail-closed refusal is a signal, not a non-event.
            logger.info("[S3] %d promotion(s) capped/refused on ask %s: %s",
                        len(capped), ask_id or "?", "; ".join(capped[:12]))
        return promoted
    except Exception:
        logger.debug("[S3] ask promotion skipped (non-fatal)", exc_info=True)
        return 0
