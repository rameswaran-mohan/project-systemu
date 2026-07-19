"""T1 — OnTheTable projection (spec UNIFIED-v2 §5.10.a).

Projects the operator's operational stores into TableItems: MCP servers/connections,
the vault tool catalog, and the credential-NAME registry. This is the read-only
"day-one populated table" — an existing install shows what it has with zero input.

Projection is DETERMINISTIC and IDEMPOTENT: an item's id is a stable hash of its
per-kind identity ``ref_key`` (`table_store.ref_key`), so re-projection dedups and
"heals" (a re-added store object re-attaches to its existing item, never a
duplicate). Tombstoned refs (operator-removed) are never re-added. Operator-set
fields (``pinned``, ``usage``) survive re-projection.

Never subtracts: this only maps the live stores; it is called by the SituationInventory
(a later release) as one CURATED input among five — it can annotate/re-rank but never
hides a live store object from the planner (the Callout-3 "look at everything" floor).
Defensive throughout — a broken sub-store contributes nothing, never an exception.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List

from systemu.runtime import table_store as ts
from systemu.runtime.table_store import TableItem

logger = logging.getLogger(__name__)


def _id_for(key: str) -> str:
    return "ti_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _project_mcp(vault) -> List[TableItem]:
    out: List[TableItem] = []
    try:
        from systemu.runtime.mcp import connections
    except Exception:
        return out
    try:
        servers = connections.all_servers(vault)
    except Exception:
        return out
    try:
        enabled = connections.enabled_tools(vault)
    except Exception:
        enabled = []
    for server in servers:
        try:
            key = ts.ref_key("mcp_server", {"server": server})
            connected = False
            try:
                connected = connections.is_server_connected(vault, server)
            except Exception:
                connected = False
            n_tools = sum(1 for e in enabled if str(e.get("server", "")).rstrip("/") == str(server).rstrip("/"))
            out.append(TableItem(
                id=_id_for(key), kind="mcp_server", name=str(server),
                detail=(f"{n_tools} tool(s) enabled" if n_tools else "connected server"),
                status="ready" if connected else "declared",
                provenance="migrated", origin_class="operator",
                ref={"server": str(server).rstrip("/")},
            ))
        except Exception:
            continue
    return out


def _project_tools(vault) -> List[TableItem]:
    out: List[TableItem] = []
    try:
        tools = vault.list_tools()
    except Exception:
        return out
    for t in (tools or []):
        if not isinstance(t, dict):
            continue
        try:
            tid = t.get("id") or ""
            name = t.get("name") or ""
            if not (tid or name):
                continue
            key = ts.ref_key("tool", {"tool_id": tid, "name": name})
            enabled = bool(t.get("enabled"))
            dry = str(t.get("dry_run_status") or "")
            if enabled:
                status = "ready"
            elif dry == "failed" or t.get("forge_rejected"):
                status = "broken"
            else:
                status = "declared"
            out.append(TableItem(
                id=_id_for(key), kind="tool", name=str(name),
                detail=str(t.get("description") or "")[:200],
                status=status, provenance="migrated",
                # The TAINT axis (§5.10.b "who vouches for this content"), NOT an
                # authorship record — so it is UNCONDITIONAL. It was derived from
                # ``forged_by_systemu`` ("an LLM authored this tool body"), which
                # stamped every non-forged tool ``operator`` — this code asserting
                # THE OPERATOR DECLARED IT about shipped seed tools the operator
                # never touched. Authorship lives truthfully in
                # ``capability_index.origin`` (builtin|forged|mcp); this now agrees
                # with ``situational_inventory``'s CapabilityRef default for the
                # same objects.
                origin_class="systemu_authored",
                ref={"tool_id": tid, "name": name},
                # effect_tags (populated once G0 lands) ride in usage metadata for
                # the page + later gate context; empty on the plain v0.9.52 base.
                usage={"effect_tags": list(t.get("effect_tags") or [])},
            ))
        except Exception:
            continue
    return out


def _project_credentials(vault) -> List[TableItem]:
    out: List[TableItem] = []
    try:
        from systemu.runtime.credentials.store import CredentialStore
        cs = CredentialStore(base_dir=getattr(vault, "root", None))
        names = cs.list_names()
    except Exception:
        return out
    for name in (names or []):
        try:
            key = ts.ref_key("credential_ref", {"credential_name": name})
            out.append(TableItem(
                id=_id_for(key), kind="credential_ref", name=str(name),
                detail="stored credential", status="ready",
                provenance="migrated", origin_class="operator",
                ref={"credential_name": name},
            ))
        except Exception:
            continue
    return out


def project(vault) -> List[TableItem]:
    """Compute the current TableItems from the live operational stores.

    Deterministic + idempotent; tombstoned refs excluded. ``created_at`` and
    ``usage`` are preserved from any existing item with the same (stable) id;
    ``pinned`` is read from the UI-owned ``pins.json`` sidecar (`ts.load_pins`) so
    operator pin curation never has to write ``items.json`` (DEC-10 single-writer)."""
    tombstones = ts.load_tombstones(vault)
    pins = ts.load_pins(vault)
    existing = {it.id: it for it in ts.load_items(vault)}

    projected: Dict[str, TableItem] = {}
    live_keys: set = set()
    for item in (_project_mcp(vault) + _project_tools(vault) + _project_credentials(vault)):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones:
            continue                       # operator removed it — never re-add
        live_keys.add(key)
        item.pinned = key in pins          # authoritative from the UI-owned sidecar
        prior = existing.get(item.id)
        if prior is not None:
            # heal/preserve: keep the original creation time + curated usage
            item.created_at = prior.created_at
            if prior.usage and not item.usage:
                item.usage = prior.usage
        projected[item.id] = item          # dict keyed by stable id dedups

    # merge operator_added declarations (§5.10.a): a declared intent surfaces only
    # while it has NO live object — once the real thing appears at the same ref_key
    # the migrated item wins (declared→ready heal, AC2), so no duplicate card.
    for item in ts.load_operator_items(vault):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones or key in live_keys:
            continue
        item.pinned = key in pins
        projected.setdefault(item.id, item)

    # merge T3 CONSULT declarations (§5.10.1). Both this and the palette above are
    # operator-typed, so their relative order only decides which BADGE an operator
    # sees when they name the same thing twice; the palette's is the more
    # deliberate act, so it goes first and wins.
    #
    # NOTE the shape difference from the two loops that bracket it: this one and
    # the proposals loop below check ONLY the tombstone, not `live_keys` or a
    # per-source key set. Precedence here is carried entirely by loop ORDER plus
    # `setdefault`, because every id — live and sidecar alike — derives from the
    # shared `ref_key` through the same function (`_id_for` == `ts.id_for_key`),
    # so the earlier, more-trusted card already occupies the id this one would
    # claim. Adding a key check back would be a guard no test could kill, which
    # this codebase treats as a defect in its own right. The two things that ARE
    # load-bearing are pinned directly:
    # `test_the_projector_and_the_store_derive_the_same_item_id` (the derivation)
    # and `test_an_operator_declaration_outranks_a_proposal_for_the_same_thing`
    # (the order). The tombstone check is NOT redundant: a tombstoned live object
    # is skipped above, so without it a sidecar row would resurrect the removal.
    for item in ts.load_consulted_items(vault):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones:
            continue
        item.pinned = key in pins
        projected.setdefault(item.id, item)

    # merge §5.9 LEARNED items (G-LEARN slice 3) — LAST, deliberately. An operator
    # declaration and a live store object both outrank a learned suggestion, so this
    # loop runs after both and uses `setdefault`: a ref_key collision keeps the
    # operator's / the live object's card, never the learned one. Tombstoned refs are
    # skipped here too — a learned suggestion must never resurrect something the
    # operator removed (`add_learned_item` refuses at the write; this is the read-side
    # half, so a sidecar written before the tombstone still cannot re-add).
    operator_keys = {ts.ref_key(i.kind, i.ref) for i in ts.load_operator_items(vault)}
    for item in ts.load_learned_items(vault):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones or key in live_keys or key in operator_keys:
            continue
        item.pinned = key in pins
        projected.setdefault(item.id, item)

    # merge T3 task PROPOSALS (`table_propose`, §5.10.b#2) — the least-trusted
    # source, so it goes LAST. Same shape and same reasoning as the consult loop
    # above. `status`/`origin_class` are clamped by the loader; nothing here
    # promotes a suggestion to anything.
    for item in ts.load_proposed_items(vault):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones:
            continue
        item.pinned = key in pins
        projected.setdefault(item.id, item)

    # ── R-B4: apply the operator's ACCEPTANCES (§5.10.b#1 / AC5) ───────────────
    # Runs LAST, over the finished projection, and is deliberately the only place
    # a `suggested` item ever changes status. Three properties make it safe:
    #
    #   1. STATUS ONLY. `provenance` and `origin_class` are not touched — the taint
    #      rule is "accepting changes status, never origin". An accepted proposal
    #      keeps `provenance="proposed"`/`origin_class="content_derived"` and so
    #      keeps its badge, its fence, and its no-silent-bind treatment.
    #   2. It promotes ONLY `suggested`. It cannot resurrect a `broken` item into
    #      `ready` or otherwise launder a health state, and a stale acceptance for
    #      a key that later became a live `ready` object is inert rather than a
    #      downgrade.
    #   3. The source is an operator-only sidecar (`accepted.json`). Nothing in the
    #      runtime writes it, so no code path auto-promotes.
    #
    # Tombstoned keys never reach here — every loop above skips them — so an
    # accepted-then-removed item stays removed.
    accepted = ts.load_accepted(vault)
    if accepted:
        for item in projected.values():
            if item.status == "suggested" \
                    and ts.ref_key(item.kind, item.ref) in accepted:
                item.status = "declared"
    return list(projected.values())


def reconcile_once(vault) -> int:
    """Project and persist the snapshot to ``<vault>/table/items.json``. Returns
    the item count. Never raises — a projection failure leaves the last snapshot
    in place. Registered as a periodic daemon job in a later release (T1b)."""
    try:
        items = project(vault)
        ts.save_items(vault, items)
        return len(items)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[OnTheTable] reconcile_once failed (non-fatal): %s", exc)
        return 0
