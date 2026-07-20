"""R-P3b — the action ledger (MASTER-SPEC §6): an inspectable, exportable,
append-ordered PROJECTION over already-persisted sources (RUL-7 — NO new durable
writer). This module holds the projection PRIMITIVES + exporters; the vault
projection (``iter_rows``/``project``) is Part 2.

Design decisions (grounded against the installed sources; see the R-P3b ledger
scope):
  * **PII-out-of-chain (DEC-23 S-1 / CMP-2).** Every free-text/PII value appears
    twice — as a DIGEST inside the hashable body (``params_digest``,
    ``evidence_fingerprint``, ``Criterion.text_digest``) and as a raw value ONLY
    under :attr:`LedgerRow.raw_beside`, which the canonical/hash pass AND both
    exporters SKIP. A lawful erasure blanks ``raw_beside`` and the chain still
    verifies.
  * **Fixed-width ``ts`` (AC3).** ``datetime.isoformat`` omits microseconds when
    zero → variable-width strings that break lexicographic ordering AND byte-stable
    re-export. :func:`norm_ts` re-emits every stamp as ``…​.ffffffZ`` (always 6
    micro digits).
  * **ONE canonical encoder** (:func:`canonical_bytes`, compact ``sort_keys`` — NOT
    the ``indent=2`` snapshot/receipt precedent) used by BOTH the chain hash and the
    JSONL exporter, so they are byte-identical and FROZEN once shipped.

Slice-2a status: :func:`canonical_bytes` / :func:`compute_row_hash` are built +
tested but are NOT yet invoked to POPULATE ``row_hash``/``seq``/``prev_hash`` (those
serialize ``null``) — fixing the rule now makes the future chain a zero-migration
switch (CMP-2). The live chain writer, the ``resolved_via`` channel stamp, the
DEC-13 criteria authoring path, and the UI page are deferred companion slices.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any, List, Optional

from pydantic import BaseModel, Field


# ── models ───────────────────────────────────────────────────────────────────

class Criterion(BaseModel):
    text_digest: str                      # sha256(canonical(raw_text)) — DIGEST, in-chain
    state: str                            # "met" | "unmet" | "unverifiable"


class LedgerRow(BaseModel):
    # identity — deterministic, stable across re-projection
    source_kind: str                      # action_audit | decision | committed_effect
    source_id:   str

    # event
    ts:         str                       # FIXED-WIDTH ISO-8601 UTC "…​.ffffffZ" (norm_ts)
    event_kind: str                       # effect | gate_decision | committed_effect

    # actor — id-bearing provenance in run_ref; origin is a coerce_origin token ONLY
    actor: dict = Field(default_factory=dict)

    # action — IN-CHAIN values are digests only
    action: dict = Field(default_factory=dict)

    # gate
    gate: dict = Field(default_factory=dict)

    # outcome — DEC-13: verification is NEVER conflated with the criteria N-of-M
    outcome: dict = Field(default_factory=dict)

    # undo
    undo: dict = Field(default_factory=lambda: {"kind": "none", "ref": None})

    # raw-beside — PII, un-chained, erase-able; NEVER hashed, NEVER exported
    raw_beside: dict = Field(default_factory=dict)

    # S-1 chain fields (DEC-23) — RESERVED-NULL until the chain writer (CMP-2)
    seq:        Optional[int] = None       # chained field (IN body)
    chain_day:  Optional[str] = None       # "YYYY-MM-DD"; chained field (IN body)
    prev_hash:  Optional[str] = None       # HASH FIELD — excluded from body
    row_hash:   Optional[str] = None       # HASH FIELD — excluded from body


# ── canonical serialization + chain hash (the AC3/AC5 crux; FROZEN once shipped) ─

_HASH_FIELDS = ("prev_hash", "row_hash")   # excluded from the hashed body
_RAW_FIELD = "raw_beside"                  # PII — never chained, never exported


def canonical_bytes(obj: Any) -> bytes:
    """RFC-8785-style canonical JSON — the ONE encoder used by the chain hash AND
    the JSONL exporter so they are byte-identical. FROZEN: changing any kwarg
    re-hashes the world."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _digest(obj: Any) -> str:
    """sha256 over the canonical bytes of ``obj`` (hex)."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def _chained_body(row: LedgerRow) -> dict:
    d = row.model_dump(mode="json")        # all JSON-native
    d.pop(_RAW_FIELD, None)                # PII out of chain
    for k in _HASH_FIELDS:                # exclude ONLY prev_hash, row_hash
        d.pop(k, None)
    return d                               # seq + chain_day REMAIN (chained fields)


def compute_row_hash(prev_hash: Optional[str], row: LedgerRow) -> str:
    """``row_hash = sha256( prev_hash || canonical(row-without-hash-fields) )``.
    Genesis: ``prev_hash=""``. prev is fixed-width hex (or "") and the body always
    begins with '{', so the byte-concat is unambiguous."""
    prev = (prev_hash or "").encode("utf-8")
    return hashlib.sha256(prev + canonical_bytes(_chained_body(row))).hexdigest()


def norm_ts(ts: str) -> str:
    """Normalize any ISO-8601 UTC stamp to fixed-width ``YYYY-MM-DDTHH:MM:SS.ffffffZ``
    (always 6 micro digits, Z suffix) so lexicographic order == chronological and
    re-export is byte-stable (AC3). An unparseable value is returned unchanged
    (honest — never a crash)."""
    s = (ts or "").strip()
    if not s:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def mask_and_digest_params(params: Any) -> "tuple[Any, str]":
    """MASK secrets, THEN digest the MASKED form — so the digest never encodes a
    secret and matches the raw-beside masked form (AC4). Returns (masked, digest)."""
    from systemu.runtime.external_verifier import _mask_evidence
    masked = _mask_evidence(params if params is not None else {})
    return masked, _digest(masked)


# ── SIEM export (AC3 byte-stable; raw_beside NEVER emitted) ───────────────────

_COLUMNS = [
    "ts", "seq", "event_kind", "source_kind", "source_id",
    "actor_lane", "actor_origin", "action_tool", "action_effect_tags",
    "action_params_digest", "action_host", "gate_verdict", "gate_decision_id",
    "gate_resolved_via", "outcome_status", "outcome_verification",
    "outcome_evidence_ref", "outcome_evidence_fingerprint",
    "criteria_met", "criteria_total", "undo_kind", "undo_ref",
    "chain_day", "prev_hash", "row_hash",
]   # 25 columns, FROZEN order


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "|".join(str(x) for x in v)
    return str(v)


def _csv_cells(row: LedgerRow) -> "list[str]":
    a, act, g, o, u = (row.actor or {}), (row.action or {}), (row.gate or {}), \
        (row.outcome or {}), (row.undo or {})
    vals = [
        row.ts, row.seq, row.event_kind, row.source_kind, row.source_id,
        a.get("lane"), a.get("origin"), act.get("tool"),
        sorted(act.get("effect_tags") or []), act.get("params_digest"), act.get("host"),
        g.get("verdict"), g.get("decision_id"), g.get("resolved_via"),
        o.get("status"), o.get("verification"), o.get("evidence_ref"),
        o.get("evidence_fingerprint"), o.get("criteria_met"), o.get("criteria_total"),
        u.get("kind"), u.get("ref"), row.chain_day, row.prev_hash, row.row_hash,
    ]
    return [_cell(v) for v in vals]


def export_csv(rows: List[LedgerRow]) -> bytes:
    """SIEM-shaped CSV — fixed 25-column header, deterministic row order, UTF-8
    (no BOM), ``\\n`` line endings. ``raw_beside`` is never emitted. Byte-stable:
    ``export_csv(rows) == export_csv(rows)``."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(_COLUMNS)
    for r in rows:
        w.writerow(_csv_cells(r))
    return buf.getvalue().encode("utf-8")


def export_jsonl(rows: List[LedgerRow]) -> bytes:
    """One canonical JSON line per row (same encoder as the chain hash → byte-
    identical), ``raw_beside`` stripped (the compliance export is not the authed-UI
    store). Byte-stable."""
    out = bytearray()
    for r in rows:
        d = r.model_dump(mode="json")
        d.pop(_RAW_FIELD, None)
        out += canonical_bytes(d) + b"\n"
    return bytes(out)


# ── Part 2: the vault projection (RUL-7 — reads only) ─────────────────────────

import re as _re

_HOST_KEYS = ("url", "endpoint", "host")
# A bare hostname (+ optional :port) ONLY — no path, '@', query, space. This is what
# stops a raw endpoint path / email / query-string from leaking into action.host
# (endpoint/host/url are not secret-named keys, so key-redaction doesn't touch them).
_HOSTNAME_RE = _re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?(:\d{1,5})?$")


def _read_action_audit(vault, execution_id=None, since_ts=None, until_ts=None) -> "list[dict]":
    """Action-audit rows in append order. A specific ``execution_id`` routes through
    the backend-aware vault query; the FULL projection reads the file backend's
    ``audit/actions.jsonl`` directly.

    ``since_ts``/``until_ts`` are INCLUSIVE bounds compared on the normalized
    (fixed-width) stamp, so the compliance export can name a CLOSED range whose
    re-export stays byte-stable even after later rows land (AC3).

    Degrades gracefully (skips a corrupt/non-object line, tolerates a stray non-UTF-8
    byte) — EXCEPT it raises loudly on a non-file backend, so a compliance consumer
    can never mistake a silently-empty ledger (no full-scan API yet) for "no actions
    happened"."""
    _until = norm_ts(str(until_ts)) if until_ts else None
    if execution_id:
        try:
            got = vault.query_action_audit(execution_id=execution_id, since_ts=since_ts) or []
        except Exception:
            return []
        if _until:      # the backend query has no upper bound — apply it here
            got = [r for r in got if isinstance(r, dict)
                   and norm_ts(str(r.get("ts", ""))) <= _until]
        return got
    # Full projection — file backend only in this slice.
    backend = getattr(vault, "_storage_backend", "file")
    if backend != "file":
        raise NotImplementedError(
            f"full action-ledger projection is file-backend-only in this slice; the "
            f"{backend!r} backend has no full-scan API yet — pass execution_id or add "
            "one before exporting (never a silently-empty ledger)")
    from pathlib import Path as _P
    root = getattr(vault, "root", None)
    if root is None:
        raise NotImplementedError(
            "action-ledger full projection needs a file-backend vault exposing .root")
    audit = _P(root) / "audit" / "actions.jsonl"
    if not audit.exists():
        return []
    _since = norm_ts(str(since_ts)) if since_ts else None
    rows: "list[dict]" = []
    # errors="replace" localizes byte corruption to one line instead of emptying all.
    for line in audit.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue                              # valid JSON but not an object — skip (never crash)
        _rts = norm_ts(str(r.get("ts", "")))
        if _since and _rts < _since:
            continue
        if _until and _rts > _until:
            continue
        rows.append(r)
    return rows


def _derive_host(masked_params: Any) -> Optional[str]:
    """Best-effort DESTINATION HOSTNAME from masked params — never fabricated, and
    never raw PII. A scheme URL is parsed to its hostname (urlparse drops userinfo /
    path / query); a bare ``host`` value is accepted ONLY if it is a plain hostname
    (the regex rejects a path/email/endpoint). Anything else → None (honest)."""
    if not isinstance(masked_params, dict):
        return None
    from urllib.parse import urlparse
    for k in _HOST_KEYS:
        v = masked_params.get(k)
        if not isinstance(v, str) or not v:
            continue
        if "://" in v:
            try:
                h = urlparse(v).hostname
                if h:
                    return h
            except Exception:
                pass
        elif _HOSTNAME_RE.match(v):
            return v            # a plain hostname only — a path/email/query never matches
    return None


def _effect_row(audit: dict) -> LedgerRow:
    """One `event_kind="effect"` row from an action-audit entry (the AC1 spine)."""
    eid = str(audit.get("execution_id") or "")
    oid = audit.get("objective_id")
    action = str(audit.get("action") or "")
    ts = norm_ts(str(audit.get("ts") or ""))
    masked, digest = mask_and_digest_params(audit.get("params") or {})
    lane = "quick" if eid.startswith("quick_") else "workflow"
    return LedgerRow(
        source_kind="action_audit",
        source_id=f"{eid}|{oid}|{action}|{ts}",
        ts=ts, event_kind="effect",
        actor={"lane": lane, "origin": _derive_origin(eid),
               "run_ref": {"execution_id": eid, "activity_id": None,
                           "scroll_id": None, "shadow_id": None}},
        action={"tool": action, "effect_tags": [],
                "params_digest": digest, "host": _derive_host(masked)},
        gate={"verdict": None, "decision_id": None,
              "resolution_class": None, "resolved_via": None},
        outcome={"status": ("success" if audit.get("success") else "failed"),
                 "verification": None, "evidence_ref": None,
                 "evidence_fingerprint": None,
                 "criteria_met": None, "criteria_total": None, "criteria": []},
        raw_beside={"params": masked, "detail": None,
                    "criteria_text": [], "evidence_body": None},
    )


EXPORT_EID_PREFIX = "export_"     # operator-initiated compliance export (ledger_export)


def _derive_origin(eid: str) -> str:
    """A coerce_origin token (never a fabricated compound string). Quick-lane eids
    are chat; an operator-initiated compliance export is 'manual' (it is a person at
    the dashboard, not the scheduler); otherwise default to 'system' honestly (no
    id-bearing invention)."""
    if eid.startswith("quick_"):
        return "chat"
    if eid.startswith(EXPORT_EID_PREFIX):
        return "manual"
    return "system"


def _enrich_with_receipt(row: LedgerRow, receipts: dict, oid: str) -> bool:
    """Join a run's per-OBJECTIVE receipt onto an effect row → verification +
    evidence fingerprint (DEC-13: verified iff ``confirmed`` is True, else claimed;
    NEVER conflated with the criteria N-of-M). Returns True iff a receipt was
    attached (so the caller attaches it to ONE effect per objective, not all)."""
    r = receipts.get(oid) if isinstance(receipts, dict) else None
    if not isinstance(r, dict):
        return False
    eid = (row.actor.get("run_ref") or {}).get("execution_id", "")
    row.outcome["verification"] = "verified" if r.get("confirmed") is True else "claimed"
    row.outcome["evidence_ref"] = f"{eid}#{oid}"
    # A stable digest that survives IMPL-12 GC of the evidence body (AC5). Only fields
    # the receipt store actually persists (host was always None — dropped).
    row.outcome["evidence_fingerprint"] = _digest(
        {"oid": oid, "method": r.get("method"), "stamped_at": r.get("stamped_at")})
    return True


def iter_rows(vault, *, data_dir=None, execution_id=None, since_ts=None, until_ts=None):
    """Project the persisted sources into LedgerRows (RUL-7 — reads only). The
    effect-row spine is action-audit successes (AC1: one row per side-effectful
    execution); each is MASK-redacted (AC4). A run's receipt is per-OBJECTIVE, so it
    enriches the FIRST effect row of that objective only — "verified" rows count
    verified objectives, never fanned onto every action. Best-effort: a missing/
    corrupt source degrades to fewer rows and never raises (a non-file backend raises
    loudly instead of a silently-empty ledger — see _read_action_audit)."""
    from systemu.runtime import receipts_store
    audits = _read_action_audit(vault, execution_id=execution_id,
                                since_ts=since_ts, until_ts=until_ts)
    _receipt_cache: dict = {}
    _receipted: set = set()                # (eid, oid) already carrying the objective's receipt
    for a in audits:
        if not isinstance(a, dict) or not a.get("success"):
            continue                       # 2a: effects COMMITTED (not attempted-failed)
        row = _effect_row(a)
        eid = a.get("execution_id")
        oid = str(a.get("objective_id"))
        if eid not in _receipt_cache:
            try:
                _receipt_cache[eid] = receipts_store.read_receipts(eid, data_dir=data_dir) or {}
            except Exception:
                _receipt_cache[eid] = {}
        key = (eid, oid)
        if key not in _receipted and _enrich_with_receipt(row, _receipt_cache[eid], oid):
            _receipted.add(key)
        yield row


def project(vault, **filters) -> List[LedgerRow]:
    """Materialized, sorted projection — total order ``(ts, source_kind, source_id)``
    (fixed-width ts ⇒ lexicographic == chronological)."""
    rows = list(iter_rows(vault, **filters))
    rows.sort(key=lambda r: (r.ts, r.source_kind, r.source_id))
    return rows
