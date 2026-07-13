"""R-CAP1 · CAP-2 + CAP-4 — the capability index (a derived cache) and the
deterministic selection view (spec §5.5.1, 4-lens'd BUILD-READY 2026-07-13).

CAP-2 (index): ``<vault>/capabilities/capability_index.json`` is a DERIVED cache,
not a second source of truth. Its SOLE writer is ``reconcile_once`` (a
reconciler-derive-only store, the OnTheTable ``table/items.json`` pattern — CAP-0.1):
inline write-time maintenance is deliberately absent, so the RMW fields CAP-4 ranks
on can never be lost across processes, and the store passes the CONC-MAP guardrail.
Structural rows rebuild from ``{vault Tool catalog} ∪ {mcp/connections.enabled_tools}``
(MCP rows are NOT in the vault catalog — CAP-0.6); ``usage`` is READ from
``capability_ledger`` — and ``verified_done_count`` is left 0 until a §5.8
independent-verified signal exists (NEVER the tool-self-reported ledger ``successes``
— CAP-0.4), so a tool cannot inflate its own rank.

CAP-4 (selection): ``select_top_k`` / ``find_tools`` score with a fully-specified
deterministic tuple key whose FINAL component is ``tool_id`` (a total terminal
tiebreak — replay-stable ordering, never storage-iteration order). ``find_tools``
is NEVER-SUBTRACT: it ranks the COMPLETE store, so a demoted/low-ranked tool is
still returnable (the §5.10.d floor applied to tools).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from systemu.runtime import capability_slots as cs

# ── effect classes that make a slot "effectful" (CAP-3 trust weighting) ────────
_EFFECTFUL_TAGS = {
    "net_call", "network", "send_message", "money_move", "oauth_call",
    "external_write", "delete", "irreversible",
}


class IndexRow(BaseModel):
    tool_id: str
    name: str
    detail: str = ""                       # description, for lexical match only
    slots: List[str] = Field(default_factory=list)     # canonical "verb:target"
    effect_tags: List[str] = Field(default_factory=list)
    io_shape_hash: str = ""
    usage: Dict[str, Any] = Field(default_factory=dict)  # last_used_at, invocations, verified_done_count
    status: str = "ready"
    origin: str = "builtin"                # builtin | forged | mcp:<server>
    parent_id: Optional[str] = None
    superseded_by: Optional[str] = None


# --------------------------------------------------------------------------- #
# derivation inputs (thin wrappers so tests can monkeypatch the sources)
# --------------------------------------------------------------------------- #

def _catalog_tools(vault) -> List[Any]:
    try:
        return list(vault.list_tools() or [])
    except Exception:
        return []


def _field(tool: Any, key: str, default: Any = None) -> Any:
    """Read a tool header field from EITHER a dict (the real vault.list_tools()
    shape — the same contract table_reconciler._project_tools reads) or an object
    with attributes. The vault returns dicts, so dict access is the live path."""
    if isinstance(tool, dict):
        return tool.get(key, default)
    return getattr(tool, key, default)


def _mcp_enabled_tools(vault) -> List[Dict[str, Any]]:
    try:
        from systemu.runtime.mcp import connections
        return list(connections.enabled_tools(vault) or [])
    except Exception:
        return []


def _usage_for(vault, name: str) -> Dict[str, Any]:
    """Read usage SIGNALS from capability_ledger. verified_done_count is NOT the
    ledger's self-reported ``successes`` (CAP-0.4) — it stays 0 until a §5.8
    independent-verified source exists, so a tool can't inflate its own rank."""
    inv, last = 0, None
    try:
        from systemu.runtime import capability_ledger
        stats = capability_ledger.get_stats(vault, name)
        if isinstance(stats, dict):
            inv = int(stats.get("invocations") or 0)
            last = stats.get("last_used_at")
    except Exception:
        pass
    return {"last_used_at": last, "invocations": inv, "verified_done_count": 0}


# --------------------------------------------------------------------------- #
# CAP-2 — derive + persist (reconciler-sole-writer)
# --------------------------------------------------------------------------- #

def io_shape_hash(schema: Dict[str, Any]) -> str:
    """A stable hash of a tool's input SHAPE (sorted param name:type), so two
    tools with the same interface collide regardless of description wording. A
    non-dict property value (a legal JSON-Schema boolean subschema) contributes
    type '?' rather than raising (which would silently drop the whole tool)."""
    props = (schema or {}).get("properties") or {}
    if not isinstance(props, dict):
        return hashlib.sha1(b"").hexdigest()[:16]
    shape = sorted(
        f"{k}:{v.get('type', '?') if isinstance(v, dict) else '?'}"
        for k, v in props.items())
    return hashlib.sha1("|".join(shape).encode("utf-8")).hexdigest()[:16]


def _ready(tool: Any) -> bool:
    if not bool(_field(tool, "enabled", False)):
        return False
    if not _field(tool, "implementation_path", ""):
        return False
    return True


def _origin_for(tool: Any) -> str:
    return "forged" if bool(_field(tool, "forged_by_systemu", False)) else "builtin"


def derive_index(vault) -> List[IndexRow]:
    """Compute the current index rows from the live stores. Deterministic +
    idempotent; ordered by tool_id so the persisted file is stable."""
    rows: Dict[str, IndexRow] = {}

    for t in _catalog_tools(vault):
        try:
            if not _ready(t):
                continue
            name = _field(t, "name", "") or ""
            tid = _field(t, "id", "") or name
            if not tid:
                continue
            slots = [cs.slot_str(s) for s in cs.slots_from_name(name)]
            rows[tid] = IndexRow(
                tool_id=tid, name=name,
                detail=str(_field(t, "description", "") or "")[:300],
                slots=slots,
                effect_tags=list(_field(t, "effect_tags", []) or []),
                io_shape_hash=io_shape_hash(_field(t, "parameters_schema", {}) or {}),
                usage=_usage_for(vault, name),
                status=str(_field(t, "status", "ready") or "ready"),
                origin=_origin_for(t),
            )
        except Exception:
            continue

    for e in _mcp_enabled_tools(vault):
        try:
            if not isinstance(e, dict):
                continue
            server = str(e.get("server", "")).rstrip("/")
            name = str(e.get("name", "") or "")
            if not name:
                continue
            tid = f"mcp:{server}:{name}"
            if tid in rows:
                continue
            slots = [cs.slot_str(s) for s in cs.slots_from_name(name)]
            rows[tid] = IndexRow(
                tool_id=tid, name=name,
                detail=str(e.get("description", "") or "")[:300],
                slots=slots,
                # MCP effect tags are UNKNOWN (no source to AST-scan) — carried as
                # empty here; the risk tier is decided at the dispatch gate, and
                # ranking never lets an mcp row's lexical match downgrade severity.
                effect_tags=[],
                io_shape_hash=io_shape_hash(e.get("schema", {}) or {}),
                usage=_usage_for(vault, name),
                status="ready",
                origin=f"mcp:{server}",
            )
        except Exception:
            continue

    return [rows[k] for k in sorted(rows)]


def _index_path(vault) -> Path:
    return Path(vault.root) / "capabilities" / "capability_index.json"


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


def reconcile_index(vault) -> int:
    """Derive + persist the index snapshot. THE SOLE writer of capability_index.json
    (CAP-0.1 — only the daemon's periodic job calls this; readers that want a fresh
    view use ``find_tools(..., live=True)``, which derives in memory and never
    writes). Never raises — a failure leaves the prior snapshot in place."""
    try:
        rows = derive_index(vault)
        _write_atomic(_index_path(vault),
                      json.dumps([r.model_dump(mode="json") for r in rows], indent=2))
        return len(rows)
    except Exception:
        return 0


def load_index(vault) -> List[IndexRow]:
    """All persisted index rows. Defensive: a broken/absent file ⇒ []."""
    try:
        path = _index_path(vault)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: List[IndexRow] = []
        for entry in (raw or []):
            if not isinstance(entry, dict):
                continue
            try:
                out.append(IndexRow(**entry))
            except Exception:
                continue
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# CAP-4 — deterministic selection view
# --------------------------------------------------------------------------- #

import re as _re
_WORD = _re.compile(r"[a-z0-9]+")

# lower origin_trust sorts first: a keyword-stuffed mcp row can't outrank a
# builtin on a lexical tie (CAP-3 untrusted-origin weighting).
_ORIGIN_TRUST = {"builtin": 0, "forged": 1}

# query verbs that select an EFFECTFUL action — for these, origin trust outranks
# the (tool-controlled) lexical signal so an untrusted tool can't climb into the
# top-K of an effectful slot by stuffing keywords (CAP-3 / CAP-0.4).
_EFFECTFUL_VERBS = {"create", "send", "delete", "update", "run"}


def _tokens(*parts: str) -> set:
    out: set = set()
    for p in parts:
        out.update(_WORD.findall((p or "").lower()))
    return out


def _origin_trust(origin: str) -> int:
    return _ORIGIN_TRUST.get(origin, 2)          # mcp:* and anything unknown = 2


def _query_slots(query: str) -> List[str]:
    return [cs.slot_str(s) for s in cs.slots_from_name(query)]


def score_key(row: IndexRow, query: str) -> tuple:
    """A TOTAL, deterministic sort key (ascending = best first). Priority: exact
    canonical slot match → (for an effectful query) origin trust → lexical token
    overlap → origin trust → verified-usage → recency → tool_id (the terminal
    tiebreak — CAP-4). Lexical match is over NAME + SLOTS only — the tool-controlled
    ``description`` is NEVER a ranking signal (CAP-3 keyword-stuffing defense)."""
    qslots = _query_slots(query)
    slot_exact = 1 if (set(qslots) & set(row.slots)) else 0
    q_tokens = _tokens(query)
    row_tokens = _tokens(row.name, " ".join(row.slots))     # NOT row.detail
    lex = len(q_tokens & row_tokens)
    verified = int((row.usage or {}).get("verified_done_count") or 0)
    recency = int((row.usage or {}).get("invocations") or 0)
    trust = _origin_trust(row.origin)
    effectful = any(s.split(":", 1)[0] in _EFFECTFUL_VERBS for s in qslots)
    if effectful:
        # trust outranks the tool-controlled lexical signal (CAP-3): an mcp/forged
        # row can't stuff keywords to beat a builtin for an effectful slot.
        return (-slot_exact, trust, -lex, -verified, -recency, row.tool_id)
    return (-slot_exact, -lex, trust, -verified, -recency, row.tool_id)


def rank(rows: List[IndexRow], query: str) -> List[IndexRow]:
    """The full store, ranked best-first — never subtracts a row (CAP-4 floor)."""
    return sorted(rows, key=lambda r: score_key(r, query))


def select_top_k(rows: List[IndexRow], query: str, k: int = 12) -> List[IndexRow]:
    """The top-K full records for a tool-consuming prompt (CAP-4a)."""
    return rank(rows, query)[:max(0, int(k))]


def find_tools(vault, query: str, limit: Optional[int] = None,
               *, live: bool = False) -> List[Dict[str, Any]]:
    """CAP-4c — a deterministic index lookup (no LLM, burns no harness-request
    budget). NEVER-SUBTRACT: ranks the COMPLETE store, so every tool is
    returnable. Returns compact dicts (name+slot+origin+id).

    ``live=True`` derives the rows in memory (fresh, never persisted) — for a
    read-only caller (a CLI) that wants current results without becoming a second
    writer of ``capability_index.json`` (the daemon reconciler stays sole writer,
    CAP-0.1). ``live=False`` reads the daemon-maintained snapshot."""
    rows = derive_index(vault) if live else load_index(vault)
    ranked = rank(rows, query)
    if limit is not None:
        ranked = ranked[:max(0, int(limit))]
    return [{"tool_id": r.tool_id, "name": r.name, "slots": r.slots,
             "origin": r.origin, "detail": r.detail} for r in ranked]
