"""R-P3b — the operator-facing compliance export (MASTER-SPEC Part II §6).

``runtime/ledger.py`` holds a finished, frozen, byte-stable pair of exporters
(:func:`ledger.export_csv` / :func:`ledger.export_jsonl`) that, until this module,
had **zero callers** — the compliance artifact was unreachable. This module is the
only thing between them and Settings.

It deliberately does FOUR things and no more:

1. **Range-filters** the projection into a CLOSED ``[since, until]`` window. The
   window matters: the export itself writes a ledger row (below), so an open-ended
   range would never re-export byte-identically and §6/AC3 could not hold. An
   unparseable bound raises :class:`ValueError` — it is NEVER silently widened to
   "everything" (that would accept the operator's input and do something else).
2. **Previews** the exact artifact before it exists (:func:`preview`). An export is
   a file leaving the system; the operator confirms content + destination first.
3. **Writes** the artifact plus a companion ``.manifest.json``
   (:func:`write_export`).
4. **Records** the export as a ledger row, via the EXISTING single audit writer
   ``runtime.audit_log.append_action`` — RUL-7 holds, ``ledger.py`` stays a pure
   projection and this module introduces no new durable writer of its own.

**S-1 is NOT activated here.** ``canonical_bytes``/``compute_row_hash`` remain
uninvoked and ``seq``/``chain_day``/``prev_hash``/``row_hash`` still serialize as
null. Wiring a UI button must not switch the chain on as a side effect.

**The always-blank-column problem.** 11 of the frozen 25 columns have no producer in
this build (see :data:`UNPOPULATED_COLUMNS`). An empty ``gate_verdict`` cell reads to
an auditor as "no approval was required", when the truth is "this projection does not
join gate decisions yet". The column list is FROZEN and its byte-stability is pinned,
so the honest fix is to ANNOTATE rather than omit: the manifest — and the Settings
preview — name every unpopulated column and why. A blank cell is never presented as
a finding.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.runtime import ledger

# ── formats ──────────────────────────────────────────────────────────────────

FORMATS = ("csv", "jsonl")
_EXPORTERS = {"csv": ledger.export_csv, "jsonl": ledger.export_jsonl}

EXPORT_ACTION = "compliance_export"     # the audit `action` name for the export row
EXPORT_DIRNAME = "exports"              # <vault root>/exports/


# ── the columns this build cannot fill (annotate, never silently omit) ───────
#
# Two-way pinned by tests/test_rp3b_compliance_export.py against a MAXIMALLY
# populated real projection: if a producer lands, or a new column goes always-blank,
# the pin fails rather than the manifest going quietly stale.

UNPOPULATED_COLUMNS: Dict[str, str] = {
    "seq":            "reserved for the S-1 hash chain — the chain writer is not built",
    "chain_day":      "reserved for the S-1 hash chain — the chain writer is not built",
    "prev_hash":      "reserved for the S-1 hash chain — the chain writer is not built",
    "row_hash":       "reserved for the S-1 hash chain — the chain writer is not built",
    "action_effect_tags":
        "the projection does not classify effect tags yet (ledger._effect_row emits [])",
    "gate_verdict":
        "gate/approval decisions are not joined into this projection yet — blank does "
        "NOT mean no approval was required",
    "gate_decision_id":   "no gate join yet (see gate_verdict)",
    "gate_resolved_via":  "no gate join yet — the dashboard/telegram channel stamp is unbuilt",
    "criteria_met":       "the DEC-13 acceptance-criteria authoring path is unbuilt",
    "criteria_total":     "the DEC-13 acceptance-criteria authoring path is unbuilt",
    "undo_ref":           "undo handles are never populated in this build",
}

# Present but carrying one constant value in this build — not blank, so it cannot be
# caught by the blank-column derivation, and it would otherwise read as a finding.
CONSTANT_COLUMNS: Dict[str, str] = {
    "undo_kind": "always the literal 'none' — undo handles are never populated",
    "event_kind": "always 'effect' — only action-audit effect rows are projected",
    "source_kind": "always 'action_audit' — decision / committed_effect rows are unprojected",
}


# ── range bounds ─────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_bound(raw: Any, *, end_of_day: bool) -> Optional[str]:
    """Parse one operator-supplied range bound into a fixed-width normalized stamp.

    Accepts ``YYYY-MM-DD`` (widened to the start or end of that UTC day, per
    ``end_of_day``) or a full ISO-8601 stamp. Empty/None → ``None`` (unbounded on
    that side). ANYTHING ELSE RAISES ``ValueError`` — an export must never quietly
    ignore a range the operator typed and hand back a different range's data.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        d = d.replace(tzinfo=timezone.utc)
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
        return ledger.norm_ts(d.isoformat())
    except ValueError:
        pass
    # Parse DIRECTLY rather than inferring from a norm_ts round-trip: norm_ts returns
    # an unparseable value unchanged, so "notatimeZ" would round-trip identically and
    # a round-trip test would wave it through as a valid bound.
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"unrecognised date {raw!r} — use YYYY-MM-DD or a full ISO-8601 UTC stamp"
        ) from None
    return ledger.norm_ts(s)


def _resolve_window(since: Any, until: Any, *, now: Optional[datetime] = None) -> "tuple[Optional[str], str]":
    """(since_ts, until_ts). ``until`` defaults to NOW so the window is always CLOSED:
    the export's own ledger row is stamped after this bound and therefore never lands
    inside the range it is reporting on (which is what keeps re-export byte-stable)."""
    since_ts = parse_bound(since, end_of_day=False)
    until_ts = parse_bound(until, end_of_day=True)
    if until_ts is None:
        until_ts = ledger.norm_ts((now or _now()).isoformat())
    if since_ts is not None and since_ts > until_ts:
        raise ValueError(f"empty range — 'from' {since_ts} is after 'to' {until_ts}")
    return since_ts, until_ts


# ── blank-column derivation (truthful about THIS file, not about the build) ──

def blank_columns(rows: List[ledger.LedgerRow]) -> List[str]:
    """Columns whose cell is empty in EVERY supplied row. Derived from the real CSV
    cells (never from a hand-kept list), so it cannot drift from the artifact."""
    if not rows:
        return list(ledger._COLUMNS)
    nonblank = set()
    for r in rows:
        for name, cell in zip(ledger._COLUMNS, ledger._csv_cells(r)):
            if cell != "":
                nonblank.add(name)
    return [c for c in ledger._COLUMNS if c not in nonblank]


# ── preview ──────────────────────────────────────────────────────────────────

def _project_and_describe(vault, *, fmt, since, until, data_dir, dest_dir, now):
    """Project the window ONCE and describe exactly those rows.

    The single projection is the point. Both :func:`preview` and :func:`write_export`
    go through here, so the counts that get attested to and the rows that get
    serialized are the SAME list object — a reported ``row_count`` cannot drift from
    the artifact, because there is no second projection to drift against. (An earlier
    shape ran two projections and re-derived the count from the written rows; that was
    correct in practice, but "two paths that happen to agree" is the exact defect class
    a false attestation on a compliance artifact comes from.)

    Returns ``(rows, summary)``.
    """
    fmt = _check_fmt(fmt)
    stamp = now or _now()
    since_ts, until_ts = _resolve_window(since, until, now=stamp)
    rows = ledger.project(vault, data_dir=data_dir, since_ts=since_ts, until_ts=until_ts)
    dest = _dest_dir(vault, dest_dir)
    name = _filename(stamp, fmt)
    blank = blank_columns(rows)
    return rows, {
        "format": fmt,
        "since_ts": since_ts,
        "until_ts": until_ts,
        "row_count": len(rows),
        "column_count": len(ledger._COLUMNS),
        "columns": list(ledger._COLUMNS),
        "destination_dir": str(dest),
        "filename": name,
        "destination_path": str(dest / name),
        "manifest_path": str(dest / (name + ".manifest.json")),
        "blank_columns": blank,
        "unpopulated_columns": {c: UNPOPULATED_COLUMNS[c]
                                for c in ledger._COLUMNS if c in UNPOPULATED_COLUMNS},
        "constant_columns": dict(CONSTANT_COLUMNS),
        "excluded": _EXCLUDED,
    }


def preview(vault, *, fmt: str = "csv", since: Any = None, until: Any = None,
            data_dir=None, dest_dir=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the operator must see BEFORE an export file exists.

    Projects the real range and reports the real row count, the real destination
    path, and the real blank/unpopulated columns. Raises loudly (``ValueError`` for a
    bad range or format, ``NotImplementedError`` for a backend with no full-scan API)
    rather than previewing an export that would be empty for a reason other than
    "nothing happened".
    """
    return _project_and_describe(vault, fmt=fmt, since=since, until=until,
                                 data_dir=data_dir, dest_dir=dest_dir, now=now)[1]


_EXCLUDED = [
    "raw_beside — every raw parameter value, free-text detail, and evidence body "
    "(the un-chained PII store) is stripped by both exporters",
    "credential values — parameters are MASK-redacted before they are digested, and "
    "only the digest is exported",
    "message/document contents — only a sha256 params_digest and a bare destination "
    "hostname ever reach a cell",
]


# ── write ────────────────────────────────────────────────────────────────────

def write_export(vault, *, fmt: str = "csv", since: Any = None, until: Any = None,
                 data_dir=None, dest_dir=None, now: Optional[datetime] = None,
                 record_event: bool = True) -> Dict[str, Any]:
    """Produce the export on disk and return the preview dict plus what was written.

    Order matters: project → write artifact → write manifest → THEN record the export
    event. The event row is stamped after ``until_ts``, so re-exporting the same
    explicit range returns byte-identical bytes (§6/AC3) even though the ledger grew.
    """
    # ONE projection. `result` describes precisely the rows in `payload` because they
    # come from the same call — the manifest's row_count is a property of the artifact,
    # not a second opinion about it.
    rows, result = _project_and_describe(vault, fmt=fmt, since=since, until=until,
                                         data_dir=data_dir, dest_dir=dest_dir, now=now)
    payload = _EXPORTERS[result["format"]](rows)

    dest = Path(result["destination_dir"])
    dest.mkdir(parents=True, exist_ok=True)
    out = Path(result["destination_path"])
    out.write_bytes(payload)

    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "artifact": result["filename"],          # filename only — the absolute path
                                                 # carries the OS account name, which
                                                 # has no business travelling with a
                                                 # file handed to an auditor
        "format": result["format"],
        "sha256": digest,
        "byte_count": len(payload),
        "row_count": result["row_count"],
        "range": {"since_ts": result["since_ts"], "until_ts": result["until_ts"]},
        "columns": result["columns"],
        "columns_never_populated_by_this_build": result["unpopulated_columns"],
        "columns_constant_in_this_build": result["constant_columns"],
        "columns_blank_in_this_export": result["blank_columns"],
        "excluded_from_this_export": _EXCLUDED,
        "hash_chain": "not activated in this build — seq/chain_day/prev_hash/row_hash "
                      "are reserved and serialize as null",
        "reading_note": "A blank cell in a never-populated column means this build has "
                        "no producer for it — NOT that the event lacked that property.",
    }
    Path(result["manifest_path"]).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result.update({"sha256": digest, "byte_count": len(payload), "manifest": manifest})
    if record_event:
        result["event_recorded"] = _record_export_event(vault, result)
    return result


def _record_export_event(vault, result: Dict[str, Any]) -> bool:
    """"the export event is itself a ledger row" (§6). Goes through the EXISTING single
    action-audit writer — no new durable writer is introduced.

    Best-effort by design: a produced-but-unlogged export is strictly better than
    losing an export the operator already holds. The caller surfaces the False.
    """
    from systemu.runtime import audit_log
    # FileVault (the dashboard's IVault adapter) forwards .root but NOT
    # append_action_audit — it has no __getattr__. Reach the raw Vault the same way
    # dashboard_state._resolve_project_root does.
    raw = getattr(vault, "_v", vault)
    try:
        audit_log.append_action(
            raw,
            execution_id=f"{ledger.EXPORT_EID_PREFIX}{result['filename']}",
            objective_id=0,
            action=EXPORT_ACTION,
            # No absolute path, no operator identity — the filename, the range, the
            # size and the content digest are what an auditor needs.
            params={"format": result["format"], "artifact": result["filename"],
                    "sha256": result["sha256"], "row_count": result["row_count"],
                    "since_ts": result["since_ts"], "until_ts": result["until_ts"]},
            success=True,
        )
        return True
    except Exception:
        return False


# ── helpers ──────────────────────────────────────────────────────────────────

def _check_fmt(fmt: str) -> str:
    f = str(fmt or "").strip().lower()
    if f not in _EXPORTERS:
        raise ValueError(f"unknown export format {fmt!r} — choose one of {list(FORMATS)}")
    return f


def _filename(stamp: datetime, fmt: str) -> str:
    return f"ledger-export-{stamp.strftime('%Y%m%dT%H%M%SZ')}.{fmt}"


def _dest_dir(vault, dest_dir) -> Path:
    if dest_dir is not None:
        return Path(dest_dir)
    root = getattr(vault, "root", None)
    if root is None:
        raise NotImplementedError(
            "compliance export needs an explicit destination: this vault exposes no "
            ".root to default to (never guess where a compliance file lands)")
    return Path(root) / EXPORT_DIRNAME
