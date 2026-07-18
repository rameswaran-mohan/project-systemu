"""R-W1 (W-A slice-1) — the World Model v2 fact substrate (§5.11.a/.b).

The greenfield FOUNDATION the successor W-program (W-A…W-F) rides on:

  * WM-1  a universal, durable, provenance-IMMUTABLE ``Fact`` store;
  * WM-2  negative knowledge (``NegativeFact``) with a short TTL — "searched and
          NOT found" is a first-class, expiring fact;
  * WM-4  a deterministic ``world.query`` view family over the store.

PURE substrate — **nothing in the run loop reads or writes it yet.** Slice-2 wires
the payoff (the §5.1 SituationReport becomes a ranked *view* over this store, the
§5.5 discovery negative-fact loop, and the §5.3 binder's AC1 assertion). Because no
existing bind source reads the store (verified: no run-loop module imports this),
the §5.11.f risk-5 invariant holds trivially — the agent behaves IDENTICALLY when
this feature is absent or empty (a smaller world, never a broken one).

Trust (WM-15 / §5.10.b): a ``Fact`` is untrusted DATA carrying an IMMUTABLE
``origin_class``. Taint never launders — the store REJECTS any attempt to change a
fact's ``origin_class`` on update (E1). ``Fact.taint_permits_silent_bind`` is a
taint-only, NECESSARY-NOT-SUFFICIENT advisory: the §5.3 binder (slice-2) is the sole
authority and ANDs taint ∧ confidence ∧ verification ∧ effect-class. This module
DESCRIBES; it never AUTHORIZES (§5.10.b#3). It holds ids/names/paths only, never a
secret value (E6).

Deferred to slice-2+ and W-D (documented, not dropped): the report-as-view inversion,
the binder AC1 assertion, the discovery negative-fact write/read loop, populating the
store from live inventory, and WM-5 WorldGraph / WM-3 belief-revision / WM-13 gardener
decay (all W-D — so slice-1 facts are FLAT, no edges, and there is no confidence decay;
"absence expires faster than presence" is realised here as a short ABSOLUTE default
TTL, and becomes relative-to-presence when W-D decay lands).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

from systemu.runtime import capability_slots as _cs

# The canonical IMMUTABLE taint axis (identical to table_store.TableItem.origin_class
# and SituationReport). Only ``content_derived`` rides the untrusted-content fence.
ORIGIN_CLASSES = {"operator", "systemu_authored", "content_derived"}

#: WM-2 — absence expires FASTER than presence. In slice-1 there is no positive-fact
#: decay horizon yet (WM-13 gardener is W-D), so this is a short ABSOLUTE default;
#: the relative "faster than presence" comparison goes live with W-D decay.
DEFAULT_NEGATIVE_TTL_SECONDS = 6 * 60 * 60          # 6 hours

_WORD = re.compile(r"[a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(*parts: Any) -> set:
    out: set = set()
    for p in parts:
        out.update(_WORD.findall(str(p if p is not None else "").lower()))
    return out


class ImmutableProvenanceError(ValueError):
    """Raised when a ``put_fact`` update would change an existing fact's
    ``origin_class`` — taint never launders (§5.10.b#1)."""


# ── WM-1: the universal fact schema ──────────────────────────────────────────

class ProvStep(BaseModel):
    """One step in a fact's append-only ``source_chain``. Ids/names/paths only —
    never a secret value (E6)."""
    source_kind: str                       # census | probe | root_profile | mcp_resource | distillation | inventory | operator | ...
    ref: str = ""                          # a non-secret handle (a host, path, tool id, server name)
    at: str = Field(default_factory=_now)


class Fact(BaseModel):
    """A world-model fact (WM-1): ``{value, origin_class, confidence, last_confirmed,
    source_chain}`` plus a stable ``fact_id`` (WM-4 ``provenance`` needs it) and an
    OPEN ``kind`` (an unknown kind is stored, fenced, gated — never refused, WM-5
    Callout 2). Flat — no edges in slice-1 (WorldGraph is W-D)."""
    fact_id: str
    kind: str                              # OPEN vocabulary: service | account | credential_ref | capability | data_location | device | artifact | skill | <unknown> (an unknown kind is stored+fenced, never refused)
    value: Any                             # ids/names/paths only — NEVER a secret value (E6)
    origin_class: str                      # IMMUTABLE, CLOSED taint axis — operator | systemu_authored | content_derived
    confidence: float = 0.0
    last_confirmed: Optional[str] = None
    source_chain: List[ProvStep] = Field(default_factory=list)

    @field_validator("origin_class")
    @classmethod
    def _origin_class_in_vocab(cls, v: str) -> str:
        """``origin_class`` is a CLOSED taint axis (unlike the open ``kind``). Refuse a
        typo'd/unknown value at construction — a mis-tagged provenance must fail LOUD
        (fail-closed), never be silently accepted as taint-clear (F1)."""
        if v not in ORIGIN_CLASSES:
            raise ValueError(f"origin_class must be one of {sorted(ORIGIN_CLASSES)}, got {v!r}")
        return v

    @property
    def taint_permits_silent_bind(self) -> bool:
        """Taint-only, NECESSARY-NOT-SUFFICIENT advisory (E2). A WHITELIST that fails
        CLOSED (F1): only ``operator``/``systemu_authored`` are taint-permitted; a
        ``content_derived`` fact — or, defensively, any unrecognized origin — can NEVER
        silent-bind (AC1). The actual silent-bind decision is the §5.3 binder's
        (slice-2), which ALSO requires sufficient confidence/verification for the effect
        class. This property is never itself the gate — the world model describes, it
        never authorizes."""
        return self.origin_class in {"operator", "systemu_authored"}


# ── WM-2: negative knowledge ─────────────────────────────────────────────────

class NegativeFact(BaseModel):
    """"Searched and NOT found" as a first-class, EXPIRING fact (WM-2). ``scope`` is a
    canonical goal/target key; ``probes`` is what was searched (so a handoff can cite
    what+when, AC2); ``recorded_at`` is when. Absence expires faster than presence."""
    scope: str
    probes: List[str] = Field(default_factory=list)
    recorded_at: str = Field(default_factory=_now)
    ttl_seconds: int = DEFAULT_NEGATIVE_TTL_SECONDS

    def is_expired(self, now: Optional[str] = None) -> bool:
        """True once ``ttl_seconds`` have elapsed since ``recorded_at``. Fail-OPEN on
        an unparseable timestamp (treat as expired ⇒ re-search) — a corrupt negative
        fact must never permanently suppress a real search."""
        try:
            rec = datetime.fromisoformat(self.recorded_at)
            cur = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
            if rec.tzinfo is None:
                rec = rec.replace(tzinfo=timezone.utc)
            if cur.tzinfo is None:
                cur = cur.replace(tzinfo=timezone.utc)
            return (cur - rec).total_seconds() > max(0, int(self.ttl_seconds))
        except Exception:
            return True


# ── survey watermark: what the last survey actually COVERED ──────────────────
# Absence of re-confirmation is only evidence of staleness when the survey genuinely
# looked. The survey is scope-varying (grant-scoped roots, a per-run file cap, per-source
# timeouts), so "not re-seen" alone would mass-stale perfectly valid facts. Recording the
# COVERAGE lets staleness be derived READ-SIDE — with no mutation of the facts themselves,
# so the store stays append-only/confirm-in-place and belief revision (a later program)
# is not pre-empted.

#: keep the last N watermarks — enough to reason about recent coverage, bounded on disk.
_MAX_SURVEYS = 20


class SurveyWatermark(BaseModel):
    """One survey's coverage record. Written by the populator; read only by the
    operator-facing surfaces.

    ``at`` is REQUIRED on purpose: defaulting it would make a malformed/older row
    validate to read-time *now*, which is newer than every fact — turning the whole
    store stale in one read. Required means such a row is skipped on load instead."""
    at: str
    kinds_surveyed: List[str] = Field(default_factory=list)   # fact kinds this survey produced
    roots_covered: List[str] = Field(default_factory=list)    # granted roots that actually yielded entries
    data_location_cap_hit: bool = False                       # the file listing was truncated


def _as_dt(value: str) -> datetime:
    """Parse an ISO timestamp, assuming UTC when naive. Raises on garbage."""
    d = datetime.fromisoformat(str(value))
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def _path_under(value: Any, root: Any) -> bool:
    """Path-COMPONENT containment, not a raw string prefix — ``C:/Radiology/scan.pdf`` is
    NOT under ``C:/R``. Mirrors the confinement layer's rule without touching the
    filesystem; also normalises separators and a trailing slash."""
    v = str(value or "").replace("\\", "/").rstrip("/").lower()
    r = str(root or "").replace("\\", "/").rstrip("/").lower()
    if not v or not r:
        return False
    return v == r or v.startswith(r + "/")


def staleness_of(fact: Fact, survey: Optional[SurveyWatermark]) -> str:
    """Derive a fact's staleness from its ``last_confirmed`` against what the latest
    survey COVERED. Returns:

      ``confirmed``    — re-seen by the latest survey;
      ``unconfirmed``  — the survey covered this fact's scope and did NOT re-see it
                         (the honest "this may be gone" signal — e.g. a disconnected
                         service);
      ``not_surveyed`` — the survey did not cover this kind/scope, so absence is NOT
                         evidence (the case a naive "not re-seen ⇒ stale" rule gets
                         wrong);
      ``unknown``      — no survey recorded yet.

    Pure; never raises."""
    try:
        if survey is None:
            return "unknown"
        last = getattr(fact, "last_confirmed", None)
        if not last:
            return "unknown"                   # never confirmed ≠ evidence it disappeared
        try:
            # REAL datetime comparison. A lexicographic string compare gets this wrong
            # for a differing UTC offset or a naive stamp — and every such error points
            # the dangerous way (a newer fact reading as stale).
            if _as_dt(last) >= _as_dt(survey.at):
                return "confirmed"
        except Exception:
            return "unknown"                   # unparseable ⇒ never claim stale
        if fact.kind not in (survey.kinds_surveyed or ()):
            return "not_surveyed"
        if fact.kind == "data_location":
            if survey.data_location_cap_hit:
                return "not_surveyed"          # the listing was truncated — we stopped looking
            if not any(_path_under(fact.value, r) for r in (survey.roots_covered or [])):
                return "not_surveyed"          # outside the roots this survey actually walked
        return "unconfirmed"
    except Exception:
        return "unknown"


def fact_id_for(kind: str, value: Any) -> str:
    """A deterministic id for a ``(kind, value)`` — so re-observing the same fact
    confirms it IN PLACE rather than duplicating. Never uses wall-clock/randomness
    (replay-stable)."""
    try:
        canon = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canon = str(value)
    digest = hashlib.sha1(f"{kind}\x00{canon}".encode("utf-8", "replace")).hexdigest()[:16]
    return f"{kind}:{digest}"


# ── the durable store (atomic write + defensive reads; sole-writer) ──────────

def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FactStore:
    """The durable world-model store: facts (WM-1) + negative facts (WM-2), each in
    an atomically-written JSON file under ``<vault>/world_model/``. Defensive reads
    (a broken/absent file ⇒ empty). Sole-writer discipline — the only persister of
    these files."""

    def __init__(self, vault):
        self._vault = vault

    @property
    def _dir(self) -> Path:
        return Path(self._vault.root) / "world_model"

    @property
    def _facts_path(self) -> Path:
        return self._dir / "facts.json"

    @property
    def _negatives_path(self) -> Path:
        return self._dir / "negatives.json"

    # ── facts ────────────────────────────────────────────────────────────────
    def _load_facts(self) -> Dict[str, Fact]:
        try:
            if not self._facts_path.exists():
                return {}
            data = json.loads(self._facts_path.read_text(encoding="utf-8"))
            rows = data.get("facts") if isinstance(data, dict) else None
            out: Dict[str, Fact] = {}
            for r in (rows or []):
                try:
                    f = Fact.model_validate(r)
                    out[f.fact_id] = f
                except Exception:
                    continue                # one bad row never empties the store
            return out
        except Exception:
            return {}

    def _save_facts(self, facts: Dict[str, Fact]) -> None:
        payload = {"version": 1,
                   "facts": [f.model_dump(mode="json") for f in facts.values()]}
        _write_atomic(self._facts_path, json.dumps(payload, indent=2))

    @staticmethod
    def _resolve_put(existing: Optional[Fact], fact: Fact) -> Fact:
        """The fact to STORE for one put. ``origin_class`` is IMMUTABLE (a change
        raises ``ImmutableProvenanceError`` — E1/§5.10.b#1) and so is ``kind``
        (identity-defining); ``value``/``confidence``/``last_confirmed`` update;
        ``source_chain`` is APPEND-ONLY but deduped by ``(source_kind, ref)`` — so
        re-observing a fact from the SAME source never grows the chain (recency lives
        in ``last_confirmed``), while a DISTINCT source is still appended. Pure."""
        if existing is None:
            return fact
        if existing.origin_class != fact.origin_class:
            raise ImmutableProvenanceError(
                f"origin_class is immutable for {fact.fact_id!r}: "
                f"{existing.origin_class!r} → {fact.origin_class!r} refused")
        if existing.kind != fact.kind:
            # kind is identity-defining (fact_id_for folds it in) — a re-typed re-put on
            # a hand-set id is a caller error, refused not silently re-typed (F3).
            raise ImmutableProvenanceError(
                f"kind is immutable for {fact.fact_id!r}: "
                f"{existing.kind!r} → {fact.kind!r} refused")
        merged_chain = list(existing.source_chain)
        seen = {(s.source_kind, s.ref) for s in existing.source_chain}
        for step in fact.source_chain:
            key = (step.source_kind, step.ref)
            if key not in seen:
                merged_chain.append(step)
                seen.add(key)
        return fact.model_copy(update={
            "origin_class": existing.origin_class,   # preserved regardless (belt-and-braces)
            "source_chain": merged_chain,
        })

    def put_fact(self, fact: Fact) -> Fact:
        """Insert or CONFIRM one fact (see :meth:`_resolve_put` for the immutability +
        append-only rules). Returns the stored fact."""
        facts = self._load_facts()
        stored = self._resolve_put(facts.get(fact.fact_id), fact)
        facts[stored.fact_id] = stored
        self._save_facts(facts)
        return stored

    def put_facts(self, new_facts: "List[Fact]") -> int:
        """BULK insert/confirm — ONE load + ONE save for the whole batch, so a
        per-run populator costs O(N) instead of N whole-file rewrites (calling
        ``put_fact`` in a loop is O(N²) disk work). Same per-fact immutability +
        append-only rules; a fact that violates immutability is SKIPPED (logged)
        rather than aborting the batch. Returns the number stored."""
        if not new_facts:
            return 0
        facts = self._load_facts()
        n = 0
        for f in new_facts:
            try:
                stored = self._resolve_put(facts.get(f.fact_id), f)
            except ImmutableProvenanceError:
                logger.debug("[world-model] refused an immutable-provenance change for %s",
                             getattr(f, "fact_id", "?"), exc_info=True)
                continue
            facts[stored.fact_id] = stored
            n += 1
        if n:
            self._save_facts(facts)
        return n

    def get(self, fact_id: str) -> Optional[Fact]:
        return self._load_facts().get(fact_id)

    def all_facts(self) -> List[Fact]:
        return list(self._load_facts().values())

    def query_facts(self, *, kind: Optional[str] = None,
                    limit: Optional[int] = None) -> List[Fact]:
        """The raw store query. NEVER-SUBTRACT (E3/§5.10.d binds the STORE): with the
        default ``limit=None`` it returns EVERY matching fact, so nothing is silently
        hidden — a caller can always broaden a trimmed view back to the whole store.
        Deterministic order (by ``fact_id``)."""
        rows = self.all_facts()
        if kind is not None:
            rows = [f for f in rows if f.kind == kind]
        rows.sort(key=lambda f: f.fact_id)
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        return rows

    # ── negative facts ─────────────────────────────────────────────────────────
    def _load_negatives(self) -> Dict[str, NegativeFact]:
        try:
            if not self._negatives_path.exists():
                return {}
            data = json.loads(self._negatives_path.read_text(encoding="utf-8"))
            rows = data.get("negatives") if isinstance(data, dict) else None
            out: Dict[str, NegativeFact] = {}
            for r in (rows or []):
                try:
                    n = NegativeFact.model_validate(r)
                    out[n.scope] = n
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _save_negatives(self, negs: Dict[str, NegativeFact]) -> None:
        payload = {"version": 1,
                   "negatives": [n.model_dump(mode="json") for n in negs.values()]}
        _write_atomic(self._negatives_path, json.dumps(payload, indent=2))

    def put_negative(self, neg: NegativeFact) -> None:
        """Record (or refresh) a negative fact for ``neg.scope`` (one per scope — a
        re-search overwrites the prior record with a fresh timestamp)."""
        negs = self._load_negatives()
        negs[neg.scope] = neg
        self._save_negatives(negs)

    def query_negative(self, scope: str, now: Optional[str] = None) -> Optional[NegativeFact]:
        """The negative fact for ``scope`` IFF present and not yet expired (AC2). An
        expired negative returns None ⇒ the caller re-searches."""
        neg = self._load_negatives().get(scope)
        if neg is None or neg.is_expired(now):
            return None
        return neg

    def all_negatives(self) -> List[NegativeFact]:
        return list(self._load_negatives().values())

    # ── survey watermarks (write: populator · read: operator surfaces) ───────
    @property
    def _surveys_path(self) -> Path:
        return self._dir / "surveys.json"

    def record_survey(self, survey: SurveyWatermark) -> None:
        """Append a survey-coverage watermark. WRITE-ONLY w.r.t. the facts — it never
        loads or touches ``facts.json``, so the populator can call it without becoming a
        reader of the store."""
        try:
            existing = self.all_surveys()
        except Exception:
            existing = []
        rows = (existing + [survey])[-_MAX_SURVEYS:]
        payload = {"version": 1, "surveys": [s.model_dump(mode="json") for s in rows]}
        _write_atomic(self._surveys_path, json.dumps(payload, indent=2))

    def all_surveys(self) -> List[SurveyWatermark]:
        try:
            if not self._surveys_path.exists():
                return []
            data = json.loads(self._surveys_path.read_text(encoding="utf-8"))
            rows = data.get("surveys") if isinstance(data, dict) else None
            out: List[SurveyWatermark] = []
            for r in (rows or []):
                try:
                    out.append(SurveyWatermark.model_validate(r))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def latest_survey(self) -> Optional[SurveyWatermark]:
        """The most recent watermark BY TIMESTAMP, not by write order — two concurrent
        runs can append out of order, and the newest is what staleness must compare to."""
        rows = self.all_surveys()
        if not rows:
            return None
        try:
            return max(rows, key=lambda s: _as_dt(s.at))
        except Exception:
            return rows[-1]


# ── WM-4: the world.query view family (deterministic; results are fenced data) ─
# Each view returns full ``Fact`` objects (E3: results carry fact_id + origin_class +
# confidence + last_confirmed, so the slice-2 fence/binder keeps the taint/verification
# signal). Ranked best-first; NEVER-SUBTRACT — ``limit`` is caller-overridable and the
# store is always fully reachable via ``query_facts``/``about``/``get``.

def _rank_by_overlap(facts: List[Fact], query: str,
                     limit: Optional[int]) -> List[Fact]:
    q = _tokens(query)
    def key(f: Fact) -> tuple:
        overlap = len(q & _tokens(f.value, f.kind))
        return (-overlap, -f.confidence, f.fact_id)     # total, deterministic
    ranked = sorted(facts, key=key)
    if limit is not None:
        ranked = ranked[:max(0, int(limit))]
    return ranked


def find_services(store: FactStore, match: str,
                  limit: Optional[int] = None) -> List[Fact]:
    """``service`` facts ranked by token overlap with ``match``."""
    return _rank_by_overlap(store.query_facts(kind="service"), match, limit)


def what_can(store: FactStore, verb: str, target_class: str,
             limit: Optional[int] = None) -> List[Fact]:
    """WM-4 ``what_can(verb, target_class)`` — ``capability`` facts whose derived slot
    matches the canonical ``verb:target`` (reusing the R-CAP1 slot canonicalizer). The
    fact-store expression of the ``find_tools`` seed (CAP-9). NEVER-SUBTRACT."""
    want = _cs.slot_str(_cs.canonical_slot(verb, target_class))
    caps = store.query_facts(kind="capability")
    def slots_of(f: Fact) -> List[str]:
        return [_cs.slot_str(s) for s in _cs.slots_from_name(str(f.value))]
    exact = [f for f in caps if want in slots_of(f)]
    exact.sort(key=lambda f: (-f.confidence, f.fact_id))
    if limit is not None:
        exact = exact[:max(0, int(limit))]
    return exact


def find_data(store: FactStore, like: str, under: Optional[str] = None,
              limit: Optional[int] = None) -> List[Fact]:
    """``data_location`` facts matching ``like``, optionally restricted to those whose
    value/handle is ``under`` a path/prefix."""
    rows = store.query_facts(kind="data_location")
    if under:
        u = str(under).lower()
        rows = [f for f in rows if u in str(f.value).lower()]
    return _rank_by_overlap(rows, like, limit)


def about(store: FactStore, key: str, limit: Optional[int] = None) -> List[Fact]:
    """Everything the world model believes about ``key`` (a host / app / account
    identifier — WM-6 tie), across all kinds, ranked by overlap. The broadening
    escape hatch that keeps the never-subtract floor honest — a fact trimmed from a
    ranked view is still reachable here."""
    q = _tokens(key)
    hits = [f for f in store.all_facts() if q & _tokens(f.value, f.kind, f.fact_id)]
    return _rank_by_overlap(hits, key, limit)


def provenance(store: FactStore, fact_id: str) -> Optional[List[ProvStep]]:
    """WM-4 ``provenance(fact_id)`` — the fact's append-only ``source_chain``, or None
    if unknown."""
    f = store.get(fact_id)
    return list(f.source_chain) if f is not None else None
