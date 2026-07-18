"""MATRIX · encoding / shape edge cases.

Proves the core promise across hostile byte/shape inputs an agent can realistically
read. Each cell was empirically classified against the public API and asserts the
matching documented contract:

* **whole-offload / passthrough byte-exact** (``assert_text_lossless_byte_exact``):
  ANSI-SGR logs, CRLF + mixed endings, CJK/emoji/ZWJ/combining/astral/bidi, NUL
  bytes, latin-1-sourced code points, base64 lines & one big base64 line, markdown
  with embedded code fences, deeply-nested JSON (>=100 levels), dotted keys, and
  empty / whitespace / single-char inputs — recovered byte-identical, no mangling.

* **fail-open, byte-exact + LOUD error** (``assert_text_failopen_byte_exact``): a
  multi-MB single line (tiktoken catastrophic backtracking) and a lone-surrogate
  string (UTF-8 encode error at the Rust FFI). ``compress`` returns the ORIGINAL
  unchanged with ``result.error`` set — no silent loss, the failure is surfaced.

* **MATRIX-02 (xfail)** — a public totality gap: ``CompressResult.ccr_hashes``
  raises ``TypeError`` on raw ``bytes`` message content instead of returning ``[]``.
"""

from __future__ import annotations

import pytest

from furl_ctx import compress
from tests.matrix import _matrix as m

# ─── whole-offload byte-exact (store the raw original; salt for cold isolation) ──

_OFFLOAD_SHAPES = [
    ("ansi_sgr_log", m.ansi_log),
    ("crlf_log", m.crlf_log),
    ("cjk_emoji_zwj_combining_astral_bidi", m.cjk_emoji_combining),
    ("nul_bytes", m.null_byte_text),
    ("bidi_override", m.bidi_override),
    ("base64_lines", m.base64_lines),
    ("latin1_sourced_codepoints", m.latin1_blob),
]


@pytest.mark.parametrize(
    "shape_id, generator", _OFFLOAD_SHAPES, ids=[c[0] for c in _OFFLOAD_SHAPES]
)
def test_offload_shape_is_byte_exact_recoverable(shape_id, generator, salt) -> None:
    m.assert_text_lossless_byte_exact(generator(), salt=salt)


def test_crlf_line_endings_are_preserved_not_normalized(salt) -> None:
    # A CR/LF normalizer would silently rewrite bytes. Pin exact \r\n survival.
    doc = m.salted(m.crlf_log(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "crlf log is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == doc
    assert recovered.count("\r\n") == doc.count("\r\n")
    assert recovered.count("\r\n") >= 300  # the fixture's CRLF terminators, intact


def test_unicode_extremes_survive_byte_exact(salt) -> None:
    # ZWJ family emoji, astral math alphanumerics, combining marks and bidi
    # overrides must round-trip code-point-exact (no NFC/NFD folding, no stripping).
    doc = m.salted(m.cjk_emoji_combining(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "unicode blob is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == doc
    assert "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466" in recovered  # ZWJ family
    assert "‮" in recovered and "̈" in recovered  # bidi override + combining diaeresis


# ─── passthrough byte-exact (below the offload floor / declined; used once each,
#     so no cache collision — no salt needed) ───────────────────────────────────

_PASSTHROUGH_SHAPES = [
    ("astral_plane_only", m.astral_only),
    ("base64_single_huge_line", m.base64_blob_single_line),
    ("markdown_with_code_fences", m.markdown_with_fences),
    ("deeply_nested_json_150_levels", m.deeply_nested_json),
    ("dotted_key_object", m.dotted_key_object),
]


@pytest.mark.parametrize(
    "shape_id, generator", _PASSTHROUGH_SHAPES, ids=[c[0] for c in _PASSTHROUGH_SHAPES]
)
def test_shape_is_byte_exact_recoverable(shape_id, generator) -> None:
    m.assert_text_lossless_byte_exact(generator())


def test_deeply_nested_json_does_not_crash_or_hang() -> None:
    # >=100 levels of nesting must not blow the recursive detector's stack nor
    # hang; the content comes back byte-exact (recovered whole).
    m.assert_text_lossless_byte_exact(m.deeply_nested_json(depth=150))


@pytest.mark.parametrize(
    "content",
    ["", " ", "x", "   \n\t  \n   "],
    ids=["empty", "one_space", "one_char", "whitespace"],
)
def test_tiny_and_empty_inputs_are_byte_exact(content) -> None:
    m.assert_text_lossless_byte_exact(content)


# ─── fail-open: byte-exact + LOUD error (no salt: fail-open stores nothing) ───


def test_huge_single_line_fails_open_byte_exact() -> None:
    # A 2 MB single line trips tiktoken's catastrophic regex backtracking. That
    # failure used to be PLATFORM-DIVERGENT — a hard raise on macOS (small stack)
    # vs a silent super-linear passthrough on CI Linux (bigger stack), where the
    # router handed the input back with error=None (MATRIX-03 silent decline).
    # The tokenizer now rejects any long same-class run BEFORE the encode
    # (TiktokenCounter._MAX_SAFE_SAME_CLASS_RUN), so the decline is LOUD and
    # DETERMINISTIC on every platform: the ValueError rides compress()'s fail-open
    # into result.error. error_contains="backtracking" is a substring the guard's
    # message guarantees on BOTH platforms (the guard names that same failure
    # mode and fires before the encode), so this assertion holds cross-platform —
    # no per-route substring split is needed.
    m.assert_text_failopen_byte_exact(
        m.huge_single_line(megabytes=2), error_contains="backtracking"
    )


def test_lone_surrogate_fails_open_byte_exact() -> None:
    # A lone surrogate cannot be UTF-8 encoded at the Rust FFI boundary.
    m.assert_text_failopen_byte_exact(m.lone_surrogate_text(), error_contains="surrogate")


# ─── MATRIX-02 — bytes content is a public totality gap ──────────────────────


def test_bytes_content_ccr_hashes_is_total() -> None:
    payload = bytes([0xFF, 0xFE, 0x00, 0x01]) * 500  # non-str, non-JSON-serializable
    result = compress([{"role": "user", "content": payload}], model="gpt-4o")
    # compress() itself tolerates non-str content (passes it through untouched):
    assert result.error is None
    assert result.messages[0]["content"] == payload  # byte-exact passthrough
    # The documented intent is "non-string content passes through untouched", so a
    # successfully-returned result's public ``ccr_hashes`` must be TOTAL (no markers
    # in bytes → []). It currently raises TypeError from json.dumps(content).
    assert result.ccr_hashes == []
