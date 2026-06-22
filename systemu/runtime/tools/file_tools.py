"""v0.9.3 file toolset — read_file / write_file / search_files.

First batch of code-registered tools. Registers at module load via
``registry.register(...)`` so the AST-scan discovery picks them up.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from systemu.runtime.tool_registry_v2 import registry
from systemu.runtime.tool_hygiene.path_security import safe_resolve, PathSecurityError


_DEFAULT_OUTPUT_CAP = 100_000

# Handlers accept an optional ``_root`` kwarg so tests can sandbox without
# a real ToolSandbox. In production, the runtime resolves _root from the
# vault/config layer before invocation.


def read_file_handler(*, path: str, _root: Optional[str] = None, **_kw) -> Dict[str, Any]:
    """Read a text file's contents. Path is resolved against ``_root``.

    Returns: {"success": bool, "content": str | None, "error": str | None}
    """
    root = Path(_root) if _root else Path.cwd()
    try:
        resolved = safe_resolve(path, root=root)
    except PathSecurityError as exc:
        return {"success": False, "content": None, "error": str(exc)}
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"success": False, "content": None, "error": f"file not found: {path}"}
    except OSError as exc:
        return {"success": False, "content": None, "error": str(exc)}
    return {"success": True, "content": content, "error": None}


def write_file_handler(
    *, path: str, content: str, _root: Optional[str] = None, **_kw
) -> Dict[str, Any]:
    """Write ``content`` to a file. Creates parent dirs as needed.

    Returns: {"success": bool, "bytes_written": int, "error": str | None}
    """
    root = Path(_root) if _root else Path.cwd()
    try:
        resolved = safe_resolve(path, root=root)
    except PathSecurityError as exc:
        return {"success": False, "bytes_written": 0, "error": str(exc)}
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        n = resolved.write_text(content or "", encoding="utf-8")
    except OSError as exc:
        return {"success": False, "bytes_written": 0, "error": str(exc)}
    # v0.9.33 A1: echo the RESOLVED absolute path. The runtime's artifact
    # collector (collect_artifact_paths) resolves relative param paths against
    # the process CWD, not output_dir — so a write redirected into output_dir
    # would be omitted from files_produced whenever CWD != output_dir (the
    # normal daemon/local-backend case). "path" is in artifacts._PATH_KEYS, so
    # echoing it here registers the deliverable CWD-independently, mirroring how
    # the v1 file_write tool already echoes its absolute path.
    return {"success": True, "bytes_written": int(n),
            "path": str(resolved), "error": None}


def search_files_handler(
    *, pattern: str, root: str = ".",
    _root: Optional[str] = None, **_kw
) -> Dict[str, Any]:
    """Find files matching a glob pattern under ``root``.

    Returns: {"success": bool, "files": List[str], "error": str | None}
    """
    sandbox = Path(_root) if _root else Path.cwd()
    try:
        search_dir = safe_resolve(root, root=sandbox)
    except PathSecurityError as exc:
        return {"success": False, "files": [], "error": str(exc)}
    if not search_dir.is_dir():
        return {"success": False, "files": [], "error": f"not a directory: {root}"}
    matches: List[str] = []
    for p in search_dir.rglob("*"):
        if not p.is_file():
            continue
        if fnmatch.fnmatch(p.name, pattern):
            matches.append(str(p))
    return {"success": True, "files": matches, "error": None}


# ── Schemas ──────────────────────────────────────────────────────────

_READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the file to read."},
    },
    "required": ["path"],
}

_WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to write."},
        "content": {"type": "string", "description": "Text content to write."},
    },
    "required": ["path", "content"],
}

_SEARCH_FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Glob pattern like '*.py'."},
        "root": {"type": "string", "description": "Directory to search.", "default": "."},
    },
    "required": ["pattern"],
}


# ── Module-level registrations (AST-scan discovery picks these up) ──

registry.register(
    name="read_file", toolset="file",
    schema=_READ_FILE_SCHEMA, handler=read_file_handler,
    description="Read a text file's contents.",
    is_action_tool=False,
    max_result_size_chars=_DEFAULT_OUTPUT_CAP,
)

registry.register(
    name="write_file", toolset="file",
    schema=_WRITE_FILE_SCHEMA, handler=write_file_handler,
    description="Write text to a file (creates parent dirs).",
    is_action_tool=True,
    max_result_size_chars=_DEFAULT_OUTPUT_CAP,
)

registry.register(
    name="search_files", toolset="file",
    schema=_SEARCH_FILES_SCHEMA, handler=search_files_handler,
    description="Find files by glob pattern.",
    is_action_tool=False,
    max_result_size_chars=_DEFAULT_OUTPUT_CAP,
)
