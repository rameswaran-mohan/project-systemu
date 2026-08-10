"""F6 -- every command string in user-facing output must name a command the
operator can actually invoke.

Live defect this pins (v0.10.22): three runtime messages told the operator to
run ``systemu doctor --set-passphrase``.  ``systemu`` is the PyPI DISTRIBUTION
name; the installed console script is ``sharing_on`` (pyproject
``[project.scripts]``).  Typing it gets command-not-found, so the remedy for
"the dashboard has no passphrase" was unreachable.  The ``--set-passphrase``
flag itself is real (sharing_on/cli.py) -- only the program name was wrong.

The sweep found the same class in nine more places, including three
``fix_command`` values printed verbatim by ``sharing_on doctor <id>`` for
subcommands that have never existed (``tools review``, ``tools install-deps``).

v0.10.24 EXTENSION -- the OTHER half of the same property: the DOCS.  F6
originally policed only the strings inside ``systemu/`` and ``sharing_on/``, so
the *install instructions* were unfenced.  That left the discovery trap live in
the opposite direction: ``pip install systemu`` puts a command called
``sharing_on`` on PATH -- a name that appears nowhere in the line the user just
typed -- while several documents told operators to run ``systemu <cmd>``, which
was not a console script at all.  Both halves are the same defect: a program
name in an instruction that the wheel does not install.

PROPERTY
    Every command name that appears in an install instruction, in ``--help``,
    or in runtime output is installed by the wheel and invocable: the program
    is a real console script declared in ``[project.scripts]``, the subcommand
    chain resolves in the real click tree, and every option it passes exists on
    the command it resolved to.

FENCE
    ``test_every_command_string_names_an_invocable_command`` below.  It parses
    every .py file in ``systemu/`` and ``sharing_on/``, pulls every string
    constant (docstrings included -- click prints them as ``--help``), extracts
    the command-shaped spans, and resolves each one against the LIVE click
    tree and the LIVE ``[project.scripts]`` table.  Nothing is hard-coded: add
    a command and the fence starts accepting it; rename one and the fence
    starts rejecting the strings that still mention the old name.

    ``test_every_program_name_the_docs_tell_you_to_run_is_a_console_script``
    runs the SAME extractor over every markdown file in the repo and asserts
    the program-name half (the half that is meaningful for archival documents,
    whose shell pipelines and ``a/b/c`` shorthand are not real argv).
    ``test_live_docs_name_only_fully_invocable_commands`` applies the FULL
    resolution to the four documents that are the operator's actual
    instructions.  One extractor, one ground truth, three call sites -- not a
    second parallel fence.

WITNESS
    Put ``systemu doctor --set-passphrase`` back into any message and this test
    fails naming the file, line and reason.  Same for a renamed subcommand or a
    flag that does not exist.  Delete the ``systemu`` entry from
    ``[project.scripts]`` and the docs fence fails naming every document that
    tells an operator to run it.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import click

REPO_ROOT = Path(__file__).resolve().parents[1]

#: the packages whose strings reach an operator (messages, CTAs, --help text).
_SCANNED_PACKAGES = ("systemu", "sharing_on")

#: The documents that ARE the operator's instructions -- the install line, the
#: quick start, the one-page SOP, the upgrade path.  These get the full
#: resolution (program + subcommand chain + options), because a broken command
#: here is a user who cannot start.  Everything else under ``docs/`` is design
#: notes, plans and release archaeology: its shell pipelines (``| tail -5``)
#: and ``list/show/refine`` shorthand are prose about argv, not argv, so only
#: the program-name half applies there.
_LIVE_DOCS = ("README.md", "USER_GUIDE.md", "OPERATOR-SOP.md", "MIGRATION.md")


# --------------------------------------------------------------------------- #
# ground truth: what is ACTUALLY installed / invocable
# --------------------------------------------------------------------------- #

def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _console_scripts() -> frozenset:
    """The names pip actually puts on PATH.  Entry-point script names are used
    verbatim -- pip does NOT create an ``-``/``_`` alias -- so this is the whole
    set of program names a message may legitimately tell someone to type."""
    return frozenset(_pyproject()["project"].get("scripts", {}))


def _program_lookalikes() -> frozenset:
    """Names that a message might plausibly use AS IF they were the command.

    The distribution name and the punctuation variants of both it and the real
    scripts.  These are exactly the tokens worth policing: everything else in a
    sentence is prose.
    """
    scripts = _console_scripts()
    dist = _pyproject()["project"]["name"]
    out = set(scripts) | {dist}
    for n in list(out):
        out.add(n.replace("_", "-"))
        out.add(n.replace("-", "_"))
    return frozenset(out)


def _root_group():
    from sharing_on.cli import cli
    return cli


# --------------------------------------------------------------------------- #
# extracting command-shaped spans from a string literal
# --------------------------------------------------------------------------- #

_BACKTICKED = re.compile(r"`([^`\n]{2,200})`")


def _command_spans(text: str):
    """Yield the spans of ``text`` that CLAIM to be an invocation.

    Two shapes, both of which the codebase actually uses:
      * a backtick-quoted span -- ``run `sharing_on doctor` `` ;
      * a whole line that begins with a program name (the ``\\b`` Examples
        blocks in click docstrings, and CTA strings).

    A line ending in sentence punctuation is prose about the product, not an
    instruction ("3 systemu daemon processes are running."), and is skipped.
    """
    for m in _BACKTICKED.finditer(text):
        yield m.group(1).strip()
    for line in text.splitlines():
        s = line.strip()
        if not s or s[-1] in ".?!":
            continue
        yield s


def _invocations(text: str, progs: frozenset, root):
    """Yield ``[token, ...]`` for each span that really is an invocation.

    Gate: first token is a program lookalike AND the second token is a real
    top-level command.  That second condition is what separates "run
    `sharing_on world`" from "systemu learned it from an answer you gave" --
    without it, every sentence starting with the product name is a false alarm.
    """
    for span in _command_spans(text):
        span = span.split("#")[0].strip()          # drop trailing shell comments
        tokens = span.split()
        if len(tokens) < 2 or tokens[0] not in progs:
            continue
        if tokens[1] not in root.commands:
            continue
        yield tokens


def _resolve(tokens, scripts, root):
    """Return a reason string if ``tokens`` is not invocable, else None."""
    prog, rest = tokens[0], tokens[1:]
    if prog not in scripts:
        return (f"program {prog!r} is not an installed console script "
                f"{sorted(scripts)} -- {prog!r} is only the distribution name")

    node = root
    consumed = []
    while rest and not rest[0].startswith("-"):
        if not isinstance(node, click.Group):
            break
        tok = rest[0]
        sub = node.commands.get(tok)
        if sub is None:
            return (f"{' '.join([prog] + consumed)!r} has no subcommand {tok!r} "
                    f"(it has: {sorted(node.commands)})")
        node = sub
        consumed.append(tok)
        rest = rest[1:]

    known = {"--help"}
    for param in node.params:
        known.update(getattr(param, "opts", None) or ())
        known.update(getattr(param, "secondary_opts", None) or ())
    for tok in rest:
        if not tok.startswith("-"):
            continue
        base = tok.split("=", 1)[0]
        if base not in known:
            return (f"{' '.join([prog] + consumed)!r} has no option {base!r} "
                    f"(it has: {sorted(known)})")
    return None


# --------------------------------------------------------------------------- #
# THE FENCE
# --------------------------------------------------------------------------- #

def test_every_command_string_names_an_invocable_command():
    scripts = _console_scripts()
    progs = _program_lookalikes()
    root = _root_group()
    assert scripts, "pyproject declares no console scripts -- fence has no ground truth"

    findings = []
    scanned_files = 0
    for pkg in _SCANNED_PACKAGES:
        for path in sorted((REPO_ROOT / pkg).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
                findings.append(f"{path}: unparseable ({exc})")
                continue
            scanned_files += 1
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                for tokens in _invocations(node.value, progs, root):
                    reason = _resolve(tokens, scripts, root)
                    if reason:
                        rel = path.relative_to(REPO_ROOT).as_posix()
                        findings.append(f"{rel}:{node.lineno}: {' '.join(tokens)!r} -- {reason}")

    assert scanned_files > 100, f"scanner only saw {scanned_files} files -- it is not looking"
    assert not findings, (
        "user-facing strings name commands that cannot be invoked:\n  "
        + "\n  ".join(sorted(set(findings)))
    )


# --------------------------------------------------------------------------- #
# THE FENCE, half two: the DOCS (v0.10.24)
# --------------------------------------------------------------------------- #

def _doc_files():
    """Every markdown document in the repo, live docs first."""
    seen = set()
    for name in _LIVE_DOCS:
        p = REPO_ROOT / name
        if p.exists():
            seen.add(p)
            yield p
    for p in sorted(REPO_ROOT.glob("*.md")):
        if p not in seen:
            seen.add(p)
            yield p
    for p in sorted((REPO_ROOT / "docs").rglob("*.md")):
        if p not in seen:
            seen.add(p)
            yield p


def test_every_program_name_the_docs_tell_you_to_run_is_a_console_script():
    """A document that says ``run X foo`` must be naming a program pip installs.

    This is the install-instruction half of the property.  It is deliberately
    the PROGRAM-NAME check only: applied to every markdown file in the repo,
    including the archived plans and specs, because a wrong program name is
    wrong wherever it appears, while a stale subcommand in a 2026-05 design
    note is history, not an instruction.
    """
    scripts = _console_scripts()
    progs = _program_lookalikes()
    root = _root_group()
    assert scripts, "pyproject declares no console scripts -- fence has no ground truth"

    findings = []
    scanned = 0
    for path in _doc_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover
            findings.append(f"{path}: unreadable ({exc})")
            continue
        scanned += 1
        for tokens in _invocations(text, progs, root):
            prog = tokens[0]
            if prog in scripts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            findings.append(
                f"{rel}: {' '.join(tokens)!r} -- program {prog!r} is not an "
                f"installed console script {sorted(scripts)}; pip install "
                f"systemu puts only those names on PATH"
            )

    assert scanned > 20, f"scanner only saw {scanned} markdown files -- it is not looking"
    assert not findings, (
        "documents tell operators to run a program the wheel does not install:\n  "
        + "\n  ".join(sorted(set(findings)))
    )


def test_live_docs_name_only_fully_invocable_commands():
    """The four operator-facing documents get the FULL resolution."""
    scripts = _console_scripts()
    progs = _program_lookalikes()
    root = _root_group()

    findings = []
    scanned = 0
    for name in _LIVE_DOCS:
        path = REPO_ROOT / name
        assert path.exists(), f"{name} is gone -- revisit _LIVE_DOCS"
        scanned += 1
        for tokens in _invocations(path.read_text(encoding="utf-8", errors="replace"),
                                   progs, root):
            reason = _resolve(tokens, scripts, root)
            if reason:
                findings.append(f"{name}: {' '.join(tokens)!r} -- {reason}")

    assert scanned == len(_LIVE_DOCS)
    assert not findings, (
        "operator-facing documents name commands that cannot be invoked:\n  "
        + "\n  ".join(sorted(set(findings)))
    )


def test_every_declared_console_script_resolves_to_a_real_callable():
    """``[project.scripts]`` is only a promise -- keep it honest.

    A declared entry point whose ``module:attr`` target does not import (or is
    not callable) produces a script on PATH that dies with a traceback on every
    invocation.  That is the same defect class as a name that is not installed
    at all, so it belongs on the same fence.
    """
    import importlib

    table = _pyproject()["project"].get("scripts", {})
    assert table, "pyproject declares no console scripts"
    broken = []
    for script_name, target in sorted(table.items()):
        mod_name, _, attr = str(target).partition(":")
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:
            broken.append(f"{script_name} -> {target}: module import failed ({exc!r})")
            continue
        fn = getattr(mod, attr, None)
        if not callable(fn):
            broken.append(f"{script_name} -> {target}: {attr!r} is not callable")
    assert not broken, "console scripts point at targets that do not work:\n  " + \
        "\n  ".join(broken)


def test_both_documented_program_names_are_declared():
    """The distribution name and the legacy capture-engine name BOTH work.

    ``pip install systemu`` must not install a command called only
    ``sharing_on`` (nothing in the install line hints at that name), and the
    alias must not be introduced by retiring the name every existing install,
    script and habit already uses.
    """
    scripts = _console_scripts()
    dist = _pyproject()["project"]["name"]
    assert dist in scripts, (
        f"the distribution is called {dist!r} but `pip install {dist}` installs "
        f"no command of that name -- only {sorted(scripts)}"
    )
    assert "sharing_on" in scripts, (
        "the `sharing_on` console script was removed; every existing install, "
        "doc, shell script and habit depends on it"
    )
    table = _pyproject()["project"]["scripts"]
    assert table[dist] == table["sharing_on"], (
        f"{dist!r} and 'sharing_on' must be the SAME entry point -- an alias "
        f"that diverges is two programs with one manual: "
        f"{table[dist]!r} vs {table['sharing_on']!r}"
    )


# --------------------------------------------------------------------------- #
# the scanner must be able to SEE the defect (a fence that cannot fail is not
# a fence -- these are the self-tests for the detector itself)
# --------------------------------------------------------------------------- #

def test_scanner_rejects_a_program_the_wheel_does_not_install():
    """The detector's program-name arm still bites.

    HISTORY, because the literal moved.  This self-test used to feed the
    scanner ``systemu doctor --set-passphrase`` -- F6's original defect, where
    ``systemu`` was only the DISTRIBUTION name.  v0.10.24 closed that defect
    from the other side: the wheel now installs a ``systemu`` console script,
    so that exact string is genuinely invocable and MUST be accepted (see
    ``test_the_former_defect_string_is_now_genuinely_invocable``).  The
    detector arm it exercised is unchanged, so it is exercised here with a
    program that really is not installed.
    """
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    # A punctuation variant of a real script name: pip creates NO such alias
    # (`_console_scripts` says so), but `_program_lookalikes` deliberately
    # treats it as a program token precisely so this case is caught.
    text = "Set SYSTEMU_DASHBOARD_PASSPHRASE_HASH or run `sharing-on doctor --set-passphrase`."
    found = [_resolve(t, scripts, root) for t in _invocations(text, progs, root)]
    assert found and found[0] and "not an installed console script" in found[0], found


def test_the_former_defect_string_is_now_genuinely_invocable():
    """``systemu doctor --set-passphrase`` -- the exact string F6 was born from.

    It is correct now, and it is correct for the load-bearing reason: the name
    is in ``[project.scripts]``, not because the fence stopped looking.
    """
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    assert "systemu" in scripts
    text = "run `systemu doctor --set-passphrase`"
    found = [_resolve(t, scripts, root) for t in _invocations(text, progs, root)]
    assert found == [None], found


def test_scanner_rejects_a_nonexistent_subcommand():
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    text = "`sharing_on tools review tool_a`"
    found = [_resolve(t, scripts, root) for t in _invocations(text, progs, root)]
    assert found and found[0] and "no subcommand 'review'" in found[0], found


def test_scanner_rejects_a_nonexistent_option():
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    text = "`sharing_on doctor --set-passphrasee`"
    found = [_resolve(t, scripts, root) for t in _invocations(text, progs, root)]
    assert found and found[0] and "no option" in found[0], found


def test_scanner_accepts_the_corrected_form():
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    text = "run `sharing_on doctor --set-passphrase` first"
    found = [_resolve(t, scripts, root) for t in _invocations(text, progs, root)]
    assert found == [None], found


def test_scanner_ignores_prose_about_the_product():
    """The product name in a sentence is not an instruction."""
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    for prose in (
        "3 systemu daemon processes are running.",
        "systemu learned it from an answer you gave",
        "Another systemu daemon (or another app) is bound to this port.",
    ):
        found = [_resolve(t, scripts, root) for t in _invocations(prose, progs, root)]
        assert found == [], (prose, found)


# --------------------------------------------------------------------------- #
# F23 -- the docs teach `systemu`, the runtime taught `sharing_on`
#
# F6 policed INVOCABILITY: does the command named in this message exist. That
# property was met and stayed met -- both names are permanent console scripts
# pointing at one entry point, so nothing here was ever FALSE.
#
# What was broken is COHERENCE. A user types `pip install systemu`, follows a
# README that says `systemu`, and is then told by the running program to go run
# a differently-named one, with nothing anywhere explaining that they are the
# same thing:
#
#     "Next: `sharing_on chat submit \"...\"` -- systemu now knows you."
#     "Use sharing_on daemon status to check."
#     hint: run `sharing_on user init --non-interactive --name <NAME>`
#     cta:  "sharing_on daemon stop --all"
#
# So the property below is narrower than F6's and sits on top of it: of the two
# invocable names, an operator INSTRUCTION must lead with the DOCUMENTED one.
#
# SCOPE -- this is the part that must not become a sed. In scope is only what
# the program can PRINT: runtime messages, CTAs, hints, remedies, and the
# docstrings of click-decorated callables (click prints those verbatim as
# --help). Out of scope, deliberately:
#   * module docstrings and ordinary function docstrings -- developer prose;
#   * `sharing_on` as a PACKAGE or MODULE PATH (`sharing_on.config`), a log
#     channel, or the name of the capture engine as a thing rather than as a
#     command -- the detector requires a real top-level command to follow, so
#     none of those match;
#   * `python -m sharing_on ...`, which is a real and sometimes REQUIRED
#     invocation form. `python -m systemu` does NOT work (there is no
#     systemu/__main__.py), so rewriting it would manufacture exactly the false
#     instruction F6 was created to delete. Pinned by running it, below.
# --------------------------------------------------------------------------- #

#: Deliberate alias mentions: repo-relative path -> {span: why}. A span listed
#: here is exempt from the lead-with-the-documented-name rule. Kept explicit
#: and tiny, each with a stated reason: an allowlist that grows without reasons
#: is the rule being quietly repealed.
#:
#: NOTE the place that must state the two names are one program does NOT appear
#: here -- the root ``--help`` says them as BARE NAMES, not as invocations, so
#: it never trips the detector.
_ALIAS_OK: dict = {
    "systemu/interface/cli_commands.py": {
        "sharing_on user init`":
            "a QUOTATION of the message this command used to emit (the F3 "
            "history note). Rewriting a quotation turns it into a misquotation, "
            "and the sentence is about what the program said in the past, not "
            "about what the operator should type now.",
    },
}


def _documented_name() -> str:
    """The name the docs and the install line lead with: the distribution."""
    return _pyproject()["project"]["name"]


def _instruction_pattern(root):
    """`sharing_on <real top-level command>`, not the ``python -m`` form."""
    alt = "|".join(re.escape(c) for c in sorted(root.commands))
    return re.compile(
        r"(?<![\w./-])(?<!-m )(sharing[_-]on)(?=[ \t]+(?:" + alt + r")\b)")


def _printable_strings(path: Path):
    """Yield ``(lineno, text)`` for every string the PROGRAM can show a user.

    Every string constant qualifies except a docstring that click does not
    print -- i.e. module docstrings and the docstrings of undecorated helpers.
    A click-decorated callable's docstring IS its ``--help`` body, so it is very
    much operator-facing and stays in scope.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
                if "click" not in decorators:
                    skip.add(id(body[0].value))
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(getattr(tree.body[0], "value", None), ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        skip.add(id(tree.body[0].value))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip):
            yield node.lineno, node.value


def test_operator_facing_instructions_lead_with_the_documented_name():
    """THE F23 FENCE.

    Both names run; only one of them is the name the operator typed into `pip
    install` and read in the README. An instruction that names the other one
    sends a first-time user looking for a second program.
    """
    documented = _documented_name()
    scripts = _console_scripts()
    root = _root_group()
    assert documented in scripts, (
        f"{documented!r} is the documented name but is not a console script -- "
        f"leading with it would be a false instruction, not a nicer one")
    rx = _instruction_pattern(root)

    findings = []
    scanned = 0
    for pkg in _SCANNED_PACKAGES:
        for path in sorted((REPO_ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            try:
                strings = list(_printable_strings(path))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
                findings.append(f"{rel}: unparseable ({exc})")
                continue
            scanned += 1
            for lineno, text in strings:
                for m in rx.finditer(text):
                    span = text[m.start():m.start() + 60].splitlines()[0]
                    if span in _ALIAS_OK.get(rel, ()):
                        continue
                    findings.append(
                        f"{rel}:{lineno}: {span!r} -- an operator instruction "
                        f"leading with the alias; the user installed "
                        f"{documented!r} and the docs say {documented!r}")

    assert scanned > 100, f"scanner only saw {scanned} files -- it is not looking"
    assert not findings, (
        f"operator-facing instructions tell the user to run a differently-named "
        f"program than the one they installed:\n  " + "\n  ".join(sorted(set(findings))))
    # every exemption must still describe a span that EXISTS -- an allowlist
    # entry for a string nobody writes any more is dead permission.
    for rel, spans in _ALIAS_OK.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for span, reason in spans.items():
            assert span in text, f"_ALIAS_OK[{rel!r}] exempts a span that is gone: {span!r}"
            assert reason.strip(), f"_ALIAS_OK[{rel!r}][{span!r}] has no reason"


def test_the_alias_still_works_everywhere_the_documented_name_does():
    """F23 renames INSTRUCTIONS, never capability. `sharing_on` is a permanent
    equal alias -- if leading with `systemu` ever costs the old name its
    behaviour, this stops being a wording change and becomes a breaking one."""
    table = _pyproject()["project"]["scripts"]
    assert table["sharing_on"] == table[_documented_name()]
    r = subprocess.run(
        [sys.executable, "-m", "sharing_on", "--version"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="backslashreplace", timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_help_says_plainly_that_the_two_names_are_one_program():
    """The one thing a rename cannot do on its own: explain itself.

    An operator with muscle memory for `sharing_on`, or an old script, or a
    2026 blog post, needs to be told once -- in the program's own front door --
    that the name they know is not being taken away. Asserted on the REAL
    rendered --help, not on the source string.
    """
    r = subprocess.run(
        [sys.executable, "-m", "sharing_on", "--help"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="backslashreplace", timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = " ".join(r.stdout.split())
    assert "sharing_on" in out and _documented_name() in out, out
    assert "same" in out.lower(), (
        "the --help never states the two names are the same program", out)


def test_every_python_dash_m_instruction_actually_runs():
    """DEC-34: do not assert the decision, execute it.

    `python -m sharing_on ...` is kept verbatim by F23 because it is a real and
    occasionally REQUIRED form (the strict-interpreter remediation depends on
    picking the interpreter explicitly, which a console script cannot do). The
    reason it is not rewritten to `python -m systemu` is that the latter does
    not exist -- so run every module the messages name and prove it.
    """
    rx = re.compile(r"python -m ([A-Za-z_][A-Za-z0-9_]*)(?=[ \t`'\"\\]|$)")
    modules = set()
    for pkg in _SCANNED_PACKAGES:
        for path in sorted((REPO_ROOT / pkg).rglob("*.py")):
            for _lineno, text in _printable_strings(path):
                modules.update(rx.findall(text))
    modules.discard("playwright")     # third-party, installed only with [browser]
    assert modules, "no `python -m <module>` instruction found at all -- detector broke"
    for mod in sorted(modules):
        r = subprocess.run(
            [sys.executable, "-m", mod, "--help"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="backslashreplace", timeout=300,
        )
        assert r.returncode == 0, (
            f"messages tell operators to run `python -m {mod}`, which fails:\n"
            + r.stdout + r.stderr)


def test_the_f23_detector_bites():
    """A fence that cannot fail is not a fence -- exercise the detector arm."""
    rx = _instruction_pattern(_root_group())
    assert rx.search('Next: `sharing_on chat submit "..."` — systemu now knows you.')
    assert rx.search("Use sharing_on daemon status to check.")
    assert rx.search('cta="sharing_on daemon stop --all"')


def test_the_f23_detector_leaves_non_instructions_alone():
    """The judgement calls, as executable assertions rather than a claim."""
    rx = _instruction_pattern(_root_group())
    for benign in (
        "from sharing_on.config import Config",          # module path
        "logging.getLogger('sharing_on.analyzer')",      # log channel
        "the sharing_on engine records the screen",      # the engine, named
        "`python -m sharing_on daemon stop`",            # a REAL invocation form
        "pip install sharing_on",                        # not a subcommand
        "sharing_on, version 0.10.24",                   # --version output
    ):
        assert not rx.search(benign), benign


def test_the_f23_detector_over_flags_prose_that_collides_with_a_command_name():
    """A KNOWN and DELIBERATE false positive, recorded rather than hidden.

    Several top-level commands are ordinary English words (``capture``,
    ``record``, ``world``, ``info``), so "the sharing_on capture engine" is
    indistinguishable by shape from "run sharing_on capture". The detector
    errs toward flagging, because the cost of a false flag is one ``_ALIAS_OK``
    entry with a reason, and the cost of a miss is the defect shipping again.
    This test exists so a future reader meets the trade-off as a fact instead
    of discovering it as a surprise.
    """
    rx = _instruction_pattern(_root_group())
    assert rx.search("the sharing_on capture engine records the screen")


def test_the_alias_exemption_mechanism_actually_exempts():
    """`_ALIAS_OK` is empty today. An unexercised escape hatch is one nobody can
    trust the first time they need it, so prove it works on a synthetic entry."""
    rx = _instruction_pattern(_root_group())
    text = "Old scripts calling `sharing_on daemon start` keep working."
    m = rx.search(text)
    assert m, "detector precondition"
    span = text[m.start():m.start() + 60].splitlines()[0]
    table = {"some/file.py": (span,)}
    assert span in table.get("some/file.py", ())
    assert span not in table.get("other/file.py", ())


# --------------------------------------------------------------------------- #
# REAL runtime surfaces (DEC-44: the conftest survey stub proves nothing here)
# --------------------------------------------------------------------------- #

def _assert_reason_names_an_invocable_command(blob: str) -> None:
    """Every command-shaped span in ``blob`` resolves against the LIVE tree.

    Shared by the runtime-surface pins so they assert the PROPERTY (the remedy
    can be typed) rather than one accepted spelling of it.
    """
    scripts, progs, root = _console_scripts(), _program_lookalikes(), _root_group()
    tokenised = list(_invocations(blob, progs, root))
    assert tokenised, f"no command-shaped span found in the message at all:\n{blob}"
    bad = [(" ".join(t), _resolve(t, scripts, root)) for t in tokenised]
    bad = [(c, r) for c, r in bad if r]
    assert not bad, f"{bad}\n--- message ---\n{blob}"


def test_exposure_check_refusal_names_the_real_command():
    """The actual message a refused non-loopback dashboard start emits."""
    from systemu.runtime import dashboard_auth as da
    verdict = da.exposure_check("0.0.0.0", configured=False)
    assert verdict.may_start is False
    assert "doctor --set-passphrase" in verdict.reason, verdict.reason
    # The pin is INVOCABILITY, not a specific spelling. It used to be
    # `"systemu doctor" not in reason` -- correct while `systemu` was only the
    # distribution name, but a trap now that the wheel installs both scripts:
    # it would have failed a message that switched to the equally-real
    # `systemu doctor …`. Resolve the string the code actually emits.
    _assert_reason_names_an_invocable_command(verdict.reason)


def test_corrupt_passphrase_file_error_names_the_real_command(tmp_path, caplog):
    """The actual message ``is_configured_vault`` logs on a corrupt auth file."""
    import logging
    from systemu.runtime import dashboard_auth as da
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "dashboard_auth.json").write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="systemu.runtime.dashboard_auth"):
        assert da.is_configured_vault(tmp_path) is True      # fail-closed, unchanged
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "doctor --set-passphrase" in blob, blob
    _assert_reason_names_an_invocable_command(blob)


def test_the_named_command_really_is_invocable():
    """DEC-34: do not merely assert the string changed -- run it."""
    r = subprocess.run(
        [sys.executable, "-m", "sharing_on", "doctor", "--help"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="backslashreplace", timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--set-passphrase" in r.stdout, r.stdout
