"""P19 / R-W2 -- the census widened to WM-7's remaining read-only categories.

WM-7 names: installed applications, CLIs on PATH (+ auth state), COM ProgIDs /
UIA-automatable apps, cloud-sync roots, default file-type handlers, devices/printers. Before
this slice three of those had probes. This file pins the three added here --
``automation_apps``, ``file_handlers``, ``installed_printers`` -- on the properties the
existing three are held to:

  * consent-gated per category: an ungranted category's SOURCE is never touched (asserted
    on the fake registry's open log, not on a fact count);
  * read-only by construction: every registry handle is opened with ``KEY_READ``;
  * a FIXED published list, never an enumeration;
  * nothing that identifies the operator is recorded: no path, no account SID;
  * an UNAVAILABLE category is never scanned and never stamped, so it can never read as
    "scanned, found nothing" (DEC-27);
  * revoking purges what it found (WM-7: "revoking a category stops future scans AND purges
    its derived facts").

The registry is a recorded fake handed in through ``ambient_census._winreg`` so these run
on every platform; the REAL probes are also run, bounded, against this machine.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.runtime import ambient_census as ac
from systemu.runtime import census_consent as cc
from systemu.runtime.world_model import FactStore

NEW = ("automation_apps", "file_handlers", "installed_printers")


# ── a recorded registry ──────────────────────────────────────────────────────

class _Key:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def Close(self):
        return None


class FakeWinreg:
    """A registry as ``{"HIVE\\\\sub\\\\key": {value_name: value}}``. Records every open
    and the access mask it was asked for."""

    HKEY_CLASSES_ROOT = "HKCR"
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 0x20019

    def __init__(self, tree):
        self._orig = {k.lower(): k for k in tree}
        self._vals = {k.lower(): v for k, v in tree.items()}
        self.opened = []
        self.access = []

    def OpenKey(self, base, sub, reserved=0, access=None):
        root = base.path if isinstance(base, _Key) else base
        path = root + ("\\" + sub if sub else "")
        self.opened.append(path)
        self.access.append(access)
        if path.lower() not in self._vals:
            raise OSError(path)
        return _Key(path)

    def QueryValueEx(self, key, name):
        vals = self._vals.get(key.path.lower(), {})
        if name not in vals:
            raise OSError(name)
        return (vals[name], 1)

    def EnumKey(self, key, i):
        prefix = key.path.lower() + "\\"
        kids = sorted({self._orig[k][len(prefix):].split("\\")[0]
                       for k in self._vals if k.startswith(prefix)})
        if i >= len(kids):
            raise OSError("no more")
        return kids[i]


_PRN = "HKLM\\" + ac._PRINTERS_SUBKEY
_EXTS = "HKCU\\" + ac._FILE_EXTS_SUBKEY
_URLS = "HKCU\\" + ac._URL_ASSOC_SUBKEY

_REGISTRY = {
    # automation_apps -- one listed, one NOT on the published list
    "HKCR\\Excel.Application\\CLSID": {"": "{00024500-0000-0000-C000-000000000046}"},
    "HKCR\\AcmeInternal.Application\\CLSID": {"": "{deadbeef}"},
    # file_handlers -- the operator's own choice, a class default, a PATH-shaped value
    _EXTS + "\\.pdf\\UserChoice": {"ProgId": "MSEdgePDF"},
    "HKCR\\.pdf": {"": "AcroExch.Document"},
    "HKCR\\.docx": {"": "Word.Document.12"},
    "HKCR\\.txt": {"": "C:\\Users\\alice\\tools\\editor.exe"},
    "HKCR\\.unlisted": {"": "Secret.Thing"},
    _URLS + "\\mailto\\UserChoice": {"ProgId": "Outlook.URL.mailto.15"},
    # installed_printers -- a printer, a per-user SID key (witnessed on a real machine:
    # it carries a driver AND a SID-bearing Name), a non-printer, a UNC-named printer
    _PRN: {},
    _PRN + "\\Microsoft Print to PDF": {"Printer Driver": "Microsoft Print To PDF",
                                         "Name": "Microsoft Print to PDF"},
    _PRN + "\\S-1-5-21-111-222-333-1001": {
        "Printer Driver": "x", "Name": "S-1-5-21-111-222-333-1001:OneNote"},
    _PRN + "\\Some Config Key": {"Unrelated": "1"},
    _PRN + "\\Office Laser": {"Printer Driver": "PCL6",
                               "Name": "\\\\printsrv\\office-laser"},
}


@pytest.fixture()
def windows_registry(monkeypatch):
    fake = FakeWinreg(_REGISTRY)
    monkeypatch.setattr(ac, "_winreg", lambda: fake)
    monkeypatch.setattr(ac, "_platform_family", lambda: "windows")
    return fake


def _vault(tmp_path):
    return SimpleNamespace(root=tmp_path)


# ── structure ────────────────────────────────────────────────────────────────

def test_the_three_wm7_categories_are_declared_carded_and_probed():
    for cat in NEW:
        assert cat in cc.CATEGORIES and cat in ac.PROBES, cat
    assert set(ac.PROBES) == set(cc.CATEGORIES)


def test_every_category_has_an_availability_answer():
    """A category cannot ship without saying where it can run."""
    assert set(ac._CATEGORY_PLATFORMS) == set(ac.PROBES)


@pytest.mark.parametrize("family", ["windows", "macos", "freedesktop"])
def test_an_unavailable_category_always_carries_an_ascii_reason(family):
    for cat in ac.PROBES:
        ok, why = ac.category_availability(cat, family)
        if ok:
            assert why == ""
        else:
            assert why.strip() and why.isascii(), (cat, family, why)


def test_the_new_kinds_do_not_collide_with_the_inventory_kinds():
    """``purge_source_ref``'s 'detached is unreachable' argument rests on census kinds
    being disjoint from the inventory populator's."""
    census_kinds = {kind for _probe, kind in ac.PROBES.values()}
    assert not census_kinds & {"service", "capability", "data_location", "credential_ref"}
    assert len(census_kinds) == len(ac.PROBES)


def test_the_new_categories_ship_with_their_withdraw_controls():
    """P19 surface: grantable, and the card names the controls that withdraw them."""
    for cat in NEW:
        assert cat in cc.SURFACED_CATEGORIES
        card = cc.consent_card(cat)
        assert card["revocation_surface_shipped"] is True
        assert f"census revoke {cat}" in card["revocation_notice"]
        assert "table page" in card["revocation_notice"]


# ── the probes: fixed lists, KEY_READ, nothing identifying ────────────────────

def test_automation_apps_reads_only_the_published_list_with_key_read(windows_registry):
    found = ac.probe_automation_apps(50, ac._Budget(5.0))
    assert found == ["Excel.Application"]
    assert not any("AcmeInternal" in p for p in windows_registry.opened), \
        "allowlist, not an enumeration of the class registry"
    assert set(windows_registry.access) == {FakeWinreg.KEY_READ}


def test_file_handlers_prefer_the_operators_choice_and_never_record_a_path(
        windows_registry):
    found = ac.probe_file_handlers(50, ac._Budget(5.0))
    assert found == [".pdf -> MSEdgePDF", ".docx -> Word.Document.12",
                     "mailto: -> Outlook.URL.mailto.15"], found
    assert not any("alice" in v or "Secret" in v for v in found)
    assert set(windows_registry.access) == {FakeWinreg.KEY_READ}


def test_printers_skip_account_sids_non_printers_and_unc_paths(windows_registry):
    """THE REAL-MACHINE CATCH. On the build machine the print registry's per-user keys
    carried a ``Printer Driver`` value AND a ``Name`` of ``S-1-5-21-...:OneNote``, so a
    driver-only filter recorded an account SID. The SID check is what keeps it out."""
    found = ac.probe_installed_printers(50, ac._Budget(5.0))
    assert found == ["Microsoft Print to PDF"], found
    assert not any(ac._SID_RE.search(v) for v in found)
    assert set(windows_registry.access) == {FakeWinreg.KEY_READ}


def test_file_handlers_on_linux_read_mimeapps_for_the_fixed_list(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "mimeapps.list").write_text(
        "[Default Applications]\n"
        "application/pdf=org.gnome.Evince.desktop;other.desktop;\n"
        "text/x-unlisted=leak.desktop\n"
        "x-scheme-handler/mailto=/home/alice/bin/mail.desktop\n", encoding="utf-8")
    monkeypatch.setattr(ac, "_platform_family", lambda: "freedesktop")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(home)))
    assert ac.probe_file_handlers(50, ac._Budget(5.0)) == \
        [".pdf -> org.gnome.Evince.desktop"]


def test_an_oversized_mimeapps_file_is_not_read(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "mimeapps.list").write_text(
        "[Default Applications]\napplication/pdf=a.desktop\n"
        + "#" * (ac._MAX_MIMEAPPS_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr(ac, "_platform_family", lambda: "freedesktop")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(home)))
    assert ac.probe_file_handlers(50, ac._Budget(5.0)) == []


def test_a_value_is_dropped_never_cut():
    """DEC-31 does not arise: nothing is sliced. An over-long or path-shaped id is not
    recorded at all."""
    assert ac._usable_id("x" * (ac._MAX_VALUE_CHARS + 1)) == ""
    assert ac._usable_id("C:\\Users\\alice\\a.exe") == ""
    assert ac._usable_id("/home/alice/a.desktop") == ""
    assert ac._usable_id("\\\\srv\\share") == ""
    assert ac._usable_id("Word.Document.12") == "Word.Document.12"


@pytest.mark.parametrize("probe", [ac.probe_automation_apps, ac.probe_file_handlers,
                                   ac.probe_installed_printers])
def test_each_new_probe_runs_bounded_on_this_real_machine(probe):
    """Against the REAL platform source, not the fake. The bound and the identity rules
    are what must hold on any machine; an empty result is a legitimate answer."""
    values = probe(5, ac._Budget(5.0))
    assert isinstance(values, list) and len(values) <= 5
    for v in values:
        assert isinstance(v, str) and v.strip()
        assert not ac._SID_RE.search(v), v
        assert ":\\" not in v and not v.startswith(("/", "\\\\")), v


# ── reachability: the REAL probes, through run_census, behind consent ─────────

def test_an_ungranted_new_category_never_touches_its_source(tmp_path, windows_registry):
    summary = ac.run_census(_vault(tmp_path))
    for cat in NEW:
        assert summary["skipped"][cat] == "not_consented"
    assert windows_registry.opened == [], \
        "an ungranted category's registry source must never be opened"


@pytest.mark.parametrize("category,kind,value", [
    ("automation_apps", "com_automation_server", "Excel.Application"),
    ("file_handlers", "default_file_handler", ".pdf -> MSEdgePDF"),
    ("installed_printers", "printer", "Microsoft Print to PDF"),
])
def test_a_granted_new_category_reaches_its_real_probe_through_run_census(
        tmp_path, windows_registry, category, kind, value):
    """THE REACHABILITY PIN for the widened scan. ``run_census`` is the call
    ``shadow_runtime`` makes on every survey; the probe is the real one; only the registry
    is recorded. Drop the category's ``PROBES`` entry, or its registry read, and this
    goes red."""
    v = _vault(tmp_path)
    ac.grant_category(v, category)
    summary = ac.run_census(v)
    assert summary["scanned"] == [category], summary
    facts = [(f.kind, f.value) for f in FactStore(v).all_facts()]
    assert (kind, value) in facts, facts
    for f in FactStore(v).all_facts():
        assert f.origin_class == "content_derived"
        assert [(s.source_kind, s.ref) for s in f.source_chain] == [("census", category)]


def test_revoking_a_new_category_stops_it_and_purges_its_facts(tmp_path, windows_registry):
    """WM-7: revocation stops future scans AND purges the derived facts."""
    v = _vault(tmp_path)
    ac.grant_category(v, "installed_printers")
    ac.run_census(v)
    assert FactStore(v).all_facts()
    out = ac.revoke_category(v, "installed_printers")
    assert out["revoked"] is True and out["facts_removed"] >= 1
    assert FactStore(v).all_facts() == []
    windows_registry.opened.clear()
    after = ac.run_census(v, min_interval_seconds=0)
    assert after["skipped"]["installed_printers"] == "not_consented"
    assert windows_registry.opened == []


def test_an_unavailable_category_is_never_scanned_or_stamped(tmp_path, monkeypatch):
    """DEC-27. Granted (through the library -- no surface can) on a platform with no
    source: the probe is never reached, ``last_ran_at`` is never stamped, and the summary
    says ``unavailable`` rather than reporting a scan that found nothing."""
    calls = []

    def _spy(limit, budget):
        calls.append(limit)
        return ["x"]

    monkeypatch.setitem(ac.PROBES, "automation_apps", (_spy, "com_automation_server"))
    monkeypatch.setattr(ac, "_platform_family", lambda: "macos")
    v = _vault(tmp_path)
    ac.grant_category(v, "automation_apps")
    summary = ac.run_census(v)
    assert summary["skipped"]["automation_apps"] == "unavailable"
    assert calls == []
    assert cc.CensusConsentStore(tmp_path).last_ran_at("automation_apps") is None
    assert FactStore(v).all_facts() == []


def test_the_budget_still_stops_before_the_availability_check(tmp_path, monkeypatch):
    """Ordering pin: on a platform where a category is unavailable, an exhausted budget is
    still reported as the budget -- so the pre-existing budget test means the same thing
    on every platform."""
    monkeypatch.setattr(ac, "_platform_family", lambda: "macos")
    v = _vault(tmp_path)
    for cat in ac.PROBES:
        ac.grant_category(v, cat)
    summary = ac.run_census(v, budget_seconds=0.0)
    assert set(summary["skipped"].values()) == {"budget_exhausted"}


# ── DEC-9: an existing vault keeps its meaning ────────────────────────────────

def test_an_existing_consent_file_keeps_its_meaning(tmp_path, windows_registry):
    """The consent FORMAT is unchanged (version 2, signed, per generation), so a vault
    granted by the previous build reads the same: its grant stands, and none of the new
    categories is granted by its absence from the file -- nothing new starts scanning."""
    v = _vault(tmp_path)
    ac.grant_category(v, "cloud_sync_roots")          # the previous build's only grant
    raw = (tmp_path / "census_consent.json").read_text(encoding="utf-8")
    assert '"version": 2' in raw
    store = cc.CensusConsentStore(tmp_path)
    assert store.is_active("cloud_sync_roots") is True
    for cat in NEW:
        assert store.is_granted(cat) is False
    summary = ac.run_census(v)
    for cat in NEW:
        assert summary["skipped"][cat] == "not_consented"
    assert windows_registry.opened == []
