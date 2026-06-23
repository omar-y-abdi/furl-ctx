"""Tool injection for CCR (Compress-Cache-Retrieve).

This module provides the retrieval tool definition that gets injected into
LLM requests when compression occurs. The tool allows the LLM to retrieve
original uncompressed content if needed.

Two injection modes:
1. Tool Definition Injection: Adds a function tool to the tools array
2. System Message Injection: Adds instructions to the system message

The LLM can then call the tool or follow instructions to retrieve more data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Tool name constant - used for matching tool calls
CCR_TOOL_NAME = "headroom_retrieve"

# Accepted CCR hash widths (number of hex characters):
# - 12: SmartCrusher path — sha256(payload)[:6] → 12 hex chars
#        (crusher.rs:1620, asserted at crusher.rs:2792)
# - 24: Canonical compute_key — BLAKE3 → 24 lowercase hex chars
#        (ccr/mod.rs:69), used by read_lifecycle, diff_compressor, etc.
# Do NOT add arbitrary lengths — the exact-width check is the spoofing guard.
CCR_HASH_WIDTHS: frozenset[int] = frozenset({12, 24})


def create_ccr_tool_definition(
    provider: str = "anthropic",
) -> dict[str, Any]:
    """Create the CCR retrieval tool definition.

    This tool definition is injected into the request's tools array when
    compression occurs. The LLM can call this tool to retrieve original
    uncompressed content.

    Args:
        provider: The provider type ("anthropic", "openai", "google").
                  Affects the tool definition format.

    Returns:
        Tool definition dict in the appropriate format.
    """
    # Base tool definition (OpenAI format)
    openai_definition = {
        "type": "function",
        "function": {
            "name": CCR_TOOL_NAME,
            "description": (
                "Retrieve original uncompressed content that was compressed to save tokens. "
                "Use this when you need more data than what's shown in compressed tool results. "
                "The hash is provided in compression markers like [N items compressed... hash=abc123]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "Hash key from the compression marker (e.g., 'abc123' from hash=abc123)",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional search query to filter results. "
                            "If provided, only returns items matching the query. "
                            "If omitted, returns all original items."
                        ),
                    },
                },
                "required": ["hash"],
            },
        },
    }

    if provider == "openai":
        return openai_definition

    elif provider == "anthropic":
        # Anthropic uses a slightly different format
        return {
            "name": CCR_TOOL_NAME,
            "description": (
                "Retrieve original uncompressed content that was compressed to save tokens. "
                "Use this when you need more data than what's shown in compressed tool results. "
                "The hash is provided in compression markers like [N items compressed... hash=abc123]."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "Hash key from the compression marker (e.g., 'abc123' from hash=abc123)",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional search query to filter results. "
                            "If provided, only returns items matching the query. "
                            "If omitted, returns all original items."
                        ),
                    },
                },
                "required": ["hash"],
            },
        }

    elif provider == "google":
        # Google/Gemini format
        return {
            "name": CCR_TOOL_NAME,
            "description": (
                "Retrieve original uncompressed content that was compressed to save tokens. "
                "Use this when you need more data than what's shown in compressed tool results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "Hash key from the compression marker",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional search query to filter results",
                    },
                },
                "required": ["hash"],
            },
        }

    else:
        # Default to OpenAI format
        return openai_definition


def create_system_instructions(
    hashes: list[str],
    retrieval_endpoint: str = "/v1/retrieve",
) -> str:
    """Create system message instructions for CCR retrieval.

    This is an alternative to tool injection - adds instructions to the
    system message telling the LLM how to retrieve compressed data.

    Args:
        hashes: List of hash keys for compressed content in this context.
        retrieval_endpoint: The endpoint path for retrieval.

    Returns:
        Instruction text to append to system message.
    """
    hash_list = ", ".join(hashes) if len(hashes) <= 5 else f"{', '.join(hashes[:5])} ..."

    return f"""
## Compressed Context Available

Some tool outputs have been compressed to reduce context size. If you need
the full uncompressed data, you can retrieve it using the `{CCR_TOOL_NAME}` tool.

**How to retrieve:**
- Call `{CCR_TOOL_NAME}(hash="<hash>")` to get all original items
- Call `{CCR_TOOL_NAME}(hash="<hash>", query="search terms")` to search within

**Available hashes:** {hash_list}

Look for markers like `[N items compressed to M. Retrieve more: hash=abc123]`
in tool results to find the hash for each compressed output.
"""


@dataclass
class CCRToolInjector:
    """Manages CCR tool injection into LLM requests.

    This class handles:
    1. Detecting compression markers in messages
    2. Injecting the retrieval tool definition
    3. Adding system message instructions
    4. Tracking which hashes are available

    Usage:
        injector = CCRToolInjector(provider="anthropic")

        # Process messages to detect compression markers
        injector.scan_for_markers(messages)

        # Inject tool if compression was detected
        if injector.has_compressed_content:
            tools = injector.inject_tool(tools)
            messages = injector.inject_system_instructions(messages)
    """

    provider: str = "anthropic"
    inject_tool: bool = True
    inject_system_instructions: bool = True
    retrieval_endpoint: str = "/v1/retrieve"

    # Detected compression markers
    _detected_hashes: list[str] = field(default_factory=list)
    # Multiple marker patterns to match different compressors:
    # - SmartCrusher: [100 items compressed to 10. Retrieve more: hash=abc123]
    # - Kompress: [100 lines compressed to 10. Retrieve more: hash=abc123]
    # - LogCompressor: [200 lines compressed to 20. Retrieve more: hash=abc123]
    # - SearchCompressor: [50 matches compressed to 5. Retrieve more: hash=abc123]
    # - Generic: any [... compressed ... hash=xxx] pattern
    _marker_patterns: list[re.Pattern] = field(
        default_factory=lambda: [
            # Bracket-form patterns require exactly 24 hex characters for hash validation.
            # CCR canonical compute_key uses BLAKE3 → 24 lowercase hex chars (ccr/mod.rs:69).
            # Requiring exact length prevents hash spoofing attacks with shorter hashes.
            #
            # Standard format: [N <type> compressed to M. Retrieve more: hash=xxx]
            # Matches items, lines, matches, or any other type
            re.compile(r"\[(\d+) \w+ compressed to (\d+)\. Retrieve more: hash=([a-f0-9]{24})\]"),
            # Legacy format without "to M" or "Retrieve more:" (old TextCompressor)
            re.compile(r"\[(\d+) \w+ compressed\. hash=([a-f0-9]{24})\]"),
            # Generic fallback: any compression marker with hash (exactly 24 chars)
            re.compile(r"\[.*?compressed.*?hash=([a-f0-9]{24})\]", re.IGNORECASE),
            # SmartCrusher <<ccr:HASH ...>> / <<ccr:HASH>> double-angle-bracket forms.
            # The hash is either 12 hex chars (SmartCrusher: sha256(payload)[:6],
            # crusher.rs:1620) or 24 hex chars (canonical BLAKE3 compute_key, mod.rs:69).
            # Four emit shapes (crusher.rs:1160 / formatter.rs:560 / mod.rs:81):
            #   <<ccr:HASH N_rows_offloaded>>   — row-drop sentinel (space)
            #   <<ccr:HASH#rows N_chunks>>       — row-index sentinel (#)
            #   <<ccr:HASH,KIND,SIZE>>           — opaque-blob sentinel (comma)
            #   <<ccr:HASH>>                     — bare sentinel (>> immediately after hash)
            # One capturing group (the hash) so re.findall returns strings directly.
            # Alternation longest-first: 24 before 12 to avoid prefix-matching a 24-char
            # hash as 12 chars when followed by more hex digits.
            re.compile(
                r"<<ccr:([a-f0-9]{24}|[a-f0-9]{12})(?:[ ,#>]|>>)"
            ),
        ]
    )

    def __post_init__(self) -> None:
        # Reset detected hashes
        self._detected_hashes = []

    @property
    def has_compressed_content(self) -> bool:
        """Check if any compressed content was detected."""
        return len(self._detected_hashes) > 0

    @property
    def detected_hashes(self) -> list[str]:
        """Get list of detected compression hashes."""
        return self._detected_hashes.copy()

    def scan_for_markers(self, messages: list[dict[str, Any]]) -> list[str]:
        """Scan messages for compression markers and extract hashes.

        Args:
            messages: List of messages to scan.

        Returns:
            List of detected hash keys.
        """
        self._detected_hashes = []

        for message in messages:
            content = message.get("content", "")

            # Handle string content
            if isinstance(content, str):
                self._scan_text(content)

            # Handle list content (Anthropic format with content blocks)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        # Text blocks
                        if block.get("type") == "text":
                            self._scan_text(block.get("text", ""))
                        # Tool result blocks
                        elif block.get("type") == "tool_result":
                            tool_content = block.get("content", "")
                            if isinstance(tool_content, str):
                                self._scan_text(tool_content)
                            elif isinstance(tool_content, list):
                                for item in tool_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        self._scan_text(item.get("text", ""))

            # Handle Google/Gemini format with parts
            parts = message.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict):
                        # Text parts
                        if "text" in part:
                            self._scan_text(part.get("text", ""))
                        # Function response parts (tool results)
                        elif "functionResponse" in part:
                            response = part.get("functionResponse", {}).get("response", {})
                            if isinstance(response, str):
                                self._scan_text(response)
                            elif isinstance(response, dict):
                                # Scan string values in response
                                for value in response.values():
                                    if isinstance(value, str):
                                        self._scan_text(value)

        return self._detected_hashes

    def _scan_text(self, text: str) -> None:
        """Scan text for compression markers from any compressor."""
        for pattern in self._marker_patterns:
            matches = pattern.findall(text)
            for match in matches:
                # Extract hash_key from match (last group is always the hash)
                if isinstance(match, tuple):
                    hash_key = match[-1]  # Last capture group is the hash
                else:
                    hash_key = match  # Single capture group (generic pattern)
                if hash_key and hash_key not in self._detected_hashes:
                    self._detected_hashes.append(hash_key)

    def inject_tool_definition(
        self,
        tools: list[dict[str, Any]] | None,
        *,
        session_has_done_ccr: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Inject CCR retrieval tool into tools list.

        PR-B7 (`REALIGNMENT/04-phase-B-live-zone.md`): callers may pass
        ``session_has_done_ccr=True`` so the tool is injected even when
        THIS request has no fresh compression markers. That is the
        sticky-on path: once a session has done CCR, the
        ``headroom_retrieve`` tool must stay in ``body["tools"]`` for
        every subsequent request, otherwise the tool list bytes flip
        on/off mid-session and bust the prompt cache.

        This method is the per-request path used when no session_id is
        available (e.g. Google handler, legacy code paths); callers that
        have a ``SessionCcrTracker`` thread it directly.

        Args:
            tools: Existing tools list (may be None or empty).
            session_has_done_ccr: When True, inject regardless of
                whether the current request contained compression
                markers. Default False preserves legacy per-request
                behaviour.

        Returns:
            Tuple of (updated_tools, was_injected).
            was_injected is False if tool was already present (e.g., from MCP).
        """
        if not self.inject_tool:
            return tools or [], False
        # PR-B7: sticky-on takes precedence. If the session has
        # previously done CCR, register the tool even when this turn
        # has no fresh markers. Otherwise fall back to the per-request
        # check for backwards compat.
        if not (session_has_done_ccr or self.has_compressed_content):
            return tools or [], False

        tools = tools or []

        # Check if already present (e.g., from MCP server)
        for tool in tools:
            tool_name = tool.get("name") or tool.get("function", {}).get("name")
            if tool_name == CCR_TOOL_NAME:
                return tools, False  # Already present, skip injection

        # Add CCR tool
        ccr_tool = create_ccr_tool_definition(self.provider)
        return tools + [ccr_tool], True

    def inject_into_system_message(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Inject retrieval instructions into system message.

        Args:
            messages: List of messages.

        Returns:
            Updated messages with instructions added to system message.
        """
        if not self.inject_system_instructions or not self.has_compressed_content:
            return messages

        instructions = create_system_instructions(
            self._detected_hashes,
            self.retrieval_endpoint,
        )

        # Find and update system message
        updated_messages = []
        system_found = False

        for message in messages:
            if message.get("role") == "system" and not system_found:
                system_found = True
                content = message.get("content", "")

                # Don't add if already present
                if "Compressed Context Available" in content:
                    updated_messages.append(message)
                else:
                    # Append instructions
                    if isinstance(content, str):
                        updated_messages.append(
                            {
                                **message,
                                "content": content + instructions,
                            }
                        )
                    else:
                        # Handle structured content
                        updated_messages.append(message)
            else:
                updated_messages.append(message)

        # If no system message, prepend one
        if not system_found:
            updated_messages.insert(
                0,
                {
                    "role": "system",
                    "content": instructions.strip(),
                },
            )

        return updated_messages

    def process_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        session_has_done_ccr: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, bool]:
        """Process a request, scanning for markers and injecting as needed.

        This is a convenience method that does:
        1. Scan messages for compression markers
        2. Inject tool definition if enabled (skipped if already present from MCP)
        3. Inject system instructions if enabled

        PR-B7: when ``session_has_done_ccr`` is True the tool gets
        injected even when the current message stream has no fresh
        markers. System-instruction injection still keys off
        per-request markers (the system prompt is the cache hot zone —
        we never mutate it without a current-turn reason).

        Args:
            messages: Request messages.
            tools: Request tools (may be None).
            session_has_done_ccr: PR-B7 sticky-on flag — when True,
                register the tool regardless of this turn's marker scan.

        Returns:
            Tuple of (updated_messages, updated_tools, tool_was_injected).
            tool_was_injected is False if tool was already present (e.g., from MCP).
        """
        self.scan_for_markers(messages)

        if not (self.has_compressed_content or session_has_done_ccr):
            return messages, tools, False

        updated_tools, was_injected = self.inject_tool_definition(
            tools, session_has_done_ccr=session_has_done_ccr
        )
        updated_messages = self.inject_into_system_message(messages)

        return updated_messages, updated_tools if updated_tools else None, was_injected


def parse_tool_call(
    tool_call: dict[str, Any],
    provider: str = "anthropic",
) -> tuple[str | None, str | None]:
    """Parse a CCR tool call to extract hash and query.

    Args:
        tool_call: The tool call object from the LLM response.
        provider: The provider type for format detection.

    Returns:
        Tuple of (hash, query) or (None, None) if not a CCR tool call.
    """
    # Get tool name and input data based on provider format
    if provider == "anthropic":
        name = tool_call.get("name")
        input_data = tool_call.get("input", {})
    elif provider == "openai":
        function = tool_call.get("function", {})
        name = function.get("name")
        # OpenAI passes args as JSON string
        args_str = function.get("arguments", "{}")
        try:
            input_data = json.loads(args_str)
        except json.JSONDecodeError:
            input_data = {}
    elif provider == "google":
        # Google/Gemini format: {"functionCall": {"name": "...", "args": {...}}}
        function_call = tool_call.get("functionCall", {})
        name = function_call.get("name")
        input_data = function_call.get("args", {})
    else:
        # Generic fallback
        name = tool_call.get("name")
        input_data = tool_call.get("input", tool_call.get("args", {}))

    if name != CCR_TOOL_NAME:
        return None, None

    hash_key = input_data.get("hash")
    query = input_data.get("query")

    # Validate hash format: must be exactly 12 or 24 hex characters.
    # Legitimate widths (CCR_HASH_WIDTHS):
    #   12 — SmartCrusher sha256(payload)[:6] → 12 lowercase hex (crusher.rs:1620)
    #   24 — canonical BLAKE3 compute_key (ccr/mod.rs:69), used by read_lifecycle & diff
    # Any other length is rejected to prevent hash spoofing attacks.
    if hash_key is not None:
        if not isinstance(hash_key, str) or len(hash_key) not in CCR_HASH_WIDTHS:
            return None, None
        # Validate hex characters only
        if not all(c in "0123456789abcdef" for c in hash_key.lower()):
            return None, None

    return hash_key, query
