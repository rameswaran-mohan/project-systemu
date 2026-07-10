"""R-A10 step B3 — the requirement binder (spec UNIFIED-v2 §5.3, BIND-mode).

For each objective, read the CHOSEN capability's schema and, for every REQUIRED
leaf, attempt to BIND it from the 5 spec-ordered sources. "What's missing" is then
a schema-DIFF (a leaf no source could bind), never an LLM guess. This is the
open-world reframe: the planner reasons over a concrete gap list, not a hunch.

The 5 bind sources, tried IN ORDER (first hit wins) — §5.3:
  1. FileHandle       — a granted-root salient file (re-gated through
                        GrantedRootsStore.is_within_granted). Origin content_derived.
  2. run-context      — a prior objective's produced file / run state (best-effort
                        from ctx.files_produced; a typed objective_outputs store is
                        deferred to R-A11). Origin content_derived.
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

The T_high gate (net-new here — §5.3 leaves the threshold to the binder):
  * bound, confidence >= T_HIGH, and NOT content_derived  → state="have"   (silent)
  * bound, below T_HIGH, OR content_derived               → state="resolvable" + ask_bundle
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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    oracle. The source *accessors* live on the bind functions, not here — this only
    holds the raw material + the output list."""
    situation: dict
    ctx: Any
    granted: Any
    tool_name: str = ""
    reqs: List[Any] = field(default_factory=list)
    t_high: float = T_HIGH
    reference_text: str = ""          # R-A11a §5.4: the objective goal text the resolver reads


# ── source #1: a granted-root salient FileHandle ─────────────────────────────
def _bind_filehandle(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple[str, str, str, float]]:
    """R-A11a §5.4: resolve a path leaf to a granted-root file by SCORING the objective's
    reference text against the situation's salient handles (was: blind first-salient @0.9).
    Preserves the 4-tuple contract and the IMPL-5 clamp — a resolved FILE is inherently
    content_derived, so it NEVER silent-binds (the _needs_ask gate forces the confirm)."""
    from systemu.runtime.reference_resolver import resolve_reference
    try:
        verdict = resolve_reference(bc.reference_text, situation=bc.situation,
                                    granted=bc.granted, key=key)
    except Exception:
        logger.debug("[binder] reference_resolver raised; leaf falls through", exc_info=True)
        return None
    if verdict.state != "resolvable" or not verdict.referent:
        return None                                   # → falls through → input/missing ask-for-path
    # CLAMP to content_derived regardless of score (IMPL-5 fail-untrusted).
    return (f"file:{verdict.referent}", "situation", _CONTENT_DERIVED, float(verdict.confidence))


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
            return (f"run_context:{p}", "run_context", _CONTENT_DERIVED, 0.5)
    return None


# ── source #3: a SituationReport inventory ENTRY (services/caps/roots/creds) ──
def _bind_inventory_entry(bc: _BindCtx, key: str, spec: dict) -> Optional[Tuple[str, str, str, float]]:
    """Bind from a matching inventory entry, preferring ``curated=True``. The taint is
    DERIVED from the source kind (scanned inventory content clamps to content_derived —
    IMPL-5 fail-untrusted; a forged ``origin_class`` can never launder into a silent
    bind). Handles the IMPL-8 multi-identity case: two services matching the same leaf ⇒
    signal a DECISION (return None here so the leaf falls through to a decision
    requirement)."""
    kl = (key or "").lower()

    # credentials are NAMES only (AC2 of R-A9) — a leaf naming a service whose
    # credential we hold binds operator-origin (the operator authorized it).
    creds = _situation_list(bc.situation, "credentials")
    for name in creds or []:
        if isinstance(name, str) and name and (name.lower() in kl or kl in name.lower()):
            return (f"credential:{name}", "situation", _OPERATOR, 0.85)

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
                return (f"service:{_get(s, 'name')}#{acct}", "situation", origin, 0.85)

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
            return (f"inventory:{field_name}", "situation", origin, 0.8)
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
            return (f"profile:{f}", "operator_profile", _OPERATOR, 1.0)

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
                return (f"profile_fact:{fid}", "operator_profile", _OPERATOR, conf)
    return None


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
        return (f"schema_default:{key}", "schema", _SYSTEMU, 1.0)
    if "const" in spec:
        return (f"schema_const:{key}", "schema", _SYSTEMU, 1.0)
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        return (f"schema_enum0:{key}", "schema", _SYSTEMU, 1.0)
    return None


# the ordered bind pipeline (spec §5.3)
_SOURCES = (_bind_filehandle, _bind_run_context, _bind_inventory_entry,
            _bind_profile, _bind_schema_default)


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
        if schema_value is not _SENTINEL:
            _emit_requirement(bc, kind=("input" if is_path else "decision"),
                              schema_path=schema_path, state="have", source="schema",
                              value_origin=_SYSTEMU,
                              bound_value_ref=f"schema_{schema_value_kind or 'value'}:{key}",
                              confidence=1.0,
                              rationale=f"schema {schema_value_kind or 'value'} (systemu_authored)")
            return

        # Otherwise try the 5 sources in order (first hit wins).
        bound = None
        for src in _SOURCES:
            try:
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

        bound_ref, source, value_origin, confidence = bound
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
                      bound_value_ref, confidence, rationale) -> None:
    """Construct + append one Requirement, CLAMPING ``value_origin`` to a canonical
    taint value first (Finding 2 / #2 fail-safe): a non-canonical origin can never
    raise a ValidationError that propagates to the outer except and empties the whole
    objective. ``None`` (a genuine no-value gap) is preserved as-is."""
    from systemu.core.models import Requirement
    vo = None if value_origin is None else _coerce_origin(value_origin)
    bc.reqs.append(Requirement(
        kind=kind, schema_path=schema_path, state=state, source=source,
        value_origin=vo, bound_value_ref=bound_value_ref, confidence=float(confidence),
        rationale=rationale,
    ))


# ── EffectTag → requires_external_verification (AC4, §5.8) ────────────────────
def _requires_external_verification(capability) -> bool:
    """Dangerous-until-proven: an external / irreversible / money / UNKNOWN effect
    (or an EMPTY tag list = UNKNOWN-until-classified) ⇒ True. A capability whose
    effects are ALL benign local-reads ⇒ False.

    S4 Step 0 (the None-capability guard): a ``None``/absent capability is NOT a
    real EffectTag classification — it is "no capability resolved yet". The only
    live binder call pre-loop passes ``capability=None`` (shadow_runtime's
    producer), so absent this guard EVERY objective would be stamped
    ``requires_external_verification=True`` off a phantom classification. The
    trigger must reflect a REAL EffectTag; the real per-objective capability lands
    with R-A12. Until then a None capability ⇒ False (do NOT stamp external)."""
    if capability is None:
        return False                             # S4: no real classification ⇒ don't stamp
    try:
        from systemu.runtime.effect_tags import coerce, EffectTag
    except Exception:
        return True                              # can't classify ⇒ fail-safe dangerous
    tags = _get(capability, "effect_tags") or []
    if not isinstance(tags, list) or not tags:
        return True                              # [] = UNKNOWN-until-classified ⇒ dangerous
    # the effects considered "provably safe to skip external ground-truth":
    _BENIGN = {EffectTag.LOCAL_READ.value}
    for raw in tags:
        v = coerce(raw)
        if v not in _BENIGN:
            return True                          # any non-benign / UNKNOWN effect ⇒ dangerous
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
def compute_requirements(objective, capability, situation, ctx) -> List[Any]:
    """Compute the per-objective ``list[Requirement]`` by BIND-mode schema-diff.

    Reads ``capability``'s schema, diffs every REQUIRED leaf against the 5 sources,
    applies the T_high + content_derived gate (IMPL-5), and stamps
    ``requires_external_verification`` on ``objective`` from the EffectTag (AC4/§5.8).
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
                objective.requires_external_verification = _requires_external_verification(capability)
        except Exception:
            logger.debug("[binder] could not stamp requires_external_verification", exc_info=True)

        schema = _capability_schema(capability)
        root = _normalized_root(schema)
        if not root:
            return []

        # resolve the granted-roots store (source #1 re-gate). Prefer one already on
        # ctx; else construct from the vault; else None (source #1 no-ops).
        granted = getattr(ctx, "_granted_roots", None)
        if granted is None:
            granted = _granted_from_ctx(ctx)

        tool_name = str(_get(capability, "name") or "")
        sit = situation if isinstance(situation, dict) else {}
        _goal = str(_get(objective, "goal") or "") if objective is not None else ""
        _crit = str(_get(objective, "success_criteria") or "") if objective is not None else ""
        bc = _BindCtx(situation=sit, ctx=ctx, granted=granted, tool_name=tool_name,
                      reference_text=(_goal + " " + _crit).strip())
        _diff_schema(bc, root)
        return bc.reqs
    except Exception:
        logger.debug("[binder] compute_requirements failed; returning []", exc_info=True)
        return []


def _granted_from_ctx(ctx):
    """Best-effort GrantedRootsStore from the ctx's vault; None on any miss."""
    vault = getattr(ctx, "vault", None)
    if vault is None:
        return None
    try:
        from systemu.runtime.granted_roots import GrantedRootsStore
        return GrantedRootsStore(base_dir=vault.root)
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


def build_requirement_report(objectives, capability, situation, ctx):
    """Aggregate ``compute_requirements`` across objectives into a RequirementReport:
    ``per_objective`` + a DEDUPED ``ask_bundle`` (every non-'have' requirement). The
    per-objective core is ``compute_requirements``; this is the §5.6 pull/scope-card
    feed. Defensive: never raises."""
    from systemu.core.models import RequirementReport

    per: Dict[int, List[Any]] = {}
    ask: List[Any] = []
    seen_keys = set()
    for obj in objectives or []:
        try:
            reqs = compute_requirements(obj, capability, situation, ctx)
        except Exception:
            reqs = []
        oid = _get(obj, "id")
        per[oid] = reqs
        for r in reqs:
            if not _needs_ask(r):
                continue
            key = (_get(r, "schema_path"), _get(r, "kind"), _get(r, "state"),
                   _get(r, "value_origin"))
            if key in seen_keys:
                continue                          # dedupe identical asks across objectives
            seen_keys.add(key)
            ask.append(r)
    try:
        return RequirementReport(per_objective=per, ask_bundle=ask)
    except Exception:
        logger.debug("[binder] RequirementReport assembly failed", exc_info=True)
        return RequirementReport()
