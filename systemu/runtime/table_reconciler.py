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
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.runtime import optional_deps as _od
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
            detail = str(t.get("description") or "")[:200]
            # F24: this page's entire job is telling the operator what systemu
            # can actually USE, and it was calling an enabled tool "ready" on a
            # machine where its optional dependency group is not installed —
            # the same lie the Build page was telling, one page over. The
            # verdict is CONSUMED from the one mint (DEC-43), never re-probed,
            # and it outranks `enabled`, which is a fact about the record.
            unavailable_reason = _od.unavailable_reason(t.get("dependencies") or [])
            if unavailable_reason:
                # NOT "broken" — nothing is broken, it was never installed, and
                # this word is what decides whether the operator reaches for the
                # forge or for pip. The card colours it as a problem and offers
                # the "Fix in Build" deep-link either way.
                status = "unavailable"
                # the WHY rides on the card: a red dot with no sentence sends
                # the operator to another page to find out what to run.
                detail = (detail + "  " + unavailable_reason).strip()
            elif enabled:
                status = "ready"
            elif dry == "failed" or t.get("forge_rejected"):
                status = "broken"
            else:
                status = "declared"
            out.append(TableItem(
                id=_id_for(key), kind="tool", name=str(name),
                detail=detail,
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
                # effect_tags ride in usage metadata for the page + later gate
                # context. This reads the INDEX HEADER (`list_tools()` returns
                # `load_index("tools")`), so it is only non-empty because
                # `vault._tool_header` emits the key and
                # `vault_migrator.converge_index_effect_tags` projects each body's
                # tags onto its header every boot. Empty is UNKNOWN, not "no
                # effects" — a tool PROVED effect-free carries the positive
                # `["no_effect"]` instead (F14). See
                # `capability_index.IndexRow.effect_tags`.
                usage={"effect_tags": list(t.get("effect_tags") or [])},
            ))
        except Exception:
            continue
    return out


def _credential_store(vault):
    """The vault's credential store, or None. ONE instance per projection: both the
    names projection and the presence witness read it, and each construction probes
    the OS keyring backend."""
    try:
        from systemu.runtime.credentials.store import CredentialStore
        return CredentialStore(base_dir=getattr(vault, "root", None))
    except Exception:
        return None


def _project_credentials(vault, store=None) -> List[TableItem]:
    out: List[TableItem] = []
    try:
        cs = store if store is not None else _credential_store(vault)
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


# ── R-B1 health: an orphan BREAKS, it never vanishes (§5.10.a, AC2) ──────────
# Before this, a card whose backing thing went away -- a deleted credential, a
# removed MCP server -- simply dropped out of the next projection. The operator
# never learned that something they relied on was gone; the table just got
# quietly smaller. Now the previous snapshot (`items.json`, which `project()`
# already reads for `created_at`) is the memory of what was there: a live-backed
# card whose object has gone is carried forward as `broken`, with a one-line
# reason and the time it broke, until the operator removes it (the tombstone the
# /table page already writes) or the object returns -- at which point the live
# projection takes the card back and it recovers on its own.
#
# Recovery re-derives the status from the live store rather than restoring a
# remembered one: for the same object those are the same answer, and where they
# differ (a server re-added but not yet connected) the stored one would be a lie.

#: Why an orphan broke, by kind. ASCII, one line: this is the diagnosis the /table
#: card leads with and the line "Needs you" shows. A kind absent from this map is
#: never orphaned (its cards keep the old vanish-on-removal behaviour).
ORPHAN_REASONS: Dict[str, str] = {
    "credential_ref": "credential removed",
    "mcp_server": "MCP server disconnected",
    "data_root": "folder grant revoked",
}
_GRANT_REVOKED = ORPHAN_REASONS["data_root"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clear_health(item: TableItem) -> TableItem:
    """Drop the projector-owned health fields from a SIDECAR row. Only `project()`
    may say an item broke, when, or which grant covers it; a hand-edited or
    task-written file may not."""
    item.broken_reason = ""
    item.broken_at = None
    item.data_root = None
    return item


# ── R-B1: folders come from GrantedRoots ─────────────────────────────────────
# The table is a READ-ONLY holder of the GrantedRoots store (CONC-MAP): it lists
# the operator's consent-gated grants and never grants, revokes or writes. Two
# things follow from that one read:
#
#   * every granted root is on the table as a `migrated` folder (§5.10.a "auto-
#     materialize ... granted root") -- so revoking it orphans the card exactly
#     like a deleted credential, reason "folder grant revoked";
#   * every OTHER folder card takes its `data_root` from the grant that covers
#     it. A declared folder inside a granted root is `ready` (the access is real);
#     one outside every grant stays `declared` and confers nothing (§5.10.b#3);
#     one that WAS covered and whose root was revoked breaks with that reason.
#
# `list_roots()` answers [] for a corrupt grants file. That is deliberately taken
# at its word here, unlike the two witnesses above: the confinement check reads
# the same list, so with that file unreadable the agent genuinely has no folder
# access, and "folder grant revoked" is the operational truth.

def _granted_roots(vault) -> Optional[List[str]]:
    """The canonical granted roots (read-only), or None if the store is unreachable."""
    try:
        from systemu.runtime.granted_roots import GrantedRootsStore
        return [r for r in GrantedRootsStore(base_dir=vault.root).list_roots()
                if type(r) is str and r]
    except Exception:
        return None


def _project_roots(roots: Optional[List[str]]) -> List[TableItem]:
    out: List[TableItem] = []
    for root in (roots or []):
        try:
            key = ts.ref_key("data_root", {"root_path": root})
            out.append(TableItem(
                id=_id_for(key), kind="data_root", name=root,
                detail="granted folder", status="ready",
                # the operator granted it, through a consent prompt that defaults
                # to NO -- this is operator-declared access, not content.
                provenance="migrated", origin_class="operator",
                ref={"root_path": root}, data_root=root,
            ))
        except Exception:
            continue
    return out


def _covering_root(path: Any, roots: List[str]) -> Optional[str]:
    """The granted root containing ``path`` (longest wins), else None. Uses the
    confinement module's own canonicalization and component-boundary check, so the
    table and the boundary agree about what "inside a grant" means."""
    if type(path) is not str or not path.strip():
        return None
    try:
        from systemu.runtime.granted_roots import _is_within, canonicalize
        canon = canonicalize(path)
    except Exception:
        return None
    best: Optional[str] = None
    for root in roots:
        if _is_within(canon, root) and (best is None or len(root) > len(best)):
            best = root
    return best


def _apply_folder_grants(projected: Dict[str, TableItem],
                         existing: Dict[str, TableItem],
                         roots: Optional[List[str]], now: str) -> None:
    """Stamp each non-root folder card with the grant that covers it (see above)."""
    if roots is None:
        return
    for item in projected.values():
        if item.kind != "data_root" or item.provenance == "migrated":
            continue                       # a root card: handled by projection/orphans
        covering = _covering_root((item.ref or {}).get("root_path"), roots)
        if covering is not None:
            item.data_root = covering
            if item.status == "declared":
                # ONLY `declared`: a grant never promotes a suggestion (acceptance is
                # the sole way out of the tray) and never launders another health
                # state. Status only -- provenance and origin are untouched.
                item.status = "ready"
            continue
        prior = existing.get(item.id)
        if prior is None:
            continue
        was_covered = prior.status == "ready" and bool(prior.data_root)
        still_broken = prior.status == "broken" and prior.broken_reason == _GRANT_REVOKED
        if item.status in ("declared", "ready") and (was_covered or still_broken):
            item.status = "broken"
            item.broken_reason = _GRANT_REVOKED
            item.broken_at = (prior.broken_at if still_broken and prior.broken_at
                              else now)


def _credential_witness(store) -> Optional[set]:
    """ref_keys of every credential name the registry WITNESSABLY holds, or None."""
    try:
        names = store.read_names_witnessed() if store is not None else None
    except Exception:
        return None
    if names is None:
        return None
    return {ts.ref_key("credential_ref", {"credential_name": n}) for n in names}


def _mcp_witness(vault) -> Optional[set]:
    """ref_keys of every server a FRESH, strict read of the connections file lists,
    or None when that read cannot be witnessed.

    Needed because the projection's own read cannot tell "no servers" from "could
    not read": `connections.get_state` answers the empty state for an unreadable
    file, and `connections._save` is a plain (non-atomic) rewrite, so a tick that
    lands mid-write would otherwise read every server as removed. Env-declared
    servers are deliberately not listed -- one that is still declared is live, and
    one whose declaration was withdrawn really is gone."""
    try:
        from systemu.runtime.mcp import connections
        path = Path(connections._path(vault))
    except Exception:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            return set() if Path(vault.root).is_dir() else None
        except Exception:
            return None
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if type(data) is not dict:
        return None
    servers = data.get("servers", [])
    if type(servers) is not list:
        return None
    return {ts.ref_key("mcp_server", {"server": s}) for s in servers if type(s) is str}


def _presence_witness(vault, store, roots) -> Dict[str, Optional[set]]:
    """kind -> the ref_keys its store WITNESSABLY holds right now (None = unknown).

    DEC-27: absence is acted on only when it is WITNESSED. A store that cannot be
    read is not evidence that everything in it was deleted, so a card is broken only
    when a successful read shows its object is not there. (Granted roots are their
    own witness -- see the GrantedRoots section below.)"""
    return {
        "credential_ref": _credential_witness(store),
        "mcp_server": _mcp_witness(vault),
        "data_root": (None if roots is None else
                      {ts.ref_key("data_root", {"root_path": r}) for r in roots}),
    }


def _as_orphan(prior: TableItem, reason: str, now: str) -> TableItem:
    """The prior card, carried forward as `broken`. `broken_at` is the moment it
    first broke and is never refreshed by a later tick; provenance and origin are
    untouched (breaking changes status, never origin)."""
    item = prior.model_copy(deep=True)
    already = (item.status == "broken" and item.broken_reason == reason
               and bool(item.broken_at))
    item.status = "broken"
    item.broken_reason = reason
    if item.kind == "data_root":
        item.data_root = None              # no granted root covers it any more
    if not already:
        item.broken_at = now
        item.updated_at = now
    return item


def _still_covered(prior: TableItem, covering: str) -> TableItem:
    """A root card whose OWN grant was revoked while another grant still contains
    it: the folder is still reachable, so it is not broken. Kept (never silently
    deleted), `ready`, with its data_root moved to the covering grant."""
    item = prior.model_copy(deep=True)
    item.status = "ready"
    item.broken_reason = ""
    item.broken_at = None
    item.data_root = covering
    return item


def project(vault) -> List[TableItem]:
    """Compute the current TableItems from the live operational stores.

    Deterministic + idempotent; tombstoned refs excluded. ``created_at`` and
    ``usage`` are preserved from any existing item with the same (stable) id;
    ``pinned`` is read from the UI-owned ``pins.json`` sidecar (`ts.load_pins`) so
    operator pin curation never has to write ``items.json`` (DEC-10 single-writer).

    R-B1: a live-backed card whose object has gone is carried forward as `broken`
    (see the section above). READ-ONLY like the rest of this function -- it runs
    on the palette refresher and the /table page as well as the daemon tick, and
    only `reconcile_once` persists."""
    tombstones = ts.load_tombstones(vault)
    pins = ts.load_pins(vault)
    existing = {it.id: it for it in ts.load_items(vault)}
    cred_store = _credential_store(vault)
    roots = _granted_roots(vault)

    projected: Dict[str, TableItem] = {}
    live_keys: set = set()
    for item in (_project_mcp(vault) + _project_tools(vault)
                 + _project_credentials(vault, cred_store) + _project_roots(roots)):
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

    # R-B1: orphans. Runs BEFORE the sidecar merges below on purpose: they all use
    # `setdefault`, so a broken orphan outranks an operator declaration or a
    # suggestion for the same thing. "Something you relied on is gone" is the
    # truer card than "you said you have this".
    witness = _presence_witness(vault, cred_store, roots)
    now = _now()
    for prior in existing.values():
        reason = ORPHAN_REASONS.get(prior.kind)
        if reason is None or prior.provenance != "migrated":
            continue                       # only a live-backed card can be orphaned
        key = ts.ref_key(prior.kind, prior.ref)
        if key in tombstones or key in live_keys:
            continue                       # removed by the operator / still live
        held = witness.get(prior.kind)
        if held is None or key in held:
            # Absence NOT witnessed: the store could not be read, or a fresh read
            # says the object is still there and only this tick's enumeration
            # missed it. Carry the card forward exactly as it was -- never a
            # vanish, and never a break manufactured out of a read failure.
            item = prior.model_copy(deep=True)
        else:
            covering = (_covering_root((prior.ref or {}).get("root_path"), roots)
                        if prior.kind == "data_root" and roots else None)
            item = (_still_covered(prior, covering) if covering is not None
                    else _as_orphan(prior, reason, now))
        item.pinned = key in pins
        projected[item.id] = item

    # merge operator_added declarations (§5.10.a): a declared intent surfaces only
    # while it has NO live object — once the real thing appears at the same ref_key
    # the migrated item wins (declared→ready heal, AC2), so no duplicate card.
    for item in ts.load_operator_items(vault):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones or key in live_keys:
            continue
        item.pinned = key in pins
        projected.setdefault(item.id, _clear_health(item))

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
        projected.setdefault(item.id, _clear_health(item))

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
        projected.setdefault(item.id, _clear_health(item))

    # merge T3 task PROPOSALS (`table_propose`, §5.10.b#2) — the least-trusted
    # source, so it goes LAST. Same shape and same reasoning as the consult loop
    # above. `status`/`origin_class` are clamped by the loader; nothing here
    # promotes a suggestion to anything.
    for item in ts.load_proposed_items(vault):
        key = ts.ref_key(item.kind, item.ref)
        if key in tombstones:
            continue
        item.pinned = key in pins
        projected.setdefault(item.id, _clear_health(item))

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

    # R-B1: folder cards take their data_root from GrantedRoots. AFTER acceptance,
    # so an accepted folder inside a grant is `ready` on the same tick.
    _apply_folder_grants(projected, existing, roots, now)
    return list(projected.values())


# ── R-B1: the credential NAMES backfill ──────────────────────────────────────
# The names registry (`CredentialStore` `.credential_names.json`) only learns a
# name through `set`, so credentials stored before it existed are invisible to
# the table. Spec §5.10.a: "backfilled from `Tool.requires_credentials`". The
# keyring cannot enumerate itself, so the declared keys are the only names it can
# be asked about.
#
# ONCE per name per process: a declared key the keyring did not hold is not asked
# about again until the daemon restarts. Re-asking every tick would mean a locked
# desktop keyring prompting to unlock every few minutes for keys that simply do
# not exist. (The at-rest file is enumerable and costs no prompt, so the store
# re-checks it every time.)

_PROBE_LOCK = threading.Lock()
_PROBED: Dict[str, set] = {}


def backfill_credential_names(vault) -> List[str]:
    """Record the names of stored credentials the registry is missing. Returns the
    names recorded ([] when none, or when the registry could not be read). Never
    raises; reads no value it keeps; writes names only."""
    try:
        # ONE definition of "the credential keys tools declare": the witnessed
        # roster reader the secret-redaction corpus already uses (auth_type
        # "none" skipped). Completeness does not matter here -- an incomplete
        # roster only means fewer names get backfilled this time.
        from systemu.runtime.credentials.known_values import _declared_credential_keys
        declared, _complete, _reason = _declared_credential_keys(vault)
    except Exception:
        declared = set()
    root = str(getattr(vault, "root", "") or "")
    with _PROBE_LOCK:
        fresh = set(declared) - _PROBED.get(root, set())
    store = _credential_store(vault)
    if store is None:
        return []
    try:
        recorded = store.backfill_names(fresh)
    except Exception:
        logger.debug("[OnTheTable] names backfill failed (non-fatal)", exc_info=True)
        return []
    if recorded is None:
        return []                          # registry unreadable: nothing probed, retry later
    with _PROBE_LOCK:
        _PROBED.setdefault(root, set()).update(fresh)
    return list(recorded)


#: Serializes the one writer of ``items.json``. Two daemon jobs drive it now (the
#: 60s projection reconciler and the R-B1 health tick), both on APScheduler pool
#: threads. Each derives the whole file from the previous snapshot, so an
#: overlapping pair would race the read of that snapshot against the other's
#: write -- and on Windows the losing ``os.replace`` of an overlapping pair raises.
_RECONCILE_LOCK = threading.Lock()


def reconcile_once(vault) -> int:
    """Project and persist the snapshot to ``<vault>/table/items.json``. Returns
    the item count. Never raises — a projection failure leaves the last snapshot
    in place. Registered as the daemon's 60s ``table_reconciler`` job (T1b) and
    re-run by the R-B1 health tick; the two are serialized by ``_RECONCILE_LOCK``."""
    try:
        with _RECONCILE_LOCK:
            items = project(vault)
            ts.save_items(vault, items)
        return len(items)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[OnTheTable] reconcile_once failed (non-fatal): %s", exc)
        return 0


# ── R-B1: the health tick ────────────────────────────────────────────────────
# The 60s reconcile already carries the orphan and folder-grant checks inside
# `project()`. The health tick is the slower, heavier pass on top of it: it runs
# the names backfill (which may ask the OS keyring) and then re-runs the
# reconcile so whatever the backfill found -- and whatever broke -- is on the
# table in the same pass. Registered in `scheduler/daemon.py` via
# `jobs._table_health_job`, i.e. on the APScheduler pool, never the UI thread.

#: Operator-tunable cadence. Default 10 minutes; clamped to [1, 1440].
HEALTH_INTERVAL_ENV = "SYSTEMU_TABLE_HEALTH_MINUTES"
HEALTH_INTERVAL_DEFAULT_MIN = 10
_HEALTH_INTERVAL_MIN, _HEALTH_INTERVAL_MAX = 1, 1440


def health_interval_minutes() -> int:
    """The health tick's cadence in minutes, from ``SYSTEMU_TABLE_HEALTH_MINUTES``.
    Unset or not an integer -> the default; out of range -> clamped. Read once, at
    daemon boot (an interval trigger is fixed at registration)."""
    import os
    raw = os.environ.get(HEALTH_INTERVAL_ENV)
    try:
        minutes = int(str(raw).strip()) if raw is not None else HEALTH_INTERVAL_DEFAULT_MIN
    except (TypeError, ValueError):
        return HEALTH_INTERVAL_DEFAULT_MIN
    return max(_HEALTH_INTERVAL_MIN, min(_HEALTH_INTERVAL_MAX, minutes))


def run_health_checks(vault) -> Dict[str, int]:
    """One health pass: names backfill, then the reconcile (whose projection runs
    the orphan and folder-grant checks). Returns ``{"items", "broken",
    "backfilled"}`` where ``broken`` counts health-broken items (those with a
    recorded reason). Never raises."""
    backfilled = 0
    try:
        backfilled = len(backfill_credential_names(vault))
    except Exception:
        logger.debug("[OnTheTable] health backfill failed (non-fatal)", exc_info=True)
    items = reconcile_once(vault)
    broken = 0
    try:
        broken = sum(1 for it in ts.load_items(vault)
                     if it.status == "broken" and it.broken_reason)
    except Exception:
        broken = 0
    if broken:
        logger.info("[OnTheTable] health tick: %d item(s) on the table are broken", broken)
    return {"items": items, "broken": broken, "backfilled": backfilled}
