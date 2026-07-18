"""Lossless per-column encodings on the public ``compress()`` path.

Covers the CSV-schema formatter's column encodings:

* **Constant-column fold** — a column holding the identical scalar in
  every row declares ``name:type=value`` once in the ``[N]{...}`` header
  and is omitted from the rows. The value is verbatim in the output and
  every row is reconstructible from the output alone (zero loss).

* **Ditto marks** — a cell identical to the SAME column's cell in the
  previous row renders as a bare ``=``; a literal string cell ``"="`` is
  CSV-quoted so the marker stays unambiguous. Reconstruction is
  carry-forward of the last materialized value (zero loss).

The reconstruction contract is the SAME decoder the CCR recovery
invariant uses (``tests/test_ccr_recovery_invariant.py``): a consumer
holding ONLY the output must be able to recover every distinct row.
"""

from __future__ import annotations

import json

from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig

# TEST-19: helpers come from the shared fixtures module — importing them
# from a SIBLING TEST FILE (tests.test_ccr_recovery_invariant) coupled this
# suite to that file's internals and executed its module top level on import.
from tests._fixtures import canonical_repr as _repr
from tests._fixtures import decode_csv_schema_into as _decode_csv_schema

# These tests assert the LOSSLESS CSV-schema rendering directly (every
# row encoded in the output, `isinstance(parsed, str)`). The production
# default is route-by-min-tokens, which would ship the fewer-token render
# — for several of these shapes that is the lossy/dropped render, flipping
# the assertions. Force LosslessFirst so this suite exercises the lossless
# encoder/decoder in isolation, as intended. (The recovery invariant under
# the default policy is covered by tests/test_ccr_recovery_invariant.py.)
_LOSSLESS_FIRST = ContentRouterConfig(smart_crusher_routing_policy="lossless-first")


def _compress_to_text(items: list) -> str:
    result = ContentRouter(_LOSSLESS_FIRST).compress(json.dumps(items, ensure_ascii=False))
    rendered = result.compressed
    try:
        parsed = json.loads(rendered)
    except (json.JSONDecodeError, ValueError):
        return rendered
    assert isinstance(parsed, str), f"expected lossless rendering, got: {type(parsed)}"
    return parsed


def _reconstruct(text: str) -> set[str]:
    recovered: set[str] = set()
    _decode_csv_schema(text, recovered)
    return recovered


def test_constant_columns_fold_and_round_trip() -> None:
    # Shape mirrors the real `ping` benchmark capture: three constant
    # columns + a monotone counter + a varying float.
    items = [
        {
            "bytes": 64,
            "from": "127.0.0.1",
            "icmp_seq": i,
            "ttl": 64,
            "time_ms": round(0.031 + (i % 7) * 0.013, 3),
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]

    # Constants are folded into the declaration, verbatim, exactly once.
    assert "bytes:int=64" in decl, decl
    assert "from:string=127.0.0.1" in decl, decl
    assert "ttl:int=64" in decl, decl
    assert text.count("127.0.0.1") == 1, "constant must appear exactly once"

    # Zero loss: every distinct row reconstructible from the output alone.
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_constant_fold_does_not_fire_on_varying_columns() -> None:
    # `id` hops non-arithmetically ((i*7) % 50 — distinct, varying step)
    # so NEITHER the constant fold NOR the arithmetic fold may fire.
    items = [{"id": (i * 7) % 50, "msg": f"record-{i}-distinct-payload"} for i in range(50)]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    assert "=" not in decl, f"no constant/progression exists, nothing may fold: {decl}"
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing


def test_ditto_marks_round_trip_consecutive_repeats() -> None:
    # Shape mirrors the real rg-search benchmark capture: `path` repeats
    # in consecutive runs (matches grouped per file), other columns vary.
    # Distinct, NON-affix-sharing path tokens per run (so the column pins
    # ditto in isolation and the cross-row affix fold finds nothing to
    # share). `lines` is per-row unique; ditto fires on the repeating path.
    runs = ["zeta", "Quark9", "moon!", "Vault", "iris7", "Omega"]
    items = [
        {
            "path": runs[i // 10],
            "line_number": 3 * i + 1,
            "lines": f"snippet for row {i} here",
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    body = text.split("\n")[1:]

    # Runs of the same path render as ditto cells after the first row.
    assert any(line.startswith("=,") or ",=," in line or line.endswith(",=") for line in body), (
        f"expected ditto marks in rows; first rows: {body[:3]}"
    )
    # Each distinct path appears exactly once (first row of its run).
    for d in runs:
        assert text.count(d) == 1

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_literal_equals_sign_cell_is_not_mistaken_for_ditto() -> None:
    # A real data cell whose value is exactly "=" must render CSV-quoted
    # so bare `=` stays unambiguous, and must round-trip as the literal.
    items = [{"id": i, "op": "=" if i % 2 == 0 else f"op-{i}", "v": f"val-{i}"} for i in range(40)]
    text = _compress_to_text(items)
    assert '"="' in text, f"literal '=' cell must be quoted: {text.split(chr(10))[:4]}"

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_repeated_numeric_cells_ditto_and_round_trip() -> None:
    items = [
        {"seq": i, "status_code": 200 if i % 5 else 503, "latency_ms": 12.5 + (i % 3)}
        for i in range(40)
    ]
    text = _compress_to_text(items)
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_arith_fold_monotone_counter_round_trips() -> None:
    # Shape mirrors the real `ping` benchmark capture: the monotone
    # icmp_seq counter is an exact arithmetic progression and folds into
    # the declaration as `icmp_seq:int=0+1`; rows carry only the real
    # varying latency. Reconstruction regenerates every counter value
    # from the row index — exact, not verbatim.
    items = [
        {
            "bytes": 64,
            "from": "127.0.0.1",
            "icmp_seq": i,
            "ttl": 64,
            "time_ms": round(0.031 + (i % 7) * 0.013, 3),
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    assert "icmp_seq:int=0+1" in decl, decl
    # The counter column is folded: no bare counter cells in the rows.
    body_lines = text.split("\n")[1:]
    assert all("," not in line for line in body_lines if line), (
        f"rows must carry only the latency cell after const + arith folds: {body_lines[:3]}"
    )

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_iso_delta_timestamps_round_trip() -> None:
    # Shape mirrors real structured logs: a strict-shape ISO-8601
    # timestamp column (mixed timezone spellings, non-monotone order, a
    # duplicate) plus low-entropy content columns. The column declares
    # `ts:string~`, ships the first timestamp verbatim and second-deltas
    # after; reconstruction is pure integer civil-calendar math.
    tzs = ["+02:00", "+02:00", "-07:00", "Z", "+02:00", "-04:00"]
    items = [
        {
            "ts": f"2026-06-{(i % 9) + 10:02d}T{(i * 7) % 24:02d}:{(i * 13) % 60:02d}:"
            f"{(i * 17) % 60:02d}{tzs[i % 6]}",
            "level": "info" if i % 4 else "warn",
            "msg": f"request {i} completed",
        }
        for i in range(50)
    ]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    assert "ts:string~" in decl, decl
    # Only the FIRST timestamp appears verbatim; the rest are deltas.
    assert text.count("2026-06-") == 1, "later timestamps must be delta cells"

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_iso_delta_z_and_offset_spellings_survive() -> None:
    # `Z` and `+00:00` are numerically equal but lexically distinct —
    # reconstruction must preserve the original spelling of each row.
    items = [
        {"ts": f"2026-01-01T00:00:{i:02d}" + ("Z" if i % 2 else "+00:00"), "n": f"e{i}"}
        for i in range(20)
    ]
    text = _compress_to_text(items)
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_fractional_second_timestamps_stay_verbatim() -> None:
    # Non-strict shapes (fractional seconds) must NOT delta-encode —
    # every value stays verbatim in the output and still round-trips.
    # Constant columns keep the array on the lossless tabular route so
    # the timestamp column's behavior is what the test isolates.
    items = [
        {
            "ts": f"2026-06-11T21:02:{i:02d}.{i:03d}+02:00",
            "host": "api-gateway-1",
            "service": "checkout",
            "status": 200,
            "n": f"e{i}",
        }
        for i in range(30)
    ]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    # The fractional second poisons the strict ISO-delta path: the column
    # must NOT be `ts:string~`. It MAY be cross-row affix-folded (lossless),
    # so the invariant we assert is exact round-trip, not verbatim presence.
    assert "ts:string~" not in decl, decl
    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_dict_encoding_low_cardinality_column_round_trips() -> None:
    # Shape mirrors the real git-log benchmark capture: a small set of
    # authors repeats NON-consecutively across many rows (ditto cannot
    # catch that), each subject is distinct. The dictionary line carries
    # each distinct author verbatim exactly once; rows carry indexes.
    authors = ["Alice Cooper", "Bob the Builder", "Carol Danvers", "Dan Abnett"]
    items = [
        {
            "author": authors[(i * 3) % 4],
            "subject": f"feat(area-{i % 10}): change number {i} with details",
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    lines = text.split("\n")
    assert lines[1].startswith("__dict:author="), lines[:3]
    for a in authors:
        assert text.count(a) == 1, f"{a} must appear exactly once (in the dict line)"

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_dict_encoding_values_with_commas_round_trip() -> None:
    # Dictionary values are CSV-escaped in the preamble line; commas and
    # quotes inside a value must survive reconstruction exactly.
    names = ["Smith, John", 'O"Hara, Anne', "plain name"]
    items = [{"name": names[(i * 2) % 3], "event": f"login attempt {i}"} for i in range(45)]
    text = _compress_to_text(items)
    assert "__dict:name=" in text, text.split("\n")[:3]

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_all_distinct_string_column_never_dict_encodes() -> None:
    # An all-distinct column gains nothing from dictionary INDEXES — it
    # must never be `__dict:` encoded (that would be a fake low-cardinality
    # win). The shared path root / extension IS legitimate cross-row affix
    # structure, which the engine may fold losslessly; the honest gate is
    # "no dictionary encoding" + exact round-trip.
    items = [
        {"path": f"src/pkg_{i}/module_{i}.py", "match": f"def handler_{i}():"} for i in range(40)
    ]
    text = _compress_to_text(items)
    assert "__dict:" not in text, "all-distinct column must not dict-encode"

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_decimal_scale_fold_round_trips() -> None:
    # Shape mirrors the real `ping` benchmark capture: after const +
    # arith folds the rows are just the latency column; the decimal
    # scale-fold renders `0.053` as `53` (`time_ms:float%3`).
    # Decode is pure string manipulation — exact value reconstruction.
    items = [
        {
            "bytes": 64,
            "from": "127.0.0.1",
            "icmp_seq": i,
            "ttl": 64,
            "time_ms": round(0.031 + (i % 23) * 0.013, 3),
        }
        for i in range(60)
    ]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    assert "time_ms:float%3" in decl, decl
    body = text.split("\n")[1:]
    assert "0.031" not in text, "latency cells must be scale-encoded"
    assert any(line == "31" for line in body), body[:4]

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_arith_fold_negative_step_round_trips() -> None:
    # Descending counters (e.g. remaining-retries) fold with a negative
    # step and reconstruct exactly.
    items = [{"remaining": 500 - 5 * i, "event": f"attempt-{i}-of-100"} for i in range(40)]
    text = _compress_to_text(items)
    decl = text.split("\n", 1)[0]
    assert "remaining:int=500+-5" in decl, decl

    recovered = _reconstruct(text)
    missing = {_repr(it) for it in items} - recovered
    assert not missing, f"{len(missing)} rows unrecoverable; first: {sorted(missing)[:2]}"


def test_offload_survivor_render_demotes_subset_only_constant_review_f1b() -> None:
    """Review F1b: on the OFFLOAD path (min-tokens routing, survivors only) the
    survivor render must not present a per-column constant/dict that holds only
    for the SHOWN rows as if it were universal.

    Rows 170-199 are all ``event_type="payout_request"`` while 0-169 span three
    other categories. When the crusher keeps only the payout_request tail, the
    header must NOT claim ``event_type:string=payout_request`` (false for the 170
    offloaded rows) — event_type must render as an ordinary per-row column so the
    offloaded categories are not silently erased from the view.
    """
    from furl_ctx.cache.compression_store import reset_compression_store
    from furl_ctx.compress import compress

    rows = [
        {
            "event_id": f"evt_{i:05d}",
            "ts": f"2026-07-13T10:00:{i % 60:02d}Z",
            "user_id": f"u{i % 40}",
            "amount": round(10 + i * 1.5, 2),
            "event_type": (
                "payout_request" if i >= 170 else ["purchase", "refund", "login"][i % 3]
            ),
            "velocity_1h": i % 15,
        }
        for i in range(200)
    ]

    reset_compression_store()
    try:
        result = compress(
            [{"role": "tool", "content": json.dumps(rows)}],
            model="claude-sonnet-4-5-20250929",
        )
        view = result.messages[0]["content"]
        view = view if isinstance(view, str) else json.dumps(view)

        # It really did take the offload/survivor path (not the full lossless one).
        assert "router:smart_crusher" in ", ".join(result.transforms_applied)
        assert "_rows_offloaded" in view or "_ccr_dropped" in view
        # The false-universal constant claim is gone; event_type is per-row.
        assert "event_type:string=payout_request" not in view
        assert "event_type:string," in view or "event_type:string}" in view
    finally:
        reset_compression_store()
