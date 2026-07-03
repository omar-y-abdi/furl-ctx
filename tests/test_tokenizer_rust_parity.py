"""TEST-8 (ARCH-6): cross-language tokenizer parity pins — Python twin.

The same corpus + expected counts are pinned in
``crates/furl-core/tests/tokenizer_python_parity.rs``. Each side asserts
against the same constants (computed once with Python ``tiktoken``, the
reference implementation, and the shared fixed-ratio estimation formula
``max(1, int(chars / cpt + 0.5))``), so a drift on either side of the
FFI fails its own suite — the two counts are transitively pinned to
agree without adding tokenizer FFI surface.

Families covered (the ones that SHOULD agree):
- OpenAI tiktoken: ``gpt-4o`` (o200k_base) and ``gpt-4`` (cl100k_base).
- Anthropic estimation at 3.5 chars/token.
- Gemini/Cohere estimation at 4.0 chars/token.

Known divergences deliberately NOT covered (documented in both
registries, see ``furl_ctx/tokenizers/registry.py`` and
``crates/furl-core/src/tokenizer/registry.rs``):
- unknown-model estimation (Python auto-detects density + overhead;
  Rust uses fixed 4.0);
- legacy OpenAI encoding corners (e.g. ``davinci-002``).
"""

from __future__ import annotations

import pytest

from furl_ctx.tokenizers import EstimatingTokenCounter, get_tokenizer
from furl_ctx.tokenizers.tiktoken_counter import TiktokenCounter

# ── Corpus (byte-identical to the Rust twin) ───────────────────────────────

ASCII = "The quick brown fox jumps over the lazy dog. 0123456789 taxes=42%"
CJK = "东京タワーは高いです。北京烤鸭很好吃。한국어 텍스트도 있다."
EMOJI = "Deploy status: ✅ rocket 🚀 then 👨‍👩‍👧‍👦 family emoji with ZWJ"
CODE = (
    "def count(xs):\n"
    "    return {k: len(v) for k, v in xs.items()}  # O(n)\n"
    'if __name__ == "__main__":\n'
    '    print(count({"a": [1, 2, 3]}))\n'
)


def _blob_100kb() -> str:
    """Deterministic ~100KB log-shaped blob (1819 lines, 100,045 chars).

    Identical construction to ``blob_100kb()`` in the Rust twin — the
    pinned counts below are for THESE exact bytes.
    """
    lines: list[str] = []
    total = 0
    i = 0
    while total < 100_000:
        line = f"Line {i:04}: status=ok latency_ms={i % 250:03} path=/api/v1/items\n"
        lines.append(line)
        total += len(line)
        i += 1
    return "".join(lines)


# (name, text, gpt-4o/o200k, gpt-4/cl100k, est@3.5, est@4.0)
_CORPUS: list[tuple[str, str, int, int, int, int]] = [
    ("ascii", ASCII, 19, 19, 19, 16),
    ("cjk", CJK, 24, 34, 9, 8),
    ("emoji", EMOJI, 25, 33, 17, 15),
    ("code", CODE, 49, 49, 37, 33),
    ("blob_100kb", _blob_100kb(), 34561, 34561, 28584, 25011),
]

_IDS = [c[0] for c in _CORPUS]


def test_blob_construction_matches_rust_twin() -> None:
    blob = _blob_100kb()
    assert len(blob) == 100_045
    assert blob.count("\n") == 1_819


@pytest.mark.parametrize(("name", "text", "want_4o", "want_4", "_e35", "_e40"), _CORPUS, ids=_IDS)
def test_tiktoken_counts_match_rust_reference(
    name: str, text: str, want_4o: int, want_4: int, _e35: int, _e40: int
) -> None:
    assert TiktokenCounter("gpt-4o").count_text(text) == want_4o, f"gpt-4o/o200k: {name}"
    assert TiktokenCounter("gpt-4").count_text(text) == want_4, f"gpt-4/cl100k: {name}"


@pytest.mark.parametrize(("name", "text", "_w4o", "_w4", "want_35", "want_40"), _CORPUS, ids=_IDS)
def test_estimation_counts_match_rust_formula(
    name: str, text: str, _w4o: int, _w4: int, want_35: int, want_40: int
) -> None:
    # The registry's Anthropic/Gemini/Cohere factories return FIXED-ratio
    # estimators — the mode that agrees across the FFI.
    assert EstimatingTokenCounter(chars_per_token=3.5).count_text(text) == want_35, name
    assert EstimatingTokenCounter(chars_per_token=4.0).count_text(text) == want_40, name


@pytest.mark.parametrize(("name", "text", "_w4o", "_w4", "want_35", "want_40"), _CORPUS, ids=_IDS)
def test_registry_dispatch_uses_the_agreeing_fixed_ratio_counters(
    name: str, text: str, _w4o: int, _w4: int, want_35: int, want_40: int
) -> None:
    """The registry's family dispatch (not just the raw counters) lands on
    the fixed-ratio calibrations the Rust registry mirrors."""
    assert get_tokenizer("claude-sonnet-4-6").count_text(text) == want_35, name
    assert get_tokenizer("gemini-1.5-pro").count_text(text) == want_40, name
    assert get_tokenizer("command-r-plus").count_text(text) == want_40, name
