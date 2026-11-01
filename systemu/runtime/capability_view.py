"""R-CAP1 · CAP-4 (+ CAP-3 ordering) -- THE selection view, and the ``find_tools``
lookup behind it.

ONE MINT (DEC-43). Every tool-consuming prompt renders its tool list from
:func:`build_view` -- the quick lane, the runtime decision loop, the Stage-3
extractor, the skill recalibrator, the scroll validator and the refinery
appraiser. A builder gathers its own CANDIDATES (that set is its dispatch or
dedup surface and is not this module's business) and hands them here; what the
model is SHOWN comes back as one value. ``tools/lint_tool_view.py`` fails if a
builder writes a tool list into a prompt payload from anything else.

TWO-STAGE DISCLOSURE (CAP-4 a/b). The candidates are sorted by one fully
specified, deterministic key (:func:`view_key`). The first ``K`` are rendered as
FULL records (unchanged dicts); the rest become a one-line ``name (slots)``
index. With ``K`` or fewer candidates nothing is demoted -- the view is the whole
surface, in key order (the R9(a) floor: set identity + determinism, not byte
identity with the pre-CAP prompt). Meta-affordances (:data:`PINNED_AFFORDANCES`,
e.g. ``find_tools`` itself) are always rendered in full and never count toward
``K`` -- an escape hatch hidden behind the hatch would be no hatch.

THE KEY (CAP-4, in order):
  1. exact-name match          -- the query IS this tool's name
  2. eligibility gate (CAP-3)  -- effectful query only; see below
  3. canonical slot match      -- (verb, target) both present in the query
  4. target match              -- the target class is present in the query
  5. lexical overlap           -- query tokens shared with NAME + SLOTS
  6. effect-tag compatibility  -- a read-only query prefers read-only tags
  7. origin trust              -- builtin < forged < mcp/unknown (tie-break)
  8. io-shape fit              -- INERT (0): a free-text query has no typed input
  9. verified usage            -- INERT (0): see below
 10. ``tool_id``               -- the terminal tiebreak
then the record's canonical JSON, so the order is TOTAL even if two records
share an id: replay-stable, never storage-iteration order. The tool-controlled
description is NEVER a signal (CAP-3 keyword-stuffing defense).

CAP-3 TRUST, AND WHAT IS MISSING (DEC-34). The spec makes an INDEPENDENT
verified-done record (§5.8) the gate for an effectful slot's top-K and then a
recency signal within it. No such per-tool record exists: §5.8 evidence
(``ExternalEvidence``) is per OBJECTIVE, not attributed to a tool, and is not
live in the loop; ``receipts_store`` is display-only. The capability ledger's
``success`` is tool-self-reported and is forbidden as this input (CAP-0.4). So
:attr:`ToolTraits.verified_done` has NO production writer and is always 0, and
nothing here claims a ranking by verified usage. The documented fallback is the
shipped CAP-3 origin trust: for an effectful query an unverified row is ordered
by origin (builtin before forged before mcp) BEFORE any lexical signal, so a
keyword-stuffed forged/mcp name cannot lift itself into the top-K of an
effectful slot. "Operator-enable" -- the other admission CAP-3 names -- has no
witness either: vault ``enabled`` is also set by the auto-deploy path and MCP
``enabled_tools`` by a bulk harness grant, so neither is read as one.

NEVER-SUBTRACT (CAP-4c / R9(b)). :func:`lookup` searches the COMPLETE store --
every Tool-catalog row whatever its state, plus the enabled MCP tools, plus any
lane extras -- and every hit carries its state (enabled / disabled / broken /
quarantined / not-ready) with a reason. It never changes DISPATCH: what a lane
may call is still decided by that lane, and a non-callable hit tells the agent
to ask the operator, never to forge a duplicate. No LLM, no harness request.

DEC-27. The tool roster is read through ``load_tool_index_strict``; a read that
raises is a WITNESSED, distinct state (:data:`CATALOG_DEGRADED`) that the view
renders into the prompt -- an unreadable roster can never look like "no tools".
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from systemu.runtime import capability_slots as cs

logger = logging.getLogger(__name__)

#: Full records per prompt (CAP-4a default).
K_DEFAULT = 12

#: The registered lookup tool's name (one spelling, shared by every lane).
FIND_TOOLS_NAME = "find_tools"

#: Meta-affordances: always rendered in full, never ranked, never counted toward K.
PINNED_AFFORDANCES = frozenset({FIND_TOOLS_NAME, "mcp_search_tools", "mcp_call_tool"})

#: Payload keys the view adds beside a builder's own full-record key.
INDEX_KEY = "more_tools"
INDEX_NOTE_KEY = "more_tools_note"
STATUS_KEY = "tool_catalog_status"
VIEW_EXTRA_KEYS = (INDEX_KEY, INDEX_NOTE_KEY, STATUS_KEY)

_INDEX_NOTE_LOOKUP = (
    "These tools also exist and can be called by name, but only their name and "
    "capability slot are shown here to save context. Call find_tools with a query "
    "(or a tool's exact name) to get its full record before calling it.")
_INDEX_NOTE_PLAIN = (
    "These tools also exist; only their name and capability slot are shown, "
    "because they ranked below the full records for this task.")

# ── catalog witness (DEC-27) ─────────────────────────────────────────────────

CATALOG_OK = "ok"
#: the strict read raised; ``headers`` are a best-effort forgiving read.
CATALOG_DEGRADED = "degraded"
#: the backend has no strict reader; completeness cannot be witnessed.
CATALOG_UNWITNESSED = "unwitnessed"
#: the lane's candidates are an explicit id list, not a roster read.
CATALOG_NOT_A_ROSTER = "not_a_roster_read"


@dataclass(frozen=True)
class CatalogRead:
    """The tool roster AND how it was read -- one value, so the witness cannot be
    separated from the rows it describes (DEC-32: a fence is a value)."""
    headers: Tuple[Any, ...]
    state: str
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.state in (CATALOG_OK, CATALOG_NOT_A_ROSTER)


#: For lanes whose candidates are NOT a roster read (the runtime loop loads an
#: activity's explicit tool ids). A builder must say so -- ``catalog`` is a
#: required argument of :func:`build_view`.
NO_ROSTER_READ = CatalogRead(headers=(), state=CATALOG_NOT_A_ROSTER,
                             reason="candidates are an explicit tool list, not a roster read")


def _ascii(text: Any, limit: int = 240) -> str:
    out = str(text or "").encode("ascii", "backslashreplace").decode("ascii")
    return out[:limit]


def _forgiving_headers(vault) -> Tuple[Any, ...]:
    try:
        if hasattr(vault, "load_index"):
            raw = vault.load_index("tools") or []
        else:
            raw = vault.list_tools() or []
        return tuple(h for h in list(raw) if h is not None)
    except Exception:
        return ()


def read_tool_headers(vault) -> CatalogRead:
    """Read the tool roster with a completeness witness.

    The strict reader is looked up on the CLASS, so a ``MagicMock`` vault (whose
    instances invent any attribute) is honestly reported as unwitnessed instead of
    'reading' a mock. A strict read that raises keeps the forgiving rows -- so no
    surface loses tools it showed before -- but carries :data:`CATALOG_DEGRADED`
    and the reason, which every view renders."""
    strict = getattr(type(vault), "load_tool_index_strict", None)
    if strict is None:
        return CatalogRead(_forgiving_headers(vault), CATALOG_UNWITNESSED,
                           "this vault backend cannot witness a complete tool roster read")
    try:
        raw = vault.load_tool_index_strict()
    except Exception as exc:  # VaultUnreadable on every real backend
        stage = getattr(exc, "stage", "") or "unknown"
        return CatalogRead(
            _forgiving_headers(vault), CATALOG_DEGRADED,
            _ascii(f"the tool roster could not be read (stage {stage}: {exc})"))
    if type(raw) is not list:
        return CatalogRead(_forgiving_headers(vault), CATALOG_DEGRADED,
                           "the strict tool roster read did not return a list")
    return CatalogRead(tuple(raw), CATALOG_OK)


# ── per-tool traits (the ranking inputs a record does not carry) ─────────────

@dataclass(frozen=True)
class ToolTraits:
    tool_id: str
    origin: str = "unknown"                 # builtin | forged | mcp:<server> | unknown
    effect_tags: Tuple[str, ...] = ()
    #: §5.8 INDEPENDENT verified-done count for THIS tool. No production writer
    #: exists (see the module docstring); never fill it from a self-report.
    verified_done: int = 0
    verified_recency: int = 0


#: Whether an independent per-tool verified-done source is wired. It is not.
VERIFIED_USAGE_SOURCE = "absent"
VERIFIED_USAGE_NOTE = (
    "no independent per-tool verified-done record exists yet (section 5.8 evidence "
    "is per objective and not tool-attributed), so verified usage does not rank; "
    "effectful asks fall back to origin trust")


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def traits_from_tool(tool: Any) -> ToolTraits:
    """A vault Tool (object or index header)."""
    name = str(_field(tool, "name", "") or "")
    tid = str(_field(tool, "id", "") or "") or f"name:{name}"
    origin = "forged" if bool(_field(tool, "forged_by_systemu", False)) else "builtin"
    tags = tuple(sorted(str(getattr(t, "value", t))
                        for t in (_field(tool, "effect_tags", None) or [])))
    return ToolTraits(tool_id=tid, origin=origin, effect_tags=tags)


def traits_from_mcp(entry: Mapping[str, Any]) -> ToolTraits:
    """An operator-connected MCP tool. Effect tags are UNKNOWN (no source to scan;
    a server's own annotations are untrusted -- CAP-3), carried as empty."""
    server = str(entry.get("server", "") or "").rstrip("/")
    name = str(entry.get("name", "") or "")
    return ToolTraits(tool_id=f"mcp:{server}:{name}", origin=f"mcp:{server}")


def traits_from_v2(entry: Mapping[str, Any]) -> ToolTraits:
    """A code-registered (v2) catalog entry. Repo code is builtin; the MCP bridge
    registers ``mcp__<server>__<tool>`` entries under toolset ``mcp``."""
    name = str(entry.get("name", "") or "")
    if entry.get("toolset") == "mcp" or name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) >= 3 else ""
        return ToolTraits(tool_id=f"v2:{name}", origin=f"mcp:{server}")
    return ToolTraits(tool_id=f"v2:{name}", origin="builtin")


def traits_by_name(tools: Iterable[Any]) -> Dict[str, ToolTraits]:
    out: Dict[str, ToolTraits] = {}
    for t in tools or ():
        name = str(_field(t, "name", "") or "")
        if name and name not in out:
            out[name] = traits_from_tool(t)
    return out


# ── the key ──────────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")
_CANON_VERBS = frozenset({"create", "send", "read", "update", "delete", "list", "run"})
#: Same set the shipped CAP-3 trust weighting keys off (capability_index).
_EFFECTFUL_VERBS = frozenset({"create", "send", "delete", "update", "run"})
_READ_VERBS = frozenset({"read", "list"})
_READ_TAGS = frozenset({"local_read", "net_read", "no_effect", "screen_capture",
                        "clipboard_read"})
_STOP = frozenset({"a", "an", "the", "to", "my", "me", "of", "for", "and", "or",
                   "in", "on", "at", "with", "from", "by", "it", "this", "that",
                   "please", "is", "be", "i", "you", "your", "our", "we"})
_ORIGIN_TRUST = {"builtin": 0, "forged": 1}


def _is_verb(token: str) -> bool:
    canon = cs.canonical_verb(token)
    return canon in _CANON_VERBS and (canon != token or token in _CANON_VERBS)


@dataclass(frozen=True)
class QueryProfile:
    text: str
    tokens: frozenset
    verbs: frozenset
    targets: frozenset
    effectful: bool
    read_only: bool


def profile_query(query: Any) -> QueryProfile:
    """Deterministic, lexical+typed only (CAP-8). An ambiguous noun-verb ("file",
    "open") counts as a verb: that errs toward an EFFECTFUL reading, which only
    ever tightens the CAP-3 gate -- the safe direction."""
    text = str(query or "").strip().lower()
    toks = _WORD.findall(text)
    verbs = frozenset(cs.canonical_verb(t) for t in toks if _is_verb(t))
    targets = frozenset(cs.canonical_target(t) for t in toks
                        if not _is_verb(t) and t not in _STOP and not t.isdigit())
    return QueryProfile(
        text=text, tokens=frozenset(toks), verbs=verbs, targets=targets,
        effectful=bool(verbs & _EFFECTFUL_VERBS),
        read_only=bool(verbs) and verbs <= _READ_VERBS)


def _origin_trust(origin: str) -> int:
    return _ORIGIN_TRUST.get(origin, 2)


def _slots_of(name: str) -> List[Tuple[str, str]]:
    try:
        return list(cs.slots_from_name(name))
    except Exception:
        return []


def view_key(name: str, traits: ToolTraits, q: QueryProfile, *, gate: bool = True) -> tuple:
    """The CAP-4 key (ascending = best first). ``gate`` applies the CAP-3
    effectful-slot eligibility gate; the view always gates, :func:`lookup` (an
    explicit search whose every hit is labelled with its origin) does not."""
    name = str(name or "")
    pairs = _slots_of(name)
    slot_strs = [cs.slot_str(p) for p in pairs]
    row_tokens = set(_WORD.findall(name.lower())) | set(
        _WORD.findall(" ".join(slot_strs)))
    name_exact = 0 if (q.text and q.text == name.lower()) else 1
    trust = _origin_trust(traits.origin)
    if gate and q.effectful:
        # verified rows (an independent §5.8 signal -- none exist today) are
        # eligible; everything else is ordered by origin trust BEFORE relevance.
        eligibility = (0, 0) if traits.verified_done > 0 else (1, trust)
    else:
        eligibility = (0, 0)
    slot_match = 0 if any(v in q.verbs and t and t in q.targets for v, t in pairs) else 1
    target_match = 0 if any(t and t in q.targets for _v, t in pairs) else 1
    lex = len(q.tokens & row_tokens)
    if q.read_only:
        tags = set(traits.effect_tags)
        if not tags or "unknown" in tags:
            effect_compat = 1
        elif tags <= _READ_TAGS:
            effect_compat = 0
        else:
            effect_compat = 2
    else:
        effect_compat = 0
    io_fit = 0   # inert: a free-text query carries no typed input shape to fit
    return (name_exact, eligibility, slot_match, target_match, -lex, effect_compat,
            trust, io_fit, -int(traits.verified_done), -int(traits.verified_recency),
            str(traits.tool_id))


def _content_key(record: Any) -> str:
    try:
        return json.dumps(record, sort_keys=True, default=str)
    except Exception:
        return repr(record)


def _name_of(record: Any, name_key: str) -> str:
    try:
        return str(_field(record, name_key, "") or "")
    except Exception:
        return ""


def _copy(record: Any) -> Any:
    return dict(record) if isinstance(record, dict) else record


def _index_line(name: str) -> str:
    slots = [cs.slot_str(p) for p in _slots_of(name)]
    return f"{name} ({', '.join(slots)})" if slots else name


# ── the view ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SelectionView:
    full: Tuple[Any, ...]
    pinned: Tuple[Any, ...]
    index: Tuple[str, ...]
    k: int
    total: int
    catalog_state: str
    catalog_reason: str = ""
    error: str = ""

    def full_records(self) -> List[Any]:
        """The records rendered in FULL: the top-K in key order, then the pinned
        affordances. Fresh copies -- a caller mutating its payload never aliases."""
        return [_copy(r) for r in self.full] + [_copy(r) for r in self.pinned]

    def index_lines(self) -> List[str]:
        return list(self.index)

    def status_note(self) -> str:
        if self.error:
            return _ascii("DEGRADED: the tool selection view failed "
                          f"({self.error}); every tool is shown in full.")
        if self.catalog_state in (CATALOG_OK, CATALOG_NOT_A_ROSTER):
            return ""
        if self.catalog_state == CATALOG_UNWITNESSED:
            return ("UNWITNESSED: this vault cannot prove its tool roster was read "
                    "completely, so the tool list may be missing tools. A missing tool "
                    "here is NOT evidence that it does not exist.")
        return _ascii("DEGRADED: " + (self.catalog_reason or "the tool roster could not be read")
                      + ". The tool list is INCOMPLETE - it is NOT evidence that a "
                      "tool does not exist. Use find_tools if you have it, or ask the "
                      "operator.", 600)

    def extra_fields(self, *, lookup: bool) -> Dict[str, Any]:
        """The keys the view adds beside the full records: the one-line index (only
        when something was demoted) and the degradation status (only when the
        roster read was not complete)."""
        out: Dict[str, Any] = {}
        if self.index:
            out[INDEX_KEY] = self.index_lines()
            out[INDEX_NOTE_KEY] = _INDEX_NOTE_LOOKUP if lookup else _INDEX_NOTE_PLAIN
        note = self.status_note()
        if note:
            out[STATUS_KEY] = note
        return out

    def payload_fields(self, full_key: str, *, lookup: bool) -> Dict[str, Any]:
        out: Dict[str, Any] = {full_key: self.full_records()}
        out.update(self.extra_fields(lookup=lookup))
        return out


def build_view(records: Sequence[Any], query: Any, *, catalog: CatalogRead,
               traits: Optional[Mapping[str, ToolTraits]] = None,
               k: Optional[int] = None, name_key: str = "name") -> SelectionView:
    """THE mint (DEC-43). ``records`` are the builder's candidate dicts (never
    mutated); ``traits`` maps a record's name to its ranking traits (a record
    with none ranks as an unknown origin -- fail-closed for trust). ``catalog`` is
    REQUIRED: a roster-reading builder passes its :class:`CatalogRead`, an
    explicit-id lane passes :data:`NO_ROSTER_READ`."""
    recs = list(records or [])
    kk = K_DEFAULT if k is None else max(0, int(k))
    state = str(getattr(catalog, "state", CATALOG_DEGRADED))
    reason = str(getattr(catalog, "reason", "") or "")
    tmap = dict(traits or {})
    try:
        q = profile_query(query)
        pinned, ranked = [], []
        for r in recs:
            (pinned if _name_of(r, name_key) in PINNED_AFFORDANCES else ranked).append(r)

        def _key(r):
            name = _name_of(r, name_key)
            t = tmap.get(name) or ToolTraits(tool_id=f"name:{name}")
            return (view_key(name, t, q, gate=True), _content_key(r))

        ordered = sorted(ranked, key=_key)
        full, rest = ordered[:kk], ordered[kk:]
        pinned.sort(key=lambda r: (_name_of(r, name_key), _content_key(r)))
        return SelectionView(
            full=tuple(full), pinned=tuple(pinned),
            index=tuple(_index_line(_name_of(r, name_key)) for r in rest),
            k=kk, total=len(recs), catalog_state=state, catalog_reason=reason)
    except Exception as exc:
        # Never shrink on a ranking failure: every record in full, deterministic.
        logger.warning("[CapabilityView] selection view failed; rendering every tool "
                       "in full: %s", exc)
        return SelectionView(
            full=tuple(sorted(recs, key=_content_key)), pinned=(), index=(),
            k=kk, total=len(recs), catalog_state=state, catalog_reason=reason,
            error=type(exc).__name__)


# ── find_tools: the complete-store lookup (CAP-4c / R9(b)) ───────────────────

STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_BROKEN = "broken"
STATE_QUARANTINED = "quarantined"
STATE_NOT_READY = "not-ready"
TOOL_STATES = (STATE_ENABLED, STATE_DISABLED, STATE_BROKEN, STATE_QUARANTINED,
               STATE_NOT_READY)

_RUNTIME_READY = frozenset({"deployed", "tested", "upgraded"})
_ABSENT = object()

FIND_TOOLS_DEFAULT_LIMIT = 8
FIND_TOOLS_MAX_LIMIT = 25

FIND_TOOLS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": ("Keywords for what you need (e.g. 'send email', 'read pdf') "
                            "or a tool's exact name."),
        },
        "limit": {
            "type": "integer",
            "description": (f"Maximum matches to return (default {FIND_TOOLS_DEFAULT_LIMIT}, "
                            f"max {FIND_TOOLS_MAX_LIMIT})."),
        },
    },
    # `query` is deliberately NOT required: the runtime's missing-required seam
    # turns a missing required param into an operator INPUT request, and a lookup
    # must never cost an operator ask. An empty query is answered with an error.
    "required": [],
}

FIND_TOOLS_DESCRIPTION = (
    "Search EVERY tool systemu has (enabled or not, including ones not listed in "
    "full here) by keywords or exact name. Returns each match's full record, its "
    "state (enabled / disabled / broken / quarantined / not-ready) and whether you "
    "can call it here. Deterministic and free: no model call, no operator ask. If a "
    "match exists but is not callable, ask the operator to enable it - never forge "
    "a duplicate.")


def find_tools_record() -> Dict[str, Any]:
    """The LLM-visible record for a lane that does not read the v2 registry."""
    return {
        "name": FIND_TOOLS_NAME,
        "description": FIND_TOOLS_DESCRIPTION,
        "parameter_names": ["query", "limit"],
        "parameters_schema": json.loads(json.dumps(FIND_TOOLS_SCHEMA)),
    }


@dataclass(frozen=True)
class ToolHit:
    name: str
    tool_id: str
    origin: str
    slots: Tuple[str, ...]
    state: str
    reason: str
    callable: Optional[bool]          # None = the caller gave no lane context
    description: str = ""
    parameters: Any = None
    next_step: str = ""


@dataclass(frozen=True)
class LookupResult:
    query: str
    hits: Tuple[ToolHit, ...]
    total_in_store: int
    catalog_state: str
    catalog_reason: str = ""

    @property
    def complete(self) -> bool:
        return self.catalog_state in (CATALOG_OK, CATALOG_NOT_A_ROSTER)


#: A lane extra: (record dict, traits, state, reason).
LaneExtra = Tuple[Dict[str, Any], ToolTraits, str, str]


@dataclass
class _StoreEntry:
    name: str
    traits: ToolTraits
    state: str
    reason: str
    description: str
    parameters: Any
    source: str                     # vault | mcp | lane
    shadowed: bool = False


def _status_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _header_state(h: Any) -> Tuple[str, str]:
    """State + reason from an index header. (Quarantine needs the full Tool and is
    applied to returned hits only -- see :func:`_full_tool_facts`.)"""
    enabled = bool(_field(h, "enabled", False))
    status = _status_str(_field(h, "status", ""))
    dry = _status_str(_field(h, "dry_run_status", "not_run")) or "not_run"
    if dry == "failed":
        return STATE_BROKEN, ("its dry-run FAILED, so it cannot be enabled until it is "
                              "repaired")
    if not enabled:
        if status == "proposed":
            return STATE_NOT_READY, ("it is only a proposal - no implementation has "
                                     "been forged yet")
        return STATE_DISABLED, "the operator has not enabled it"
    if status and status not in _RUNTIME_READY:
        return STATE_NOT_READY, (f"it is enabled but its status is '{status}', which "
                                 "is not runnable yet")
    impl = _field(h, "implementation_path", _ABSENT)
    if impl is not _ABSENT and not impl:
        return STATE_NOT_READY, "it is enabled but has no implementation"
    try:
        from systemu.runtime import optional_deps as _od
        missing = _od.unavailable_reason(
            [str(d) for d in (_field(h, "dependencies", []) or [])])
    except Exception:
        missing = "could not verify its optional dependencies on this machine"
    if missing:
        return STATE_NOT_READY, _ascii(missing, 300)
    return STATE_ENABLED, "enabled and ready"


def _mcp_entries(vault) -> List[Dict[str, Any]]:
    # Residual (not a witness): connections.get_state swallows its own read errors,
    # so an unreadable connector store reads as "no connectors" here.
    try:
        from systemu.runtime.mcp import connections
        return [e for e in (connections.enabled_tools(vault) or []) if isinstance(e, dict)]
    except Exception:
        return []


def v2_lane_extras(config=None, *, available_only: bool = False) -> List[LaneExtra]:
    """Code-registered tools as lookup extras (record, traits, state, reason)."""
    out: List[LaneExtra] = []
    try:
        from systemu.runtime.tool_registry_v2 import registry as _v2
        cfg = config
        if cfg is None:
            try:
                from sharing_on.config import Config as _Config
                cfg = _Config.from_env()
            except Exception:
                cfg = None
        for e in _v2.list():
            if e.name in PINNED_AFFORDANCES:
                continue
            ok = _v2.available(e.name, cfg) if e.check_fn is not None else True
            if available_only and not ok:
                continue
            rec = {"name": e.name, "description": e.description or "",
                   "parameters_schema": dict(e.schema or {}), "toolset": e.toolset}
            out.append((rec, traits_from_v2(rec),
                        STATE_ENABLED if ok else STATE_NOT_READY,
                        "enabled and ready" if ok else
                        "its availability check failed (a backend or setting is missing)"))
    except Exception:
        logger.debug("[CapabilityView] v2 extras unavailable", exc_info=True)
    return out


def store_entries(vault, *, lane_extras: Optional[Sequence[LaneExtra]] = None
                  ) -> Tuple[List[_StoreEntry], CatalogRead]:
    """The COMPLETE store: every Tool-catalog header (any state), every enabled MCP
    tool, and the lane's extras. Nothing is filtered by state -- that is the point."""
    catalog = read_tool_headers(vault)
    entries: List[_StoreEntry] = []
    vault_enabled_names = set()
    for h in catalog.headers:
        name = str(_field(h, "name", "") or "")
        if not name:
            continue
        state, reason = _header_state(h)
        if state == STATE_ENABLED:
            vault_enabled_names.add(name)
        entries.append(_StoreEntry(
            name=name, traits=traits_from_tool(h), state=state, reason=reason,
            description=str(_field(h, "description", "") or ""),
            parameters=(_field(h, "parameters_schema", None)
                        or _field(h, "parameters_schema_summary", None) or {}),
            source="vault"))
    for e in _mcp_entries(vault):
        name = str(e.get("name", "") or "")
        if not name:
            continue
        entries.append(_StoreEntry(
            name=name, traits=traits_from_mcp(e), state=STATE_ENABLED,
            reason="an operator-connected connector tool",
            description=str(e.get("description", "") or ""),
            parameters=dict(e.get("schema") or {}), source="mcp",
            shadowed=name in vault_enabled_names))
    extras = lane_extras if lane_extras is not None else v2_lane_extras()
    seen = {(en.name, en.traits.tool_id) for en in entries}
    for rec, tr, state, reason in extras or ():
        name = str((rec or {}).get("name", "") or "")
        if not name or name in PINNED_AFFORDANCES or (name, tr.tool_id) in seen:
            continue
        seen.add((name, tr.tool_id))
        entries.append(_StoreEntry(
            name=name, traits=tr, state=state, reason=reason,
            description=str(rec.get("description", "") or ""),
            parameters=dict(rec.get("parameters_schema") or {}), source="lane"))
    return entries, catalog


def _full_tool_facts(vault, entry: _StoreEntry) -> Tuple[Optional[str], Any]:
    """(quarantine refusal line or None, full parameters schema or None) for a
    vault hit -- one ``get_tool`` per RETURNED hit, never per store row."""
    if entry.source != "vault":
        return None, None
    try:
        tool = vault.get_tool(entry.traits.tool_id)
    except Exception:
        return None, None
    try:
        refusals = list(getattr(tool, "credential_refusals", []) or [])
    except Exception:
        refusals = []
    schema = getattr(tool, "parameters_schema", None)
    return ((str(refusals[0]) if refusals else None),
            (dict(schema) if isinstance(schema, dict) else None))


def _next_step(name: str, state: str, callable_here: Optional[bool], reason: str) -> str:
    dont = "Do NOT forge or improvise a duplicate."
    if state == STATE_ENABLED:
        if callable_here is False:
            return (f"It exists but is not callable in this run: ask the operator for "
                    f"'{name}' through your ask action, naming this exact tool. {dont}")
        return "Call it by this exact name."
    if state == STATE_QUARANTINED:
        return (f"Its enable path is operator review: ask the operator to review "
                f"'{name}' on the Tools page. {dont}")
    if state == STATE_BROKEN:
        return f"Ask the operator to repair and enable '{name}' (your ask action). {dont}"
    if state == STATE_NOT_READY:
        return f"Ask the operator to make '{name}' ready ({reason}). {dont}"
    return f"Ask the operator to enable '{name}' (your ask action). {dont}"


def lookup(vault, query: Any, *, limit: Optional[int] = None,
           callable_names: Optional[Iterable[str]] = None,
           lane_extras: Optional[Sequence[LaneExtra]] = None) -> LookupResult:
    """Rank the COMPLETE store for ``query`` and return the top ``limit`` hits, each
    with its state and (given ``callable_names``) whether this lane can call it.
    A hit is callable only when it is ``enabled`` AND in the lane's dispatch set --
    the lookup reports dispatch eligibility, it never extends it."""
    q = profile_query(query)
    lim = FIND_TOOLS_DEFAULT_LIMIT if limit is None else int(limit)
    lim = max(1, min(FIND_TOOLS_MAX_LIMIT, lim))
    entries, catalog = store_entries(vault, lane_extras=lane_extras)
    ranked = sorted(entries, key=lambda en: (view_key(en.name, en.traits, q, gate=False),
                                             en.source, en.name))
    callable_set = None if callable_names is None else set(callable_names)
    hits: List[ToolHit] = []
    for en in ranked[:lim]:
        state, reason, params = en.state, en.reason, en.parameters
        refusal, full_schema = _full_tool_facts(vault, en)
        if refusal:
            state, reason = STATE_QUARANTINED, _ascii(refusal, 300)
        if full_schema is not None:
            params = full_schema
        if en.shadowed:
            reason = ("shadowed by a local tool of the same name (a connector never "
                      "shadows a local tool)")
        if callable_set is None:
            can: Optional[bool] = None
        else:
            can = (state == STATE_ENABLED and en.name in callable_set and not en.shadowed)
        hits.append(ToolHit(
            name=en.name, tool_id=en.traits.tool_id, origin=en.traits.origin,
            slots=tuple(cs.slot_str(p) for p in _slots_of(en.name)),
            state=state, reason=reason, callable=can,
            description=_ascii(en.description, 300),
            parameters=params if state == STATE_ENABLED else None,
            next_step=_next_step(en.name, state,
                                 False if en.shadowed else can, reason)))
    return LookupResult(query=str(query or ""), hits=tuple(hits),
                        total_in_store=len(entries), catalog_state=catalog.state,
                        catalog_reason=catalog.reason)


def render_lookup(result: LookupResult) -> str:
    """ASCII text for the model. A STRING on purpose: the runtime's history slice
    previews any dict/list value over 400 chars, which would cut the schemas off."""
    lines: List[str] = []
    if not result.complete:
        lines.append(_ascii(
            "WARNING: the tool catalog could not be read completely ("
            + (result.catalog_reason or result.catalog_state)
            + "); tools may be missing from these results - that is NOT evidence a "
              "tool does not exist.", 600))
    lines.append(_ascii(
        f'find_tools: {len(result.hits)} match(es) for "{result.query}" out of '
        f"{result.total_in_store} tool(s) in the store (ranked, nothing filtered out; "
        "use a more specific query or an exact name to see others).", 400))
    for i, h in enumerate(result.hits, 1):
        can = {True: "yes", False: "no", None: "unknown"}[h.callable]
        lines.append(_ascii(
            f"[{i}] {h.name} | state: {h.state} | callable here: {can} | origin: "
            f"{h.origin} | slots: {', '.join(h.slots) or '-'}", 400))
        if h.description:
            lines.append("    what: " + _ascii(h.description, 300))
        if h.state != STATE_ENABLED or h.callable is False:
            lines.append("    why: " + _ascii(h.reason, 300))
        if h.parameters:
            try:
                ptxt = json.dumps(h.parameters, sort_keys=True, default=str)
            except Exception:
                ptxt = str(h.parameters)
            lines.append("    parameters: " + _ascii(ptxt, 1200))
        lines.append("    next: " + _ascii(h.next_step, 400))
    return "\n".join(lines)


def find_tools_tool_result(vault, params: Any, *,
                           callable_names: Optional[Iterable[str]] = None,
                           lane_extras: Optional[Sequence[LaneExtra]] = None
                           ) -> Dict[str, Any]:
    """The tool-result dict every lane returns for a ``find_tools`` call. Never
    raises -- a lookup failure is an honest failure result, not a crash."""
    p = params if isinstance(params, dict) else {}
    query = str(p.get("query") or "").strip()
    if not query:
        return {"success": False, "error_type": "find_tools_needs_query",
                "error": ("find_tools needs a 'query': keywords for what you need, or "
                          "a tool's exact name.")}
    try:
        limit = int(p.get("limit")) if p.get("limit") not in (None, "") else None
    except (TypeError, ValueError):
        limit = None
    try:
        res = lookup(vault, query, limit=limit, callable_names=callable_names,
                     lane_extras=lane_extras)
    except Exception as exc:
        return {"success": False, "error_type": "find_tools_failed",
                "error": _ascii(f"find_tools failed: {type(exc).__name__}: {exc}")}
    return {
        "success": True,
        "query": query,
        "count": len(res.hits),
        "total_in_store": res.total_in_store,
        "catalog_complete": res.complete,
        "ranking": ("name, capability slot and word match (descriptions are never a "
                    "signal), then origin trust; " + VERIFIED_USAGE_NOTE),
        "matches": render_lookup(res),
    }
