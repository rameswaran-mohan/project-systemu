"""F21 -- an optional dependency may make a capability UNAVAILABLE, never broken.

v0.10.24 moved two heavy packages out of the default install: ``playwright``
(38.2 MB, the single largest wheel in the dependency set) and ``nicegui``
(20.3 MB plus ~15 transitive packages).  ``pip install systemu`` was pulling
126 MB across 122 wheels and taking minutes; a pure-CLI user paid for a browser
engine and a web UI framework they never started.

That move is the DANGEROUS kind of fix in this codebase.  The failure mode we
have spent this whole effort deleting is the capability that LOOKS present and
silently does nothing: a tool listed as ready that dies at call time, or worse,
returns a shrug.  Making a dependency optional creates exactly that shape for
free unless the absence is carried all the way to every surface that lists the
capability.

PROPERTY
    A capability whose optional dependency is not installed is reported as
    UNAVAILABLE everywhere it is listed, and any attempt to use it names the
    exact remedy -- the literal command that fixes it, never an ImportError.

    Three obligations, all fenced below:
      1. LISTING     -- the capability index, ``find-tools`` and ``tools list``
                        say unavailable, not ready.
      2. INVOCATION  -- the attempt returns an actionable, typed refusal naming
                        ``pip install systemu[<extra>]``.
      3. DIAGNOSIS   -- ``doctor`` shows which optional groups are missing.

    Plus two structural obligations without which the above are theatre:
      4. GROUNDING   -- every group names a REAL pyproject extra containing
                        exactly those packages, so the remedy string is not a
                        lie pip would reject.
      5. COMPLETENESS-- every package any seed tool declares is either a core
                        dependency or a registered optional group.  A dep that
                        is neither has no honest-unavailability path at all,
                        which is the silent-no-op state itself.

FENCE
    This file.  Availability is probed in exactly ONE place
    (``optional_deps.is_installed``), so every test here drives the real
    production surface with that single probe patched -- there is no second
    mock of a listing surface anywhere.

WITNESS
    Delete the ``optional_deps`` call from ``capability_index.derive_index``
    and ``test_the_index_marks_a_tool_unavailable_when_its_group_is_missing``
    goes red.  Delete it from ``dependency_installer.ensure_satisfied`` and
    ``test_invocation_names_the_exact_install_command`` goes red.  Delete the
    ``dashboard`` group and ``test_daemon_dashboard_refuses_with_the_remedy``
    goes red.  Each obligation has its own named test; none of them share a
    fixture that could make one pass on another's behalf.

DEC-44
    Every test below drives a REAL production function.  Only the environment
    probe is faked -- and the fresh-venv runtime verification in the packet
    report exercises the same paths with the packages genuinely absent.
"""
from __future__ import annotations

import json
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

import pytest

from systemu.runtime import capability_index as ci
from systemu.runtime import optional_deps as od

REPO = Path(__file__).resolve().parents[1]
SEED_INDEX = REPO / "systemu" / "vault" / "tools" / "index.json"


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _core_dependency_names() -> set:
    out = set()
    for spec in _pyproject()["project"]["dependencies"]:
        out.add(od.canonical(re.split(r"[<>=!~;\[ ]", spec.strip())[0]))
    return out


def _seed_tools() -> list:
    return json.loads(SEED_INDEX.read_text(encoding="utf-8"))


@pytest.fixture()
def nothing_optional_installed(monkeypatch):
    """The ONE probe, flipped: every optional-group package is absent."""
    optional = {p for g in od.GROUPS for p in g.packages}
    real = od.is_installed
    monkeypatch.setattr(
        od, "is_installed",
        lambda pkg: False if od.canonical(pkg) in optional else real(pkg),
    )
    return optional


# --------------------------------------------------------------------------- #
# 4. GROUNDING -- the remedy string must be a command pip accepts
# --------------------------------------------------------------------------- #

def test_every_group_names_a_real_extra_that_contains_its_packages():
    """``pip install systemu[browser]`` must actually install playwright.

    An extra that does not exist is not an error pip raises -- it prints a
    warning and installs nothing.  A remedy that no-ops is worse than no
    remedy, so the group table is checked against the real pyproject.
    """
    extras = _pyproject()["project"]["optional-dependencies"]
    assert od.GROUPS, "no optional groups registered"
    for g in od.GROUPS:
        assert g.extra in extras, (
            f"group {g.extra!r} tells operators to run {od.install_command(g.packages)!r} "
            f"but pyproject declares no such extra (it has: {sorted(extras)})"
        )
        declared = {od.canonical(re.split(r"[<>=!~;\[ ]", s.strip())[0])
                    for s in extras[g.extra]}
        missing = {od.canonical(p) for p in g.packages} - declared
        assert not missing, (
            f"extra [{g.extra}] does not install {sorted(missing)} -- the remedy "
            f"would run and change nothing"
        )


def test_no_group_package_is_also_a_core_dependency():
    """If it ships by default, the unavailable path can never be reached --
    which means it is untested code claiming to protect a live state."""
    core = _core_dependency_names()
    overlap = {od.canonical(p) for g in od.GROUPS for p in g.packages} & core
    assert not overlap, (
        f"{sorted(overlap)} is registered as OPTIONAL but is still a core "
        f"dependency -- either the move did not happen or the group is dead code"
    )


def test_the_all_extra_installs_every_group():
    """One command for operators who want the whole product."""
    extras = _pyproject()["project"]["optional-dependencies"]
    assert "all" in extras, "no [all] extra -- operators need one line for everything"
    declared = {od.canonical(re.split(r"[<>=!~;\[ ]", s.strip())[0])
                for s in extras["all"]}
    for g in od.GROUPS:
        for p in g.packages:
            assert od.canonical(p) in declared, (
                f"[all] omits {p} (from [{g.extra}])"
            )


# --------------------------------------------------------------------------- #
# 5. COMPLETENESS -- no seed-tool dependency may be uncovered
# --------------------------------------------------------------------------- #

def test_every_seed_tool_dep_is_core_or_a_registered_group():
    """The successor to starter-pack conformance's "every dep is core".

    That rule was right when nothing was optional.  The honest generalisation
    is: a declared dep is either shipped by default, or it belongs to a group
    with a remedy -- never neither.  Neither is the silent-no-op state.
    """
    core = _core_dependency_names()
    grouped = {od.canonical(p) for g in od.GROUPS for p in g.packages}
    uncovered = sorted({od.canonical(d) for t in _seed_tools()
                        for d in (t.get("dependencies") or [])} - core - grouped)
    assert not uncovered, (
        f"seed tools declare {uncovered}, which is neither a core dependency nor "
        f"a registered optional group -- a tool needing it has no honest "
        f"unavailability path"
    )


def test_only_the_tools_that_truly_need_a_browser_declare_playwright():
    """Grounds the whole packet in the REAL packaged catalog, not a fixture.

    ``web_read`` is DELIBERATELY absent. Its primary paths are the Jina Reader
    and a raw GET; Chromium is only the last-resort render escalation. It used
    to declare playwright, and once playwright became optional that declaration
    would have made ``ensure_satisfied`` refuse to run a tool that works
    perfectly over HTTP. A false UNAVAILABLE is the same defect as a false
    READY, pointed the other way.
    """
    declaring = sorted(t["name"] for t in _seed_tools()
                       if "playwright" in {od.canonical(d)
                                           for d in (t.get("dependencies") or [])})
    assert declaring == ["web_act", "web_screenshot"], declaring


def test_web_read_still_runs_with_no_browser_extra(nothing_optional_installed):
    """The regression the correction above prevents, driven through the real
    gate: the tool the runtime consults BEFORE invoking must let web_read pass."""
    from systemu.runtime.dependency_installer import (
        InstallMode, InstallStatus, ensure_satisfied,
    )
    manifest = next(t for t in _seed_tools() if t["name"] == "web_read")
    r = ensure_satisfied(manifest.get("dependencies") or [],
                         mode=InstallMode.PROMPT, approvals=None,
                         tool_name="web_read")
    assert r.ok is True and r.status is InstallStatus.SATISFIED, (r.status, r.error)


def test_web_reads_body_declares_the_same_manifest_as_the_index():
    """The index header and the tool body must not drift: the runtime reads the
    header for admission and the body for execution."""
    import ast
    src = (REPO / "systemu" / "vault" / "tools" / "implementations"
           / "web_read.py").read_text(encoding="utf-8")
    meta = next(
        ast.literal_eval(n.value)
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "TOOL_META" for t in n.targets)
    )
    header = next(t for t in _seed_tools() if t["name"] == "web_read")
    assert meta.get("dependencies", []) == (header.get("dependencies") or [])


# --------------------------------------------------------------------------- #
# 1. LISTING -- the capability surfaces
# --------------------------------------------------------------------------- #

class _Vault:
    def __init__(self, root: Path, tools):
        self.root = str(root)
        self._tools = tools

    def list_tools(self, status=None):
        return [dict(t) for t in self._tools]


def _browser_vault(tmp_path: Path) -> _Vault:
    return _Vault(tmp_path, [
        {"id": "tool_web_read", "name": "web_read", "description": "read a page",
         "enabled": True, "status": "deployed", "dependencies": ["playwright"],
         "implementation_path": "vault/tools/implementations/web_read.py"},
        {"id": "tool_file_read", "name": "file_read", "description": "read a file",
         "enabled": True, "status": "deployed", "dependencies": [],
         "implementation_path": "vault/tools/implementations/file_read.py"},
    ])


def test_the_index_marks_a_tool_unavailable_when_its_group_is_missing(
        tmp_path, nothing_optional_installed):
    rows = {r.name: r for r in ci.derive_index(_browser_vault(tmp_path))}
    assert set(rows) == {"web_read", "file_read"}, (
        "NEVER-SUBTRACT: an unavailable tool must still be LISTED, not hidden"
    )
    assert rows["web_read"].available is False
    assert 'pip install "systemu[browser]"' in rows["web_read"].unavailable_reason
    assert rows["file_read"].available is True
    assert rows["file_read"].unavailable_reason == ""


def test_the_index_marks_it_available_when_the_group_is_present(tmp_path, monkeypatch):
    monkeypatch.setattr(od, "is_installed", lambda pkg: True)
    rows = {r.name: r for r in ci.derive_index(_browser_vault(tmp_path))}
    assert rows["web_read"].available is True
    assert rows["web_read"].unavailable_reason == ""


def test_availability_is_recomputed_live_and_never_read_from_the_persisted_index(
        tmp_path, monkeypatch):
    """DEC-32: the index is a DERIVED cache. A stale ``available: true`` on disk
    must not be able to speak for the current machine -- uninstalling the extra
    after the daemon last reconciled is the exact real-world sequence."""
    vault = _browser_vault(tmp_path)
    monkeypatch.setattr(od, "is_installed", lambda pkg: True)
    assert ci.reconcile_index(vault) == 2
    on_disk = json.loads(
        (tmp_path / "capabilities" / "capability_index.json").read_text(encoding="utf-8"))
    assert any(r["name"] == "web_read" for r in on_disk)

    monkeypatch.setattr(od, "is_installed", lambda pkg: False)
    row = {r.name: r for r in ci.load_index(vault)}["web_read"]
    assert row.available is False, (
        "load_index trusted the persisted availability verdict -- a value written "
        "when playwright WAS installed"
    )
    assert 'pip install "systemu[browser]"' in row.unavailable_reason


def test_find_tools_carries_the_unavailability_to_its_caller(
        tmp_path, nothing_optional_installed):
    rows = {r["name"]: r for r in ci.find_tools(_browser_vault(tmp_path), "read", live=True)}
    assert rows["web_read"]["available"] is False
    assert 'pip install "systemu[browser]"' in rows["web_read"]["unavailable_reason"]
    assert rows["file_read"]["available"] is True


def test_the_find_tools_cli_prints_unavailable_and_the_remedy(
        tmp_path, capsys, nothing_optional_installed):
    from systemu.interface.cli_commands import run_find_tools
    assert run_find_tools(_browser_vault(tmp_path), "read") == 0
    out = capsys.readouterr().out
    assert "web_read" in out
    assert "UNAVAILABLE" in out, out
    assert 'pip install "systemu[browser]"' in out, out


def test_the_tools_list_cli_prints_unavailable_and_the_remedy(
        tmp_path, nothing_optional_installed):
    """The real click command, through CliRunner, against a real vault object."""
    from click.testing import CliRunner
    from systemu.interface.cli_commands import tools_list

    vault = _browser_vault(tmp_path)
    runner = CliRunner()
    result = runner.invoke(tools_list, obj={"vault": vault, "config": None},
                           catch_exceptions=False)
    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "web_read" in plain
    assert "UNAVAILABLE" in plain, plain
    assert "systemu[browser]" in plain, plain


# --------------------------------------------------------------------------- #
# 1b. LISTING -- the DASHBOARD listers (F24)
#
# The original packet audited the capability index, find-tools, tools list,
# doctor and /health, and declared the property met.  It was not: the Build page
# (/tools) renders every row through ``entity_rows.render_tool_row``, which did
# not consult ``optional_deps`` at all.  On a cold install with only the
# [dashboard] extra, the CLI said `web_act UNAVAILABLE` and the PRIMARY UI said
# `DEPLOYED`, at the same moment, against the same vault.  The dashboard is the
# surface an operator actually reads, so it was the one that was wrong.
#
# The fix is in the RENDERER, not the page: ``entity_rows`` is documented as
# "ONE canonical renderer per tool ... so the Tools page and any other lister
# share ONE definition", so fixing it there fixes every lister that shares it.
#
# These tests drive the REAL renderer against a recording ``ui`` -- the same
# pattern tests/test_rux2_health_load_chip.py and tests/test_f8_provider_
# satisfaction.py use -- because a test that only asserts "the module imports
# optional_deps" would still pass with the cell painting DEPLOYED.
# --------------------------------------------------------------------------- #

class _Node:
    """Chainable, context-manager-able stand-in for a NiceGUI element."""

    def __init__(self, rec, kind):
        self._rec, self._kind = rec, kind

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def classes(self, *a, **k):
        if a:
            self._rec.classes.append(str(a[0]))
        return self

    def style(self, *a, **k):
        return self

    def props(self, *a, **k):
        if a:
            self._rec.props.append(str(a[0]))
        return self

    def tooltip(self, *a, **k):
        if a:
            self._rec.tooltips.append(str(a[0]))
        return self

    def on(self, *a, **k):
        return self


class _RecordingUI:
    """Records every ``ui.<fn>(...)`` the renderer makes."""

    def __init__(self):
        self.calls, self.classes, self.props, self.tooltips = [], [], [], []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            return _Node(self, name)
        return _call

    def texts(self, fn=None):
        return [str(a[0]) for n, a, _k in self.calls
                if a and (fn is None or n == fn)]

    def markup(self):
        # NOT named ``html`` -- that name is dispatched to ``ui.html(...)``.
        return " ".join(self.texts("html"))

    def rendered(self):
        """Everything an operator can read in the row, joined."""
        return " | ".join(self.texts("html") + self.texts("label")
                          + self.tooltips)


def _render_tool_row(tool: dict, vault=None, *, editable: bool = True) -> _RecordingUI:
    """Execute the REAL ``entity_rows.render_tool_row`` against a recording ui.

    ``render_tool_row`` does ``from nicegui import ui`` at CALL time, so swapping
    the module in ``sys.modules`` is enough -- no NiceGUI runtime, no client
    context, and the production function is the one under test.
    """
    import sys
    import types
    from unittest import mock

    # Import before the patch: dashboard_state must resolve against the real
    # package, and only the renderer's own lazy `from nicegui import ui` should
    # see the stub.
    import systemu.interface.dashboard_state  # noqa: F401
    from systemu.interface.components import entity_rows

    rec = _RecordingUI()
    fake = types.ModuleType("nicegui")
    fake.ui = rec
    with mock.patch.dict(sys.modules, {"nicegui": fake}):
        entity_rows.render_tool_row(dict(tool), vault, editable=editable)
    return rec


_WEB_ACT_ROW = {
    "id": "tool_web_act", "name": "web_act", "tool_type": "python_function",
    "status": "deployed", "enabled": True, "dry_run_status": "passed",
    "description": "drive a browser", "dependencies": ["playwright"],
    "implementation_path": "vault/tools/implementations/web_act.py",
}
_FILE_READ_ROW = {
    "id": "tool_file_read", "name": "file_read", "tool_type": "python_function",
    "status": "deployed", "enabled": True, "dry_run_status": "passed",
    "description": "read a file", "dependencies": [],
    "implementation_path": "vault/tools/implementations/file_read.py",
}


def test_the_build_page_row_says_unavailable_not_deployed(nothing_optional_installed):
    """THE F24 DEFECT, pinned at the renderer every lister shares.

    A green DEPLOYED pill on a tool that refuses at call time is the exact
    silent-no-op shape this whole packet exists to delete.
    """
    rec = _render_tool_row(_WEB_ACT_ROW)
    shown = rec.rendered()
    assert "UNAVAILABLE" in shown, shown
    # ...and the remedy, in the row, not somewhere the operator has to go find.
    assert 'pip install "systemu[browser]"' in shown, shown
    # the record status must NOT be painted as the green ready pill
    assert 'class="s-pill s-pill--success">deployed<' not in rec.markup(), rec.markup()


def test_the_build_page_row_keeps_the_ready_pill_when_the_group_is_present(
        monkeypatch):
    """The mirror image: a false UNAVAILABLE is the same defect pointed the
    other way (see ``test_only_the_tools_that_truly_need_a_browser_...``)."""
    monkeypatch.setattr(od, "is_installed", lambda pkg: True)
    rec = _render_tool_row(_WEB_ACT_ROW)
    assert "UNAVAILABLE" not in rec.rendered(), rec.rendered()
    assert "deployed" in rec.markup(), rec.markup()


def test_the_build_page_leaves_a_tool_with_no_optional_dep_alone(
        nothing_optional_installed):
    """NEVER-SUBTRACT and never over-flag: file_read declares nothing optional,
    so nothing about its row changes when the browser extra is absent."""
    rec = _render_tool_row(_FILE_READ_ROW)
    assert "UNAVAILABLE" not in rec.rendered(), rec.rendered()
    assert "deployed" in rec.markup(), rec.markup()


def test_the_build_page_row_consumes_the_mint_and_does_not_re_derive_it(
        monkeypatch):
    """DEC-43: ONE mint, every surface consumes the VALUE.

    Patching the mint must change what the row paints. If the renderer computed
    its own verdict (importing playwright, reading pyproject, keeping a second
    table) this row would keep its own answer and the two surfaces would drift
    apart again -- which is precisely how F24 happened.
    """
    sentinel = "UNAVAILABLE - minted-by-optional-deps-not-by-the-renderer"
    monkeypatch.setattr(od, "unavailable_reason",
                        lambda pkgs: sentinel if list(pkgs or []) else "")
    rec = _render_tool_row(_WEB_ACT_ROW)
    assert sentinel in rec.rendered(), rec.rendered()


def test_the_build_page_row_never_injects_the_remedy_into_markup(
        nothing_optional_installed):
    """The mirror of the Rich-markup hazard optional_deps already documents.

    The remedy contains ``"`` and ``[...]``. Interpolated into an HTML attribute
    it breaks out of the attribute; interpolated into element text it is at best
    a rendering accident. It must reach the DOM as TEXT (a ``ui.label`` /
    escaped prop), never as a substring of a ``ui.html`` blob.
    """
    import html as _html

    rec = _render_tool_row(_WEB_ACT_ROW)
    reason = od.unavailable_reason(["playwright"])
    assert reason and '"' in reason, reason
    for blob in rec.texts("html"):
        assert reason not in blob, (
            f"the remedy was interpolated raw into markup: {blob!r}")
        # ...and not a partially-escaped smuggle either
        assert _html.escape(reason, quote=False) not in blob, blob
    # it DID reach the operator, as text
    assert reason in (rec.texts("label") + rec.tooltips), rec.rendered()


def test_the_build_page_and_the_cli_agree_at_the_same_moment(
        tmp_path, nothing_optional_installed):
    """THE F24 REPORT, as a test: same vault, same probe state, both surfaces.

    Reported from a cold install: `systemu tools list` said UNAVAILABLE while
    the Build page said DEPLOYED. Whatever else changes, these two may never
    disagree about whether a tool can run.
    """
    from click.testing import CliRunner
    from systemu.interface.cli_commands import tools_list

    vault = _Vault(tmp_path, [_WEB_ACT_ROW, _FILE_READ_ROW])
    cli = " ".join(CliRunner().invoke(
        tools_list, obj={"vault": vault, "config": None},
        catch_exceptions=False).output.split())
    page = _render_tool_row(_WEB_ACT_ROW, vault).rendered()

    assert ("UNAVAILABLE" in cli) is ("UNAVAILABLE" in page), (cli, page)
    assert "UNAVAILABLE" in cli and "UNAVAILABLE" in page
    assert "systemu[browser]" in cli and "systemu[browser]" in page
    # ...and neither surface flags the tool that needs nothing optional
    page_ok = _render_tool_row(_FILE_READ_ROW, vault).rendered()
    assert "UNAVAILABLE" not in page_ok, page_ok
    assert cli.count("UNAVAILABLE") >= 1 and "1 tool(s) are UNAVAILABLE" in cli, cli


def test_the_tools_list_remedy_survives_a_narrow_terminal(
        tmp_path, monkeypatch, nothing_optional_installed):
    """Found while auditing the CLI half of F24.

    The remedy lived only inside a Rich table cell, and Rich sizes columns to
    the terminal and ellipsises the overflow. At the default 80 columns the
    operator got ``pip install "systemu[brow…`` — while the footer directly
    below it asserted they had been "listed above with the exact install
    command". A claim the output does not honour is the DEC-34 defect, and an
    uncopyable install line is exactly the "remedy that no-ops" this module
    exists to prevent.
    """
    from click.testing import CliRunner
    from systemu.interface.cli_commands import tools_list

    monkeypatch.setenv("COLUMNS", "80")
    out = CliRunner().invoke(
        tools_list, obj={"vault": _Vault(tmp_path, [_WEB_ACT_ROW]),
                         "config": None},
        catch_exceptions=False).output
    # on ONE line, unbroken and unellipsised — copy-paste is the whole point
    assert any('pip install "systemu[browser]"' in ln for ln in out.splitlines()), out
    assert "…" not in " ".join(
        ln for ln in out.splitlines() if "systemu[browser" in ln), out


def test_the_build_page_row_reads_the_deps_the_vault_carries_not_only_the_header(
        nothing_optional_installed):
    """``tool_row_deps`` already falls back to ``vault.get_tool(id)`` when the
    index header omits ``dependencies`` (the settings.py header shape). The
    availability verdict must ride the SAME resolved list, or a header-shaped
    row would silently render ready."""
    from types import SimpleNamespace
    header = dict(_WEB_ACT_ROW)
    header.pop("dependencies")
    vault = SimpleNamespace(
        get_tool=lambda tid: SimpleNamespace(dependencies=["playwright"]))
    assert "UNAVAILABLE" in _render_tool_row(header, vault).rendered()


def test_the_row_view_model_carries_the_verdict_for_every_other_lister():
    """``tool_row_model`` is the pure half every non-table lister can consume,
    so a future surface has a value to read instead of a probe to re-invent."""
    from systemu.interface.components.entity_rows import tool_row_model
    m = tool_row_model(dict(_WEB_ACT_ROW))
    assert set(("available", "unavailable_reason")) <= set(m)


def test_the_table_page_does_not_call_an_uninstallable_tool_ready(
        tmp_path, nothing_optional_installed):
    """The SECOND dashboard lister found by the F24 audit.

    ``/table`` (OnTheTable) projects every enabled tool with the literal status
    ``ready`` and paints it with the positive colour token. Same lie, different
    page -- and this one is the page whose whole job is telling the operator
    what systemu can actually use.
    """
    from systemu.runtime import table_reconciler as tr
    from systemu.interface.pages import table as table_page

    items = {it.name: it for it in tr.project(_Vault(tmp_path, [
        _WEB_ACT_ROW, _FILE_READ_ROW]))}
    assert items["web_act"].status != "ready", (
        "the table calls a tool READY whose optional dependency group is absent")
    assert items["file_read"].status == "ready"
    # whatever word it uses, the page must colour it as a problem and offer the
    # repair deep-link -- an unrecognised status renders grey and inert.
    assert table_page._STATUS_COLOR.get(items["web_act"].status) == "negative"
    assert table_page.repair_route("tool", items["web_act"].status) != ("", "")
    # ...and the operator is told WHY, on the card, not only on another page.
    assert "systemu[browser]" in items["web_act"].detail, items["web_act"].detail


def test_the_command_palette_flags_a_tool_it_cannot_run(
        tmp_path, nothing_optional_installed):
    """The THIRD lister. The palette prefills ``run: web_act`` into chat; it must
    not offer that as an unqualified capability when the run cannot succeed.

    It already reads capability-index rows, which already carry the verdict --
    this is purely a matter of not throwing the value away.
    """
    from systemu.interface.components import command_palette as cp
    entries = {e.label: e for e in cp.build_index(_Vault(tmp_path, [
        _WEB_ACT_ROW, _FILE_READ_ROW])) if e.group == "Tools"}
    assert set(entries) == {"web_act", "file_read"}, sorted(entries)
    assert "UNAVAILABLE" in entries["web_act"].detail, entries["web_act"].detail
    assert "UNAVAILABLE" not in entries["file_read"].detail


def test_the_palette_fallback_path_is_honest_too(
        tmp_path, monkeypatch, nothing_optional_installed):
    """The palette has TWO sources and the second one is the dangerous one.

    ``_tool_entries`` prefers capability-index rows (which already carry the
    verdict) and falls back to the raw tool index when the index is empty. That
    fallback fires on a vault whose tools cannot be indexed -- and it once
    carried the whole Tools group in production. Fixing only the primary source
    would have left the honest listing conditional on the index working, which
    is the same shape as not fixing it.
    """
    from systemu.interface.components import command_palette as cp
    monkeypatch.setattr(ci, "derive_index", lambda _v: [])
    entries = {e.label: e for e in cp.build_index(_Vault(tmp_path, [
        _WEB_ACT_ROW, _FILE_READ_ROW])) if e.group == "Tools"}
    assert set(entries) == {"web_act", "file_read"}, sorted(entries)
    assert "UNAVAILABLE" in entries["web_act"].detail, entries["web_act"].detail
    assert 'pip install "systemu[browser]"' in entries["web_act"].detail
    assert "UNAVAILABLE" not in entries["file_read"].detail


# --------------------------------------------------------------------------- #
# 2. INVOCATION -- the attempt must name the remedy, never traceback
# --------------------------------------------------------------------------- #

def test_invocation_names_the_exact_install_command(nothing_optional_installed):
    """``ensure_satisfied`` is the single choke point every executor funnels
    through (ToolRegistry self-heal, LocalBackend, the daemon, the deps CLI)."""
    from systemu.runtime.dependency_installer import (
        InstallMode, InstallStatus, ensure_satisfied,
    )
    r = ensure_satisfied(["playwright"], mode=InstallMode.ALWAYS,
                         tool_name="web_read", tool_id="tool_web_read")
    assert r.ok is False
    assert r.status is InstallStatus.BLOCKED_MISSING_EXTRA
    assert 'pip install "systemu[browser]"' in (r.error or ""), r.error


def test_invocation_refusal_survives_every_install_mode(nothing_optional_installed):
    """A first-party optional group is never silently pip-installed mid-task,
    and never routed into the operator dep-approval queue as if it were a
    third-party package a forged tool asked for."""
    from systemu.runtime.dependency_installer import (
        InstallMode, InstallStatus, ensure_satisfied,
    )
    for mode in (InstallMode.OFF, InstallMode.PROMPT,
                 InstallMode.ALWAYS, InstallMode.ALLOWLIST):
        r = ensure_satisfied(["playwright"], mode=mode, tool_name="web_read")
        assert r.status is InstallStatus.BLOCKED_MISSING_EXTRA, (mode, r.status)
        assert 'pip install "systemu[browser]"' in (r.error or ""), (mode, r.error)


def test_a_third_party_dep_is_still_routed_to_the_approval_flow(monkeypatch):
    """The new branch must not swallow the EXISTING behaviour for packages that
    are genuinely not ours -- a forged tool asking for `geopy2` still goes to
    the operator approval queue, not to a bogus `systemu[...]` remedy."""
    from systemu.runtime.dependency_installer import (
        InstallMode, InstallStatus, ensure_satisfied,
    )
    r = ensure_satisfied(["definitely-not-a-real-package-xyz"],
                         mode=InstallMode.PROMPT, approvals=None,
                         tool_name="forged_thing")
    assert r.status is InstallStatus.BLOCKED_PENDING_APPROVAL
    assert "systemu[" not in (r.error or ""), r.error


def test_browser_pool_refuses_with_the_remedy_not_a_module_not_found(
        nothing_optional_installed):
    """The direct-import path (a tool body reaching BrowserPool) must not leak
    ``ModuleNotFoundError: No module named 'playwright'`` to the operator."""
    from systemu.runtime.web.browser_pool import BrowserPool, OptionalDependencyMissing
    with pytest.raises(OptionalDependencyMissing) as exc:
        BrowserPool()._ensure_browser()
    assert 'pip install "systemu[browser]"' in str(exc.value)


# --------------------------------------------------------------------------- #
# 3. DIAGNOSIS -- doctor
# --------------------------------------------------------------------------- #

def test_doctor_reports_every_missing_optional_group(nothing_optional_installed):
    from systemu.runtime import platform_profile as pp
    report = pp.build_doctor_report(
        provider_configured=True, provider_reachable=True,
        keyring_locked=False, daemon_running=True,
    )
    groups = {g["extra"]: g for g in report["optional_groups"]}
    assert set(groups) == {g.extra for g in od.GROUPS}
    for extra, row in groups.items():
        assert row["installed"] is False
        assert f'pip install "systemu[{extra}]"' in row["remedy"]
    # A missing optional group is a WARNING, never blocking: a pure-CLI user
    # who never wanted the dashboard must not see `doctor` exit nonzero.
    assert pp.report_exit_code(report) == 0
    ids = {p["id"] for p in report["problems"]}
    assert "optional_group_missing:dashboard" in ids
    assert all(not p["blocking"] for p in report["problems"]
               if p["id"].startswith("optional_group_missing"))


def test_the_dashboard_health_view_reports_the_same_groups(nothing_optional_installed):
    """F19's lesson applied: `doctor` and /health must not derive this twice.

    A dashboard operator can have [dashboard] and not [browser], so "which
    capabilities are missing" is a live question on this page too.
    """
    from systemu.interface.pages.health import health_view
    view = health_view(provider_configured=True, provider_reachable=True,
                       keyring_locked=False, daemon_running=True, load={})
    groups = {g["extra"]: g for g in view["optional_groups"]}
    assert set(groups) == {g.extra for g in od.GROUPS}
    assert all(g["installed"] is False for g in groups.values())
    assert 'pip install "systemu[browser]"' in groups["browser"]["remedy"]


def test_doctor_is_silent_about_groups_that_are_installed(monkeypatch):
    from systemu.runtime import platform_profile as pp
    monkeypatch.setattr(od, "is_installed", lambda pkg: True)
    report = pp.build_doctor_report(
        provider_configured=True, provider_reachable=True,
        keyring_locked=False, daemon_running=True,
    )
    assert all(g["installed"] for g in report["optional_groups"])
    assert not [p for p in report["problems"]
                if p["id"].startswith("optional_group_missing")]


def test_dashboard_entry_point_refuses_with_the_remedy(nothing_optional_installed):
    """``run_dashboard`` used to log "Run: pip install nicegui" and RETURN --
    a bare package name, and a return that reads to every caller as a clean
    start.  It must raise, and it must raise BEFORE any side effect."""
    import os
    from systemu.interface import dashboard as dash

    class _Cfg:
        vault_dir = "unused - the gate must fire before this is read"

    os.environ.pop("SYSTEMU_DASHBOARD_PORT", None)
    with pytest.raises(od.OptionalDependencyMissing) as exc:
        dash.run_dashboard(_Cfg(), port=8765)
    assert 'pip install "systemu[dashboard]"' in str(exc.value)
    assert "SYSTEMU_DASHBOARD_PORT" not in os.environ, (
        "the refused start still stamped dashboard environment state"
    )


def test_daemon_start_refuses_fast_with_the_remedy(nothing_optional_installed,
                                                   monkeypatch):
    """`daemon start` must not spawn and then poll for 60s for a port nothing
    will ever bind. It refuses in under a second, naming the command."""
    from click.testing import CliRunner
    from systemu.interface import cli_commands as cc

    spawned = []
    monkeypatch.setattr("systemu.scheduler.daemon.start_daemon",
                        lambda **k: spawned.append(k))
    monkeypatch.setattr(cc, "_get_vault_and_config",
                        lambda ctx: (object(), object()))
    res = CliRunner().invoke(cc.daemon_start, [], obj={}, catch_exceptions=False)
    assert res.exit_code == 1, res.output
    assert 'pip install "systemu[dashboard]"' in " ".join(res.output.split()), res.output
    assert spawned == [], "a daemon was spawned for a dashboard that cannot start"


def test_the_daemon_does_not_announce_a_dashboard_it_did_not_start(
        nothing_optional_installed, monkeypatch, caplog):
    """The daemon logged "Dashboard thread launched on http://127.0.0.1:8765"
    for a thread whose body returned immediately.  The URL was a lie."""
    import ast
    import inspect
    import textwrap
    from systemu.scheduler import daemon as dmn

    assert od.missing_groups(("nicegui",)), "fixture precondition"
    # A REACHABILITY pin, per the standing rule: assert the guard is at the
    # production call site and ORDERED before the spawn. Running the real
    # `_run_daemon_loop` here is not an option (it blocks forever on a
    # scheduler), and a copy of the branch in the test would prove nothing
    # about the daemon -- so the pin walks the AST of the one function that
    # decides. AST, not substring search: a COMMENT naming the call was enough
    # to make a substring ordering check pass on the wrong occurrence.
    tree = ast.parse(textwrap.dedent(inspect.getsource(dmn._run_daemon_loop)))

    def _lines_calling(name):
        return [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and (getattr(n.func, "attr", None) == name
                     or getattr(n.func, "id", None) == name)]

    guard = _lines_calling("missing_groups")
    spawn = _lines_calling("run_dashboard_thread")
    assert guard, (
        "_run_daemon_loop no longer consults optional_deps before launching "
        "the dashboard thread -- it can announce a URL nothing serves again"
    )
    assert spawn, "the dashboard is no longer launched here -- revisit this pin"
    assert min(guard) < min(spawn), (
        "the guard must run BEFORE the thread is spawned: run_dashboard_thread "
        "returns as soon as the thread starts, so a raise inside it is invisible"
    )


# --------------------------------------------------------------------------- #
# the probe itself -- ground truth, not a guess
# --------------------------------------------------------------------------- #

def test_is_installed_agrees_with_the_real_environment():
    """The probe is the ONE place truth enters; if it lies, everything above
    lies in unison. Checked against packages whose state is not in doubt."""
    assert od.is_installed("click") is True          # a core dependency, always here
    assert od.is_installed("no-such-distribution-at-all") is False


def test_canonical_normalises_pep503_spellings():
    assert od.canonical("Python-DOCX") == od.canonical("python_docx") == "python-docx"


def test_install_command_is_a_single_copyable_line():
    cmd = od.install_command(["playwright"])
    assert cmd == 'pip install "systemu[browser]"'
    assert "\n" not in cmd
