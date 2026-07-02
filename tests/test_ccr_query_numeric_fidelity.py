"""COR-54: query-path numeric fidelity in the CCR store.

The QUERY retrieval path re-parsed the stored canonical with ``json.loads``
and re-serialized matching items with ``json.dumps``: a stored ``1e400`` came
back as bare ``Infinity`` — RFC-invalid JSON rejected by strict parsers — and
high-precision decimals collapsed (``0.30000000000000004444`` →
``0.30000000000000004``), while the no-query path and ``ccr_get`` return
verbatim bytes. The engine's serde is configured with ``arbitrary_precision``
precisely to preserve these; the query path undid it.

Fix: ``_search_items_from_original`` detects float-round-trip loss (overflow
to inf, precision collapse, bare ``Infinity``/``NaN`` constants) and falls
back to the existing verbatim text-chunk path, so query results carry the
exact source literals. The MCP response additionally serializes with
``allow_nan=False`` and falls back to the byte-exact no-query envelope on
``ValueError`` (pinned in test_mcp_server_handlers.py).
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache.compression_store import CompressionStore

H = "abcdef123456"

# The two regression fixtures the finding names: an overflow literal and a
# 20-significant-digit decimal — both representable under serde
# arbitrary_precision, neither by a Python float.
OVERFLOW = '[{"id": 1, "reading": 1e400, "note": "alpha needle"}]'
HIGH_PRECISION = '[{"id": 1, "price": 0.30000000000000004444, "note": "alpha needle"}]'


def _reject_constant(name: str) -> float:
    raise AssertionError(f"bare {name} constant in serialized query results")


def _search(original: str) -> list:
    store = CompressionStore(max_entries=10)
    store.store(original=original, compressed=f"<<ccr:{H}>>", explicit_hash=H)
    # score_threshold=0.0 keeps the assertion about SHAPE deterministic on a
    # tiny corpus (BM25 scores on 1-2 items are not the subject here).
    return store.search(H, "needle", score_threshold=0.0)


def test_overflow_literal_not_returned_as_infinity() -> None:
    results = _search(OVERFLOW)
    assert results, "expected verbatim text chunks for the lossy canonical"
    dumped = json.dumps(results, allow_nan=False)  # must not raise
    json.loads(dumped, parse_constant=_reject_constant)  # strict round-trip
    assert "1e400" in dumped, f"source literal lost: {dumped}"


def test_20_significant_digit_decimal_preserved_verbatim() -> None:
    results = _search(HIGH_PRECISION)
    assert results
    dumped = json.dumps(results, allow_nan=False)
    json.loads(dumped, parse_constant=_reject_constant)
    assert "0.30000000000000004444" in dumped, f"precision collapsed: {dumped}"


def test_bare_infinity_constant_falls_back_to_verbatim_chunks() -> None:
    # Python's json PARSES bare Infinity/NaN, but re-emitting them is
    # RFC-invalid — such canonicals must also take the verbatim-chunk path.
    results = _search('[{"v": Infinity, "note": "alpha needle"}]')
    assert results
    assert all(item.get("type") == "text" for item in results)
    json.dumps(results, allow_nan=False)  # must not raise


@pytest.mark.parametrize(
    "fixture",
    [
        OVERFLOW,
        HIGH_PRECISION,
        '[{"v": Infinity}]',
        '[{"v": -Infinity}]',
        '[{"v": NaN}]',
    ],
)
def test_items_normalizer_serves_verbatim_chunks_for_lossy_numerics(fixture: str) -> None:
    store = CompressionStore(max_entries=10)
    items = store._search_items_from_original(fixture)
    assert [item["type"] for item in items] == ["text"], fixture
    assert items[0]["text"] == fixture  # byte-exact slice of the original


def test_roundtrippable_numerics_keep_structured_items() -> None:
    # Value-level check, not textual: 1e3 == 1000.0 (same number) and Python
    # ints are arbitrary-precision — neither may trigger the fallback. The
    # legacy structured-array result shape is preserved for them.
    store = CompressionStore(max_entries=10)
    original = (
        '[{"id": 1, "price": 0.5, "note": "alpha needle"},'
        ' {"id": 2, "price": 1e3, "big": 123456789012345678901234567890}]'
    )
    items = store._search_items_from_original(original)
    assert items == [
        {"id": 1, "price": 0.5, "note": "alpha needle"},
        {"id": 2, "price": 1000.0, "big": 123456789012345678901234567890},
    ]


def test_search_lossless_canonical_returns_structured_items() -> None:
    # End-to-end guard for the legacy result shape on lossless canonicals.
    store = CompressionStore(max_entries=10)
    original = '[{"id": 1, "price": 0.5, "note": "alpha needle"}, {"id": 2, "note": "beta"}]'
    store.store(original=original, compressed=f"<<ccr:{H}>>", explicit_hash=H)
    results = store.search(H, "needle", score_threshold=0.0)
    assert {"id": 1, "price": 0.5, "note": "alpha needle"} in results
