"""T3 — the agent-callable ``table_propose`` registry tool (spec §5.10.b#2).

A task that discovers the operator uses something ("their invoices live in
D:/Invoices") may PROPOSE it onto the table. Everything it writes lands
``suggested`` + ``content_derived`` in the "New on your table" tray, awaiting the
operator — a proposal is a suggestion, never a fact.

TWO THINGS THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------------
1. **It exposes no consult channel.** The schema carries ``kind``/``name``/
   ``detail`` and nothing else. §5.10.b#2 forces provenance "``consulted`` only
   from the designated consult context"; making that structural — there is no
   parameter in which a task could spell a consult context — is stronger than any
   runtime check on a caller-supplied string. The consult writes through
   ``table_consult.commit``, which is reachable only from the /table UI.
2. **It is not an action tool.** It creates an intent card. It configures nothing,
   connects nothing, grants nothing (§5.10.b#3 "the table never authorizes"), so
   it needs no gate — and, being an ordinary ``TOOL_CALL``, it costs no
   harness-request budget (the §5.10.1 non-action path).

The v2 rail injects no vault, so the handler opens one the way every other v2
handler builds its own config (``skill_tools`` → ``Config.from_env()``).
"""
from __future__ import annotations

from typing import Any, Dict

from systemu.runtime.tool_registry_v2 import registry

TABLE_PROPOSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["service", "mcp_server", "data_root", "credential_ref",
                     "preference", "device"],
            "description": "What sort of thing this is. 'tool' is not proposable.",
        },
        "name": {
            "type": "string",
            "description": "The operational identifier — a service name, a server "
                           "URL, a folder path, or a credential NAME. Never a "
                           "secret value.",
        },
        "detail": {
            "type": "string",
            "description": "A short note about it (optional).",
        },
    },
    "required": ["kind", "name"],
}


def _open_vault():
    """The active vault. Split out as a seam so a test can drive the handler
    without a configured environment."""
    from sharing_on.config import Config
    from systemu.vault.factory import open_vault
    return open_vault(Config.from_env())


def table_propose_handler(**kwargs: Any) -> Dict[str, Any]:
    """Propose ONE item onto the operator's table. Never raises."""
    from systemu.runtime import table_consult

    try:
        vault = _open_vault()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "accepted": False, "error": f"no vault: {exc}"}

    result = table_consult.propose(
        vault,
        kind=str(kwargs.get("kind") or ""),
        name=str(kwargs.get("name") or ""),
        detail=str(kwargs.get("detail") or ""),
    )
    return {
        "success": True,
        "accepted": bool(result.get("accepted")),
        "reason": result.get("reason", ""),
        "note": ("Proposed to the operator's table as a SUGGESTION — it is not "
                 "confirmed and confers no access."),
    }


# ── Module-level registration (AST-scan discovery picks this up) ──

registry.register(
    name="table_propose", toolset="table",
    schema=TABLE_PROPOSE_SCHEMA, handler=table_propose_handler,
    description="Propose something the operator has (a service, MCP server, "
                "folder, credential NAME, preference or device) onto their "
                "table as a SUGGESTION for them to confirm. Creates an intent "
                "card only — it configures and authorizes nothing.",
    is_action_tool=False,
    max_result_size_chars=2_000,
)
