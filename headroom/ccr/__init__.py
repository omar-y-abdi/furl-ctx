"""CCR (Compress-Cache-Retrieve) module for reversible compression.

This module provides tool injection and retrieval handling for the CCR architecture.
When tool outputs are compressed, the LLM can retrieve more data if needed.

Key components:
1. Tool Injection: the hook injects the headroom_retrieve tool into requests

Two distribution channels for the retrieval tool:
1. Tool Injection: the hook injects the tool into a request when compression occurs
2. MCP Server: standalone server exposes the tool via MCP protocol

When MCP is configured, tool injection is skipped to avoid duplicates.
"""

from .tool_injection import (
    CCR_TOOL_NAME,
    CCRToolInjector,
    create_ccr_tool_definition,
    create_system_instructions,
    parse_tool_call,
)

# MCP server is optional (requires mcp package)
try:
    from .mcp_server import HeadroomMCPServer, create_ccr_mcp_server

    MCP_SERVER_AVAILABLE = True
except ImportError:
    HeadroomMCPServer = None  # type: ignore
    create_ccr_mcp_server = None  # type: ignore
    MCP_SERVER_AVAILABLE = False

__all__ = [
    # Tool injection
    "CCR_TOOL_NAME",
    "CCRToolInjector",
    "create_ccr_tool_definition",
    "create_system_instructions",
    "parse_tool_call",
    # MCP server
    "HeadroomMCPServer",
    "create_ccr_mcp_server",
    "MCP_SERVER_AVAILABLE",
]
