"""P19 / R-W2 -- the census consent surface on ``/table``.

WM-7: each census category is a consent "visible on the table"; the spec 5.11 ownership
rule: "census consents render on the table". Before this slice ``/table`` had no census
reference at all, so an operator who had granted a standing scan could not see it there.

Two layers, pinned separately:

  * ``ambient_census.census_overview`` -- the data. Every declared category with its state
    (granted / paused / not granted / unavailable + reason), what it reads, and a fact
    count ONLY where a scan witnessed one (DEC-27: a never-scanned category is never "0
    found"; an unreadable store is never 0). Read-only: a fresh vault gains no file.
  * ``interface/pages/table_census.py`` -- the panel. Reachability (the page calls it; it
    fetches through ``run.io_bound``), the render (a recording ``ui``), and the honesty of
    its controls (a command is named only where the CLI really offers it -- DEC-34).
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

from systemu.interface.pages import table, table_census
from systemu.runtime import ambient_census as ac
from systemu.runtime import census_consent as cc
from systemu.runtime.world_model import Fact, FactStore, ProvStep, fact_id_for


def _vault(tmp_path):
    return SimpleNamespace(root=tmp_path)


def _rows(overview):
    return {r["category"]: r for r in overview["rows"]}


def _census_fact(category, kind, value):
    return Fact(fact_id=fact_id_for(kind, value), kind=kind, value=value,
                origin_class="content_derived", confidence=1.0,
                last_confirmed="2026-10-19T00:00:00+00:00",
                source_chain=[ProvStep(source_kind="census", ref=category,
                                       at="2026-10-19T00:00:00+00:00")])


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ══ the overview ═════════════════════════════════════════════════════════════

def test_a_fresh_vault_shows_every_category_and_writes_nothing(tmp_path):
    before = _tree(tmp_path)
    ov = ac.census_overview(_vault(tmp_path))
    assert _tree(tmp_path) == before, "reading the overview created state in the vault"
    rows = _rows(ov)
    assert set(rows) == set(cc.CATEGORIES)
    assert ov["refused"] == ""
    for cat, row in rows.items():
        assert row["state"] in (ac.STATE_NOT_GRANTED, ac.STATE_UNAVAILABLE), cat
        assert row["facts_held"] is None, f"{cat}: a never-scanned category has a count"
        assert row["facts_line"] == "", cat


def test_unavailable_always_carries_its_reason_and_available_never_does(tmp_path):
    for row in ac.census_overview(_vault(tmp_path))["rows"]:
        if row["state"] == ac.STATE_UNAVAILABLE:
            assert row["reason"].strip(), row["category"]
        else:
            assert row["reason"] == "", row["category"]


def test_a_category_with_no_shipped_control_says_so(tmp_path):
    """DEC-34: the panel must not present a category as something the operator can turn
    on when no surface in this build can grant it."""
    rows = _rows(ac.census_overview(_vault(tmp_path)))
    for cat, row in rows.items():
        if cat in cc.SURFACED_CATEGORIES:
            assert row["controls_shipped"] is True
        else:
            assert row["controls_shipped"] is False
            assert row["state"] == ac.STATE_UNAVAILABLE
            assert row["reason"] == ac.NO_CONTROL_REASON or \
                row["reason"].startswith("not available on this machine")


def test_a_platform_gap_is_reported_as_the_platform_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_platform_family", lambda: "macos")
    rows = _rows(ac.census_overview(_vault(tmp_path)))
    assert rows["automation_apps"]["state"] == ac.STATE_UNAVAILABLE
    assert "windows" in rows["automation_apps"]["reason"].lower()


def test_granted_but_never_scanned_is_not_scanned_yet_never_zero(tmp_path):
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    row = _rows(ac.census_overview(v))["cloud_sync_roots"]
    assert row["state"] == ac.STATE_GRANTED
    assert row["facts_held"] is None
    assert row["facts_line"] == "Not scanned yet."
    assert "0" not in row["facts_line"]


def test_a_scan_that_found_nothing_says_so_with_its_time(tmp_path, monkeypatch):
    monkeypatch.setitem(ac.PROBES, "cloud_sync_roots",
                        (lambda limit, budget: [], "cloud_sync_root"))
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    ac.run_census(v, now="2026-10-19T09:00:00+00:00")
    row = _rows(ac.census_overview(v))["cloud_sync_roots"]
    assert row["facts_held"] == 0
    assert "2026-10-19T09:00:00+00:00" in row["facts_line"]
    assert "recorded nothing" in row["facts_line"]


def test_a_scan_that_found_something_shows_the_count(tmp_path, monkeypatch):
    monkeypatch.setitem(ac.PROBES, "cloud_sync_roots",
                        (lambda limit, budget: ["D:/sync/a", "D:/sync/b"],
                         "cloud_sync_root"))
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    ac.run_census(v)
    row = _rows(ac.census_overview(v))["cloud_sync_roots"]
    assert row["facts_held"] == 2 and "holding 2 fact(s)" in row["facts_line"]


def test_paused_is_shown_as_paused(tmp_path):
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    ac.pause_category(v, "cloud_sync_roots", True)
    assert _rows(ac.census_overview(v))["cloud_sync_roots"]["state"] == ac.STATE_PAUSED


def test_an_unreadable_fact_store_is_unknown_never_zero(tmp_path):
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    ac.run_census(v, now="2026-10-19T09:00:00+00:00")
    (tmp_path / "world_model").mkdir(exist_ok=True)
    (tmp_path / "world_model" / "facts.json").write_text("{ not json", encoding="utf-8")
    ov = ac.census_overview(v)
    assert ov["facts_readable"] is False
    row = _rows(ov)["cloud_sync_roots"]
    assert row["facts_held"] is None
    assert "unknown" in row["facts_line"].lower()


def test_facts_left_after_consent_ends_are_labelled_as_such(tmp_path):
    """The spec PURGES on withdrawal (WM-7), so this state is reached only when a purge did
    not run -- e.g. the consent file was refused. It is then labelled, never hidden."""
    v = _vault(tmp_path)
    FactStore(v).put_facts([_census_fact("cloud_sync_roots", "cloud_sync_root", "D:/x")])
    row = _rows(ac.census_overview(v))["cloud_sync_roots"]
    assert row["state"] == ac.STATE_NOT_GRANTED
    assert row["facts_held"] == 1
    assert "gathered before consent was withdrawn" in row["facts_line"]


def test_withdrawal_follows_the_spec_and_purges(tmp_path, monkeypatch):
    """WM-7 is not silent on what happens to gathered data: revocation purges it. After a
    revoke the overview shows no count and no residue."""
    monkeypatch.setitem(ac.PROBES, "cloud_sync_roots",
                        (lambda limit, budget: ["D:/sync/a"], "cloud_sync_root"))
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    ac.run_census(v)
    ac.revoke_category(v, "cloud_sync_roots")
    row = _rows(ac.census_overview(v))["cloud_sync_roots"]
    assert row["state"] == ac.STATE_NOT_GRANTED
    assert row["facts_held"] is None and row["facts_line"] == ""


def test_a_refused_consent_file_is_said_and_grants_nothing(tmp_path):
    (tmp_path / "census_consent.json").write_text(
        '{"version": 1, "grants": {"cloud_sync_roots": '
        '{"granted_at": "2026-07-21T10:00:00+00:00"}}}', encoding="utf-8")
    ov = ac.census_overview(_vault(tmp_path))
    assert ov["refused"] and "refused" in ov["refused"]
    assert _rows(ov)["cloud_sync_roots"]["state"] == ac.STATE_NOT_GRANTED


def test_no_vault_root_shows_nothing_rather_than_a_guess():
    assert ac.census_overview(SimpleNamespace()) is None
    assert ac.census_overview(None) is None


def test_every_overview_string_is_ascii(tmp_path):
    def walk(x):
        if isinstance(x, str):
            assert x.isascii(), x
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(ac.census_overview(_vault(tmp_path)))


def test_the_overview_reads_consent_through_the_scanners_own_gate(tmp_path):
    """The panel and the scanner must agree. A granted state on the panel comes from the
    same authenticated load ``run_census`` gates on, so a tampered file shows NOT granted
    on the panel exactly when the scanner refuses it."""
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    path = tmp_path / "census_consent.json"
    path.write_text(path.read_text(encoding="utf-8").replace("false", "true"),
                    encoding="utf-8")                  # flips `paused`; MAC now wrong
    assert cc.CensusConsentStore(tmp_path).is_active("cloud_sync_roots") is False
    assert _rows(ac.census_overview(v))["cloud_sync_roots"]["state"] == \
        ac.STATE_NOT_GRANTED


# ══ the panel: reachability ══════════════════════════════════════════════════

def _called_names(src):
    return {n.func.id for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_the_table_page_mounts_the_census_panel():
    """THE REACHABILITY PIN for the consent surface. Delete the call in
    ``build_table_page`` and this goes red."""
    assert "build_census_panel" in _called_names(inspect.getsource(table.build_table_page))
    assert ("from systemu.interface.pages.table_census import build_census_panel"
            in inspect.getsource(table))


def test_the_panel_fetches_the_overview_off_the_ui_thread():
    """R-UX2: the overview reads files and derives HMAC keys. It must reach the panel
    through ``run.io_bound`` -- and never be CALLED on the loop."""
    tree = ast.parse(inspect.getsource(table_census))
    offloaded = direct = 0
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == "io_bound" \
                and isinstance(f.value, ast.Name) and f.value.id == "run" \
                and n.args and isinstance(n.args[0], ast.Name) \
                and n.args[0].id == "census_overview":
            offloaded += 1
        if isinstance(f, ast.Name) and f.id == "census_overview":
            direct += 1
    assert offloaded == 1, "the panel no longer fetches the overview via run.io_bound"
    assert direct == 0, "census_overview is called directly -- that is the UI thread"


def test_the_panel_passes_the_offload_lint():
    from tools.lint_offload import find_violations
    src = inspect.getsource(table_census)
    assert find_violations(src, "systemu/interface/pages/table_census.py") == []


def test_the_panels_consent_writers_live_only_in_its_declared_region():
    """The panel withdraws, pauses and resumes -- every such reference below its region
    marker -- and NEVER grants (ruled: grant is CLI-only). It never constructs a store or
    stamps a scan either."""
    src = inspect.getsource(table_census)
    marker = "R-W2 CENSUS CONSENT SURFACE :: REGION START"
    head, _sep, region = src.partition(marker)
    assert region, "the panel lost its declared region marker"
    for writer in ("revoke_category", "pause_category"):
        assert writer not in head, writer
        assert writer in region, writer
    for never in ("grant_category", "CensusConsentStore", "mark_ran", "set_paused",
                  "run_census_grant"):
        assert never not in src, never


# ══ the panel: render ════════════════════════════════════════════════════════

class _Node:
    def __getattr__(self, name):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingUI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node()
        return _call

    def text(self, kind):
        return [str(a[0]) for n, a, _k in self.calls if n == kind and a]


def _render(monkeypatch, overview):
    rec = _RecordingUI()
    monkeypatch.setattr(table_census, "ui", rec)
    table_census._render_overview(overview)
    return rec


def test_the_panel_renders_every_category_its_state_and_what_it_reads(
        tmp_path, monkeypatch):
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")
    ov = ac.census_overview(v)
    rec = _render(monkeypatch, ov)
    labels = "\n".join(rec.text("label"))
    badges = rec.text("badge")
    for row in ov["rows"]:
        assert f"{row['title']} ({row['category']})" in labels, row["category"]
        assert table_census.reads_line(row) in labels
        assert table_census.never_reads_line(row) in labels
        if row["state"] == ac.STATE_UNAVAILABLE:
            assert "Why: " + row["reason"] in labels, row["category"]
    assert "Granted" in badges
    assert "Not scanned yet." in labels
    assert "systemu census revoke cloud_sync_roots" in labels


def test_the_panel_names_no_command_the_cli_would_refuse(tmp_path, monkeypatch):
    rec = _render(monkeypatch, ac.census_overview(_vault(tmp_path)))
    labels = "\n".join(rec.text("label"))
    for cat in set(cc.CATEGORIES) - set(cc.SURFACED_CATEGORIES):
        assert f"systemu census grant {cat}" not in labels, cat
    assert "systemu census grant cloud_sync_roots" in labels


def test_a_failed_read_renders_the_failure_not_an_empty_census(monkeypatch):
    rec = _render(monkeypatch, None)
    assert rec.text("label") == [table_census.CENSUS_PANEL_FAILED]
    assert rec.text("badge") == []


def test_a_refused_file_and_an_unreadable_store_are_both_said(monkeypatch):
    rec = _render(monkeypatch, {"refused": "a consent file exists but was refused (x)",
                                "facts_readable": False, "rows": []})
    labels = rec.text("label")
    assert "a consent file exists but was refused (x)" in labels
    assert table_census.CENSUS_FACTS_UNREADABLE in labels


def test_an_unknown_state_never_renders_as_a_reassuring_label():
    assert table_census.state_label({"state": "weird"}) == "Unknown"
    assert table_census.state_label({}) == "Unknown"
    assert table_census.reason_line({"state": ac.STATE_UNAVAILABLE}) == \
        "Why: no reason was recorded"


def test_control_lines_follow_the_state_and_the_shipped_scope():
    base = {"category": "cloud_sync_roots", "controls_shipped": True}
    assert [ln.split(": ", 1)[1] for ln in table_census.control_lines(
        {**base, "state": ac.STATE_GRANTED})] == [
        "systemu census pause cloud_sync_roots", "systemu census revoke cloud_sync_roots"]
    assert [ln.split(": ", 1)[1] for ln in table_census.control_lines(
        {**base, "state": ac.STATE_PAUSED})] == [
        "systemu census resume cloud_sync_roots", "systemu census revoke cloud_sync_roots"]
    assert [ln.split(": ", 1)[1] for ln in table_census.control_lines(
        {**base, "state": ac.STATE_NOT_GRANTED})] == [
        "systemu census grant cloud_sync_roots"]
    assert table_census.control_lines({**base, "controls_shipped": False,
                                       "state": ac.STATE_NOT_GRANTED}) == []


def test_every_panel_string_constant_is_ascii():
    for name in dir(table_census):
        if name.startswith("CENSUS_"):
            val = getattr(table_census, name)
            vals = val.values() if isinstance(val, dict) else [val]
            for s in vals:
                assert isinstance(s, str) and s.isascii(), name


def test_the_panel_copy_states_the_standing_scan_and_the_transmission():
    """Re-audit items 1 and 2 of the grant-surface guard apply to any surface that shows
    the census: it must say the permission is STANDING and that findings reach the model
    provider, and it must say what withdrawing does."""
    blurb = table_census.CENSUS_PANEL_BLURB.lower()
    assert "standing" in blurb
    assert "model provider" in blurb
    assert "withdraw" in blurb and "deletes" in blurb


def test_no_operator_facing_card_text_names_an_internal_field():
    """The installed_apps card told the operator to 'see `transmission_notice`' -- a
    field name, meaningless outside this code. Every card string an operator reads (CLI
    card, /table panel) must say it in words."""
    for category in cc.CATEGORIES:
        card = cc.consent_card(category)
        fields = set(card)
        texts = [v for v in card.values() if isinstance(v, str)]
        texts += [x for k in ("collects", "excludes") for x in card[k]]
        for text in texts:
            for field in fields:
                assert f"`{field}`" not in text and f"see {field}" not in text, (
                    category, field, text)
