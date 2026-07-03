"""CCR (Compress-Cache-Retrieve) module for reversible compression.

When tool outputs are compressed, the LLM can retrieve the original
content by hash. Two components live here:

1. ``marker_grammar`` — the single owned definition of the CCR wire
   format (marker shapes, hash widths, the ``furl_retrieve`` tool
   name, and the ``is_valid_ccr_hash`` spoofing guard).
2. ``mcp_server`` — the standalone MCP server that exposes the
   ``furl_retrieve`` tool (plus ``furl_compress``/``furl_stats``) via
   the MCP protocol. This is the production retrieval channel.

The historical proxy-side injection plane (``tool_injection`` —
``CCRToolInjector``, request/system-message injection, tool-call
parsing) had zero production callers post proxy-removal and was excised
(SIMP-4). ``CCR_TOOL_NAME`` and the marker patterns it owned live on in
``marker_grammar``.
"""

from .marker_grammar import CCR_TOOL_NAME, is_valid_ccr_hash

# MCP server is optional (requires mcp package)
try:
    from .mcp_server import FurlMCPServer, create_ccr_mcp_server

    MCP_SERVER_AVAILABLE = True
except ImportError:
    FurlMCPServer = None  # type: ignore
    create_ccr_mcp_server = None  # type: ignore
    MCP_SERVER_AVAILABLE = False

__all__ = [
    # Wire contract
    "CCR_TOOL_NAME",
    "is_valid_ccr_hash",
    # MCP server
    "FurlMCPServer",
    "create_ccr_mcp_server",
    "MCP_SERVER_AVAILABLE",
]
