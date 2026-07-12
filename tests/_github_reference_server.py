"""R-A14a slice 4 — a hermetic in-process GitHub-style reference MCP server.

Spawned over stdio by ``stdio_client`` (``sys.executable`` + this path), exactly
like ``tests/_mcp_reference_server.py``. It exposes ONE non-destructive action
tool, ``create_issue``, that returns a STRUCTURED dict modelling a created GitHub
issue:

    {"html_url": "https://93.184.216.34/repos/{repo}/issues/{n}",
     "number": n, "id": n, "state": "open", "title": title}

Two properties make this the JOIN point for the G-DEMO v0 acceptance fixture:

  * It returns a **dict** (NOT a str), annotated as an ``IssueResult`` TypedDict.
    Against the installed FastMCP (mcp 1.28.0) a plain ``-> dict`` return is
    wrapped one level deep under ``structuredContent = {"result": {...}}`` — which
    would hide ``html_url`` from ``_synthesize_directive`` (it reads ``html_url``
    at the TOP level) and silently degrade the receipt to CLAIMED. A TypedDict (or
    BaseModel) return annotation gives FastMCP a field schema, so it emits a FLAT
    ``structuredContent`` with ``html_url`` at the top level. The function still
    returns a plain ``dict`` literal at runtime (a TypedDict IS a dict). A ``str``
    return would collapse to unstructured ``content`` with no ``html_url`` at all.
  * ``html_url`` carries a **public-IP literal host** (``93.184.216.34``) so the
    independent ``ProdReadbackClient`` SSRF gate passes with NO DNS resolution, and
    the ``n`` embedded in the path is how the create seam and the readback seam
    JOIN with no shared runtime state (the mock-REST handler parses ``n`` back out).

Run as a script: ``python tests/_github_reference_server.py``.
"""
from typing import TypedDict

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("github")


class IssueResult(TypedDict):
    """The structured create_issue result. Its field schema is what makes
    FastMCP emit a FLAT ``structuredContent`` (``html_url`` at the top level)
    rather than nesting the dict under a ``result`` key."""
    html_url: str
    number: int
    id: int
    state: str
    title: str


class PaymentResult(TypedDict):
    """The structured create_payment result — a money-move receipt. Shaped like
    a created resource (an https receipt URL + an id/number token) so the SAME
    ``_synthesize_directive`` builds a readback directive. The money-move gate
    (no independent MCP pre-submit probe) is what keeps it CLAIMED — proving a
    money-move can never be faux-verified even when the readback echoes the id."""
    html_url: str
    number: int
    id: int
    status: str
    amount: str

# Deterministic in-process issue counter (starts at 1). Each spawned server
# process gets its own counter — the fixture only needs determinism WITHIN a
# single created issue, and the readback handler parses `n` back from the url.
_ISSUE_N = {"n": 0}


def _next_issue_number() -> int:
    _ISSUE_N["n"] += 1
    return _ISSUE_N["n"]


# A DISTINCTIVE, large, opaque id base (a real GitHub issue ``id`` is a big int,
# unlike the small human ``number``). This is load-bearing for the fixture's
# RIGOR: ``_synthesize_directive`` collects BOTH ``id`` and ``number`` as expected
# tokens, and ``_tokens_all_present`` requires EVERY one present (substring OK). A
# small numeric token like "1" would substring-match by coincidence (e.g. inside
# the IP host ``93.184.216.34``), so a WRONG readback could spuriously "confirm".
# A distinctive 10-digit id CANNOT coincidentally appear in the IP/timestamp, so a
# non-matching readback genuinely fails the token gate — the readback is proven
# load-bearing. ``issue_id_for(n)`` is shared with the readback handler so the two
# live seams still join deterministically via the ``n`` carried in the url.
_ID_BASE = 4_010_500_000


def issue_id_for(n: int) -> int:
    """The deterministic distinctive id for issue ``n`` (shared with the readback
    handler so a CORRECT re-read echoes it and a WRONG one cannot)."""
    return _ID_BASE + n


# A public-IP literal host: the readback SSRF gate passes with no DNS, and the
# path carries `n` so the two live seams join with no shared runtime state.
_PUB_IP = "93.184.216.34"


@mcp.tool()
def create_issue(repo: str, title: str, body: str = "") -> IssueResult:
    """Open a GitHub issue. NON-destructive action (no readOnlyHint / no
    destructiveHint → the dispatch gate tiers it as an action). Returns a
    STRUCTURED dict so FastMCP emits a flat ``structuredContent``. The ``id`` is a
    DISTINCTIVE large int (``issue_id_for(n)``) so the readback token match is
    load-bearing (a wrong readback cannot coincidentally echo it)."""
    n = _next_issue_number()
    return {
        "html_url": f"https://{_PUB_IP}/repos/{repo}/issues/{n}",
        "number": n,
        "id": issue_id_for(n),
        "state": "open",
        "title": title,
    }


@mcp.tool()
def create_payment(payee: str, amount: str) -> PaymentResult:
    """Send a payment. The money-move negative control: it returns a created-
    resource-shaped receipt (an https receipt URL + id/number), so the same
    synthesize+readback path runs — yet the money-move fail-closed gate must keep
    the receipt CLAIMED (no independent MCP pre-submit probe proves freshness)."""
    n = _next_issue_number()
    return {
        "html_url": f"https://{_PUB_IP}/payments/{n}",
        "number": n,
        "id": n,
        "status": "settled",
        "amount": amount,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
