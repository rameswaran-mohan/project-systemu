"""R-A10 step B3 — the requirement binder (spec UNIFIED-v2 §5.3, BIND-mode).

For each objective, read the CHOSEN capability's schema and, for every REQUIRED
leaf, attempt to BIND it from the 5 spec-ordered sources. "What's missing" is then
a schema-DIFF (a leaf no source could bind), never an LLM guess. This is the
open-world reframe: the planner reasons over a concrete gap list, not a hunch.

The 5 bind sources, tried IN ORDER (first hit wins) — §5.3:
  1. FileHandle       — a granted-root salient file (re-gated through
                        GrantedRootsStore.is_within_granted). Origin content_derived.
                        Consulted ONLY for a leaf the path oracle typed as a path
                        (``_PATH_ONLY_SOURCES``): it scores the objective's GOAL TEXT,
                        so ungated it pre-filled a path into every leaf in the schema —
                        ``password``, ``query``, ``count``. See that constant.
  2. run-context      — a prior objective's produced file / run state (best-effort
                        from ctx.files_produced; a typed objective_outputs store is
                        deferred to R-A11). Origin content_derived. Like source #1 it
                        binds a FILE PATH with no key test, so it too is consulted ONLY
                        for a path leaf (``_PATH_ONLY_SOURCES``) — ungated it pre-filled
                        a path into every leaf AND, running before the profile, masked
                        the silent operator-profile bind. See that constant.
  3. inventory ENTRY  — a SituationReport hit (services / capabilities / roots /
                        credentials / declared_intents), prefer curated=True. Origin is
                        DERIVED from the source kind (scanned/surveyed content clamps to
                        content_derived — a survey entry's origin_class is unvalidated str
                        and could be forged; never laundered into a silent bind).
  4. operator PROFILE — situation["profile"] UserProfile spine + user_facts (a default
                        like account_id/default_repo is a user_facts entry, matched by
                        tag/key). Origin operator; confidence = the fact's confidence.
  5. schema           — the leaf's own default / const / enum[0]. Origin
                        systemu_authored (systemu's own catalog).

IMPL-5 (taint travels): ``Requirement.value_origin`` is COPIED from the winning
source object's ``origin_class`` — NEVER recomputed. A ``content_derived`` value
(untrusted file bytes) is NEVER silently bound: even at confidence 1.0 it is forced
into the ``ask_bundle`` (one-click operator confirm). This is the load-bearing safety
invariant — an untrusted inventory value can close a gap for the PLANNER's view but
can never become an unattended input.

The T_high gate (net-new here — §5.3 leaves the threshold to the binder). NOTE the
two axes are SEPARATE: ``state`` is governed by confidence alone, and taint is
enforced by ``_needs_ask``, NOT by demoting ``state``. A content_derived bind at
confidence >= T_HIGH really is ``state="have"`` — it is kept out of a silent bind
because ``_needs_ask`` surfaces it anyway. Do not "fix" this by forcing the state;
``test_ac1_silent_bind_invariant`` pins the split on purpose ("state='have' alone
can never make it silent"), and a stale earlier version of this very docstring —
which claimed content_derived → state="resolvable" — has already misled a reader
into specifying the wrong invariant.
  * bound, confidence >= T_HIGH, and NOT content_derived  → state="have"   (silent)
  * bound, below T_HIGH                                    → state="resolvable" + ask_bundle
  * bound, content_derived (ANY confidence)                → state per confidence,
        ALWAYS in the ask_bundle via ``_needs_ask`` (one-click confirm, never silent)
  * required + no source bound it                          → state="missing"  + ask_bundle,
        kind = "input" (a path leaf), else "capability" (no candidate path can do it),
        else "decision" (an operator choice — which repo / which identity)

T_HIGH = 0.80: a deterministic, provenance-carrying bind (an operator profile fact,
a schema default) sits at/above it and binds silently; anything softer (a fuzzy
inventory match, a heuristic run-context guess) sits below and is surfaced for a
one-click confirm. 0.80 (not 1.0) lets a strong-but-not-certain operator-origin
match bind without an ask, while a weak match still routes to the operator — the
asymmetry the open-world card wants (never silently act on a shaky binding).

AC4 (§5.8): the Objective's ``requires_external_verification`` is stamped here from
the capability's EffectTag — an external / irreversible / money / UNKNOWN effect is
dangerous-until-proven ⇒ True. An EMPTY effect_tags list is UNKNOWN-until-classified
(never "no effect") ⇒ also True.

Local imports throughout (cycle-avoidance: the runtime stores import back through
the runtime package). Every public entry is defensive — a broken schema / missing
situation yields ``[]`` or a best-effort list, never an exception.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# The silent-bind threshold. A bind at/above T_HIGH that is NOT content_derived is
# trusted enough to use unattended (state="have"); below it (or content_derived) the
# operator gets a one-click confirm (state="resolvable", added to the ask_bundle).
T_HIGH = 0.80

# Canonical taint values (mirror table_store.TableItem.origin_class). content_derived
# is the untrusted axis — never silent-bound (§5.10.b / IMPL-5).
_CONTENT_DERIVED = "content_derived"
_OPERATOR = "operator"
_SYSTEMU = "systemu_authored"

# The canonical value_origin axis (Requirement.value_origin is a Literal of these).
# A bind carrying anything else is CLAMPED to content_derived (fail-untrusted) before
# a Requirement is constructed, so a poisoned/non-canonical origin can never raise a
# ValidationError that empties the whole objective's diff (Finding 2 / #2).
_CANONICAL_ORIGINS = frozenset({_OPERATOR, _SYSTEMU, _CONTENT_DERIVED})


def _coerce_origin(origin) -> str:
    """Clamp a bind's ``value_origin`` to a canonical taint value. A non-canonical /
    absent origin fails UNTRUSTED (→ content_derived), never systemu_authored/operator
    — an unknown taint is treated as the dangerous axis (IMPL-5 fail-safe)."""
    return origin if origin in _CANONICAL_ORIGINS else _CONTENT_DERIVED


# ── the per-objective bind context ───────────────────────────────────────────
@dataclass
class _BindCtx:
    """Carries the growing Requirement list, the 5 resolved sources, and T_high.

    ``situation`` is the SituationReport dict; ``ctx`` the run context; ``granted``
    the GrantedRootsStore (re-gate for source #1); ``tool_name`` feeds the path
    oracle. ``provided_params`` (R-A12c) are the CURRENT tool-call's already-supplied
    parameters (``decision.parameters`` at the tool-call seam) — source #0 reads them.
    The source *accessors* live on the bind functions, not here — this only holds the
    raw material + the output list."""
    situation: dict
    ctx: Any
    granted: Any
    tool_name: str = ""
    reqs: List[Any] = field(default_factory=list)
    t_high: float = T_HIGH
    reference_text: str = ""          # R-A11a §5.4: the objective goal text the resolver reads
    # R-A12c: the tool-call's already-supplied params (decision.parameters). A dict or
    # None; source #0 (_bind_provided_params) binds a required leaf whose key is present.
    provided_params: Optional[dict] = None
    # R-A16 §5.9: the vault, threaded EXPLICITLY from the call site. It feeds TWO
    # things: _value_digest's keying, and (via _granted_store) the GrantedRootsStore
    # that source #1's confinement re-gate needs — without it ``granted`` is None and
    # source #1 is DORMANT. It is deliberately NOT read off ``ctx``: the real
    # ExecutionContext carries no vault (and must not — it is serialized and
    # snapshotted, so a live handle on it is a snapshot-shape hazard).
    vault: Any = None


def _descend_provided(container, segs):
    """Resolve the value at a schema-walk PATH inside a provided-params tree. ``segs`` are
    fixture_synth._walk's accumulator segments: a property name descends a dict; the literal
    ``'[]'`` iterates a list (EVERY element must supply the remaining sub-path — a
    fully-provided array). Returns the bound value (the first element's, for an array), or
    None if any segment is absent / None / type-mismatched (⇒ a real gap)."""
    cur = container
    for i, seg in enumerate(segs):
        if seg == "[]":
            if not isinstance(cur, list) or not cur:
                return None
            rest = segs[i + 1:]
            first = None
            for el in cur:
                v = _descend_provided(el, rest) if rest else el
                if v is None:
                    return None
                if first is None:
                    first = v
            return first
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


# ── source #0: a param the CURRENT tool-call ALREADY supplied (R-A12c / R-A13a) ──
def _bind_provided_params(bc: _BindCtx, key: str, spec: dict,
                          path: tuple = ()) -> Optional[Tuple[str, str, str, float]]:
    """Bind a required leaf from the CURRENT tool-call's provided params, resolved BY the
    schema-walk ``path`` (R-A13a) — NOT a flat ``key in dict`` (which falsely flagged a
    nested/array/oneOf leaf the LLM DID supply nested: the R-A12c over-ask defects #2/#3).

    IMPL-5 taint (the judgment call): raw provided params carry NO taint signal of their
    own, so an LLM-emitted plan value defaults to ``systemu_authored`` (systemu's own
    reasoning, non-content ⇒ binds silently, state="have", never asked — the over-ask fix).

    THE PROMPT-CHANNEL EXCEPTION (see :func:`_provided_value_is_content_seeded`). An
    earlier version of this docstring claimed "NOT laundering: there is no
    content_derived signal on raw provided params to preserve". That is FALSE whenever
    the value came out of a ``content_derived`` user_fact that this system itself put in
    front of the model: the signal exists — it is sitting in the profile — and defaulting
    to ``systemu_authored`` DISCARDS it. Executed end-to-end, the same value bound
    ``content_derived``/ASK through source #4 and ``systemu_authored``/SILENT through this
    source, purely because it took a detour through a prompt. The clamp below is the
    "future per-key taint map" this docstring anticipated, for the one carrier where the
    taint record already exists."""
    params = getattr(bc, "provided_params", None)
    if not isinstance(params, dict):
        return None
    segs = [s for s in (path or ())]
    if not segs:                                  # top-level leaf (or path not threaded)
        segs = [key] if key else []
    if not segs:
        return None
    val = _descend_provided(params, segs)
    if val is None:                               # absent / None ⇒ not a supplied value
        return None
    origin = (_CONTENT_DERIVED if _provided_value_is_content_seeded(bc, val)
              else _SYSTEMU)
    return (f"provided:{'/'.join(str(s) for s in segs)}", "provided", origin, 1.0, val)


# ── source #1: a granted-root salient FileHandle ─────────────────────────────
def _bind_filehandle(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple]:
    """R-A11a §5.4: resolve a path leaf to a granted-root file by SCORING the objective's
    reference text against the situation's salient handles (was: blind first-salient @0.9).
    Preserves the 4-tuple contract and the IMPL-5 clamp — a resolved FILE is inherently
    content_derived, so it NEVER silent-binds (the _needs_ask gate forces the confirm)."""
    from systemu.runtime.reference_resolver import resolve_reference
    try:
        verdict = resolve_reference(bc.reference_text, situation=bc.situation,
                                    granted=bc.granted, key=key, vault=bc.vault)
    except Exception:
        logger.debug("[binder] reference_resolver raised; leaf falls through", exc_info=True)
        return None
    if verdict.state != "resolvable" or not verdict.referent:
        return None                                   # → falls through → input/missing ask-for-path
    # CLAMP to content_derived regardless of score (IMPL-5 fail-untrusted).
    # R-B5 / T5: attribute the file to the curated ROOT that contains it — a
    # `data_root` is the item kind operators most often put on the table, and
    # without this the chip would under-report folders specifically.
    return (f"file:{verdict.referent}", "situation", _CONTENT_DERIVED,
            float(verdict.confidence), verdict.referent,
            _root_table_id(bc, verdict.referent))


def _root_table_id(bc: _BindCtx, path) -> Optional[str]:
    """The table id of the curated granted-root CONTAINING ``path``, else None.

    Longest-prefix wins, so a curated sub-root nested inside a plain parent is
    attributed to the sub-root rather than to whichever happened to be enumerated
    first. Matching mirrors ``compose_table``'s own ``os.path.normcase`` comparison
    so the two agree about what "the same root" means.

    Attribution only, and best-effort: any failure returns None (not attributed)
    rather than disturbing a bind that has already succeeded."""
    try:
        target = os.path.normcase(os.path.abspath(str(path or "")))
        if not target:
            return None
        best_len, best_id = -1, None
        for root in _situation_list(bc.situation, "roots") or []:
            tid = _get(root, "table_item_id")
            if not isinstance(tid, str) or not tid:
                continue
            rp = os.path.normcase(os.path.abspath(str(_get(root, "path") or "")))
            if not rp:
                continue
            # component-boundary safe: `/data` must not match `/database/x`
            if (target == rp or target.startswith(rp + os.sep)) and len(rp) > best_len:
                best_len, best_id = len(rp), tid
        return best_id
    except Exception:
        return None


# ── source #2: run-context / a prior objective's output ──────────────────────
def _bind_run_context(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple[str, str, str, float]]:
    """Best-effort bind from the run context's produced files (a typed
    objective_outputs store is deferred to R-A11). A produced file is treated as a
    content_derived candidate — so it too routes to the ask (never silent)."""
    produced = getattr(bc.ctx, "files_produced", None)
    if not produced or not isinstance(produced, list):
        return None
    for p in produced:
        if isinstance(p, str) and p:
            # heuristic, low-confidence, content_derived → always an ask
            return (f"run_context:{p}", "run_context", _CONTENT_DERIVED, 0.5, p)
    return None


# ── source #3: a SituationReport inventory ENTRY (services/caps/roots/creds) ──
def _bind_inventory_entry(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple]:
    """Bind from a matching inventory entry, preferring ``curated=True``. The taint is
    DERIVED from the source kind (scanned inventory content clamps to content_derived —
    IMPL-5 fail-untrusted; a forged ``origin_class`` can never launder into a silent
    bind). Handles the IMPL-8 multi-identity case: two services matching the same leaf ⇒
    signal a DECISION (return None here so the leaf falls through to a decision
    requirement).

    R-B5 / T5: a table-backed winner ALSO returns a 6th tuple element — the
    ``table_item_id`` ``compose_table`` stamped on the entry. Attribution only; it
    never changes the taint, the confidence, or the ask decision (see
    ``Requirement.table_item_id``). Sources with no table provenance return the
    5-tuple unchanged, which reads as "not table-attributed"."""
    kl = (key or "").lower()

    # credentials are NAMES only (AC2 of R-A9) — a leaf naming a service whose
    # credential we hold binds operator-origin (the operator authorized it).
    creds = _situation_list(bc.situation, "credentials")
    for name in creds or []:
        if isinstance(name, str) and name and (name.lower() in kl or kl in name.lower()):
            # NO resolved value: the bind is a credential NAME, and the value behind it
            # is a secret that must never be digested into an observability corpus.
            return (f"credential:{name}", "situation", _OPERATOR, 0.85, None)

    # services — a leaf about an account / service identity. TWO matching services
    # (two acting identities) is an IMPL-8 DECISION, not a silent pick.
    services = _situation_list(bc.situation, "services")
    if services and any(w in kl for w in ("account", "identity", "service", "as_user", "login")):
        matched = [s for s in services if _service_relevant(s, kl)]
        if len(matched) >= 2:
            return None            # ambiguous identity → fall through to a decision
        if len(matched) == 1:
            s = matched[0]
            acct = _get(s, "account")
            if acct:
                # IMPL-5: DERIVE the taint from the source kind — a scanned service entry
                # clamps to content_derived (its claimed origin_class is unvalidated str
                # and can be forged); never launders a forged 'operator' into a silent bind.
                origin = _entry_origin(s)
                return (f"service:{_get(s, 'name')}#{acct}", "situation", origin,
                        0.85, acct, _table_id(s))

    # declared_intents / capabilities — a curated-first name match.
    for field_name in ("capabilities", "declared_intents"):
        entries = _situation_list(bc.situation, field_name)
        best = None
        for e in entries or []:
            nm = str(_get(e, "name") or _get(e, "tool_id") or "").lower()
            if nm and (nm in kl or kl in nm):
                if _get(e, "curated"):
                    best = e
                    break
                best = best or e
        if best is not None:
            # IMPL-5: DERIVE the taint from the source kind — a scanned capability /
            # declared-intent match clamps to content_derived (fail-untrusted; its
            # claimed origin_class is unvalidated str). A genuinely systemu_authored value
            # comes from source #5 (schema default), not a fuzzy inventory name match.
            origin = _entry_origin(best)
            return (f"inventory:{field_name}", "situation", origin, 0.8,
                    _get(best, "name") or _get(best, "tool_id"), _table_id(best))
    return None


def _table_id(entry) -> Optional[str]:
    """The TableItem id behind an inventory entry, or None when the entry is not
    table-backed (R-B5 / T5 attribution).

    Two shapes, because ``compose_table`` produces two:
      * an ANNOTATED live entry (service / capability / root) carries ``table_item_id``;
      * a ``declared_intents`` row is a plain dict that IS a table item — its ``id``.

    A ``declared_intents`` row is table-backed BY CONSTRUCTION (``compose_table`` is
    its sole producer), so the ``id`` fallback is read only for that shape and only
    after ``table_item_id`` is absent — never as a general "any dict with an id"
    rule, which would let an unrelated inventory entry claim table provenance.

    Returns None on anything non-str/empty: attribution is decorative, and a
    malformed id must degrade to "not attributed", never raise into the bind."""
    try:
        tid = _get(entry, "table_item_id")
        if not tid and isinstance(entry, dict) and entry.get("kind"):
            # a declared_intents row (dict + kind) — its own id IS the table id
            tid = entry.get("id")
        return tid if isinstance(tid, str) and tid else None
    except Exception:
        return None


def _entry_origin(entry) -> str:
    """The taint for an inventory-entry bind, DERIVED from the source KIND — NOT copied
    verbatim from the object's ``origin_class`` field (IMPL-5 fail-untrusted).

    ``_entry_origin`` is called only for SCANNED/SURVEYED inventory entries (services,
    capabilities, declared_intents, roots) whose ``origin_class`` is a plain UNVALIDATED
    str on the survey model. A poisoned SituationReport (a resume-rehydrated snapshot, or
    any future non-live source) could carry an entry FORGING ``origin_class="operator"`` →
    a silent bind laundering an untrusted value into the trusted axis. So any entry whose
    value originates from scanned/untrusted content is CLAMPED to content_derived; only a
    genuinely operator-authored SOURCE (the operator-PROFILE fact — bound in
    ``_bind_profile`` with a hard-coded operator origin, never here) may carry operator.

    NOTE: a systemu-authored capability entry ALSO clamps here — a bind that needs the
    systemu_authored axis comes from source #5 (``_bind_schema_default``), which stamps it
    directly; an INVENTORY capability match is a fuzzy name hit over surveyed data, so it
    fails untrusted too. When in doubt, clamp to content_derived (fail-untrusted)."""
    return _CONTENT_DERIVED


def _service_relevant(svc, kl: str) -> bool:
    """A service is relevant to an account/identity leaf if it carries a live token
    (an actable identity). Named-service match narrows it further when possible."""
    if not _get(svc, "has_live_token"):
        # still count it as a candidate identity if it names the leaf explicitly
        nm = str(_get(svc, "name") or "").lower()
        return bool(nm and nm in kl)
    return True


# ── source #4: the operator PROFILE (UserProfile spine + user_facts) ──────────
_PROFILE_SPINE = {"name", "location_text", "timezone", "default_output_dir"}


def _bind_profile(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple[str, str, str, float]]:
    """Bind from the operator profile: the 4-field UserProfile spine OR a user_facts
    entry (where a default like account_id/default_repo lives), matched by tag/key.
    Origin operator; confidence carried from the fact (spine facts are confidence 1.0)."""
    profile = _get(bc.situation, "profile")
    if not isinstance(profile, dict) or not profile:
        return None
    kl = (key or "").lower()

    # spine fields — a direct key match (default_output_dir ⇒ an output_dir leaf).
    for f in _PROFILE_SPINE:
        val = profile.get(f)
        if val and (f in kl or kl in f or _key_token_overlap(kl, f)):
            return (f"profile:{f}", "operator_profile", _OPERATOR, 1.0, val)

    # user_facts — scan by a tag OR key-token match; carry the fact's confidence.
    facts = profile.get("user_facts")
    if isinstance(facts, list):
        for fact in facts:
            tags = _get(fact, "tags") or []
            tag_hit = any(isinstance(t, str) and (t.lower() in kl or kl in t.lower()
                                                  or _key_token_overlap(kl, t.lower()))
                          for t in tags)
            fact_txt = str(_get(fact, "fact") or "").lower()
            txt_hit = bool(kl) and kl in fact_txt
            if tag_hit or txt_hit:
                conf = _get(fact, "confidence")
                conf = float(conf) if isinstance(conf, (int, float)) else 1.0
                fid = _get(fact, "id") or "fact"
                # NO resolved value: a user_fact is a SENTENCE ("my default repo is
                # acme/prod"), not the parameter value. The bind names a fact id and
                # leaves extraction to the operator, so there is nothing to compare an
                # answer against. Digesting the sentence would guarantee a mismatch and
                # report every profile-fact ask as "the binder's value was wrong".
                return (f"profile_fact:{fid}", "operator_profile",
                        _fact_origin(fact), conf, None)
    return None


#: ``UserFact.source`` values for which an ABSENT ``origin_class`` must NOT be
#: grandfathered to ``operator``. Both are sole-writer strings for writers that are
#: NOT operator-authoring surfaces:
#:
#:   * ``auto_extract`` — ``fact_extractor.extract_from_chat`` (R-A16): an LLM picks
#:     which sentences of operator-DELIVERED text become durable facts; nobody reviews
#:     the result. Closes that legacy corpus with no migration.
#:   * ``ask_promotion`` — ``ask_promotion`` (G-LEARN slice 3, §5.9): the promoter
#:     STAMPS the answer's original origin explicitly, so this entry is pure
#:     defence-in-depth. It is the difference between the slice's most likely defect
#:     (a forgotten ``origin_class=`` kwarg) causing an extra confirm — safe — and
#:     causing a page-derived value to silent-bind as trusted — the laundering bug.
#:     Fail-untrusted is the right default for a value systemu wrote on its own.
_UNTRUSTED_ABSENT_SOURCES = frozenset({"auto_extract", "ask_promotion"})


def _fact_origin(fact) -> str:
    """The taint for a ``user_facts`` bind (R-A16 slice-1, IMPL-5 "taint travels").

    Unlike ``_entry_origin`` — which DERIVES taint from the source kind because a
    surveyed inventory entry's self-declared ``origin_class`` is forgeable — a profile
    fact's stamp IS authoritative: the profile is written only through operator
    surfaces and the §5.9 promoter, never rehydrated from scanned content. So the
    stamp is READ, then CLAMPED:

      * ABSENT + ``source="auto_extract"`` ⇒ ``content_derived``. See below — the
        grandfather is WRONG for this one source.
      * ABSENT  ⇒ ``operator``. Grandfathers every fact written before this slice and
        every other current writer (all verified operator surfaces), so legacy
        behavior is unchanged — the compatibility claim of the slice.
      * PRESENT ⇒ ``_coerce_origin``: canonical values pass through; anything else
        (a hand-edited JSONL, a poisoned stamp) fails UNTRUSTED to ``content_derived``.

    THE ``auto_extract`` CARVE-OUT (R-A16). ``fact_extractor.extract_from_chat`` now
    stamps ``content_derived`` at the write, but every such fact ALREADY persisted in
    an operator vault carries an ABSENT stamp and would keep the grandfather. This
    reader-side clamp closes that legacy corpus with no migration, and it is
    DETERMINISTIC rather than a heuristic:

      * ``UserFact.source`` is a REQUIRED field (no default) validated on read, so an
        ``auto_extract`` row is unambiguously identifiable;
      * ``fact_extractor`` is the SOLE writer of that source string in the tree.

    Those facts are LLM extractions from ``chat_entry["prompt"]`` — operator-DELIVERED
    text, not operator-AUTHORED. Paste an email or a scraped page into chat and the
    extractor, not the operator, decides which of its sentences become durable facts;
    nobody reviews the result. Slice 1 allowlisted that caller as an operator surface;
    that claim is retracted here.

    The clamp is keyed to that ONE source deliberately: ``onboarding`` (welcome/tour)
    and ``explicit_user`` (``user remember``) are operator-authored and MUST keep
    binding silently, or the profile stops paying off and re-asks the operator for
    what they typed themselves.

    A ``content_derived`` result can never silent-bind: ``_needs_ask`` forces it into
    the ask_bundle regardless of confidence. That is what stops a §5.9 promotion from
    laundering page-derived content into the trusted axis on the NEXT run.
    """
    raw = _get(fact, "origin_class")
    if raw is None:
        if _get(fact, "source") in _UNTRUSTED_ABSENT_SOURCES:
            return _CONTENT_DERIVED
        return _OPERATOR
    if not isinstance(raw, str):
        # A profile dict is rehydrated from JSON, so this field can be ANY type. A
        # non-str stamp is meaningless ⇒ fail UNTRUSTED. Also sidesteps a latent
        # TypeError: ``_coerce_origin`` does ``origin in <frozenset>``, which RAISES
        # on an unhashable value (a list/dict from a malformed or poisoned profile).
        # That raise is currently absorbed by the per-leaf fail-safe and degrades the
        # leaf to a "missing" gap; clamping here makes the taint decision explicit
        # instead of relying on the broad except.
        return _CONTENT_DERIVED
    return _coerce_origin(raw)


# ── the PROMPT-CHANNEL taint clamp (source #0) ───────────────────────────────
#
# THE CHANNEL. ``content_derived`` user_facts are injected verbatim into planning
# prompts (``scroll_refiner``'s tier-1 elder_intake payload, recent 20; the planner's
# fenced SituationReport; ``shadow_runtime``'s profile block, recent 5). Every one of
# those is now capped to a most-recent window — the SituationReport was NOT, and capping
# it is what makes ``_PROMPT_FACT_WINDOW`` sound; see that constant. Nothing stops the
# model from copying such a value into a tool call's params — and source #0 then stamped it
# ``systemu_authored`` at confidence 1.0, a TRUSTED axis, so it bound SILENTLY. The
# taint laundered through the MODEL rather than through the store: the same value that
# source #4 confirm-gates as ``content_derived`` came back clean.
#
# WHY A GATE AND NOT A PROMPT MARKER. Rendering provenance inline and asking the model
# to honour it is not a control — it is a request to an untrusted component, and the
# tainted fact itself can carry text arguing against it. CAP-0 #4 already bans exactly
# this shape ("NOT ... any tool self-report" as the source of truth for a trust
# decision). The evidence is direct: the planner's fenced SituationReport ALREADY
# renders ``origin_class`` verbatim on every user_fact, and the laundering completed
# anyway — because the decision is made HERE, not there. So the clamp is deterministic
# and carrier-agnostic: it does not care which prompt exposed the value, only that the
# value matches something this system recorded as tainted.
#
# WHAT THIS DOES NOT CLOSE (deliberate, documented, not implied by the code):
#   * IN-CONTEXT content with no stored taint record — a tool result, a fetched page, a
#     file the model read this turn. There IS no recorded origin to match against; that
#     needs a real per-key taint map threaded from the content source.
#   * THE QUICK LANE — ``quick_task`` builds its own prompt (via
#     ``user_context.profile_context_block``) and dispatches through ``ToolSandbox``
#     WITHOUT ever calling the binder, so no IMPL-5 gate runs there at all.
#
#     BUT THE EXPOSURE IS BOUNDED, and the bound matters for any arming decision made
#     off this note. The quick lane is not ungoverned: ``quick_task._execute_tool``
#     threads ``tool=`` into ``ToolSandbox.execute_tool``, so ``_maybe_gate_tool`` — the
#     EFFECT-CLASS gate — runs on every quick-lane call. Measured end to end: every
#     ``action_governance._APPROVAL_TAGS`` effect (net_mutate / send_message /
#     money_move / oauth_call / local_delete / shell_exec) AND the UNKNOWN empty-tags
#     floor confirm-card, with the ACTUAL parameter values on the card via
#     ``args_preview``. Only the ALLOW band (net_read / local_read) executes unattended.
#     So the residual is "a tainted value can reach a READ unattended", NOT "a tainted
#     value can move money unattended" — IMPL-5's protective purpose is met for every
#     dangerous effect by effect-class gating, and it is IMPL-5's taint-based LETTER
#     that is unmet, on the read band only.
#
#     That bound rests entirely on that one ``tool=`` keyword and had no dedicated pin;
#     ``tests/test_quick_lane_action_gate_wiring.py`` now pins it in BOTH directions
#     (dangerous bands card / read band stays frictionless) and characterizes the
#     read-band residual so closing it is a visible edit. Note the arming consequence:
#     the quick lane is not a ShadowRuntime run, so it never writes the ``s4_shadow``
#     meter — ``s4_activation.s4_shadow_arm_verdict`` reads READY off full-lane evidence
#     alone and is blind to the DEFAULT lane.
#   * Values shorter than ``_MIN_TAINT_MATCH_LEN`` (see below).
#   * A value SPLIT ACROSS TWO FACTS — "the prefix is acct-99", "the suffix is attacker"
#     — which the model can rejoin into a value neither fact contains. Closing it means
#     matching against CONCATENATIONS of the corpus, and that is not a smaller version of
#     what ``_canonical_taint_form`` does: joining two facts manufactures adjacencies that
#     never existed in either, so every join seam becomes a new false-match site, and the
#     candidate set grows quadratically in a corpus this clamp just paid to bound. The
#     honest statement is that a determined author who controls TWO stored facts can still
#     launder one value past this gate. What that buys is bounded by the same
#     effect-class gating described for the quick lane below: the dangerous bands still
#     card. Deliberately left open, with the reasoning recorded rather than the gap
#     merely noted.
#   * A ``+``-encoded space (``a+b`` for ``a b``). ``unquote`` does not decode ``+`` —
#     only ``unquote_plus`` does — and applying the plus rule unconditionally would fold
#     genuine ``+`` characters (a version string, an email tag address) into separators.
#     Narrow, and the safe direction.

#: Minimum length for a provided value to be matched against the tainted corpus. A
#: short value ("is", "id", "on") is a token of almost any sentence, so matching it
#: would clamp unrelated leaves and re-introduce the R-A12c over-ask defect wholesale.
#: 4 keeps realistic identifiers (account ids, emails, paths, bare amounts like "5000")
#: in scope. Values below it bind unchanged — a documented residual, not an oversight.
_MIN_TAINT_MATCH_LEN = 4

#: How many of the most-recent user_facts the taint corpus may draw from — the BOUND
#: on the clamp's over-ask cost, and the other half of the canonicalisation below.
#:
#: THE PRINCIPLE. This clamp exists for exactly one channel: a value that reached the
#: MODEL through a prompt and came back as a trusted parameter. A value the model was
#: never shown cannot have travelled that channel, so matching against it buys no
#: security — only over-asks. The sound corpus is therefore the PROMPT-RENDERED fact
#: set, not the whole vault.
#:
#: WHY 20. It is the WIDEST cap any prompt renderer applies, enumerated against every
#: fact-rendering prompt path in the tree rather than assumed:
#:   * ``scroll_refiner`` tier-1 elder_intake  — ``load_user_facts(recent=20)``
#:   * ``shadow_runtime`` profile block        — ``load_user_facts(recent=5)``
#:   * the planner's fenced SituationReport    — ``render_situation_for_prompt``, which
#:     json-dumps ``build_profile``'s UNCAPPED list. That renderer is capped to THIS
#:     constant in ``situational_inventory``; without that edit this bound would be a
#:     real security regression, not a cost fix, because fact #21 IS shown to the
#:     planner. The two constants are pinned equal by
#:     ``test_ra16_taint_corpus_bound.test_render_cap_matches_binder_window``.
#:   * ``user_context.profile_context_block`` — UNCAPPED, but filtered to the
#:     ``office_context`` tag, and its ONLY caller is ``quick_task``. The quick lane
#:     never calls the binder (see the residual note above), so no bind decision is ever
#:     made against what that prompt rendered and it cannot widen the channel this clamp
#:     closes. Named here because "the corpus is what the prompts carried" is the whole
#:     argument, and an unlisted renderer would silently weaken it — if the quick lane
#:     ever gains a bind path, this entry is the one that has to be revisited.
#: Taking the widest is the fail-safe direction: a fact any renderer showed is in scope.
#:
#: ORDERING. ``build_profile`` -> ``get_facts`` returns facts NEWEST-LAST and
#: ``recent=N`` means "the last N after filtering", so the last-N slice here selects
#: the same rows the renderers carry. Both read the same non-superseded view.
#:
#: WHAT THIS GIVES UP. A fact that has since aged out of every prompt window but whose
#: value the model lifted in an EARLIER run and is only now replaying through provided
#: params. That requires the value to survive across runs in the model's output without
#: any prompt carrying it — the plan text itself would have to be the carrier, which is
#: the "in-context content with no stored taint record" residual already documented
#: above, not a new one.
_PROMPT_FACT_WINDOW = 20

#: Zero-width and BOM code points. Invisible in every operator-facing surface, so a
#: value carrying one is indistinguishable from the clean value to the human who would
#: review the confirm — which is exactly why it must not change the match decision.
_ZERO_WIDTH = dict.fromkeys(
    map(ord, "​‌‍⁠﻿"), None)

#: Matched pairs only — a lone leading quote is not a wrapper and must not be eaten.
#: Kept byte-identical to ``replay_metrics._QUOTE_PAIRS`` (R-A16 F2) so the two
#: canonicalisers stay unifiable; see ``_canonical_taint_form``.
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
                ("«", "»"), ("`", "`"))

#: TRAILING only, never interior — stripping interior punctuation would fold ``a.b.c``
#: into ``abc``, a substring-style collapse that manufactures over-asks.
_TRAILING_PUNCT = ".,;:!?)]}>"

#: The SEPARATOR CLASS. ``-``, ``_`` and whitespace are interchangeable FORM for the
#: same identifier, and which one is stored is chosen by whoever authored the content —
#: so a separator swap is an attacker-reachable reshape, not a benign difference.
#: Folded to a single space, which is non-alphanumeric and therefore still a TOKEN
#: DELIMITER for ``_appears_as_token`` — the fold loosens which forms match, never the
#: boundary rule that keeps this from being a substring test.
#:
#: ``/`` is deliberately NOT in the class: ``out/report.md`` and ``out report.md`` are
#: different values, and folding path structure into the separator class is the one
#: step that would start manufacturing false matches.
_SEP_RUN = re.compile(r"[-_\s]+")
_DUP_SLASH = re.compile(r"/{2,}")


def _strip_wrappers(s: str) -> str:
    """Peel matched surrounding quotes and trailing punctuation until stable.

    Bounded (never unbounded), and never strips to empty — a value that is ENTIRELY
    punctuation keeps its own form rather than collapsing to ``""``, which would match
    inside every fact in the corpus. Mirrors ``replay_metrics._strip_wrappers``."""
    for _ in range(4):
        before = s
        for lo, hi in _QUOTE_PAIRS:
            if len(s) >= 2 and s.startswith(lo) and s.endswith(hi):
                s = s[1:-1].strip()
                break
        trimmed = s.rstrip(_TRAILING_PUNCT)
        if trimmed:
            s = trimmed
        if s == before:
            break
    return s


def _canonical_taint_form(value) -> str:
    """The FORM-INSENSITIVE canonical string used on BOTH sides of the taint match.

    The clamp compared RAW text, so it caught only case and surrounding whitespace.
    Measured end-to-end, every other reshape LAUNDERED: separator swapped (``-``→``_``,
    ``-``→space), a trailing period, a quote pair, URL-encoding, an interposed
    zero-width space. Those are not exotic — quoting, punctuation and URL-encoding a URL
    parameter are ordinary MODEL behaviour, so the clamp failed open on benign output;
    and because whoever authors the content picks the STORED form, each one is also
    attacker-reachable by construction.

    THE LINE THIS MUST NOT CROSS. Every step is a total rewrite to a canonical string,
    and the match over the result stays TOKEN-DELIMITED (:func:`_appears_as_token`), not
    a raw ``in``. No step deletes a structural character, so ``acct99attacker`` must
    never canonicalise onto ``acct-99-attacker`` and ``acct-98-attacker`` must never
    reach ``acct-99-attacker``. Separator-DELETED and sibling-value forms therefore stay
    UNMATCHED — pinned negatively by
    ``test_ra16_taint_corpus_bound.test_canonicalisation_does_not_become_a_substring_match``.

    ORDER MATTERS, and follows ``replay_metrics.canonical_compare_form`` (R-A16 F2) so
    the two stay unifiable once both land: strip zero-width first (one can sit between
    a quote and the value), unwrap (a quoted value may hide the ``%``), URL-decode (a
    decode can REVEAL a separator: ``acct%2D99`` → ``acct-99``), then fold separators,
    then casefold.

    That sibling is NOT imported: it lives on an unmerged branch, and ``value_ref`` /
    ``normalize_value`` may not be widened because every already-stamped on-disk digest
    depends on their exact output. This is a separate, comparison-only helper that
    persists nothing.

    Never raises — an unstringable value yields ``""``, which callers treat as no-match."""
    try:
        s = str(value)
    except Exception:
        return ""
    s = s.translate(_ZERO_WIDTH).strip()
    if not s:
        return ""
    s = _strip_wrappers(s)
    if "%" in s:
        # errors="strict" so a mangled escape RAISES rather than silently mojibaking two
        # distinct values onto one form. `unquote` leaves a non-escape '%' alone, so
        # "100% cotton" and "%APPDATA%/x" pass through untouched.
        try:
            decoded = unquote(s, errors="strict")
            if decoded and decoded != s:
                s = _strip_wrappers(decoded.translate(_ZERO_WIDTH).strip())
        except Exception:
            pass
    if "/" in s or "\\" in s:
        s = s.replace("\\", "/")
        scheme, sep, rest = s.partition("://")
        if sep:
            s = scheme + sep + _DUP_SLASH.sub("/", rest)
        else:
            lead = "//" if s.startswith("//") else ""
            s = lead + _DUP_SLASH.sub("/", s[len(lead):])
    s = _SEP_RUN.sub(" ", s).strip()
    return s.casefold()


def _tainted_fact_texts(bc) -> List[str]:
    """The CANONICAL text of each ``content_derived`` user_fact the PROMPTS could have
    shown the model — the most-recent :data:`_PROMPT_FACT_WINDOW`, not the whole vault.

    Read from ``bc.situation["profile"]["user_facts"]``, which
    ``situational_inventory.build_profile`` already threads — so this needs no new
    plumbing and no vault access. Taint is decided by :func:`_fact_origin`, the SAME
    reader source #4 uses, so the legacy unstamped ``auto_extract`` corpus is covered by
    that function's clamp here too (one taint definition, not two).

    THE WINDOW IS THE COST BOUND, and it replaces a claim this docstring used to make.
    It previously described the whole-profile superset as "deliberate and fail-safe —
    matching against more tainted values can only add confirms, never remove one". True
    of the SECURITY direction and silent on cost, which is where the defect was: the
    corpus grew without limit, and measured on a 40-value panel of ordinary parameter
    values against ordinary English facts, the clamp rate ran 0% / 7.5% / 22.5% / 35% /
    55% at 0 / 1 / 5 / 10 / 20 facts. Because ``_UNTRUSTED_ABSENT_SOURCES`` includes
    ``auto_extract``, that corpus is non-empty in any install where extraction ever ran,
    and the growth was SELF-AMPLIFYING: over-clamp → more asks → §5.9 promotes more
    answers as ``content_derived`` → bigger corpus → more over-clamp. The window breaks
    that loop — the cost is now bounded by a constant, not by vault age.

    The WINDOW is applied BEFORE the taint filter, not after: the renderers cap the
    most-recent N of ALL non-superseded facts and only some of those are tainted, so
    slicing first reproduces exactly what a prompt carried. Taking the last N TAINTED
    facts instead would silently re-widen the corpus past the prompt.

    Defensive: a rehydrated profile can hold anything, so every step is guarded and any
    failure yields ``[]`` (⇒ no clamp ⇒ prior behavior), never an exception that would
    propagate to the per-leaf except and degrade the leaf to a spurious gap."""
    out: List[str] = []
    try:
        profile = _get(bc.situation, "profile")
        if not isinstance(profile, dict):
            return out
        facts = profile.get("user_facts")
        if not isinstance(facts, list):
            return out
        for fact in facts[-_PROMPT_FACT_WINDOW:]:
            try:
                if _fact_origin(fact) != _CONTENT_DERIVED:
                    continue
                txt = _get(fact, "fact")
                if isinstance(txt, str) and txt:
                    canon = _canonical_taint_form(txt)
                    if canon:
                        out.append(canon)
            except Exception:
                continue                          # one bad row never voids the corpus
    except Exception:
        return []
    return out


def _appears_as_token(haystack: str, needle: str) -> bool:
    """True if ``needle`` occurs in ``haystack`` delimited by non-alphanumerics.

    Token-delimited rather than raw ``in``: a raw substring test lets a short value
    match inside a longer word ("acct" inside "accounting"), which manufactures
    over-asks. Implemented by inspecting the adjacent characters rather than with a
    regex ``\\b`` — the values that matter most here (an email, a POSIX path, a
    hyphenated account id) START or END with a non-word character, where ``\\b`` has
    the opposite meaning and would silently fail to match."""
    n = len(needle)
    if not n:
        return False
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return False
        j = i + n
        if ((i == 0 or not haystack[i - 1].isalnum())
                and (j >= len(haystack) or not haystack[j].isalnum())):
            return True
        start = i + 1


def _provided_value_is_content_seeded(bc, val) -> bool:
    """True when a provided param's VALUE appears as a token inside a ``content_derived``
    user_fact — i.e. the model most plausibly lifted it out of tainted content that this
    system itself placed in its context.

    Containment, not equality: a user_fact is a SENTENCE ("account_id is acct-42") while
    the model emits the extracted VALUE ("acct-42"). An equality test would miss the
    realistic shape entirely.

    Both sides go through :func:`_canonical_taint_form` first, so a value the model
    reshaped — requoted, re-separated, URL-encoded, punctuated, zero-width-padded — still
    matches the stored form. The match itself stays TOKEN-DELIMITED; canonicalisation
    changes which FORMS compare equal, never the boundary rule.

    ON "FALSE POSITIVES". A value that the operator really did supply, which also happens
    to appear as a token inside a tainted fact, clamps to an extra one-click confirm.
    That is the correct reading, not a bug: the value DID occur in untrusted content, and
    the binder cannot distinguish "operator typed it" from "model copied it" — the two
    are byte-identical by the time they reach ``provided_params``. Erring toward the
    confirm is the IMPL-5 fail-untrusted direction.

    THE COST, MEASURED — this paragraph previously claimed "cost is bounded and usually
    zero ... behavior is byte-identical to before the clamp", which was the opposite of
    what the code did. The zero case is real but narrow: it holds only when NO
    ``content_derived`` fact is in the window, and since ``_UNTRUSTED_ABSENT_SOURCES``
    clamps the legacy unstamped ``auto_extract`` corpus, that is false in any install
    where fact extraction ever ran. On the ordinary 40-value panel described in
    :func:`_tainted_fact_texts` the clamp rate reached 55% at 20 facts and kept climbing
    with vault age. It is bounded NOW, by :data:`_PROMPT_FACT_WINDOW` — the cost is a
    function of the window, not of how long the vault has existed. The realized count is
    recorded (:func:`replay_metrics.record_taint_clamp`) and surfaced in the avoidable-ask
    report, so what remains is visible rather than silent.

    A NOTE ON FRAGMENTS. A hyphen/underscore is a token delimiter, so a PREFIX or SUFFIX
    of a stored compound value ("acct-99" against a stored "acct-99-attacker") matches.
    That is intended and pre-dates this change: a fragment the model lifted out of
    tainted content is itself content-derived. It is a genuine cost contributor, which is
    why it is named here rather than left to be rediscovered."""
    s = _canonical_taint_form(val)
    if len(s) < _MIN_TAINT_MATCH_LEN:
        return False
    corpus = _tainted_fact_texts(bc)
    for txt in corpus:
        if _appears_as_token(txt, s):
            _record_clamp(bc, len(corpus))
            return True
    return False


def _record_clamp(bc, corpus_size: int) -> None:
    """Record one realized clamp for the ask report (observability only).

    Wrapped and fully swallowed: this is a measurement side-effect on a SAFETY path, and
    a recording failure must never change the bind decision or raise into the per-leaf
    handler (which would degrade the leaf to a spurious gap — turning a metrics hiccup
    into a behavior change). Records no value; see ``replay_metrics.record_taint_clamp``."""
    try:
        vault = getattr(bc, "vault", None)
        if vault is None:
            return
        from systemu.runtime.replay_metrics import record_taint_clamp
        record_taint_clamp(vault, corpus_size=corpus_size,
                           tool_name=str(getattr(bc, "tool_name", "") or ""))
    except Exception:
        pass


def _key_token_overlap(kl: str, other: str) -> bool:
    """True if the two identifiers share a meaningful token (split on non-alnum),
    ignoring trivial connectors. Lets ``account_id`` match a tag ``account_id`` and a
    ``default_output_dir`` spine field match an ``output_dir`` leaf."""
    import re
    a = {t for t in re.split(r"[^a-z0-9]+", kl) if len(t) > 2}
    b = {t for t in re.split(r"[^a-z0-9]+", other) if len(t) > 2}
    return bool(a & b)


# ── source #5: the schema's own default / const / enum[0] ─────────────────────
def _bind_schema_default(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple[str, str, str, float]]:
    """The leaf's own default / const / enum[0] — systemu's authored catalog value."""
    if not isinstance(spec, dict):
        return None
    if "default" in spec and spec.get("default") is not None:
        return (f"schema_default:{key}", "schema", _SYSTEMU, 1.0, spec.get("default"))
    if "const" in spec:
        return (f"schema_const:{key}", "schema", _SYSTEMU, 1.0, spec.get("const"))
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        return (f"schema_enum0:{key}", "schema", _SYSTEMU, 1.0, enum[0])
    return None


# the ordered bind pipeline (spec §5.3). R-A12c: _bind_provided_params is FIRST — a
# value the current tool-call already supplied wins over inventory / schema default.
#
# BIND TUPLE CONTRACT: ``(bound_value_ref, source, value_origin, confidence,
# resolved_value)``.
#   * ``bound_value_ref`` is a namespaced HANDLE naming the SOURCE (``file:<path>``,
#     ``profile:<field>``, …) — it is NOT the value and can never equal an answer.
#   * ``resolved_value`` (R-A16 §5.9) is the VALUE the handle stands for — what the
#     operator would have to type to confirm this bind. It is consumed ONLY to compute
#     a keyed, non-reversible digest (``Requirement.bound_value_digest``) and is never
#     stored, logged or persisted. A source that binds an IDENTIFIER rather than an
#     extractable value (``credential:`` — a secret; ``profile_fact:`` — a sentence)
#     returns ``None`` here, which degrades the §5.9 signal to candidate-only rather
#     than manufacturing a guaranteed mismatch.
# A legacy 4-tuple is tolerated by ``_bind_one_leaf`` (⇒ no digest).
_SOURCES = (_bind_provided_params, _bind_filehandle, _bind_run_context,
            _bind_inventory_entry, _bind_profile, _bind_schema_default)

# Sources that can only ever produce a FILE PATH, and are therefore consulted ONLY for
# a leaf the path oracle typed as a path (``_bind_one_leaf``'s ``is_path``).
#
# WHY. ``_bind_filehandle`` scores the OBJECTIVE'S GOAL TEXT (``bc.reference_text``),
# not the leaf key — and ``reference_resolver`` folds the key in with a UNION
# (``_tokens(text) | _tokens(key or "")``), so the key can only WIDEN a match, never
# constrain one. Ungated, one goal naming a granted-root file resolved for EVERY leaf
# in the schema: measured over the repo's harvested tool schemas, 83/142 requirements
# came back pre-filled with a path and 50 of those (35.2%, 30 distinct keys) were on
# leaves that cannot hold one — ``password``, ``query``, ``count``, ``verbose``,
# ``process_id``. The operator saw a confident wrong default in a box that wanted a
# secret. This was inert until source #1 was threaded its vault and went live, so the
# exposure is as new as that fix, not a legacy shape.
#
# NOT the resolver's scoring. Requiring a path-SHAPED value there was investigated and
# rejected: the file genuinely matches the goal text, so the match is not the error —
# consulting a file source for a non-path leaf is.
#
# COST. Pre-fill now rides on the oracle's recall. That is affordable because
# ``looks_like_path`` is deliberately high-recall (it unions ``format``,
# ``contentMediaType``, key patterns AND the description), and the only leaf it never
# sees is one with NO ``type`` — a union like ``["string", "null"]`` still resolves
# through ``_first_type``. Such a leaf degrades to an honest ``missing`` ask, which
# beats a confidently wrong pre-fill.
#
# ``_bind_run_context`` (source #2) IS GATED TOO, and the argument is stronger than
# symmetry with source #1.
#
# THE SHAPE. It binds the first entry of ``ctx.files_produced`` — a FILE PATH — into
# whatever leaf it is asked about, at 0.5, with no key test whatsoever. So any run that
# produced one file pre-filled EVERY leaf. Measured over the same harvested-tool corpus
# used for source #1: 29 of 104 requirements (27.9%) came back pre-filled by this source
# and 29 of those 29 — 100% — were on leaves the oracle does not type as a path
# (``password``, ``verbose``, ``query``, ``command``, ``pid``, ``lat``, ``lon``, ``url``,
# ``message``, ``date_str`` …, 15 distinct keys). Source #1 runs FIRST and wins the
# genuine path leaves, so before this gate source #2's entire contribution to that corpus
# was wrong pre-fills. After the gate: 0.
#
# WHY IT IS WORSE THAN A COSMETIC WRONG DEFAULT. ``_SOURCES`` orders this source BEFORE
# inventory (#3), profile (#4) and schema-default (#5). A junk 0.5 ``content_derived``
# path bind therefore MASKS the source-#4 profile bind that would otherwise have fired —
# and because ``content_derived`` never silent-binds, a G-LEARN-promoted ``operator``
# fact stops paying off: the operator is re-asked, with a wrong path pre-filled in the
# box. That structurally defeats the promotion payoff. It also stuffs the R-A13.5
# avoidable-ask corpus with guaranteed-mismatch candidates recorded as "the binder was
# wrong", poisoning the very signal G-LEARN learns from.
#
# THE TAINT CLAMP IS UNCHANGED. The bind this gate still lets through (a path leaf) is
# ``content_derived`` exactly as before — ``tests/test_ra11a_source1_liveness.py`` pins
# both IMPL-5 directions and must stay green.
_PATH_ONLY_SOURCES = frozenset({_bind_filehandle, _bind_run_context})


# ── small tolerant readers (TableItem-or-dict, list-or-missing) ──────────────
def _get(obj, field):
    """Read a field off a pydantic model OR a plain dict, tolerantly. None on miss."""
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _situation_list(situation, field) -> list:
    """A list slice off the situation dict/model; [] on anything unexpected."""
    v = _get(situation, field)
    return v if isinstance(v, list) else []


# ── the leaf visitor: bind one leaf → one Requirement ────────────────────────
def _classify_missing_kind(is_path: bool, key: str) -> str:
    """Kind for an UNBINDABLE required leaf: a path leaf is an ``input``; an
    account/identity/repo-style leaf is a ``decision``; otherwise a ``capability``
    gap (no candidate path can do it — the planner must find/forge a tool)."""
    if is_path:
        return "input"
    kl = (key or "").lower()
    if any(w in kl for w in ("account", "identity", "repo", "which", "choice",
                             "target", "branch", "channel", "project", "as_user")):
        return "decision"
    return "capability"


def _schema_path_from(path: tuple) -> str:
    """Build the JSON-pointer-ish ``schema_path`` string from ``_walk``'s ``path``
    accumulator tuple. ``_walk`` appends the property NAME on object descent and the
    literal ``"[]"`` on array/tuple descent — the exact segments the old ``_walk_bind``
    joined with ``/`` (so an array-item marker stays ``.../[]``)."""
    return "/".join(str(seg) for seg in path)


# ── the schema walk (DRIVES the real fixture_synth._walk via leaf_fn — AC3) ──
def _diff_schema(bc: _BindCtx, root: dict) -> None:
    """Diff every declared leaf of ``root`` into ``bc.reqs`` by DRIVING the real
    ``fixture_synth._walk`` (Part B / AC3 / MEDIUM-2). The binder no longer mirrors the
    traversal — it passes a bind ``leaf_fn`` into the ONE walk synth already uses, so
    ``prefixItems`` (2020-12 tuple arrays), ``additionalProperties``, ``$ref`` cycles
    and anyOf/allOf are handled by construction (identically to synth). The binder's
    ``leaf_fn`` records a Requirement per leaf and NEVER materializes a fixture."""
    from systemu.pipelines import fixture_synth

    # A throwaway synth ctx: _walk threads it for path-leaf materialization, but the
    # bind leaf_fn ignores it (records a Requirement instead of writing a file). We
    # give it a sandbox under the system temp so the rare path a default _synth_leaf
    # path never runs; the bind leaf_fn is what _walk calls.
    import tempfile
    from pathlib import Path
    synth_ctx = fixture_synth._Ctx(
        tool_name=bc.tool_name,
        sandbox=Path(tempfile.gettempdir()),
    )

    def leaf_fn(node, *, key, required, kind, ext, ctx, path,
                schema_value=fixture_synth._SENTINEL, schema_value_kind=None):
        _bind_one_leaf(bc, node=node, key=key, required=required, kind=kind,
                       path=path, schema_value=schema_value,
                       schema_value_kind=schema_value_kind)
        return None  # ignored — the binder reads bc.reqs

    try:
        fixture_synth._walk(root, key="", required=False, root=root, ctx=synth_ctx,
                            depth=0, seen=frozenset(), leaf_fn=leaf_fn, path=())
    except Exception:
        logger.debug("[binder] _walk drive failed; partial diff kept", exc_info=True)


def _bind_one_leaf(bc: _BindCtx, *, node, key, required, kind, path,
                   schema_value, schema_value_kind) -> None:
    """Record (at most) one Requirement for a single terminal leaf. Guarded so a bad
    leaf degrades to a best-effort gap and NEVER empties the whole objective."""
    from systemu.pipelines.fixture_synth import _SENTINEL
    try:
        schema_path = _schema_path_from(path)
        is_path = bool(kind)                       # the oracle classified it a path leaf
        spec = node if isinstance(node, dict) else {}

        # A const/enum/default leaf: bind from source #5 (schema) — systemu's own
        # authored catalog value. It is a ``have`` (systemu_authored is trusted).
        # R-A12c: UNLESS the current tool-call already supplied this param — a provided
        # value wins over a schema default. _walk routes a default leaf straight through
        # leaf_fn with schema_value set (never consulting _SOURCES), so we must defer to
        # the provided source HERE; when present, fall through to the _SOURCES loop where
        # _bind_provided_params (first) binds it.
        if schema_value is not _SENTINEL and _bind_provided_params(bc, key, spec, path) is None:
            _emit_requirement(bc, kind=("input" if is_path else "decision"),
                              schema_path=schema_path, state="have", source="schema",
                              value_origin=_SYSTEMU,
                              bound_value_ref=f"schema_{schema_value_kind or 'value'}:{key}",
                              confidence=1.0, resolved_value=schema_value,
                              rationale=f"schema {schema_value_kind or 'value'} (systemu_authored)")
            return

        # Otherwise try the 5 sources in order (first hit wins).
        bound = None
        for src in _SOURCES:
            # a file-valued source may only fill a leaf the oracle typed as a path
            if src in _PATH_ONLY_SOURCES and not is_path:
                continue
            try:
                if src is _bind_provided_params:
                    bound = src(bc, key, spec, path)
                else:
                    bound = src(bc, key, spec)
            except Exception:
                logger.debug("[binder] source %s raised on %s",
                             getattr(src, "__name__", src), schema_path, exc_info=True)
                bound = None
            if bound is not None:
                break

        if bound is None:
            if not required:
                return                             # optional + unbindable → not a gap
            kind_missing = _classify_missing_kind(is_path, key)
            _emit_requirement(bc, kind=kind_missing, schema_path=schema_path,
                              state="missing", source="schema", value_origin=None,
                              bound_value_ref=None, confidence=0.0,
                              rationale="no source bound this required leaf (schema-diff gap)")
            return

        # 5-tuple contract (see _SOURCES); a legacy 4-tuple binds with NO value digest.
        # R-B5 / T5: an OPTIONAL 6th element carries the TableItem id behind the bind
        # (attribution only — never trust). Absent ⇒ not table-attributed.
        bound_ref, source, value_origin, confidence = bound[:4]
        resolved_value = bound[4] if len(bound) > 4 else None
        table_item_id = bound[5] if len(bound) > 5 else None
        if is_path:
            slot_kind = "input"
        elif source == "situation" and str(bound_ref).startswith("credential:"):
            slot_kind = "credential"
        else:
            slot_kind = "decision"

        state = "have" if confidence >= bc.t_high else "resolvable"
        _emit_requirement(bc, kind=slot_kind, schema_path=schema_path, state=state,
                          source=source, value_origin=value_origin,
                          bound_value_ref=bound_ref, confidence=float(confidence),
                          resolved_value=resolved_value, table_item_id=table_item_id,
                          rationale=("content_derived → one-click operator confirm "
                                     "(never silent-bound)"
                                     if _coerce_origin(value_origin) == _CONTENT_DERIVED
                                     else f"bound from {source} (conf {confidence:.2f})"))
    except Exception:
        # Fail-safe: a single bad leaf degrades to a best-effort missing gap — it
        # NEVER propagates to empty the whole objective's diff (Finding 2 / #2).
        logger.debug("[binder] leaf %s degraded to best-effort gap", key, exc_info=True)
        try:
            _emit_requirement(bc, kind="decision", schema_path=_schema_path_from(path),
                              state="missing", source="schema", value_origin=None,
                              bound_value_ref=None, confidence=0.0,
                              rationale="leaf bind raised → best-effort gap (fail-safe)")
        except Exception:
            logger.debug("[binder] even the fail-safe gap failed for %s", key, exc_info=True)


def _emit_requirement(bc: _BindCtx, *, kind, schema_path, state, source, value_origin,
                      bound_value_ref, confidence, rationale,
                      resolved_value=None, table_item_id=None) -> None:
    """Construct + append one Requirement, CLAMPING ``value_origin`` to a canonical
    taint value first (Finding 2 / #2 fail-safe): a non-canonical origin can never
    raise a ValidationError that propagates to the outer except and empties the whole
    objective. ``None`` (a genuine no-value gap) is preserved as-is.

    ``resolved_value`` is digested (never stored) into ``bound_value_digest``."""
    from systemu.core.models import Requirement
    vo = None if value_origin is None else _coerce_origin(value_origin)
    bc.reqs.append(Requirement(
        kind=kind, schema_path=schema_path, state=state, source=source,
        value_origin=vo, bound_value_ref=bound_value_ref, confidence=float(confidence),
        bound_value_digest=_value_digest(bc, kind=kind, schema_path=schema_path,
                                         value=resolved_value),
        bound_value_canon_digest=_value_digest(bc, kind=kind, schema_path=schema_path,
                                               value=resolved_value, canonical=True),
        # R-B5 / T5 attribution. Coerced to a plain str-or-None here so a malformed
        # 6th tuple element can never raise a ValidationError that the outer except
        # would turn into an emptied objective diff (the Finding-2 fail-safe).
        table_item_id=(table_item_id if isinstance(table_item_id, str) and table_item_id
                       else None),
        rationale=rationale,
    ))


def _value_digest(bc: _BindCtx, *, kind, schema_path, value,
                  canonical: bool = False) -> Optional[str]:
    """R-A16 §5.9 — the KEYED, non-reversible digest of a bind's RESOLVED VALUE.

    THE ONLY thing the value is used for. It is never stored, logged or persisted; the
    digest is what crosses the suspend inside a card spec, so the answer-side can ask
    "did the operator confirm what the binder held?" — a question ``bound_value_ref``
    (a source HANDLE) cannot answer.

    SECRET BOUNDARY AT THE PRODUCER: a credential-kind or secret-mode leaf gets NO
    digest, using the codebase's canonical secret marker (via ``replay_metrics``, which
    delegates to ``elicitation.is_secret_field``) rather than a bespoke rule. So no
    secret-derived datum is even computed here, let alone carried.

    THE VAULT COMES FROM THE CALL SITE, NOT FROM ``ctx``. It was originally read as
    ``ctx.vault`` — an attribute the real ``ExecutionContext`` has never had — so the
    digest evaluated to ``None`` on every production bind and this field shipped inert:
    ``resolvable_confirmed`` could not fire, and every bound ask recorded as
    ``missing_answered`` ("the binder had nothing"), which was false. The ctx fallback
    below is retained only for callers that pass a ctx-shaped stand-in; production
    threads ``vault=`` explicitly. Adding ``vault`` to ``ExecutionContext`` is NOT the
    repair — that object is serialized and snapshotted.

    Observability-only and TOTALLY defensive: an unavailable vault, an unkeyable
    value, any failure at all ⇒ ``None`` (the §5.9 row degrades to candidate-only).
    A metric must never be able to perturb a bind."""
    if value is None:
        return None
    try:
        from systemu.runtime import replay_metrics as _rm
        if str(kind or "").lower() == "credential":
            return None
        if _rm._is_secret_path(str(schema_path or ""), str(kind or "")):
            return None
        vault = getattr(bc, "vault", None) or getattr(getattr(bc, "ctx", None),
                                                      "vault", None)
        if vault is None:
            return None
        # ``canonical=True`` stamps the FORM-INSENSITIVE twin (R-A16 F2). BOTH pass
        # through the SAME credential/secret refusals above — deliberately one
        # function, so a future guard added here can never protect one digest and
        # silently miss the other.
        if canonical:
            return _rm.canonical_value_ref(value, vault)
        return _rm.value_ref(value, vault)
    except Exception:
        logger.debug("[binder] value digest skipped for %s", schema_path, exc_info=True)
        return None


def _s4_stamp_mode() -> str:
    """R-A13a §5.8 — the 3-state S4 stamp obligation (NEVER operator-facing; Stage 3
    removes it). Read from SYSTEMU_S4_STAMP:
      'off' (default) — never WRITE requires_external_verification (Stage 1);
      'shadow'        — compute + record to a shadow attr, do NOT write the live field
                        (Stage 2, feeds the park-surface report);
      'enforce'       — write the live field (Stage 3)."""
    import os
    v = str(os.environ.get("SYSTEMU_S4_STAMP", "off") or "off").strip().lower()
    return v if v in {"off", "shadow", "enforce"} else "off"


# DEC-24: the POSITIVE set of effects that demand external ground-truth. UNKNOWN + an
# empty tag list still stamp (BLOCKER-3). NOT effect_tags.HIGH_SEVERITY (that has
# LOCAL_DELETE and drops OAUTH_CALL).
def _stamp_effect_values() -> frozenset:
    from systemu.runtime.effect_tags import EffectTag
    return frozenset({EffectTag.NET_MUTATE.value, EffectTag.MONEY_MOVE.value,
                      EffectTag.SEND_MESSAGE.value, EffectTag.OAUTH_CALL.value})


# ── EffectTag → requires_external_verification (DEC-24, §5.8) ─────────────────
def _requires_external_verification(capability) -> bool:
    """DEC-24: an effect in the positive _STAMP_EFFECTS set — or an UNKNOWN / empty tag
    list (UNKNOWN-until-classified, BLOCKER-3) — demands external verification ⇒ True; all
    other classes (local_read/write/delete, shell_exec, net_read) ⇒ False.

    S4 Step 0: a None/absent capability is NOT a classification (the pre-loop producer
    passes None) ⇒ False. NOTE: this is the VALUE only; whether it is WRITTEN onto the
    objective is governed independently by _s4_stamp_mode() (off/shadow/enforce)."""
    if capability is None:
        return False
    return _effect_tags_are_dangerous(capability)


def _effect_tags_are_dangerous(capability) -> bool:
    """The DEC-24 EffectTag classification (kept SEPARATE from the write-gate so the two
    concerns stay orthogonal): any tag in _STAMP_EFFECTS ⇒ True; UNKNOWN ⇒ True; an empty /
    unreadable tag list ⇒ True (UNKNOWN-until-classified); otherwise False. Unavailable
    classifier ⇒ fail-safe True."""
    try:
        from systemu.runtime.effect_tags import coerce, EffectTag
    except Exception:
        return True
    tags = _get(capability, "effect_tags") or []
    if not isinstance(tags, list) or not tags:
        return True                              # [] = UNKNOWN-until-classified ⇒ stamp
    stamp = _stamp_effect_values()
    for raw in tags:
        v = coerce(raw)
        v = getattr(v, "value", v)               # coerce → str value (defensive)
        if v == EffectTag.UNKNOWN.value or v in stamp:
            return True
    return False


# ── schema resolution (v1 flat Tool.parameters_schema OR v2/MCP registry) ────
def _capability_schema(capability) -> dict:
    """The capability's parameters schema. Accepts a Tool (``.parameters_schema``), a
    v2/MCP registry entry (``.schema``), or a raw schema dict. {} on anything else."""
    if capability is None:
        return {}
    if isinstance(capability, dict):
        return capability
    sch = _get(capability, "parameters_schema")
    if isinstance(sch, dict) and sch:
        return sch
    sch = _get(capability, "schema")
    if isinstance(sch, dict) and sch:
        return sch
    return {}


def _normalized_root(schema: dict) -> dict:
    """Wrap a flat ``{param: spec}`` map into a JSON-Schema object node so the walk
    is uniform. A leaf with no ``default`` is REQUIRED (the DRIFT rule: a flat
    Tool.parameters_schema has no ``required[]``, so required-ness = missing-default).
    An already-wrapped/JSON-Schema node is passed through untouched (its own
    ``required[]`` governs)."""
    if not isinstance(schema, dict) or not schema:
        return {}
    if ("properties" in schema or schema.get("type") == "object" or "$ref" in schema
            or any(k in schema for k in ("anyOf", "allOf", "oneOf"))):
        return schema                            # already a schema node
    # flat {name: spec} → wrap. required = every leaf WITHOUT a default (the drift rule).
    try:
        from systemu.core.schema_utils import normalize_parameters_schema
        norm = normalize_parameters_schema(schema)
    except Exception:
        norm = schema
    props: Dict[str, Any] = {n: (s if isinstance(s, dict) else {})
                             for n, s in (norm or {}).items()}
    required = [n for n, s in props.items()
                if not (isinstance(s, dict) and ("default" in s and s.get("default") is not None))]
    root: Dict[str, Any] = {"type": "object", "properties": props}
    root["required"] = required                  # explicit (even if empty)
    return root


# ── the public entry (§5.3) ──────────────────────────────────────────────────
def compute_requirements(objective, capability, situation, ctx, provided_params=None,
                         vault=None) -> List[Any]:
    """Compute the per-objective ``list[Requirement]`` by BIND-mode schema-diff.

    Reads ``capability``'s schema, diffs every REQUIRED leaf against the ordered sources,
    applies the T_high + content_derived gate (IMPL-5), and stamps
    ``requires_external_verification`` on ``objective`` from the EffectTag (AC4/§5.8).
    ``provided_params`` (R-A12c) are the CURRENT tool-call's already-supplied params —
    a required leaf present there binds (source #0) instead of generating a spurious ask.
    ``vault`` (R-A16 §5.9) does TWO jobs and is NOT optional in practice: it keys the
    per-bind ``bound_value_digest``, AND it builds the GrantedRootsStore that source #1
    (granted-root FileHandle) re-gates through. Omit it and binds carry no digest AND
    source #1 cannot fire at all — the resolver fail-closes on a None store, so a
    required path leaf degrades to a ``missing`` gap. The ``ctx`` carries neither, so
    the thread from the call site is the ONLY route.
    Defensive: a broken schema / missing situation yields [] or a best-effort list,
    never raises."""
    try:
        # AC4: stamp dangerous-until-proven onto the objective (best-effort — a stamp
        # failure never blocks the diff). S4 Step 0: only stamp when a REAL capability
        # is present — a None/absent capability is not a classification (the pre-loop
        # producer passes None; stamping off it would flip EVERY objective to True).
        # _requires_external_verification(None) also returns False (defence-in-depth).
        try:
            if (objective is not None and capability is not None
                    and hasattr(objective, "requires_external_verification")):
                _mode = _s4_stamp_mode()
                if _mode != "off":
                    _stamp = _requires_external_verification(capability)
                    if _mode == "enforce":
                        objective.requires_external_verification = _stamp
                    else:  # 'shadow' — record without writing the live gate-read field
                        logger.debug("[binder S4-SHADOW] obj=%s would-stamp "
                                     "requires_external_verification=%s",
                                     _get(objective, "id"), _stamp)
                        try:
                            objective.__dict__["_s4_stamp_shadow"] = _stamp
                        except Exception:
                            pass
        except Exception:
            logger.debug("[binder] could not stamp requires_external_verification", exc_info=True)

        schema = _capability_schema(capability)
        root = _normalized_root(schema)
        if not root:
            return []

        # resolve the granted-roots store (source #1 re-gate). Prefer one injected on
        # ctx (a TEST-ONLY seam — nothing in systemu/ ever assigns it); else build one
        # from the EXPLICITLY THREADED vault; else None (source #1 no-ops).
        granted = getattr(ctx, "_granted_roots", None)
        if granted is None:
            granted = _granted_store(vault, ctx)

        tool_name = str(_get(capability, "name") or "")
        sit = situation if isinstance(situation, dict) else {}
        _goal = str(_get(objective, "goal") or "") if objective is not None else ""
        _crit = str(_get(objective, "success_criteria") or "") if objective is not None else ""
        pp = provided_params if isinstance(provided_params, dict) else None
        bc = _BindCtx(situation=sit, ctx=ctx, granted=granted, tool_name=tool_name,
                      reference_text=(_goal + " " + _crit).strip(),
                      provided_params=pp, vault=vault)
        _diff_schema(bc, root)
        return bc.reqs
    except Exception:
        logger.debug("[binder] compute_requirements failed; returning []", exc_info=True)
        return []


def _granted_store(vault, ctx=None):
    """Best-effort GrantedRootsStore for source #1's confinement re-gate; None on any miss.

    THE VAULT MUST BE THREADED FROM THE CALL SITE. This was previously
    ``_granted_from_ctx(ctx)``, reading ``ctx.vault`` — an attribute the real
    ``ExecutionContext`` has never had — so it returned ``None`` on EVERY production
    bind. ``reference_resolver`` FAIL-CLOSES on a ``None`` store (it drops every
    candidate rather than skipping the confinement gate), which meant source #1 never
    once fired in a real run: a required path leaf fell through to a bare ``missing``
    gap and the operator was asked to hand-type a path to a file sitting in a root they
    had already granted. Identical root cause, and identical repair, to ``_value_digest``.

    Adding ``vault`` to ``ExecutionContext`` is NOT the fix — that object is serialized
    and snapshotted, so a live handle on it is a snapshot-shape hazard. The ``ctx``
    fallback below is kept only as an explicit injection seam (it is ``None`` in
    production, and ``tests/test_ra11a_source1_liveness.py`` pins that)."""
    v = vault if vault is not None else getattr(ctx, "vault", None)
    if v is None:
        return None
    try:
        from systemu.runtime.granted_roots import GrantedRootsStore
        return GrantedRootsStore(base_dir=v.root)
    except Exception:
        return None


# ── ask_bundle gate + the aggregating report (§5.3 / §5.6) ───────────────────
def _needs_ask(req) -> bool:
    """A requirement is surfaced in the ask_bundle when it is NOT a silent 'have':
    a below-T_high ``resolvable``, a ``missing`` gap, OR a content_derived bind (IMPL-5
    — an untrusted inventory/file value is NEVER silent-bound, even at state='have';
    it becomes a one-click operator confirm). Only a ``have`` whose value_origin is a
    TRUSTED axis (operator / systemu_authored) binds silently."""
    if _get(req, "state") != "have":
        return True
    return _get(req, "value_origin") == _CONTENT_DERIVED


def build_requirement_report(objectives, capability, situation, ctx,
                             provided_params=None, vault=None):
    """Aggregate ``compute_requirements`` across objectives into a RequirementReport:
    ``per_objective`` + a DEDUPED ``ask_bundle`` (every non-'have' requirement). The
    per-objective core is ``compute_requirements``; this is the §5.6 pull/scope-card
    feed. ``provided_params`` (R-A12c) threads the tool-call's already-supplied params
    through to the per-objective diff. ``vault`` (R-A16 §5.9) keys each bind's
    ``bound_value_digest`` AND supplies source #1's GrantedRootsStore — it MUST be
    threaded from the call site, because the ``ExecutionContext`` passed as ``ctx``
    carries neither a vault nor a granted-roots store of its own. Omitting it leaves
    source #1 dormant, not merely un-digested.
    Defensive: never raises."""
    from systemu.core.models import RequirementReport

    per: Dict[int, List[Any]] = {}
    ask: List[Any] = []
    seen_keys = set()
    for obj in objectives or []:
        try:
            reqs = compute_requirements(obj, capability, situation, ctx,
                                        provided_params=provided_params, vault=vault)
        except Exception:
            reqs = []
        oid = _get(obj, "id")
        per[oid] = reqs
        for r in reqs:
            if not _needs_ask(r):
                continue
            key = (_get(r, "schema_path"), _get(r, "kind"), _get(r, "state"),
                   _get(r, "value_origin"), _get(r, "bound_value_ref"))
            if key in seen_keys:
                continue                          # dedupe identical asks across objectives
            # NOTE: bound_value_ref is IN the key — two objectives binding the same
            # schema_path to DIFFERENT values (distinct bound_value_ref) are DISTINCT
            # asks; deduping them (as the pre-fix key did) silently dropped the second
            # binding from the operator's one-click bundle.
            seen_keys.add(key)
            ask.append(r)
    try:
        return RequirementReport(per_objective=per, ask_bundle=ask)
    except Exception:
        logger.debug("[binder] RequirementReport assembly failed", exc_info=True)
        return RequirementReport()
