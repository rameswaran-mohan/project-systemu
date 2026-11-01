"""Durable, DISPLAY-ONLY store for external-verification receipts.

A receipt is the ``ExternalEvidence`` produced when the daemon acts through a
connected tool and INDEPENDENTLY reads the effect back (R-A14a / §12A): a
``confirmed`` bit + the ``method`` (e.g. ``api_readback``) + a human ``detail``.
It is persisted live into ``context._external_evidence`` → the ExecutionSnapshot,
which is DELETED on completion — so a completed run has no durable receipt to show.
This store keeps a durable copy at ``<audit data root>/audit/exec_<eid>/receipts.json``
(co-located with the snapshot but NEVER deleted) so the UI can render a
verified/claimed badge after the run finishes.

SECURITY — this store is DISPLAY-ONLY. It is written best-effort ALONGSIDE (never
instead of) the live ``context._external_evidence`` write, and it is read only
by display surfaces: the UI badges and the R-P3b action-ledger projection
(``runtime.ledger``), neither of which credits anything. The credit gate reads the
live in-run evidence, so a tampered/forged ``receipts.json`` can NEVER credit an
effect. (It is deliberately NOT ``action_audit`` — ``state_delta`` reads that
store.) It stores only display fields (objective_id / confirmed / method / detail /
stamped_at / shadow) — no tokens, no secrets (``detail`` is contractually
non-secret).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()

#: The display fields kept on a durable receipt — no tokens/secrets.
#: ``shadow`` (R-P3b) is the writer's explicit bool: True for the S4 shadow meter's
#: record-only measurement, False for a live verification. Absent on a receipt
#: written before it existed, which therefore says nothing either way.
_RECEIPT_FIELDS = ("objective_id", "confirmed", "method", "detail", "stamped_at",
                   "shadow")

#: An execution_id is daemon-generated (``quick_<hex>`` / ``exec_<…>`` / an MCP
#: derivation), but it lands in a filesystem path — sanitize defensively so a
#: separator/``..`` can never traverse out of the audit dir.
_UNSAFE_EID = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_eid(execution_id: str) -> str:
    eid = _UNSAFE_EID.sub("_", str(execution_id))
    return eid.replace("..", "_") or "unknown"


def _receipts_path(data_dir: Optional[Path], execution_id: str) -> Path:
    """``<audit data root>/audit/exec_<eid>/receipts.json`` — mirrors the snapshot
    layout (``execution_snapshot._snapshot_path``) but a distinct, durable file that
    survives run completion. The eid is path-sanitized.

    R-P3b: the root is ``execution_snapshot.audit_data_root(data_dir)`` -- the ONE
    resolver the snapshot and the table-payoff record use (an explicit ``data_dir``
    wins unchanged; otherwise ``<operating home>/data``). It used to be the relative
    ``"data"``, so a reader in another working directory (the CLI, an export) read a
    different tree than the daemon wrote."""
    from systemu.runtime.execution_snapshot import audit_data_root
    return (audit_data_root(data_dir) / "audit" / f"exec_{_safe_eid(execution_id)}"
            / "receipts.json")


def _project(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the display fields; coerce ``confirmed`` to a real bool so the
    badge branches on a clean value (fail-closed: anything non-True ⇒ claimed)."""
    out = {k: receipt.get(k) for k in _RECEIPT_FIELDS if k in receipt}
    out["confirmed"] = receipt.get("confirmed") is True
    return out


def write_receipt(execution_id: Optional[str], objective_id: Any,
                  receipt: Dict[str, Any], *, data_dir: Optional[Path] = None) -> None:
    """Merge one display receipt into a run's durable ``receipts.json`` (keyed by
    ``objective_id``). Best-effort + atomic (tmp+replace under a lock). A falsy
    ``execution_id`` (a call outside any run) or bad input is a NO-OP — display
    persistence must NEVER break a run."""
    if not execution_id or not isinstance(receipt, dict):
        return
    try:
        target = _receipts_path(data_dir, str(execution_id))
        with _lock:
            existing: Dict[str, Any] = {}
            if target.exists():
                try:
                    existing = json.loads(target.read_text(encoding="utf-8")) or {}
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            existing[str(objective_id)] = _project(receipt)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, target)
    except Exception:
        logger.debug("[Receipts] write_receipt failed (swallowed)", exc_info=True)


def read_receipts(execution_id: Optional[str], *,
                  data_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return ``{objective_id: receipt}`` for a run, or ``{}`` when absent/corrupt.
    Never raises."""
    if not execution_id:
        return {}
    try:
        target = _receipts_path(data_dir, str(execution_id))
        if not target.exists():
            return {}
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("[Receipts] read_receipts failed (swallowed)", exc_info=True)
        return {}


# ── the witnessed read (DEC-27: absent and unreadable are different answers) ──

READ_PRESENT = "present"
READ_ABSENT = "absent"
READ_UNREADABLE = "unreadable"

#: Parsed-record cache bound (the UI re-reads on every refresh tick).
_CACHE_MAX = 512
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[Tuple[int, int], Dict[str, Any]]] = {}


@dataclass(frozen=True)
class ReceiptRead:
    """One run's receipt record as read: ``present`` with its rows, ``absent``
    (the file does not exist), or ``unreadable`` with the reason."""
    status: str
    rows: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def read_receipts_witnessed(execution_id: Optional[str], *,
                            data_dir: Optional[Path] = None) -> ReceiptRead:
    """Read a run's durable receipts, saying WHICH of present / absent /
    unreadable it found. Only a missing file (ENOENT on the open itself) is
    absent; a permission error, a directory in the way, bytes that are not
    UTF-8, text that is not JSON or JSON that is not an object are each
    ``unreadable`` with a reason -- never folded into "no receipts". Never raises.

    :func:`read_receipts` keeps its own contract (``{}`` for both); the
    verification badge reads through this one (``runtime.verification_badge``).
    Parsed records are cached on the file's (mtime_ns, size), so a UI tick costs
    a stat plus, when the file changed, one small read."""
    if not execution_id:
        return ReceiptRead(status=READ_ABSENT)
    try:
        target = _receipts_path(data_dir, str(execution_id))
    except Exception as exc:
        return ReceiptRead(status=READ_UNREADABLE,
                           reason=f"the receipt path could not be resolved ({type(exc).__name__})")
    try:
        fh = open(target, "rb")
    except FileNotFoundError:
        return ReceiptRead(status=READ_ABSENT)
    except Exception as exc:
        return ReceiptRead(status=READ_UNREADABLE,
                           reason=f"the receipt file could not be opened ({type(exc).__name__})")
    try:
        with fh:
            st = os.fstat(fh.fileno())
            stamp = (st.st_mtime_ns, st.st_size)
            key = str(target)
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
            if hit is not None and hit[0] == stamp:
                return ReceiptRead(status=READ_PRESENT, rows=dict(hit[1]))
            raw = fh.read()
    except Exception as exc:
        return ReceiptRead(status=READ_UNREADABLE,
                           reason=f"the receipt file could not be read ({type(exc).__name__})")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ReceiptRead(status=READ_UNREADABLE, reason="the receipt file is not UTF-8 text")
    try:
        data = json.loads(text)
    except Exception:
        return ReceiptRead(status=READ_UNREADABLE, reason="the receipt file is not valid JSON")
    if type(data) is not dict:
        return ReceiptRead(status=READ_UNREADABLE,
                           reason="the receipt file does not hold a record per objective")
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = (stamp, data)
    return ReceiptRead(status=READ_PRESENT, rows=dict(data))


def receipt_badges_for(execution_id: Optional[str], *,
                       data_dir: Optional[Path] = None) -> list:
    """Per-objective render data for the verified/claimed/measured badges (fold-in
    #3 / DEC-13): one dict per objective's receipt, sorted by objective_id. A THIN
    adapter over the one mint (``runtime.verification_badge``, DEC-43): the state,
    label and tooltip are the mint's, never re-derived here. An empty list means
    no receipt is on record; an unreadable record is one ``not_recorded`` entry,
    never an empty list. The badge is SEPARATE from cost/status chrome: it says
    the effect was verified, never that the run succeeded."""
    from systemu.runtime import verification_badge as vb
    rec = vb.effect_record(execution_id if execution_id else None, data_dir=data_dir)
    if rec.record == vb.RECORD_UNREADABLE:
        v = vb.view(vb.RunBadge(state=vb.NOT_RECORDED, reason=rec.reason))
        return [{"objective_id": None, "state": v.state, "verified": False,
                 "label": v.label, "method": "", "detail": "", "tooltip": v.tooltip}]
    out = []
    for e in rec.effects:
        v = vb.view(e)
        out.append({
            "objective_id": e.objective_id,
            "state": v.state,
            "verified": v.state == vb.VERIFIED,
            "label": v.label,
            "method": vb.method_text(e.method) if e.method else "",
            "detail": vb.detail_text(e.detail),
            "tooltip": v.tooltip,
        })
    return out
