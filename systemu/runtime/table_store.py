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
is IMMUTABLE through every status TRANSITION — accepting a suggestion changes
status, never origin (the §5.10.b taint rule).

That immutability is a rule about the CURATION lifecycle, **not** about the
projector. ``table_reconciler.project()`` is this store's sole writer and RE-DERIVES
``origin_class`` from the live stores on every tick (only ``created_at``/``usage``
carry forward, and ``pinned`` comes from the sidecar) — a derived label has to track
its derivation, or a stamp we later find to be wrong would be frozen into every
install that already persisted it. Pinned by
``test_projection_recomputes_origin_class_each_tick``.
"""
from __future__ import annotations

import hashlib
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
    # operator | systemu_authored | content_derived. IMMUTABLE across status
    # transitions; RE-DERIVED by the projector each tick (see the module docstring).
    origin_class: str = "operator"
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


def remove_tombstone(vault, ref_key: str) -> None:
    """Undo a removal: drop ``ref_key`` from the tombstone set so the projector
    re-adds the live-store object. A no-op (never raises) if the key is absent."""
    tomb = load_tombstones(vault)
    tomb.discard(str(ref_key))
    _write_atomic(_tombstones_path(vault), json.dumps(sorted(tomb), indent=2))


# ── operator pins — a UI-owned curation sidecar (§5.10.c, DEC-10) ──────────────
# `pins.json` is written ONLY by the /table page and READ by the reconciler's
# projection. This keeps `items.json` a single-writer store (the reconciler): the
# UI never writes items.json, so pin curation cannot race the 60s reconcile.

def _pins_path(vault) -> Path:
    return _dir(vault) / "pins.json"


def load_pins(vault) -> set:
    """The set of operator-pinned ref-keys. Defensive: broken/absent ⇒ empty."""
    try:
        path = _pins_path(vault)
        if not path.exists():
            return set()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k) for k in (raw or []) if isinstance(k, str)}
    except Exception:
        return set()


def set_pin(vault, ref_key: str, pinned: bool) -> None:
    """Pin/unpin a ref-key. UI-only writer; the projector reads this back onto the
    matching item's ``pinned`` flag. Atomic + never raises on a missing key."""
    pins = load_pins(vault)
    if pinned:
        pins.add(str(ref_key))
    else:
        pins.discard(str(ref_key))
    _write_atomic(_pins_path(vault), json.dumps(sorted(pins), indent=2))


# ── operator_added declarations — a UI-owned curation sidecar (§5.10.a/.b) ─────
# The "+ Put on the table" palette creates DECLARED intent items (a service the
# operator says they use, a folder, a credential NAME) that have no live-store
# object yet. They persist in `operator_items.json` (written ONLY by the /table UI
# — §5.10.b#2 "operator_added settable ONLY by direct operator UI action") and are
# MERGED by the projector. items.json stays a single-writer store (the reconciler);
# a declared item whose live object later appears heals via a shared ref_key.

def id_for_key(ref_key: str) -> str:
    """Stable item id from a ref-key (the projector uses the same derivation, so a
    declared item and its later migrated twin collapse to ONE card)."""
    return "ti_" + hashlib.sha1(str(ref_key).encode("utf-8")).hexdigest()[:12]


# per-kind ref shape for an operator declaration — chosen so ref_key() keys it the
# SAME way the migrated projection would, enabling declared→ready heal on collision.
def _operator_ref(kind: str, name: str) -> Dict[str, Any]:
    if kind in ("service", "mcp_server"):
        return {"server": name}
    if kind == "data_root":
        return {"root_path": name}
    if kind == "credential_ref":
        return {"credential_name": name}
    if kind == "tool":
        return {"name": name}
    return {"name": name}


def make_operator_item(kind: str, name: str, detail: str = "") -> "TableItem":
    """Construct a DECLARED operator_added item. origin_class is forced to
    ``operator`` (operator-typed = trusted, §5.10.b#7); no secret ever in ``ref``
    (a credential declaration carries the NAME only, §5.10.b#6). A credential
    declaration also carries NO free-text ``detail`` — so an operator can't
    accidentally park a secret value in a note on a Keys-zone item (§5.10.b#6)."""
    ref = _operator_ref(kind, name)
    if kind == "credential_ref":
        detail = ""
    return TableItem(
        id=id_for_key(ref_key(kind, ref)), kind=kind, name=name, detail=detail,
        status="declared", provenance="operator_added", origin_class="operator", ref=ref)


def _operator_items_path(vault) -> Path:
    return _dir(vault) / "operator_items.json"


def load_operator_items(vault) -> List[TableItem]:
    """Persisted operator_added declarations. Defensive: broken/absent ⇒ []."""
    try:
        path = _operator_items_path(vault)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: List[TableItem] = []
        for entry in (raw or []):
            if not isinstance(entry, dict):
                continue
            try:
                it = TableItem(**entry)
                # harden the sidecar: nothing here can be anything but a direct
                # operator declaration, regardless of what a hand-edited file claims.
                it.provenance = "operator_added"
                it.origin_class = "operator"
                out.append(it)
            except Exception:
                continue
        return out
    except Exception:
        return []


def save_operator_items(vault, items: List[TableItem]) -> None:
    payload = [it.model_dump(mode="json") for it in items]
    _write_atomic(_operator_items_path(vault), json.dumps(payload, indent=2))


def add_operator_item(vault, item: "TableItem") -> None:
    """Append an operator declaration, deduped by ref_key (re-adding the same thing
    is a no-op that keeps the first). UI-only writer.

    An explicit "Put on the table" is a DIRECT operator action (§5.10.b#2), so it
    OVERRIDES any prior removal tombstone for the same ref_key — re-declaring X
    means the operator wants X back (mirroring the 10s undo). Without this, a stale
    tombstone would silently swallow the re-declaration while the UI reported success."""
    key = ref_key(item.kind, item.ref)
    remove_tombstone(vault, key)              # declaring un-removes (overrides tombstone)
    items = load_operator_items(vault)
    if any(ref_key(it.kind, it.ref) == key for it in items):
        return
    items.append(item)
    save_operator_items(vault, items)


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
