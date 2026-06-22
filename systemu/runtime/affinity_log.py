"""Activity-to-shadow affinity log (v0.4.1-b).

When the Intelligent Supervisor TERMINATEs an execution, the affinity
log records ``(scroll_intent_hash, shadow_id) → timestamp``.  Future
shadow-assignment decisions consult the log and **exclude shadows that
TERMINATEd on similar activities within the last N hours** (default 48).

The hash is computed from the scroll's intent + objectives (a stable
short signature) so "similar" means "looks like the same kind of work",
not "exact same scroll id".  Two different scrolls that both ask a
shadow to do something it's bad at will share an intent hash and
therefore share the exclusion.

Storage: JSON file at ``data/affinity_log.json``.  Atomic writes via
the same tmp-rename pattern used by other v0.3/v0.4 stores.  Read-on-
every-check pattern (no in-memory cache to go stale across processes).

Auto-expiration: ``recent_terminations()`` filters by window_hours,
defaulting to 48.  Entries older than that are still in the file but
ignored by exclusion queries — operators can wipe the file at any time
to fully reset.

This is **NOT** a hard ban — `is_excluded()` is a *signal* that callers
combine with other criteria (specialty match, current load, etc.).
The supervisor's SWAP_SHADOW action consults it; operator-driven
manual assignment is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path("data") / "affinity_log.json"
_DEFAULT_WINDOW_HOURS = 48


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def compute_intent_hash(
    *,
    intent: Optional[str],
    objectives: Optional[Iterable] = None,
) -> str:
    """Stable short hash representing what an activity is *trying to do*.

    Composed from the scroll's intent + the first 200 chars of its
    concatenated objective goals.  Same scroll → same hash; two
    different scrolls with the same kind of work → same hash.

    Deterministic; no LLM.  10-char hex prefix is enough — collisions
    across an operator-scale shadow army are extremely unlikely and
    even on collision the cost is at most one extra exclusion entry.
    """
    parts: List[str] = [(intent or "").strip().lower()]
    if objectives:
        try:
            goals = []
            for obj in objectives:
                if isinstance(obj, dict):
                    goals.append(str(obj.get("goal") or "").strip().lower())
                else:
                    goals.append(str(getattr(obj, "goal", "") or "").strip().lower())
            parts.append("|".join(goals)[:200])
        except Exception:
            logger.debug("[AffinityLog] objective hash fallback", exc_info=True)
    blob = "||".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:10]


# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Termination:
    intent_hash: str
    shadow_id:   str
    ts_iso:      str
    scroll_id:   Optional[str] = None
    execution_id: Optional[str] = None
    reason:      Optional[str] = None   # e.g. "supervisor_terminate"


class AffinityLog:
    """JSON-file-backed log of (intent_hash, shadow_id) terminations."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or _DEFAULT_PATH)
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def record_termination(
        self,
        *,
        intent_hash: str,
        shadow_id:   str,
        scroll_id:   Optional[str] = None,
        execution_id: Optional[str] = None,
        reason:      str = "supervisor_terminate",
    ) -> None:
        """Append a termination entry.  Atomic write; safe for concurrent callers."""
        with self._lock:
            data = self._load()
            entries: List[Dict[str, Any]] = data.setdefault("terminations", [])
            entries.append({
                "intent_hash":  intent_hash,
                "shadow_id":    shadow_id,
                "scroll_id":    scroll_id,
                "execution_id": execution_id,
                "ts":           _now_iso(),
                "reason":       reason,
            })
            self._save(data)
        logger.info(
            "[AffinityLog] recorded termination (intent=%s, shadow=%s, reason=%s)",
            intent_hash, shadow_id, reason,
        )

    def recent_terminations(
        self,
        *,
        intent_hash: Optional[str] = None,
        shadow_id:   Optional[str] = None,
        window_hours: int = _DEFAULT_WINDOW_HOURS,
    ) -> List[Termination]:
        """Return terminations matching the filters within the window."""
        cutoff = (_now() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
        with self._lock:
            data = self._load()
        out: List[Termination] = []
        for e in data.get("terminations", []):
            if e.get("ts", "") < cutoff:
                continue
            if intent_hash and e.get("intent_hash") != intent_hash:
                continue
            if shadow_id and e.get("shadow_id") != shadow_id:
                continue
            out.append(Termination(
                intent_hash=e.get("intent_hash", ""),
                shadow_id=e.get("shadow_id", ""),
                ts_iso=e.get("ts", ""),
                scroll_id=e.get("scroll_id"),
                execution_id=e.get("execution_id"),
                reason=e.get("reason"),
            ))
        return out

    def is_excluded(
        self,
        *,
        intent_hash: str,
        shadow_id:   str,
        window_hours: int = _DEFAULT_WINDOW_HOURS,
    ) -> bool:
        """True when (intent_hash, shadow_id) has a termination in the window."""
        return bool(self.recent_terminations(
            intent_hash=intent_hash, shadow_id=shadow_id, window_hours=window_hours,
        ))

    def clear(self) -> int:
        """Wipe the log.  Returns the number of entries removed."""
        with self._lock:
            data = self._load()
            n = len(data.get("terminations", []))
            self._save({"terminations": []})
        return n

    # ── Internals ─────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"terminations": []}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"terminations": []}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("affinity log is not a JSON object")
            data.setdefault("terminations", [])
            return data
        except Exception:
            logger.exception(
                "[AffinityLog] could not parse %s — starting empty (file left in place)",
                self.path,
            )
            return {"terminations": []}

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[AffinityLog] could not persist %s", self.path)


# Module-level singleton for runtime use.
_singleton: Optional[AffinityLog] = None
_singleton_lock = threading.Lock()


def get_affinity_log(force_path: Optional[Path] = None) -> AffinityLog:
    global _singleton
    with _singleton_lock:
        if force_path is not None:
            return AffinityLog(force_path)
        if _singleton is None:
            _singleton = AffinityLog()
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
