"""F2 -- no advertised command may die of a bare configuration error on a
DEFAULT (file-storage) install.

Live defect this pins (v0.10.22):

    $ python -m sharing_on world
    ERROR: SYSTEMU_DATABASE_URL not set
    exit 2

``world`` is advertised in ``--help`` as "Show what systemu's world model
believes about your setup", yet it demanded a SQL database that the default
install does not have and that nothing else on the default path needs.  The
same copy-pasted block sat in ``find-tools`` and in ``doctor <scope_id>``.

CODE EVIDENCE that the world model does not need a database at all:
  * ``systemu/runtime/world_model.py`` -- ``FactStore._dir`` is
    ``Path(vault.root) / "world_model"``; facts and negatives are plain JSON
    files.  The only vault attribute it touches is ``.root``, which BOTH
    backends expose (``systemu/vault/vault.py`` and
    ``systemu/storage/sqlite/vault.py:837``).
  * ``systemu/runtime/tools/world_tools.py:80`` -- the in-agent world tool
    already resolves its vault with ``open_vault(Config.from_env())`` and
    works on the file backend.  Only the CLI surface disagreed.

So ``world`` and ``find-tools`` are fixed by routing them through the single
authoritative factory (option (a)).  ``doctor <scope_id>`` genuinely cannot be
served by the file backend -- ``RecoveryEngine`` needs ``find_scroll``,
``find_activity``, ``find_activity_for_scroll``, ``find_shadow``,
``find_tool`` and ``skill_exists``, none of which the file ``Vault``
implements -- so it takes option (b): an honest message naming the exact
remedy, never a bare ``ERROR: <ENV_VAR> not set``.

PROPERTY
    No command advertised in ``sharing_on --help`` fails on a default
    file-storage install without an actionable message naming the exact
    remedy.

FENCE
    ``test_no_cli_command_resolves_storage_from_env_directly`` walks the WHOLE
    click command tree (no allowlist, no exemptions) and refuses any command
    callback that reaches storage by reading an env var or by constructing a
    backend class directly instead of calling the fail-soft
    ``systemu.vault.factory.open_vault``.  That is the exact copy-paste shape
    that produced three broken commands from one mistake.

WITNESS
    Reintroduce the bug -- paste ``db_url = os.environ.get(
    "SYSTEMU_DATABASE_URL") ... SqliteVault(database_url=db_url)`` into any new
    or existing command -- and the fence test fails naming that command, while
    the real-CLI subprocess tests below fail with the bare error text.
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import click

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _iter_commands(cmd, path=()):
    """Yield ``(dotted_path, click.Command)`` for every command in the tree,
    groups included.  This is the *advertised* surface: exactly what a user can
    reach from ``--help``."""
    here = path + (cmd.name,) if cmd.name else path
    yield here, cmd
    if isinstance(cmd, click.Group):
        for sub in cmd.commands.values():
            yield from _iter_commands(sub, here)


def _cli_tree():
    from sharing_on.cli import cli
    return list(_iter_commands(cli))


def _default_install_env(vault_dir: Path) -> dict:
    """A DEFAULT install environment: no SYSTEMU_STORAGE (so 'file'), and no
    SYSTEMU_DATABASE_URL, whatever the developer happens to have exported."""
    env = dict(os.environ)
    for var in ("SYSTEMU_DATABASE_URL", "DATABASE_URL", "SYSTEMU_STORAGE"):
        env.pop(var, None)
    env["SYSTEMU_VAULT_DIR"] = str(vault_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_cli(args, vault_dir: Path):
    return subprocess.run(
        [sys.executable, "-m", "sharing_on", *args],
        cwd=str(REPO_ROOT),
        env=_default_install_env(vault_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=300,
    )


#: the shape of a BARE configuration error -- an env var name and a complaint,
#: with nothing the operator can actually run.
_BARE_CONFIG_ERROR = re.compile(r"ERROR:\s*[A-Z][A-Z0-9_]{4,}\s+not set")


# --------------------------------------------------------------------------- #
# FENCE -- quantified over the WHOLE advertised command tree
# --------------------------------------------------------------------------- #

#: constructing any of these inside a command callback bypasses the factory.
_BACKEND_CTORS = frozenset({"SqliteVault", "ParallelVault", "FileVault"})


def _callback_ast(cmd):
    """The parsed body of a command callback, or None.

    AST, not text: a comment or a ``--help`` docstring that happens to *name*
    an env var is documentation, while ``os.environ.get("...")`` is behaviour.
    Scanning raw source cannot tell them apart and would punish the comment
    explaining this very fix.
    """
    cb = getattr(cmd, "callback", None)
    if cb is None:
        return None
    try:
        src = textwrap.dedent(inspect.getsource(cb))
        tree = ast.parse(src)
    except (OSError, TypeError, SyntaxError, IndentationError):  # pragma: no cover
        return None
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):  # pragma: no cover
        return None
    body = list(fn.body)
    # drop the docstring: it is help text, not behaviour
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _string_constants(body):
    for node in body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub.value


def _called_names(body):
    for node in body:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    yield f.id
                elif isinstance(f, ast.Attribute):
                    yield f.attr


def test_no_cli_command_resolves_storage_from_env_directly():
    """No CLI callback may resolve storage itself. Flat prohibition, no ordering.

    ``open_vault`` (and, for scoped recovery, ``open_recovery_vault``) are the
    only places that decide which store serves a command; both honour
    ``SYSTEMU_STORAGE`` and neither hard-exits on a default install. A callback
    that reads a ``*_DATABASE_URL`` env var, or news up a backend class, has
    opted out of that. If a command genuinely cannot serve the file backend it
    must ask the factory and then refuse with a runnable remedy (see ``doctor``).

    F31 -- WHY THIS IS BACK TO A FLAT BAN. An intermediate version weakened this
    to an ORDERING rule ("a URL read is fine provided some ``open_vault`` call
    appears above it"), so that scoped ``doctor`` could honour an explicitly
    supplied ``SYSTEMU_DATABASE_URL`` inline. An adversarial review then showed
    the ordering form is satisfiable by a DECOY: a discarded ``open_vault``
    call, a dead-code decoy, a same-line ternary, an attribute-form constructor,
    or an unconditional override written below the call all passed the ordering
    rule while the flat ban caught every one. Only the historical pre-F2 shape
    failed both -- which is precisely the single mutant the weakened version had
    been mutation-tested against, so the check looked sound and was not.

    The lesson is not "write a cleverer predicate". Textual call order is a proxy
    for behaviour, and a proxy the checked party controls is not a check at all
    (DEC-34). The fix was to remove the need: the store decision now lives in
    ``systemu.vault.factory.open_recovery_vault`` (DEC-43, one mint), so no
    callback has any reason to name a URL and this can go back to a flat ban.
    """
    offenders = []
    for path, cmd in _cli_tree():
        body = _callback_ast(cmd)
        if body is None:
            continue
        name = " ".join(path)
        if any("DATABASE_URL" in s for s in _string_constants(body)):
            offenders.append(f"{name}: reads a *_DATABASE_URL env var directly")
        for called in _called_names(body):
            if called in _BACKEND_CTORS:
                offenders.append(f"{name}: constructs {called} directly")
    assert not offenders, (
        "CLI commands must resolve storage via systemu.vault.factory.open_vault "
        "(file-by-default, never a hard exit) -- offenders:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_no_cli_command_emits_a_bare_configuration_error_string():
    """No command callback may print a bare ``ERROR: SOME_VAR not set``.

    Complements the test above: the remedy half of the property.  A command may
    still refuse -- it may not refuse *uselessly*.
    """
    offenders = []
    for path, cmd in _cli_tree():
        body = _callback_ast(cmd)
        if body is None:
            continue
        if any(_BARE_CONFIG_ERROR.search(s) for s in _string_constants(body)):
            offenders.append(" ".join(path))
    assert not offenders, (
        "bare 'ERROR: <ENV_VAR> not set' messages give the operator nothing to "
        f"do -- offenders: {sorted(set(offenders))}"
    )


# --------------------------------------------------------------------------- #
# REAL CLI (DEC-44: the conftest survey stub cannot reach a subprocess)
# --------------------------------------------------------------------------- #

def test_world_runs_on_a_default_file_storage_install(tmp_path):
    r = _run_cli(["world"], tmp_path / "vault")
    combined = r.stdout + r.stderr
    assert "SYSTEMU_DATABASE_URL" not in combined, combined
    assert r.returncode == 0, combined
    assert "world model" in combined.lower(), combined


def test_world_query_runs_on_a_default_file_storage_install(tmp_path):
    r = _run_cli(["world", "github"], tmp_path / "vault")
    combined = r.stdout + r.stderr
    assert "SYSTEMU_DATABASE_URL" not in combined, combined
    assert r.returncode == 0, combined


def test_find_tools_runs_on_a_default_file_storage_install(tmp_path):
    r = _run_cli(["find-tools", "send", "email"], tmp_path / "vault")
    combined = r.stdout + r.stderr
    assert "SYSTEMU_DATABASE_URL" not in combined, combined
    assert r.returncode == 0, combined


def test_doctor_scope_refuses_with_a_runnable_remedy_not_a_bare_error(tmp_path):
    """``doctor <scope_id>`` is option (b): it needs the SQL recovery store.

    It must still not die of a bare env-var complaint -- the operator has to be
    told the exact thing to run.
    """
    r = _run_cli(["doctor", "scr_doesnotexist"], tmp_path / "vault")
    combined = r.stdout + r.stderr
    assert not _BARE_CONFIG_ERROR.search(combined), combined
    # names the exact remedy, as a command the operator can copy
    assert "SYSTEMU_STORAGE=sqlite" in combined, combined
    # F23: resolve the program name instead of spelling one of the two equal
    # console scripts -- the property is "a command the operator can copy",
    # which a literal that pip may not install would not have proven.
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    _scripts = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["scripts"]
    assert any(f"{p} doctor" in combined for p in _scripts), (
        f"the remedy must name an installed console script {sorted(_scripts)}:\n"
        f"{combined}")


def test_the_remedy_doctor_prints_actually_works(tmp_path):
    """DEC-34: a remedy asserted but never executed is a false claim.

    Follow the exact instruction the previous test read off the screen and
    confirm it gets PAST the backend gate (it then reports the record missing,
    which is the pre-existing, correct behaviour for an unknown id).
    """
    env = _default_install_env(tmp_path / "vault")
    env["SYSTEMU_STORAGE"] = "sqlite"          # <- the printed remedy, verbatim
    r = subprocess.run(
        [sys.executable, "-m", "sharing_on", "doctor", "scr_doesnotexist"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="backslashreplace", timeout=300,
    )
    combined = r.stdout + r.stderr
    assert "needs the SQL recovery store" not in combined, combined
    assert "not found in vault" in combined, combined


def test_bare_doctor_self_diagnosis_still_works_on_file_storage(tmp_path):
    """Regression guard: the no-argument ``doctor`` path never needed a DB and
    must not acquire one."""
    r = _run_cli(["doctor"], tmp_path / "vault")
    combined = r.stdout + r.stderr
    assert "SYSTEMU_DATABASE_URL" not in combined, combined
