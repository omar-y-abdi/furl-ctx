"""Tabular ingestion: raw CSV → records → SmartCrusher.

Covers the four contract planes:

* **Detection triples** — clean CSV detects (as JSON_ARRAY with the
  ``tabular_csv`` provenance flag); prose and genuine JSON do not.
* **Conversion** — header row as keys, RFC-4180 parsing via the csv
  stdlib, exact-round-trip numeric coercion (a coerced value always
  renders back to the original cell bytes).
* **Recovery invariant** — the ORIGINAL RAW CSV BYTES are retrievable
  from the CCR store under the shipped marker's exact hash; a store
  veto serves the raw bytes with no dangling marker.
* **Routing** — pinned through the REAL router (real Rust crusher, real
  store): floors and ambiguous sniffs fail open to passthrough, and a
  detected table never falls through to the lossy LOG fallback.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr.marker_grammar import BRACKET_RETRIEVE_PATTERN
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms import csv_ingest
from furl_ctx.transforms.content_detector import ContentType, detect_content_type
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.csv_ingest import (
    _coerce_cell,
    raw_recovery_hash,
    sniff_csv,
)
from furl_ctx.transforms.router_policy import CompressionStrategy

# Real token counter (COR-17): apply() always threads one in production;
# whitespace word counts undercount low-whitespace CSV so badly that the
# tabular savings gate would never pass under the word proxy.
_COUNT = get_tokenizer("claude-sonnet-4-5-20250929").count_text


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    yield
    reset_compression_store()


# ─── Fixtures ────────────────────────────────────────────────────────────────


def structured_csv(rows: int = 20) -> str:
    """A fold-friendly table: arithmetic id, constant status, %3-scale
    floats, shared-prefix endpoints — the shapes SmartCrusher compacts."""
    lines = ["id,status,latency_ms,endpoint"]
    for i in range(rows):
        lines.append(f"{i + 1},ok,0.{53 + i:03d},api/v1/resource{i}")
    return "\n".join(lines) + "\n"


PROSE = (
    "The scheduler processed customer records before the morning deadline. "
    "Meanwhile, the ingestion service, which had been throttled, kept growing. "
    "Nobody noticed the retry queue, the dashboards, or the overflow traffic. "
    "A background daemon archived payment batches without operator intervention. "
    "The on-call engineer confirmed everything stayed green despite packet loss."
)

JSON_ARRAY_TEXT = json.dumps(
    [{"id": i, "status": "ok", "latency_ms": 0.053 + i / 1000} for i in range(10)]
)


# ─── Detection triples ───────────────────────────────────────────────────────


def test_detection_clean_csv_yes() -> None:
    result = detect_content_type(structured_csv())
    assert result.content_type is ContentType.JSON_ARRAY
    assert result.metadata["tabular_csv"] is True
    assert result.metadata["row_count"] == 20
    assert result.metadata["delimiter"] == ","


def test_detection_prose_no() -> None:
    result = detect_content_type(PROSE)
    assert result.metadata.get("tabular_csv") is None
    assert result.content_type is ContentType.PLAIN_TEXT


def test_detection_json_no() -> None:
    result = detect_content_type(JSON_ARRAY_TEXT)
    assert result.content_type is ContentType.JSON_ARRAY
    # Genuine JSON keeps the historical detection path + metadata.
    assert result.metadata.get("tabular_csv") is None
    assert result.metadata.get("is_dict_array") is True


def test_detection_tab_and_semicolon_delimiters() -> None:
    for delim in ("\t", ";"):
        content = structured_csv().replace(",", delim)
        table = sniff_csv(content)
        assert table is not None, f"delimiter {delim!r} must sniff"
        assert table.delimiter == delim


def test_detection_ambiguous_delimiter_tie_fails_open() -> None:
    # Comma and semicolon are equally consistent with equal field counts:
    # an ambiguous sniff must return None (fail-open). Fixture sits above
    # the byte floor so the TIE rule (not the floor) is what bites.
    content = "\n".join(["header-one,header;two"] + ["alpha-value,beta;gamma"] * 12)
    assert len(content) >= csv_ingest.MIN_BYTES
    assert sniff_csv(content) is None


# ─── Floors + ambiguity (fail-open) ──────────────────────────────────────────


def test_floor_min_rows() -> None:
    # 4 data rows — padded past the byte floor so only the row floor bites.
    content = structured_csv(rows=4).replace("resource", "resource-with-long-name")
    assert len(content) >= csv_ingest.MIN_BYTES
    assert sniff_csv(content) is None


def test_floor_min_bytes() -> None:
    content = "a,b\n" + "\n".join(f"{i},{i}" for i in range(6)) + "\n"
    assert len(content) < csv_ingest.MIN_BYTES
    assert sniff_csv(content) is None


def test_ragged_rows_fail_open() -> None:
    content = structured_csv()
    content = content.replace("2,ok,0.054,api/v1/resource1", "2,ok,0.054,api/v1/resource1,EXTRA")
    assert sniff_csv(content) is None


def test_duplicate_header_fails_open() -> None:
    content = structured_csv().replace("id,status,latency_ms,endpoint", "id,id,latency_ms,endpoint")
    assert sniff_csv(content) is None


def test_empty_header_cell_fails_open() -> None:
    content = structured_csv().replace("id,status,latency_ms,endpoint", "id,,latency_ms,endpoint")
    assert sniff_csv(content) is None


def test_all_numeric_header_fails_open() -> None:
    # A headerless numeric table must not have keys invented from data.
    lines = ["1,2,3.5,4"] + [f"{i},{i * 2},{i * 3}.5,{i}" for i in range(2, 30)]
    content = "\n".join(lines)
    assert len(content) >= csv_ingest.MIN_BYTES
    assert sniff_csv(content) is None


# ─── Conversion ──────────────────────────────────────────────────────────────


def test_conversion_header_keys_and_typed_records() -> None:
    table = sniff_csv(structured_csv())
    assert table is not None
    assert table.row_count == 20
    first = table.records[0]
    assert first == {"id": 1, "status": "ok", "latency_ms": 0.053, "endpoint": "api/v1/resource0"}


def test_conversion_round_trip_cell_exact() -> None:
    """Every coerced value renders back to the exact original cell bytes —
    the conversion never misrepresents a cell."""
    table = sniff_csv(structured_csv())
    assert table is not None
    raw_rows = [ln.split(",") for ln in structured_csv().strip().splitlines()[1:]]
    keys = ["id", "status", "latency_ms", "endpoint"]
    for record, raw in zip(table.records, raw_rows):
        for key, cell in zip(keys, raw):
            value = record[key]
            rendered = repr(value) if isinstance(value, float) else str(value)
            assert rendered == cell


def test_conversion_rfc4180_quoted_cells() -> None:
    # Quoting the naive line-sniff cannot see must stay within the 10%
    # slack: 18 plain rows, one row with a QUOTED comma, one row with a
    # QUOTED embedded newline (two aberrant physical lines out of 22).
    # The real csv parser then reassembles both quoted cells exactly.
    lines = ["name,note"]
    for i in range(18):
        lines.append(f"user{i},plain note {i}")
    lines.append('user18,"quoted, with comma"')
    lines.append('user19,"first\nsecond"')
    content = "\n".join(lines)
    assert len(content) >= csv_ingest.MIN_BYTES
    table = sniff_csv(content)
    assert table is not None
    assert table.row_count == 20
    assert table.records[18]["note"] == "quoted, with comma"
    assert table.records[19]["note"] == "first\nsecond"


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("12", 12),
        ("-3", -3),
        ("0.053", 0.053),
        ("007", "007"),  # leading zero: not round-trip-exact as int
        ("1.10", "1.10"),  # trailing zero: not round-trip-exact as float
        ("1e3", "1e3"),  # exponent spelling: repr differs
        ("inf", "inf"),  # non-finite never coerces
        ("nan", "nan"),
        ("", ""),
        ("abc", "abc"),
        ("-", "-"),
    ],
)
def test_coercion_exact_round_trip_rule(cell: str, expected: object) -> None:
    assert _coerce_cell(cell) == expected
    assert type(_coerce_cell(cell)) is type(expected)


# ─── Recovery invariant (raw bytes, marker, veto) ────────────────────────────


def test_recovery_byte_exact_raw_csv_through_real_router() -> None:
    raw = structured_csv()
    router = ContentRouter()
    result = router.compress(raw, token_counter=_COUNT)

    assert result.strategy_used is CompressionStrategy.SMART_CRUSHER
    assert result.compressed != raw

    match = BRACKET_RETRIEVE_PATTERN.search(result.compressed)
    assert match is not None, f"raw-recovery marker missing: {result.compressed!r}"
    assert match.group(1) == "20"  # N = raw data rows
    hash_key = match.group(3)
    assert hash_key == raw_recovery_hash(raw)

    entry = get_compression_store().retrieve(hash_key)
    assert entry is not None, "marker hash must resolve in the store"
    assert entry.original_content == raw  # byte-exact raw CSV


def test_store_veto_serves_raw_csv_unchanged(monkeypatch) -> None:
    # persist failure ⇒ the marker must never ship dangling: raw passthrough.
    monkeypatch.setattr(csv_ingest, "persist_to_python_ccr", lambda *a, **k: False)
    raw = structured_csv()
    result = ContentRouter().compress(raw, token_counter=_COUNT)
    assert result.compressed == raw
    assert "hash=" not in result.compressed


# ─── Routing pins through the real router ────────────────────────────────────


def test_routing_pin_csv_routes_to_smart_crusher() -> None:
    raw = structured_csv()
    result = ContentRouter().compress(raw, token_counter=_COUNT)
    assert result.strategy_used is CompressionStrategy.SMART_CRUSHER
    assert result.strategy_chain == [CompressionStrategy.SMART_CRUSHER.value]
    assert len(result.routing_log) == 1
    assert result.routing_log[0].content_type is ContentType.JSON_ARRAY
    # The shipped render is smaller in real tokens than the raw CSV.
    assert _COUNT(result.compressed) < _COUNT(raw)


def test_small_csv_below_floors_keeps_old_routing() -> None:
    raw = "a,b\n1,2\n3,4\n5,6\n"
    result = ContentRouter().compress(raw, token_counter=_COUNT)
    assert result.compressed == raw  # passthrough, exactly as before


def test_uncompressible_csv_fails_open_to_raw_not_log_fallback() -> None:
    # Converts fine, but the record view cannot beat the raw bytes: the
    # dispatcher must serve the RAW CSV byte-exact — never the lossy LOG
    # fallback (no dropped lines, no markers of any shape). Cells are
    # lexically DISTINCT word pairs (no digit-only variation — that would
    # be honestly collapsible by SmartCrusher's masked dedup and ship).
    words_a = ["apricot", "monsoon", "velvet", "granite", "ember", "lagoon"]
    words_b = ["lantern", "drill", "compass", "orchid", "quarry", "saddle"]
    lines = ["left-column-name,right-column-name"]
    for i in range(6):
        lines.append(f"{words_a[i]}-{words_b[5 - i]}-cell,{words_b[i]}-{words_a[5 - i]}-cell")
    raw = "\n".join(lines) + "\n"
    assert len(raw) >= csv_ingest.MIN_BYTES
    assert sniff_csv(raw) is not None

    result = ContentRouter().compress(raw, token_counter=_COUNT)
    assert result.compressed == raw
    assert "<<ccr:" not in result.compressed
    assert "compressed to" not in result.compressed


def test_lossless_only_never_converts_csv() -> None:
    raw = structured_csv()
    router = ContentRouter(ContentRouterConfig(lossless_only=True))
    result = router.compress(raw, token_counter=_COUNT)
    assert result.compressed == raw


def test_compress_tabular_returns_none_when_no_savings() -> None:
    # Direct unit pin of the savings gate: a token counter that makes the
    # candidate look expensive forces the raw-bytes outcome.
    raw = structured_csv()
    table = sniff_csv(raw)
    assert table is not None

    class _Crusher:
        def crush(self, content: str, query: str = "", bias: float = 1.0):
            class _R:
                compressed = content  # no shrink at all

            return _R()

    shipped = csv_ingest.compress_tabular_csv(raw, table, _Crusher(), token_counter=len)
    assert shipped is None
