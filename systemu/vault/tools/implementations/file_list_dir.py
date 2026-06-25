#!/usr/bin/env python3
"""List files in a directory, optionally filtered by a glob pattern."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "file_list_dir",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")
    pattern: str = kwargs.get("pattern", "*")
    recursive: bool = bool(kwargs.get("recursive", False))

    if not path:
        return {"success": False, "files": [], "count": 0, "error": "path is required"}

    if not pattern:
        pattern = "*"

    try:
        p = Path(path).expanduser()

        if not p.exists():
            return {"success": False, "files": [], "count": 0, "error": f"Directory not found: {p}"}

        if not p.is_dir():
            return {"success": False, "files": [], "count": 0, "error": f"Path is not a directory: {p}"}

        if recursive:
            import os as _os
            rp = str(p.resolve())
            # Refuse a recursive scan rooted at a drive root or the user-home root —
            # it enumerates millions of entries and always hits the sandbox timeout.
            _drive, _tail = _os.path.splitdrive(rp)
            _home = _os.path.normcase(_os.path.expanduser("~"))
            if _tail in ("", _os.sep) or _os.path.normcase(rp) == _home:
                return {"success": False, "files": [], "count": 0,
                        "error": ("Recursive scan of a drive/home root is refused (too "
                                  "many files). Give a specific subdirectory.")}
            _MAX_DEPTH, _MAX_RESULTS = 5, 5000
            matches = []
            _base_depth = rp.rstrip(_os.sep).count(_os.sep)
            for _root, _dirs, _files in _os.walk(rp):
                if _root.rstrip(_os.sep).count(_os.sep) - _base_depth >= _MAX_DEPTH:
                    _dirs[:] = []   # prune deeper levels
                    continue
                for _f in _files:
                    fp = Path(_root) / _f
                    if fp.match(pattern):
                        matches.append(fp)
                        if len(matches) >= _MAX_RESULTS:
                            break
                if len(matches) >= _MAX_RESULTS:
                    break
            files = sorted(str(f) for f in matches if f.is_file())
            return {"success": True, "files": files, "count": len(files), "error": None}
        else:
            matches = p.glob(pattern)
            files = sorted(str(f) for f in matches if f.is_file())
            return {"success": True, "files": files, "count": len(files), "error": None}

    except Exception as exc:
        return {"success": False, "files": [], "count": 0, "error": str(exc)}
