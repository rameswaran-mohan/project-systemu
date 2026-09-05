"""P2d - local first-run funnel counters (`systemu/runtime/funnel.py`).

The store is a single JSON sidecar in the vault root, first-write-wins, atomic,
defensive on read, and it NEVER raises into the host path it rides on. The whole
point of the surface is a privacy claim -- "counted on this machine only, nothing
is sent anywhere" -- so the module's source purity (no network import, anywhere)
is pinned as hard as its behaviour.

Reachability: every production call site gets an AST pin. Deleting the call from
the shipped module must turn a NAMED test red -- a wired-looking milestone that
nothing stamps would make the Insights table lie by omission forever.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SYSTEMU = _ROOT / "systemu"


class _FakeVault:
    """The only thing funnel.py may consume off a vault is `.root`."""

    def __init__(self, root):
        self.root = root


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return _FakeVault(root)


def _funnel_file(vault) -> Path:
    return Path(vault.root) / "funnel.json"


# --- the store: first-write-wins -------------------------------------------
def test_a_stamped_milestone_is_never_overwritten(vault):
    from systemu.runtime import funnel

    assert funnel.mark_milestone(vault, "welcome_rendered") == funnel.STAMPED
    first = funnel.read_funnel(vault)["welcome_rendered"]

    # A second (and third) visit must not move the clock.
    assert funnel.mark_milestone(vault, "welcome_rendered") == funnel.ALREADY_STAMPED
    assert funnel.mark_milestone(vault, "welcome_rendered") == funnel.ALREADY_STAMPED
    assert funnel.read_funnel(vault)["welcome_rendered"] == first


def test_a_second_milestone_joins_the_same_file(vault):
    from systemu.runtime import funnel

    funnel.mark_milestone(vault, "welcome_rendered")
    funnel.mark_milestone(vault, "first_wish")
    on_disk = json.loads(_funnel_file(vault).read_text(encoding="utf-8"))
    assert set(on_disk) == {"welcome_rendered", "first_wish"}
    # ONE sidecar, not one file per milestone.
    assert sorted(p.name for p in Path(vault.root).iterdir()) == ["funnel.json"]


def test_every_declared_milestone_is_accepted(vault):
    from systemu.runtime import funnel

    for name in funnel.MILESTONES:
        assert funnel.mark_milestone(vault, name) == funnel.STAMPED
    assert set(funnel.read_funnel(vault)) == set(funnel.MILESTONES)


# --- the closed vocabulary: a VERDICT, never a raise ------------------------
def test_unknown_milestone_returns_a_verdict_and_writes_nothing(vault):
    from systemu.runtime import funnel

    assert funnel.mark_milestone(vault, "first_coffee") == funnel.UNKNOWN_MILESTONE
    assert not _funnel_file(vault).exists()


@pytest.mark.parametrize("bogus", [None, 5, b"welcome_rendered", ["welcome_rendered"]])
def test_a_non_string_name_is_refused_by_type_not_by_duck_typing(vault, bogus):
    from systemu.runtime import funnel

    assert funnel.mark_milestone(vault, bogus) == funnel.UNKNOWN_MILESTONE
    assert not _funnel_file(vault).exists()


def test_the_vocabulary_is_exactly_the_six_funnel_steps():
    from systemu.runtime import funnel

    assert funnel.MILESTONES == (
        "welcome_rendered",
        "setup_finished",
        "first_task_submitted",
        "first_task_succeeded",
        "first_recording",
        "first_wish",
    )
    assert type(funnel.MILESTONES) is tuple


# --- defensive read ---------------------------------------------------------
def test_a_corrupt_sidecar_reads_as_empty_and_does_not_raise(vault):
    from systemu.runtime import funnel

    _funnel_file(vault).write_text("{not json at all", encoding="utf-8")
    assert funnel.read_funnel(vault) == {}
    # ... and the next milestone still lands (the surface self-heals).
    assert funnel.mark_milestone(vault, "setup_finished") == funnel.STAMPED
    assert "setup_finished" in funnel.read_funnel(vault)


@pytest.mark.parametrize("payload", ['["a", "b"]', '"hello"', "42", "null"])
def test_a_foreign_shaped_sidecar_reads_as_empty(vault, payload):
    from systemu.runtime import funnel

    _funnel_file(vault).write_text(payload, encoding="utf-8")
    assert funnel.read_funnel(vault) == {}


def test_unknown_and_non_string_rows_are_filtered_out_of_a_read(vault):
    from systemu.runtime import funnel

    _funnel_file(vault).write_text(json.dumps({
        "welcome_rendered": "2026-01-02T03:04:05+00:00",
        "first_coffee": "2026-01-02T03:04:05+00:00",
        "setup_finished": 12345,
    }), encoding="utf-8")
    assert funnel.read_funnel(vault) == {
        "welcome_rendered": "2026-01-02T03:04:05+00:00"}


def test_a_missing_sidecar_reads_as_empty(vault):
    from systemu.runtime import funnel

    assert funnel.read_funnel(vault) == {}


def test_a_vaultless_caller_gets_a_verdict_not_an_exception():
    from systemu.runtime import funnel

    assert funnel.mark_milestone(None, "welcome_rendered") == funnel.NO_VAULT
    assert funnel.mark_milestone(_FakeVault(""), "welcome_rendered") == funnel.NO_VAULT
    assert funnel.read_funnel(None) == {}


# --- atomicity --------------------------------------------------------------
def test_the_write_is_tmp_plus_os_replace(vault):
    """Source pin AND behaviour pin: the CONC-MAP atomic-write invariant."""
    from systemu.runtime import funnel

    src = (_SYSTEMU / "runtime" / "funnel.py").read_text(encoding="utf-8")
    assert "os.replace(" in src, (
        "funnel.json is a durable side-store; a mid-write crash must never be "
        "able to leave a torn file. Keep the tmp + os.replace pattern.")

    funnel.mark_milestone(vault, "welcome_rendered")
    leftovers = [p.name for p in Path(vault.root).iterdir()
                 if p.name != "funnel.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_a_failed_replace_returns_a_verdict_and_leaves_no_temp_file(
        vault, monkeypatch):
    from systemu.runtime import funnel

    def _boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", _boom)
    assert funnel.mark_milestone(vault, "welcome_rendered") == funnel.WRITE_FAILED
    assert [p.name for p in Path(vault.root).iterdir()] == []


def test_marking_never_raises_even_when_the_vault_root_is_a_file(tmp_path):
    from systemu.runtime import funnel

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    assert funnel.mark_milestone(_FakeVault(blocker), "first_wish") in {
        funnel.WRITE_FAILED, funnel.NO_VAULT}


# --- the operator-visible view ---------------------------------------------
def test_journey_rows_cover_every_milestone_in_order(vault):
    from systemu.runtime import funnel

    rows = funnel.journey_rows(vault)
    assert [r[0] for r in rows] == [funnel.MILESTONE_LABELS[m]
                                    for m in funnel.MILESTONES]
    assert all(r[1] == funnel.NOT_YET for r in rows)


def test_a_stamped_milestone_renders_its_date(vault):
    from systemu.runtime import funnel

    _funnel_file(vault).write_text(json.dumps({
        "welcome_rendered": "2026-03-04T05:06:07+00:00"}), encoding="utf-8")
    rows = dict(funnel.journey_rows(vault))
    assert rows[funnel.MILESTONE_LABELS["welcome_rendered"]] == "2026-03-04"
    assert rows[funnel.MILESTONE_LABELS["first_wish"]] == funnel.NOT_YET


def test_an_unparseable_stamp_never_reads_as_not_yet(vault):
    """The two errors are not symmetric: showing 'not yet' for something that DID
    happen is the surface lying about the operator's own history."""
    from systemu.runtime import funnel

    _funnel_file(vault).write_text(json.dumps({
        "first_wish": "sometime last tuesday"}), encoding="utf-8")
    rows = dict(funnel.journey_rows(vault))
    assert rows[funnel.MILESTONE_LABELS["first_wish"]] != funnel.NOT_YET


def test_the_privacy_sentence_is_the_ruled_wording():
    from systemu.runtime import funnel

    assert funnel.PRIVACY_NOTE == (
        "Counted on this machine only. Nothing is sent anywhere.")
    assert funnel.JOURNEY_TITLE == "Your first-run journey"


def test_journey_rows_of_a_broken_store_is_all_not_yet(vault):
    from systemu.runtime import funnel

    _funnel_file(vault).write_text("<<<broken>>>", encoding="utf-8")
    assert all(r[1] == funnel.NOT_YET for r in funnel.journey_rows(vault))
    assert funnel.journey_rows(None)  # never raises, still six rows
    assert len(funnel.journey_rows(None)) == len(funnel.MILESTONES)


# --- source purity: the privacy sentence must be TRUE of the code -----------
_NETWORK_ROOTS = frozenset({
    "socket", "ssl", "http", "https", "urllib", "urllib2", "urllib3",
    "requests", "httpx", "aiohttp", "websocket", "websockets", "ftplib",
    "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "asyncio",
    "subprocess", "webbrowser", "boto3", "httplib",
})


def test_funnel_module_imports_nothing_that_could_reach_the_network():
    """The Insights copy says nothing is sent anywhere. That claim is only worth
    what the module's import list makes true."""
    src = (_SYSTEMU / "runtime" / "funnel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module.split(".")[0])
    offenders = sorted(found & _NETWORK_ROOTS)
    assert not offenders, (
        f"systemu/runtime/funnel.py imports {offenders}, which contradicts the "
        f"Insights page sentence {'Nothing is sent anywhere.'!r}.")


def test_the_purity_scan_is_not_vacuous():
    """A scan that finds no imports at all would pass on an empty file."""
    src = (_SYSTEMU / "runtime" / "funnel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert {"json", "os"} <= roots, (
        "the purity scan did not see the module's real imports - it is measuring "
        "the wrong file or the wrong node types")


def test_the_funnel_is_not_a_fact_writer():
    """No `add_fact` -> no R-A16 census entry is owed. Verified, not assumed."""
    src = (_SYSTEMU / "runtime" / "funnel.py").read_text(encoding="utf-8")
    assert "add_fact" not in src


# --- reachability pins ------------------------------------------------------
def _milestones_marked_in(rel: str, func: str | None = None) -> set[str]:
    """Every literal milestone name passed to a `mark_milestone(...)` call in
    `rel` (optionally scoped to the function named `func`, at any nesting)."""
    tree = ast.parse((_SYSTEMU / rel).read_text(encoding="utf-8"))
    scopes = [tree]
    if func is not None:
        scopes = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == func]
        assert scopes, f"{rel} has no function named {func!r}"
    names: set[str] = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            called = (fn.attr if isinstance(fn, ast.Attribute)
                      else fn.id if isinstance(fn, ast.Name) else "")
            if called != "mark_milestone":
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and type(arg.value) is str:
                    names.add(arg.value)
    return names


def test_welcome_page_render_stamps_welcome_rendered():
    assert "welcome_rendered" in _milestones_marked_in(
        "interface/pages/welcome.py", "build_welcome_page")


def test_finalize_onboarding_stamps_setup_finished():
    assert "setup_finished" in _milestones_marked_in(
        "interface/pages/welcome.py", "finalize_onboarding")


def test_quick_lane_submission_stamps_first_task_submitted():
    assert "first_task_submitted" in _milestones_marked_in(
        "pipelines/quick_task.py", "submit_quick_task")


def test_workflow_lane_submission_stamps_first_task_submitted():
    assert "first_task_submitted" in _milestones_marked_in(
        "pipelines/direct_task.py", "run_direct_task")


def test_both_lane_terminals_stamp_first_task_succeeded():
    """The quick lane is the DEFAULT lane. Wiring only the workflow lane would
    leave a successful operator staring at 'not yet' forever."""
    assert "first_task_succeeded" in _milestones_marked_in(
        "pipelines/direct_task.py", "run_direct_task")
    assert "first_task_succeeded" in _milestones_marked_in(
        "pipelines/quick_task.py", "submit_quick_task")


def test_add_wish_stamps_first_wish():
    assert "first_wish" in _milestones_marked_in("interface/wishes.py", "add_wish")


def test_the_record_dialog_stamps_first_recording():
    assert "first_recording" in _milestones_marked_in(
        "interface/dashboard.py", "_do_record")


def test_no_production_site_stamps_a_milestone_outside_the_vocabulary():
    from systemu.runtime import funnel

    seen: set[str] = set()
    for py in _SYSTEMU.rglob("*.py"):
        rel = py.relative_to(_SYSTEMU).as_posix()
        if rel == "runtime/funnel.py":
            continue
        seen |= _milestones_marked_in(rel)
    assert seen <= set(funnel.MILESTONES), (
        f"a call site stamps a name the module will refuse: "
        f"{sorted(seen - set(funnel.MILESTONES))}")
    assert seen == set(funnel.MILESTONES), (
        f"declared but never stamped anywhere in production: "
        f"{sorted(set(funnel.MILESTONES) - seen)}")


def _calls_in(rel: str, func: str | None = None) -> set[str]:
    tree = ast.parse((_SYSTEMU / rel).read_text(encoding="utf-8"))
    scopes = [tree]
    if func is not None:
        scopes = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == func]
        assert scopes, f"{rel} has no function named {func!r}"
    names: set[str] = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            names.add(fn.attr if isinstance(fn, ast.Attribute)
                      else fn.id if isinstance(fn, ast.Name) else "")
    return names


def test_insights_page_renders_the_journey_and_the_privacy_sentence():
    """The table AND its privacy sentence are one deliverable - the sentence is
    the only thing that tells the operator what the counters are.

    Pinned in BOTH directions on purpose: that the card reads the funnel, AND
    that ``build_insights_page`` actually calls the card. Checking only the
    former would stay green while the renderer sat there uncalled - the exact
    half-built shape (code lands, wiring does not) this pin exists to refuse.
    """
    rel = "interface/pages/insights.py"
    src = (_SYSTEMU / rel).read_text(encoding="utf-8")
    assert "journey_rows" in _calls_in(rel, "build_first_run_journey_card"), (
        "the Insights journey card no longer reads the funnel - 'Your "
        "first-run journey' is unwired")
    assert "build_first_run_journey_card" in _calls_in(rel, "build_insights_page"), (
        "the journey card exists but the Insights page never renders it")
    assert "PRIVACY_NOTE" in src and "JOURNEY_TITLE" in src, (
        "the page must render the module's own constants, never a re-typed "
        "copy that can drift from what the code does")


# --- add_wish, end to end ---------------------------------------------------
def test_add_wish_actually_lands_a_stamp(vault, monkeypatch):
    from systemu.interface import wishes
    from systemu.runtime import funnel, user_profile

    class _Fact:
        id = "uf_1"

    monkeypatch.setattr(user_profile, "add_fact",
                        lambda *_a, **_k: _Fact(), raising=True)
    assert wishes.add_wish(vault, "a nicer expense report") == "uf_1"
    assert "first_wish" in funnel.read_funnel(vault)


def test_a_blank_wish_stamps_nothing(vault, monkeypatch):
    from systemu.interface import wishes
    from systemu.runtime import funnel, user_profile

    monkeypatch.setattr(user_profile, "add_fact",
                        lambda *_a, **_k: pytest.fail("should not be reached"),
                        raising=True)
    assert wishes.add_wish(vault, "   ") is None
    assert funnel.read_funnel(vault) == {}
