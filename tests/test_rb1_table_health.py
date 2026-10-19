"""R-B1 / R-B4 -- the HEALTH half of OnTheTable (spec UNIFIED-v2 sec 5.10.a).

The projection half shipped long ago: `table_reconciler.project()` maps the live
stores onto the operator's table every 60s. What it never did was notice when a
thing the operator relied on went AWAY. A deleted credential, a removed MCP server
or a revoked folder grant made its card silently VANISH, so the operator never
learned that something they counted on was gone.

These tests pin the ruled health layer:

  1. orphans become `broken` with a one-line reason and the time they broke, stay
     listed until the operator removes them, and recover by themselves when the
     backing thing returns -- never silently deleted;
  2. folder-backed items take their `data_root` from GrantedRoots, and an item
     under a revoked root breaks with that reason;
  3. a periodic health tick re-runs the checks off the UI thread, registered with
     the daemon's scheduler;
  4. credential names are backfilled once, idempotently;
  5. broken items count in "Needs you" with a reason and a /table link, and
     /health carries a "Table" row;
  6. every status the projector can emit is in `ITEM_STATUSES`.

Hermetic: every vault is a tmp dir, no daemon is started, and no test touches the
OS keyring (the credential store is pinned to a fake or to no keyring).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from systemu.runtime import table_store as ts

_SYSTEMU = Path(__file__).resolve().parent.parent / "systemu"


# =========================================================================== #
# 6. the status vocabulary is complete
# =========================================================================== #

def _emitted_statuses(rel: str) -> set:
    """Every string literal this module ASSIGNS to a `status`.

    Table-driven over the AST, not a hand list: a `status=` keyword in any call
    (`TableItem(..., status=...)`), an assignment to a bare `status` name or to any
    `.status` attribute, and the annotated class default. Every string constant
    anywhere inside the assigned expression counts, so a conditional
    (`"ready" if connected else "declared"`) contributes both arms. Comparisons
    (`item.status == "suggested"`) are READS and are deliberately not collected.
    """
    tree = ast.parse((_SYSTEMU / rel).read_text(encoding="utf-8"))
    out: set = set()

    def _strs(node):
        for sub in ast.walk(node):
            if type(sub) is ast.Constant and type(sub.value) is str:
                out.add(sub.value)

    def _is_status_target(t) -> bool:
        return ((type(t) is ast.Name and t.id == "status")
                or (type(t) is ast.Attribute and t.attr == "status"))

    for node in ast.walk(tree):
        if type(node) is ast.Call:
            for kw in node.keywords:
                if kw.arg == "status":
                    _strs(kw.value)
        elif type(node) is ast.Assign:
            if any(_is_status_target(t) for t in node.targets):
                _strs(node.value)
        elif type(node) is ast.AnnAssign:
            if _is_status_target(node.target) and node.value is not None:
                _strs(node.value)
    return out


def test_every_status_the_projector_emits_is_in_the_vocabulary():
    """`unavailable` was emitted by `_project_tools` (F24) and missing from
    `ITEM_STATUSES`, so nothing could ask whether it was a known state."""
    emitted = (_emitted_statuses("runtime/table_reconciler.py")
               | _emitted_statuses("runtime/table_store.py"))
    # not vacuous: the scan really sees the projector's states
    assert {"ready", "declared", "broken", "unavailable", "suggested"} <= emitted
    unknown = emitted - ts.ITEM_STATUSES
    assert not unknown, (
        f"the projector emits status(es) {sorted(unknown)} that ITEM_STATUSES does "
        "not carry - add them with a meaning, or stop emitting them")


def test_every_status_carries_a_meaning():
    assert set(ts.ITEM_STATUS_MEANINGS) == ts.ITEM_STATUSES
    for status, meaning in ts.ITEM_STATUS_MEANINGS.items():
        assert type(meaning) is str and meaning.strip(), status
        assert meaning.isascii(), status
    assert "unavailable" in ts.ITEM_STATUSES
    assert "install" in ts.ITEM_STATUS_MEANINGS["unavailable"]


def test_every_status_has_a_table_colour():
    """F24: an unrecognised status falls through the colour map to grey -- it
    renders as unremarkable inventory, a silent downgrade."""
    from systemu.interface.pages.table import _STATUS_COLOR
    assert ts.ITEM_STATUSES <= set(_STATUS_COLOR)


# =========================================================================== #
# fixtures shared by the projection tests
# =========================================================================== #

class _Vault:
    """Minimal vault: a real tmp `.root` + a fixed tool list (mirrors list_tools())."""

    def __init__(self, root: Path, tools=None):
        self.root = str(root)
        self._tools = tools or []

    def list_tools(self, status=None):
        return list(self._tools)


@pytest.fixture
def no_keyring(monkeypatch):
    """Pin the credential store to its file backend: no test may read or write the
    machine's real OS keyring."""
    from systemu.runtime.credentials import store as cred_store
    monkeypatch.setattr(cred_store, "usable_keyring", lambda: None)
    return cred_store


@pytest.fixture
def no_env_servers(monkeypatch):
    """`connections.all_servers` merges env-declared servers in; the operator's own
    environment must not leak into a projection test."""
    monkeypatch.delenv("SYSTEMU_MCP_SERVER_URLS", raising=False)


def _cred_store(vault):
    from systemu.runtime.credentials.store import CredentialStore
    return CredentialStore(base_dir=vault.root)


def _by_key(items):
    return {ts.ref_key(it.kind, it.ref): it for it in items}


_CRED_KEY = ts.ref_key("credential_ref", {"credential_name": "gh_pat"})
_MCP = "http://mcp.example"
_MCP_KEY = ts.ref_key("mcp_server", {"server": _MCP})


def _connect(vault, server=_MCP, connected=True):
    from systemu.runtime.mcp import connections
    connections.add_server(vault, server)
    connections.set_server_meta(vault, server, label=server, transport="http",
                                connected=connected)


# =========================================================================== #
# 1. orphans become broken -- with a reason and a time -- and never vanish
# =========================================================================== #

def test_a_deleted_credential_breaks_instead_of_vanishing(tmp_path, no_keyring):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    assert _by_key(ts.load_items(v))[_CRED_KEY].status == "ready"

    _cred_store(v).delete("gh_pat")
    tr.reconcile_once(v)

    item = _by_key(ts.load_items(v)).get(_CRED_KEY)
    assert item is not None, "the operator relied on this - it must not vanish"
    assert item.status == "broken"
    assert item.broken_reason == "credential removed"
    assert item.broken_at, "the time it broke is recorded"
    assert item.provenance == "migrated", "breaking changes status, never origin"


def test_a_removed_mcp_server_breaks_instead_of_vanishing(tmp_path, no_env_servers):
    from systemu.runtime import table_reconciler as tr
    from systemu.runtime.mcp import connections
    v = _Vault(tmp_path)
    _connect(v)
    tr.reconcile_once(v)
    assert _by_key(ts.load_items(v))[_MCP_KEY].status == "ready"

    connections.remove_server(v, _MCP)
    tr.reconcile_once(v)

    item = _by_key(ts.load_items(v)).get(_MCP_KEY)
    assert item is not None
    assert item.status == "broken"
    assert item.broken_reason == "MCP server disconnected"
    assert item.broken_at


def test_the_break_time_is_the_moment_it_broke_not_the_latest_tick(tmp_path, no_keyring):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    _cred_store(v).delete("gh_pat")
    tr.reconcile_once(v)
    first = _by_key(ts.load_items(v))[_CRED_KEY].broken_at

    for _ in range(3):
        tr.reconcile_once(v)
    again = _by_key(ts.load_items(v))[_CRED_KEY]
    assert again.status == "broken" and again.broken_at == first


def test_a_broken_credential_recovers_when_it_comes_back(tmp_path, no_keyring):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    before = _by_key(ts.load_items(v))[_CRED_KEY]

    _cred_store(v).delete("gh_pat")
    tr.reconcile_once(v)
    _cred_store(v).set("gh_pat", "new-value")
    tr.reconcile_once(v)

    after = _by_key(ts.load_items(v))[_CRED_KEY]
    assert after.status == before.status == "ready", "recovers to its previous status"
    assert after.broken_reason == "" and after.broken_at is None
    assert after.id == before.id and after.created_at == before.created_at, \
        "the SAME card heals - never a duplicate"


def test_a_broken_server_recovers_to_its_previous_status(tmp_path, no_env_servers):
    from systemu.runtime import table_reconciler as tr
    from systemu.runtime.mcp import connections
    v = _Vault(tmp_path)
    _connect(v)
    tr.reconcile_once(v)
    connections.remove_server(v, _MCP)
    tr.reconcile_once(v)
    assert _by_key(ts.load_items(v))[_MCP_KEY].status == "broken"

    _connect(v)
    tr.reconcile_once(v)
    after = _by_key(ts.load_items(v))[_MCP_KEY]
    assert after.status == "ready" and after.broken_reason == ""
    assert sum(1 for it in ts.load_items(v)
               if ts.ref_key(it.kind, it.ref) == _MCP_KEY) == 1


def test_removing_a_broken_item_is_how_the_operator_dismisses_it(tmp_path, no_keyring):
    """'Stay listed until the operator removes them' - removal is the tombstone the
    /table page already writes, and it sticks."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    _cred_store(v).delete("gh_pat")
    tr.reconcile_once(v)
    ts.add_tombstone(v, _CRED_KEY)
    tr.reconcile_once(v)
    tr.reconcile_once(v)
    assert _CRED_KEY not in _by_key(ts.load_items(v))


def test_an_unreadable_registry_never_manufactures_a_break(tmp_path, no_keyring):
    """The names registry is a PROJECTION aid - the secrets live in the keyring. A
    registry that cannot be read is not evidence that every credential was deleted,
    so absence is acted on only when it is WITNESSED."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    (tmp_path / ".credential_names.json").write_text("not json {", encoding="utf-8")

    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v)).get(_CRED_KEY)
    assert item is not None and item.status == "ready" and item.broken_reason == ""


def test_a_torn_connections_file_never_manufactures_a_break(tmp_path, no_env_servers):
    """`connections._save` is a plain rewrite; a tick that lands mid-write reads an
    empty state. That is not a removal."""
    from systemu.runtime import table_reconciler as tr
    from systemu.runtime.mcp import connections
    v = _Vault(tmp_path)
    _connect(v)
    tr.reconcile_once(v)
    path = Path(connections._path(v))
    path.write_text(path.read_text(encoding="utf-8")[:20], encoding="utf-8")

    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v)).get(_MCP_KEY)
    assert item is not None and item.status == "ready" and item.broken_reason == ""


def test_an_operator_declaration_with_no_live_object_never_breaks(tmp_path):
    """Only a thing a live store backed can be orphaned. A declared intent that was
    never connected stays `declared`."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    ts.add_operator_item(v, ts.make_operator_item("service", "Stripe"))
    for _ in range(2):
        tr.reconcile_once(v)
    stripe = next(it for it in ts.load_items(v) if it.name == "Stripe")
    assert stripe.status == "declared" and stripe.broken_reason == ""


def test_no_sidecar_can_forge_a_break(tmp_path):
    """The health fields are PROJECTOR-OWNED: a hand-edited sidecar row claiming a
    reason or a break time has both cleared on merge."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    forged = ts.make_operator_item("service", "Stripe").model_copy(
        update={"broken_reason": "credential removed",
                "broken_at": "2000-01-01T00:00:00+00:00"})
    ts.save_operator_items(v, [forged])
    stripe = next(it for it in tr.project(v) if it.name == "Stripe")
    assert stripe.broken_reason == "" and stripe.broken_at is None


def test_project_stays_read_only_when_it_finds_an_orphan(tmp_path, no_keyring):
    """`project()` runs on the palette refresher and the /table page too; only
    `reconcile_once` may persist (DEC-10 single writer)."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    _cred_store(v).delete("gh_pat")
    snapshot = (tmp_path / "table" / "items.json").read_bytes()

    projected = _by_key(tr.project(v))
    assert projected[_CRED_KEY].status == "broken"
    assert (tmp_path / "table" / "items.json").read_bytes() == snapshot


def test_a_broken_card_leads_with_its_reason_and_when():
    from systemu.interface.pages.table import broken_line
    from systemu.runtime.table_store import TableItem
    it = TableItem(id="ti_x", kind="credential_ref", name="gh_pat", status="broken",
                   broken_reason="credential removed",
                   broken_at="2026-10-19T09:30:00+00:00")
    line = broken_line(it)
    assert line.startswith("credential removed")
    assert "2026-10-19 09:30" in line and line.isascii()
    healthy = it.model_copy(update={"status": "ready", "broken_reason": ""})
    assert broken_line(healthy) == ""


# =========================================================================== #
# 2. folder-backed items take their data_root from GrantedRoots
# =========================================================================== #

def _grants(vault):
    from systemu.runtime.granted_roots import GrantedRootsStore
    return GrantedRootsStore(base_dir=vault.root)


def _folder(tmp_path, *parts) -> Path:
    p = tmp_path.joinpath("files", *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _root_key(canon):
    return ts.ref_key("data_root", {"root_path": canon})


def test_a_granted_root_is_on_the_table_as_a_ready_folder(tmp_path):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    canon = _grants(v).grant(str(_folder(tmp_path, "work")))

    item = _by_key(tr.project(v)).get(_root_key(canon))
    assert item is not None, "a granted root must appear without the operator re-typing it"
    assert item.kind == "data_root" and item.status == "ready"
    assert item.provenance == "migrated" and item.origin_class == "operator"
    assert item.data_root == canon, "its data_root IS the grant"


def test_a_revoked_root_breaks_with_that_reason_and_recovers_on_regrant(tmp_path):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    work = _folder(tmp_path, "work")
    canon = _grants(v).grant(str(work))
    tr.reconcile_once(v)

    _grants(v).revoke(str(work))
    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v)).get(_root_key(canon))
    assert item is not None and item.status == "broken"
    assert item.broken_reason == "folder grant revoked" and item.broken_at

    _grants(v).grant(str(work))
    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v))[_root_key(canon)]
    assert item.status == "ready" and item.broken_reason == "" and item.data_root == canon


def test_a_declared_folder_under_a_granted_root_takes_its_data_root_from_the_grant(tmp_path):
    from systemu.runtime import table_reconciler as tr
    from systemu.runtime.granted_roots import canonicalize
    v = _Vault(tmp_path / "vault")
    canon = _grants(v).grant(str(_folder(tmp_path, "work")))
    invoices = str(_folder(tmp_path, "work", "invoices"))
    ts.add_operator_item(v, ts.make_operator_item("data_root", invoices))

    item = next(it for it in tr.project(v)
                if it.kind == "data_root" and it.name == invoices)
    assert item.data_root == canon == canonicalize(str(tmp_path / "files" / "work"))
    assert item.status == "ready", "access is real: the folder is inside a granted root"
    assert item.provenance == "operator_added", "a grant changes status, never origin"


def test_a_declared_folder_under_a_revoked_root_breaks_and_recovers(tmp_path):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    work = _folder(tmp_path, "work")
    _grants(v).grant(str(work))
    invoices = str(_folder(tmp_path, "work", "invoices"))
    ts.add_operator_item(v, ts.make_operator_item("data_root", invoices))
    tr.reconcile_once(v)

    _grants(v).revoke(str(work))
    tr.reconcile_once(v)
    tr.reconcile_once(v)
    item = next(it for it in ts.load_items(v) if it.name == invoices)
    assert item.status == "broken" and item.broken_reason == "folder grant revoked"
    assert item.data_root is None, "no granted root covers it any more"
    broke_at = item.broken_at
    assert broke_at

    tr.reconcile_once(v)
    assert next(it for it in ts.load_items(v) if it.name == invoices).broken_at == broke_at

    _grants(v).grant(str(work))
    tr.reconcile_once(v)
    item = next(it for it in ts.load_items(v) if it.name == invoices)
    assert item.status == "ready" and item.broken_reason == "" and item.data_root


def test_a_folder_outside_every_grant_stays_declared_and_confers_nothing(tmp_path):
    """Spec sec 5.10.b#3: a data_root item outside GrantedRoots renders `declared` --
    it confers nothing."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    _grants(v).grant(str(_folder(tmp_path, "work")))
    elsewhere = str(_folder(tmp_path, "elsewhere"))
    ts.add_operator_item(v, ts.make_operator_item("data_root", elsewhere))
    for _ in range(2):
        tr.reconcile_once(v)
    item = next(it for it in ts.load_items(v) if it.name == elsewhere)
    assert item.status == "declared" and item.data_root is None
    assert item.broken_reason == ""


def test_a_grant_never_promotes_a_suggestion(tmp_path):
    """Acceptance is the ONLY path out of `suggested` (sec 5.10.b#1); a task-proposed
    folder under a granted root stays in the tray."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    _grants(v).grant(str(_folder(tmp_path, "work")))
    sub = str(_folder(tmp_path, "work", "drop"))
    assert ts.add_proposed_item(v, ts.make_proposed_item("data_root", sub)) == ""
    item = next(it for it in tr.project(v) if it.name == sub)
    assert item.status == "suggested" and item.origin_class == "content_derived"


def test_no_sidecar_can_claim_a_grant(tmp_path):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    outside = str(_folder(tmp_path, "outside"))
    forged = ts.make_operator_item("data_root", outside).model_copy(
        update={"data_root": outside})
    ts.save_operator_items(v, [forged])
    item = next(it for it in tr.project(v) if it.name == outside)
    assert item.data_root is None and item.status == "declared"


def test_the_table_reconciler_only_reads_granted_roots():
    """It is a READ-ONLY GrantedRoots holder (CONC-MAP): it may never grant,
    revoke, or write the confinement set."""
    tree = ast.parse((_SYSTEMU / "runtime" / "table_reconciler.py")
                     .read_text(encoding="utf-8"))
    calls = {n.func.attr for n in ast.walk(tree)
             if type(n) is ast.Call and type(n.func) is ast.Attribute}
    assert "list_roots" in calls, "not vacuous: it does read the grants"
    assert not calls & {"grant", "revoke", "_write"}


# =========================================================================== #
# 4. credential names are backfilled once, idempotently
# =========================================================================== #

class _FakeKeyring:
    """An in-memory stand-in for the OS keyring that COUNTS reads."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.reads = []

    def get_password(self, service, key):
        self.reads.append(key)
        return self.values.get(key)

    def set_password(self, service, key, value):
        self.values[key] = value

    def delete_password(self, service, key):
        self.values.pop(key, None)


@pytest.fixture
def fake_keyring(monkeypatch):
    from systemu.runtime.credentials import store as cred_store
    kr = _FakeKeyring()
    monkeypatch.setattr(cred_store, "usable_keyring", lambda: kr)
    return kr


def _names_file(root) -> Path:
    return Path(root) / ".credential_names.json"


def test_backfill_names_a_keyring_credential_that_was_never_registered(tmp_path, fake_keyring):
    """A credential written before the names registry existed lives in the keyring
    with no name on record, so the table cannot see it."""
    fake_keyring.values["OWM_KEY"] = "a-real-secret-value"
    cs = _cred_store(_Vault(tmp_path))
    assert cs.list_names() == []

    assert cs.backfill_names({"OWM_KEY", "NEVER_SET_KEY"}) == ["OWM_KEY"]
    assert cs.list_names() == ["OWM_KEY"], "an absent credential is never named"
    assert "a-real-secret-value" not in _names_file(tmp_path).read_text(encoding="utf-8")


def test_backfill_names_a_file_backed_credential(tmp_path, no_keyring):
    cs = _cred_store(_Vault(tmp_path))
    cs._write_file({"LEGACY_TOKEN": "legacy-secret-value"})   # pre-registry write
    assert cs.list_names() == []
    assert cs.backfill_names(set()) == ["LEGACY_TOKEN"]
    assert cs.list_names() == ["LEGACY_TOKEN"]


def test_backfill_is_idempotent(tmp_path, fake_keyring):
    fake_keyring.values["OWM_KEY"] = "a-real-secret-value"
    cs = _cred_store(_Vault(tmp_path))
    assert cs.backfill_names({"OWM_KEY"}) == ["OWM_KEY"]
    before = _names_file(tmp_path).read_bytes()
    assert cs.backfill_names({"OWM_KEY"}) == []
    assert _names_file(tmp_path).read_bytes() == before


def test_backfill_never_writes_through_an_unreadable_registry(tmp_path, fake_keyring):
    """Writing would REPLACE every name the unreadable file holds."""
    fake_keyring.values["OWM_KEY"] = "a-real-secret-value"
    _names_file(tmp_path).write_text("not json {", encoding="utf-8")
    cs = _cred_store(_Vault(tmp_path))
    assert cs.backfill_names({"OWM_KEY"}) is None
    assert _names_file(tmp_path).read_text(encoding="utf-8") == "not json {"


def _real_vault_declaring(tmp_path, *keys):
    from systemu.core.models import CredentialRequirement, Tool, ToolType
    from systemu.vault.vault import Vault
    v = Vault(root=tmp_path / "v")
    v.save_tool(Tool(
        id="t_weather", name="weather", description="d", tool_type=ToolType.API_CALL,
        requires_credentials=[CredentialRequirement(key=k, label="L") for k in keys]))
    return v


def test_the_table_backfills_names_from_what_tools_declare(tmp_path, fake_keyring):
    """Spec sec 5.10.a: the registry is backfilled from `Tool.requires_credentials`.
    After the backfill the credential is on the table."""
    from systemu.runtime import table_reconciler as tr
    fake_keyring.values["OWM_KEY"] = "a-real-secret-value"
    v = _real_vault_declaring(tmp_path, "OWM_KEY", "OTHER_KEY")

    assert tr.backfill_credential_names(v) == ["OWM_KEY"]
    key = ts.ref_key("credential_ref", {"credential_name": "OWM_KEY"})
    item = _by_key(tr.project(v)).get(key)
    assert item is not None and item.status == "ready"


def test_the_table_backfill_probes_each_declared_name_once(tmp_path, fake_keyring):
    """Once per daemon, not once per tick: a locked desktop keyring would otherwise
    be asked to unlock every ten minutes for keys that simply do not exist."""
    from systemu.runtime import table_reconciler as tr
    v = _real_vault_declaring(tmp_path, "MISSING_KEY")
    assert tr.backfill_credential_names(v) == []
    assert fake_keyring.reads.count("MISSING_KEY") == 1
    assert tr.backfill_credential_names(v) == []
    assert fake_keyring.reads.count("MISSING_KEY") == 1


def test_a_backfill_racing_a_delete_never_resurrects_the_deleted_name(tmp_path, monkeypatch):
    """The backfill is a BACKGROUND writer of the names registry; `delete` is an
    operator click. Unlocked, a backfill that loaded the registry before the delete
    and wrote after it would put the deleted name back -- a removed credential
    reading as present on the table."""
    import threading

    from systemu.runtime.credentials import store as cred_store

    probing, deleted = threading.Event(), threading.Event()

    class _SlowKeyring(_FakeKeyring):
        def get_password(self, service, key):
            if key == "NEW_KEY":
                probing.set()
                deleted.wait(timeout=0.5)     # give a racing delete its chance
            return super().get_password(service, key)

    kr = _SlowKeyring({"OLD_KEY": "old-secret-value", "NEW_KEY": "new-secret-value"})
    monkeypatch.setattr(cred_store, "usable_keyring", lambda: kr)
    cs = _cred_store(_Vault(tmp_path))
    cs._write_names(["OLD_KEY"])

    def _operator_deletes():
        probing.wait(timeout=5)
        _cred_store(_Vault(tmp_path)).delete("OLD_KEY")
        deleted.set()

    t = threading.Thread(target=_operator_deletes)
    t.start()
    cs.backfill_names({"NEW_KEY"})
    t.join(timeout=10)
    assert cs.list_names() == ["NEW_KEY"], "the deleted name came back"


# =========================================================================== #
# 3. the health tick: registered with the daemon's scheduler, runs the checks
# =========================================================================== #

def _daemon_loop_ast() -> ast.FunctionDef:
    tree = ast.parse((_SYSTEMU / "scheduler" / "daemon.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if type(n) is ast.FunctionDef and n.name == "_run_daemon_loop")


def test_the_daemon_registers_the_table_health_tick():
    """Structural pin on the ONE production registration site (no daemon is
    started): an interval job on the daemon's APScheduler - its worker pool, never
    the UI thread - whose cadence comes from `health_interval_minutes()`."""
    fn = _daemon_loop_ast()
    imported = {a.name for n in ast.walk(fn) if type(n) is ast.ImportFrom
                and n.module == "systemu.scheduler.jobs" for a in n.names}
    assert "_table_health_job" in imported

    regs = [n for n in ast.walk(fn)
            if type(n) is ast.Call and type(n.func) is ast.Attribute
            and n.func.attr == "add_job" and n.args
            and type(n.args[0]) is ast.Name and n.args[0].id == "_table_health_job"]
    assert len(regs) == 1, "the table health tick must be registered exactly once"
    kw = {k.arg: k.value for k in regs[0].keywords}
    assert type(kw["trigger"]) is ast.Constant and kw["trigger"].value == "interval"
    assert type(kw["id"]) is ast.Constant and kw["id"].value == "table_health"
    minutes = kw["minutes"]
    assert type(minutes) is ast.Call and type(minutes.func) is ast.Name \
        and minutes.func.id == "health_interval_minutes"


def test_the_tick_runs_the_health_checks(tmp_path, no_keyring, monkeypatch):
    from systemu.runtime import table_reconciler as tr
    from systemu.scheduler import jobs
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    _cred_store(v).delete("gh_pat")

    monkeypatch.setattr(jobs, "_vault", v)
    jobs._table_health_job()

    item = _by_key(ts.load_items(v))[_CRED_KEY]
    assert item.status == "broken" and item.broken_reason == "credential removed"


def test_the_tick_is_a_noop_before_the_daemon_initialises_it(monkeypatch):
    from systemu.scheduler import jobs
    monkeypatch.setattr(jobs, "_vault", None)
    jobs._table_health_job()                       # must not raise


def test_a_health_pass_backfills_names_then_reconciles(tmp_path, fake_keyring):
    from systemu.runtime import table_reconciler as tr
    fake_keyring.values["OWM_KEY"] = "a-real-secret-value"
    v = _real_vault_declaring(tmp_path, "OWM_KEY")

    summary = tr.run_health_checks(v)

    assert summary == {"items": summary["items"], "broken": 0, "backfilled": 1}
    key = ts.ref_key("credential_ref", {"credential_name": "OWM_KEY"})
    assert _by_key(ts.load_items(v))[key].status == "ready", \
        "the backfilled name is on the table after ONE pass"


def test_a_health_pass_counts_what_it_found_broken(tmp_path, no_keyring):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    _cred_store(v).delete("gh_pat")
    assert tr.run_health_checks(v)["broken"] == 1


def test_a_health_pass_never_raises(tmp_path):
    from systemu.runtime import table_reconciler as tr
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    out = tr.run_health_checks(_Vault(not_a_dir))
    assert set(out) == {"items", "broken", "backfilled"}


@pytest.mark.parametrize("raw,minutes", [
    (None, 10), ("10", 10), ("3", 3), ("0", 1), ("-5", 1), ("99999", 1440),
    ("ten", 10), ("", 10), (" 7 ", 7),
])
def test_the_tick_cadence_is_ten_minutes_by_default_and_configurable(
        monkeypatch, raw, minutes):
    from systemu.runtime import table_reconciler as tr
    if raw is None:
        monkeypatch.delenv("SYSTEMU_TABLE_HEALTH_MINUTES", raising=False)
    else:
        monkeypatch.setenv("SYSTEMU_TABLE_HEALTH_MINUTES", raw)
    assert tr.health_interval_minutes() == minutes


def test_the_health_tick_and_the_60s_reconcile_never_write_concurrently(tmp_path, monkeypatch):
    """Two scheduler jobs now drive the ONE writer of items.json. Its write is
    serialized, so they cannot interleave (on Windows the losing os.replace of an
    overlapping pair raises)."""
    import threading
    import time

    from systemu.runtime import table_reconciler as tr

    state = {"inside": 0, "peak": 0}
    guard = threading.Lock()
    real_project = tr.project

    def _slow_project(vault):
        with guard:
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
        time.sleep(0.05)
        try:
            return real_project(vault)
        finally:
            with guard:
                state["inside"] -= 1

    monkeypatch.setattr(tr, "project", _slow_project)
    v = _Vault(tmp_path)
    threads = [threading.Thread(target=tr.reconcile_once, args=(v,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert state["peak"] == 1


# =========================================================================== #
# 5. broken items count in "Needs you"; /health carries a "Table" row
# =========================================================================== #

def _vault_with_a_broken_credential(tmp_path):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path)
    _cred_store(v).set("gh_pat", "s3cret-value")
    tr.reconcile_once(v)
    _cred_store(v).delete("gh_pat")
    tr.reconcile_once(v)
    return v


def test_a_broken_item_counts_in_needs_you_with_its_reason(tmp_path, no_keyring):
    from systemu.interface.components.attention import needs_you_breakdown
    b = needs_you_breakdown(_vault_with_a_broken_credential(tmp_path))
    assert b["table_broken"] == 1
    assert b["total"] == b["gates"] + b["asks"] + b["table_suggestions"] + 1
    (row,) = b["table_broken_rows"]
    assert row["line"] == "gh_pat: credential removed"
    assert row["href"] == "/table" and row["since"]


def test_the_badge_points_at_the_table_when_a_broken_item_is_all_that_waits(
        tmp_path, no_keyring):
    from systemu.interface.dashboard import needs_you_badge_model
    m = needs_you_badge_model(_vault_with_a_broken_credential(tmp_path))
    assert m["count"] == 1 and m["visible"] is True and m["target"] == "/table"


def test_removing_the_broken_item_clears_the_attention(tmp_path, no_keyring):
    from systemu.interface.components.attention import needs_you_breakdown
    from systemu.runtime import table_reconciler as tr
    v = _vault_with_a_broken_credential(tmp_path)
    ts.add_tombstone(v, _CRED_KEY)
    tr.reconcile_once(v)
    b = needs_you_breakdown(v)
    assert b["table_broken"] == 0 and b["table_broken_rows"] == []


def test_a_broken_status_without_a_health_reason_does_not_nag(tmp_path):
    """Only HEALTH-broken items (something relied on went away) ask for the
    operator. A tool whose dry run failed is `broken` too, but it already has its
    own fail card, and a forge-rejected tool was the operator's own decision."""
    from systemu.interface.components.attention import needs_you_breakdown
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path, tools=[{"id": "t2", "name": "poster", "enabled": False,
                                 "dry_run_status": "failed"}])
    tr.reconcile_once(v)
    assert any(it.status == "broken" for it in ts.load_items(v))
    assert needs_you_breakdown(v)["table_broken"] == 0


def test_the_table_row_on_health_says_how_many_are_broken(tmp_path, no_keyring):
    from systemu.interface.components.attention import needs_you_breakdown
    from systemu.interface.pages.health import table_status_text
    assert table_status_text(
        needs_you_breakdown(_vault_with_a_broken_credential(tmp_path))) == "1 broken"


def test_the_table_row_never_reassures_when_it_could_not_look(tmp_path):
    """/health is reachable precisely when the install is broken; '0 broken' read
    from a table nobody has checked would be a reassuring claim it cannot back."""
    from systemu.interface.components.attention import needs_you_breakdown
    from systemu.interface.pages.health import table_status_text
    v = _Vault(tmp_path)
    assert table_status_text(None).startswith("unknown")
    assert table_status_text(needs_you_breakdown(v)).startswith("not checked yet")
    (tmp_path / "table").mkdir()
    (tmp_path / "table" / "items.json").write_text("not json {", encoding="utf-8")
    assert table_status_text(needs_you_breakdown(v)).startswith("unknown")
    for text in (table_status_text(None), table_status_text(needs_you_breakdown(v))):
        assert text.isascii()


def test_the_health_page_renders_the_table_row_and_the_broken_lines():
    """Wiring pin: the helpers above are only worth anything if /health renders
    them. Parsed from build_health_page itself."""
    import inspect

    from systemu.interface.pages import health
    src = inspect.getsource(health.build_health_page)
    tree = ast.parse(inspect.cleandoc("\n" + src))
    called = {n.func.id for n in ast.walk(tree)
              if type(n) is ast.Call and type(n.func) is ast.Name}
    assert "table_status_text" in called
    consts = {n.value for n in ast.walk(tree)
              if type(n) is ast.Constant and type(n.value) is str}
    assert "Table" in consts, "the Status section has a 'Table' row"
    assert "table_broken" in consts and "table_broken_rows" in consts


# =========================================================================== #
# 2b. a revoked grant is only "broken" when access is actually gone
# =========================================================================== #

def test_a_revoked_root_has_no_data_root_left(tmp_path):
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    work = _folder(tmp_path, "work")
    canon = _grants(v).grant(str(work))
    tr.reconcile_once(v)
    _grants(v).revoke(str(work))
    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v))[_root_key(canon)]
    assert item.status == "broken" and item.data_root is None, \
        "no granted root covers it any more"


def test_revoking_a_nested_grant_under_a_still_granted_parent_is_not_a_break(tmp_path):
    """Revoke `work/sub` while `work` stays granted: the folder is still reachable
    through the parent grant, so "folder grant revoked" would be a false alarm. The
    card stays, `ready`, with its data_root moved to the parent."""
    from systemu.runtime import table_reconciler as tr
    v = _Vault(tmp_path / "vault")
    parent = _grants(v).grant(str(_folder(tmp_path, "work")))
    sub_path = _folder(tmp_path, "work", "sub")
    sub = _grants(v).grant(str(sub_path))
    tr.reconcile_once(v)

    _grants(v).revoke(str(sub_path))
    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v)).get(_root_key(sub))
    assert item is not None, "never silently deleted"
    assert item.status == "ready" and item.broken_reason == ""
    assert item.data_root == parent

    _grants(v).revoke(str(_folder(tmp_path, "work")))
    tr.reconcile_once(v)
    item = _by_key(ts.load_items(v))[_root_key(sub)]
    assert item.status == "broken" and item.broken_reason == "folder grant revoked"
