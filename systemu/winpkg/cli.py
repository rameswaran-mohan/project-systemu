"""E2 — the small CLI the installer shells out to (SPEC §14 E2).

Invoked by systemu.iss during install. Kept deliberately tiny and stdlib-only
at the entry point, because it runs inside the freshly-created embedded env
before anything else has been proven to work.

    python -m systemu.winpkg.cli stamp-installed --root "%LOCALAPPDATA%\\systemu"
    python -m systemu.winpkg.cli stamp-first-task --root ...
    python -m systemu.winpkg.cli report --root ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .layout import literal_layout, resolve_layout
from .metrics import FirstRunMetrics


def _layout_for(root: Optional[str]):
    """Resolve the layout, honouring an explicit --root from the installer.

    Inno passes ``{app}``, which is ALREADY ``%LOCALAPPDATA%\\systemu`` — the
    install root itself, not its parent. Feeding that to :func:`resolve_layout`
    would append a second ``systemu`` component, so an explicit --root is always
    taken literally. Guessing here would put the marker file somewhere the
    uninstaller never looks.
    """
    if root:
        return _literal_layout(Path(root))
    return resolve_layout()


def _literal_layout(base: Path):
    """Thin alias for :func:`systemu.winpkg.layout.literal_layout`.

    The construction used to be inlined here AND in the launcher; one shared
    constructor means a new :class:`InstallLayout` field cannot land in one copy
    only.
    """
    return literal_layout(base)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="systemu.winpkg.cli")
    parser.add_argument("command",
                        choices=("stamp-installed", "stamp-first-task", "report"))
    parser.add_argument("--root", default=None,
                        help="install root (Inno passes {app})")
    parser.add_argument("--version", default="unknown")
    args = parser.parse_args(argv)

    layout = _layout_for(args.root)
    metrics = FirstRunMetrics(layout.marker_file)

    if args.command == "stamp-installed":
        metrics.stamp_installed(version=args.version)
        return 0

    if args.command == "stamp-first-task":
        metrics.stamp_first_task_completed()
        summary = metrics.human_summary()
        if summary:
            print(summary)
        return 0

    summary = metrics.human_summary()
    print(summary or "time-to-first-completed-task: not yet known")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
