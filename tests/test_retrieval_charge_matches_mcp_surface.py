"""The retrieval charge is pinned to the surface a MODEL actually pays on.

``verify/measure.py::effective_savings`` prices a retrieval from three measured
constants plus a recomputed escaping. Every one of them is a claim about the
production ``furl_retrieve`` handler, and a claim about another module's output
rots silently unless something compares the two. This file is that comparison:
it drives the REAL ``FurlMCPServer._handle_retrieve`` over real ``compress()``
output and asserts the charge reproduces what the handler returns.

WHY THE CHARGE IS NOT ``count_text(blob)``
------------------------------------------
``recovered`` holds the byte-exact original, which is what the LIBRARY returns.
A model gets the MCP response, where the payload is JSON-escaped inside the
``original_content`` field. Measured over 18 blobs spanning 76..75530 content
tokens, the raw-bytes charge undercharged by 82-2185 tokens per blob
(1.94%-6.95%) and never once overcharged. A single fudge constant cannot fix
that: the gap spans 2103 tokens and a least-squares fit in content size still
leaves 945 tokens of residual, because escaping cost tracks how many quotes and
newlines a payload holds, not how big it is.

NO ``importorskip`` HERE, BY DESIGN
-----------------------------------
This file imports the MCP server unguarded. If the ``mcp`` extra is missing the
file goes RED rather than quietly vanishing — the same rule
``tests/test_optional_suites_not_silently_disarmed.py`` exists to enforce, and
the same reason: a charge model pinned by a suite that disarms itself when a
dependency is absent is not pinned at all.

THE MEMOIZATION TRAP THIS FILE AVOIDS
-------------------------------------
``FurlMCPServer`` memoizes ``_local_store`` on first use. Reusing ONE server
across ``reset_compression_store()`` makes every later retrieve read a dead
store and return a ~110-token MISS — which looks exactly like a cheap retrieval
and would "prove" the charge is far too high. Each case therefore builds a fresh
server, and every response is asserted to carry the payload byte-exact before
any token count is taken from it.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("tiktoken")

from furl_ctx import compress
from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.ccr.mcp_server import FurlMCPServer
from verify.measure import (
    MCP_RESPONSE_SCAFFOLD_TOKENS,
    RETRIEVE_CALL_OVERHEAD_TOKENS,
    TOOL_RESULT_ENVELOPE_TOKENS,
    _emitted_drop_hashes,
    _retrieve_originals,
    _stringify,
    _tok,
    retrieved_blob_tokens,
)

MODEL = "gpt-4o"

# Token-cost bounds come from broad measured distributions, especially random 24-hex hashes. Keep ranges wide enough for
# tokenization variance but narrow enough to catch response/call-shape changes; do not widen them to silence failures.
SCAFFOLD_RANGE = (60, 76)
SCAFFOLD_MEDIAN = 68
CALL_RANGE = (26, 39)
CALL_MEDIAN = 31


def _retrieved_blobs(messages: list[dict[str, Any]], query: str) -> dict[str, str]:
    """Compress on a cold store and return the sentinel-backed payloads."""
    reset_compression_store()
    result = compress(messages, model=MODEL)
    sentinelled: set[str] = set()
    for message in result.messages:
        sentinelled |= _emitted_drop_hashes(_stringify(message.get("content")))
    return _retrieve_originals(sentinelled, query)


def _mcp_response(hash_key: str) -> str:
    """The text a model receives from a real ``furl_retrieve``.

    A FRESH server per call: see the memoization trap in the module docstring.
    """
    server = FurlMCPServer()
    parts = asyncio.run(server._handle_retrieve({"hash": hash_key}))
    return "".join(getattr(part, "text", "") for part in parts)


def _prose_offload_case() -> tuple[dict[str, str], str]:
    """A whole-blob offload carrying REAL newlines. Shape chosen by measurement.

    Two obvious-looking shapes are both wrong for this half:

    * a JSON ARRAY of rows — the store keeps the engine's own compact
      re-serialisation, in which an embedded newline is already the
      two-character sequence ``\\n``. Such a blob holds NO literal newline.
    * log-shaped text — the router compresses it (measured
      ``router:search:0.17``), so nothing is offloaded and there is no payload
      to price at all.

    Free prose with quotes and a per-line UUID defeats both: the router cannot
    shrink it, so it takes ``router:ccr_offload`` and stores the payload
    byte-identically, newlines intact. Seeded, so it is deterministic.
    """
    rng = random.Random(20260726)
    words = (
        "ledger",
        "quorum",
        "ratchet",
        "obsidian",
        "parallax",
        "tundra",
        "cadence",
        "verdigris",
        "syzygy",
        "kestrel",
    )
    payload = "\n".join(
        f'note {i}: the reviewer wrote "{" ".join(rng.choice(words) for _ in range(9))}" '
        f"and signed {uuid.UUID(int=rng.getrandbits(128))}"
        for i in range(60)
    )
    query = "Review this note and summarize it."
    blobs = _retrieved_blobs(
        [{"role": "user", "content": query}, {"role": "tool", "content": payload}], query
    )
    assert list(blobs.values()) == [payload], "the offload must store the payload byte-identically"
    assert "\n" in payload and '"' in payload
    return blobs, query


def _dense_quote_case() -> tuple[dict[str, str], str]:
    """A row-drop offload whose blob is DENSE in quotes — where escaping is paid.

    This case exists because the prose case above cannot see the content term.
    Measured: escaping the prose payload costs **2 tokens**, inside any sane
    constant margin, so an end-to-end test built on it alone stays green when
    the charge reverts to raw bytes. A JSON-array blob carries thousands of
    quotes and the same reversion costs **thousands** of tokens.

    Keep both. The prose case is the only one with literal newlines; this one is
    the only one where the escaping term is large enough to be visible end to end.
    """
    rows = [
        {
            "id": i,
            "ts": f"2025-05-{(i % 28) + 1:02d}T{i % 24:02d}:{i % 60:02d}:{(i * 11) % 60:02d}Z",
            "level": ["INFO", "WARN", "ERROR"][i % 3],
            "service": f"svc-{i:04x}",
            "message": f'request {i} said "ok" after {(i * 17) % 9999}ms',
        }
        for i in range(600)
    ]
    query = "Summarize the failures in this log"
    blobs = _retrieved_blobs(
        [
            {"role": "user", "content": query},
            {"role": "tool", "content": json.dumps(rows, ensure_ascii=False)},
        ],
        query,
    )
    assert blobs, "fixture must offload at least one blob"
    assert all(b.count('"') > 1000 for b in blobs.values()), "fixture must be escape-dense"
    return blobs, query


def _offloading_cases() -> Iterator[tuple[str, dict[str, str]]]:
    """Both shapes, LAZILY — each built and consumed before the next exists.

    A generator, not a list, and that is load-bearing. Building both up front
    means the second fixture's ``reset_compression_store()`` wipes the first
    fixture's entries, so every later retrieve of a prose hash is a MISS. The
    byte-exact response assertions catch it, but only because the store is torn
    down between yields rather than under a set of already-collected hashes.
    """
    yield "prose", _prose_offload_case()[0]
    yield "dense-quote", _dense_quote_case()[0]


@pytest.mark.slow
def test_charged_payload_equals_the_escaped_payload_the_model_receives() -> None:
    """The CONTENT term. ``retrieved_blob_tokens`` must reproduce the payload as
    it appears in the handler's response, not as the library hands it over.

    Both fixtures, because they fail differently: the prose blob is the only one
    with literal newlines, the dense-quote blob the only one where escaping is
    large enough to be visible end to end.
    """
    tok = _tok()

    for label, blobs in _offloading_cases():
        for hash_key, blob in sorted(blobs.items()):
            assert '"' in blob, f"{label}: fixture must exercise escaping"
            response = _mcp_response(hash_key)
            # Byte-exact, BEFORE counting anything: a miss must never be priced.
            assert json.loads(response)["original_content"] == blob, (
                f"{label}: handler did not return the payload for {hash_key[:8]} — "
                "a miss counted as a cost would understate the charge"
            )

            charged = retrieved_blob_tokens(blob, tok)
            raw = tok.count_text(blob)
            # The escaped form is what the model reads, and it is strictly larger.
            assert charged > raw, f"{label}: escaping is not token-neutral"
            # And it is the DOMINANT term of the real response, bar scaffolding.
            scaffold = tok.count_text(response) - charged
            assert SCAFFOLD_RANGE[0] <= scaffold <= SCAFFOLD_RANGE[1], (
                f"{label}: response minus escaped payload is {scaffold} tokens, outside the "
                f"measured {SCAFFOLD_RANGE} — the handler's response shape changed, so "
                "MCP_RESPONSE_SCAFFOLD_TOKENS is stale"
            )


@pytest.mark.slow
def test_scaffold_and_envelope_constants_bracket_the_real_response() -> None:
    """The two FIXED terms. ``MCP_RESPONSE_SCAFFOLD_TOKENS`` must sit inside the
    measured range, and the tool-result envelope must still be what we charge."""
    tok = _tok()

    # The constant is the measured MEDIAN, not merely inside the bound.
    assert MCP_RESPONSE_SCAFFOLD_TOKENS == SCAFFOLD_MEDIAN

    for label, blobs in _offloading_cases():
        for hash_key, blob in sorted(blobs.items()):
            response = _mcp_response(hash_key)
            assert json.loads(response)["original_content"] == blob
            envelope = tok.count_messages([{"role": "tool", "content": response}]) - tok.count_text(
                response
            )
            assert envelope == TOOL_RESULT_ENVELOPE_TOKENS, (
                f"{label}: a tool result now costs {envelope} envelope tokens, "
                f"not {TOOL_RESULT_ENVELOPE_TOKENS}"
            )


@pytest.mark.slow
def test_outgoing_call_constant_matches_a_real_retrieve_call() -> None:
    """The THIRD term. The model pays to ASK. The old constant was 12, an
    unmeasured guess; a real hash-only call is 28-34 tokens."""
    tok = _tok()

    # The constant is the CENTRE of the hash-space distribution, not a fixture's
    # value: a hash is 24 random hex characters and tokenises to 26..39.
    assert RETRIEVE_CALL_OVERHEAD_TOKENS == CALL_MEDIAN
    assert RETRIEVE_CALL_OVERHEAD_TOKENS > 12, (
        "12 was the pre-measurement guess; a real call cannot be that cheap"
    )
    # Random hex, seeded.
    rng = random.Random(0x5EED)
    sample = [
        tok.count_text(
            json.dumps(
                {
                    "name": "furl_retrieve",
                    "arguments": {
                        "hash": "".join(rng.choice("0123456789abcdef") for _ in range(24))
                    },
                }
            )
        )
        for _ in range(500)
    ]
    assert abs(statistics.median(sample) - RETRIEVE_CALL_OVERHEAD_TOKENS) <= 1, (
        f"median real call is {statistics.median(sample)}, not "
        f"{RETRIEVE_CALL_OVERHEAD_TOKENS} — the constant is off-centre"
    )

    for label, blobs in _offloading_cases():
        for hash_key in sorted(blobs):
            call = tok.count_text(
                json.dumps({"name": "furl_retrieve", "arguments": {"hash": hash_key}})
            )
            assert CALL_RANGE[0] <= call <= CALL_RANGE[1], (
                f"{label}: a real furl_retrieve call is {call} tokens, "
                f"outside the measured {CALL_RANGE}"
            )


@pytest.mark.slow
def test_total_charge_tracks_the_real_round_trip_within_the_scaffold_margin() -> None:
    """End to end: what we charge for one retrieval versus what one costs.

    The margin is the constant spread (scaffolding 6 + call 6), NOT a percentage
    — the point of recomputing the escaping is that the error stays constant
    instead of growing with payload size.

    RUN OVER BOTH FIXTURES, and the dense-quote one is why. Escaping the prose
    payload costs 2 tokens, so on that fixture alone this assertion stays GREEN
    when the charge reverts to raw bytes — measured, not supposed. On the
    dense-quote blob the same reversion is thousands of tokens and this goes red.
    """
    tok = _tok()

    for label, blobs in _offloading_cases():
        for hash_key, blob in sorted(blobs.items()):
            response = _mcp_response(hash_key)
            assert json.loads(response)["original_content"] == blob
            real = (
                tok.count_text(response)
                + (
                    tok.count_messages([{"role": "tool", "content": response}])
                    - tok.count_text(response)
                )
                + tok.count_text(
                    json.dumps({"name": "furl_retrieve", "arguments": {"hash": hash_key}})
                )
            )
            # This hash's ACTUAL call cost, not the constant: hash tokenisation spans 13 tokens,
            # and folding that into the margin would slacken it enough to hide a wrong constant.
            charged = (
                retrieved_blob_tokens(blob, tok)
                + MCP_RESPONSE_SCAFFOLD_TOKENS
                + TOOL_RESULT_ENVELOPE_TOKENS
                + tok.count_text(
                    json.dumps({"name": "furl_retrieve", "arguments": {"hash": hash_key}})
                )
            )
            margin = SCAFFOLD_RANGE[1] - SCAFFOLD_RANGE[0]
            assert abs(real - charged) <= margin, (
                f"{label}: charge {charged} vs real {real} for {hash_key[:8]}: "
                f"off by {real - charged}, beyond the {margin}-token constant margin"
            )
