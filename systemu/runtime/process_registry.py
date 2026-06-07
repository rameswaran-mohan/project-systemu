"""v0.9.6 L7 process_registry — turn-surviving registry of long-running processes.

The agent starts a background command (curl, build, etc.), registers it here,
and can check on / kill it across LLM turns. Exposed as v2 LLM tools so the
LLM can inspect what it spawned.

In-process singleton (resets between Python processes). For across-process
persistence, lifecycle managers should serialize to vault — that's L7+ work.
"""
from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Singleton state ───────────────────────────────────────────────────

_PROCESSES: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    return f"proc_{secrets.token_hex(4)}"


# ── Public API ────────────────────────────────────────────────────────

def register_process(*, name: str, command: str, pid: int, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Register a new running process. Returns the registry id."""
    with _LOCK:
        rid = _new_id()
        _PROCESSES[rid] = {
            "id": rid,
            "name": name,
            "command": command,
            "pid": pid,
            "status": "running",
            "registered_at": _now(),
            "completed_at": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "metadata": dict(metadata or {}),
        }
        return rid


def mark_done(
    process_id: str,
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Mark a process completed. No-op if process_id is unknown."""
    with _LOCK:
        entry = _PROCESSES.get(process_id)
        if entry is None:
            return
        entry["status"] = "completed"
        entry["completed_at"] = _now()
        entry["exit_code"] = int(exit_code)
        entry["stdout"] = str(stdout)[:10_000]
        entry["stderr"] = str(stderr)[:10_000]


def check_process(process_id: str) -> Optional[Dict[str, Any]]:
    """Return process state dict, or None if unknown."""
    with _LOCK:
        entry = _PROCESSES.get(process_id)
        if entry is None:
            return None
        return dict(entry)


def list_processes() -> List[Dict[str, Any]]:
    """Return ALL registered processes (running + completed)."""
    with _LOCK:
        return [dict(e) for e in _PROCESSES.values()]


def clear_completed() -> int:
    """Remove completed processes. Returns count of removed entries."""
    with _LOCK:
        completed_ids = [pid for pid, e in _PROCESSES.items() if e.get("status") == "completed"]
        for pid in completed_ids:
            _PROCESSES.pop(pid, None)
        return len(completed_ids)


def clear_all() -> None:
    """Clear the registry. Useful for tests."""
    with _LOCK:
        _PROCESSES.clear()


# ── LLM tool handlers ─────────────────────────────────────────────────

def _process_list_handler(**kwargs) -> Dict[str, Any]:
    return {"success": True, "processes": list_processes()}


def _process_check_handler(**kwargs) -> Dict[str, Any]:
    process_id = kwargs.get("process_id", "")
    info = check_process(process_id)
    if info is None:
        return {"success": False, "error": f"process not found: {process_id}"}
    return {"success": True, "process": info}


# ── Tool schemas ──────────────────────────────────────────────────────
#
# The matching ``registry.register(...)`` calls live in
# ``systemu/runtime/tools/process_tools.py`` — NOT here.  Boot-time v2
# discovery (`_discover_v2_tools` → `registry.discover_modules`) AST-scans
# ONLY the ``systemu.runtime.tools`` package, so registrations placed in this
# module (which lives in ``systemu.runtime``) would never fire in production.
# Keeping the schemas + handlers here and the registrations in the tools
# shim makes the tools visible to the LLM at runtime.

_LIST_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
}

_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "process_id": {"type": "string", "description": "Registry id returned by register_process."},
    },
    "required": ["process_id"],
}
