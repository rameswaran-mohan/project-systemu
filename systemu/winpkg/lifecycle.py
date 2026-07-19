"""E2 — upgrade and uninstall (SPEC §14 E2; AC2, AC3).

Upgrade swaps the environment and never touches the vault. Uninstall removes
the environment, **leaves the vault**, and writes an honest notice saying so
and naming the path.

The safety property is structural (see :mod:`~systemu.winpkg.layout`): the
vault is a sibling of the env, so no env-directory removal can reach it. Both
entry points re-assert that invariant before deleting anything, so a future
layout change that breaks it fails loudly here instead of quietly eating an
operator's data.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .layout import InstallLayout


class UpgradeFailed(Exception):
    """The upgrade did not complete. The previous environment has been restored."""


class UnsafeLayout(Exception):
    """The layout would let an env removal reach the vault. Nothing was deleted."""


@dataclass(frozen=True)
class UninstallReport:
    removed: tuple
    vault_kept: Optional[Path]
    notice_file: Optional[Path]


def vault_fingerprint(vault_dir: Path) -> dict:
    """A content fingerprint of every file under ``vault_dir``.

    Used by the AC2 test to prove the vault is byte-for-byte identical across
    an upgrade. Returns ``{relative_posix_path: sha256}``.
    """
    out: dict = {}
    if not vault_dir.exists():
        return out
    for path in sorted(vault_dir.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out[path.relative_to(vault_dir).as_posix()] = digest
    return out


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def perform_upgrade(
    layout: InstallLayout,
    install_new_env: Callable[[Path], None],
) -> None:
    """Swap in a new environment, rolling back on any failure.

    ``install_new_env`` receives the staging directory and must populate it
    (that is where the wheelhouse install happens). It is injected so this
    function is testable without running pip — and so a failure can be
    provoked exactly where a real one would occur.

    The vault is never an operand of this function. That is the point.
    """
    if not layout.vault_is_outside_env():
        raise UnsafeLayout(
            "the vault resolves inside an environment directory; refusing to "
            "swap the environment because doing so could destroy operator data"
        )

    staging = layout.env_staging_dir
    previous = layout.env_previous_dir

    _rmtree(staging)
    _rmtree(previous)
    staging.mkdir(parents=True, exist_ok=True)

    # 1. Build the new env. A failure here has touched nothing live.
    try:
        install_new_env(staging)
    except Exception as exc:                     # noqa: BLE001
        _rmtree(staging)
        raise UpgradeFailed(f"the new environment could not be built: {exc}") from exc

    # 2. Swap. Each step is individually reversible.
    had_previous_env = layout.env_dir.exists()
    try:
        if had_previous_env:
            layout.env_dir.rename(previous)
        staging.rename(layout.env_dir)
    except Exception as exc:                     # noqa: BLE001
        # Restore whatever we moved.
        if had_previous_env and previous.exists() and not layout.env_dir.exists():
            previous.rename(layout.env_dir)
        _rmtree(staging)
        raise UpgradeFailed(f"the environment swap failed: {exc}") from exc

    # 3. Commit — drop the old env only once the new one is in place.
    _rmtree(previous)


def perform_uninstall(
    layout: InstallLayout,
    *,
    write_notice: bool = True,
) -> UninstallReport:
    """Remove the environment; keep the vault; say so in writing (AC3)."""
    if not layout.vault_is_outside_env():
        raise UnsafeLayout(
            "the vault resolves inside an environment directory; refusing to "
            "uninstall because removing the environment would delete it"
        )

    removed = []
    for path in (layout.env_dir, layout.env_staging_dir, layout.env_previous_dir,
                 layout.wheelhouse_dir):
        if path.exists():
            shutil.rmtree(path)
            removed.append(path)

    vault_kept = layout.vault_dir if layout.vault_dir.exists() else None

    notice_file: Optional[Path] = None
    if write_notice and vault_kept is not None:
        notice_file = layout.uninstall_notice_file
        notice_file.parent.mkdir(parents=True, exist_ok=True)
        notice_file.write_text(_notice_text(vault_kept), encoding="utf-8")

    return UninstallReport(
        removed=tuple(removed),
        vault_kept=vault_kept,
        notice_file=notice_file,
    )


def _notice_text(vault_dir: Path) -> str:
    """The honest uninstall notice. Names the path, and does not pretend the
    uninstall was total."""
    return (
        "systemu has been uninstalled.\n"
        "\n"
        "Your vault was NOT deleted. It is still here:\n"
        f"\n    {vault_dir}\n"
        "\n"
        "It holds your tasks, credentials registry, and world model — the things\n"
        "systemu learned while you used it. We leave it because deleting it is\n"
        "not ours to decide, and an uninstaller is a bad place to make that call.\n"
        "\n"
        "If you want it gone, delete that folder yourself. Nothing else remains.\n"
    )
