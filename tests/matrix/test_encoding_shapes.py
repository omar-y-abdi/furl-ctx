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

The THREE OFFLOADING tests below (``test_offload_shape_is_byte_exact_recoverable``
over its 7 hostile shapes, plus the CRLF and unicode-extremes pins) run against
BOTH CCR backends via an indirect ``ccr_backend`` parametrize: the in-memory
store (a raw-``str`` identity) AND the durable ``sqlite`` backend production
runs — whose ``surrogatepass`` BLOB round-trip (``cache/backends/sqlite.py``) is
the exact operation a NUL-truncation or mis-decode defect would live in. The
hostile-byte offload shapes (NUL, latin-1, CJK, bidi, CRLF) are meaningless as a
fidelity proof unless they pass through that backend, so they now do.

The remaining tests here stay memory-only, deliberately: passthrough shapes,
tiny/empty inputs, the deeply-nested-JSON case, the two fail-open cases (which
store NOTHING by contract) and the bytes-content totality gap never WRITE the CCR
store, so the backend is unobservable to them — a sqlite leg would be a duplicate
test that cannot fail differently, i.e. cost without coverage. Backend fidelity
here is exactly as wide as the set of tests that actually persist something.
"""

from __future__ import annotations

import pytest

from furl_ctx import compress
from tests.matrix import _matrix as m

# Both CCR backends for the tests that actually WRITE the store (see the module
# docstring). Applied per-test, indirect: it feeds ``tests/matrix/conftest.py``'s
# ``ccr_backend`` fixture, which the autouse ``_isolate_ccr_store`` consumes to
# pin ``FURL_CCR_BACKEND``. Each leg gets its own per-test ``ccr.sqlite3`` (fresh
# ``FURL_WORKSPACE_DIR``), and the per-test ``salt`` is the node name — which
# carries the backend as its LAST id component (``[nul_bytes-sqlite]``,
# ``[crlf_log-sqlite]``, or bare ``[sqlite]`` where this is the only parametrize)
# — so the two legs salt differently, every offload stays a cold compress, and
# they never collide in the process-global Rust store.
_BOTH_BACKENDS = pytest.mark.parametrize("ccr_backend", ["memory", "sqlite"], indirect=True)


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


@_BOTH_BACKENDS
@pytest.mark.parametrize(
    "shape_id, generator", _OFFLOAD_SHAPES, ids=[c[0] for c in _OFFLOAD_SHAPES]
)
def test_offload_shape_is_byte_exact_recoverable(shape_id, generator, salt, ccr_backend) -> None:
    result = m.assert_text_lossless_byte_exact(generator(), salt=salt)
    # The helper proved byte-exact store recovery for whatever surfaced — but it
    # is PATH-AGNOSTIC and also accepts a byte-exact PASSTHROUGH. That loophole is
    # exactly how a backend that mangles hostile bytes hides: the router's
    # ccr_offload does a store round-trip verify (``router_engine`` — store, read
    # back, compare) and, on a readback mismatch, KEEPS THE ORIGINAL (passthrough)
    # — no silent loss, but no offload either. These shapes are sized to offload
    # and DO on the memory backend; requiring the offload makes the sqlite leg go
    # RED if the ``surrogatepass`` BLOB round-trip ever stops reproducing
    # NUL/latin-1/CJK bytes (the whole reason these tests now run on sqlite),
    # instead of silently degrading to a passthrough this assertion would wave
    # through. It is what gives the deliberate-break test its teeth.
    #
    # FALSE-RED VECTORS, so a future failure here is read correctly. The offload
    # gate is BOTH ``len(content) >= _OFFLOAD_MIN_CHARS`` (4000) AND
    # ``compression_ratio >= _OFFLOAD_TRIGGER_RATIO`` (0.9) —
    # ``router_engine._should_ccr_offload``. The char margin is comfortable
    # (thinnest is nul_bytes at ~6.6k, 1.6x), so the floor would have to more
    # than half again before it bites. The RATIO gate is the live risk: 0.9
    # means "nothing upstream meaningfully shrank this", so teaching any
    # upstream strategy to compress NUL-delimited rows or bidi text stops the
    # offload firing and reddens this assertion on content that is still
    # perfectly byte-exact. That is a routing change, NOT a fidelity
    # regression — retune the fixture or move the shape to the passthrough
    # family; do not weaken the assertion.
    assert result.ccr_hashes, (
        f"{shape_id} did not offload on the {ccr_backend} backend: the CCR store "
        f"round-trip verify rejected it, so these hostile bytes are NOT faithfully "
        f"stored+recovered by this backend (a mangling backend degrades to a "
        f"passthrough here rather than losing data — but the offload contract is "
        f"broken). If an upstream strategy just learned to compress this shape, see "
        f"the _OFFLOAD_TRIGGER_RATIO note above: that is a routing change, not a "
        f"fidelity regression"
    )


@_BOTH_BACKENDS
def test_crlf_line_endings_are_preserved_not_normalized(salt, ccr_backend) -> None:
    # A CR/LF normalizer would silently rewrite bytes. Pin exact \r\n survival.
    doc = m.salted(m.crlf_log(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, f"crlf log is expected to offload ({ccr_backend} backend)"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == doc
    assert recovered.count("\r\n") == doc.count("\r\n")
    assert recovered.count("\r\n") >= 300  # the fixture's CRLF terminators, intact


@_BOTH_BACKENDS
def test_unicode_extremes_survive_byte_exact(salt, ccr_backend) -> None:
    # ZWJ family emoji, astral math alphanumerics, combining marks and bidi
    # overrides must round-trip code-point-exact (no NFC/NFD folding, no stripping).
    doc = m.salted(m.cjk_emoji_combining(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, f"unicode blob is expected to offload ({ccr_backend} backend)"
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
