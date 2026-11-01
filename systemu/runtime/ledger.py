"""R-P3b — the action ledger (spec §6): an inspectable, exportable,
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

Rows (R-P3b): ``effect`` rows from the action audit carry the effect classes, the
verdict, the pass path and (when the pass consumed one) the decision id the sandbox
gate STAMPED on the audit row when it let the call run
(``tool_sandbox.ToolResult.gate``); ``committed_effect`` rows come from durable
receipts their writer marked live (``shadow`` is False) and confirmed -- the IMPL-7
set, still readable after the run's snapshot is gone; ``gate_decision`` rows are the
operator's decided gate cards from the decision queue.

**DEC-27 -- completeness is witnessed, never inferred.** A value the source record
did not capture is never left as an empty string or ``[]`` that reads as a finding:
the field is ``null``, the group's ``not_recorded_because`` map names the reason, and
the CSV cell reads :data:`NOT_RECORDED`. A field that does not apply to a row kind
(no card was posted for an allow-band call; a committed effect's gate is on its
effect row) reads :data:`NOT_APPLICABLE` the same way, under
``not_applicable_because``.
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


# ── DEC-27: a value the source did not capture is NAMED, never blank ─────────

NOT_RECORDED = "not_recorded"             # the CSV cell for an uncaptured value
NOT_RECORDED_KEY = "not_recorded_because"  # per-group {field: reason} (JSONL)


def _not_recorded(group: dict, field: str, reason: str) -> None:
    """Mark ``group[field]`` as not captured by its source: ``null`` in JSONL plus the
    reason under ``not_recorded_because``; :data:`NOT_RECORDED` in the CSV cell."""
    group[field] = None
    group.setdefault(NOT_RECORDED_KEY, {})[field] = reason


NOT_APPLICABLE = "not_applicable"            # the CSV cell for a field this row lacks
NOT_APPLICABLE_KEY = "not_applicable_because"  # per-group {field: reason} (JSONL)


def _not_applicable(group: dict, field: str, reason: str) -> None:
    """Mark ``group[field]`` as not part of this row kind (see :func:`_not_recorded`)."""
    group[field] = None
    group.setdefault(NOT_APPLICABLE_KEY, {})[field] = reason


def _field(group: Any, name: str) -> Any:
    """The exported value of ``group[name]``: :data:`NOT_RECORDED` /
    :data:`NOT_APPLICABLE` when the group names it so, the stored value otherwise
    (unchanged for a group with neither map, so such rows export byte-identically)."""
    if not isinstance(group, dict):
        return None
    nr = group.get(NOT_RECORDED_KEY)
    if isinstance(nr, dict) and name in nr:
        return NOT_RECORDED
    na = group.get(NOT_APPLICABLE_KEY)
    if isinstance(na, dict) and name in na:
        return NOT_APPLICABLE
    return group.get(name)


def _tags_cell(action: Any) -> Any:
    v = _field(action, "effect_tags")
    if isinstance(v, str):
        return v
    return sorted(v or [])


def _csv_cells(row: LedgerRow) -> "list[str]":
    a, act, g, o, u = (row.actor or {}), (row.action or {}), (row.gate or {}), \
        (row.outcome or {}), (row.undo or {})
    vals = [
        row.ts, row.seq, row.event_kind, row.source_kind, row.source_id,
        _field(a, "lane"), _field(a, "origin"), _field(act, "tool"),
        _tags_cell(act), _field(act, "params_digest"), _field(act, "host"),
        _field(g, "verdict"), _field(g, "decision_id"), _field(g, "resolved_via"),
        _field(o, "status"), _field(o, "verification"), _field(o, "evidence_ref"),
        _field(o, "evidence_fingerprint"), _field(o, "criteria_met"),
        _field(o, "criteria_total"),
        _field(u, "kind"), _field(u, "ref"), row.chain_day, row.prev_hash, row.row_hash,
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


# Why a stamped value is absent. Plain ASCII: these reach the JSONL export.
_NO_GATE_STAMP = (
    "the action-audit entry carries no gate stamp: it was written before the sandbox "
    "gate recorded its outcome on audit rows, or by a writer that had no gate outcome "
    "for the call (the code-registered tool path, the export event, or a storage "
    "backend with no column for it)")
_STAMP_NO_CLASS_COMMAND = (
    "the command gate let this call run; it scores the command text, not an effect "
    "class, and the tool declared none")
_STAMP_NO_CLASS = "the gate stamp on this entry names no effect class"
_STAMP_NO_BASIS = "the gate stamp does not say whether the classes were scored or declared"
# A code-registered tool is dispatched by name (ToolSandbox.execute), where the per-tool
# action gate does not run: there is no gate outcome to record, so none is claimed.
CODE_REGISTERED = "code_registered"
_CODE_REGISTERED_REASON = (
    "dispatched through the code-registered tool path, which the per-tool action gate "
    "does not score; only the destructive-call check and any gate inside the tool's own "
    "handler apply there, and neither reports an outcome to the ledger (an MCP call's "
    "own gate does, and its row carries that gate's stamp instead)")
_TAG_BASES = ("scored", "declared")


def _gate_stamp(audit: dict) -> Optional[dict]:
    """The sandbox gate's record stamped on an action-audit entry, or None. The entry
    is data off disk, so the concrete type is pinned before any use (DEC-36)."""
    stamp = audit.get("gate")
    return stamp if type(stamp) is dict else None


def _apply_effect_tags(action: dict, stamp: Optional[dict]) -> None:
    """Fill ``action.effect_tags`` (+ ``effect_tags_basis``) from the gate stamp --
    the classes the gate decided on for THIS call, never re-derived from the tool as
    it is today. Anything else is named as not recorded (DEC-27)."""
    if stamp is None:
        _not_recorded(action, "effect_tags", _NO_GATE_STAMP)
        return
    if stamp.get("passed_via") == CODE_REGISTERED:
        _not_recorded(action, "effect_tags", _CODE_REGISTERED_REASON)
        return
    tags = stamp.get("effect_tags")
    if type(tags) is list and tags and all(type(t) is str and t for t in tags):
        action["effect_tags"] = sorted(set(tags))
        basis = stamp.get("effect_tags_basis")
        if type(basis) is str and basis in _TAG_BASES:
            action["effect_tags_basis"] = basis
        else:
            _not_recorded(action, "effect_tags_basis", _STAMP_NO_BASIS)
        return
    gate = stamp.get("gate")
    _not_recorded(action, "effect_tags",
                  _STAMP_NO_CLASS_COMMAND if (type(gate) is str and gate == "command")
                  else _STAMP_NO_CLASS)


# Why a gate field is absent on an effect row, keyed by the stamp's pass path.
_NO_CHANNEL = (
    "the decision carries no resolving channel: it was resolved before resolutions "
    "recorded their surface, by a path that names none, or it is not in this vault")
_NO_CARD = {
    "auto_allow": "the governor scored the call in the allow band; no card was posted",
    "read_only_command": "the command was provably read-only; no card was posted",
    # P21: the MCP gate's own no-card pass.
    "read_only_hint": ("the MCP server marked the tool read-only, the tool's pinned "
                       "definition was trusted, and nothing else classified the "
                       "call; no card was posted"),
}
_AUTO_VIA = {"auto_allow": "auto:allow", "read_only_command": "auto:read_only_command",
             "standing_approval": "auto:standing_approval",
             "read_only_hint": "auto:read_only_hint",
             "session_trust": "auto:session_trust"}
# Passes on a STANDING grant: no card for this call, and the grant keeps no id.
_STANDING_PASSES = ("standing_approval", "session_trust")
_PASS_NO_DECISION = {
    "standing_approval": "a standing approval keeps no decision id",
    "session_trust": "an MCP session trust keeps no decision id",
    "resume_bridge": ("the one-shot resume bridge that let this call run carries no "
                      "decision id (minted before bridges recorded one)"),
    "approve_once": ("the approve-once pass recorded no decision id (a pass recorded "
                     "before the command gate kept the decision)"),
}
_NO_DECISION_NO_CHANNEL = ("the channel is recorded per decision, and this pass does "
                           "not name its decision")
_STAMP_NO_VERDICT = "the gate stamp names no verdict"
_STAMP_NO_PASS = "the gate stamp names no pass path"


def _apply_gate(group: dict, stamp: Optional[dict], channels: dict) -> None:
    """Fill the effect row's gate group from the gate stamp: the verdict AS SCORED,
    the decision id the pass consumed, and the channel that decision was resolved on
    (joined by exact decision id). Nothing is inferred: an unstamped entry, a pass
    that keeps no decision id, or a decision whose channel nobody recorded is named
    as not recorded (DEC-27)."""
    if stamp is None:
        for f in ("verdict", "decision_id", "resolved_via"):
            _not_recorded(group, f, _NO_GATE_STAMP)
        return
    if stamp.get("passed_via") == CODE_REGISTERED:
        group["passed_via"] = CODE_REGISTERED
        for f in ("verdict", "decision_id", "resolved_via"):
            _not_recorded(group, f, _CODE_REGISTERED_REASON)
        return
    verdict = stamp.get("verdict")
    if type(verdict) is str and verdict:
        group["verdict"] = verdict
    else:
        _not_recorded(group, "verdict", _STAMP_NO_VERDICT)
    kind = stamp.get("gate")
    group["gate_kind"] = kind if type(kind) is str else None
    passed = stamp.get("passed_via")
    passed = passed if type(passed) is str else ""
    group["passed_via"] = passed or None
    did = stamp.get("decision_id")
    if type(did) is str and did:
        group["decision_id"] = did
        via = channels.get(did)
        if type(via) is str and via:
            group["resolved_via"] = via
        else:
            _not_recorded(group, "resolved_via", _NO_CHANNEL)
    elif passed in _NO_CARD:
        _not_applicable(group, "decision_id", _NO_CARD[passed])
        group["resolved_via"] = _AUTO_VIA[passed]
    elif passed in _STANDING_PASSES:
        _not_recorded(group, "decision_id", _PASS_NO_DECISION[passed])
        group["resolved_via"] = _AUTO_VIA[passed]
    elif passed in _PASS_NO_DECISION:
        _not_recorded(group, "decision_id", _PASS_NO_DECISION[passed])
        _not_recorded(group, "resolved_via", _NO_DECISION_NO_CHANNEL)
    else:
        _not_recorded(group, "decision_id", _STAMP_NO_PASS)
        _not_recorded(group, "resolved_via", _STAMP_NO_PASS)


def _lane(eid: str) -> str:
    return "quick" if eid.startswith("quick_") else "workflow"


def _effect_row(audit: dict, channels: Optional[dict] = None) -> LedgerRow:
    """One `event_kind="effect"` row from an action-audit entry (the AC1 spine)."""
    eid = str(audit.get("execution_id") or "")
    oid = audit.get("objective_id")
    action = str(audit.get("action") or "")
    ts = norm_ts(str(audit.get("ts") or ""))
    masked, digest = mask_and_digest_params(audit.get("params") or {})
    lane = _lane(eid)
    action_group = {"tool": action, "effect_tags": [],
                    "params_digest": digest, "host": _derive_host(masked)}
    _apply_effect_tags(action_group, _gate_stamp(audit))
    gate_group = {"verdict": None, "decision_id": None,
                  "resolution_class": None, "resolved_via": None}
    _apply_gate(gate_group, _gate_stamp(audit), channels or {})
    return LedgerRow(
        source_kind="action_audit",
        source_id=f"{eid}|{oid}|{action}|{ts}",
        ts=ts, event_kind="effect",
        actor={"lane": lane, "origin": _derive_origin(eid),
               "run_ref": {"execution_id": eid, "activity_id": None,
                           "scroll_id": None, "shadow_id": None}},
        action=action_group,
        gate=gate_group,
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


def _enrich_with_receipt(row: LedgerRow, effects: dict, oid: str) -> bool:
    """Join a run's per-OBJECTIVE receipt onto an effect row -> verification +
    evidence fingerprint. The verification is the ONE mint's value
    (``verification_badge.mint_effect``, DEC-43): verified, claimed, or measured
    for the S4 shadow meter's record-only measurement -- NEVER conflated with the
    criteria N-of-M. A receipt entry the mint could not read is named not recorded
    (DEC-27), never left blank. Returns True iff the objective's receipt was
    attached (so the caller attaches it to ONE effect per objective, not all)."""
    from systemu.runtime import verification_badge as vb
    e = effects.get(oid) if type(effects) is dict else None
    if type(e) is not vb.EffectBadge:
        return False
    if e.state == vb.NOT_RECORDED:
        _not_recorded(row.outcome, "verification", _RECEIPT_ENTRY_UNREADABLE)
        return True
    eid = (row.actor.get("run_ref") or {}).get("execution_id", "")
    row.outcome["verification"] = e.state
    if e.state == vb.MEASURED:
        # The measurement credits nothing; the would-credit bit rides beside it so
        # the measurement is not lost.
        row.outcome["would_credit"] = e.would_credit
    row.outcome["evidence_ref"] = f"{eid}#{oid}"
    row.outcome["evidence_fingerprint"] = _receipt_fingerprint(oid, e)
    return True


def _receipt_fingerprint(oid: str, effect) -> str:
    """A stable digest that survives IMPL-12 GC of the evidence body (AC5). Only
    fields the receipt store actually persists (host was always None -- dropped),
    taken from the minted effect AS READ. Shared by the effect row's join and the
    committed_effect row, so the two rows for one objective carry the SAME
    fingerprint (a witnessed link, not a guess)."""
    return _digest({"oid": oid, "method": effect.method,
                    "stamped_at": effect.stamped_at})


def receipt_fingerprint(effect) -> str:
    """The ``outcome_evidence_fingerprint`` an export carries for this minted
    effect -- so the receipt panel can show the operator the value to look for."""
    return _receipt_fingerprint(effect.objective_id, effect)


#: DEC-13 vocabulary beside verified / claimed / handoff: a receipt its writer marked
#: as the S4 shadow meter's record-only measurement. Spelled by the mint.
from systemu.runtime.verification_badge import MEASURED  # noqa: E402

_RECEIPT_ENTRY_UNREADABLE = "the run's receipt entry for this objective could not be read"
_RECEIPT_RECORD_UNREADABLE = "the run's receipt record could not be read"


# ── committed_effect rows (IMPL-7 set, from the durable receipts) ─────────────

_NORM_TS_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_RECEIPT_NO_TOOL = ("the durable receipt does not name the tool; the effect row with "
                    "the same evidence_ref and evidence_fingerprint does")
_RECEIPT_NO_CLASS = "the durable receipt does not record an effect class"
_RECEIPT_NO_PARAMS = "the durable receipt carries no call parameters"
_COMMITTED_GATE_ELSEWHERE = ("a committed effect is not a gated call; the gate outcome "
                             "is on the effect row with the same evidence_ref")


# ── gate_decision rows (the operator's decided gate cards) ───────────────────

_DECIDED = ("resolved", "consumed", "expired")
_CARD_NO_RUN = "the card carries no run coordinates"
_CARD_NO_TOOL_COMMAND = "a command card records the command signature, not the shell tool"
_CARD_NO_TOOL = "the card names no tool"
_CARD_NO_CLASS_COMMAND = "the command gate scores the command text, not an effect class"
_CARD_NO_CLASS = "the card records no effect class"
_CARD_NO_PARAMS = ("the card keeps a fingerprint of the UNMASKED arguments, which is "
                   "not exported")
_CARD_NO_VERDICT = "the card records no verdict (only tool and command cards stamp one)"
_CARD_EXPIRED = "nobody resolved it: the card expired"
_CARD_NOT_EFFECT = "a gate decision is not an effect; verification is on effect rows"
_FREE_TEXT = "answered (free text, not exported)"
_LEGACY_CONSUMED = ("consumed before consumption was recorded separately; this card's "
                    "resolved_at (the row's ts) is its consumption time")


def _read_decisions(vault, execution_id=None) -> "list[dict]":
    """Every persisted decision as a plain dict. The vault's own decision API when it
    has one (backend-aware); else the file layout under ``.root``; else none. A
    ``execution_id`` keeps the cards whose context names that run."""
    out: "list[dict]" = []
    if hasattr(vault, "load_index") and hasattr(vault, "get_decision"):
        try:
            headers = vault.load_index("decisions") or []
        except Exception:
            headers = []
        for h in headers:
            did = h.get("id") if isinstance(h, dict) else None
            if type(did) is not str:
                continue
            try:
                d = vault.get_decision(did)
            except Exception:
                continue
            out.append(d.to_dict() if hasattr(d, "to_dict") else d)
    else:
        root = getattr(vault, "root", None)
        if root is None:
            return []
        from pathlib import Path as _P
        ddir = _P(root) / "decisions"
        if not ddir.is_dir():
            return []
        for p in sorted(ddir.glob("*.json")):
            if p.name == "index.json":
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(d, dict):
                out.append(d)
    if execution_id:
        out = [d for d in out if isinstance(d.get("context"), dict)
               and d["context"].get("execution_id") == execution_id]
    return [d for d in out if isinstance(d, dict)]


def _resolution_channels(vault, decisions: "list[dict]") -> dict:
    """``{decision_id: resolved_via}`` from the witnesses that exist, strongest
    first: the channel the resolving surface stamped on the card in the same save
    (``context["resolved_via"]``: dashboard / cli / the phone bridge's channel /
    auto:policy); for a card resolved before that stamp existed, the phone bridge's OK
    rows in ``messaging/resolve_audit.jsonl`` and ``resolved_by == "auto_policy"`` on
    a born-resolved policy auto-grant. Every other decision's channel is unrecorded
    and stays so."""
    out: dict = {}
    stamped: set = set()
    for d in decisions:
        ctx = d.get("context")
        did = d.get("id")
        if type(did) is not str or type(ctx) is not dict:
            continue
        via = ctx.get("resolved_via")
        if type(via) is str and via:
            out[did] = via
            stamped.add(did)
        elif ctx.get("resolved_by") == "auto_policy":
            out[did] = "auto:policy"
    root = getattr(vault, "root", None)
    if root is None:
        return out
    from pathlib import Path as _P
    p = _P(root) / "messaging" / "resolve_audit.jsonl"
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if type(r) is not dict or r.get("outcome") != "OK":
            continue
        did, ch = r.get("decision_id"), r.get("channel")
        if type(did) is str and did and type(ch) is str and ch and did not in stamped:
            out[did] = ch
    return out


def _decision_outcome(d: dict) -> str:
    status = d.get("status")
    if status == "expired":
        return "expired"
    choice, options = d.get("choice"), d.get("options")
    if type(choice) is str and type(options) is list and choice in options:
        return choice
    return _FREE_TEXT


def _decision_row(d: dict, channels: dict) -> Optional[LedgerRow]:
    """One ``event_kind="gate_decision"`` row for a DECIDED gate card (resolved,
    consumed or expired; an open card is not a decision yet). ts is the persisted
    ``resolved_at`` -- when the operator decided. A consumed card also carries
    ``consumed_at``; a card consumed before that was recorded separately had its
    ``resolved_at`` overwritten by the consumption, and says so.
    Only structured, fixed-vocabulary fields are exported: never the title, body,
    inspect text, command, argument preview or a free-text answer."""
    ctx = d.get("context")
    did = d.get("id")
    status = d.get("status")
    if type(ctx) is not dict or ctx.get("kind") != "gate" or type(did) is not str:
        return None
    if type(status) is not str or status not in _DECIDED:
        return None
    raw_ts = d.get("resolved_at")
    ts = norm_ts(raw_ts) if type(raw_ts) is str else ""
    if not _NORM_TS_RE.match(ts):
        return None
    gate_type = ctx.get("gate_type") if type(ctx.get("gate_type")) is str else None
    eid = ctx.get("execution_id") if type(ctx.get("execution_id")) is str else ""
    actor: dict = {"run_ref": {
        "execution_id": eid or None,
        "activity_id": ctx.get("activity_id") if type(ctx.get("activity_id")) is str else None,
        "scroll_id": None,
        "shadow_id": ctx.get("shadow_id") if type(ctx.get("shadow_id")) is str else None}}
    if eid:
        actor["lane"], actor["origin"] = _lane(eid), _derive_origin(eid)
    else:
        _not_recorded(actor, "lane", _CARD_NO_RUN)
        _not_recorded(actor, "origin", _CARD_NO_RUN)
    is_command = gate_type == "command"
    action: dict = {"tool": None, "effect_tags": None, "params_digest": None, "host": None}
    tool = ctx.get("tool_name")
    if type(tool) is str and tool:
        action["tool"] = tool
    else:
        _not_recorded(action, "tool", _CARD_NO_TOOL_COMMAND if is_command else _CARD_NO_TOOL)
    tags = ctx.get("effect_tags")
    if type(tags) is list and tags and all(type(t) is str and t for t in tags):
        action["effect_tags"] = sorted(set(tags))
        action["effect_tags_basis"] = "scored"
    else:
        _not_recorded(action, "effect_tags",
                      _CARD_NO_CLASS_COMMAND if is_command else _CARD_NO_CLASS)
    _not_recorded(action, "params_digest", _CARD_NO_PARAMS)
    gate: dict = {"verdict": None, "decision_id": did, "resolution_class": None,
                  "resolved_via": None, "gate_kind": gate_type}
    verdict = ctx.get("verdict")
    if type(verdict) is str and verdict:
        gate["verdict"] = verdict
    else:
        _not_recorded(gate, "verdict", _CARD_NO_VERDICT)
    rc = ctx.get("resolution_class")
    gate["resolution_class"] = rc if type(rc) is str else None
    if status == "expired":
        _not_applicable(gate, "resolved_via", _CARD_EXPIRED)
    elif type(channels.get(did)) is str and channels.get(did):
        gate["resolved_via"] = channels[did]
    else:
        _not_recorded(gate, "resolved_via", _NO_CHANNEL)
    outcome: dict = {"status": _decision_outcome(d), "decision_status": status,
                     "verification": None, "evidence_ref": None,
                     "evidence_fingerprint": None, "criteria_met": None,
                     "criteria_total": None, "criteria": []}
    if status == "consumed":
        consumed = ctx.get("consumed_at")
        if type(consumed) is str and _NORM_TS_RE.match(norm_ts(consumed)):
            outcome["consumed_at"] = norm_ts(consumed)
        else:
            _not_recorded(outcome, "consumed_at", _LEGACY_CONSUMED)
    for f in ("verification", "evidence_ref", "evidence_fingerprint"):
        _not_applicable(outcome, f, _CARD_NOT_EFFECT)
    return LedgerRow(source_kind="decision", source_id=did, ts=ts,
                     event_kind="gate_decision", actor=actor, action=action,
                     gate=gate, outcome=outcome,
                     raw_beside={"params": None, "detail": None,
                                 "criteria_text": [], "evidence_body": None})


def _committed_effect_rows(eid: str, effects: Any, *, since: Optional[str],
                           until: Optional[str]):
    """One ``event_kind="committed_effect"`` row per objective whose durable receipt
    WITNESSES a live, confirmed effect: the mint says ``verified`` (DEC-13: the only
    credit bit) AND the writer marked it live (``live_marked``: the explicit
    statement that it was not the S4 meter's record-only measurement, which IMPL-7
    excludes; a receipt written before the flag existed says neither, so it yields
    no row) AND a non-blank ``detail`` (IMPL-7's own filter) AND a parseable
    ``stamped_at`` (the row's time; a confirmed verification always stamps one).

    The receipt's ``detail`` is a human note, so it rides ``raw_beside`` only
    (MASK-redacted, never truncated -- DEC-31) and is never exported."""
    from systemu.runtime import verification_badge as vb
    if type(effects) is not dict or not eid:
        return
    from systemu.runtime.external_verifier import _mask_evidence
    for key in sorted(effects, key=str):
        e = effects[key]
        if type(e) is not vb.EffectBadge:
            continue
        if e.state != vb.VERIFIED or e.live_marked is not True:
            continue
        detail = e.detail
        if type(detail) is not str or not detail.strip():
            continue
        stamped = e.stamped_at
        ts = norm_ts(stamped) if type(stamped) is str else ""
        if not _NORM_TS_RE.match(ts):
            continue
        if (since and ts < since) or (until and ts > until):
            continue
        oid = str(key)
        action: dict = {"tool": None, "effect_tags": None,
                        "params_digest": None, "host": None}
        _not_recorded(action, "tool", _RECEIPT_NO_TOOL)
        _not_recorded(action, "effect_tags", _RECEIPT_NO_CLASS)
        _not_recorded(action, "params_digest", _RECEIPT_NO_PARAMS)
        gate: dict = {"verdict": None, "decision_id": None,
                      "resolution_class": None, "resolved_via": None}
        for f in ("verdict", "decision_id", "resolved_via"):
            _not_applicable(gate, f, _COMMITTED_GATE_ELSEWHERE)
        yield LedgerRow(
            source_kind="committed_effect",
            source_id=f"{eid}#{oid}",
            ts=ts, event_kind="committed_effect",
            actor={"lane": _lane(eid), "origin": _derive_origin(eid),
                   "run_ref": {"execution_id": eid, "activity_id": None,
                               "scroll_id": None, "shadow_id": None}},
            action=action,
            gate=gate,
            outcome={"status": "committed", "verification": e.state,
                     "evidence_ref": f"{eid}#{oid}",
                     "evidence_fingerprint": _receipt_fingerprint(oid, e),
                     "criteria_met": None, "criteria_total": None, "criteria": []},
            raw_beside={"params": None, "detail": _mask_evidence(detail),
                        "criteria_text": [], "evidence_body": None},
        )


def iter_rows(vault, *, data_dir=None, execution_id=None, since_ts=None, until_ts=None):
    """Project the persisted sources into LedgerRows (RUL-7 — reads only). The
    effect-row spine is action-audit successes (AC1: one row per side-effectful
    execution); each is MASK-redacted (AC4). A run's receipt is per-OBJECTIVE, so it
    enriches the FIRST effect row of that objective only — "verified" rows count
    verified objectives, never fanned onto every action. Each effect row carries the
    effect classes its gate stamp recorded, or names them as not recorded.

    Then, per run named in the audit, one ``committed_effect`` row per witnessed
    live, confirmed receipt (see :func:`_committed_effect_rows`), windowed on the
    receipt's own ``stamped_at``; then one ``gate_decision`` row per decided gate card
    (see :func:`_decision_row`), windowed on its ``resolved_at``. Effect rows also
    carry the gate stamp's verdict / decision id, and the resolving channel joined by
    exact decision id. Best-effort: a missing/corrupt source degrades to
    fewer rows and never raises (a non-file backend raises loudly instead of a
    silently-empty ledger — see _read_action_audit)."""
    from systemu.runtime import verification_badge as vb
    since = norm_ts(str(since_ts)) if since_ts else None
    until = norm_ts(str(until_ts)) if until_ts else None
    # ONE unbounded read, windowed per row below (same inclusive normalized-stamp
    # comparison as before). The unbounded set is what names the runs whose
    # committed effects are looked up: a run whose tool call landed just before the
    # window can still have its effect CONFIRMED inside it.
    audits = _read_action_audit(vault, execution_id=execution_id)
    decisions = _read_decisions(vault, execution_id=execution_id)
    channels = _resolution_channels(vault, decisions)
    _receipt_cache: dict = {}

    def _record(eid):
        """The run's receipt record, minted once per run (DEC-43: the one mint)."""
        if eid not in _receipt_cache:
            try:
                _receipt_cache[eid] = vb.effect_record(eid, data_dir=data_dir)
            except Exception:
                _receipt_cache[eid] = vb.EffectRecord(
                    record=vb.RECORD_UNREADABLE, reason=_RECEIPT_RECORD_UNREADABLE)
        return _receipt_cache[eid]

    def _receipts(eid):
        return _record(eid).by_objective()

    _receipted: set = set()                # (eid, oid) already carrying the objective's receipt
    _runs: list = []                       # execution ids of THIS vault's audit, first-seen order
    _seen_runs: set = set()
    for a in audits:
        if not isinstance(a, dict):
            continue
        eid = a.get("execution_id")
        if type(eid) is str and eid and eid not in _seen_runs:
            _seen_runs.add(eid)
            _runs.append(eid)
        if not a.get("success"):
            continue                       # 2a: effects COMMITTED (not attempted-failed)
        _ts = norm_ts(str(a.get("ts", "")))
        if (since and _ts < since) or (until and _ts > until):
            continue
        row = _effect_row(a, channels)
        oid = str(a.get("objective_id"))
        key = (eid, oid)
        if type(eid) is str and eid and _record(eid).record == vb.RECORD_UNREADABLE:
            # DEC-27: an unreadable record is named, never a blank cell that reads
            # as "no receipt".
            _not_recorded(row.outcome, "verification",
                          f"{_RECEIPT_RECORD_UNREADABLE}: {_record(eid).reason}")
        elif key not in _receipted and _enrich_with_receipt(row, _receipts(eid), oid):
            _receipted.add(key)
        else:
            # DEC-13: an effect with no receipt attached (no record, no entry for
            # its objective, or the objective's receipt already rides an earlier
            # effect) is the mint's "claimed" -- never a blank cell. It carries no
            # evidence ref/fingerprint: nothing independent confirmed it.
            row.outcome["verification"] = vb.mint_unreceipted(oid).state
        yield row
    # Committed effects are looked up only for runs this vault's audit names, so an
    # export never carries another vault's receipts from a shared data dir.
    for eid in _runs:
        yield from _committed_effect_rows(eid, _receipts(eid), since=since, until=until)
    for d in decisions:
        drow = _decision_row(d, channels)
        if drow is None or (since and drow.ts < since) or (until and drow.ts > until):
            continue
        yield drow


def project(vault, **filters) -> List[LedgerRow]:
    """Materialized, sorted projection — total order ``(ts, source_kind, source_id)``
    (fixed-width ts ⇒ lexicographic == chronological)."""
    rows = list(iter_rows(vault, **filters))
    rows.sort(key=lambda r: (r.ts, r.source_kind, r.source_id))
    return rows
