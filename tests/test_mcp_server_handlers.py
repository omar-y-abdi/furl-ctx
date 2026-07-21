"""Integration tests for the MCP tool-handler plane (furl_ctx/ccr/mcp_server.py).

The user-facing handler surface — ``call_tool`` routing plus the four
``_handle_*`` entries — was the biggest real coverage hole (28%). These tests
drive each handler through its public async entry against a REAL in-process CCR
store (no mocked store, no mocked compressor), asserting the structured JSON
envelopes that an MCP host actually receives.

Coverage map (mcp_server.py):
    call_tool routing / unknown-tool ......... :500, :520
    _handle_compress missing-param ........... :543, :550
    _handle_retrieve missing-param ........... :560, :567
    _handle_stats combined savings ........... :587, :621
    _handle_read missing/not-found/not-file .. :627, :638, :647, :654

Requires the optional ``mcp`` extra (``pip install 'mcp>=1.0.0'``); the file is
skipped wholesale where the SDK is absent, so the coverage gain only reproduces
where ``mcp`` is installed.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

import furl_ctx.ccr.regex_budget as regex_budget  # noqa: E402
from furl_ctx.cache.compression_store import (  # noqa: E402
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.ccr import mcp_server  # noqa: E402
from furl_ctx.ccr.mcp_server import (  # noqa: E402
    CCR_TOOL_NAME,
    COMPRESS_TOOL_NAME,
    READ_TOOL_NAME,
    STATS_TOOL_NAME,
    FurlMCPServer,
)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    # The CCR store is a process-singleton; reset around every test so a
    # round-trip in one test cannot leak entries into another's stats/retrieve.
    # furl_read is jailed to $FURL_WORKSPACE_DIR (default cwd). Point it
    # at the per-test sandbox so file-read happy-path tests run inside the jail.
    # The shared-stats paths follow the same env per call (SEC-7:
    # shared_stats_file() re-reads FURL_WORKSPACE_DIR), so this single setenv
    # also keeps record_compression() from ever writing to ~/.furl.
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    # The session TTL is env-aware now (_mcp_session_ttl): the COR-51 tests
    # below pin the env-UNSET contract (MCP_SESSION_TTL), so the premise must
    # be established — earlier suite files (e.g. in-process cli.main() runs)
    # legitimately leave their own FURL_CCR_TTL_SECONDS setdefault behind.
    monkeypatch.delenv("FURL_CCR_TTL_SECONDS", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    """Decode the single-TextContent JSON envelope an MCP host receives."""
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    item = result[0]
    assert isinstance(item, mt.TextContent)
    assert item.type == "text"
    return json.loads(item.text)


def _get_call_tool(server: FurlMCPServer):
    """Return the routing handler via its public attribute (TEST-22).

    ``FurlMCPServer`` exposes the registered ``call_tool`` closure as
    ``route_call_tool`` exactly so tests can exercise the ROUTING /
    unknown-tool branches without introspecting the MCP SDK's private
    wrapper closure — the old closure-cell walk broke on any ``mcp`` bump.
    """
    return server.route_call_tool


# ─── _handle_compress / _handle_retrieve missing-param envelopes ────────────


async def test_compress_missing_content_returns_error_envelope(server) -> None:
    # :550 — no `content` key → structured error, not a crash.
    env = _envelope(await server._handle_compress({}))
    assert env == {"error": "content parameter is required"}


async def test_compress_empty_content_is_falsy_and_errors(server) -> None:
    # Boundary: "" is falsy, so the guard fires exactly as for an absent key.
    env = _envelope(await server._handle_compress({"content": ""}))
    assert env == {"error": "content parameter is required"}


async def test_retrieve_missing_hash_returns_error_envelope(server) -> None:
    # :567 — no `hash` key → structured error.
    env = _envelope(await server._handle_retrieve({}))
    assert env == {"error": "hash parameter is required"}


@pytest.mark.parametrize(
    "bad_hash",
    [
        "abc",  # too short for any width
        "a" * 13,  # off-by-one over width 12
        "a" * 23,  # off-by-one under width 24
        "g" * 12,  # right width, non-hex char
        "<<ccr:" + "a" * 6,  # marker text, not a bare key
    ],
)
async def test_retrieve_malformed_hash_rejected_before_store(server, bad_hash) -> None:
    # The width+charset spoofing guard (is_valid_ccr_hash) rejects a present but
    # malformed key with a loud 400, matching the tool-call parse path — the key
    # never reaches the store. Distinct from the missing-hash and the
    # valid-but-unknown ("0"*24 → loud miss) envelopes.
    env = _envelope(await server._handle_retrieve({"hash": bad_hash}))
    assert env == {"error": "invalid hash format (expected 12 or 24 lowercase-hex chars)"}


# ─── _handle_read missing / not-found / not-a-file envelopes ────────────────


async def test_read_missing_file_path_returns_error_envelope(server) -> None:
    # :638 — `_handle_read` validates file_path itself (independent of the
    # FURL_MCP_READ routing flag at :514), so a bound call reaches it.
    env = _envelope(await server._handle_read({}))
    assert env == {"error": "file_path parameter is required"}


async def test_read_nonexistent_path_reports_not_found(server, tmp_path: Path) -> None:
    # A path INSIDE the workspace jail that does not resolve to anything on disk
    # (passes the jail, reaches the existence check).
    env = _envelope(await server._handle_read({"file_path": str(tmp_path / "zzz.txt")}))
    assert env["error"].startswith("File not found:")
    assert "zzz.txt" in env["error"]


async def test_read_directory_reports_not_a_file(server, tmp_path: Path) -> None:
    # :654 — a real path that exists but is a directory, not a file.
    env = _envelope(await server._handle_read({"file_path": str(tmp_path)}))
    assert env["error"].startswith("Not a file:")


async def test_read_real_file_returns_numbered_content(server, tmp_path: Path) -> None:
    # Real-I/O happy path: a file on disk → line-numbered content (Read-tool
    # shape), with each line prefixed by a right-justified 1-based line number.
    f = tmp_path / "sample.txt"
    f.write_text("alpha\nbeta\ngamma")
    result = await server._handle_read({"file_path": str(f)})
    assert len(result) == 1
    text = result[0].text
    assert text == "     1\talpha\n     2\tbeta\n     3\tgamma"


async def test_read_outside_workspace_is_rejected(server, tmp_path: Path) -> None:
    # A path outside the workspace jail is rejected BEFORE any existence probe
    # (works even for a path that doesn't exist — no file-existence oracle), with
    # a generic message that never echoes the attempted path back to the model.
    escapee = tmp_path.parent / "escapee_outside_jail.txt"
    env = _envelope(await server._handle_read({"file_path": str(escapee)}))
    assert env["error"] == "path outside workspace"
    assert "escapee" not in env["error"]


async def test_read_oversized_file_is_rejected(server, tmp_path: Path, monkeypatch) -> None:
    # Files past the byte cap are rejected by stat() BEFORE read_bytes(), so an
    # oversized file is never allocated into memory (OOM DoS guard).
    monkeypatch.setattr(mcp_server, "_MAX_READ_BYTES", 16)
    big = tmp_path / "big.txt"
    big.write_text("x" * 64)
    env = _envelope(await server._handle_read({"file_path": str(big)}))
    assert env["error"].startswith("File too large to read:")


# ─── clean compress → retrieve round-trip (real store) ──────────────────────


async def test_compress_then_retrieve_round_trip(server) -> None:
    payload = "def add(a, b):\n    return a + b\n" * 40

    comp = _envelope(await server._handle_compress({"content": payload}))
    # Compress envelope contract: a retrieval hash + token accounting.
    assert set(comp).issuperset(
        {"compressed", "hash", "original_tokens", "compressed_tokens", "tokens_saved"}
    )
    hash_key = comp["hash"]
    assert isinstance(hash_key, str) and hash_key

    ret = _envelope(await server._handle_retrieve({"hash": hash_key}))
    # Retrieving by the issued hash returns the byte-for-byte original.
    assert ret["hash"] == hash_key
    assert ret["source"] == "local"
    assert ret["original_content"] == payload


async def test_retrieve_unknown_hash_reports_loud_miss(server) -> None:
    # Boundary: a hash that was never stored must error loudly (cause-honest
    # miss), never a silent empty success.
    env = _envelope(await server._handle_retrieve({"hash": "0" * 24}))
    assert "error" in env
    assert env["hash"] == "0" * 24


# ─── _handle_stats combined-savings aggregation + all_input boundary ────────


def _write_shared_events(path: Path, events: list[dict]) -> None:
    foreign_pid = 10_000_000  # not this process → counted as a sub-agent
    now = time.time()
    lines = []
    for evt in events:
        record = {"timestamp": now, "pid": foreign_pid, **evt}
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")


async def test_stats_aggregates_subagent_savings(server, tmp_path) -> None:
    # Drive the cross-process aggregation branch (:603-623): a sub-agent
    # (foreign pid) compressed 1000 → 100 tokens. With this session's own
    # stats at zero, all_input = 1000, all_saved = 900 → 90.0%.
    # The autouse fixture's FURL_WORKSPACE_DIR=tmp_path makes this exact path
    # what shared_stats_file() resolves to (SEC-7 live paths).
    stats_file = tmp_path / "session_stats.jsonl"
    _write_shared_events(
        stats_file,
        [{"type": "compress", "input_tokens": 1000, "output_tokens": 100}],
    )

    env = _envelope(await server._handle_stats())

    assert "combined" in env, "sub-agent events must surface a combined block"
    combined = env["combined"]
    assert combined["total_tokens_saved"] == 900
    assert combined["savings_percent"] == 90.0
    assert env["sub_agents"]["compressions"] == 1
    assert env["sub_agents"]["tokens_saved"] == 900


async def test_stats_all_input_zero_boundary_returns_zero(server, tmp_path) -> None:
    # :621 boundary — a sub-agent event with input_tokens == 0 enters the
    # aggregation block (other_events non-empty) but yields all_input == 0,
    # so savings_percent must take the `else 0` branch (no ZeroDivisionError).
    stats_file = tmp_path / "session_stats.jsonl"  # == shared_stats_file() under the fixture env
    _write_shared_events(
        stats_file,
        [{"type": "compress", "input_tokens": 0, "output_tokens": 0}],
    )

    env = _envelope(await server._handle_stats())

    assert "combined" in env
    assert env["combined"]["savings_percent"] == 0
    assert env["combined"]["total_tokens_saved"] == 0


async def test_stats_no_subagent_events_has_no_combined_block(server, tmp_path) -> None:
    # Boundary complement: with NO foreign events, the aggregation block is
    # skipped entirely — no `combined`/`sub_agents` keys, only base stats.
    stats_file = tmp_path / "session_stats.jsonl"  # == shared_stats_file() under the fixture env
    stats_file.write_text("")  # empty → no events

    env = _envelope(await server._handle_stats())

    assert "combined" not in env
    assert "sub_agents" not in env
    assert env["compressions"] == 0


# ─── call_tool routing + unknown-tool (single closure-driven test) ──────────


async def test_call_tool_routes_and_rejects_unknown(server) -> None:
    call_tool = _get_call_tool(server)

    # Unknown tool → routing falls through to the unknown-tool envelope (:520).
    unknown = _envelope(await call_tool("nonexistent_tool", {}))
    assert unknown == {"error": "Unknown tool: nonexistent_tool"}

    # Known tool → routed to its handler (:512), returns the stats envelope.
    stats = _envelope(await call_tool(STATS_TOOL_NAME, {}))
    assert "compressions" in stats and "savings_percent" in stats

    # End-to-end through the dispatcher: compress then retrieve round-trips.
    comp = _envelope(await call_tool(COMPRESS_TOOL_NAME, {"content": "round trip me"}))
    again = _envelope(await call_tool(CCR_TOOL_NAME, {"hash": comp["hash"]}))
    assert again["original_content"] == "round trip me"


def test_read_tool_name_is_distinct_constant() -> None:
    # Guard the four tool-name constants are distinct (a copy/paste collision
    # would silently misroute call_tool).
    names = {COMPRESS_TOOL_NAME, CCR_TOOL_NAME, STATS_TOOL_NAME, READ_TOOL_NAME}
    assert len(names) == 4


# ─── COR-32 / COR-51 / COR-54 — retrieve-plane data-integrity regressions ───


@pytest.mark.parametrize("case_fn", [str.upper, str.title], ids=["upper", "title"])
async def test_retrieve_case_variant_hash_hits_lowercase_store_key(server, case_fn) -> None:
    """COR-32: ``is_valid_ccr_hash`` is case-insensitive but store keys are
    lowercase — an upper/title-cased hash passed the format guard then MISSED
    in the store with a confusing eviction-style error. Ingress normalizes."""
    h = "abcdef123456"
    store = server._get_local_store()
    store.store(original='[{"id": 7, "v": "needle"}]', compressed=f"<<ccr:{h}>>", explicit_hash=h)

    env = _envelope(await server._handle_retrieve({"hash": case_fn(h)}))

    assert "error" not in env, f"case-variant hash missed: {env}"
    assert env["hash"] == h  # response reports the normalized store key
    assert "needle" in env["original_content"]


def test_local_store_singleton_carries_session_ttl(server) -> None:
    """COR-51: the MCP process's store singleton is configured with the
    session TTL on first init (absent FURL_CCR_TTL_SECONDS — when set, the
    env wins, see tests/test_mcp_ttl_env.py), and it IS the process singleton
    the pipeline's own no-arg ``get_compression_store()`` call resolves to."""
    store = server._get_local_store()
    assert store.default_ttl_seconds == mcp_server.MCP_SESSION_TTL
    assert get_compression_store() is store


def test_pipeline_marker_rows_carry_session_ttl(server, monkeypatch) -> None:
    """COR-51: the pipeline inside ``_compress_content`` persists dropped rows
    under the marker hash embedded in the compressed text WITHOUT an explicit
    ttl. Those rows must inherit MCP_SESSION_TTL, not the stock 1800 s default
    — otherwise granular retrieval via the embedded marker missed mid-session
    while the wrapper hash advertised session persistence. This also
    pins ordering: the singleton must be configured BEFORE compress() runs its
    own config-on-first-init ``get_compression_store()`` call."""
    # furl_ctx/__init__.py re-exports the compress FUNCTION, shadowing the
    # submodule attribute — resolve the real module for patching.
    compress_mod = importlib.import_module("furl_ctx.compress")

    marker_hash = "abc123def456"

    def fake_compress(messages, **kwargs):
        # Mimic the pipeline: persist dropped rows through the process
        # singleton with NO explicit ttl (the default-TTL path), then embed
        # the marker in the compressed text.
        get_compression_store().store(
            original='[{"id": 1, "v": "dropped row"}]',
            compressed=f"<<ccr:{marker_hash}>>",
            explicit_hash=marker_hash,
        )
        return types.SimpleNamespace(
            messages=[{"role": "tool", "content": f"crushed <<ccr:{marker_hash}>>"}],
            tokens_before=100,
            tokens_after=10,
            transforms_applied=["smart_crusher"],
            error=None,  # part of the CompressResult interface the stub mimics
            opaque_offloads=[],  # part of the CompressResult interface the stub mimics
        )

    monkeypatch.setattr(compress_mod, "compress", fake_compress)
    result = server._compress_content("row " * 200)

    store = get_compression_store()
    marker_entry = store.retrieve(marker_hash)
    assert marker_entry is not None, "embedded marker hash must resolve"
    assert marker_entry.ttl == mcp_server.MCP_SESSION_TTL
    wrapper_entry = store.retrieve(result["hash"])
    assert wrapper_entry is not None
    assert wrapper_entry.ttl == mcp_server.MCP_SESSION_TTL


async def test_query_response_with_infinity_falls_back_to_byte_exact(server, monkeypatch) -> None:
    """COR-54 backstop: if a query-path result somehow materializes float
    inf/nan past the store's numeric-fidelity fallback, ``_handle_retrieve``
    must not emit RFC-invalid bare ``Infinity`` — it returns the byte-exact
    no-query response instead."""
    h = "abcdef123456"
    raw = '[{"reading": 1e400, "note": "needle"}]'
    store = server._get_local_store()
    store.store(original=raw, compressed=f"<<ccr:{h}>>", explicit_hash=h)
    # Force the (normally unreachable) lossy search result past the store's
    # numeric-fidelity fallback.
    monkeypatch.setattr(store, "search", lambda hk, q, **kw: [{"reading": float("inf")}])

    result = await server._handle_retrieve({"hash": h, "query": "reading"})

    text = result[0].text
    env = json.loads(
        text,
        parse_constant=lambda name: pytest.fail(f"bare {name} in MCP response"),
    )
    assert env["original_content"] == raw  # byte-exact no-query fallback


# ─── T11: refuse agent-supplied regex filters when RE2 is unavailable ───────
#
# furl_retrieve's `pattern` and furl_compress's `include_patterns`/
# `exclude_patterns` are matched inside run_in_executor / asyncio.to_thread
# (_compress_content / _retrieve_content), off the event loop, where no
# SIGALRM watchdog can ever arm (Python signal handlers are main-thread only).
# Without RE2 a crafted pattern used to fall back to unbounded stdlib `re`
# there, and CPython's `sre` holds the GIL for the whole match, so a wedged
# worker thread freezes every session on the process -- not just the caller's
# own request. These pin the fix (furl_ctx.ccr.mcp_server._refuse_regex_filters):
# the handler refuses the call outright instead of ever reaching that path.


@pytest.fixture
def without_re2(monkeypatch):
    """Force RE2 genuinely absent, not just the imported ``re2_available`` name.

    Patches ``regex_budget._RE2`` (the module-level engine handle both
    ``mcp_server.re2_available`` and the actual matcher read at call time), so
    a reproduction that somehow bypassed the T11 guard would take the real
    unbounded fallback path -- exactly as it did before the fix -- rather than
    being trivially "safe" only because the mock made it so.
    """
    monkeypatch.setattr(regex_budget, "_RE2", None)
    regex_budget._compile_re2.cache_clear()
    yield
    regex_budget._compile_re2.cache_clear()


def _seed(server: FurlMCPServer, hash_key: str, content: str) -> None:
    server._get_local_store().store(original=content, compressed="c", explicit_hash=hash_key)


# Calibrated against this handler directly (see PR description for the run):
# "ab" * 20 against "(a|b|ab)+Z" costs ~0.5s unbounded -- long enough to prove
# the guard fires before dispatch (a refusal is microseconds), short enough
# that even an orphaned worker thread, were the guard somehow bypassed, could
# not stall the suite. The bounded wait_for below is a second, independent
# backstop against an actual hang, matching the daemon-thread-join pattern
# test_regex_budget.py uses for the same class of risk.
_CATASTROPHIC_PATTERN = "(a|b|ab)+Z"
_CATASTROPHIC_TEXT = "ab" * 20
_REFUSAL_WAIT_FOR_SECONDS = 10.0
_NOT_FROZEN_CEILING_SECONDS = 5.0


async def test_compress_refuses_catastrophic_include_pattern_without_re2(
    server, without_re2, monkeypatch
) -> None:
    """T11 RED/GREEN: RE2 forced absent, furl_compress's include_patterns must
    refuse a catastrophic-backtracking pattern instead of running it unbounded
    on the worker thread. Fails before the fix (the call completes normally in
    ~0.5s, having run the pattern to completion on real content); passes after
    (refused in microseconds, never dispatched to run_in_executor).

    F4 hardening: spies on the running loop's ``run_in_executor``, the actual
    compress dispatch primitive, and asserts it is never called on the
    refusal path -- not just that a fast error came back. A response-shape
    and timing assertion alone would still pass a future refactor that moved
    the RE2 check to inside ``_compress_content`` (still fast, still an
    error, but AFTER the pattern was already handed to a worker thread); this
    assertion is what would catch that regression. See the PR description
    for the empirical red-proof of this exact scenario.
    """
    loop = asyncio.get_running_loop()
    original_run_in_executor = loop.run_in_executor
    executor_calls: list[tuple[object, ...]] = []

    def _spy_run_in_executor(*args: object, **kwargs: object) -> object:
        executor_calls.append(args)
        return original_run_in_executor(*args, **kwargs)

    monkeypatch.setattr(loop, "run_in_executor", _spy_run_in_executor)

    start = time.monotonic()
    result = await asyncio.wait_for(
        server._handle_compress(
            {"content": _CATASTROPHIC_TEXT, "include_patterns": [_CATASTROPHIC_PATTERN]}
        ),
        timeout=_REFUSAL_WAIT_FOR_SECONDS,
    )
    elapsed = time.monotonic() - start
    env = _envelope(result)
    assert elapsed < _NOT_FROZEN_CEILING_SECONDS, f"took {elapsed:.2f}s -- did it run unbounded?"
    assert "error" in env, f"expected a refusal envelope, got a result: {env}"
    assert "RE2" in env["error"]
    assert executor_calls == [], (
        "run_in_executor was called on the refusal path -- the pattern was "
        "dispatched to a worker thread instead of being refused before it "
        f"ever got there: {executor_calls!r}"
    )


async def test_retrieve_refuses_catastrophic_pattern_without_re2(
    server, without_re2, monkeypatch
) -> None:
    """T11 RED/GREEN: same hazard, furl_retrieve's `pattern`. Before the fix the
    catastrophic match runs to completion inside asyncio.to_thread
    (_retrieve_content); after, it is refused before that dispatch.

    F4 hardening: spies on ``asyncio.to_thread``, the actual retrieve dispatch
    primitive, and asserts it is never called on the refusal path -- the
    retrieve-side counterpart of the compress spy above, for the same reason:
    a refusal that fires only after dispatch would still look fast and
    error-shaped from the outside.
    """
    h = "aaaa1111aaaa"
    _seed(server, h, _CATASTROPHIC_TEXT)

    original_to_thread = asyncio.to_thread
    to_thread_calls: list[tuple[object, ...]] = []

    async def _spy_to_thread(*args: object, **kwargs: object) -> object:
        to_thread_calls.append(args)
        return await original_to_thread(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

    start = time.monotonic()
    result = await asyncio.wait_for(
        server._handle_retrieve({"hash": h, "pattern": _CATASTROPHIC_PATTERN}),
        timeout=_REFUSAL_WAIT_FOR_SECONDS,
    )
    elapsed = time.monotonic() - start
    env = _envelope(result)
    assert elapsed < _NOT_FROZEN_CEILING_SECONDS, f"took {elapsed:.2f}s -- did it run unbounded?"
    assert "error" in env, f"expected a refusal envelope, got a result: {env}"
    assert "RE2" in env["error"]
    assert to_thread_calls == [], (
        "asyncio.to_thread was called on the refusal path -- the pattern was "
        "dispatched to a worker thread instead of being refused before it "
        f"ever got there: {to_thread_calls!r}"
    )


async def test_compress_refuses_benign_include_pattern_without_re2(server, without_re2) -> None:
    """T11: the refusal is unconditional on RE2 absence, not on how dangerous a
    pattern looks -- without RE2, ingress cannot tell a benign filter from a
    catastrophic one (classify_boundability returns UNKNOWN either way), so
    even an everyday pattern like "ERROR.*" is refused rather than guessed at."""
    env = _envelope(
        await server._handle_compress(
            {"content": "line one\nERROR two\n", "include_patterns": ["ERROR.*"]}
        )
    )
    assert "error" in env
    assert "RE2" in env["error"]


async def test_retrieve_refuses_benign_pattern_without_re2(server, without_re2) -> None:
    """T11: the retrieve-side counterpart of the benign-pattern refusal above."""
    h = "bbbb2222bbbb"
    _seed(server, h, "line one\nERROR two\n")
    env = _envelope(await server._handle_retrieve({"hash": h, "pattern": "ERROR.*"}))
    assert "error" in env
    assert "RE2" in env["error"]


async def test_compress_without_patterns_unaffected_by_missing_re2(server, without_re2) -> None:
    """T11 must not over-refuse: a plain compress call with no include/exclude
    patterns never reaches the regex engine at all, so it must work exactly as
    it does with RE2 present -- RE2 absence alone is not an error."""
    env = _envelope(await server._handle_compress({"content": "line one\nline two\n"}))
    assert "error" not in env
    assert "compressed" in env


async def test_retrieve_line_range_unaffected_by_missing_re2(server, without_re2) -> None:
    """T11 must not over-refuse: line_range/fields/select filters carry no
    regex at all -- only a `pattern` is the hazard this guard exists for."""
    h = "cccc3333cccc"
    _seed(server, h, "line one\nline two\nline three\n")
    env = _envelope(await server._handle_retrieve({"hash": h, "line_range": [1, 2]}))
    assert "error" not in env
    assert env["filtered"] is True


async def test_compress_include_pattern_works_normally_with_re2_present(server) -> None:
    """T11 regression guard: with RE2 present (the default test environment,
    and the common `pip install furl-ctx[mcp]` path), include_patterns must
    keep working exactly as before -- the new guard must never fire when
    re2_available() is True."""
    assert regex_budget.re2_available(), "precondition: this test needs real RE2"
    env = _envelope(
        await server._handle_compress(
            {"content": "keep this\nERROR this\n", "include_patterns": ["ERROR.*"]}
        )
    )
    assert "error" not in env
    assert env["filtered"] is True


async def test_retrieve_pattern_works_normally_with_re2_present(server) -> None:
    """T11 regression guard: with RE2 present, furl_retrieve's pattern filter
    must keep matching normally, unaffected by the new refusal branch."""
    assert regex_budget.re2_available(), "precondition: this test needs real RE2"
    h = "dddd4444dddd"
    _seed(server, h, "alpha\nERROR beta\ngamma")
    env = _envelope(await server._handle_retrieve({"hash": h, "pattern": "ERROR"}))
    assert "error" not in env
    assert env["matched_count"] == 1
