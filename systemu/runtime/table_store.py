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
import threading
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
#: The five legal `provenance` values (R-B4). Previously documented ONLY in the
#: field comment on `TableItem.provenance`, which meant nothing could ASK whether a
#: value was known — and a renderer that cannot tell "unknown" from "known" has to
#: guess, which is how a badge ends up flattering an unrecognised row. The
#: §5.10.b#4 provenance banner reads this set to decide whether it may name a
#: source at all; anything outside it is reported as undetermined, never as
#: operator-declared.
ITEM_PROVENANCES = {"consulted", "operator_added", "learned", "migrated", "proposed"}


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
    provenance: str = "migrated"          # consulted | operator_added | learned | migrated | proposed
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


# ── accepted suggestions — a UI-owned curation sidecar (R-B4, §5.10.b#1) ──────
# `accepted.json` is the exact mirror of `tombstones.json`: a set of ref-keys the
# operator ACCEPTED from the tray, written ONLY by direct operator UI action and
# read back by the projector.
#
# It is an OVERLAY rather than an in-place status edit, and that is the whole
# design. `load_proposed_items` clamps `status="suggested"` unconditionally on
# every load — that clamp IS the trust boundary (§5.10.b#2b), so a persisted
# acceptance must never be expressed by writing `declared` into a task-writable
# sidecar, where the clamp would either erase it or have to be weakened to honour
# it. Keeping acceptance in a separate operator-only file lets the clamp stay
# absolute while a real acceptance still sticks.
#
# It carries STATUS ONLY. `provenance` and `origin_class` are untouched by
# acceptance — §5.10.b#1: "accepting a `suggested` item changes *status*
# (→`declared`), never origin." An accepted content_derived item is still
# content_derived, still fenced, still never silent-bound.
#
# A tombstone BEATS an acceptance: every merge loop in `project()` skips a
# tombstoned key before the overlay is ever consulted, so accept-then-remove
# removes. The reverse order is also safe — acceptance of a tombstoned key
# projects nothing.

def _accepted_path(vault) -> Path:
    return _dir(vault) / "accepted.json"


def load_accepted(vault) -> set:
    """The set of ref-keys the operator accepted from the tray. Defensive: a
    broken/absent file ⇒ empty (fail-closed: an unreadable file means nothing is
    accepted, never that everything is)."""
    try:
        path = _accepted_path(vault)
        if not path.exists():
            return set()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k) for k in (raw or []) if isinstance(k, str)}
    except Exception:
        return set()


def add_accepted(vault, ref_key: str) -> None:
    """Record an operator acceptance. Direct-UI-action only — nothing in the
    runtime may call this, or `suggested` would auto-promote (the §5.10.b#1 hard
    invariant). Pinned by ``test_nothing_in_the_runtime_accepts_a_suggestion``."""
    acc = load_accepted(vault)
    acc.add(str(ref_key))
    _write_atomic(_accepted_path(vault), json.dumps(sorted(acc), indent=2))


def remove_accepted(vault, ref_key: str) -> None:
    """Undo an acceptance — the item falls back to `suggested`. No-op if absent."""
    acc = load_accepted(vault)
    acc.discard(str(ref_key))
    _write_atomic(_accepted_path(vault), json.dumps(sorted(acc), indent=2))


# ── answer receipts — what answering ONE ask put on the table (R-B4, §5.6) ────
# §5.6: "Answer cards carry an 'on your table ✓' acknowledgement + undo chip when
# the answer auto-materializes a TableItem — an acknowledgement, never a gate."
#
# The card needs to name WHAT it put there, and it is rendered by the UI long
# after (and in a different process from) the reconciler tick that promoted the
# answer. Re-deriving it in the UI would mean re-running the leaf→kind mapping and
# the ref-shape construction — a second copy of the mapping whose drift from
# `ask_promotion`'s copy is exactly how a learned card and an operator removal stop
# meeting. So the promoter WRITES what it actually did, and the card reads it.
#
# Keyed by ask_id. Bounded: `MAX_ANSWER_RECEIPTS` asks, oldest evicted — an
# acknowledgement is worthless once the card is gone, so this must not grow
# without limit on a long-lived vault.

MAX_ANSWER_RECEIPTS = 50


def _receipts_path(vault) -> Path:
    return _dir(vault) / "answer_receipts.json"


def load_answer_receipts(vault) -> Dict[str, List[Dict[str, str]]]:
    """ask_id → the rows that answering it materialized. Defensive: broken ⇒ {}."""
    try:
        path = _receipts_path(vault)
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, List[Dict[str, str]]] = {}
        for ask_id, rows in raw.items():
            if not isinstance(ask_id, str) or not isinstance(rows, list):
                continue
            clean = [
                {"key": str(r.get("key", "")), "name": str(r.get("name", "")),
                 "kind": str(r.get("kind", ""))}
                for r in rows
                if isinstance(r, dict) and isinstance(r.get("key"), str) and r.get("key")
            ]
            if clean:
                out[ask_id] = clean
        return out
    except Exception:
        return {}


def record_answer_receipt(vault, ask_id: str, *, ref_key_: str, name: str,
                          kind: str) -> None:
    """Note that answering ``ask_id`` put ``ref_key_`` on the table.

    Best-effort and NEVER raises: §5.9 already treats the card as the visible half
    and the profile fact as the durable half, so a receipt failure must not fail a
    promotion that already succeeded. Deduped by key within one ask, so a re-answer
    that heals the same card in place does not stack acknowledgements.
    """
    try:
        if not ask_id or not ref_key_:
            return
        data = load_answer_receipts(vault)
        rows = data.get(ask_id, [])
        if any(r["key"] == str(ref_key_) for r in rows):
            return
        rows.append({"key": str(ref_key_), "name": str(name), "kind": str(kind)})
        data[ask_id] = rows
        if len(data) > MAX_ANSWER_RECEIPTS:
            # dicts preserve insertion order, so the oldest ask_ids are first
            for stale in list(data.keys())[: len(data) - MAX_ANSWER_RECEIPTS]:
                data.pop(stale, None)
        _write_atomic(_receipts_path(vault), json.dumps(data, indent=2))
    except Exception:
        pass


def clear_answer_receipt(vault, ask_id: str) -> None:
    """Drop one ask's receipt (the operator used the undo chip). Never raises."""
    try:
        data = load_answer_receipts(vault)
        if data.pop(str(ask_id), None) is not None:
            _write_atomic(_receipts_path(vault), json.dumps(data, indent=2))
    except Exception:
        pass


def undo_answer_receipt(vault, ask_id: str) -> List[str]:
    """The §5.6 undo chip: take back everything answering ``ask_id`` put on the
    table. Returns the ref-keys removed.

    TOMBSTONES rather than deleting the sidecar row, because deletion would not
    stick: the promotion's profile fact survives the undo (it is the durable half
    of §5.9 and undoing a table card is not a retraction of the answer), so the very
    next promotion for the same leaf would cheerfully re-add the card. A tombstone
    is the one removal `add_learned_item` actually honours.

    The receipt is cleared last so a failure part-way leaves the chip visible and
    the undo retryable, rather than clearing the acknowledgement for a removal that
    did not happen.
    """
    removed: List[str] = []
    try:
        for row in load_answer_receipts(vault).get(str(ask_id), []):
            key = row.get("key") or ""
            if not key:
                continue
            try:
                add_tombstone(vault, key)
                removed.append(key)
            except Exception:
                continue
    except Exception:
        return removed
    if removed:
        clear_answer_receipt(vault, ask_id)
    return removed


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


# ── learned items — the §5.9 promotion sidecar (G-LEARN slice 3) ──────────────
# The MIRROR IMAGE of the operator_items sidecar above, and a SEPARATE file for a
# load-bearing reason: `load_operator_items` force-stamps BOTH `provenance` and
# `origin_class`. That is right for a direct operator declaration and fatal for a
# learned one — it would destroy the very taint §5.9 promotion exists to carry, and
# would badge the /table card as operator-declared when the operator never declared it.
# So this loader force-stamps `provenance="learned"` only, and PRESERVES the origin
# (clamping a non-canonical value to `content_derived`, fail-untrusted).
#
# It must also be its own file rather than a row in `items.json`: the reconciler is
# that file's SOLE writer and re-projects from scratch, so an item written there is
# GONE after one `reconcile_once` tick. `table_reconciler.project()` merges this
# sidecar AFTER the operator loop (an operator declaration wins any ref_key collision).

#: Canonical taint values — mirrors `requirement_binder`'s clamp. Anything else fails
#: UNTRUSTED to `content_derived` rather than passing through to a trusted axis.
_CANONICAL_ORIGINS = frozenset({"operator", "systemu_authored", "content_derived"})


def make_learned_item(kind: str, name: str, *, origin_class: str,
                      detail: str = "") -> "TableItem":
    """Construct a §5.9 learned item. ``origin_class`` is keyword-only with NO default
    — the whole point of the slice is that the answer's ORIGINAL origin travels, so a
    forgotten stamp must be a ``TypeError``, never a silent trusted default.

    ``status="suggested"``: a learned proposal enters the "New on your table" tray
    (§5.10.a). It is never auto-confirmed."""
    ref = _operator_ref(kind, name)
    if kind == "credential_ref":
        detail = ""                       # §5.10.b#6 — never a note on a Keys item
    origin = origin_class if origin_class in _CANONICAL_ORIGINS else "content_derived"
    return TableItem(
        id=id_for_key(ref_key(kind, ref)), kind=kind, name=name, detail=detail,
        status="suggested", provenance="learned", origin_class=origin, ref=ref)


def _learned_items_path(vault) -> Path:
    return _dir(vault) / "learned_items.json"


def load_learned_items(vault) -> List[TableItem]:
    """Persisted §5.9 learned items. Defensive: broken/absent ⇒ [], malformed entry
    skipped. ``provenance`` is force-stamped; ``origin_class`` is PRESERVED (see the
    section comment) but clamped to the canonical vocabulary, fail-untrusted."""
    try:
        path = _learned_items_path(vault)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: List[TableItem] = []
        for entry in (raw or []):
            if not isinstance(entry, dict):
                continue
            try:
                it = TableItem(**entry)
                # a hand-edited sidecar cannot claim to be an operator declaration and
                # collect an operator badge...
                it.provenance = "learned"
                # ...nor launder its taint by claiming an unknown/trusted origin.
                if it.origin_class not in _CANONICAL_ORIGINS:
                    it.origin_class = "content_derived"
                out.append(it)
            except Exception:
                continue
        return out
    except Exception:
        return []


def save_learned_items(vault, items: List[TableItem]) -> None:
    payload = [it.model_dump(mode="json") for it in items]
    _write_atomic(_learned_items_path(vault), json.dumps(payload, indent=2))


def add_learned_item(vault, item: "TableItem") -> bool:
    """Append a learned item, deduped by ``ref_key``. Returns True when written.

    Unlike ``add_operator_item`` this does NOT clear a tombstone: an explicit "Put on
    the table" is a direct operator action that overrides a prior removal, but a
    LEARNED suggestion is not — resurrecting something the operator removed is exactly
    the re-add flapping tombstones exist to prevent."""
    key = ref_key(item.kind, item.ref)
    if key in load_tombstones(vault):
        return False
    items = load_learned_items(vault)
    if any(ref_key(it.kind, it.ref) == key for it in items):
        return False
    items.append(item)
    save_learned_items(vault, items)
    return True


# ── consulted items — the T3 "Set the table" sidecar (§5.10.1) ────────────────
# The THIRD sidecar, and a separate file for the same reason `learned_items.json`
# is: each loader force-stamps the values its own file's WRITER can vouch for, and
# no loader may vouch for another writer's content.
#
# `consulted_items.json` is written ONLY by `table_consult.commit()`, which runs
# after the operator has passed the mandatory one-screen review — i.e. every row
# here is operator-typed text the operator then re-read and approved. §5.10.b#7
# names that case explicitly ("consult answers ... trusted operator input, same as
# a §5.6 elicitation answer"), so the loader force-stamps `origin_class="operator"`
# on the same basis `load_operator_items` does.
#
# It force-stamps `provenance="consulted"`, NOT `operator_added`: the anti-forgery
# stamp on `operator_items.json` belongs to the direct-UI-action path and is not
# borrowed here. The two provenances stay distinguishable on the card badge, which
# is the honest thing to render — the operator DECLARED one and ANSWERED the other.

def make_consulted_item(kind: str, name: str, detail: str = "") -> "TableItem":
    """Construct a DECLARED consult item (§5.10.1 "declare-now-configure-later is
    the DEFAULT"): status ``declared``, never ``ready``/``configuring`` — the
    consult captures INTENT and the setup happens later in the existing flows.

    Credential declarations carry the NAME only, with no free-text note, matching
    ``make_operator_item`` (§5.10.b#6 — nothing on a Keys-zone item where a value
    could be parked)."""
    ref = _operator_ref(kind, name)
    if kind == "credential_ref":
        detail = ""
    return TableItem(
        id=id_for_key(ref_key(kind, ref)), kind=kind, name=name, detail=detail,
        status="declared", provenance="consulted", origin_class="operator", ref=ref)


def _consulted_items_path(vault) -> Path:
    return _dir(vault) / "consulted_items.json"


def load_consulted_items(vault) -> List[TableItem]:
    """Persisted consult declarations. Defensive: broken/absent ⇒ [].

    All three of ``provenance``/``origin_class``/``status`` are force-stamped. The
    first two for the reason above; ``status`` because this file holds pure INTENT
    — a consult item is only ever projected while NO live object exists at its
    ref_key, so a row claiming ``ready`` would paint a healthy status dot on
    something that was never configured."""
    try:
        path = _consulted_items_path(vault)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: List[TableItem] = []
        for entry in (raw or []):
            if not isinstance(entry, dict):
                continue
            try:
                it = TableItem(**entry)
                it.provenance = "consulted"
                it.origin_class = "operator"
                it.status = "declared"
                out.append(it)
            except Exception:
                continue
        return out
    except Exception:
        return []


def save_consulted_items(vault, items: List[TableItem]) -> None:
    payload = [it.model_dump(mode="json") for it in items]
    _write_atomic(_consulted_items_path(vault), json.dumps(payload, indent=2))


def add_consulted_item(vault, item: "TableItem") -> bool:
    """Append a consult declaration, deduped by ``ref_key``. Returns True when
    written.

    Clears a prior tombstone, exactly like ``add_operator_item``: naming X in the
    consult is a DIRECT operator action, and a stale tombstone silently swallowing
    it while the review screen reported success is the failure that rule exists to
    prevent. (Contrast ``add_learned_item``, which must NOT clear one — a learned
    suggestion is not a direct operator action.)"""
    key = ref_key(item.kind, item.ref)
    remove_tombstone(vault, key)
    items = load_consulted_items(vault)
    if any(ref_key(it.kind, it.ref) == key for it in items):
        return False
    items.append(item)
    save_consulted_items(vault, items)
    return True


# ── proposed items — the bounded `table_propose` sidecar (§5.10.b#2) ───────────
# The FOURTH sidecar. Everything in this file arrives from a NON-consult context —
# a task calling the `table_propose` registry tool — so it is untrusted by
# construction, and the loader clamps to the untrusted end UNCONDITIONALLY.
#
# That unconditional clamp is why this is not a row in `learned_items.json`. That
# loader must PRESERVE `origin_class`, because a §5.9 learned item can legitimately
# be `operator` (the operator typed the answer being promoted). A task proposal
# never can be — so giving it its own file buys a strictly stronger guarantee: no
# byte a task (or a hand-edit) can write here reaches a trusted axis.
#
# NOTE ON THE VOCABULARY. `provenance="proposed"` is a FIFTH value beyond the four
# the spec's model literal lists. The alternative was to reuse `learned`, which
# would have (a) misattributed a task's guess to the §5.9 promotion path in the
# audit trail and on the card badge, and (b) forced this content through a loader
# that cannot clamp. The /table card renders any non-`migrated` provenance as a
# badge, so the new value surfaces honestly with no UI change.

def make_proposed_item(kind: str, name: str, detail: str = "") -> "TableItem":
    """Construct a task-proposed item: ``suggested`` + ``content_derived``
    (§5.10.b#2 "any other task's writes land suggested+content_derived").

    There is no ``origin_class`` parameter on purpose — unlike
    ``make_learned_item``, this constructor has exactly one correct answer, and a
    parameter would only create a way to get it wrong."""
    ref = _operator_ref(kind, name)
    if kind == "credential_ref":
        detail = ""
    return TableItem(
        id=id_for_key(ref_key(kind, ref)), kind=kind, name=name, detail=detail,
        status="suggested", provenance="proposed", origin_class="content_derived",
        ref=ref)


def _proposed_items_path(vault) -> Path:
    return _dir(vault) / "proposed_items.json"


def load_proposed_items(vault) -> List[TableItem]:
    """Persisted task proposals. Defensive: broken/absent ⇒ [], malformed entry
    skipped. ``provenance``/``origin_class``/``status`` are ALL force-stamped — a
    row here claiming operator trust, a ``consulted`` badge or a ``ready`` status
    is a forgery whichever way it got written."""
    try:
        path = _proposed_items_path(vault)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: List[TableItem] = []
        for entry in (raw or []):
            if not isinstance(entry, dict):
                continue
            try:
                it = TableItem(**entry)
                it.provenance = "proposed"
                it.origin_class = "content_derived"
                it.status = "suggested"     # never auto-promoted (§5.10.b#1)
                out.append(it)
            except Exception:
                continue
        return out
    except Exception:
        return []


def save_proposed_items(vault, items: List[TableItem]) -> None:
    payload = [it.model_dump(mode="json") for it in items]
    _write_atomic(_proposed_items_path(vault), json.dumps(payload, indent=2))


#: Standing cap on the proposal tray. §5.10.b#2 words the bound as "capped per
#: session", but a task has no session object to carry a counter on, and a
#: per-call cap is no cap at all against a loop. A ceiling on the FILE is the
#: bound that actually holds however many tasks call the tool.
MAX_PROPOSED_ITEMS = 25


#: The one genuinely CONCURRENT writer among the four table sidecars. The other
#: three are UI-owned (one nicegui thread) or daemon-owned; this one is reached
#: from `table_propose` inside a shadow execution thread, and two concurrent runs
#: proposing at once would race the read-modify-write below — atomic writes stop a
#: torn file, they do not stop a lost update. In-process only, which is the whole
#: exposure: concurrent runs are threads of one daemon.
_PROPOSED_LOCK = threading.Lock()


def add_proposed_item(vault, item: "TableItem") -> str:
    """Append a task proposal. Returns ``""`` on success, else a refusal reason.

    A reason string rather than a bool so the caller can report accurately WITHOUT
    re-implementing these checks: the tombstone and dedup guards are STORE-level
    invariants that protect every caller, and a second copy upstream would make
    this one unkillable (delete it and no test fails).

    Like ``add_learned_item`` and unlike ``add_operator_item``, this does NOT clear
    a tombstone. Resurrecting something the operator removed is precisely the
    re-add flapping tombstones exist to prevent, and a task's guess is nowhere near
    the direct operator action that earns the override."""
    key = ref_key(item.kind, item.ref)
    if key in load_tombstones(vault):
        return "tombstoned"
    with _PROPOSED_LOCK:
        items = load_proposed_items(vault)
        if any(ref_key(it.kind, it.ref) == key for it in items):
            return "duplicate"
        if len(items) >= MAX_PROPOSED_ITEMS:
            return "capped"
        items.append(item)
        save_proposed_items(vault, items)
    return ""


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
