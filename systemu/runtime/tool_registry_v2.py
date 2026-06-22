"""v0.9.3 Layer 3 — code-side tool registry (parallel to the v1 vault registry).

Code-side registry: tool modules call ``registry.register(...)`` at module
load. AST-scan discovers modules under ``systemu/runtime/tools/`` that
contain top-level register calls. Coexists with the existing v1 vault-based
registry; tool_sandbox.execute() consults v2 first and falls back to v1.

Design:
- ToolEntry dataclass
- check_fn with TTL cache (default 30s, configurable via Config.check_fn_cache_ttl_seconds)
- AST-scan to find modules with top-level register calls before importing
- Module-level singleton: ``from systemu.runtime.tool_registry_v2 import registry``
"""
from __future__ import annotations

import ast
import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ToolEntry:
    name: str
    toolset: str
    schema: Dict[str, Any]
    handler: Callable
    check_fn: Optional[Callable[[], bool]] = None
    requires_env: List[str] = field(default_factory=list)
    is_async: bool = False
    description: str = ""
    emoji: str = ""
    is_action_tool: bool = False
    max_result_size_chars: Optional[int] = None
    timeout_seconds: Optional[int] = None
    # v0.9.5 (code-side tool-registry pattern): zero-arg callable returning dict
    # overrides applied to the schema at LLM-rendering time. Used by delegate_task
    # to surface live config (current max_depth, etc.) in its description.
    dynamic_schema_overrides: Optional[Callable[[], Dict[str, Any]]] = None


def _module_registers_tools(module_path: Path) -> bool:
    """AST-only check: does this file contain top-level ``registry.register(...)``?"""
    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
    except (OSError, SyntaxError):
        return False
    for stmt in tree.body:
        if not isinstance(stmt, ast.Expr):
            continue
        call = stmt.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if (isinstance(func, ast.Attribute)
                and func.attr == "register"
                and isinstance(func.value, ast.Name)
                and func.value.id == "registry"):
            return True
    return False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolEntry] = {}
        self._check_fn_cache: Dict[Callable, tuple] = {}  # fn -> (ts_monotonic, bool)
        self._lock = threading.RLock()

    def register(
        self,
        *,
        name: str,
        toolset: str,
        schema: Dict[str, Any],
        handler: Callable,
        check_fn: Optional[Callable[[], bool]] = None,
        requires_env: Optional[List[str]] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        is_action_tool: bool = False,
        max_result_size_chars: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        dynamic_schema_overrides: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        with self._lock:
            self._tools[name] = ToolEntry(
                name=name, toolset=toolset, schema=schema, handler=handler,
                check_fn=check_fn,
                requires_env=list(requires_env or []),
                is_async=is_async,
                description=description, emoji=emoji,
                is_action_tool=is_action_tool,
                max_result_size_chars=max_result_size_chars,
                timeout_seconds=timeout_seconds,
                dynamic_schema_overrides=dynamic_schema_overrides,
            )

    def unregister(self, name: str) -> bool:
        """Remove a tool entry by name. Returns True when present. Used by the
        MCP registry bridge to drop namespaced tools on disable / lease-revoke."""
        with self._lock:
            existed = name in self._tools
            self._tools.pop(name, None)
            return existed

    def unregister_prefix(self, prefix: str) -> int:
        """Remove every tool whose name starts with ``prefix``. Returns the count
        removed (used to drop all of one MCP server's namespaced tools)."""
        with self._lock:
            doomed = [n for n in self._tools if n.startswith(prefix)]
            for n in doomed:
                self._tools.pop(n, None)
            return len(doomed)

    def get(self, name: str) -> Optional[ToolEntry]:
        return self._tools.get(name)

    def list(self) -> List[ToolEntry]:
        return list(self._tools.values())

    def list_by_toolset(self, toolset: str) -> List[ToolEntry]:
        return [e for e in self._tools.values() if e.toolset == toolset]

    # ── check_fn availability with TTL cache ──────────────────────────────

    def available(self, name: str, config) -> bool:
        """Return True if the tool is registered AND its check_fn returns True
        (or no check_fn). Cached for ``config.check_fn_cache_ttl_seconds``."""
        entry = self._tools.get(name)
        if entry is None:
            return False
        cf = entry.check_fn
        if cf is None:
            return True
        ttl = int(getattr(config, "check_fn_cache_ttl_seconds", 30))
        now = time.monotonic()
        with self._lock:
            cached = self._check_fn_cache.get(cf)
            if cached is not None:
                ts, value = cached
                if now - ts < ttl:
                    return value
        try:
            value = bool(cf())
        except Exception as exc:
            logger.debug("[Registry] check_fn for %s raised: %s", name, exc)
            value = False
        with self._lock:
            self._check_fn_cache[cf] = (now, value)
        return value

    def invalidate_check_fn_cache(self) -> None:
        """Drop all cached check_fn results — call after config changes that
        affect availability (e.g. operator toggles a backend)."""
        with self._lock:
            self._check_fn_cache.clear()

    # ── AST-scan discovery ────────────────────────────────────────────────

    # ── Per-context whitelists ─────────────────────────────────────────

    # Hard-coded named whitelists. Code-registered tools that match these
    # names by string are allowed; tools that don't exist in the registry
    # but are in the whitelist are still listed (whitelist is intent, not
    # availability — registry.available() is the gate).
    _CONTEXT_WHITELISTS: Dict[str, Set[str]] = {
        "verifier_fork": {
            # Read-only tools — verifier must NOT mutate state while judging.
            "read_file",
            "search_files",
            "vault.get_audit_log",
            "vault.get_record",
            "scroll.read_path",
            "session_search",
            "session_recall",
        },
        "curator": {
            # L7 idle-time curator (lifecycle management of agent-created skills).
            "skill_list",
            "skill_view",
            "skill_archive",
            "memory_consolidate",
        },
        "fact_extractor": {
            # Pipeline that runs after chat — single write to user_facts.jsonl.
            "write_user_fact",
        },
        "delegate_child": {
            # Inherits parent's whitelist minus 'delegate' (no recursion).
            # The runtime overrides this dynamically per call; we expose
            # an "empty by default + parent-minus-delegate" intent here.
            # In practice the delegate machinery computes: parent - {delegate}.
        },
    }

    def whitelist_for_context(self, context: str) -> Set[str]:
        """Return the set of tool names allowed in ``context``.

        Known contexts: "main", "verifier_fork", "curator",
        "fact_extractor", "delegate_child".

        - "main" returns the names of all CURRENTLY-REGISTERED tools.
        - Named whitelists are hard-coded constants (read by the runtime
          to scope what each fork sees).
        - "delegate_child" returns the empty set — runtime composes
          parent_whitelist - {"delegate"} dynamically.
        - Unknown context raises ValueError.
        """
        if context == "main":
            return set(self._tools.keys())
        if context == "delegate_child":
            return set()
        if context in self._CONTEXT_WHITELISTS:
            return set(self._CONTEXT_WHITELISTS[context])
        raise ValueError(f"unknown whitelist context: {context!r}")

    # ── AST-scan discovery ────────────────────────────────────────────────

    def discover_modules(self, package_path: str) -> List[str]:
        """Import every submodule of ``package_path`` that contains a top-level
        ``registry.register(...)`` call. Returns the list of imported module
        names.

        ``package_path`` is a Python package path like ``"systemu.runtime.tools"``."""
        try:
            pkg = importlib.import_module(package_path)
        except Exception as exc:
            logger.warning("[Registry] cannot import package %s: %s", package_path, exc)
            return []
        pkg_dir = Path(pkg.__file__).resolve().parent

        module_names: List[str] = []
        for path in sorted(pkg_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            if not _module_registers_tools(path):
                continue
            mod_name = f"{package_path}.{path.stem}"
            try:
                importlib.import_module(mod_name)
                module_names.append(mod_name)
            except Exception as exc:
                logger.warning("[Registry] failed to import %s: %s", mod_name, exc)
        return module_names


# Module-level singleton — module-level register() calls in tool files
# bind to this instance.
registry: ToolRegistry = ToolRegistry()
