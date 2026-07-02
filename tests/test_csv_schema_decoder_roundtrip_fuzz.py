"""RFC-4180 quote-aware round-trip fuzz tests for csv_schema_decoder.

Three test families:

1. **Cluster-A regression** (targeted): a row containing a cell with an
   embedded newline must survive the full Rust-compact → Python-decode
   pipeline with zero loss.  Before the fix, ``text.split('\\n')`` at
   decoder line 325 shattered these logical rows into multiple physical
   lines, producing ``len(parts) != len(var_cols)`` at line 403, silently
   dropping rows on the lossless path with no ``_ccr_dropped`` sentinel.

2. **Property/fuzz** (200 adversarial cases, fixed seed): fresh
   out-of-sample rows with adversarial cell shapes — embedded newlines,
   commas-in-cells, embedded double-quotes, JSON nulls, absent keys,
   empty strings, unicode — feed through the real Rust compaction render →
   ``decode_csv_schema_rows`` and assert deep equality (or, when the Rust
   path takes the lossy/opaque branch, assert the ``_ccr_dropped``
   sentinel is present and the item is covered by a recoverable hash).
   ``null`` / absent-key / ``""`` are kept distinct via the reserved
   ``__null__`` / ``__missing__`` cell sentinels.

3. **COR-13 decoder-coverage honesty** (heterogeneous + nested-object
   shapes): the reference decoder proves flat-scalar tables only, so
   ``Compaction::Buckets`` renders and ``CellValue::Nested`` cells must
   be DECLINED from the lossless tier (fail-closed), while object/array
   cells in ``json``-tagged columns of a flat table must round-trip via
   ``json.loads``.  Contract: decode byte-exact OR decline — never ship
   unverifiable bytes under the lossless claim.

Note on "colons-in-keys": the CSV-schema formatter never quotes COLUMN
NAMES — only cells (the ``kv_field_name`` quoting belongs to the
Markdown-KV formatter).  A key containing ``:`` ``,`` ``{`` ``}`` ``=``
``"`` or CR/LF — or starting with the reserved ``__`` marker prefix —
cannot be emitted raw without corrupting the ``name:type`` header /
preamble grammar, so the Rust compactor DECLINES compaction for such
arrays (COR-15, fail-closed): they ship as verbatim JSON instead of a
silently mis-keyed table.  The fuzz generator therefore keeps colons in
VALUES; grammar-breaking KEYS are covered by the targeted COR-15 test
(``test_grammar_breaking_column_key_declines_compaction``).

Acceptance criteria:
  - Every embedded-newline/comma-in-cell row reconstructs to deep equality.
  - Fuzz over ≥200 adversarial cases shows 0 silent loss.
  - The decoder NEVER invents data (only returns rows it can prove).
"""

from __future__ import annotations

import json
import random
import re

import pytest

from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.csv_schema_decoder import (
    _split_logical_lines,  # type: ignore[attr-defined]  # private helper
    decode_csv_schema_rows,
)

# CCR recovery helper — resolves drop sentinels in the compressed output via
# both the Rust store and the Python compression_store.  Imported from the
# CCR recovery invariant suite; do NOT modify that function here.
from tests.test_ccr_recovery_invariant import _recover_from_output as _ccr_recover_from_output

# Force lossless-first so the targeted regression uses the lossless path.
# Mirrors the approach used in tests/test_lossless_column_encodings.py.
_LOSSLESS_FIRST = ContentRouterConfig(smart_crusher_routing_policy="lossless-first")

# MinTokens policy (default): rows are either lossless-kept or
# CCR-dropped with a recoverable sentinel in the output.
_MIN_TOKENS = ContentRouterConfig()

# Regex patterns for CCR drop sentinels.
_DROP_RE = re.compile(r"<<ccr:([a-f0-9]{6,}) (\d+)_rows_offloaded>>")
_OPAQUE_RE = re.compile(r"<<ccr:([a-f0-9]{6,}),[a-z0-9]+,[0-9.]+\w+>>")
_CCR_SENTINEL_KEY = "_ccr_dropped"

# Fixed seed for deterministic fuzz.
_FUZZ_SEED = 42
# Number of adversarial fuzz cases (≥200 per acceptance spec).
_FUZZ_N = 200


# ─────────────────────────── helpers ──────────────────────────────────────────


def _repr(x: object) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _compress_to_text(items: list) -> str:
    """Compress *items* via the lossless-first path and return the CSV text.

    Mirrors ``_compress_to_text`` from ``test_lossless_column_encodings``.
    The engine may return a JSON string (CSV-schema text) or a JSON list
    (when the array is too small to compress).  In the latter case we fall
    through and return the raw rendered string so callers can introspect.
    """
    result = ContentRouter(_LOSSLESS_FIRST).compress(json.dumps(items, ensure_ascii=False))
    rendered = result.compressed
    try:
        parsed = json.loads(rendered)
    except (json.JSONDecodeError, ValueError):
        return rendered
    if isinstance(parsed, str):
        return parsed
    # Small arrays come back as a JSON list — return serialised form so the
    # caller can detect "not a CSV text" via decode_csv_schema_rows → None.
    return rendered


def _compress_to_csv_text(items: list) -> str:
    """Like ``_compress_to_text`` but asserts the result is a CSV-schema string.

    Use for tests that require the lossless CSV path (non-trivial arrays).
    """
    text = _compress_to_text(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None, (
        f"Expected lossless CSV-schema output for {len(items)} rows, "
        f"but decode_csv_schema_rows returned None.  "
        f"Try passing more rows to exceed the compressor's min-token threshold."
    )
    return text


def _has_ccr_sentinel(text: str) -> bool:
    """True if *text* contains any CCR drop sentinel."""
    return bool(_DROP_RE.search(text)) or bool(_OPAQUE_RE.search(text))


def _has_json_ccr_sentinel(value: object) -> bool:
    """True if *value* (possibly nested) contains a _ccr_dropped sentinel."""
    if isinstance(value, str):
        if _CCR_SENTINEL_KEY in value:
            try:
                obj = json.loads(value)
                return isinstance(obj, dict) and _CCR_SENTINEL_KEY in obj
            except (json.JSONDecodeError, ValueError):
                pass
        return _DROP_RE.search(value) is not None or _OPAQUE_RE.search(value) is not None
    if isinstance(value, list):
        return any(_has_json_ccr_sentinel(x) for x in value)
    if isinstance(value, dict):
        return any(_has_json_ccr_sentinel(v) for v in value.values())
    return False


# ─────────────────────── fuzz generators ──────────────────────────────────────


def _make_adversarial_rows(rng: random.Random, n_rows: int) -> list[dict]:
    """Generate *n_rows* rows with adversarial cell shapes.

    Adversarial shapes per the task spec:
      - Embedded newlines in VALUES (not keys — see module docstring)
      - Commas inside cells
      - Colons inside VALUES (a colon in a KEY declines compaction — COR-15)
      - Embedded double-quotes in values
      - Empty strings
      - JSON ``null`` values (the ``__null__`` sentinel path)
      - Absent keys (the ``__missing__`` sentinel path)
      - Unicode (emoji, CJK, RTL)
      - Multi-line stack-trace style values (the primary Cluster-A shape)
      - An all-rows-identical multiline-string column that constant-folds
        into a CSV-quoted declaration containing a newline (COR-1 shape)
      - A head-dict path column whose unique tails carry a comma and a
        double-quote, so every encoded row cell is CSV-quoted (COR-2 shape)

    JSON ``null``, an absent key, and the empty string ``""`` are now
    encoded distinctly by the Rust formatter (the reserved ``__null__`` /
    ``__missing__`` cell sentinels) and inverted exactly by the decoder, so
    all three coexist in one run with zero round-trip ambiguity.  The ``id``
    column stays present + non-null in every row so the table always has a
    stable variable anchor column; only ``msg`` carries null / absent.
    """
    UNICODE_SAMPLES = [
        "héllo",
        "日本語",
        "العربية",
        "🚀🔥💡",
        "ñoño",
        "Ünïcödé",
        "中文",
        "한국어",
    ]
    # All-rows-identical multiline value: ``stamp_constant_columns`` folds it
    # and ``const_decl_value`` CSV-quotes it INTO the ``[N]{...}`` declaration,
    # so the header logical line carries an embedded newline (COR-1 shape).
    BANNER = "fuzz banner line one\nfuzz banner line two"
    # Low-cardinality path roots for the head-dict column (COR-2 shape).
    HEAD_ROOTS = ["svc/api", "svc/worker", "lib/core"]
    rows = []
    for i in range(n_rows):
        choice = rng.randint(0, 8)
        if choice == 0:
            # Embedded newline in a value.
            row = {
                "id": i,
                "msg": f"line one\nline two (row {i})",
                "tag": f"tag-{i % 5}",
            }
        elif choice == 1:
            # Comma inside a string value.
            row = {
                "id": i,
                "msg": f"value with, a comma, here (row {i})",
                "tag": f"tag-{i % 5}",
            }
        elif choice == 2:
            # Colon inside a VALUE (a colon KEY declines compaction — COR-15).
            row = {
                "id": i,
                "msg": f"namespace:value for row {i}",
                "tag": f"tag-{i % 5}",
            }
        elif choice == 3:
            # Embedded double-quote in value.
            row = {
                "id": i,
                "msg": f'he said "hello" (row {i})',
                "tag": f"tag-{i % 5}",
            }
        elif choice == 4:
            # Empty string — must stay distinct from null / missing.
            row = {
                "id": i,
                "msg": "",
                "tag": f"tag-{i % 5}",
            }
        elif choice == 5:
            # Unicode.
            row = {
                "id": i,
                "msg": rng.choice(UNICODE_SAMPLES) + f" row-{i}",
                "tag": f"tag-{i % 5}",
            }
        elif choice == 6:
            # JSON null value (the ``__null__`` sentinel path).
            row = {
                "id": i,
                "msg": None,
                "tag": f"tag-{i % 5}",
            }
        elif choice == 7:
            # Absent ``msg`` key (the ``__missing__`` sentinel path).
            row = {
                "id": i,
                "tag": f"tag-{i % 5}",
            }
        else:
            # Multi-line stack-trace style value (the primary Cluster-A shape).
            row = {
                "id": i,
                "msg": (
                    f"Error: something failed (row {i})\n"
                    f"  at module.func (file.py:42)\n"
                    f"  at caller (other.py:{i})"
                ),
                "tag": f"tag-{i % 5}",
            }
        # The two decoder-regression shapes ride along on EVERY row (see the
        # focused COR-1 / COR-2 tests above the fuzz section):
        #   - ``banner``: identical multiline text in all rows — the header
        #     declaration itself carries a CSV-quoted newline.
        #   - ``path``: head-dict column (few ``<root>/`` heads, unique tails)
        #     whose tails carry a comma AND a double-quote, so every encoded
        #     ``<idx><delim><tail>`` row cell ships CSV-quoted.
        rows.append(
            {
                **row,
                "banner": BANNER,
                "path": f'{HEAD_ROOTS[i % len(HEAD_ROOTS)]}/job {i}, "part {i % 4}".log',
            }
        )
    return rows


# ─────────────────── self-consistency check for the helper ────────────────────


def test_split_logical_lines_matches_plain_split_on_unquoted_text() -> None:
    """``_split_logical_lines`` must be byte-identical to ``str.split('\\n')``
    for quote-free text — this guards against breakage in the 30+ existing
    decoder tests that rely on the current behaviour.
    """
    cases = [
        "",
        "a",
        "a\nb",
        "a\nb\n",
        "a\nb\nc\n",
        "[10]{id:int,msg:string}\n1,hello\n2,world\n",
        "no newlines here",
        "\n",
        "\n\n",
    ]
    for s in cases:
        assert _split_logical_lines(s) == s.split("\n"), (
            f"mismatch for {s!r}: got {_split_logical_lines(s)!r}, expected {s.split(chr(10))!r}"
        )


def test_split_logical_lines_keeps_embedded_newline_inside_quoted_field() -> None:
    """A ``\\n`` inside a CSV-quoted field must NOT break the logical line."""
    # Simulates: [3]{id:int,msg:string}\n0,"line1\nline2"\n
    cases = [
        # One logical line: a quoted cell contains a newline.
        ('"hello\nworld"', ['"hello\nworld"']),
        # Two logical lines: first has a quoted cell with newline.
        ('"line1\nline2",x\nnext', ['"line1\nline2",x', "next"]),
        # Doubled-quote RFC-4180 escape — net no-op for in_quotes state.
        ('"a""b\nc",d\nnext', ['"a""b\nc",d', "next"]),
    ]
    for text, expected in cases:
        result = _split_logical_lines(text)
        assert result == expected, f"Input {text!r}: got {result!r}, expected {expected!r}"


# ──────────────────── Test #1 — Cluster-A targeted regression ─────────────────


def test_embedded_newline_in_cell_round_trips_lossless() -> None:
    """A string cell containing a literal '\\n' must survive Rust-compact →
    Python-decode with deep equality (zero row loss on the lossless path).

    This is the Cluster-A break: text.split('\\n') at line 325 shattered
    a logical row containing an embedded newline into multiple physical
    lines; ``len(parts) != len(var_cols)`` at line 403 silently dropped
    the row with no sentinel → unrecoverable.

    After the fix, ``_split_logical_lines`` tracks quote-open state and
    only breaks on unquoted newlines, reconstructing the logical row exactly.
    """
    # Use 60 rows — enough to exceed the min-token threshold that triggers
    # the lossless CSV-schema path; the actual newline-containing rows are
    # interleaved with plain rows to exercise the state machine.
    items = []
    for i in range(60):
        if i % 4 == 0:
            msg = f"Error: connection refused\n  at connect() line {i}\n  at main() line {i + 1}"
            sev = "ERROR"
        elif i % 4 == 1:
            msg = f"Warning: retrying\n  attempt {i} of 3"
            sev = "WARN"
        elif i % 4 == 2:
            msg = f"plain message without newline {i}"
            sev = "INFO"
        else:
            msg = f"Stack trace:\nFile 'app.py', line {i}\nFile 'lib.py', line {i + 1}"
            sev = "ERROR"
        items.append({"id": i, "msg": msg, "severity": sev})

    text = _compress_csv(items)

    # The rendered text must contain quoted cells (the formatter CSV-quotes
    # any value containing a newline).
    assert '"' in text, f"expected quoted cells in CSV output; got:\n{text}"

    rows = decode_csv_schema_rows(text)
    assert rows is not None, "decode_csv_schema_rows returned None (not a CSV-schema text)"

    recovered = {_repr(row) for row in rows}
    expected = {_repr(it) for it in items}
    missing = expected - recovered
    assert not missing, (
        f"{len(missing)} row(s) not reconstructed from the lossless output.\n"
        f"Missing: {sorted(missing)[:3]}\n"
        f"Rendered text:\n{text[:500]}"
    )


def _compress_csv(items: list) -> str:
    """Force lossless CSV and assert the output is a CSV-schema table."""
    text = _compress_to_text(items)
    # Must decode as a CSV-schema table
    result = decode_csv_schema_rows(text)
    assert result is not None, (
        f"lossless path did not produce CSV-schema text for {len(items)} items.  "
        f"Raw output (first 200): {text[:200]}"
    )
    return text


def test_embedded_newline_multi_line_stack_trace_round_trips() -> None:
    """A multi-line stack trace in a cell must round-trip exactly.

    Uses a predictable multi-line format so the lossless CSV-schema path
    engages (highly-unique long values force the engine to the JSON-list
    path that bypasses the CSV decoder).
    """
    items = [
        {
            "id": i,
            "trace": (f"Traceback:\n  File test.py, line {i * 3 + 1}\nValueError: row {i}"),
            "level": "ERROR" if i % 2 == 0 else "WARN",
        }
        for i in range(60)
    ]

    text = _compress_csv(items)

    rows = decode_csv_schema_rows(text)
    assert rows is not None

    recovered = {_repr(row) for row in rows}
    expected = {_repr(it) for it in items}
    missing = expected - recovered
    assert not missing, (
        f"{len(missing)}/{len(items)} row(s) lost.\nFirst missing: {sorted(missing)[:1]}"
    )


def test_comma_in_cell_round_trips_lossless() -> None:
    """A string cell containing a literal comma must round-trip correctly."""
    items = [{"id": i, "msg": f"step {i}: collect, process, store", "ok": True} for i in range(60)]

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None

    recovered = {_repr(row) for row in rows}
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows lost with comma-in-cell"


def test_embedded_double_quote_in_cell_round_trips_lossless() -> None:
    """A string cell containing a double-quote must round-trip correctly."""
    items = [{"id": i, "msg": f'He said "hello {i}" clearly', "tag": "quote"} for i in range(60)]

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None

    recovered = {_repr(row) for row in rows}
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows lost with double-quote-in-cell"


# ─────────────── Test #1b — null / missing-key / empty-string parity ──────────


def test_null_missing_empty_string_are_distinct_on_lossless_path() -> None:
    """JSON ``null``, an absent key, and ``""`` must reconstruct DISTINCTLY.

    Before the ``__null__`` / ``__missing__`` cell sentinels, the Rust
    formatter rendered all three as an empty CSV cell and the decoder mapped
    every empty cell to ``""`` — so ``null`` and a missing key were silently,
    unrecoverably corrupted into the empty string.  This drives the real
    Rust crush → Python decode and asserts each shape round-trips exactly:

      - ``{"a": None, "b": X}``  ->  ``a`` present and ``None``
      - ``{"b": X}``             ->  ``a`` ABSENT from the reconstructed row
      - ``{"a": "",  "b": X}``   ->  ``a`` present and ``""``

    ``b`` is a genuinely variable string column (non-arith, non-constant) so
    the table keeps a stable per-row anchor and routes through the normal
    multi-column CSV-schema path (not the single-var empty-line path).
    """
    # 60 rows to clear the compressor's min-token threshold (matches the
    # other lossless-path tests in this module).
    items: list[dict] = []
    for i in range(60):
        shape = i % 3
        if shape == 0:
            items.append({"a": None, "b": f"v{i}"})  # JSON null
        elif shape == 1:
            items.append({"b": f"v{i}"})  # key "a" absent
        else:
            items.append({"a": "", "b": f"v{i}"})  # empty string

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None, "expected CSV-schema output for homogeneous rows"

    # Exact deep-equality reconstruction of the whole corpus, in order.
    assert rows == items, (
        "null / missing-key / empty-string not reconstructed distinctly.\n"
        f"Rendered text (first 300):\n{text[:300]}\n"
        f"First 3 decoded: {rows[:3]}"
    )

    # Explicit per-shape assertions (belt-and-suspenders on the invariant).
    assert rows[0]["a"] is None, f"null row lost: {rows[0]!r}"
    assert "a" not in rows[1], f"missing-key row leaked an 'a' key: {rows[1]!r}"
    assert rows[2]["a"] == "", f"empty-string row corrupted: {rows[2]!r}"


def test_literal_sentinel_string_value_round_trips_as_string() -> None:
    """A STRING value literally equal to a sentinel must NOT become ``None``.

    The Rust ``csv_render_str`` CSV-quotes any string equal to ``__null__`` /
    ``__missing__``, so the bare sentinels stay unambiguous.  A row whose
    ``a`` value is the literal string ``"__null__"`` must round-trip back to
    that string, never to ``None`` (and likewise ``"__missing__"`` must not
    drop the key).
    """
    items: list[dict] = []
    for i in range(60):
        shape = i % 3
        if shape == 0:
            items.append({"a": "__null__", "b": f"v{i}"})  # literal sentinel
        elif shape == 1:
            items.append({"a": "__missing__", "b": f"v{i}"})  # literal sentinel
        else:
            items.append({"a": f"plain-{i}", "b": f"v{i}"})

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None

    assert rows == items, (
        "literal sentinel string value corrupted on round-trip.\n"
        f"Rendered text (first 300):\n{text[:300]}\n"
        f"First 3 decoded: {rows[:3]}"
    )
    assert rows[0]["a"] == "__null__", f"literal '__null__' became {rows[0]['a']!r}"
    assert rows[1]["a"] == "__missing__", (
        f"literal '__missing__' became {rows[1].get('a')!r} (key present={'a' in rows[1]})"
    )


# ────────────── Test #1c — COR-1 / COR-2 decoder regressions ──────────────────


def test_multiline_string_constant_column_round_trips_lossless() -> None:
    """COR-1: a constant column whose value contains a newline must decode.

    ``stamp_constant_columns`` folds ANY all-rows-identical scalar except
    null / the empty string — multiline strings included — and
    ``const_decl_value`` CSV-quotes the value INTO the ``[N]{...}``
    declaration, so the header LOGICAL line legally carries an embedded
    newline.  Before the fix ``_HEADER_RE`` lacked ``re.DOTALL``: ``(.+)``
    could not span that newline, the header failed to match, and
    ``decode_csv_schema_rows`` returned ``None`` — 100% silent,
    unrecoverable loss with no CCR sentinel.
    """
    items = [{"id": i, "msg": f"event {i} ok", "note": "line1\nline2"} for i in range(60)]

    text = _compress_to_text(items)
    # The trigger shape: the declaration itself carries the quoted newline
    # (the first PHYSICAL line ends mid-constant).
    header_physical = text.split("\n", 1)[0]
    assert 'note:string="line1' in header_physical, (
        f"expected the multiline constant folded into the declaration; "
        f"got header {header_physical!r}"
    )

    rows = decode_csv_schema_rows(text)
    assert rows is not None, (
        "decode_csv_schema_rows returned None: the [N]{...} header regex "
        "did not match a declaration containing a CSV-quoted newline.\n"
        f"First physical line: {header_physical!r}"
    )
    assert len(rows) == len(items), f"{len(items) - len(rows)}/{len(items)} row(s) lost"
    assert rows == items, "reconstructed rows are not byte-exact"


def test_head_dict_cell_with_csv_quoted_tail_round_trips_lossless() -> None:
    """COR-2: a CSV-quoted head-dict cell must be unquoted before decoding.

    The formatter renders a head-dict row cell as ``<idx><delim><tail>``
    passed through ``csv_render_str`` — a tail containing a comma or a
    double-quote ships CSV-quoted (e.g. ``"0/file 0, part.rs"``).  Before
    the fix the decoder passed the still-quoted cell straight to
    ``_decode_head_cell``, whose leading-digit scan fails on ``"`` — every
    such row was skipped: 0 rows recovered, silently, with no CCR sentinel.
    """
    items = [
        {"id": i, "path": f"{'src' if i % 2 == 0 else 'lib'}/file {i}, part.rs"} for i in range(20)
    ]

    text = _compress_to_text(items)
    # The trigger shape: a head-dict declaration and CSV-quoted row cells.
    assert "path:string@" in text.split("\n", 1)[0], (
        f"expected a head-dict declaration; got:\n{text[:200]}"
    )
    assert '"0/file 0, part.rs"' in text, (
        f"expected a CSV-quoted head-dict cell; got:\n{text[:300]}"
    )

    rows = decode_csv_schema_rows(text)
    assert rows is not None
    assert len(rows) == len(items), (
        f"{len(items) - len(rows)}/{len(items)} head-dict rows skipped "
        f"(quoted cell not unquoted before the head-index digit scan)"
    )
    assert rows == items, "reconstructed rows are not byte-exact"


# ────────────── Test #1d — COR-15 grammar-breaking column keys ────────────────


def test_grammar_breaking_column_key_declines_compaction() -> None:
    """COR-15: a key the ``[N]{...}`` grammar cannot carry must fail closed.

    Column names are emitted RAW into the declaration and the
    ``__dict:``/``__affix:``/``__head:`` preamble lines (the CSV formatter
    quotes only cells, never names).  Before the fix a key like
    ``meta:region`` shipped anyway: ``_parse_header_segment`` split the
    name at the FIRST colon, silently mis-keying every decoded row, and
    the mismatched ``__affix:`` preamble name desynchronized the preamble
    scan — values lost their affix prefix and arith-fold values shifted
    by one row.  The Rust compactor now DECLINES compaction for such keys
    (``Untouched``): the array ships as verbatim JSON — byte-exact — and
    merely skips the lossless tier.

    8 rows: enough for the small-array lossless look to ship CSV for the
    safe-key control, small enough that a declined array stays on the
    verbatim passthrough for EVERY key shape (larger arrays fall to the
    recoverable lossy tier, which is orthogonal to this gate).
    """
    values = [f"srv-{i:03d}.internal.example.com" for i in range(8)]

    # Control: the SAME shape under a safe key takes the lossless CSV path
    # and round-trips exactly — proving the declines below are key-driven,
    # not shape-driven.
    control = [{"id": i, "meta_region": v, "status": "ok"} for i, v in enumerate(values)]
    control_rows = decode_csv_schema_rows(_compress_csv(control))
    assert control_rows == control, "control shape must round-trip via the CSV path"

    for key in ("meta:region", "a,b", "x{y", "a=b"):
        items = [{"id": i, key: v, "status": "ok"} for i, v in enumerate(values)]
        text = _compress_to_text(items)
        rows = decode_csv_schema_rows(text)
        assert rows is None, (
            f"key {key!r} must decline the CSV-schema path (fail-closed), "
            f"but a decodable table shipped.  First decoded rows: {rows[:2]}\n"
            f"Rendered (first 200): {text[:200]}"
        )
        assert json.loads(text) == items, (
            f"declined array for key {key!r} must ship verbatim "
            f"(byte-exact round-trip).\nRendered (first 300): {text[:300]}"
        )


# ────────────── Test #1e — COR-13 decoder-coverage honesty ────────────────────
#
# The engine's lossless claim is "exact reconstruction through
# ``decode_csv_schema_rows``" (module docstring of csv_schema_decoder).
# Today that decoder proves flat-scalar TABLES only: ``Compaction::Buckets``
# renders (``__buckets:``) decode to ``None`` and ``CellValue::Nested``
# cells (CSV-quoted IR JSON) decode to plain strings.  COR-13's contract:
# every shape either decodes byte-exact OR is DECLINED from the lossless
# tier (falling back to verbatim passthrough or the CCR-recoverable lossy
# path) — never shipped as "lossless" while being unverifiable.


def _assert_decoded_exact_or_declined(items: list) -> str:
    """COR-13 contract assertion.  Returns the route taken.

    A CSV-schema render with NO drop sentinel is a lossless-tier claim:
    it must reconstruct byte-exact through the reference decoder
    (route ``"lossless"``).  Any other output means the shape was
    declined from the lossless tier (route ``"declined"``); every input
    row must then still be provable from the output alone — kept inline
    verbatim or recoverable from the CCR store via a sentinel hash —
    never silently lossy.
    """
    text = _compress_to_text(items)
    expected = {_repr(it) for it in items}
    rows = decode_csv_schema_rows(text)
    if rows is not None and not _has_ccr_sentinel(text):
        recovered = {_repr(row) for row in rows}
        assert recovered == expected, (
            f"lossless-tier render is NOT byte-exact: "
            f"{len(expected - recovered)}/{len(items)} row(s) unprovable from the "
            f"output alone (COR-13).\n"
            f"First unprovable: {sorted(expected - recovered)[:1]}\n"
            f"First decoded-but-wrong: {sorted(recovered - expected)[:1]}\n"
            f"Rendered (first 300):\n{text[:300]}"
        )
        return "lossless"
    # Declined from the lossless tier: rows kept inline (passthrough /
    # lossy survivors) + rows recoverable from the CCR store must cover
    # the whole corpus.
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    inline: set[str] = set()
    if isinstance(parsed, list):
        inline = {_repr(row) for row in parsed if not _has_json_ccr_sentinel(row)}
    recovered = inline | _ccr_recover_from_output(items, ccr_enabled=True, ccr_inject_marker=True)
    missing = expected - recovered
    assert not missing, (
        f"shape was declined from the lossless tier but "
        f"{len(missing)}/{len(items)} row(s) are NOT recoverable from the "
        f"output alone — silent loss (COR-13).\n"
        f"First missing: {sorted(missing)[:1]}\n"
        f"Rendered (first 300):\n{text[:300]}"
    )
    return "declined"


def _make_heterogeneous_rows(n_rows: int) -> list[dict]:
    """Bucket-shaped corpus: two disjoint field sets sharing only a clean
    string discriminator (``kind``) — the Rust compactor's heterogeneous
    branch partitions this into ``Compaction::Buckets`` (core-field ratio
    1/7 < 0.6; ``kind`` is present in every row, all-string, 2 buckets).
    """
    rows: list[dict] = []
    for i in range(n_rows):
        if i % 2 == 0:
            rows.append(
                {
                    "kind": "user",
                    "name": f"user-{i:03d}",
                    "email": f"user{i}@example.com",
                    "role": "admin" if i % 4 == 0 else "member",
                }
            )
        else:
            rows.append(
                {
                    "kind": "metric",
                    "ts": 1700000000 + i,
                    "value": i * 3,
                    "unit": "ms",
                }
            )
    return rows


def _make_nested_array_rows(n_rows: int) -> list[dict]:
    """Flat homogeneous keys, but ``children`` holds an array of ≥2
    objects in every row — the compactor promotes those cells to
    ``CellValue::Nested`` (CSV-quoted IR JSON on the wire, which the
    reference decoder cannot invert).  The four long constant columns
    fold into the declaration and dwarf the IR-JSON envelope overhead of
    the tiny ``children`` cells, so the render clears the 30%
    ``lossless_min_savings_ratio`` byte gate — only the COR-13
    decoder-coverage gate can keep this shape off the lossless tier.
    """
    return [
        {
            "id": i,
            "service": "auth-service-primary-eu-central-1.internal.example.com",
            "status": "ok-and-healthy-and-ready",
            "region": "eu-central-1-availability-zone-a",
            "deployment": "blue-green-rollout-2026-06-15T00:00:00Z-primary",
            "children": [{"k": i}, {"k": i + 1}],
        }
        for i in range(n_rows)
    ]


def _make_json_mixed_rows(n_rows: int) -> list[dict]:
    """Flat table whose ``cfg`` column mixes object / array-of-scalars /
    string cells.  The varying shapes keep the dotted-flatten pass out of
    the picture (COR-14 — never uniform objects) and no cell is an
    array-of-objects (never ``Nested``), so the table stays flat with a
    ``json``-tagged column: object/array cells ship as CSV-quoted compact
    JSON (the formatter's ``json_scalar_to_csv`` fallback).  The two
    string variants pin the decode boundary: a quoted string cell must
    stay a string even when it contains commas or looks like a JSON
    string literal.
    """
    rows: list[dict] = []
    for i in range(n_rows):
        r = i % 4
        cfg: object
        if r == 0:
            cfg = {"retries": i, "backoff": [i, i + 1]}
        elif r == 1:
            cfg = [i, i * 2, f"opt-{i}"]
        elif r == 2:
            cfg = f"plain, with a comma {i}"
        else:
            cfg = f'"looks like a JSON string literal {i}"'
        rows.append(
            {
                "id": i,
                "service": "auth-service-primary-eu-central-1",
                "cfg": cfg,
            }
        )
    return rows


def test_json_tagged_object_and_array_cells_round_trip_byte_exact() -> None:
    """COR-13 (c): object/array cells in a ``json``-tagged column of a
    flat table must decode back to objects/arrays, not strings.

    The formatter ships such cells as CSV-quoted compact JSON.  Before
    the fix ``_decode_cell`` treated every CSV-quoted cell as a string
    ("CSV-quoted cells are ALWAYS strings" — factually wrong for this
    producer), silently corrupting the type of every object/array cell
    on the lossless path.
    """
    items = _make_json_mixed_rows(60)
    text = _compress_csv(items)

    # The trigger shape must actually be on the wire: at least one
    # CSV-quoted JSON object cell and one quoted JSON array cell.
    assert '"{' in text and '"[' in text, (
        f"expected CSV-quoted JSON container cells in the render; got:\n{text[:300]}"
    )

    rows = decode_csv_schema_rows(text)
    assert rows is not None
    assert rows == items, (
        "json-tagged object/array cells did not round-trip byte-exact "
        "(decoded as strings?).\n"
        f"First decoded cfg values: {[r.get('cfg') for r in rows[:4]]!r}\n"
        f"Rendered (first 300):\n{text[:300]}"
    )
    # Belt-and-suspenders on the exact type boundary.
    assert isinstance(rows[0]["cfg"], dict), f"object cell: {rows[0]['cfg']!r}"
    assert isinstance(rows[1]["cfg"], list), f"array cell: {rows[1]['cfg']!r}"
    assert isinstance(rows[2]["cfg"], str), f"comma-string cell: {rows[2]['cfg']!r}"
    assert rows[3]["cfg"] == '"looks like a JSON string literal 3"', (
        f"a string cell that LOOKS like a JSON string literal must stay a "
        f"string verbatim: {rows[3]['cfg']!r}"
    )


def test_heterogeneous_buckets_shape_never_ships_unverifiable_lossless() -> None:
    """COR-13 (a): a ``__buckets:`` render is unverifiable by the
    reference decoder (``decode_csv_schema_rows`` returns ``None``), so
    the crusher must DECLINE heterogeneous arrays from the lossless tier
    (fail-closed) until the decoder covers the Buckets grammar.
    """
    items = _make_heterogeneous_rows(60)
    route = _assert_decoded_exact_or_declined(items)

    text = _compress_to_text(items)
    assert not text.startswith("__buckets:"), (
        "a Compaction::Buckets render shipped under the lossless claim, but "
        "the reference decoder cannot decode the __buckets: grammar "
        f"(COR-13).\nRendered (first 200):\n{text[:200]}"
    )
    # Today's expected route.  When full Buckets decoder coverage lands,
    # this pin flips to "lossless" — deliberate, so the coverage change
    # is made consciously.
    assert route == "declined", f"unexpected route {route!r}"


def test_nested_array_of_objects_cells_never_ship_unverifiable_lossless() -> None:
    """COR-13 (b): ``CellValue::Nested`` cells render as CSV-quoted IR
    JSON (``{"_compaction":...}`` envelope) which the reference decoder
    cannot invert — a lossless-tier render carrying one is unverifiable.
    The crusher must DECLINE tables containing Nested cells from the
    lossless tier (fail-closed) until the decoder covers them.
    """
    items = _make_nested_array_rows(60)
    route = _assert_decoded_exact_or_declined(items)
    assert route == "declined", f"unexpected route {route!r}"


def test_small_array_nested_cells_decline_lossless_zone() -> None:
    """Same Nested shape through the SMALL-array lossless zone
    (``crush_array``'s tier-1 boundary, crusher.rs site 1): a small
    nested-cell array must stay verbatim passthrough, never ship an
    unverifiable table.
    """
    items = _make_nested_array_rows(6)
    route = _assert_decoded_exact_or_declined(items)
    assert route == "declined", f"unexpected route {route!r}"


def test_fuzz_cor13_shapes_decode_exact_or_decline() -> None:
    """Seeded fuzz over the three COR-13 shape families × size variants
    (203 rows total): heterogeneous (Buckets), nested array-of-objects
    cells (Nested), and flat json-tagged mixed columns.  Every corpus
    must satisfy the COR-13 contract — decode byte-exact or decline —
    regardless of which crusher zone (small-array / big-array / lossy
    survivor) the size routes it through.
    """
    rng = random.Random(_FUZZ_SEED + 4)
    corpora: list[list[dict]] = [
        _make_heterogeneous_rows(60),
        _make_heterogeneous_rows(8),
        _make_nested_array_rows(60),
        _make_nested_array_rows(6),
        _make_json_mixed_rows(60),
        _make_json_mixed_rows(9),
    ]
    for items in corpora:
        # Shuffled order (seeded): the contract may not depend on arith
        # folds / row order.  Shuffle a copy — generators stay pristine.
        shuffled = list(items)
        rng.shuffle(shuffled)
        _assert_decoded_exact_or_declined(shuffled)


# ───────────────────────── Test #2 — Property / fuzz ──────────────────────────


def test_fuzz_adversarial_cases_zero_silent_loss() -> None:
    """200 adversarial out-of-sample rows via fixed seed.

    Each input row must reconstruct to deep equality through the lossless
    Rust-compact → Python-decode pipeline.  Zero silent loss: no row may
    disappear without trace.  The decoder NEVER invents data:
    ``recovered ⊆ {_repr(it) for it in items}``.
    """
    rng = random.Random(_FUZZ_SEED)
    items = _make_adversarial_rows(rng, _FUZZ_N)

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None, "lossless path produced non-CSV-schema output"

    recovered = {_repr(row) for row in rows}
    expected = {_repr(it) for it in items}

    # No invented data: every decoded row must match an input row.
    invented = recovered - expected
    assert not invented, (
        f"Decoder invented {len(invented)} row(s) not in input: {sorted(invented)[:2]}"
    )

    # Zero silent loss on lossless path.
    missing = expected - recovered
    assert not missing, (
        f"{len(missing)}/{len(items)} rows silently lost on lossless path "
        f"(embedded newlines / adversarial cells not handled).\n"
        f"First missing: {sorted(missing)[:2]}"
    )


def test_fuzz_adversarial_cases_min_tokens_policy_zero_silent_loss() -> None:
    """Same adversarial corpus, MinTokens (default) policy.

    Under MinTokens the engine may:
    (a) take the lossless CSV-schema path (rows decode directly), or
    (b) take a lossy drop path (dropped rows have a ``_ccr_dropped``
        sentinel in the output), or
    (c) run the CrossMessageDeduper which may add a ``_dup_count`` column
        and merge duplicate rows.

    The core contract this test checks:
      - When the output is a lossless CSV-schema string, decoded rows must
        round-trip after stripping internal engine fields (``_dup_count``).
      - When the output is a JSON list with drop sentinels, the sentinel
        must be present.
      - No row disappears without trace (no silent loss).

    We strip ``_dup_count`` before comparing because the CrossMessageDeduper
    is a transformation that adds deduplication metadata, not a lossy
    drop — it is part of the lossless pipeline.
    """
    # Internal engine fields added by transforms (not in original input).
    _ENGINE_FIELDS = {"_dup_count"}

    def _strip_engine(row: dict) -> dict:
        return {k: v for k, v in row.items() if k not in _ENGINE_FIELDS}

    rng = random.Random(_FUZZ_SEED + 1)
    items = _make_adversarial_rows(rng, _FUZZ_N)

    router = ContentRouter(_MIN_TOKENS)
    result = router.compress(json.dumps(items, ensure_ascii=False))
    compressed = result.compressed

    expected_reprs = {_repr(it) for it in items}

    # Parse the output.
    try:
        parsed = json.loads(compressed)
    except (json.JSONDecodeError, ValueError):
        parsed = compressed

    if isinstance(parsed, str):
        # Lossless CSV-schema rendering (possibly with _dup_count column).
        rows = decode_csv_schema_rows(parsed)
        if rows is not None:
            # Strip internal engine fields before comparing.
            stripped = [_strip_engine(row) for row in rows]
            recovered_inline = {_repr(row) for row in stripped}
            # All stripped rows must come from the input (no invented data).
            invented = recovered_inline - expected_reprs
            assert not invented, (
                f"Decoder produced {len(invented)} row(s) after stripping engine fields "
                f"that were not in the input: {sorted(invented)[:2]}"
            )
        # Whether or not rows decoded from the CSV, some rows may be in the
        # CCR store (referenced by <<ccr:HASH>> sentinels embedded in the
        # string output).  Use the full recovery machinery to check zero loss.
        recovered = _ccr_recover_from_output(items, ccr_enabled=True, ccr_inject_marker=True)
        missing = expected_reprs - recovered
        assert not missing, (
            f"{len(missing)}/{len(items)} rows are silently lost on the MinTokens path "
            f"(lossless CSV-schema branch): neither decoded directly from the CSV nor "
            f"recoverable from the CCR store via sentinels in the output.\n"
            f"First missing: {sorted(missing)[:2]}"
        )
    elif isinstance(parsed, list):
        # MinTokens kept all rows inline (array below threshold) or
        # partially dropped rows with sentinels.
        sentinel_rows = [
            row for row in parsed if isinstance(row, dict) and _CCR_SENTINEL_KEY in row
        ]

        if sentinel_rows:
            # Some rows were dropped; verify each sentinel row carries the key.
            for sentinel in sentinel_rows:
                assert _CCR_SENTINEL_KEY in sentinel, f"Missing sentinel key: {sentinel}"
            # Rows that were not dropped must match the input (after stripping).
            non_sentinel = [
                row for row in parsed if isinstance(row, dict) and _CCR_SENTINEL_KEY not in row
            ]
            for row in non_sentinel:
                stripped = _strip_engine(row)
                assert _repr(stripped) in expected_reprs, (
                    f"Kept row (stripped) not in input: {stripped!r}"
                )
        else:
            # No drops — all rows kept inline; check stripped rows are valid input rows.
            for row in parsed:
                if isinstance(row, dict):
                    stripped = _strip_engine(row)
                    assert _repr(stripped) in expected_reprs, (
                        f"Inline row (stripped) not in input: {stripped!r}"
                    )
        # Zero-silent-loss check for the list branch: dropped rows must be CCR-recoverable.
        recovered = _ccr_recover_from_output(items, ccr_enabled=True, ccr_inject_marker=True)
        missing = expected_reprs - recovered
        assert not missing, (
            f"{len(missing)}/{len(items)} rows are silently lost on the MinTokens path "
            f"(JSON-list branch): neither kept inline nor recoverable from the CCR store "
            f"via sentinels in the output.\n"
            f"First missing: {sorted(missing)[:2]}"
        )
    else:
        # Unexpected output type — must never occur for a valid compression.
        pytest.fail(
            f"MinTokens policy produced an unexpected output type {type(parsed).__name__!r} "
            f"(not str or list). This branch previously silently passed via a tautological "
            f"sentinel check. Raw output head: {str(parsed)[:200]!r}"
        )


def test_fuzz_decoder_never_invents_data() -> None:
    """The decoder must never produce a row that was not in the input.

    Invariant: even on partial/malformed inputs, the decoder only emits
    rows it can prove from the rendered text.
    """
    rng = random.Random(_FUZZ_SEED + 2)
    items = _make_adversarial_rows(rng, 200)

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None

    expected = {_repr(it) for it in items}
    for row in rows:
        assert _repr(row) in expected, f"Decoder invented a row not present in the input: {row!r}"


def test_fuzz_embedded_newline_rows_never_split() -> None:
    """All rows containing embedded newlines must survive the lossless path.

    Specifically tests that rows are NOT shattered by a naive newline split.
    """
    rng = random.Random(_FUZZ_SEED + 3)
    # 200 rows all containing embedded newlines.
    items = [
        {
            "id": i,
            "log": f"step {i} started\n  substep A\n  substep B\n  done",
            "level": rng.choice(["INFO", "WARN", "ERROR"]),
        }
        for i in range(200)
    ]

    text = _compress_csv(items)
    rows = decode_csv_schema_rows(text)
    assert rows is not None, "expected CSV-schema output for homogeneous rows"

    recovered = {_repr(row) for row in rows}
    expected = {_repr(it) for it in items}
    missing = expected - recovered
    assert not missing, (
        f"{len(missing)}/{len(items)} embedded-newline rows shattered/lost.\n"
        f"This indicates the naive text.split('\\n') is still in place.\n"
        f"First missing: {sorted(missing)[:1]}"
    )
