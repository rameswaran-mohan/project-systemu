"""R-A9 T6: OnTheTable composition (Callout-3 floor — "curation re-ranks, NEVER
subtracts").

compose_table(report, table_items) ANNOTATES matched live entries
(curated=True + table_item_id) and ADDS declared_intents for table items that
reference something absent from the live inventory. It can NEVER subtract a live
store object — the report is identical-or-annotated, never diminished, whether the
table is empty, full, or full of tombstoned/absent references.

Keyless-testable: SituationReport + TableItem are constructed directly (Task 7
wires load_items(vault) → compose_table).
"""
from systemu.runtime.situational_inventory import (
    CapabilityRef,
    ConnectedService,
    RootSurvey,
    SituationReport,
    compose_table,
)
from systemu.runtime.table_store import TableItem


def _svc_item(id, server, name="svc", status="ready"):
    return TableItem(id=id, kind="mcp_server", name=name, status=status,
                     ref={"server": server})


def _tool_item(id, tool_id, name="tool", status="ready"):
    return TableItem(id=id, kind="tool", name=name, status=status,
                     ref={"tool_id": tool_id})


def _root_item(id, root_path, name="root", status="ready"):
    return TableItem(id=id, kind="data_root", name=name, status=status,
                     ref={"root_path": root_path})


# ── Annotate / match ─────────────────────────────────────────────────────────
def test_annotates_matched_service_capability_root():
    report = SituationReport(
        services=[ConnectedService(name="https://mcp.x/", auth_kind="oauth",
                                   has_live_token=True)],
        capabilities=[CapabilityRef(tool_id="t1", effect_tags=[])],
        roots=[RootSurvey(path="/g")],
    )
    # server URL WITHOUT the trailing slash: match must survive rstrip("/").
    items = [
        _svc_item("i-svc", "https://mcp.x"),
        _tool_item("i-tool", "t1"),
        _root_item("i-root", "/g"),
    ]
    out = compose_table(report, items)

    assert out.services[0].curated is True
    assert out.services[0].table_item_id == "i-svc"
    assert out.capabilities[0].curated is True
    assert out.capabilities[0].table_item_id == "i-tool"
    assert out.roots[0].curated is True
    assert out.roots[0].table_item_id == "i-root"

    # all three matched -> nothing left over to declare.
    assert out.declared_intents == []


def test_data_root_match_is_normcase_insensitive():
    # os.path.normcase lowercases on Windows; the match must go through it.
    report = SituationReport(roots=[RootSurvey(path="/Data/Proj")])
    items = [_root_item("i-root", "/Data/Proj")]
    out = compose_table(report, items)
    assert out.roots[0].curated is True
    assert out.roots[0].table_item_id == "i-root"


# ── NEVER subtract (the Callout-3 floor) ─────────────────────────────────────
def test_never_subtracts_live_services_with_unrelated_table():
    # Two live services + a NON-EMPTY table that references NEITHER of them.
    report = SituationReport(
        services=[
            ConnectedService(name="https://a.example", auth_kind="none",
                             has_live_token=False),
            ConnectedService(name="https://b.example", auth_kind="none",
                             has_live_token=False),
        ],
    )
    items = [_svc_item("i-other", "https://elsewhere.example")]
    out = compose_table(report, items)

    # Floor: both live services SURVIVE, count unchanged, uncurated.
    assert len(out.services) == 2
    assert {s.name for s in out.services} == {"https://a.example", "https://b.example"}
    assert all(s.curated is False for s in out.services)
    assert all(s.table_item_id is None for s in out.services)


def test_absent_reference_never_drops_a_live_entry():
    # A table item that "removed"/references a now-absent thing must NEVER drop
    # any live entry (tombstones affect the /table view, not the inventory).
    report = SituationReport(
        services=[ConnectedService(name="https://keep.me", auth_kind="none",
                                   has_live_token=False)],
        capabilities=[CapabilityRef(tool_id="keep-tool", effect_tags=[])],
        roots=[RootSurvey(path="/keep")],
    )
    # references point at things that are NOT in the live inventory.
    items = [
        _svc_item("i-gone-svc", "https://gone.svc", status="stale"),
        _tool_item("i-gone-tool", "gone-tool", status="broken"),
        _root_item("i-gone-root", "/gone", status="stale"),
    ]
    out = compose_table(report, items)

    # NEVER subtract: every live entry survives, uncurated.
    assert len(out.services) == 1 and out.services[0].name == "https://keep.me"
    assert out.services[0].curated is False
    assert len(out.capabilities) == 1 and out.capabilities[0].tool_id == "keep-tool"
    assert out.capabilities[0].curated is False
    assert len(out.roots) == 1 and out.roots[0].path == "/keep"
    assert out.roots[0].curated is False


# ── declared_intents (the SOLE add-channel) ──────────────────────────────────
def test_unmatched_data_root_becomes_declared_intent_not_a_fake_root():
    # A data_root item pointing at a path NOT in live roots -> a declared_intents
    # row, NEVER a fake RootSurvey; report.roots count unchanged.
    report = SituationReport(roots=[RootSurvey(path="/live")])
    items = [_root_item("i-declared", "/not/live/yet", name="future-root",
                        status="declared")]
    out = compose_table(report, items)

    # roots is NOT inflated with a fake entry.
    assert len(out.roots) == 1
    assert out.roots[0].path == "/live"
    assert out.roots[0].curated is False

    # the unmatched item is declared, add-only.
    assert len(out.declared_intents) == 1
    di = out.declared_intents[0]
    assert di["id"] == "i-declared"
    assert di["kind"] == "data_root"
    assert di["name"] == "future-root"
    assert di["status"] == "declared"
    # IMPL-5: declared_intents now carries the item's origin_class (taint travels).
    assert di["origin_class"] == "operator"          # TableItem default origin
    assert set(di.keys()) == {"id", "kind", "name", "detail", "status", "origin_class"}


def test_matched_item_is_not_also_declared():
    # An item that DID match a live entry must not ALSO appear as a declared intent.
    report = SituationReport(
        services=[ConnectedService(name="https://mcp.x/", auth_kind="oauth",
                                   has_live_token=True)],
    )
    items = [_svc_item("i-svc", "https://mcp.x")]
    out = compose_table(report, items)
    assert out.services[0].curated is True
    assert out.declared_intents == []


def test_preference_item_is_declared_only():
    # A 'preference' item references no live store object -> declared-intent only,
    # never a fake service/capability/root.
    report = SituationReport(
        services=[ConnectedService(name="https://s", auth_kind="none",
                                   has_live_token=False)],
    )
    items = [TableItem(id="i-pref", kind="preference", name="dark-mode",
                       status="declared", ref={"name": "dark-mode"})]
    out = compose_table(report, items)
    assert len(out.services) == 1          # no fake service added
    assert len(out.capabilities) == 0
    assert len(out.roots) == 0
    assert len(out.declared_intents) == 1
    assert out.declared_intents[0]["id"] == "i-pref"
    assert out.declared_intents[0]["kind"] == "preference"


# ── Optional table ───────────────────────────────────────────────────────────
def test_empty_table_leaves_report_unchanged():
    report = SituationReport(
        services=[ConnectedService(name="https://s", auth_kind="none",
                                   has_live_token=False)],
        capabilities=[CapabilityRef(tool_id="t", effect_tags=[])],
        roots=[RootSurvey(path="/r")],
    )
    out = compose_table(report, [])
    assert len(out.services) == 1 and out.services[0].curated is False
    assert len(out.capabilities) == 1 and out.capabilities[0].curated is False
    assert len(out.roots) == 1 and out.roots[0].curated is False
    assert out.declared_intents == []


# ── Defensive ────────────────────────────────────────────────────────────────
def test_malformed_table_item_is_skipped_no_raise():
    # A malformed table item (not a TableItem, missing fields) must be skipped,
    # never raise, and never drop a live entry.
    report = SituationReport(
        services=[ConnectedService(name="https://s", auth_kind="none",
                                   has_live_token=False)],
    )
    items = [
        {"not": "a table item"},          # a bare dict, missing everything
        None,                             # outright junk
        _svc_item("i-ok", "https://s"),   # a valid one that should still match
    ]
    out = compose_table(report, items)    # must not raise
    assert len(out.services) == 1
    assert out.services[0].curated is True         # the valid item still matched
    assert out.services[0].table_item_id == "i-ok"


def test_whole_failure_returns_report_unchanged():
    report = SituationReport(
        services=[ConnectedService(name="https://s", auth_kind="none",
                                   has_live_token=False)],
    )
    # table_items that isn't iterable at all -> whole-failure path returns report.
    out = compose_table(report, 12345)  # type: ignore[arg-type]
    assert out is report or len(out.services) == 1


# ── Empty-ref no-false-match (Fix 1: defense-in-depth) ───────────────────────
def test_empty_service_ref_matches_nothing_and_is_declared():
    # A live service whose name is "" plus a service table item with an EMPTY ref
    # ({} -> target "") must NOT false-match: the empty-name live service must stay
    # uncurated, and the empty-ref item — matching nothing — becomes a declared row.
    report = SituationReport(
        services=[ConnectedService(name="", auth_kind="none",
                                   has_live_token=False)],
    )
    items = [TableItem(id="i-empty-svc", kind="service", name="empty",
                       status="declared", ref={})]
    out = compose_table(report, items)

    # NEVER subtract + NEVER false-annotate: the empty-name live service survives,
    # uncurated (no crafted empty ref may claim it).
    assert len(out.services) == 1
    assert out.services[0].name == ""
    assert out.services[0].curated is False
    assert out.services[0].table_item_id is None

    # the empty-ref item matched nothing -> a declared_intents row.
    assert len(out.declared_intents) == 1
    assert out.declared_intents[0]["id"] == "i-empty-svc"
    assert out.declared_intents[0]["kind"] == "service"


def test_empty_data_root_ref_matches_nothing_and_is_declared():
    # Mirror of the above for the data_root branch: a live root with path "" plus a
    # data_root item with an empty ref must not false-match.
    report = SituationReport(roots=[RootSurvey(path="")])
    items = [TableItem(id="i-empty-root", kind="data_root", name="empty",
                       status="declared", ref={})]
    out = compose_table(report, items)

    assert len(out.roots) == 1
    assert out.roots[0].path == ""
    assert out.roots[0].curated is False
    assert out.roots[0].table_item_id is None

    assert len(out.declared_intents) == 1
    assert out.declared_intents[0]["id"] == "i-empty-root"
    assert out.declared_intents[0]["kind"] == "data_root"


# ── Multi-match / duplicate items (pin under-specified behavior) ─────────────
def test_ref_matching_two_live_entries_curates_both():
    # One table item whose ref matches TWO live entries of its kind -> BOTH get
    # curated=True with that table_item_id, and the item is NOT declared.
    report = SituationReport(
        services=[
            ConnectedService(name="https://dup.x", auth_kind="none",
                             has_live_token=False),
            ConnectedService(name="https://dup.x/", auth_kind="none",
                             has_live_token=False),   # rstrip("/") -> same target
        ],
    )
    items = [_svc_item("i-dup", "https://dup.x")]
    out = compose_table(report, items)

    assert all(s.curated is True for s in out.services)
    assert all(s.table_item_id == "i-dup" for s in out.services)
    assert out.declared_intents == []


def test_two_distinct_items_same_ref_yield_two_declared_rows():
    # Two DISTINCT table items (distinct ids) with the same unmatched ref -> TWO
    # declared_intents rows (one per id). Distinct items are preserved, not deduped.
    report = SituationReport(roots=[RootSurvey(path="/live")])
    items = [
        _root_item("i-a", "/not/live", name="a"),
        _root_item("i-b", "/not/live", name="b"),
    ]
    out = compose_table(report, items)

    assert len(out.roots) == 1 and out.roots[0].curated is False
    assert len(out.declared_intents) == 2
    assert {di["id"] for di in out.declared_intents} == {"i-a", "i-b"}
