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

from typing import TYPE_CHECKING, Any

from .marker_grammar import CCR_TOOL_NAME, is_valid_ccr_hash

if TYPE_CHECKING:
    from .mcp_server import FurlMCPServer

__all__ = [
    # Wire contract
    "CCR_TOOL_NAME",
    "is_valid_ccr_hash",
    # MCP server
    "FurlMCPServer",
]


def __getattr__(name: str) -> Any:
    """Resolve ``FurlMCPServer`` lazily (PEP 562).

    Importing it eagerly here pulled ``mcp_server`` into ``sys.modules`` during
    ``furl_ctx.ccr`` package init. Running the server as ``python -m
    furl_ctx.ccr.mcp_server`` then found the module already imported before
    runpy executed it, emitting the ``found in sys.modules … prior to
    execution`` RuntimeWarning on every launch. Deferring the import to first
    attribute access keeps ``from furl_ctx.ccr import FurlMCPServer`` working
    while leaving the module out of the parent package's import so the
    ``-m`` entry point is the sole importer — no warning. It also keeps the
    heavy ``mcp`` SDK off the ``import furl_ctx`` path unless the server is used.
    """
    if name == "FurlMCPServer":
        from .mcp_server import FurlMCPServer

        return FurlMCPServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
