"""TDD tests for SmartCrusher 12-char hash support in tool_injection.py.

These tests verify that CCRToolInjector.scan_for_markers and parse_tool_call
correctly handle the SmartCrusher <<ccr:HASH>> marker form with 12-hex-char hashes
(sha256[:6] -> 12 hex chars), in addition to the canonical 24-hex-char hashes.

Unit U4: Accept SmartCrusher's 12-char hash and <<ccr:HASH>> marker form.

SmartCrusher emits markers in these forms:
  <<ccr:HASH N_rows_offloaded>>   (space separator)
  <<ccr:HASH#rows N_chunks>>      (hash index form)
  <<ccr:HASH,KIND,SIZE>>          (opaque blob form)
  <<ccr:HASH>>                    (bare form, ccr/mod.rs:81 marker_for)

BLAKE3 canonical compute_key produces 24-char hashes (ccr/mod.rs:69).
SmartCrusher sha256[:6] produces 12-char hashes (crusher.rs:1620).
"""

from __future__ import annotations

from furl_ctx.ccr.tool_injection import (
    CCR_HASH_WIDTHS,
    CCR_TOOL_NAME,
    CCRToolInjector,
    parse_tool_call,
)

# A real 12-char SmartCrusher hash (sha256[:6] -> 12 hex chars)
SMARTCRUSHER_HASH = "9f3a2b1c4d5e"
# A real 24-char canonical hash (BLAKE3 -> 24 hex chars)
CANONICAL_HASH = "a1b2c3d4e5f6a1b2c3d4e5f6"


class TestCCRHashWidths:
    """Test that CCR_HASH_WIDTHS is defined with the expected values."""

    def test_ccr_hash_widths_contains_12(self):
        """CCR_HASH_WIDTHS must include 12 (SmartCrusher sha256[:6])."""
        assert 12 in CCR_HASH_WIDTHS

    def test_ccr_hash_widths_contains_24(self):
        """CCR_HASH_WIDTHS must include 24 (BLAKE3 canonical compute_key)."""
        assert 24 in CCR_HASH_WIDTHS

    def test_ccr_hash_widths_is_frozenset(self):
        """CCR_HASH_WIDTHS must be a frozenset for immutability."""
        assert isinstance(CCR_HASH_WIDTHS, frozenset)

    def test_ccr_hash_widths_rejects_other_lengths(self):
        """CCR_HASH_WIDTHS must NOT accept arbitrary lengths."""
        for bad_len in [8, 10, 16, 20, 32, 50]:
            assert bad_len not in CCR_HASH_WIDTHS, f"CCR_HASH_WIDTHS should not contain {bad_len}"


class TestScanForMarkersSmartCrusher:
    """Test #1: scan_for_markers detects 12-char SmartCrusher hashes."""

    def _make_tool_result_message(self, content: str) -> dict:
        """Create an Anthropic-format tool_result message containing content."""
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": content,
                }
            ],
        }

    def test_scan_detects_smartcrusher_row_drop_marker(self):
        """scan_for_markers detects <<ccr:HASH N_rows_offloaded>> with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH} 42_rows_offloaded>>"
        message = self._make_tool_result_message(f"Here is some output: {marker} check this")
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in detected hashes, got: {detected}"
        )

    def test_scan_detects_smartcrusher_bare_marker(self):
        """scan_for_markers detects <<ccr:HASH>> bare form with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH}>>"
        message = self._make_tool_result_message(f"Content with {marker} inside.")
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in bare marker, got: {detected}"
        )

    def test_scan_detects_smartcrusher_opaque_marker(self):
        """scan_for_markers detects <<ccr:HASH,KIND,SIZE>> with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH},blob,1.2kb>>"
        message = self._make_tool_result_message(f"Data: {marker}")
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in opaque marker, got: {detected}"
        )

    def test_scan_detects_smartcrusher_rows_index_marker(self):
        """scan_for_markers detects <<ccr:HASH#rows N_chunks>> with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH}#rows 7>>"
        message = self._make_tool_result_message(f"Index: {marker}")
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in rows index marker, got: {detected}"
        )

    def test_scan_still_detects_canonical_24char_hash(self):
        """scan_for_markers still detects 24-char canonical hashes in <<ccr:>> form."""
        marker = f"<<ccr:{CANONICAL_HASH}>>"
        message = self._make_tool_result_message(f"Content: {marker}")
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert CANONICAL_HASH in detected, (
            f"Expected 24-char hash '{CANONICAL_HASH}' in detected, got: {detected}"
        )

    def test_scan_detects_both_hashes_in_same_message(self):
        """scan_for_markers detects both 12-char and 24-char hashes."""
        text = (
            f"Dropped: <<ccr:{SMARTCRUSHER_HASH} 5_rows_offloaded>> "
            f"Blob: <<ccr:{CANONICAL_HASH},json,2.3kb>>"
        )
        message = self._make_tool_result_message(text)
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert SMARTCRUSHER_HASH in detected
        assert CANONICAL_HASH in detected

    def test_scan_no_duplicate_hashes(self):
        """scan_for_markers does not produce duplicate hash entries."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH} 3_rows_offloaded>>"
        text = f"{marker} and again {marker}"
        message = self._make_tool_result_message(text)
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert detected.count(SMARTCRUSHER_HASH) == 1

    def test_scan_still_detects_bracket_form(self):
        """Existing bracket [N items compressed to M. Retrieve more: hash=HASH] still works."""
        bracket_hash = "aabbccddeeff001122334455"  # 24-char
        text = f"[10 items compressed to 3. Retrieve more: hash={bracket_hash}]"
        message = {"role": "user", "content": text}
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        assert bracket_hash in detected, f"Bracket-form hash not detected. Got: {detected}"


class TestToolInjectionWithSmartCrusherMarkers:
    """Test #2: inject_tool_definition injects furl_retrieve when only <<ccr:>> markers present."""

    def test_inject_triggered_by_smartcrusher_marker_only(self):
        """inject_tool_definition injects tool when only <<ccr:HASH>> markers are present (no bracket form)."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH} 10_rows_offloaded>>"
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_02",
                        "content": f"Analysis: {marker}",
                    }
                ],
            }
        ]
        injector = CCRToolInjector(provider="anthropic")
        injector.scan_for_markers(messages)

        assert injector.has_compressed_content, (
            "has_compressed_content should be True after scanning message with <<ccr:>> marker"
        )

        tools, was_injected = injector.inject_tool_definition(None)
        assert was_injected, "Tool should have been injected"
        assert any(t.get("name") == CCR_TOOL_NAME for t in tools), (
            f"furl_retrieve tool not found in injected tools: {tools}"
        )

    def test_no_injection_without_markers(self):
        """inject_tool_definition does NOT inject when no markers are present."""
        messages = [{"role": "user", "content": "Plain message without any markers"}]
        injector = CCRToolInjector()
        injector.scan_for_markers(messages)

        assert not injector.has_compressed_content
        tools, was_injected = injector.inject_tool_definition(None)
        assert was_injected is False
        assert tools == []

    def test_process_request_with_smartcrusher_marker(self):
        """process_request injects tool when message has <<ccr:HASH>> marker."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH} 5_rows_offloaded>>"
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_03",
                        "content": f"Result: {marker}",
                    }
                ],
            }
        ]
        injector = CCRToolInjector(provider="anthropic")
        _, updated_tools, was_injected = injector.process_request(messages, tools=None)

        assert was_injected
        assert updated_tools is not None
        assert any(t.get("name") == CCR_TOOL_NAME for t in updated_tools)


class TestParseToolCallSmartCrusherHash:
    """Test #3: parse_tool_call accepts 12-char hashes, rejects invalid ones."""

    def _make_anthropic_tool_call(self, hash_val: str, query: str | None = None) -> dict:
        """Create an Anthropic-format tool call dict."""
        input_data: dict = {"hash": hash_val}
        if query is not None:
            input_data["query"] = query
        return {
            "name": CCR_TOOL_NAME,
            "input": input_data,
        }

    def test_parse_12char_hash_returns_hash_and_query(self):
        """parse_tool_call returns (hash, query) for a valid 12-char hash."""
        tool_call = self._make_anthropic_tool_call(SMARTCRUSHER_HASH, "error logs")
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert hash_key == SMARTCRUSHER_HASH, f"Expected '{SMARTCRUSHER_HASH}', got '{hash_key}'"
        assert query == "error logs"

    def test_parse_12char_hash_no_query(self):
        """parse_tool_call returns (hash, None) for 12-char hash without query."""
        tool_call = self._make_anthropic_tool_call(SMARTCRUSHER_HASH)
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert hash_key == SMARTCRUSHER_HASH
        assert query is None

    def test_parse_24char_canonical_hash_still_accepted(self):
        """parse_tool_call still accepts 24-char canonical hashes."""
        tool_call = self._make_anthropic_tool_call(CANONICAL_HASH, "search term")
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert hash_key == CANONICAL_HASH
        assert query == "search term"

    def test_parse_10char_hash_rejected(self):
        """parse_tool_call rejects a 10-char hash (not in CCR_HASH_WIDTHS)."""
        short_hash = "1a2b3c4d5e"  # 10 chars
        tool_call = self._make_anthropic_tool_call(short_hash)
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert (hash_key, query) == (None, None), (
            f"10-char hash should be rejected, got ({hash_key}, {query})"
        )

    def test_parse_non_hex_hash_rejected(self):
        """parse_tool_call rejects a hash with non-hex characters."""
        non_hex_hash = "9f3a2b1c4xyz"  # 12 chars but contains non-hex
        tool_call = self._make_anthropic_tool_call(non_hex_hash)
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert (hash_key, query) == (None, None), (
            f"Non-hex hash should be rejected, got ({hash_key}, {query})"
        )

    def test_parse_openai_format_12char_hash(self):
        """parse_tool_call works for OpenAI format with 12-char hash."""
        import json

        tool_call = {
            "function": {
                "name": CCR_TOOL_NAME,
                "arguments": json.dumps({"hash": SMARTCRUSHER_HASH, "query": "test"}),
            }
        }
        hash_key, query = parse_tool_call(tool_call, provider="openai")
        assert hash_key == SMARTCRUSHER_HASH
        assert query == "test"


class TestSpoofGuardIntact:
    """Test #4: Spoof guard still rejects overlong and non-hex hashes."""

    def test_50char_hash_rejected_by_parse(self):
        """parse_tool_call rejects a 50-char hash (way too long)."""
        spoofed = "a" * 50
        tool_call = {
            "name": CCR_TOOL_NAME,
            "input": {"hash": spoofed},
        }
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert (hash_key, query) == (None, None), (
            f"50-char hash should be rejected as spoofed, got ({hash_key}, {query})"
        )

    def test_all_z_hash_rejected_by_parse(self):
        """parse_tool_call rejects 'zzzzzzzzzzzz' (12 chars but non-hex)."""
        spoofed = "z" * 12
        tool_call = {
            "name": CCR_TOOL_NAME,
            "input": {"hash": spoofed},
        }
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert (hash_key, query) == (None, None), (
            f"Non-hex 12-char hash should be rejected, got ({hash_key}, {query})"
        )

    def test_16char_hash_rejected(self):
        """parse_tool_call rejects a 16-char hash (not in CCR_HASH_WIDTHS)."""
        bad_hash = "a1b2c3d4e5f6a1b2"  # 16 chars
        tool_call = {
            "name": CCR_TOOL_NAME,
            "input": {"hash": bad_hash},
        }
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert (hash_key, query) == (None, None)

    def test_32char_hash_rejected(self):
        """parse_tool_call rejects a 32-char hash (not in CCR_HASH_WIDTHS)."""
        bad_hash = "a" * 32
        tool_call = {
            "name": CCR_TOOL_NAME,
            "input": {"hash": bad_hash},
        }
        hash_key, query = parse_tool_call(tool_call, provider="anthropic")
        assert (hash_key, query) == (None, None)

    def test_scan_does_not_detect_non_hex_hash(self):
        """scan_for_markers does not detect <<ccr:>> with non-hex content."""
        # 12 chars but contains non-hex ('z')
        bad_marker = "<<ccr:zzzzzzzzzzzz 5_rows_offloaded>>"
        message = {"role": "user", "content": bad_marker}
        injector = CCRToolInjector()
        detected = injector.scan_for_markers([message])
        # Should not detect 'zzzzzzzzzzzz' since regex requires [a-f0-9]
        assert "zzzzzzzzzzzz" not in detected
