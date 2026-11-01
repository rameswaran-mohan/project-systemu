"""Defect 1 -- a server that could not be listed must never read as "zero tools".

Witnessed defect: ``ConnectionManager.list_tools`` returned ``[]`` on ANY error,
so ``mcp_list_tools`` reported ``{"success": True, "tools": []}`` for a server
that never started. That is the same fact a healthy server with no tools
produces, so the operator (and the rug-pull re-check) could not tell them apart.

Ruling (DEC-32, a fence is a value): ``list_tools`` returns a ``ToolListing``
that carries ``ok`` / ``reason`` / ``server``; every caller propagates a failure
as ``success: False`` with the reason and the server named. A plain list is not
a listing and is never accepted as success.

Hermetic: stdio servers spawned from this checkout, a vault under ``tmp_path``,
no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
ZERO_TOOL_SERVER = str(_TESTS / "_mcp_zero_tool_server.py")
REF_SERVER = str(_TESTS / "_mcp_reference_server.py")

# A "server" that dies before it speaks: it writes one line to stderr and exits 3.
_DIES_ARGS = ["-c", "import sys; sys.stderr.write('server could not start\\n'); sys.exit(3)"]


class _Vault:
    """connections.py only reads ``.root``."""

    def __init__(self, root):
        self.root = str(root)


def _spec(args):
    return {"transport": "stdio", "command": sys.executable, "args": list(args), "env": {}}


def _vault_with(tmp_path, server, args):
    from systemu.runtime.mcp import connections as c
    v = _Vault(tmp_path)
    c.add_server(v, server, transport=_spec(args))
    return v


# ---------------------------------------------------------------------------
# The manager returns a VALUE that distinguishes the two facts.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestManagerListingValue:
    async def test_a_server_that_exits_is_a_failed_listing(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager, ToolListing
        listing = await ConnectionManager(default_timeout=20.0).list_tools(
            "stdio:dies", _spec(_DIES_ARGS))
        assert type(listing) is ToolListing, (
            f"list_tools must return a ToolListing, got {type(listing).__name__}")
        assert listing.ok is False
        assert listing.server == "stdio:dies"
        assert type(listing.reason) is str and listing.reason.strip()
        assert listing.tools == ()

    async def test_iterating_a_failed_listing_is_loud_not_empty(self):
        from systemu.runtime.mcp.sdk.manager import (
            ConnectionManager, ToolListingFailed)
        listing = await ConnectionManager(default_timeout=20.0).list_tools(
            "stdio:dies", _spec(_DIES_ARGS))
        with pytest.raises(ToolListingFailed) as err:
            list(listing)
        assert "stdio:dies" in str(err.value)

    async def test_a_server_with_zero_tools_is_a_successful_empty_listing(self):
        from systemu.runtime.mcp.sdk.manager import ConnectionManager, ToolListing
        listing = await ConnectionManager(default_timeout=30.0).list_tools(
            "stdio:zero", _spec([ZERO_TOOL_SERVER]))
        assert type(listing) is ToolListing
        assert listing.ok is True, listing.reason
        assert listing.reason is None
        assert listing.tools == ()
        assert list(listing) == []


class TestToolListingInvariants:
    def test_failed_listing_must_say_why(self):
        from systemu.runtime.mcp.sdk.manager import ToolListing
        with pytest.raises(ValueError):
            ToolListing.failed("s", "")
        with pytest.raises(ValueError):
            ToolListing.failed("s", "   ")

    def test_failed_listing_carries_no_tools(self):
        from systemu.runtime.mcp.sdk.manager import ToolListing
        with pytest.raises(ValueError):
            ToolListing(server="s", ok=False, tools=({"name": "x"},), reason="down")

    def test_successful_listing_carries_no_reason(self):
        from systemu.runtime.mcp.sdk.manager import ToolListing
        with pytest.raises(ValueError):
            ToolListing(server="s", ok=True, tools=(), reason="but also down")

    def test_ok_is_a_real_bool(self):
        from systemu.runtime.mcp.sdk.manager import ToolListing
        with pytest.raises(TypeError):
            ToolListing(server="s", ok=1, tools=(), reason=None)


# ---------------------------------------------------------------------------
# Every caller propagates the failure: mcp_list_tools, discover_and_pin.
# ---------------------------------------------------------------------------

class TestCallersPropagate:
    def test_mcp_list_tools_reports_a_dead_server_as_a_failure(self, tmp_path):
        from systemu.runtime.mcp import client as mc
        v = _vault_with(tmp_path, "stdio:dies", _DIES_ARGS)
        out = mc.mcp_list_tools(server="stdio:dies", vault=v)
        assert out["success"] is False, out
        assert "stdio:dies" in out["error"], out
        assert out.get("server") == "stdio:dies", out
        assert "tools" not in out, "a failed listing must not carry a tools list"

    def test_mcp_list_tools_reports_zero_tools_as_success(self, tmp_path):
        from systemu.runtime.mcp import client as mc
        v = _vault_with(tmp_path, "stdio:zero", [ZERO_TOOL_SERVER])
        out = mc.mcp_list_tools(server="stdio:zero", vault=v)
        assert out == {"success": True, "tools": []}, out

    def test_discover_and_pin_propagates_and_pins_nothing(self, tmp_path):
        from systemu.runtime.mcp import client as mc
        from systemu.runtime.mcp import connections as c
        v = _vault_with(tmp_path, "stdio:dies", _DIES_ARGS)
        out = mc.discover_and_pin(v, "stdio:dies")
        assert out["success"] is False, out
        assert "stdio:dies" in out["error"]
        assert c.get_state(v)["hashes"] == {}

    def test_a_plain_list_from_the_manager_is_not_a_listing(self, monkeypatch):
        """Ruling: a plain list is never accepted as success -- it cannot carry
        the failure, so accepting it would re-open the defect."""
        from systemu.runtime.mcp import client as mc
        from systemu.runtime.mcp.sdk import manager as _m

        async def fake_list(self, server, spec):
            return []

        monkeypatch.setattr(_m.ConnectionManager, "list_tools", fake_list)
        out = mc.mcp_list_tools(server="stdio:fake", timeout=5)
        assert out["success"] is False, out
        assert "stdio:fake" in out["error"]
        assert "ToolListing" in out["error"]

    def test_the_reference_server_still_lists(self, tmp_path):
        from systemu.runtime.mcp import client as mc
        v = _vault_with(tmp_path, "stdio:ref", [REF_SERVER])
        out = mc.mcp_list_tools(server="stdio:ref", vault=v)
        assert out["success"] is True, out
        assert {"echo", "delete_thing"} <= {t["name"] for t in out["tools"]}
