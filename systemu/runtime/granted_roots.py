"""G2 — GrantedRoots grant + realpath-confinement store (spec UNIFIED-v2 §5.4 / §13, HIGH-3).

A NET-NEW filesystem confinement primitive. The operator grants directories; every
resolved path the agent touches is checked WITHIN a granted root AFTER
canonicalization — a path outside is rejected even if the request names it
absolutely. The `access` requirement type (§5.3), the Reference Resolver (§5.4),
and the Situational-Inventory root survey (§5.1) all rest on this.

**Canonicalization resolves the FINAL path (IMPL-9)** — `..` / symlink / junction /
reparse chains followed to the end (`os.path.realpath`), case-folded on NTFS
(`os.path.normcase`), and 8.3 short-name aliases (`PROGRA~1`) expanded on the
existing prefix (`GetLongPathNameW` via ctypes) — so a raw-string prefix check is
never the boundary. The boundary test uses `os.path.commonpath`, which respects the
component boundary (`/granted-evil` is NOT within `/granted`).

Persistence is the side-store pattern (atomic write, defensive read) at
`<vault>/granted_roots.json`; a broken/absent file yields no grants, never an
exception.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List


# --------------------------------------------------------------------------- #
# canonicalization (IMPL-9)
# --------------------------------------------------------------------------- #

def _get_long_path_windows(p: str) -> str:
    """Expand 8.3 short names for a path whose components exist (Windows only)."""
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.kernel32.GetLongPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        n = fn(p, buf, len(buf))
        if 0 < n < len(buf) and buf.value:
            return buf.value
    except Exception:
        pass
    return p


def _expand_short_names(p: str) -> str:
    """Expand the longest EXISTING prefix's 8.3 aliases, preserving a non-existent
    tail (`GetLongPathNameW` requires the whole path to exist). Fast-path when the
    path contains no `~` (no 8.3 alias possible)."""
    if "~" not in p:
        return p
    cur = Path(p)
    tail: List[str] = []
    # walk up to the longest existing ancestor
    while not cur.exists() and cur != cur.parent:
        tail.append(cur.name)
        cur = cur.parent
    base = _get_long_path_windows(str(cur)) if cur.exists() else str(cur)
    for name in reversed(tail):
        base = os.path.join(base, name)
    return base


def canonicalize(path: str) -> str:
    """The confinement-safe canonical form of a path: final-path realpath (follows
    symlinks/junctions/`..`), 8.3-expanded on Windows, case-folded/normalized."""
    p = os.path.realpath(os.path.abspath(str(path or "")))
    if os.name == "nt":
        p = _expand_short_names(p)
    return os.path.normcase(p)


def _is_within(canon_candidate: str, canon_root: str) -> bool:
    """True iff the (already-canonical) candidate is the root or lies under it.

    `commonpath` respects the component boundary, so `/granted-evil` is NOT within
    `/granted`; a `ValueError` (different drives / mixed abs-rel) ⇒ not within."""
    if not canon_candidate or not canon_root:
        return False
    try:
        return os.path.commonpath([canon_root, canon_candidate]) == canon_root
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #

class GrantedRootsStore:
    """Persistent set of operator-granted root directories + the confinement check."""

    def __init__(self, base_dir):
        self._base = Path(base_dir)

    @property
    def _file(self) -> Path:
        return self._base / "granted_roots.json"

    # ── read ──────────────────────────────────────────────────────────────
    def list_roots(self) -> List[str]:
        """The canonical granted root paths. Defensive: a broken/absent file ⇒ []."""
        try:
            if not self._file.exists():
                return []
            data = json.loads(self._file.read_text(encoding="utf-8"))
            roots = data.get("roots") if isinstance(data, dict) else None
            return [r for r in (roots or []) if isinstance(r, str)]
        except Exception:
            return []

    def is_granted_root(self, path: str) -> bool:
        return canonicalize(path) in set(self.list_roots())

    def is_within_granted(self, candidate: str) -> bool:
        """True iff `candidate` canonicalizes to a location within some granted root."""
        canon = canonicalize(candidate)
        return any(_is_within(canon, root) for root in self.list_roots())

    # ── write ─────────────────────────────────────────────────────────────
    def _write(self, roots: List[str]) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "roots": sorted(set(roots))}
        fd, tmp = tempfile.mkstemp(dir=str(self._base), prefix="granted_roots.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2))
            os.replace(tmp, str(self._file))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def grant(self, path: str) -> str:
        """Grant a directory. Idempotent; returns the CANONICAL root recorded."""
        canon = canonicalize(path)
        roots = self.list_roots()
        if canon not in roots:
            self._write(roots + [canon])
        return canon

    def revoke(self, path: str) -> bool:
        """Remove a grant. Returns True iff it was present."""
        canon = canonicalize(path)
        roots = self.list_roots()
        if canon in roots:
            self._write([r for r in roots if r != canon])
            return True
        return False
