"""Who may run each systemu CLI command: the operator only, or the agent too.

Every click command and group under the ``systemu`` / ``sharing_on`` program
carries ONE explicit classification, stamped by a decorator that sits ABOVE the
click decorator::

    @operator_only
    @census_group.command(name="grant")
    def census_grant_cmd(...): ...

    @agent_safe
    @census_group.command(name="status")
    def census_status_cmd(): ...

* ``operator_only`` -- the command changes consent, grants, posture, credentials
  or approvals, resolves a decision, or otherwise acts with the operator's
  authority (it writes, spends, spawns or starts something). The agent's shell
  never runs it: ``systemu.runtime.operator_cli_fence`` refuses any agent
  command that would, and tells the operator to run it in their own terminal.
* ``agent_safe`` -- the command only reads (status, list, show, info). A group
  is classified by what invoking it BARE does (normally: print help).
  ``operator_options`` names the params that turn an otherwise read-only
  command into an operator one (``doctor --set-passphrase``): the fence treats
  the command as operator-only whenever one of them appears.

The classification is this registry and nothing else -- the fence keeps no list
of verbs of its own -- and ``tests/test_p21_operator_cli_fence.py`` fails when a
command is added without one. This module imports nothing from systemu, so the
CLI modules can import it before anything else loads.
"""

from __future__ import annotations

from typing import Optional, Tuple

import click

#: The attribute the classification is stored under, on the click object itself.
ACCESS_ATTR = "systemu_cli_access"
#: The attribute naming the params that escalate an agent-safe command.
OPERATOR_OPTIONS_ATTR = "systemu_cli_operator_options"

OPERATOR = "operator"
AGENT = "agent"


def _stamp(command, access: str, operator_options: Tuple[str, ...]):
    if not isinstance(command, click.Command):
        raise TypeError(
            "systemu CLI access decorators go ABOVE the click decorator, so they "
            "receive the click command; got {!r}".format(type(command).__name__))
    if getattr(command, ACCESS_ATTR, None) is not None:
        raise TypeError("command {!r} is classified twice".format(command.name))
    for name in operator_options:
        if type(name) is not str or not name:
            raise TypeError("operator_options must be param names (str)")
    setattr(command, ACCESS_ATTR, access)
    setattr(command, OPERATOR_OPTIONS_ATTR, tuple(operator_options))
    return command


def operator_only(command):
    """Only the operator, in their own terminal, may run this command."""
    return _stamp(command, OPERATOR, ())


def agent_safe(command=None, *, operator_options=()):
    """The agent may run this command: it only reads.

    ``operator_options`` -- click param NAMES (``"apply_mode"``) whose presence
    makes the call operator-only. Usable bare (``@agent_safe``) or called
    (``@agent_safe(operator_options=("clear",))``).
    """
    options = tuple(operator_options)

    def apply(cmd):
        return _stamp(cmd, AGENT, options)

    if command is not None:
        return apply(command)
    return apply


def access_of(command) -> Optional[str]:
    """``OPERATOR`` / ``AGENT``, or None when the command is unclassified.

    The stored value's concrete type is pinned before it is compared (DEC-36):
    anything but an exact ``str`` holding one of the two values reads as
    unclassified, which every caller treats as operator-only.
    """
    value = getattr(command, ACCESS_ATTR, None)
    if type(value) is str and (value == OPERATOR or value == AGENT):
        return value
    return None


def operator_options_of(command) -> Tuple[str, ...]:
    """The param names that escalate ``command`` (empty when none, or malformed)."""
    value = getattr(command, OPERATOR_OPTIONS_ATTR, None)
    if type(value) is not tuple:
        return ()
    return tuple(v for v in value if type(v) is str)
