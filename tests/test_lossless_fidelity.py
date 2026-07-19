"""Type- and byte-fidelity of the lossless columnar tier (T1 / T2 / T12).

These are reproduction-first regression tests for three silent
value-corruption defects found by the lossless-fidelity pre-mortem audit.
Each drives the REAL Rust compaction render (``ContentRouter`` with the
``lossless-first`` policy) through the reference decoder
(``furl_ctx.transforms.csv_schema_decoder.decode_csv_schema_rows``) and
asserts exact round-trip of both type AND bytes — the engine's own
contract: decode byte-exact OR decline, never ship unverifiable bytes.

The defects (all reproduced against v1.3.0 before the fix):

* **T1 — mixed-type column coercion.** A column mixing a string ``"200"``
  and an int ``500`` is tagged ``json``; ``csv_render_str`` renders the
  string bare, byte-identical to the int on the wire, so the decoder's
  ``json.loads`` coerces ``"200"`` -> ``200``, ``"true"`` -> ``True``,
  ``"null"`` -> ``None``. No CCR marker, unrecoverable.

* **T2 — stringified-JSON field corruption.** A value that ORIGINATED as a
  string but happens to parse as JSON was deserialized (and, for objects,
  flattened into dotted columns) so the original string field vanished;
  array-strings lost their interior whitespace on re-serialization.

* **T12 — dotted-key collision.** A literal top-level ``"m.k"`` plus a
  nested ``{"m": {"k": ...}}`` synthesize two ``m.k`` columns; the decoder
  silently overwrites one value.

The fix guarantees: mixed scalar columns quote string cells so the decoder
reads them back verbatim; container-looking strings that cannot be
disambiguated in a ``json`` column decline to the recoverable tier;
string-origin values are never deserialized or flattened; and a flatten
that would collide with an existing column is skipped (with the decoder
failing loud on any duplicate column names).
"""

from __future__ import annotations

import json

from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows

# Force the lossless-first path so these corpora exercise the lossless
# CSV-schema tier directly (mirrors tests/test_csv_schema_decoder_roundtrip_fuzz).
_LOSSLESS_FIRST = ContentRouterConfig(smart_crusher_routing_policy="lossless-first")

# A long constant column guarantees the render clears the 30% byte-savings
# gate so every corpus below routes through the lossless CSV-schema tier.
_SERVICE = "auth-service-primary-eu-central-1.internal.example.com"


def _compress_to_text(items: list) -> str:
    """Compress *items* via the lossless-first path and return the CSV text.

    The engine returns a JSON string (CSV-schema text) or a JSON list (small
    arrays that did not compress); the latter is returned serialised so the
    caller can detect "not a CSV table" via ``decode_csv_schema_rows -> None``.
    """
    result = ContentRouter(_LOSSLESS_FIRST).compress(json.dumps(items, ensure_ascii=False))
    rendered = result.compressed
    try:
        parsed = json.loads(rendered)
    except (json.JSONDecodeError, ValueError):
        return rendered
    if isinstance(parsed, str):
        return parsed
    return rendered


# ────────────────────────────── T2 ────────────────────────────────────────────


def test_stringified_json_field_round_trips_as_string() -> None:
    """T2: a field whose value ORIGINATED as a JSON-object string must decode
    back to that exact string, never a dict and never dotted columns.

    Before the fix ``classify_string`` tagged the value ``StringifiedJson``,
    ``cell_from_value`` stored the PARSED object, and ``flatten_uniform_nested``
    promoted it to a ``payload.a`` column — the ``payload`` string vanished.
    """
    items = [{"id": i, "service": _SERVICE, "payload": f'{{"a": {i}}}'} for i in range(60)]

    text = _compress_to_text(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None, (
        f"expected a lossless CSV-schema table; got non-table output:\n{text[:200]}"
    )

    assert rows == items, (
        "stringified-JSON object field was deserialized/flattened instead of "
        "kept as the original string.\n"
        f"decoded[0] keys: {sorted(rows[0].keys())}\n"
        f"decoded[0].get('payload'): {rows[0].get('payload')!r}\n"
        f"Rendered (first 200):\n{text[:200]}"
    )
    assert rows[0]["payload"] == '{"a": 0}' and isinstance(rows[0]["payload"], str), rows[0].get(
        "payload"
    )


def test_array_string_preserves_exact_bytes() -> None:
    """T2: a field whose value is a JSON-array STRING must decode back byte-for
    -byte, interior whitespace included — never a parsed list.

    Before the fix the array-string was parsed and re-serialized compact, so
    ``'[1, 2, 0]'`` came back as ``'[1,2,0]'`` (spaces silently dropped).
    """
    items = [{"id": i, "service": _SERVICE, "arr": f"[1, 2, {i}]"} for i in range(60)]

    text = _compress_to_text(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None, (
        f"expected a lossless CSV-schema table; got non-table output:\n{text[:200]}"
    )

    assert rows == items, (
        "array-string field lost bytes on round-trip (whitespace stripped or "
        "parsed to a list).\n"
        f"decoded[0].get('arr'): {rows[0].get('arr')!r}\n"
        f"Rendered (first 200):\n{text[:200]}"
    )
    assert rows[0]["arr"] == "[1, 2, 0]" and isinstance(rows[0]["arr"], str), rows[0].get("arr")
