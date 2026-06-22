"""Operator-rejection store (v0.4.1-c).

When the operator dismisses a supervisor-published approval card, the
``pattern_signature`` (and / or ``dedup_key``) is recorded here so the
Intelligent Supervisor can consult the store before re-proposing the
same kind of intervention.

Why this matters: today's v0.4.0 supervisor remembers nothing about
operator preference.  If the operator dismisses an INJECT_REFLECTION
card for a tool that they know is fine, the supervisor proposes the
same reflection the next time the same failure shape appears.  This
module closes that gap.

Storage: JSON file at ``data/rejection_store.json``.  Atomic writes via
tmp-rename, same pattern as ``DepApprovalStore`` and ``AffinityLog``.

Auto-expiry: ``is_recently_rejected()`` filters by ``window_hours``
(default 48), so an operator's "no" decays over time and doesn't
permanently silence a useful intervention.

Operator visibility: every rejection writes an audit row to
``data/audit/rejections.jsonl`` — same shape as
``data/audit/expunged_lessons.jsonl`` from v0.4.0-a.  Operators can
review and (manually) revoke specific entries.

This module is **not gated** by ``intelligent_supervisor_enabled`` —
operator dismissals are always recorded, even when the supervisor is
off.  When the supervisor later turns on, the store is already
populated with the operator's preferences.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path("data") / "rejection_store.json"
_DEFAULT_AUDIT_PATH = Path("data") / "audit" / "rejections.jsonl"
_DEFAULT_WINDOW_HOURS = 48


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Rejection:
    pattern_signature: str
    first_rejected_at: str
    reject_count:      int
    last_action:       Optional[str] = None
    last_dedup_key:    Optional[str] = None
    reason:            Optional[str] = None


class RejectionStore:
    """JSON-backed log of operator-dismissed supervisor proposals.

    Keyed by ``pattern_signature`` (the v0.4.0-a deterministic signature).
    When a signature has any record within the window, the supervisor
    treats it as "operator said no recently — back off".
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        audit_path: Optional[Path] = None,
    ):
        self.path = Path(path or _DEFAULT_PATH)
        self.audit_path = Path(audit_path or _DEFAULT_AUDIT_PATH)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def record_rejection(
        self,
        pattern_signature: str,
        *,
        dedup_key:   Optional[str] = None,
        action:      Optional[str] = None,
        reason:      str = "operator_dismissed",
    ) -> Rejection:
        """Note that the operator dismissed a proposal carrying this signature.

        Idempotent for "newly seen" — a second dismissal of the same
        signature increments ``reject_count`` and updates ``last_*``
        fields but keeps ``first_rejected_at``.
        """
        with self._lock:
            data = self._load()
            rejections: Dict[str, Any] = data.setdefault("rejections", {})
            entry = rejections.get(pattern_signature)
            if entry is None:
                entry = {
                    "pattern_signature": pattern_signature,
                    "first_rejected_at": _now_iso(),
                    "reject_count":      1,
                    "last_action":       action,
                    "last_dedup_key":    dedup_key,
                    "last_rejected_at":  _now_iso(),
                    "reason":            reason,
                }
            else:
                entry["reject_count"] = int(entry.get("reject_count", 0)) + 1
                entry["last_action"]      = action or entry.get("last_action")
                entry["last_dedup_key"]   = dedup_key or entry.get("last_dedup_key")
                entry["last_rejected_at"] = _now_iso()
                if reason:
                    entry["reason"] = reason
            rejections[pattern_signature] = entry
            self._save(data)
        self._append_audit({
            "ts":                _now_iso(),
            "pattern_signature": pattern_signature,
            "dedup_key":         dedup_key,
            "action":            action,
            "reason":            reason,
        })
        logger.info(
            "[RejectionStore] recorded rejection sig=%s count=%d",
            pattern_signature, entry["reject_count"],
        )
        return Rejection(
            pattern_signature=pattern_signature,
            first_rejected_at=entry["first_rejected_at"],
            reject_count=int(entry["reject_count"]),
            last_action=entry.get("last_action"),
            last_dedup_key=entry.get("last_dedup_key"),
            reason=entry.get("reason"),
        )

    def is_recently_rejected(
        self,
        pattern_signature: str,
        *,
        window_hours: int = _DEFAULT_WINDOW_HOURS,
    ) -> bool:
        """True if the operator dismissed a proposal with this signature
        within the last ``window_hours``.  Auto-expires older entries
        from the caller's perspective (they remain in the file for audit).
        """
        if not pattern_signature:
            return False
        cutoff = (_now() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
        with self._lock:
            data = self._load()
            entry = data.get("rejections", {}).get(pattern_signature)
        if not entry:
            return False
        last = entry.get("last_rejected_at") or entry.get("first_rejected_at") or ""
        return last >= cutoff

    def list_rejections(
        self,
        *,
        window_hours: Optional[int] = None,
    ) -> List[Rejection]:
        """Return all rejections, optionally filtered to the given window."""
        cutoff_iso = (
            (_now() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
            if window_hours is not None else ""
        )
        with self._lock:
            data = self._load()
        out: List[Rejection] = []
        for sig, entry in data.get("rejections", {}).items():
            last = entry.get("last_rejected_at") or entry.get("first_rejected_at") or ""
            if cutoff_iso and last < cutoff_iso:
                continue
            out.append(Rejection(
                pattern_signature=sig,
                first_rejected_at=entry.get("first_rejected_at", ""),
                reject_count=int(entry.get("reject_count", 0)),
                last_action=entry.get("last_action"),
                last_dedup_key=entry.get("last_dedup_key"),
                reason=entry.get("reason"),
            ))
        return sorted(out, key=lambda r: -r.reject_count)

    def revoke(self, pattern_signature: str) -> bool:
        """Operator-driven removal of a rejection record."""
        with self._lock:
            data = self._load()
            rejections = data.get("rejections", {})
            if pattern_signature not in rejections:
                return False
            del rejections[pattern_signature]
            self._save(data)
        logger.info("[RejectionStore] revoked rejection for %s", pattern_signature)
        return True

    def clear(self) -> int:
        """Wipe the store.  Audit log is preserved."""
        with self._lock:
            data = self._load()
            n = len(data.get("rejections", {}))
            self._save({"rejections": {}})
        return n

    # ── Internals ─────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"rejections": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"rejections": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("rejection store is not a JSON object")
            data.setdefault("rejections", {})
            return data
        except Exception:
            logger.exception(
                "[RejectionStore] could not parse %s — starting empty",
                self.path,
            )
            return {"rejections": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[RejectionStore] could not persist %s", self.path)

    def _append_audit(self, row: Dict[str, Any]) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("[RejectionStore] audit append failed", exc_info=True)


# Module-level singleton ──────────────────────────────────────────────────

_singleton: Optional[RejectionStore] = None
_singleton_lock = threading.Lock()


def get_rejection_store(
    force_path: Optional[Path] = None,
) -> RejectionStore:
    global _singleton
    with _singleton_lock:
        if force_path is not None:
            return RejectionStore(force_path)
        if _singleton is None:
            _singleton = RejectionStore()
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
