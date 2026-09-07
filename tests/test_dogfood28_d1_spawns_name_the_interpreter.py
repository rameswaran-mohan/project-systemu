"""DOGFOOD-28 D1 (fence a) -- no hop may name a Python program by a bare name.

THE INVESTIGATION THIS PINS
    An operator dogfooding the shipped wheel in a fresh venv on Windows saw TWO
    live processes carrying identical daemon argv: one under the venv
    interpreter, one under the base interpreter, parent and child. The reported
    diagnosis was "a second spawn site resolves `python` from PATH".

    It is not. Every spawn in this tree already names ``sys.executable``, and
    the twin is the CPython venv redirector: on Windows ``python -m venv``
    installs ``Scripts/python.exe`` as ``venvlauncher.exe``, which CreateProcess-es
    the base interpreter with the SAME argv and waits on it inside a job object.
    That is one daemon rendered as two rows in a process list.
    ``tests/test_dogfood28_d1_one_daemon_per_vault_and_port.py`` witnesses that.

    But the reported failure mode -- a child interpreter resolved from PATH
    instead of from the launching interpreter -- is real, cheap to reintroduce,
    and invisible until an operator loses a night to it. So it gets a fence.

PROPERTY
    No process this tree starts may name a Python or systemu program by a bare
    name. The program is ALWAYS ``sys.executable`` (or a local provably derived
    from it), never ``python`` / ``python3`` / ``py`` / ``pythonw`` /
    ``systemu`` / ``sharing_on`` / ``pip`` resolved from ``PATH``.

    A bare name is not a smaller version of the same thing: ``PATH`` in a child
    process is whatever the operator's shell, the installer and every other
    Python on the box negotiated, so it selects an interpreter that is related
    to the running one only by coincidence.

FENCE
    Two, and they fail in BOTH directions (DEC-32c):
      * repo-wide: a banned bare literal in any spawn argv is fatal;
      * for the three files that own daemon launching, the exact set of
        Python-launching spawn sites is COMMITTED -- a new hop, or an existing
        hop that stops naming ``sys.executable``, is fatal.

WITNESS
    Rewrite ``daemon.py``'s ``cmd`` to start with ``"python"`` and
    ``test_no_spawn_names_a_python_program_by_a_bare_name`` goes red; drop
    ``sys.executable`` from either committed site and
    ``test_the_daemon_launching_hops_are_exactly_these_and_all_name_sys_executable``
    goes red.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import sharing_on
import systemu

# The programs whose resolution must never be delegated to PATH. Stems are
# compared case-insensitively and without a Windows executable suffix, because
# `Python.EXE` and `python` name the same delegation.
BANNED_PROGRAM_STEMS = frozenset({
    "python", "python3", "python2", "py", "pythonw", "pythonw3",
    "systemu", "sharing_on", "pip", "pip3",
})

#: Callables that start a process. ``multiprocessing.set_executable`` is here
#: because it rewrites the program EVERY later spawn will use.
SPAWN_FUNCS = frozenset({
    "Popen", "run", "call", "check_call", "check_output",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
    "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle",
    "spawnlp", "spawnlpe", "startfile", "set_executable",
})

#: The COMMITTED daemon-launching hops. (relative path, enclosing function).
#: Both directions are fatal: a missing entry means a hop was deleted or
#: renamed without anyone re-deciding how it names its interpreter; an extra
#: entry means a new hop appeared and nobody looked at it.
COMMITTED_PYTHON_HOPS = {
    ("systemu/scheduler/daemon.py", "start_daemon"),
    ("sharing_on/cli.py", "_run_analysis_in_background"),
}


def _roots() -> list[Path]:
    return [Path(systemu.__file__).resolve().parent,
            Path(sharing_on.__file__).resolve().parent]


def _repo_root() -> Path:
    return Path(systemu.__file__).resolve().parent.parent


def _py_files() -> list[Path]:
    out: list[Path] = []
    for root in _roots():
        out.extend(sorted(p for p in root.rglob("*.py")))
    return out


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_repo_root()).as_posix()


def _stem_of(text: str) -> str:
    """The program name a literal denotes, stripped of directory and suffix."""
    if type(text) is not str or not text:
        return ""
    head = text.strip().strip('"').strip("'")
    # A shell string ("python -m x") names its program in the first token.
    head = head.replace("\\", "/").split()[0] if head.split() else head
    head = head.rsplit("/", 1)[-1]
    if head.lower().endswith(".exe"):
        head = head[:-4]
    return head.lower()


def _is_sys_executable(node: ast.AST) -> bool:
    """``sys.executable`` written out, in this expression, right here."""
    return (isinstance(node, ast.Attribute)
            and node.attr == "executable"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys")


def _mentions_sys_executable(node: ast.AST) -> bool:
    return any(_is_sys_executable(sub) for sub in ast.walk(node))


class _Scope:
    """One function (or module) body, with the local list-literals it binds.

    Only assignments made in the SAME scope count as provenance. A name bound
    somewhere else is treated as unknown, never as derived -- inferring
    provenance is how a fence comes to certify what it never saw (DEC-27).
    """

    def __init__(self, node: ast.AST, name: str) -> None:
        self.node = node
        self.name = name
        self.bindings: dict[str, ast.AST] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Name):
                        self.bindings.setdefault(tgt.id, sub.value)

    def resolve(self, node: ast.AST) -> ast.AST | None:
        """The expression a spawn's first argument really carries."""
        seen = 0
        while isinstance(node, ast.Name) and seen < 4:
            nxt = self.bindings.get(node.id)
            if nxt is None:
                return None
            node, seen = nxt, seen + 1
        return node


def _scopes(tree: ast.AST, path: Path):
    yield _Scope(tree, "<module>")
    for sub in ast.walk(tree):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _Scope(sub, sub.name)


def _spawn_calls(scope: _Scope):
    """Every process-starting call whose first argument we can see."""
    for node in ast.walk(scope.node):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if type(fname) is not str or fname not in SPAWN_FUNCS:
            continue
        # `run`/`call` are common method names; require a module-qualified call
        # or an argv-shaped first argument so `self.run(...)` is not scanned.
        if not node.args:
            continue
        yield node


def _argv_head(scope: _Scope, call: ast.Call) -> ast.AST | None:
    """The expression naming the PROGRAM of this spawn, or None if unreadable."""
    first = scope.resolve(call.args[0])
    if first is None:
        return None
    if isinstance(first, (ast.List, ast.Tuple)):
        return first.elts[0] if first.elts else None
    if isinstance(first, ast.BinOp) and isinstance(first.left, (ast.List, ast.Tuple)):
        return first.left.elts[0] if first.left.elts else None
    return first          # a bare string command (shell=True) names it directly


# ═════════════════════════════════════════════════════════════════════════════
#  a.1 -- repo-wide: never delegate a Python program to PATH
# ═════════════════════════════════════════════════════════════════════════════

def test_no_spawn_names_a_python_program_by_a_bare_name():
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="backslashreplace"))
        for scope in _scopes(tree, path):
            for call in _spawn_calls(scope):
                head = _argv_head(scope, call)
                if not isinstance(head, ast.Constant):
                    continue
                if _stem_of(head.value) in BANNED_PROGRAM_STEMS:
                    offenders.append(
                        f"{_rel(path)}:{call.lineno} in {scope.name}(): "
                        f"program {head.value!r}")
    assert offenders == [], (
        "a spawn names a Python/systemu program by a bare name, so the child "
        "interpreter is whatever PATH resolves -- on an upgrader's box that is "
        "the base install, not the venv the operator is running:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_no_module_rewrites_the_interpreter_every_later_spawn_will_use():
    """``multiprocessing.set_executable`` / ``PYTHONEXECUTABLE`` silently
    redirect spawns that look correct at their own call site."""
    offenders = []
    for path in _py_files():
        src = path.read_text(encoding="utf-8-sig", errors="backslashreplace")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "attr", None)
                if fname == "set_executable":
                    offenders.append(f"{_rel(path)}:{node.lineno} set_executable")
            if isinstance(node, ast.Constant) and node.value == "PYTHONEXECUTABLE":
                offenders.append(f"{_rel(path)}:{node.lineno} PYTHONEXECUTABLE")
    assert offenders == [], (
        "the interpreter used by later spawns is rewritten here: " + "; ".join(offenders))


def test_no_shutil_which_picks_the_interpreter():
    """``shutil.which('python')`` IS a PATH lookup wearing a function call."""
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="backslashreplace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "which" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and _stem_of(arg.value) in BANNED_PROGRAM_STEMS:
                offenders.append(f"{_rel(path)}:{node.lineno} which({arg.value!r})")
    assert offenders == [], (
        "an interpreter is being chosen by PATH lookup: " + "; ".join(offenders))


# ═════════════════════════════════════════════════════════════════════════════
#  a.2 -- the daemon-launching hops are exactly these, and all name sys.executable
# ═════════════════════════════════════════════════════════════════════════════

def _python_hops() -> dict[tuple[str, str], bool]:
    """{(relpath, function): names_sys_executable} for every hop that launches
    a Python program with ``-m``."""
    found: dict[tuple[str, str], bool] = {}
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="backslashreplace"))
        for scope in _scopes(tree, path):
            if scope.name == "<module>":
                continue
            for call in _spawn_calls(scope):
                argv = scope.resolve(call.args[0])
                if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
                    continue
                dash_m = any(isinstance(e, ast.Constant) and e.value == "-m"
                             for e in argv.elts)
                if not dash_m:
                    continue
                key = (_rel(path), scope.name)
                found[key] = found.get(key, True) and _is_sys_executable(argv.elts[0])
    return found


def test_the_daemon_launching_hops_are_exactly_these_and_all_name_sys_executable():
    hops = _python_hops()
    daemon_hops = {k for k in hops
                   if k[0] in ("systemu/scheduler/daemon.py", "sharing_on/cli.py")}
    assert daemon_hops == COMMITTED_PYTHON_HOPS, (
        "the set of daemon-launching hops changed. Every entry here starts a "
        "Python program and must name sys.executable; re-decide the new one "
        "before committing it.\n"
        f"  committed: {sorted(COMMITTED_PYTHON_HOPS)}\n"
        f"  found:     {sorted(daemon_hops)}")
    for key in sorted(COMMITTED_PYTHON_HOPS):
        assert hops[key] is True, (
            f"{key[0]}::{key[1]}() no longer names sys.executable as the "
            "program -- the child interpreter is now whatever PATH resolves")


@pytest.mark.parametrize("bad", ["python", "Python.EXE", "py", "pythonw",
                                 "systemu", "C:/Windows/python3.exe",
                                 "python -m systemu.scheduler.daemon"])
def test_the_bare_name_detector_actually_detects(bad):
    """A fence that cannot see the thing it forbids is worse than none
    (DEC-34): pin the detector against the literals it must catch."""
    assert _stem_of(bad) in BANNED_PROGRAM_STEMS


@pytest.mark.parametrize("ok", ["node", "docker", "xdotool", "pkill", "playwright"])
def test_the_bare_name_detector_leaves_non_python_programs_alone(ok):
    assert _stem_of(ok) not in BANNED_PROGRAM_STEMS
