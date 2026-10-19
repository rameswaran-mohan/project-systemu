"""The ``systemu.winpkg`` package docstring says what ships WIRED, and no more.

THE DEFECT
    The docstring said the package can "run the first-run wizard, upgrade /
    uninstall itself, and time the first completed task". None of those has a
    production caller: ``winpkg/first_run.py`` (the provider-key wizard) is
    imported only by its own test, ``lifecycle.perform_upgrade`` /
    ``perform_uninstall`` are called by nothing, and nothing invokes
    ``cli stamp-first-task``. A docstring that describes an unwired capability
    is the same false assertion as a README that does (DEC-34c).

THE PIN
    Both directions, against the tree:
      * the docstring names what IS wired (installer inputs, launcher, vault
        pin, install stamp) and says ``first_run.py`` is not used;
      * the "not wired" claims are TRUE -- a scan of the shipped Python and the
        installer script finds no caller. Wire one of them and this fails until
        the docstring moves it to the wired list.

Read-only: nothing is written.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import systemu.winpkg as winpkg

REPO = Path(__file__).resolve().parents[1]
WINPKG = REPO / "systemu" / "winpkg"
ISS = REPO / "packaging" / "windows" / "systemu.iss"

#: Public entry points of the modules the docstring says are NOT wired.
_UNWIRED_NAMES = {
    "run_first_run", "record_provider_key", "decide_handoff",
    "perform_upgrade", "perform_uninstall", "stamp_first_task_completed",
}
_UNWIRED_MODULES = ("first_run", "lifecycle")

#: Where those names are DEFINED or merely re-exported -- not callers.
_DEFINERS = {
    "systemu/winpkg/first_run.py", "systemu/winpkg/lifecycle.py",
    "systemu/winpkg/metrics.py", "systemu/winpkg/__init__.py",
}


def _doc() -> str:
    doc = winpkg.__doc__ or ""
    assert doc.strip(), "systemu.winpkg has no docstring"
    return doc


# --------------------------------------------------------------------------- #
# what the docstring says
# --------------------------------------------------------------------------- #

def test_the_docstring_no_longer_claims_the_unwired_capabilities():
    low = " ".join(_doc().lower().split())
    for claim in ("run the first-run wizard", "upgrade/uninstall itself",
                  "time the first completed task"):
        assert claim not in low, f"winpkg docstring still claims: {claim!r}"


def test_the_docstring_says_first_run_is_not_used():
    flat = " ".join(_doc().split())
    assert re.search(r"first_run.{0,120}\bNOT USED\b", flat), (
        "the docstring must say plainly that first_run.py is not used")


def test_the_docstring_names_what_is_wired():
    flat = " ".join(_doc().split())
    for wired in ("build_step", "systemu-launcher.exe", "launcher",
                  "SYSTEMU_VAULT_DIR", "uninsneveruninstall", "stamp-installed"):
        assert wired in flat, f"the docstring does not name the wired {wired!r}"


# --------------------------------------------------------------------------- #
# the claims are true
# --------------------------------------------------------------------------- #

def _callers_in(path: Path) -> list:
    """Every reference in ``path`` to an unwired module or entry point."""
    rel = path.relative_to(REPO).as_posix()
    in_winpkg = rel.startswith("systemu/winpkg/")
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            absolute = any(mod.endswith(f"winpkg.{m}") for m in _UNWIRED_MODULES)
            relative = (in_winpkg and node.level >= 1
                        and mod in _UNWIRED_MODULES)
            names = {a.name for a in node.names}
            via_pkg = mod.endswith("winpkg") and (names & (_UNWIRED_NAMES
                                                           | set(_UNWIRED_MODULES)))
            if absolute or relative or via_pkg:
                hits.append(f"{rel}:{node.lineno}: from {'.' * node.level}{mod} import")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if any(a.name.endswith(f"winpkg.{m}") for m in _UNWIRED_MODULES):
                    hits.append(f"{rel}:{node.lineno}: import {a.name}")
        elif isinstance(node, ast.Name) and node.id in _UNWIRED_NAMES:
            hits.append(f"{rel}:{node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _UNWIRED_NAMES:
            hits.append(f"{rel}:{node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Constant) and node.value == "stamp-first-task":
            hits.append(f"{rel}:{node.lineno}: 'stamp-first-task'")
    return hits


def test_the_scanner_sees_a_reference_when_there_is_one():
    # Positive control: the package's own re-export IS such a reference.
    assert _callers_in(WINPKG / "__init__.py"), "the caller scan found nothing anywhere"
    assert _callers_in(WINPKG / "cli.py"), "the scan missed cli.py's stamp-first-task"


def test_nothing_shipped_calls_the_unwired_modules():
    offenders = []
    for root in ("systemu", "sharing_on"):
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel in _DEFINERS:
                continue
            hits = _callers_in(path)
            if rel == "systemu/winpkg/cli.py":
                # cli.py DEFINES the stamp-first-task command; defining it is
                # not invoking it.
                hits = [h for h in hits if "stamp" not in h]
            offenders += hits
    assert not offenders, (
        "a winpkg module the docstring calls NOT WIRED now has a caller -- move "
        "it to the wired list in systemu/winpkg/__init__.py:\n  "
        + "\n  ".join(offenders))


def test_the_installer_script_invokes_only_what_the_docstring_says():
    live = [ln for ln in ISS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith(";")]
    text = "\n".join(live)
    assert "systemu.winpkg.cli stamp-installed" in text      # wired, as stated
    assert "uninsneveruninstall" in text                     # the vault pin's half
    for unwired in ("winpkg.first_run", "winpkg.lifecycle", "stamp-first-task"):
        assert unwired not in text, f"systemu.iss now invokes {unwired}"


def test_the_launcher_is_what_the_exe_runs_and_it_pins_the_vault(tmp_path):
    from systemu.winpkg import build_step, launcher
    from systemu.winpkg.layout import literal_layout

    assert build_step.LAUNCHER_MODULE == "systemu.winpkg.launcher"
    layout = literal_layout(tmp_path)
    env: dict = {}
    launcher.launch(launcher.ACTION_OPEN, layout=layout, environ=env,
                    daemon_status=lambda _v: True, start_daemon=lambda *_a: None,
                    open_url=lambda _u: True)
    assert env[launcher.VAULT_DIR_ENV_VAR] == str(layout.vault_dir)
