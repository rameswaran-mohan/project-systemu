"""vault_root -- THE ONE MINT for "which directory is the operating vault".

WHY THIS EXISTS
    Every process in the system used to answer this question for itself, from
    the same relative string (``systemu/vault``) resolved against whatever cwd
    that process happened to have.  The CLI stood in the operator's directory;
    the daemon child it spawned stood in ``Path(systemu.__file__).parent.parent``
    whenever the operator's directory did not happen to contain a ``.env`` or a
    ``.systemu_mode`` marker.  One string, two processes, two different vaults.

    Observed live (v0.10.23+): `systemu daemon start` from an EMPTY working
    directory wrote its runtime state into the CHECKOUT -- daemon sidecar, first
    gate review marker, package manifest baseline, dashboard session secret,
    capabilities/metrics/table stores, tool bodies -- and ran the effect-tags
    migration IN PLACE over the packaged seed tools (57 tracked files modified).
    On a pip install the same shape mutates site-packages, which the next
    upgrade then destroys.  The daemon's build record landed in one vault while
    the CLI read the other, which is why the same boot also reported an
    "UNVERIFIED build".

THE RULE (one sentence)
    The operating vault root is derived from the OPERATING HOME -- the process's
    own current working directory -- and never from where the package happens to
    be installed.

    Ordered inputs, all resolved to ONE absolute path:
      1. an ``explicit`` value, when a caller already holds a chosen root (this
         is how the parent CLI hands its answer to the spawned child);
      2. ``SYSTEMU_VAULT_DIR``, used verbatim when absolute (Docker mounts) and
         resolved against the operating home when relative;
      3. otherwise ``<home>/systemu/vault``.

THE FENCE (DEC-32: a fence is a VALUE, not a raise)
    :func:`resolve_vault_root` returns a frozen :class:`VaultRootVerdict` whose
    ``refused`` bit crosses the boundary with the path itself.  Callers read the
    bit in the same frame in which they decide to boot; there is no exception
    for an intermediate frame to swallow, and the refusal path (a printed
    message + a nonzero exit / a not-ready verdict) is not reachable from the
    success path.

    The fence refuses a root that lands inside ``Path(systemu.__file__).parent``
    -- UNLESS the package directory is itself inside the operating home.  That
    carve-out is the source checkout: standing in the tree that CONTAINS the
    package makes ``./systemu/vault`` the working vault by construction, and has
    since v0.7.4.  The defect being fenced is falling INTO a package tree the
    operator is not standing in, which is a different fact and is what the
    ``refused`` bit reports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

#: The operating vault, relative to the operating home, when nothing else says.
DEFAULT_RELATIVE_VAULT = "systemu/vault"

#: The one env var that names the operating vault.
VAULT_DIR_ENV = "SYSTEMU_VAULT_DIR"


def package_dir() -> Path:
    """The directory of the ``systemu`` package THIS process imported."""
    import systemu

    return Path(systemu.__file__).resolve().parent


def _norm(path) -> str:
    """One absolute, comparison-ready spelling of a path.

    Deliberately string-level: ``os.path`` comparisons neither dispatch on the
    operand's type nor follow symlinks, so the containment answer below is the
    same one the filesystem will give the writer.
    """
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def _within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lies underneath it."""
    c = os.path.normcase(_norm(child))
    p = os.path.normcase(_norm(parent))
    return c == p or c.startswith(p + os.sep)


@dataclass(frozen=True)
class VaultRootVerdict:
    """The minted answer.  ``refused`` is the fence bit; it travels WITH the
    path so no caller can act on the path without having been handed the
    verdict about it."""

    root: str            # absolute, normalised
    home: str            # the operating home it was derived from
    source: str          # "explicit" | "env" | "default"
    package_dir: str     # the package tree this process imported
    inside_package: bool
    refused: bool
    reason: str          # "" unless refused


def resolve_vault_root(
    *,
    explicit: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> VaultRootVerdict:
    """Mint the operating vault root.  Pure: no directory is created or read."""
    environ = os.environ if env is None else env
    home = _norm(cwd if cwd is not None else os.getcwd())

    raw = "" if explicit is None else str(explicit).strip()
    if raw:
        source = "explicit"
    else:
        raw = (environ.get(VAULT_DIR_ENV) or "").strip()
        source = "env" if raw else "default"
        if not raw:
            raw = DEFAULT_RELATIVE_VAULT

    candidate = os.path.expanduser(raw)
    if not os.path.isabs(candidate):
        candidate = os.path.join(home, candidate)
    root = _norm(candidate)

    pkg = str(package_dir())
    inside_package = _within(root, pkg)
    # The source-checkout carve-out -- see the module docstring.
    home_contains_package = _within(pkg, home)
    refused = inside_package and not home_contains_package

    reason = ""
    if refused:
        reason = (
            "REFUSED: the operating vault root {root} is inside the installed "
            "systemu package {pkg}. Booting here writes runtime state into the "
            "package and migrates the packaged seed catalog in place. The "
            "operating home is {home} (source: {source})."
        ).format(root=root, pkg=pkg, home=home, source=source)

    return VaultRootVerdict(
        root=root,
        home=home,
        source=source,
        package_dir=pkg,
        inside_package=inside_package,
        refused=refused,
        reason=reason,
    )


def refusal_message(verdict: VaultRootVerdict) -> str:
    """The operator-facing text for a refused verdict. ASCII-only (DEC-32c)."""
    return "\n".join([
        "REFUSED: the operating vault would live inside the systemu package.",
        "",
        "  resolved vault root : {}".format(verdict.root),
        "  systemu package dir : {}".format(verdict.package_dir),
        "  operating home      : {}".format(verdict.home),
        "  root chosen from    : {}".format(verdict.source),
        "",
        "Starting here would write daemon state, secrets and capability stores",
        "into the package tree and migrate the packaged seed catalog in place.",
        "On a pip install that mutates site-packages and the next upgrade",
        "destroys it.",
        "",
        "Fix one of:",
        "  cd <a working directory of your own> && systemu init",
        "  {}=<an absolute path outside the package> systemu daemon start".format(
            VAULT_DIR_ENV),
    ])
