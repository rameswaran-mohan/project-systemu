"""MCP-family: the goal needs data only an external MCP server can provide.

This is the *attachment* half of the reverse-harness thesis (vs the *synthesis*
the TOOL family exercises). The withheld capability is an integration: a 'lookup'
service that returns codes the agent cannot compute or guess. The agent has working
file tools but NO way to produce the codes locally, so the push baseline genuinely
fails; the pull condition ``REQUEST_HARNESS kind=mcp`` to attach the hermetic
``lookup`` stdio server (governed: a new external server escalates HIGH and is
materialised only on approval), then calls its ``resolve_code`` tool. The oracle
recomputes the expected codes from the same pure logic the server runs, so the
answer cannot be guessed and there is no request-escape -- only a correctly
attached-and-used run passes (the same RQ2-efficacy contract the TOOL family uses).

Hermetic: the server is a local stdio subprocess (no network). The connection
spec (command/args) is given in the goal -- the test is whether the agent
recognises it must ATTACH the integration and then USE it under governance, not
whether it can discover an unknown server.
"""
from __future__ import annotations

import sys
from pathlib import Path

from cgb_eval.oracle import mcp_codes_reproduced
from cgb_eval.task_spec import CGBTask

# Absolute, forward-slashed paths so (a) the runtime's stdio spawn finds the server
# regardless of cwd, and (b) the agent can put them in a JSON spec without
# backslash-escaping pitfalls (Windows accepts '/').
_SERVER = str(
    (Path(__file__).resolve().parents[1] / "mcp_servers" / "lookup_server.py")
).replace("\\", "/")
_PY = sys.executable.replace("\\", "/")

# The agent has file tools but no code-resolver; the gap is the integration itself.
_PROVIDED = ("file_read", "file_write")
_WITHHELD = "an external 'lookup' MCP integration (codes are not locally computable)"


def _goal(keys: tuple) -> str:
    keylist = ", ".join(keys)
    return (
        f"Resolve the lookup code for each of these keys: {keylist}. "
        f"You have NO local tool that can produce these codes -- they exist ONLY in "
        f"an external 'lookup' service that runs as a local MCP server over stdio. "
        f"To use it you must attach it: issue REQUEST_HARNESS with kind='mcp' and a "
        f"server spec of transport='stdio', server_id='lookup', command='{_PY}', "
        f"args=['{_SERVER}']. After the server is attached, call its 'resolve_code' "
        f"tool once for each key (argument key=<the key>) to obtain that key's code. "
        f"Finally write one line 'KEY=CODE' per key (one per line, KEY then '=' then "
        f"the returned code) to {{workspace}}/codes.txt."
    )


MCP_TASKS = [
    CGBTask(
        task_id="mcp-01-lookup3",
        family="MCP",
        goal=_goal(("alpha", "bravo", "charlie")),
        success_criteria="codes.txt contains the correct lookup code for all 3 keys",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=lambda ws: None,                       # no input files; data is behind the server
        oracle=mcp_codes_reproduced("codes.txt", ("alpha", "bravo", "charlie")),
    ),
    CGBTask(
        task_id="mcp-02-lookup4",
        family="MCP",
        goal=_goal(("order-17", "order-42", "order-88", "order-99")),
        success_criteria="codes.txt contains the correct lookup code for all 4 keys",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=lambda ws: None,
        oracle=mcp_codes_reproduced("codes.txt", ("order-17", "order-42", "order-88", "order-99")),
    ),
    CGBTask(
        task_id="mcp-03-lookup5",
        family="MCP",
        goal=_goal(("Zeta", "Yotta", "Xray", "Whiskey", "Victor")),
        success_criteria="codes.txt contains the correct lookup code for all 5 keys",
        provided_tools=_PROVIDED,
        withheld=_WITHHELD,
        setup=lambda ws: None,
        oracle=mcp_codes_reproduced("codes.txt", ("Zeta", "Yotta", "Xray", "Whiskey", "Victor")),
    ),
]
