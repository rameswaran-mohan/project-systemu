"""P21 -- every systemu CLI command declares who may run it.

``sharing_on.cli_access`` is the registry the operator-command fence
(``systemu/runtime/operator_cli_fence.py``) reads: each click command and group
carries ``@operator_only`` or ``@agent_safe``. A command added without one fails
here -- the fence would refuse it fail-closed, which is safe but would hide the
omission.
"""
from __future__ import annotations

import click
import pytest


def _walk(command, path=()):
    yield path, command
    if isinstance(command, click.Group):
        for name, sub in sorted(command.commands.items()):
            yield from _walk(sub, path + (name,))


def test_every_cli_command_carries_an_access_classification():
    """A command added without @operator_only / @agent_safe fails HERE, not in
    production: the fence would refuse it fail-closed, which is safe but would
    hide the omission."""
    from sharing_on.cli import cli
    from sharing_on.cli_access import access_of, operator_options_of

    unclassified, bad_options = [], []
    count = 0
    for path, command in _walk(cli):
        count += 1
        if access_of(command) is None:
            unclassified.append(" ".join(("systemu",) + path))
        params = {p.name for p in command.params if isinstance(p, click.Option)}
        for name in operator_options_of(command):
            if name not in params:
                bad_options.append((path, name))
    assert count > 90, "the walk saw only {} commands -- is a group missing?".format(count)
    assert not unclassified, "unclassified CLI commands: {}".format(unclassified)
    assert not bad_options, "operator_options naming no param: {}".format(bad_options)


def test_every_click_command_defined_in_the_cli_modules_is_classified():
    """Also the ones not (yet) registered under the root, e.g. cli_commands.doctor_cmd."""
    import sharing_on.cli as so_cli
    import systemu.interface.cli_commands as cc
    from sharing_on.cli_access import access_of

    missing = []
    for module in (so_cli, cc):
        for attr, value in vars(module).items():
            if isinstance(value, click.Command) and access_of(value) is None:
                missing.append("{}.{}".format(module.__name__, attr))
    assert not missing, missing


def test_the_decorators_refuse_misuse():
    from sharing_on.cli_access import agent_safe, operator_only

    def plain():
        pass

    with pytest.raises(TypeError):
        operator_only(plain)          # BELOW the click decorator: no command yet
    cmd = operator_only(click.command("x")(plain))
    with pytest.raises(TypeError):
        agent_safe(cmd)               # classified twice


def test_an_unclassified_command_is_never_agent_safe():
    """Fail-closed at the registry: no attribute reads as UNCLASSIFIED, and a
    forged attribute of the wrong type is not a classification (DEC-36)."""
    from sharing_on.cli_access import ACCESS_ATTR, access_of

    cmd = click.command("y")(lambda: None)
    assert access_of(cmd) is None

    class _Sneaky(str):
        def __eq__(self, other):
            return True
    setattr(cmd, ACCESS_ATTR, _Sneaky("agent"))
    assert access_of(cmd) is None
