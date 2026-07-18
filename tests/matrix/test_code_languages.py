"""MATRIX · code-aware languages with ZERO prior coverage.

Scout gap §4.1: ``code_aware_compressor._LANG_CONFIGS`` declares 8 languages
(Python, JavaScript, TypeScript, Go, Rust, Java, C, C++) with real AST-node
extraction tables, but ``test_code_aware_compressor.py`` exercises only Python and
JavaScript. This register covers the SIX untested declared languages end-to-end
through the public ``compress`` / ``retrieve`` API.

Contract (``assert_text_lossless_byte_exact``): each source blob is recovered
byte-exact — whether the router passes it through unchanged or offloads it whole.
The point is NOT that code-aware compression fires (it may or may not for a given
blob); it is that source in these languages is never silently mangled or lost.
"""

from __future__ import annotations

import pytest

from tests.matrix import _matrix as m

# (id, generator) for the six declared-but-untested languages.
_LANGS = [
    ("typescript", m.typescript_source),
    ("go", m.go_source),
    ("rust", m.rust_source),
    ("java", m.java_source),
    ("c", m.c_source),
    ("cpp", m.cpp_source),
]


@pytest.mark.parametrize("lang_id, generator", _LANGS, ids=[c[0] for c in _LANGS])
def test_source_language_is_byte_exact_recoverable(lang_id, generator, salt) -> None:
    m.assert_text_lossless_byte_exact(generator(), salt=salt)


@pytest.mark.parametrize(
    "lang_id, generator",
    [c for c in _LANGS if c[0] != "typescript"],
    ids=[c[0] for c in _LANGS if c[0] != "typescript"],
)
def test_source_language_whole_offload_round_trips(lang_id, generator, salt) -> None:
    # These blobs offload whole (verified); pin the strong property: the surfaced
    # pointer reconstructs the source byte-exact, braces/indentation/all.
    src = m.salted(generator(), salt)
    result = m.run(src)
    assert result.ccr_hashes, f"{lang_id} at this size is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == src, f"{lang_id} source not recovered byte-exact"
