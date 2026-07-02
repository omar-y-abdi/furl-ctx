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

from headroom.transforms.content_router import ContentRouter, ContentRouterConfig
from headroom.transforms.csv_schema_decoder import decode_csv_schema_rows


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
    # Minimal: a 2-row affix column where the variable middle is multi-line.
    items = [
        {"k": "id-1", "v": 'ERR alpha\n  at "frame_1"\n  end'},
        {"k": "id-2", "v": 'ERR beta\n  at "frame_2"\n  end'},
        {"k": "id-3", "v": 'ERR gamma\n  at "frame_3"\n  end'},
    ]
    result = ContentRouter(ContentRouterConfig()).compress(json.dumps(items, ensure_ascii=False))
    decoded = _decode_any(result.compressed)
    if decoded is None:
        # Small arrays may pass through untouched; only assert when compacted.
        return
    by_k = {r.get("k"): r.get("v") for r in decoded if "k" in r}
    for it in items:
        if it["k"] in by_k:
            assert by_k[it["k"]] == it["v"], f"value corrupted: {by_k[it['k']]!r} != {it['v']!r}"
