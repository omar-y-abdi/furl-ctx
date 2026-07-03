"""Regression: affix-folded columns whose cells contain embedded newlines.

The lossless CSV-schema path quotes any cell containing a newline/comma/quote
(RFC-4180). When such a cell also lives in an *affix-folded* column (a shared
prefix/suffix is hoisted out), the decoder must UNQUOTE the per-row cell before
re-applying the affix. An earlier fix made the line-reader quote-aware (rows no
longer shatter) but left the affix branch concatenating the still-quoted cell,
so the surrounding quote characters leaked into the reconstructed value —
silent corruption on the lossless path for an extremely common shape (multi-line
log messages / stack traces under a common prefix).

This exercises the engine end-to-end (real compaction render -> Python decoder)
and asserts byte-exact reconstruction, proven by sha256 over the full set.
"""

from __future__ import annotations

import hashlib
import json

from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows


def _canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decode_any(compressed: str) -> list[dict] | None:
    d = decode_csv_schema_rows(compressed)
    if d is not None:
        return d
    try:
        inner = json.loads(compressed)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(inner, str):
        return decode_csv_schema_rows(inner)
    return None


def test_affix_column_with_embedded_newline_roundtrips_byte_exact() -> None:
    # Homogeneous array -> lossless columnar path. Every `match` shares the
    # prefix "panic: nil deref at op_" and suffix ")" -> affix fold fires; the
    # middle contains newlines -> the cell is CSV-quoted.
    items = [
        {
            "file": f"pkg/svc_{i:03d}/handler.go",
            "line": 7 + i,
            "match": (
                f"panic: nil deref at op_{i}\n\tgoroutine {i} [running]:\n\tmain.handle(0x{i:x})"
            ),
        }
        for i in range(96)
    ]
    raw = json.dumps(items, ensure_ascii=False)
    result = ContentRouter(ContentRouterConfig()).compress(raw)

    decoded = _decode_any(result.compressed)
    assert decoded is not None, "expected the lossless CSV-schema path"

    original = [_canon(x) for x in items]
    recovered = {_canon(x) for x in decoded}
    missing = [c for c in original if c not in recovered]
    assert not missing, f"{len(missing)}/{len(items)} rows did not round-trip byte-exact"

    orig_digest = hashlib.sha256("\n".join(sorted(original)).encode()).hexdigest()
    rec_digest = hashlib.sha256(
        "\n".join(sorted(c for c in original if c in recovered)).encode()
    ).hexdigest()
    assert orig_digest == rec_digest


def test_affix_quote_chars_do_not_leak_into_value() -> None:
    """Embedded double-quotes inside an affix-folded cell survive byte-exactly.

    TEST-11: the old 3-row fixture never reached the lossless columnar path
    at all (small arrays pass through / route lossy at every size for that
    shape), so its `if decoded is None: return` + per-key conditional assert
    made the test pass vacuously forever. This fixture is shaped like the
    96-row digest test above (proven to take the CSV-schema path under the
    DEFAULT policy) with `"`-quoted frames added inside the folded column;
    every assert is unconditional.
    """
    items = [
        {
            "file": f"pkg/svc_{i:03d}/handler.go",
            "line": 7 + i,
            "match": (
                f'panic: nil deref at op_{i}\n\tat "frame_{i}" in scope\n\tmain.handle(0x{i:x})'
            ),
        }
        for i in range(96)
    ]
    result = ContentRouter(ContentRouterConfig()).compress(json.dumps(items, ensure_ascii=False))
    decoded = _decode_any(result.compressed)
    assert decoded is not None, (
        "expected the lossless CSV-schema path — the fixture no longer "
        "compacts and this regression test has gone vacuous"
    )

    by_key = {row.get("file"): row.get("match") for row in decoded}
    original = {item["file"]: item["match"] for item in items}
    assert set(by_key) == set(original), "every row must decode (no full-row drop)"
    for key, want in original.items():
        assert by_key[key] == want, f"value corrupted for {key}: {by_key[key]!r} != {want!r}"
    # The regression class this file exists for: the quote characters
    # belong INSIDE the value, not leaked as CSV artifacts.
    assert '"frame_5"' in by_key["pkg/svc_005/handler.go"]
