"""v0.9.6 L7 — boot-discovered registration shim for the process registry.

The process *state* + public API + handlers live in
``systemu.runtime.process_registry``.  The ``registry.register(...)`` calls
live HERE, under ``systemu.runtime.tools``, because the boot-time v2
discovery (`shadow_runtime._discover_v2_tools` → `registry.discover_modules`)
AST-scans ONLY the ``systemu.runtime.tools`` package for top-level
``registry.register(...)`` calls.

Before this shim existed, ``process_list`` / ``process_check`` were defined in
``process_registry`` (which lives one level up in ``systemu.runtime``) and were
therefore never imported at boot — the tools existed in unit tests (which
import the module directly) but were invisible to the LLM in production.
"""
from __future__ import annotations

from systemu.runtime.tool_registry_v2 import registry
from systemu.runtime.process_registry import (
    _process_list_handler,
    _process_check_handler,
    _LIST_SCHEMA,
    _CHECK_SCHEMA,
)

registry.register(
    name="process_list", toolset="process",
    schema=_LIST_SCHEMA, handler=_process_list_handler,
    description="List all registered background processes (running + completed).",
    is_action_tool=False,
    max_result_size_chars=20_000,
)

registry.register(
    name="process_check", toolset="process",
    schema=_CHECK_SCHEMA, handler=_process_check_handler,
    description="Check the status + output of one registered background process.",
    is_action_tool=False,
    max_result_size_chars=20_000,
)
