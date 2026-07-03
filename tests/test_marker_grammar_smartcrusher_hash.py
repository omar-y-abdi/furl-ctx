"""SmartCrusher 12-char hash support in the CCR marker grammar.

Repointed from the excised ``tool_injection`` module (SIMP-4): the scan
behavior under test lives in ``furl_ctx.ccr.marker_grammar`` (the
compiled consumer patterns), and the hash spoofing guard is
``marker_grammar.is_valid_ccr_hash`` (the MCP ``furl_retrieve``
handler's ingress check). The injector-specific surface
(``CCRToolInjector`` tool/system-message injection, provider tool-call
parsing) was deleted with its module.

These tests verify that the grammar handles the SmartCrusher
``<<ccr:HASH>>`` marker family with 12-hex-char hashes (sha256[:6] → 12
hex chars) in addition to the canonical 24-hex-char hashes.

SmartCrusher emits markers in these forms:
  <<ccr:HASH N_rows_offloaded>>   (space separator)
  <<ccr:HASH#rows N_chunks>>      (hash index form)
  <<ccr:HASH,KIND,SIZE>>          (opaque blob form)
  <<ccr:HASH>>                    (bare form)
"""

from __future__ import annotations

import re

from furl_ctx.ccr.marker_grammar import (
    CCR_TOOL_NAME,
    HASH_WIDTHS,
    is_valid_ccr_hash,
    marker_patterns,
)

# A real 12-char SmartCrusher hash (sha256[:6] -> 12 hex chars)
SMARTCRUSHER_HASH = "9f3a2b1c4d5e"
# A real 24-char canonical hash (sha256[:24])
CANONICAL_HASH = "a1b2c3d4e5f6a1b2c3d4e5f6"


def _scan(text: str) -> list[str]:
    """Union-extract over the owned consumer patterns.

    Mirrors the production scan semantics (every pattern, last capture
    group per match, deduped preserving first-seen order) — the same
    driver the characterization suite uses.
    """
    out: list[str] = []
    for pattern in marker_patterns():
        for match in pattern.findall(text):
            hash_key = match[-1] if isinstance(match, tuple) else match
            if hash_key and hash_key not in out:
                out.append(hash_key)
    return out


class TestCCRHashWidths:
    """Pin HASH_WIDTHS (formerly re-exported as CCR_HASH_WIDTHS)."""

    def test_hash_widths_contains_12(self):
        """HASH_WIDTHS must include 12 (SmartCrusher sha256[:6])."""
        assert 12 in HASH_WIDTHS

    def test_hash_widths_contains_24(self):
        """HASH_WIDTHS must include 24 (canonical store key)."""
        assert 24 in HASH_WIDTHS

    def test_hash_widths_is_frozenset(self):
        """HASH_WIDTHS must be a frozenset for immutability."""
        assert isinstance(HASH_WIDTHS, frozenset)

    def test_hash_widths_rejects_other_lengths(self):
        """HASH_WIDTHS must NOT accept arbitrary lengths."""
        for bad_len in [8, 10, 16, 20, 32, 50]:
            assert bad_len not in HASH_WIDTHS, f"HASH_WIDTHS should not contain {bad_len}"


class TestCcrToolName:
    """The retrieval tool name is wire contract (re-homed by SIMP-4)."""

    def test_tool_name_is_furl_retrieve(self):
        assert CCR_TOOL_NAME == "furl_retrieve"

    def test_tool_name_reexported_from_ccr_package(self):
        from furl_ctx.ccr import CCR_TOOL_NAME as reexported

        assert reexported == CCR_TOOL_NAME


class TestScanSmartCrusherMarkers:
    """The grammar detects 12-char SmartCrusher hashes in every shape."""

    def test_scan_detects_smartcrusher_row_drop_marker(self):
        """<<ccr:HASH N_rows_offloaded>> with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH} 42_rows_offloaded>>"
        detected = _scan(f"Here is some output: {marker} check this")
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in detected hashes, got: {detected}"
        )

    def test_scan_detects_smartcrusher_bare_marker(self):
        """<<ccr:HASH>> bare form with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH}>>"
        detected = _scan(f"Content with {marker} inside.")
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in bare marker, got: {detected}"
        )

    def test_scan_detects_smartcrusher_opaque_marker(self):
        """<<ccr:HASH,KIND,SIZE>> with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH},blob,1.2kb>>"
        detected = _scan(f"Data: {marker}")
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in opaque marker, got: {detected}"
        )

    def test_scan_detects_smartcrusher_rows_index_marker(self):
        """<<ccr:HASH#rows N_chunks>> with 12-char hash."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH}#rows 7>>"
        detected = _scan(f"Index: {marker}")
        assert SMARTCRUSHER_HASH in detected, (
            f"Expected 12-char hash '{SMARTCRUSHER_HASH}' in rows index marker, got: {detected}"
        )

    def test_scan_still_detects_canonical_24char_hash(self):
        """24-char canonical hashes in <<ccr:>> form still detected."""
        marker = f"<<ccr:{CANONICAL_HASH}>>"
        detected = _scan(f"Content: {marker}")
        assert CANONICAL_HASH in detected, (
            f"Expected 24-char hash '{CANONICAL_HASH}' in detected, got: {detected}"
        )

    def test_scan_detects_both_hashes_in_same_text(self):
        """Both 12-char and 24-char hashes surface from one text."""
        text = (
            f"Dropped: <<ccr:{SMARTCRUSHER_HASH} 5_rows_offloaded>> "
            f"Blob: <<ccr:{CANONICAL_HASH},json,2.3kb>>"
        )
        detected = _scan(text)
        assert SMARTCRUSHER_HASH in detected
        assert CANONICAL_HASH in detected

    def test_scan_no_duplicate_hashes(self):
        """The union-extract does not produce duplicate hash entries."""
        marker = f"<<ccr:{SMARTCRUSHER_HASH} 3_rows_offloaded>>"
        detected = _scan(f"{marker} and again {marker}")
        assert detected.count(SMARTCRUSHER_HASH) == 1

    def test_scan_still_detects_bracket_form(self):
        """[N items compressed to M. Retrieve more: hash=HASH] still works."""
        bracket_hash = "aabbccddeeff001122334455"  # 24-char
        detected = _scan(f"[10 items compressed to 3. Retrieve more: hash={bracket_hash}]")
        assert bracket_hash in detected, f"Bracket-form hash not detected. Got: {detected}"


class TestSpoofGuard:
    """is_valid_ccr_hash — the retrieval-ingress width+charset guard."""

    def test_12char_hash_accepted(self):
        assert is_valid_ccr_hash(SMARTCRUSHER_HASH)

    def test_24char_canonical_hash_accepted(self):
        assert is_valid_ccr_hash(CANONICAL_HASH)

    def test_10char_hash_rejected(self):
        """A 10-char hash is not in HASH_WIDTHS."""
        assert not is_valid_ccr_hash("1a2b3c4d5e")

    def test_non_hex_hash_rejected(self):
        """A 12-char value with non-hex characters is rejected."""
        assert not is_valid_ccr_hash("9f3a2b1c4xyz")

    def test_50char_hash_rejected(self):
        """An overlong (spoofed) hash is rejected."""
        assert not is_valid_ccr_hash("a" * 50)

    def test_all_z_hash_rejected(self):
        """'zzzzzzzzzzzz' (12 chars but non-hex) is rejected."""
        assert not is_valid_ccr_hash("z" * 12)

    def test_16char_hash_rejected(self):
        assert not is_valid_ccr_hash("a1b2c3d4e5f6a1b2")

    def test_32char_hash_rejected(self):
        assert not is_valid_ccr_hash("a" * 32)

    def test_none_and_non_str_rejected(self):
        assert not is_valid_ccr_hash(None)
        assert not is_valid_ccr_hash(123456789012)

    def test_scan_does_not_detect_non_hex_hash(self):
        """The grammar does not extract <<ccr:>> with non-hex content."""
        detected = _scan("<<ccr:zzzzzzzzzzzz 5_rows_offloaded>>")
        assert "zzzzzzzzzzzz" not in detected

    def test_patterns_capture_only_strict_widths(self):
        """The double-angle pattern's width alternation is exactly {24, 12}."""
        double_angle = marker_patterns()[-1]
        assert isinstance(double_angle, re.Pattern)
        assert "{24}" in double_angle.pattern
        assert "{12}" in double_angle.pattern
