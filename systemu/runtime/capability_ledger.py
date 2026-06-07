"""v0.9.3 Layer 3 — Capability Ledger.

Tracks tool/skill capabilities systemu has seen: when each was first
registered, when last used, how many times invoked, success rate, and
last error if any. Storage is a single JSON sidecar at
`vault/capabilities/_usage.json` — name -> Capability.model_dump().

Pattern matches Hermes's tools/skill_usage.py — sidecar over the main
catalog so usage updates don't churn the canonical tool/skill records.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.core.models import Capability

logger = logging.getLogger(__name__)

# Single in-process lock — sidecar writes are short, atomic via tempfile + rename.
_WRITE_LOCK = threading.RLock()


def _sidecar_path(vault) -> Path:
    return Path(vault.root) / "capabilities" / "_usage.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(vault) -> Dict[str, Dict[str, Any]]:
    p = _sidecar_path(vault)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[CapabilityLedger] corrupt sidecar %s — starting fresh: %s", p, exc)
        return {}


def _save(vault, data: Dict[str, Dict[str, Any]]) -> None:
    p = _sidecar_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp-file + rename.
    fd, tmp_path = tempfile.mkstemp(prefix="_usage.", suffix=".json.tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _to_capability(raw: Dict[str, Any]) -> Capability:
    # Pydantic handles ISO timestamps via validate.
    return Capability.model_validate(raw)


def register(vault, *, name: str, kind: str) -> Capability:
    """Register a capability. Idempotent — returns the existing record if
    already present (preserves registered_at + usage counters)."""
    with _WRITE_LOCK:
        data = _load(vault)
        if name in data:
            return _to_capability(data[name])
        cap = Capability(
            name=name, kind=kind, registered_at=_now(),
            last_used_at=None, invocations=0, successes=0,
            failures=0, last_error=None,
        )
        data[name] = json.loads(cap.model_dump_json())
        _save(vault, data)
        return cap


def record_invocation(
    vault,
    name: str,
    *,
    success: bool,
    error: Optional[str] = None,
    kind: str = "tool",
) -> None:
    """Increment invocation counters. Auto-registers an unknown capability
    as a tool (or whatever ``kind`` is passed)."""
    with _WRITE_LOCK:
        data = _load(vault)
        raw = data.get(name)
        if raw is None:
            cap = Capability(
                name=name, kind=kind, registered_at=_now(),
                last_used_at=None, invocations=0, successes=0,
                failures=0, last_error=None,
            )
            raw = json.loads(cap.model_dump_json())
            data[name] = raw

        raw["invocations"] = int(raw.get("invocations", 0)) + 1
        if success:
            raw["successes"] = int(raw.get("successes", 0)) + 1
        else:
            raw["failures"] = int(raw.get("failures", 0)) + 1
            if error is not None:
                raw["last_error"] = str(error)[:300]
        raw["last_used_at"] = _now().isoformat()
        _save(vault, data)


def get_capability(vault, name: str) -> Optional[Capability]:
    data = _load(vault)
    raw = data.get(name)
    return _to_capability(raw) if raw is not None else None


def list_capabilities(vault, *, kind: Optional[str] = None) -> List[Capability]:
    data = _load(vault)
    out: List[Capability] = []
    for raw in data.values():
        if kind is not None and raw.get("kind") != kind:
            continue
        try:
            out.append(_to_capability(raw))
        except Exception as exc:
            logger.warning("[CapabilityLedger] skipping malformed row: %s", exc)
    return out


def get_stats(vault, name: str) -> Optional[Dict[str, Any]]:
    """Convenience accessor that returns rolled-up stats for one name.

    Returns None if the name isn't in the ledger.
    """
    cap = get_capability(vault, name)
    if cap is None:
        return None
    success_rate = float(cap.successes) / cap.invocations if cap.invocations else 0.0
    return {
        "name": cap.name,
        "kind": cap.kind,
        "invocations": cap.invocations,
        "successes": cap.successes,
        "failures": cap.failures,
        "success_rate": success_rate,
        "last_used_at": cap.last_used_at.isoformat() if cap.last_used_at else None,
        "last_error": cap.last_error,
    }
