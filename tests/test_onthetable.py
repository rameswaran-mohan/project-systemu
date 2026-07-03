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


def test_projection_preserves_pinned_across_reconcile(tmp_path):
    v = _Vault(tmp_path, tools=[{"id": "t1", "name": "a", "enabled": True}])
    items = tr.project(v)
    items[0].pinned = True
    ts.save_items(v, items)
    reprojected = {it.id: it for it in tr.project(v)}
    assert reprojected[items[0].id].pinned is True        # operator curation survives


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
