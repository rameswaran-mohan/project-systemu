"""A broken command group is NAMED on stderr -- never silently dropped.

THE DEFECT
    Every systemu command group -- `roots`, `census`, `debug`, `daemon`,
    `start`, and the rest -- was registered inside ONE
    `try: from systemu.interface.cli_commands import (...) except ImportError:
    pass`. A single broken import anywhere in that module's transitive closure
    therefore removed THE WHOLE SYSTEMU COMMAND SET, with no message at all:
    the operator types `systemu roots list` and reads "No such command", as if
    the feature had never existed. The one branch that knew the truth threw it
    away.

THE PROPERTY (DEC-32: a fence is a VALUE, not silence)
    Registration is per group. When a group cannot be loaded:
      * the OTHER groups are still registered -- one failure is one failure;
      * exactly one line goes to stderr, naming the group and the error:
            systemu: command group '<name>' unavailable: <error>
      * a placeholder command of that name IS registered, and invoking it
        prints the same line and exits 1 -- so the failure is reachable from
        the operator's own command line, not only from the startup banner they
        may have scrolled past.

    A healthy run says nothing and registers the real commands.

Nothing here starts a daemon, opens a vault, or touches the network.
"""
from __future__ import annotations

import io
import sys

import click
import pytest
from click.testing import CliRunner

import sharing_on.cli as cli_mod


SYSTEMU_MODULE = "systemu.interface.cli_commands"


def _fresh_root() -> click.Group:
    """An empty group standing in for the root CLI, so a test never mutates the
    process-wide `cli` object other tests invoke."""
    @click.group()
    def root():
        """test root"""
    return root


def _register(root, **kw):
    """Call the product's own registration entry point."""
    return cli_mod.register_systemu_groups(root, **kw)


def _names():
    return [name for name, _attr in cli_mod._SYSTEMU_COMMAND_GROUPS]


# -- the healthy path -------------------------------------------------------

def test_a_healthy_registration_installs_every_group_and_says_nothing():
    """The baseline. If this test can pass while the module is broken, every
    other test here is measuring nothing."""
    root = _fresh_root()
    err = io.StringIO()

    failed = _register(root, stream=err)

    assert failed == [], failed
    assert err.getvalue() == "", err.getvalue()
    for name in _names():
        assert name in root.commands, name
    # The real objects, not placeholders: `daemon` is a group with subcommands.
    assert isinstance(root.commands["daemon"], click.Group)
    assert "status" in root.commands["daemon"].commands


def test_the_group_table_covers_the_commands_the_product_advertises():
    """A guard on the table itself: a group dropped from it would vanish from
    the CLI exactly as silently as the defect being closed."""
    names = _names()
    for expected in ("start", "daemon", "roots", "chat", "debug", "scrolls",
                     "army", "tools", "skills", "settings", "evolve",
                     "decisions", "user", "onboarding", "session",
                     "capability", "skill"):
        assert expected in names, expected
    assert len(names) == len(set(names)), names


# -- one broken group does not take the others with it ----------------------

def test_one_broken_group_leaves_every_other_group_real(monkeypatch):
    """The heart of the defect. The import seam is failed for exactly ONE
    group; every other group must still be the genuine command object."""
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "chat_group":
            raise ImportError("no module named 'chatty'")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    root = _fresh_root()
    err = io.StringIO()

    failed = _register(root, stream=err)

    assert failed == ["chat"], failed
    # Every other group is the REAL object, straight from the module.
    import importlib
    module = importlib.import_module(SYSTEMU_MODULE)
    assert root.commands["daemon"] is module.daemon_group
    assert root.commands["roots"] is module.roots_group
    assert root.commands["start"] is module.start_cmd
    # ...and `chat` is present, so the operator gets the reason, not "No such
    # command".
    assert "chat" in root.commands


def test_one_broken_group_prints_exactly_one_stderr_line_naming_it(monkeypatch):
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "chat_group":
            raise ImportError("no module named 'chatty'")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    err = io.StringIO()

    _register(_fresh_root(), stream=err)

    lines = [l for l in err.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1, lines
    assert lines[0] == (
        "systemu: command group 'chat' unavailable: no module named 'chatty'"
    ), lines[0]


def test_the_placeholder_exits_1_with_the_same_line(monkeypatch):
    """The startup line can be scrolled past; the command cannot. Invoking the
    broken group must fail LOUDLY and say the same thing."""
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "chat_group":
            raise ImportError("no module named 'chatty'")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    root = _fresh_root()
    _register(root, stream=io.StringIO())

    res = CliRunner().invoke(root, ["chat"])

    assert res.exit_code == 1, (res.exit_code, res.output)
    assert "systemu: command group 'chat' unavailable" in res.stderr, res.stderr
    assert "no module named 'chatty'" in res.stderr, res.stderr


def test_the_placeholder_accepts_the_arguments_the_operator_actually_typed(
        monkeypatch):
    """`systemu chat send hi --now` must reach the placeholder's message, not
    die on "no such option" -- an operator does not type a bare group name."""
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "chat_group":
            raise ImportError("boom")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    root = _fresh_root()
    _register(root, stream=io.StringIO())

    res = CliRunner().invoke(root, ["chat", "send", "hi", "--now"])

    assert res.exit_code == 1, (res.exit_code, res.output)
    assert "systemu: command group 'chat' unavailable" in res.stderr, res.stderr


# -- the real thing: the whole module poisoned, no mock anywhere -------------

def test_a_poisoned_systemu_module_names_every_group_instead_of_vanishing(
        monkeypatch):
    """The defect's own scenario, reproduced with no monkeypatched seam at all:
    `sys.modules[mod] = None` is what a half-initialised package looks like to
    `import`. Before the fix this printed nothing and registered nothing."""
    monkeypatch.setitem(sys.modules, SYSTEMU_MODULE, None)
    root = _fresh_root()
    err = io.StringIO()

    failed = _register(root, stream=err)

    assert failed == _names(), failed
    text = err.getvalue()
    for name in _names():
        assert "systemu: command group '%s' unavailable:" % name in text, (
            name, text)
        assert name in root.commands, name


def test_every_placeholder_from_a_poisoned_module_exits_1(monkeypatch):
    monkeypatch.setitem(sys.modules, SYSTEMU_MODULE, None)
    root = _fresh_root()
    _register(root, stream=io.StringIO())

    runner = CliRunner()
    for name in _names():
        res = runner.invoke(root, [name])
        assert res.exit_code == 1, (name, res.exit_code, res.output)
        assert "systemu: command group '%s' unavailable" % name in res.stderr, (
            name, res.stderr)


def test_a_poisoned_systemu_module_leaves_the_capture_commands_alone(monkeypatch):
    """`record` and friends live in this module, not in systemu. A systemu
    import failure must not cost the operator the capture engine."""
    monkeypatch.setitem(sys.modules, SYSTEMU_MODULE, None)
    root = _fresh_root()
    root.add_command(click.Command("record", callback=lambda: None), name="record")

    _register(root, stream=io.StringIO())

    assert "record" in root.commands
    assert isinstance(root.commands["record"], click.Command)


# -- the line itself --------------------------------------------------------

def test_a_missing_attribute_is_reported_like_a_missing_import(monkeypatch):
    """A group whose module imports but whose symbol is gone disappears exactly
    as silently, so it is fenced the same way."""
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "roots_group":
            raise AttributeError("module has no attribute 'roots_group'")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    root = _fresh_root()
    err = io.StringIO()

    failed = _register(root, stream=err)

    assert failed == ["roots"], failed
    assert "systemu: command group 'roots' unavailable:" in err.getvalue()


def test_the_line_is_ascii_even_when_the_error_is_not(monkeypatch):
    """DEC-32c: a line a cp1252 console cannot encode is a line the operator
    never reads -- and, printed at import time, it would take the whole CLI
    down with a UnicodeEncodeError instead of naming one broken group."""
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "debug_group":
            raise ImportError("café — 中文")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    err = io.StringIO()

    _register(_fresh_root(), stream=err)

    line = err.getvalue()
    line.encode("ascii")
    assert "systemu: command group 'debug' unavailable:" in line, line


def test_a_missing_stream_does_not_break_registration(monkeypatch):
    """pythonw and friends have no stderr. The startup line is lost there, but
    the placeholder -- the fence the operator can actually reach -- is not."""
    real_load = cli_mod._load_group

    def failing_load(module_path, attr):
        if attr == "chat_group":
            raise ImportError("boom")
        return real_load(module_path, attr)

    monkeypatch.setattr(cli_mod, "_load_group", failing_load)
    monkeypatch.setattr(sys, "stderr", None)
    root = _fresh_root()

    failed = _register(root)

    assert failed == ["chat"], failed
    assert "chat" in root.commands


# -- the shipped root CLI still has everything ------------------------------

def test_the_shipped_root_cli_registered_every_group_for_real():
    """The module-level call really ran, on the object the entry point serves."""
    for name in _names():
        assert name in cli_mod.cli.commands, name
    import importlib
    module = importlib.import_module(SYSTEMU_MODULE)
    assert cli_mod.cli.commands["daemon"] is module.daemon_group
    assert cli_mod.cli.commands["start"] is module.start_cmd
