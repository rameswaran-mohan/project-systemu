"""Durable, DISPLAY-ONLY store for external-verification receipts.

A receipt is the ``ExternalEvidence`` produced when the daemon acts through a
connected tool and INDEPENDENTLY reads the effect back (R-A14a / §12A): a
``confirmed`` bit + the ``method`` (e.g. ``api_readback``) + a human ``detail``.
It is persisted live into ``context._external_evidence`` → the ExecutionSnapshot,
which is DELETED on completion — so a completed run has no durable receipt to show.
This store keeps a durable copy at ``<data_dir>/audit/exec_<eid>/receipts.json``
(co-located with the snapshot but NEVER deleted) so the UI can render a
verified/claimed badge after the run finishes.

SECURITY — this store is DISPLAY-ONLY. It is written best-effort ALONGSIDE (never
instead of) the live ``context._external_evidence`` write, and it is read by
NOTHING except the UI. The credit gate reads the live in-run evidence, so a
tampered/forged ``receipts.json`` can NEVER credit an effect. (It is deliberately
NOT ``action_audit`` — ``state_delta`` reads that store.) It stores only display
fields (objective_id / confirmed / method / detail / stamped_at) — no tokens, no
secrets (``detail`` is contractually non-secret).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()

#: The display fields kept on a durable receipt — no tokens/secrets.
_RECEIPT_FIELDS = ("objective_id", "confirmed", "method", "detail", "stamped_at")

#: An execution_id is daemon-generated (``quick_<hex>`` / ``exec_<…>`` / an MCP
#: derivation), but it lands in a filesystem path — sanitize defensively so a
#: separator/``..`` can never traverse out of the audit dir.
_UNSAFE_EID = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_eid(execution_id: str) -> str:
    eid = _UNSAFE_EID.sub("_", str(execution_id))
    return eid.replace("..", "_") or "unknown"


def _receipts_path(data_dir: Optional[Path], execution_id: str) -> Path:
    """``<data_dir or "data">/audit/exec_<eid>/receipts.json`` — mirrors the
    snapshot layout (``execution_snapshot._snapshot_path``) but a distinct,
    durable file that survives run completion. The eid is path-sanitized."""
    return Path(data_dir or "data") / "audit" / f"exec_{_safe_eid(execution_id)}" / "receipts.json"


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


def receipt_badges_for(execution_id: Optional[str], *,
                       data_dir: Optional[Path] = None) -> list:
    """Pure render-data for the verified/claimed badges (fold-in #3 / DEC-13): one
    dict per objective's receipt, sorted by objective_id. ``verified is True`` ⇒
    "Verified" (independently machine-checked) + the method; else "Claimed" (the
    tool reported it, not independently verified). An empty list ⇒ NO panel — a run
    with no external effect shows no badge, never a fabricated one. The badge is
    SEPARATE from cost/status chrome: it says the effect was verified, never that
    the run succeeded."""
    badges = []
    for oid, r in (read_receipts(execution_id, data_dir=data_dir) or {}).items():
        if not isinstance(r, dict):
            continue
        verified = r.get("confirmed") is True
        method = r.get("method") or ""
        badges.append({
            "objective_id": oid,
            "verified": verified,
            "label": "Verified" if verified else "Claimed",
            "method": method,
            "detail": r.get("detail") or "",
            "tooltip": (f"Independently machine-verified via {method} — receipts, not self-report"
                        if verified else
                        "Reported by the tool, not independently verified"),
        })

    def _key(b):
        try:
            return (0, int(b["objective_id"]))
        except Exception:
            return (1, 0)
    badges.sort(key=_key)
    return badges
