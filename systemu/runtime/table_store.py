"""T1 — OnTheTable item store (spec UNIFIED-v2 §5.10).

The operator-facing half of the Situational Inventory: a durable, curated view of
"what the operator has." This module owns the **model + side store only**; the
projection that fills it from the operational stores lives in
``runtime/table_reconciler.py``.

Persistence follows the ``mcp/connections.py`` side-store pattern — one JSON file
under the vault, defensive reads (a broken file yields empty state, never an
exception, so the page shell can't die on it) — but with **atomic writes**
(tempfile + os.replace) so an interrupted write can't corrupt the store.

  * items:      ``<vault>/table/items.json``
  * tombstones: ``<vault>/table/tombstones.json``  (removed-item ref keys the
                projector must never re-add — §5.10.a "removal = tombstone")

`TableItem`s REFERENCE the operational stores (MCP connections, the tool catalog,
the credential-name registry); they never duplicate a secret value. `origin_class`
is IMMUTABLE through every status transition — accepting a suggestion changes
status, never origin (the §5.10.b taint rule).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# item kinds / states / provenance / origin — kept as plain str (not Enums) so the
# store round-trips through JSON without enum-coercion friction, matching the
# List[str]/dict conventions elsewhere in the vault.
ITEM_KINDS = {"service", "mcp_server", "tool", "data_root", "credential_ref",
              "preference", "device"}
ITEM_STATUSES = {"declared", "configuring", "ready", "stale", "broken", "suggested"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TableItem(BaseModel):
    """One thing on the operator's table. References an operational store via
    ``ref`` (ids/names/paths only — NEVER a secret value)."""

    id: str
    kind: str
    name: str
    detail: str = ""
    status: str = "declared"
    provenance: str = "migrated"          # consulted | operator_added | learned | migrated
    origin_class: str = "operator"        # operator | systemu_authored | content_derived — IMMUTABLE
    ref: Dict[str, Any] = Field(default_factory=dict)
    parent_id: Optional[str] = None       # grouping: a service anchors its sub-items
    usage: Dict[str, Any] = Field(default_factory=dict)
    pinned: bool = False
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    verified_at: Optional[str] = None


def _dir(vault) -> Path:
    return Path(vault.root) / "table"


def _items_path(vault) -> Path:
    return _dir(vault) / "items.json"


def _tombstones_path(vault) -> Path:
    return _dir(vault) / "tombstones.json"


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


def load_items(vault) -> List[TableItem]:
    """All persisted TableItems. Defensive: a broken/absent file ⇒ []."""
    try:
        path = _items_path(vault)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: List[TableItem] = []
        for entry in (raw or []):
            if not isinstance(entry, dict):
                continue
            try:
                out.append(TableItem(**entry))
            except Exception:
                continue  # skip a malformed entry, never fail the whole load
        return out
    except Exception:
        return []


def save_items(vault, items: List[TableItem]) -> None:
    payload = [it.model_dump(mode="json") for it in items]
    _write_atomic(_items_path(vault), json.dumps(payload, indent=2))


def load_tombstones(vault) -> set:
    """The set of ref-keys the operator removed — the projector must not re-add."""
    try:
        path = _tombstones_path(vault)
        if not path.exists():
            return set()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k) for k in (raw or []) if isinstance(k, str)}
    except Exception:
        return set()


def add_tombstone(vault, ref_key: str) -> None:
    tomb = load_tombstones(vault)
    tomb.add(str(ref_key))
    _write_atomic(_tombstones_path(vault), json.dumps(sorted(tomb), indent=2))


def ref_key(kind: str, ref: Dict[str, Any]) -> str:
    """Stable per-kind identity key for dedup / tombstone / heal (§5.10.a).

    Keyed on the operational identifier, not the display name, so a rename can't
    fork an item and a re-added object heals its existing item."""
    r = ref or {}
    if kind in ("mcp_server", "service"):
        return f"{kind}:{str(r.get('server', '')).rstrip('/')}"
    if kind == "tool":
        return f"tool:{r.get('tool_id') or r.get('name', '')}"
    if kind == "data_root":
        return f"data_root:{os.path.normcase(str(r.get('root_path', '')))}"
    if kind == "credential_ref":
        return f"credential_ref:{r.get('credential_name', '')}"
    # preference / device / fallback: name-based
    return f"{kind}:{r.get('name', '')}"
