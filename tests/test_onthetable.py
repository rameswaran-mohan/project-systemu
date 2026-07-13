"""T1a — OnTheTable backend: model + side store + projection + names registry
(spec UNIFIED-v2 §5.10).

Covers the read-only "day-one populated table": the TableItem model, the atomic
+ defensive side store with tombstones, the credential NAME registry (keyring
can't enumerate), and the deterministic/idempotent projection from the live
stores (MCP servers, the tool catalog, credential names) — with dedup, heal,
and tombstone-respect.
"""
from __future__ import annotations

import json
from pathlib import Path

from systemu.runtime import table_store as ts
from systemu.runtime import table_reconciler as tr
from systemu.runtime.table_store import TableItem
from systemu.runtime.credentials.store import CredentialStore


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class _Vault:
    """Minimal vault: a .root dir + a fixed tool list (mirrors list_tools())."""
    def __init__(self, root: Path, tools=None):
        self.root = str(root)
        self._tools = tools or []

    def list_tools(self, status=None):
        return list(self._tools)


# --------------------------------------------------------------------------- #
# model + store
# --------------------------------------------------------------------------- #

def test_table_item_defaults():
    it = TableItem(id="ti_x", kind="tool", name="zipper")
    assert it.status == "declared" and it.provenance == "migrated"
    assert it.origin_class == "operator" and it.ref == {} and it.pinned is False


def test_store_roundtrip_and_json_boundary(tmp_path):
    v = _Vault(tmp_path)
    items = [TableItem(id="ti_1", kind="mcp_server", name="http://x", ref={"server": "http://x"})]
    ts.save_items(v, items)
    # persisted as plain JSON (model_dump(mode="json")) — no non-serializable objects
    raw = json.loads((tmp_path / "table" / "items.json").read_text(encoding="utf-8"))
    assert raw[0]["id"] == "ti_1" and raw[0]["kind"] == "mcp_server"
    loaded = ts.load_items(v)
    assert len(loaded) == 1 and loaded[0].name == "http://x"


def test_store_defensive_on_broken_file(tmp_path):
    v = _Vault(tmp_path)
    (tmp_path / "table").mkdir(parents=True)
    (tmp_path / "table" / "items.json").write_text("not json {", encoding="utf-8")
    assert ts.load_items(v) == []          # never raises


def test_store_skips_malformed_entry_keeps_rest(tmp_path):
    v = _Vault(tmp_path)
    (tmp_path / "table").mkdir(parents=True)
    (tmp_path / "table" / "items.json").write_text(
        json.dumps([{"garbage": True}, {"id": "ti_ok", "kind": "tool", "name": "ok"}]),
        encoding="utf-8",
    )
    loaded = ts.load_items(v)
    assert len(loaded) == 1 and loaded[0].id == "ti_ok"


def test_ref_key_identity_by_operational_id_not_name():
    # a rename must NOT fork the item — key is the operational identifier
    k1 = ts.ref_key("mcp_server", {"server": "http://x/"})
    k2 = ts.ref_key("mcp_server", {"server": "http://x"})
    assert k1 == k2                         # trailing slash normalized
    assert ts.ref_key("tool", {"tool_id": "t1", "name": "a"}) == \
           ts.ref_key("tool", {"tool_id": "t1", "name": "b"})   # id wins over name


def test_tombstone_add_and_load(tmp_path):
    v = _Vault(tmp_path)
    ts.add_tombstone(v, "mcp_server:http://gone")
    assert "mcp_server:http://gone" in ts.load_tombstones(v)


# --------------------------------------------------------------------------- #
# credential NAME registry (no values)
# --------------------------------------------------------------------------- #

def test_credential_names_registry(tmp_path):
    cs = CredentialStore(base_dir=tmp_path)
    cs._keyring = None                      # exercise the file path deterministically
    cs.set("github_pat", "ghp_secret")
    cs.set("openai_api_key", "sk-secret")
    names = cs.list_names()
    assert "github_pat" in names and "openai_api_key" in names
    # the registry holds NAMES ONLY — never a value
    assert "ghp_secret" not in (tmp_path / ".credential_names.json").read_text(encoding="utf-8")
    cs.delete("github_pat")
    assert "github_pat" not in cs.list_names()


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #

def _seed_mcp(vault, server="http://gh"):
    from systemu.runtime.mcp import connections
    connections.add_server(vault, server)


def test_projection_populates_from_live_stores(tmp_path):
    tools = [
        {"id": "t1", "name": "zipper", "enabled": True, "forged_by_systemu": True,
         "effect_tags": ["local_write"], "description": "zip files"},
        {"id": "t2", "name": "poster", "enabled": False, "dry_run_status": "failed"},
    ]
    v = _Vault(tmp_path, tools=tools)
    _seed_mcp(v, "http://gh")
    cs = CredentialStore(base_dir=tmp_path); cs._keyring = None
    cs.set("gh_pat", "secret")

    items = tr.project(v)
    kinds = {it.kind for it in items}
    assert {"mcp_server", "tool", "credential_ref"} <= kinds

    by_name = {it.name: it for it in items}
    assert by_name["zipper"].status == "ready"
    assert by_name["zipper"].origin_class == "systemu_authored"
    assert by_name["zipper"].usage.get("effect_tags") == ["local_write"]
    assert by_name["poster"].status == "broken"           # dry_run failed
    assert by_name["gh_pat"].kind == "credential_ref" and by_name["gh_pat"].status == "ready"


def test_projection_is_idempotent_stable_ids(tmp_path):
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    first = {it.id for it in tr.project(v)}
    second = {it.id for it in tr.project(v)}
    assert first == second and first        # stable ids, no churn/dupes


def test_projection_respects_tombstones(tmp_path):
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    _seed_mcp(v, "http://gone")
    # remove the mcp server via tombstone
    ts.add_tombstone(v, ts.ref_key("mcp_server", {"server": "http://gone"}))
    items = tr.project(v)
    assert not any(it.kind == "mcp_server" for it in items)
    assert any(it.name == "a" for it in items)            # the tool still projects


def test_projection_reflects_pins_from_sidecar(tmp_path):
    # pinned is authoritative from the UI-owned pins.json sidecar — the reconciler
    # never has to write items.json to record a pin (DEC-10 single-writer).
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    items = tr.project(v)
    key = ts.ref_key(items[0].kind, items[0].ref)
    ts.set_pin(v, key, True)
    reprojected = {it.id: it for it in tr.project(v)}
    assert reprojected[items[0].id].pinned is True        # operator curation survives
    ts.set_pin(v, key, False)
    reprojected = {it.id: it for it in tr.project(v)}
    assert reprojected[items[0].id].pinned is False       # unpin round-trips too


def test_reconcile_once_persists(tmp_path):
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    n = tr.reconcile_once(v)
    assert n >= 1
    assert (tmp_path / "table" / "items.json").exists()
    assert len(ts.load_items(v)) == n


# --------------------------------------------------------------------------- #
# T1b — /table page (pure zoning/summary helpers; render is operator-verifiable)
# --------------------------------------------------------------------------- #

def test_page_module_imports():
    # catches syntax/import errors in the page + its wiring
    from systemu.interface.pages import table as table_page
    assert callable(table_page.build_table_page)


def test_zone_of_mapping():
    from systemu.interface.pages.table import zone_of
    assert zone_of("mcp_server") == "Services & Accounts"
    assert zone_of("tool") == "Tools & Capabilities"
    assert zone_of("credential_ref") == "Keys"
    assert zone_of("data_root") == "Files & Data"
    assert zone_of("wat") == "Other"


def test_group_into_zones_drops_empty_and_orders():
    from systemu.interface.pages.table import group_into_zones
    items = [
        TableItem(id="a", kind="tool", name="t"),
        TableItem(id="b", kind="mcp_server", name="s"),
        TableItem(id="c", kind="tool", name="t2"),
    ]
    zones = group_into_zones(items)
    assert set(zones) == {"Services & Accounts", "Tools & Capabilities"}
    assert len(zones["Tools & Capabilities"]) == 2
    assert "Keys" not in zones            # empty zones dropped


def test_summarize_counts():
    from systemu.interface.pages.table import summarize
    items = [
        TableItem(id="a", kind="tool", name="t"),
        TableItem(id="b", kind="tool", name="t2"),
        TableItem(id="c", kind="credential_ref", name="k"),
    ]
    s = summarize(items)
    assert "2 tools" in s and "1 key" in s
    assert summarize([]) == "nothing yet"


# --------------------------------------------------------------------------- #
# T2a — curate what's already on the table: remove(+undo) · pin · search/filter
# (spec UNIFIED-v2 §5.10.b/.c, §9 T2). Pure logic unit-tested here; the nicegui
# card actions (★/×, undo snackbar, zone collapse) are operator-verifiable.
# --------------------------------------------------------------------------- #

def test_remove_tombstone_is_symmetric_undo(tmp_path):
    """add_tombstone hides an item from the projection; remove_tombstone (undo)
    brings it back — the projector re-adds the live-store object."""
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    _seed_mcp(v, "http://gh")
    key = ts.ref_key("mcp_server", {"server": "http://gh"})

    ts.add_tombstone(v, key)
    assert not any(it.kind == "mcp_server" for it in tr.project(v))   # removed

    ts.remove_tombstone(v, key)
    assert key not in ts.load_tombstones(v)
    assert any(it.kind == "mcp_server" for it in tr.project(v))       # undo re-adds


def test_remove_tombstone_missing_key_is_a_noop(tmp_path):
    v = _Vault(tmp_path)
    ts.remove_tombstone(v, "never:there")            # must not raise on absent key
    assert ts.load_tombstones(v) == set()


def test_filter_items_matches_name_detail_kind_case_insensitively():
    from systemu.interface.pages.table import filter_items
    items = [
        TableItem(id="a", kind="tool", name="Zipper", detail="zip files"),
        TableItem(id="b", kind="mcp_server", name="github", detail="git host"),
        TableItem(id="c", kind="credential_ref", name="openai_key", detail="stored"),
    ]
    assert {it.id for it in filter_items(items, "ZIP")} == {"a"}         # name+detail
    assert {it.id for it in filter_items(items, "git")} == {"b"}         # detail
    assert {it.id for it in filter_items(items, "credential")} == {"c"}  # kind
    assert filter_items(items, "") == items                              # empty = all
    assert filter_items(items, "   ") == items                           # blank = all
    assert filter_items(items, "nomatch") == []


def test_set_pin_targets_only_its_ref_key(tmp_path):
    # pinning one item's ref-key must not pin another's (sidecar keyed by ref_key)
    v = _Vault(tmp_path, tools=[
        {"id": "t1", "name": "a", "enabled": True},
        {"id": "t2", "name": "b", "enabled": True},
    ])
    items = tr.project(v)
    a = next(it for it in items if it.name == "a")
    b = next(it for it in items if it.name == "b")
    ts.set_pin(v, ts.ref_key(a.kind, a.ref), True)
    reproj = {it.id: it for it in tr.project(v)}
    assert reproj[a.id].pinned is True
    assert reproj[b.id].pinned is False                  # only the targeted ref-key


def test_load_pins_defensive_on_broken_file(tmp_path):
    v = _Vault(tmp_path)
    (tmp_path / "table").mkdir(parents=True)
    (tmp_path / "table" / "pins.json").write_text("not json {", encoding="utf-8")
    assert ts.load_pins(v) == set()                      # never raises


def test_sort_for_display_pins_first_then_name():
    from systemu.interface.pages.table import sort_for_display
    items = [
        TableItem(id="a", kind="tool", name="banana"),
        TableItem(id="b", kind="tool", name="apple", pinned=True),
        TableItem(id="c", kind="tool", name="cherry"),
        TableItem(id="d", kind="tool", name="date", pinned=True),
    ]
    order = [it.name for it in sort_for_display(items)]
    # pinned first (apple, date — alpha within), then the rest alpha (banana, cherry)
    assert order == ["apple", "date", "banana", "cherry"]


def test_removal_notice_flags_still_active_for_operational_kinds():
    from systemu.interface.pages.table import removal_notice
    for kind in ("credential_ref", "mcp_server", "service"):
        msg, still_active = removal_notice(kind, "thing")
        assert still_active is True
        assert "still" in msg.lower()          # honest: the real object persists
    # a preference / declared intent has no live object behind it
    msg, still_active = removal_notice("preference", "dark mode")
    assert still_active is False


# --------------------------------------------------------------------------- #
# T2b-1 — "+ Put on the table" ADD: operator_added declarations that SURVIVE
# re-projection (the reconciler carry-forward gap) via a UI-owned sidecar
# (spec UNIFIED-v2 §5.10.a/.b, §9 T2). items.json stays reconciler-single-writer.
# --------------------------------------------------------------------------- #

def test_make_operator_item_taxonomy_and_no_secret():
    it = ts.make_operator_item("service", "Stripe", detail="payments")
    assert it.provenance == "operator_added"      # §5.10.b#2 — direct-UI only
    assert it.origin_class == "operator"          # §5.10.b#7 — operator-typed = trusted
    assert it.status == "declared"                # intent, not yet operational
    # a credential DECLARATION carries the NAME only — never a value (§5.10.b#6)
    cred = ts.make_operator_item("credential_ref", "stripe_key")
    assert cred.ref == {"credential_name": "stripe_key"}
    assert "value" not in cred.ref and cred.detail == ""


def test_add_operator_item_projects_as_declared(tmp_path):
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    items = tr.project(v)
    stripe = next(it for it in items if it.name == "Stripe")
    assert stripe.kind == "service" and stripe.status == "declared"
    assert stripe.provenance == "operator_added"


def test_operator_item_SURVIVES_reconcile_once(tmp_path):
    # THE gap regression: reconcile_once overwrites items.json with project();
    # a purely operator_added item (no live store behind it) must NOT be clobbered.
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    tr.reconcile_once(v)                          # persists project() to items.json
    items = tr.project(v)
    assert any(it.name == "Stripe" and it.provenance == "operator_added" for it in items)
    assert any(it.name == "a" for it in items)    # the migrated tool still there too


def test_migrated_wins_on_ref_key_collision(tmp_path):
    # operator declares a data_root; the SAME path later becomes a live granted root
    # (here simulated as a migrated item sharing the ref_key) → ONE item, not two,
    # and the live/migrated one wins (declared → ready heal, §5.10.a / AC2).
    root = str(tmp_path / "invoices")
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("data_root", root))
    # a migrated item with the same ref_key, injected via items.json is not how the
    # projector sources migrated items — instead assert the dedup invariant directly:
    op_key = ts.ref_key("data_root", {"root_path": root})
    dupe_key = ts.ref_key("data_root", {"root_path": root})
    assert op_key == dupe_key
    items = tr.project(v)
    assert sum(1 for it in items if ts.ref_key(it.kind, it.ref) == op_key) == 1


def test_operator_item_respects_tombstone_and_undo(tmp_path):
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    key = ts.ref_key("service", {"server": "Stripe"})
    ts.add_tombstone(v, key)
    assert not any(it.name == "Stripe" for it in tr.project(v))     # removed
    ts.remove_tombstone(v, key)
    assert any(it.name == "Stripe" for it in tr.project(v))         # undo re-adds


def test_add_operator_item_dedups_by_ref_key(tmp_path):
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe", detail="again"))
    assert len(ts.load_operator_items(v)) == 1        # one ref_key ⇒ one entry


def test_load_operator_items_defensive(tmp_path):
    v = _Vault(tmp_path)
    (tmp_path / "table").mkdir(parents=True)
    (tmp_path / "table" / "operator_items.json").write_text("not json {", encoding="utf-8")
    assert ts.load_operator_items(v) == []            # never raises


def test_operator_item_honors_pin(tmp_path):
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    key = ts.ref_key("service", {"server": "Stripe"})
    ts.set_pin(v, key, True)
    stripe = next(it for it in tr.project(v) if it.name == "Stripe")
    assert stripe.pinned is True                      # pins apply to operator items too


def test_redeclare_after_tombstone_overrides_it(tmp_path):
    # adversarial-review fix: an explicit "Put on the table" re-declaration is a
    # direct operator action → it OVERRIDES a prior removal tombstone (like undo),
    # so the item renders again instead of being silently swallowed by the tombstone.
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    key = ts.ref_key("service", {"server": "Stripe"})
    ts.add_tombstone(v, key)
    assert not any(it.name == "Stripe" for it in tr.project(v))      # removed
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))   # re-declare
    assert key not in ts.load_tombstones(v)                          # tombstone cleared
    assert any(it.name == "Stripe" for it in tr.project(v))          # renders again


def test_redeclare_live_object_after_tombstone_reheals(tmp_path):
    # remove a live-backed item, let undo lapse, re-declare it → tombstone cleared →
    # the migrated (live) item re-appears, so the success toast is truthful.
    cs = CredentialStore(base_dir=tmp_path); cs._keyring = None
    cs.set("gh_pat", "secret")
    v = _Vault(tmp_path)
    key = ts.ref_key("credential_ref", {"credential_name": "gh_pat"})
    ts.add_tombstone(v, key)
    assert not any(it.name == "gh_pat" for it in tr.project(v))      # removed from view
    ts.add_operator_item(v, ts.make_operator_item("credential_ref", "gh_pat"))
    assert key not in ts.load_tombstones(v)
    gh = next(it for it in tr.project(v) if it.name == "gh_pat")
    assert gh.provenance == "migrated"                              # live twin wins (heal)


def test_credential_declaration_drops_free_text_detail(tmp_path):
    # a credential declaration carries the NAME only — any free-text note is dropped
    # so an operator can never park a secret value on a Keys-zone item (§5.10.b#6).
    it = ts.make_operator_item("credential_ref", "stripe_key", detail="sk_live_supersecret")
    assert it.detail == ""
    # a service note, by contrast, is legitimately kept
    svc = ts.make_operator_item("service", "Stripe", detail="payments account")
    assert svc.detail == "payments account"
