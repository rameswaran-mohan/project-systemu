"""Hermetic MCP servers for the CGB MCP capability family.

These are spawned over stdio by systemu's MCP client when the agent
``REQUEST_HARNESS kind=mcp`` to attach them. They expose tools whose output the
agent CANNOT compute or guess locally, so a pull trial passes only if the agent
actually attached the server and called its tool (mirroring the TOOL family's
real-gap contract). No network: stdio subprocess only.
"""
