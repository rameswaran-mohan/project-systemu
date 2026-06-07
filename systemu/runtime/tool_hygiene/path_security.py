"""Safe filesystem path resolution. Rejects traversal + escapes."""
from __future__ import annotations

from pathlib import Path
from typing import Union


class PathSecurityError(ValueError):
    """Raised when a path attempts to escape its sandbox root."""


def safe_resolve(path: Union[str, Path], *, root: Union[str, Path]) -> Path:
    """Resolve ``path`` against ``root``, refusing any result that escapes root.

    Rejects:
    - Parent traversal (``../``) when it lands outside root
    - Absolute paths outside root (e.g. ``/etc/passwd``)
    - Symlink escapes (path resolves to something outside root)
    """
    root_p = Path(root).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root_p / candidate).resolve()
    try:
        resolved.relative_to(root_p)
    except ValueError:
        raise PathSecurityError(
            f"path {path!r} escapes root {root!r} (resolved to {resolved})"
        )
    return resolved
