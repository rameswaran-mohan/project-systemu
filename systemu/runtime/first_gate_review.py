"""IMPL-4 — the one-time bulk FIRST-GATE review card (spec §5.7, v2.1).

When the live per-tool action gate first ships, ``vault_migrator.backfill_effect_tags``
stamps an effect classification onto every legacy vault tool. Without this module the
first post-ship session is an AMBUSH: each backfilled tool trips its own mid-run modal,
one at a time, at the worst possible moment. IMPL-4 is MEDIUM-4's anti-fatigue logic
applied to the migration moment — the operator reviews the whole inventory ONCE, up
front, instead of being interrupted N times mid-task.

    "backfilled legacy tools carrying net/UNKNOWN tags get a one-time bulk review card
    (batch classify / Always-allow / leave-gated) ... The card PARTITIONS by band: only
    REQUIRE_APPROVAL-band tools are eligible for batch Always-allow; any DENY-band
    (UNKNOWN ∩ high-severity) tool is EXCLUDED from the bulk action and resolvable only
    individually via the IMPL-2 reclassify flow + high-friction typed-confirm — never
    swept in by a bulk Always-allow."

── The DENY carve-out is enforced on BOTH sides, and it has to be ───────────────

RECORD side (here): :func:`apply_bulk_always_allow` RE-SCORES every entry from its raw
signals and records a standing allow only for the REQUIRE_APPROVAL band. It deliberately
does NOT trust the ``verdict`` stamped on the entry: that field round-trips through the
decision store between enqueue and resolve, and a stale or tampered value must not be
able to launder a DENY into the batch.

CONSUME side (``tool_sandbox._maybe_gate_tool``): the standing allow this module mints is
an ordinary ``CommandApprovalStore`` entry, and the gate consults it ONLY under
``if verdict != Verdict.DENY``. That guard — not this module — is what actually expresses
"no stored approval satisfies this band", and it is load-bearing HERE for a reason
specific to bulk:

    ``tool_signature`` is params-INDEPENDENT while the DENY verdict is params-DEPENDENT.
    A backfilled tool whose source could not be classified gets ``effect_tags=[]`` — the
    COMMONEST backfill outcome — which scores REQUIRE_APPROVAL at migration time (there
    are no params in hand) and is therefore legitimately eligible for the sweep. The very
    same signature scores DENY the moment a destructive argument arrives.

So a correctly-swept, entirely legitimate bulk allow WILL be presented to the gate on a
DENY-band call. Refusing to record is not enough and never could be — at migration there
is nothing to refuse. Only the consumption side can decline it. This is commit
``2da5547c``'s lesson arriving at the migration surface; both halves are pinned in
``tests/test_impl4_bulk_first_gate.py``.

── One card, one version ────────────────────────────────────────────────────────

The dedup key is ``tool_bulk:<version>``, so the card is idempotent: re-posting after a
restart collapses onto the same decision row, and a later version bump (which re-runs the
backfill) posts a fresh one. ``gate_type="tool_bulk"`` is on ``FLOOR_GATE_TYPES`` and the
dedup prefix is in ``_RAIL_RENDER_ONLY_DEDUP_PREFIXES``: a Bypass policy must not
auto-grant this card, and the rail's one-click quick-approve (which resolves
``options[-1]``, here the bulk allow) must not sweep an entire inventory in one click.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# The gate type + dedup namespace this surface owns.
BULK_GATE_TYPE = "tool_bulk"
BULK_DEDUP_PREFIX = "tool_bulk:"

# The exact option labels. ONE definition, imported by the card, the executor and the
# tests — a one-character drift between the card's options and the executor's match
# would make the affirmative choice a silent no-op (``decision_queue.resolve`` validates
# choice-in-options, so the drift surfaces as an inert card, not an error).
#
# Only these TWO, and only one of them acts. There is no "batch classify": assigning an
# effect class is what defeats the UNKNOWN conjunct, so doing it across a batch would
# launder the entire excluded set into approvability — the precise sweep this surface
# exists to prevent (and a friction-decreasing bulk action, which §10 forbids outright).
# Classification stays per-tool under IMPL-2's typed confirm.
OPT_LEAVE_GATED = "Leave gated"
OPT_BULK_ALLOW = "Always allow the approvable ones"

# How many tools the card NAMES per band before it says "…and N more".
#
# The card's size scales with the inventory, and that payload is both persisted in the
# decision context and rendered in the dashboard — where an oversized render has already
# dropped the socket once (R-UX2, v0.10.20; see ``live_events_pane.clip_detail``). Every
# sibling descriptor bounds its variable-length field, so this one does too.
#
# The cap is a DISPLAY bound ONLY. The counts stay exact, the partition is unaffected,
# and the recorded allow-set is unaffected — truncating the list never truncates what the
# operator is deciding about, and the truncation is always disclosed.
MAX_LISTED_PER_BAND = 40


class ReviewEntry(BaseModel):
    """One backfilled tool as it appears on the review card.

    ``verdict`` is the migration-time score (no params in hand) and is DISPLAY evidence
    only — every safety decision re-derives the band from ``name`` + ``effect_tags``.
    """

    tool_id: str = ""
    name: str = ""
    signature: str = ""
    effect_tags: Tuple[str, ...] = ()
    verdict: str = ""
    reason: str = ""

    model_config = {"extra": "forbid", "frozen": True}


class BulkReviewPartition(BaseModel):
    """The band partition the card renders and the executor acts on."""

    eligible: Tuple[ReviewEntry, ...] = ()      # REQUIRE_APPROVAL — batch-allowable
    excluded: Tuple[ReviewEntry, ...] = ()      # DENY — individually remediable ONLY
    frictionless: Tuple[ReviewEntry, ...] = ()  # ALLOW — never gates, nothing to record

    model_config = {"extra": "forbid", "frozen": True}

    @property
    def needs_review(self) -> bool:
        """Is there anything for the operator to see? An inventory that is entirely
        frictionless must not post a card at all."""
        return bool(self.eligible or self.excluded)


def migration_verdict(name: str, effect_tags: Iterable[str]) -> Tuple[str, str]:
    """Score a backfilled tool AT MIGRATION TIME — i.e. with no call parameters.

    Delegates to the ONE governor (``evaluate_action``) rather than reimplementing the
    ladder: a parallel scorer here would drift from the live gate, and the whole point of
    the partition is that it agrees with what the gate will do. ``is_destructive_param``
    is False because there is no call in hand — which is exactly why a swept allow can
    still meet a DENY later, and why the consumption-side guard is load-bearing.
    """
    from systemu.runtime.action_governance import ActionContext, evaluate_action

    verdict, reason = evaluate_action(ActionContext(
        tool=name or "",
        effect_tags={str(t) for t in (effect_tags or ())},
        is_destructive_param=False,
    ))
    return verdict.value, reason


def build_entry(*, tool_id: str, name: str, effect_tags: Sequence[str],
                signature: str) -> ReviewEntry:
    """Build one review entry, scoring it at migration time."""
    tags = tuple(sorted(str(t) for t in (effect_tags or ())))
    verdict, reason = migration_verdict(name, tags)
    return ReviewEntry(tool_id=tool_id or "", name=name or "", signature=signature or "",
                       effect_tags=tags, verdict=verdict, reason=reason)


def is_bulk_eligible(verdict: Any) -> bool:
    """May a tool with this verdict be swept into a batch Always-allow?

    ONLY the REQUIRE_APPROVAL band. Everything else is False, and the strictness is the
    point — this is an allowlist, not a DENY denylist:

      * a MISSING verdict fails CLOSED. Two readers in this codebase once carried
        opposite defaults on the same key; absence is not consent.
      * the value is normalised via ``.value`` FIRST. ``Verdict`` is a str-Enum and
        ``str(Verdict.DENY)`` is ``"Verdict.DENY"`` — which does not equal ``"deny"``
        and has silently re-opened a guard of exactly this shape before. Matching
        positively on ``"require_approval"`` means such a value falls through to False
        rather than sneaking past a ``!= "deny"`` test.
      * ALLOW is excluded too: a frictionless tool never reaches the approval store, so
        an entry for it would be noise on a safety surface.
    """
    from systemu.runtime.action_governance import Verdict

    raw = getattr(verdict, "value", verdict)
    return str(raw or "").strip().lower() == Verdict.REQUIRE_APPROVAL.value


def _authoritative_verdict(entry: ReviewEntry) -> str:
    """Re-derive the band from the entry's RAW signals.

    Never trusts ``entry.verdict``: the entry round-trips through the decision store as
    JSON between enqueue and resolve, so the stamped value is untrusted input by the time
    the operator clicks. A classification failure fails CLOSED to DENY — an entry we
    cannot score is never sweepable.
    """
    try:
        verdict, _ = migration_verdict(entry.name, entry.effect_tags)
        return verdict
    except Exception:  # noqa: BLE001 — a scoring hiccup must never widen the batch
        logger.warning("[FirstGateReview] could not re-score %r — refusing to sweep it",
                       entry.name, exc_info=True)
        from systemu.runtime.action_governance import Verdict
        return Verdict.DENY.value


def partition_entries(entries: Iterable[ReviewEntry]) -> BulkReviewPartition:
    """Split the backfilled inventory by band.

    Classifies on the RE-DERIVED verdict (see :func:`_authoritative_verdict`) while
    carrying the original entry objects through, so the card renders what was scored and
    a stale stamped verdict cannot move a tool between bands.
    """
    from systemu.runtime.action_governance import Verdict

    eligible: List[ReviewEntry] = []
    excluded: List[ReviewEntry] = []
    frictionless: List[ReviewEntry] = []

    for entry in entries or ():
        verdict = _authoritative_verdict(entry)
        if is_bulk_eligible(verdict):
            eligible.append(entry)
        elif verdict == Verdict.ALLOW.value:
            frictionless.append(entry)
        else:
            # DENY — and any band this function does not recognise. Fail closed: an
            # unrecognised verdict is treated as un-sweepable, never as eligible.
            excluded.append(entry)

    return BulkReviewPartition(eligible=tuple(eligible), excluded=tuple(excluded),
                               frictionless=tuple(frictionless))


def apply_bulk_always_allow(entries: Iterable[ReviewEntry], *, store) -> List[str]:
    """Record a STANDING allow for every genuinely eligible entry. Returns the signatures
    written.

    Two rules make this safe to run over an operator-supplied batch:

      1. every entry is RE-SCORED here (never trusting the stamped verdict), so only the
         REQUIRE_APPROVAL band is written;
      2. nothing else is minted — in particular NO single-use resume bridge. A migration
         card has no parked run to resume, so a one-shot would sit unconsumed on a
         params-INDEPENDENT signature until some later, unrelated call to that tool spent
         it. That is the dangling-bridge hazard the coords-less rescue path documents,
         and a bulk card is the widest possible way to create it.

    Best-effort per entry: a store failure on one tool must not abandon the rest.
    """
    if store is None:
        return []

    written: List[str] = []
    for entry in entries or ():
        if not entry.signature:
            continue
        if not is_bulk_eligible(_authoritative_verdict(entry)):
            logger.info("[FirstGateReview] %r is not REQUIRE_APPROVAL-band — excluded "
                        "from the bulk allow (the remedy is IMPL-2 reclassify)",
                        entry.name)
            continue
        try:
            store.approve(entry.signature)
            written.append(entry.signature)
        except Exception:  # noqa: BLE001
            logger.debug("[FirstGateReview] could not record allow for %r",
                         entry.name, exc_info=True)
    if written:
        logger.info("[FirstGateReview] bulk first-gate review: recorded %d standing "
                    "allow(s)", len(written))
    return written


def apply_bulk_decision(entries: Iterable[ReviewEntry], *, choice: Optional[str],
                        store) -> List[str]:
    """Apply a resolved bulk-review choice. Returns the signatures written.

    Only :data:`OPT_BULK_ALLOW` writes anything. "Leave gated" — and any choice this
    surface does not own — records NOTHING, which is also the fail-closed default for a
    label that drifted or arrived from another surface.
    """
    if (choice or "").strip().lower() != OPT_BULK_ALLOW.strip().lower():
        return []
    return apply_bulk_always_allow(entries, store=store)


def entries_from_context(ctx: Dict[str, Any]) -> List[ReviewEntry]:
    """Rebuild the review entries from a stored decision context. Never raises — a
    malformed row yields an empty batch (nothing swept), not a crash."""
    out: List[ReviewEntry] = []
    for raw in (ctx or {}).get("bulk_entries") or ():
        try:
            out.append(ReviewEntry(**raw))
        except Exception:  # noqa: BLE001
            logger.debug("[FirstGateReview] skipping malformed review entry",
                         exc_info=True)
    return out


def collect_backfilled_entries(vault_dir, *, tool_signature_fn=None) -> List[ReviewEntry]:
    """Read the post-backfill vault tool catalog into review entries.

    Mirrors ``vault_migrator.backfill_effect_tags``'s own traversal (index.json →
    ``tool_<id>.json``) so the card describes exactly what the backfill just stamped.
    Never raises: a migration-moment nicety must not break boot.
    """
    import json
    from pathlib import Path

    entries: List[ReviewEntry] = []
    try:
        vault_dir = Path(vault_dir)
        idx_path = vault_dir / "tools" / "index.json"
        if not idx_path.exists():
            return []
        index = json.loads(idx_path.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        logger.debug("[FirstGateReview] cannot read tool index", exc_info=True)
        return []

    if tool_signature_fn is None:
        from systemu.runtime.command_approvals import tool_signature as tool_signature_fn

    for row in index:
        try:
            tid = row.get("id")
            if not tid:
                continue
            body_path = vault_dir / "tools" / f"tool_{tid}.json"
            if not body_path.exists():
                continue
            body = json.loads(body_path.read_text(encoding="utf-8"))
            name = body.get("name") or row.get("name") or ""
            tags = list(body.get("effect_tags") or [])
            body_hash = _body_hash(vault_dir, body)
            # host_class is empty for the same reason the live gate leaves it empty:
            # there is no host resolver yet (see tool_sandbox._maybe_gate_tool D1). If
            # that changes, the signature computed here must change WITH it or a swept
            # allow would key on a signature the gate never looks up.
            sig = tool_signature_fn(name, body_hash, {str(t) for t in tags},
                                    host_class="")
            entries.append(build_entry(tool_id=tid, name=name, effect_tags=tags,
                                       signature=sig))
        except Exception:  # noqa: BLE001
            logger.debug("[FirstGateReview] skipping tool row", exc_info=True)
    return entries


def _body_hash(vault_dir, body: Dict[str, Any]) -> str:
    """Mirror ``ToolSandbox._tool_body_hash``: sha1 of the implementation file, falling
    back to ``<id>:<version>``.

    The mirror must stay EXACT — including the ``impl_path`` alias and the
    anchor-at-vault-parent rule for a relative path. A signature that disagrees with the
    gate's by one byte is worse than no sweep at all: the card reports the tools as
    remembered while every call still prompts.
    """
    import hashlib
    from pathlib import Path

    impl_rel = body.get("implementation_path") or body.get("impl_path") or ""
    if impl_rel:
        try:
            p = Path(impl_rel)
            if not p.is_absolute():
                p = Path(vault_dir).parent / impl_rel
            return hashlib.sha1(p.resolve().read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            logger.debug("[FirstGateReview] body-hash read failed for %s", impl_rel,
                         exc_info=True)
    return f"{body.get('id', '')}:{body.get('version', '')}"


_REVIEW_MARKER_FILENAME = ".first_gate_review"


def maybe_post_first_gate_review(*, vault, vault_dir, version: str) -> str:
    """Boot hook: post the one-time bulk review card, once per version.

    Version-gated on its OWN marker (``.first_gate_review``), mirroring the
    ``.effect_tags_seed`` pattern ``backfill_effect_tags`` uses — so the card follows the
    backfill that produced it, and a version bump that re-classifies the inventory posts
    a fresh review. Without the marker this would re-hash every tool body on every boot
    just to have the dedup key collapse the card.

    The marker is stamped only when the operator has genuinely been ASKED — i.e. a card
    was posted, or there was nothing to review. A FAILED post leaves it unstamped so the
    next boot retries; stamping there would silently swallow the migration review, which
    is the one thing this surface exists to guarantee.

    Never raises: a migration-moment nicety must not break boot.
    """
    from pathlib import Path

    try:
        marker = Path(vault_dir) / _REVIEW_MARKER_FILENAME
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == str(version):
            return ""
    except Exception:  # noqa: BLE001
        logger.debug("[FirstGateReview] could not read the review marker", exc_info=True)
        marker = None

    try:
        entries = collect_backfilled_entries(vault_dir)
        partition = partition_entries(entries)
        decision_id = ""
        if partition.needs_review:
            decision_id = post_bulk_review_card(entries, vault=vault, version=version)
            if not decision_id:
                # the post failed — do NOT stamp; retry on the next boot
                return ""
            logger.info("[FirstGateReview] posted the one-time first-gate review card: "
                        "%d approvable, %d excluded (unclassifiable + high-severity)",
                        len(partition.eligible), len(partition.excluded))
        if marker is not None:
            marker.write_text(str(version), encoding="utf-8")
        return decision_id
    except Exception:  # noqa: BLE001
        logger.warning("[FirstGateReview] first-gate review pass failed (non-fatal)",
                       exc_info=True)
        return ""


def post_bulk_review_card(entries: Iterable[ReviewEntry], *, vault, version: str) -> str:
    """Post the one-time bulk review card. Returns the decision id, or "" if not posted.

    No card when there is nothing to review (an all-frictionless inventory must not
    manufacture a prompt). ``policy=None`` makes it a FLOOR gate at the queue — never
    auto-executed, even under a Bypass policy; ``BULK_GATE_TYPE`` is on
    ``FLOOR_GATE_TYPES`` and the dedup prefix is render-only in the rail, so the two
    zero-click paths that have auto-granted gates in this codebase before are both shut.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue

    partition = partition_entries(entries)
    if not partition.needs_review:
        return ""

    descriptor = GateDescriptor.from_first_gate_bulk(partition, version=version)
    try:
        return InboxQueue(vault).enqueue(
            descriptor,
            gate_type=BULK_GATE_TYPE,
            policy=None,                  # floor gate — never auto-allow
            context_extras={
                "bulk_entries": [e.model_dump(mode="json")
                                 for e in (partition.eligible + partition.excluded)],
                "first_gate_version": version,
            },
        )
    except Exception:  # noqa: BLE001 — a migration nicety must not break boot
        logger.warning("[FirstGateReview] could not post the bulk review card",
                       exc_info=True)
        return ""
