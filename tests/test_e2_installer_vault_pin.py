"""E2 - the installer can never take the operator's vault with it.

Witnessed WITHOUT running an installer: the Inno script is parsed (defines
expanded, ISPP line continuations joined, comments dropped) and every section
that can delete or own a path is checked against the vault directory.

The vault lives at ``{app}\\vault`` - INSIDE the install dir - so "outside
``{app}``" is not available as the safety argument. What keeps it alive is:

  * a ``[Dirs]`` entry for it flagged ``uninsneveruninstall`` (the explicit pin),
  * no ``[InstallDelete]`` / ``[UninstallDelete]`` entry that names it, sits
    inside it, names an ANCESTOR of it (``{app}`` itself), or is a wildcard that
    matches it (``{app}\\*``),
  * no ``[Files]`` entry installing INTO it (installed files are uninstalled),
  * no ``[Code]`` deletion call aimed at it or an ancestor.

The runtime half of the same property: the launcher points
``SYSTEMU_VAULT_DIR`` at exactly that directory, and the REAL vault-root
resolver honours it from any working directory - including one inside the env
that an upgrade renames away and deletes.

Each checker is also run against a deliberately broken script, so a checker
that stopped looking cannot pass silently.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PureWindowsPath

import pytest

from systemu.winpkg.launcher import (
    ACTION_FIRST_RUN,
    ACTION_OPEN,
    DEFAULT_DASHBOARD_PORT,
    EXIT_NOT_BUILT,
    VAULT_DIR_ENV_VAR,
    hotkey_host_status,
    launch,
    main as launcher_main,
    point_runtime_at_the_sibling_vault,
)
from systemu.winpkg.layout import VAULT_DIRNAME, literal_layout

ISS_PATH = (Path(__file__).resolve().parent.parent
            / "packaging" / "windows" / "systemu.iss")


# --- a small, honest .iss reader -------------------------------------------

def _defines(text: str) -> dict:
    return dict(re.findall(r'^\s*#define\s+(\w+)\s+"([^"]*)"', text, re.MULTILINE))


def _expand(value: str, defines: dict) -> str:
    return re.sub(r"\{#(\w+)\}", lambda m: defines.get(m.group(1), m.group(0)), value)


def _sections(text: str) -> dict:
    """``{section_name_lower: [logical_line, ...]}`` with ISPP ``\\``
    continuations joined and ``;`` comment lines dropped (``[Code]`` keeps its
    raw lines - its comments are ``//`` and it is scanned differently)."""
    sections: dict = {}
    current = None
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        header = re.fullmatch(r"\[(\w+)\]", stripped)
        if header and not pending:
            current = header.group(1).lower()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        if current == "code":
            sections[current].append(raw)
            continue
        if not pending and (not stripped or stripped.startswith(";")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1].rstrip() + " "
            continue
        sections[current].append((pending + stripped).strip())
        pending = ""
    return sections


def _split_params(line: str) -> dict:
    """``Name: "x"; Flags: a b`` -> ``{"name": "x", "flags": "a b"}``."""
    params = {}
    for part in re.findall(r'(?:[^;"]|"[^"]*")+', line):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        params[key.strip().lower()] = value.strip().strip('"')
    return params


def _norm(path: str) -> str:
    return str(PureWindowsPath(path.strip().strip('"'))).rstrip("\\").lower()


def _install_dir(text: str, defines: dict) -> str:
    for line in _sections(text).get("setup", []):
        key, _, value = line.partition("=")
        if key.strip().lower() == "defaultdirname":
            return _expand(value.strip(), defines)
    raise AssertionError("[Setup] has no DefaultDirName")


def _resolve(value: str, text: str, defines: dict) -> str:
    """Expand defines and ``{app}`` into the DefaultDirName it denotes, so an
    entry naming ``{localappdata}\\systemu`` is recognised as ``{app}``."""
    expanded = _expand(value, defines)
    return _norm(expanded.replace("{app}", _install_dir(text, defines)))


def _ancestors(path: str) -> list:
    parts = path.split("\\")
    return ["\\".join(parts[:i]) for i in range(1, len(parts))]


def _reaches(name: str, protected: str) -> bool:
    """Would deleting ``name`` (``filesandordirs``) touch ``protected``?"""
    if any(ch in name for ch in "*?["):
        probes = [protected, protected + "\\tasks.json"] + _ancestors(protected)
        return any(fnmatch.fnmatchcase(p, name) for p in probes)
    return (name == protected
            or protected.startswith(name + "\\")
            or name.startswith(protected + "\\"))


_CODE_DELETERS = ("DelTree", "RemoveDir", "DeleteFile", "DelayDeleteFile")


def vault_pin_violations(text: str) -> list:
    """Every way ``text`` (an Inno script) could delete the vault. ``[]`` = safe."""
    defines = _defines(text)
    sections = _sections(text)
    vault_dirname = defines.get("VaultDirName")
    if not vault_dirname:
        return ["no #define VaultDirName - the vault directory is not declared"]
    vault = _resolve("{app}\\" + vault_dirname, text, defines)
    problems = []

    for section in ("installdelete", "uninstalldelete"):
        for line in sections.get(section, []):
            name = _split_params(line).get("name", "")
            if name and _reaches(_resolve(name, text, defines), vault):
                problems.append(f"[{section}] would delete the vault: {line}")

    for line in sections.get("files", []):
        dest = _split_params(line).get("destdir", "")
        if dest:
            resolved = _resolve(dest, text, defines)
            if resolved == vault or resolved.startswith(vault + "\\"):
                problems.append(f"[Files] installs into the vault (uninstall "
                                f"removes installed files): {line}")

    pinned = []
    for line in sections.get("dirs", []):
        params = _split_params(line)
        if _resolve(params.get("name", ""), text, defines) == vault:
            flags = params.get("flags", "").lower().split()
            if "deleteafterinstall" in flags:
                problems.append(f"[Dirs] deletes the vault after install: {line}")
            if "uninsneveruninstall" in flags:
                pinned.append(line)
    if not pinned:
        problems.append("no [Dirs] entry pins the vault with uninsneveruninstall")

    code = "\n".join(line for line in sections.get("code", [])
                     if not line.strip().startswith("//"))
    for call in re.finditer(r"\b(%s)\s*\(([^;]*)\)" % "|".join(_CODE_DELETERS), code):
        argument = call.group(2)
        if "vault" in argument.lower():
            problems.append(f"[Code] deletes the vault: {call.group(0)}")
            continue
        for literal in re.findall(r"'([^']*)'", argument):
            if _reaches(_resolve(literal, text, defines), vault):
                problems.append(f"[Code] deletes an ancestor of the vault: "
                                f"{call.group(0)}")
    return problems


# --- the shipped script ------------------------------------------------------

def _shipped() -> str:
    return ISS_PATH.read_text(encoding="utf-8")


def test_the_shipped_installer_cannot_delete_the_vault():
    """AC2/AC3 read straight off the script: install, upgrade and uninstall."""
    assert vault_pin_violations(_shipped()) == []


def test_the_vault_dir_is_pinned_uninsneveruninstall():
    """The explicit pin. Without it the vault's survival rests on nobody ever
    widening an ``[UninstallDelete]`` line - an argument from absence."""
    text = _shipped()
    defines = _defines(text)
    vault = _resolve("{app}\\" + defines["VaultDirName"], text, defines)
    entries = [_split_params(line) for line in _sections(text).get("dirs", [])]
    pinned = [e for e in entries
              if _resolve(e.get("name", ""), text, defines) == vault
              and "uninsneveruninstall" in e.get("flags", "").lower().split()]
    assert len(pinned) == 1, f"[Dirs] entries: {entries}"


def test_the_pinned_dir_is_the_one_the_runtime_writes(tmp_path):
    """Cross-artifact: the directory the .iss protects (with ``{app}`` = the
    install root) is byte-for-byte the one the launcher exports as
    ``SYSTEMU_VAULT_DIR``. A pin on a directory nobody writes is no pin."""
    text = _shipped()
    defines = _defines(text)
    assert defines["VaultDirName"] == VAULT_DIRNAME

    root = tmp_path / "systemu"
    exported = point_runtime_at_the_sibling_vault(literal_layout(root), environ={})
    assert Path(exported) == root / defines["VaultDirName"]


# --- the checker is not vacuous ---------------------------------------------

def _break(text: str, section: str, line: str) -> str:
    assert f"[{section}]\n" in text, f"the script has no [{section}] section"
    return text.replace(f"[{section}]\n", f"[{section}]\n{line}\n", 1)


@pytest.mark.parametrize("hostile", [
    'Type: filesandordirs; Name: "{app}\\{#VaultDirName}"',
    'Type: filesandordirs; Name: "{app}"',
    'Type: filesandordirs; Name: "{app}\\*"',
    'Type: filesandordirs; Name: "{localappdata}\\systemu"',
    'Type: files; Name: "{app}\\vault\\*.json"',
    'Type: filesandordirs; Name: "{app}\\VAULT"',
])
def test_the_checker_catches_an_uninstall_delete_that_reaches_the_vault(hostile):
    broken = _break(_shipped(), "UninstallDelete", hostile)
    assert vault_pin_violations(broken), f"checker missed: {hostile}"


def test_the_checker_catches_an_install_delete_on_upgrade():
    broken = _shipped().replace(
        "[UninstallDelete]\n",
        '[InstallDelete]\nType: filesandordirs; Name: "{app}\\*"\n\n[UninstallDelete]\n', 1)
    assert any("installdelete" in p for p in vault_pin_violations(broken))


def test_the_checker_catches_a_removed_pin():
    text = _shipped()
    broken = re.sub(r"(?m)^(?!\s*;).*uninsneveruninstall.*$", "", text)
    assert broken != text
    assert any("uninsneveruninstall" in p for p in vault_pin_violations(broken))


def test_the_checker_catches_files_installed_into_the_vault():
    broken = _break(_shipped(), "Files",
                    'Source: "seed\\*"; DestDir: "{app}\\{#VaultDirName}"; Flags: ignoreversion')
    assert any("[Files]" in p for p in vault_pin_violations(broken))


def test_the_checker_catches_a_code_section_that_deletes_the_vault():
    anchor = "  if CurUninstallStep = usPostUninstall then"
    assert anchor in _shipped()
    broken = _shipped().replace(
        anchor, "  DelTree(VaultDir(), True, True, True);\n" + anchor, 1)
    assert any("[Code]" in p for p in vault_pin_violations(broken))


def test_the_checker_catches_a_code_section_that_deletes_the_app_dir():
    anchor = "  if CurUninstallStep = usPostUninstall then"
    broken = _shipped().replace(
        anchor, "  DelTree(ExpandConstant('{app}'), True, True, True);\n" + anchor, 1)
    assert any("ancestor" in p for p in vault_pin_violations(broken))


def test_a_continued_line_is_read_as_one_entry():
    """``[Run]`` uses ISPP ``\\`` continuations; a reader that split them would
    see a ``Parameters:`` line with no ``Filename:`` and check nothing."""
    run = _sections(_shipped())["run"]
    pip = [line for line in run if "pip install" in line]
    assert len(pip) == 1 and pip[0].startswith("Filename:")


# --- the runtime half: the launcher's pin, end to end ------------------------

def _resolved_root(environ: dict, cwd: Path) -> str:
    """What the REAL runtime resolver makes of an environment and a CWD."""
    from systemu.runtime.vault_root import resolve_vault_root

    return resolve_vault_root(env=environ, cwd=str(cwd)).root


def _same(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
        os.path.abspath(str(b)))


def _inside(path, directory) -> bool:
    return _same(os.path.commonpath([str(path), str(directory)]), directory)


@pytest.mark.parametrize("action", [ACTION_OPEN, ACTION_FIRST_RUN])
def test_the_daemon_is_started_against_the_pinned_vault_from_any_cwd(tmp_path, action):
    """Without the pin the runtime falls back to the RELATIVE default
    ``systemu/vault``, resolved against the CWD. From a CWD inside ``env`` that
    is a vault the next upgrade renames to ``env.old`` and deletes. The stub
    sets the CWD to ``{app}``, but ``env\\python.exe -m systemu.winpkg.launcher``
    can be run from anywhere - so the pin, not the CWD, must decide."""
    layout = literal_layout(tmp_path / "systemu")
    environ: dict = {}
    seen = []

    def _start(vault_dir, port):
        seen.append(_resolved_root(dict(environ), layout.env_dir))

    result = launch(action, layout=layout, environ=environ,
                    daemon_status=lambda vault: False,
                    start_daemon=_start, open_url=lambda url: True)

    assert result.daemon_started is True
    assert len(seen) == 1
    assert _same(seen[0], layout.vault_dir), (
        f"the daemon would operate {seen[0]}, not the pinned {layout.vault_dir}")
    for doomed in (layout.env_dir, layout.env_staging_dir, layout.env_previous_dir):
        assert not _inside(seen[0], doomed)


def test_without_the_pin_the_resolver_WOULD_land_inside_env(tmp_path):
    """The hazard is real, not hypothetical: the same resolver with no
    ``SYSTEMU_VAULT_DIR`` and a CWD inside env puts the vault inside env."""
    layout = literal_layout(tmp_path / "systemu")
    assert _inside(_resolved_root({}, layout.env_dir), layout.env_dir)


# --- launcher CLI ------------------------------------------------------------

@pytest.fixture()
def clean_vault_env(monkeypatch):
    """``main()`` writes the REAL ``os.environ`` (the daemon child inherits it).
    setenv-then-delenv makes monkeypatch restore the variable's absence."""
    monkeypatch.setenv(VAULT_DIR_ENV_VAR, "placeholder")
    monkeypatch.delenv(VAULT_DIR_ENV_VAR)
    return monkeypatch


def test_main_status_names_the_pinned_vault_and_exports_it(tmp_path, capsys, clean_vault_env):
    root = tmp_path / "systemu"
    code = launcher_main(["--status", "--root", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert str(root / VAULT_DIRNAME) in out
    assert os.environ[VAULT_DIR_ENV_VAR] == str(root / VAULT_DIRNAME)


def test_main_hotkey_exits_not_built_on_stderr(tmp_path, capsys, clean_vault_env):
    code = launcher_main(["--hotkey", "--root", str(tmp_path / "systemu")])
    captured = capsys.readouterr()

    assert code == EXIT_NOT_BUILT
    assert "R-P7 is not built" in captured.err
    assert captured.out == ""
    assert VAULT_DIR_ENV_VAR not in os.environ, (
        "a refused action must not have touched the environment")


def test_the_launcher_and_the_daemon_agree_on_the_dashboard_port():
    from systemu.scheduler.daemon import DEFAULT_DASHBOARD_PORT as DAEMON_PORT

    assert DEFAULT_DASHBOARD_PORT == DAEMON_PORT


# --- the over-claim is gone and stays gone -----------------------------------

def test_the_installer_script_does_not_claim_a_hotkey_it_does_not_ship():
    """The audit found the .iss advertising an R-P7 hotkey host. Nothing in
    this repo registers a hotkey, so the script must say what the launcher
    says - and never the old claim."""
    assert hotkey_host_status()["available"] is False
    text = _shipped()
    lowered = text.lower()
    for claim in ("hosts the r-p7 hotkey", "r-p7 hotkey host)", "+ r-p7 hotkey"):
        assert claim not in lowered, f"over-claim back in the .iss: {claim!r}"
    assert "R-P7 is not built" in text


def test_the_installer_script_no_longer_says_its_inputs_are_unproduced():
    assert "NOT YET PRODUCED" not in _shipped()
    assert "python -m systemu.winpkg.build_step" in _shipped()
