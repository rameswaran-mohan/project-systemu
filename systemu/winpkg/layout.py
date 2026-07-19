"""E2 — the on-disk install layout under ``%LOCALAPPDATA%\\systemu`` (SPEC §14 E2).

The layout is the load-bearing part of AC2/AC3: the vault is a **sibling** of
the environment, never a child of it. That single invariant is what makes
"upgrade swaps the env, never touches the vault" and "uninstall removes the
env, leaves the vault" true *by construction* rather than by careful deletion
logic that one refactor could get wrong.

    %LOCALAPPDATA%\\systemu\\
        env\\            <- isolated embeddable CPython + the systemu wheel  (SWAPPED on upgrade)
        env.new\\        <- staging for an upgrade                            (transient)
        env.old\\        <- previous env, kept until the upgrade commits      (transient)
        vault\\          <- the operator's data                               (NEVER touched)
        wheelhouse\\     <- offline wheels, so a core install needs no network (AC5)
        install.json    <- install marker: version, timestamps
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Directory name constants — referenced by the Inno Setup script, so they are
#: named once here and asserted against the .iss in the tests.
ENV_DIRNAME = "env"
ENV_STAGING_DIRNAME = "env.new"
ENV_PREVIOUS_DIRNAME = "env.old"
VAULT_DIRNAME = "vault"
WHEELHOUSE_DIRNAME = "wheelhouse"
MARKER_FILENAME = "install.json"
UNINSTALL_NOTICE_FILENAME = "WHAT-WAS-LEFT-BEHIND.txt"


@dataclass(frozen=True)
class InstallLayout:
    """Resolved absolute paths for one systemu installation.

    Every field is a real :class:`pathlib.Path`. Note for future maintainers:
    do NOT write ``getattr(layout, "root", None) or layout`` style fallbacks
    against these — ``Path`` itself has a ``.root`` attribute (it is the
    filesystem anchor, ``"\\\\"`` or ``"/"``), so that idiom silently resolves a
    Path to the drive root. That exact bug has been shipped in this repo before.
    """

    root: Path
    env_dir: Path
    env_staging_dir: Path
    env_previous_dir: Path
    vault_dir: Path
    wheelhouse_dir: Path
    marker_file: Path
    uninstall_notice_file: Path
    is_windows_native: bool

    def vault_is_outside_env(self) -> bool:
        """The AC2/AC3 invariant, checkable at runtime.

        True iff the vault cannot be removed as a side effect of removing the
        environment. Asserted directly by the tests and re-asserted by
        :func:`~systemu.winpkg.lifecycle.perform_uninstall` before it deletes
        anything.
        """
        for env_path in (self.env_dir, self.env_staging_dir, self.env_previous_dir):
            try:
                self.vault_dir.relative_to(env_path)
            except ValueError:
                continue
            return False   # vault sits INSIDE an env dir -> deleting the env eats it
        return True


def resolve_layout(
    local_app_data: Optional[os.PathLike | str] = None,
    *,
    environ: Optional[dict] = None,
) -> InstallLayout:
    """Resolve the install layout.

    ``local_app_data`` wins when given (the installer passes ``{localappdata}``
    straight from Inno Setup). Otherwise ``%LOCALAPPDATA%`` is read from
    ``environ`` (defaulting to the real process environment). On a host with
    neither — a POSIX dev box or CI — we fall back to ``~/.systemu`` and report
    ``is_windows_native=False`` rather than pretending, so the caller can say so
    honestly (PAR-1: a missing capability is a visible fact, never a mystery).
    """
    env = os.environ if environ is None else environ

    base: Optional[Path] = None
    if local_app_data is not None:
        base = Path(local_app_data)
        windows_native = True
    else:
        raw = env.get("LOCALAPPDATA")
        if raw:
            base = Path(raw)
            windows_native = True
        else:
            base = Path.home() / ".local" / "share"
            windows_native = False

    root = (base / "systemu") if windows_native else (Path.home() / ".systemu")
    root = root.expanduser()

    return InstallLayout(
        root=root,
        env_dir=root / ENV_DIRNAME,
        env_staging_dir=root / ENV_STAGING_DIRNAME,
        env_previous_dir=root / ENV_PREVIOUS_DIRNAME,
        vault_dir=root / VAULT_DIRNAME,
        wheelhouse_dir=root / WHEELHOUSE_DIRNAME,
        marker_file=root / MARKER_FILENAME,
        uninstall_notice_file=root / UNINSTALL_NOTICE_FILENAME,
        is_windows_native=windows_native,
    )
