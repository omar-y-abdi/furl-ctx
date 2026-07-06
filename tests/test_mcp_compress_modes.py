"""MCP integration tests for compress mode + section filtering (NR2-2 feature c).

``furl_compress`` gains ``mode`` (lossless_only / normal / aggressive, default
normal) and ``include_patterns`` / ``exclude_patterns``. Covered: each mode's
effect on a concrete payload, the default-unchanged guarantee (byte-identical
envelope), pattern-eligible/protected partitioning with verbatim protected
lines, and the error cases.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import (  # noqa: E402
    reset_compression_store,
)
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

# A JSON-array tool output large enough to trigger a real crush, so the modes
# have room to differ.
_PAYLOAD = json.dumps(
    [
        {"id": i, "path": f"src/module_{i}.py", "match": f"def fn_{i}(x): return x + {i}"}
        for i in range(60)
    ]
)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    item = result[0]
    assert isinstance(item, mt.TextContent)
    return json.loads(item.text)


async def _compress(server: FurlMCPServer, args: dict) -> dict:
    return _envelope(await server._handle_compress(args))


# ─── default unchanged ──────────────────────────────────────────────────────


async def test_default_call_envelope_keys_unchanged(server) -> None:
    # The mode/pattern feature must not change the default envelope shape.
    env = await _compress(server, {"content": _PAYLOAD})
    assert set(env) == {
        "compressed",
        "hash",
        "original_tokens",
        "compressed_tokens",
        "tokens_saved",
        "savings_percent",
        "transforms",
        "note",
    }
    assert "mode" not in env
    assert "filtered" not in env


async def test_mode_normal_equals_default(server) -> None:
    # Explicit mode="normal" must be identical to omitting mode entirely.
    default_env = await _compress(server, {"content": _PAYLOAD})
    reset_compression_store()
    normal_env = await _compress(FurlMCPServer(), {"content": _PAYLOAD, "mode": "normal"})
    assert normal_env["hash"] == default_env["hash"]
    assert normal_env["compressed"] == default_env["compressed"]
    assert normal_env["compressed_tokens"] == default_env["compressed_tokens"]


# ─── mode effects ───────────────────────────────────────────────────────────


async def test_aggressive_compresses_more_than_normal(server) -> None:
    normal = await _compress(server, {"content": _PAYLOAD})
    reset_compression_store()
    aggressive = await _compress(FurlMCPServer(), {"content": _PAYLOAD, "mode": "aggressive"})
    assert aggressive["compressed_tokens"] < normal["compressed_tokens"]


async def test_lossless_only_emits_no_ccr_markers(server) -> None:
    env = await _compress(server, {"content": _PAYLOAD, "mode": "lossless_only"})
    rendered = (
        env["compressed"] if isinstance(env["compressed"], str) else json.dumps(env["compressed"])
    )
    # lossless_only never drops/substitutes, so no <<ccr:...>> recovery pointer.
    assert "<<ccr:" not in rendered


async def test_lossless_only_round_trips_byte_exact(server) -> None:
    env = await _compress(server, {"content": _PAYLOAD, "mode": "lossless_only"})
    ret = _envelope(await server._handle_retrieve({"hash": env["hash"]}))
    assert ret["original_content"] == _PAYLOAD


async def test_unknown_mode_errors(server) -> None:
    env = await _compress(server, {"content": _PAYLOAD, "mode": "banana"})
    assert "unknown mode" in env["error"]


async def test_non_string_mode_errors(server) -> None:
    env = await _compress(server, {"content": _PAYLOAD, "mode": 5})
    # Exact: the interpolated type name must reach the caller (an int, here).
    assert env["error"] == "mode must be a string, got int"


# ─── section filtering ──────────────────────────────────────────────────────


async def test_exclude_pattern_protects_matching_lines(server) -> None:
    content = "alpha data line\nPROTECT this exact line\nbeta data line\n" + ("payload " * 80)
    env = await _compress(server, {"content": content, "exclude_patterns": ["PROTECT"]})
    assert env["filtered"] is True
    # The protected line survives verbatim in the rendered output.
    assert "PROTECT this exact line" in env["compressed"]
    # At least one eligible run was compressed and is retrievable.
    assert env["compressed_runs"] >= 1
    assert env["hashes"]


async def test_include_pattern_limits_eligibility(server) -> None:
    # Only lines containing "DATA" are eligible; the header line is protected.
    content = "HEADER keep me\n" + "\n".join(f"DATA row {i} value" for i in range(60))
    env = await _compress(server, {"content": content, "include_patterns": ["DATA"]})
    assert env["filtered"] is True
    assert "HEADER keep me" in env["compressed"]


async def test_filtered_run_hash_round_trips(server) -> None:
    content = "KEEP header\n" + "\n".join(f"log line number {i} here" for i in range(60))
    env = await _compress(server, {"content": content, "exclude_patterns": ["KEEP"]})
    # Each eligible run stored its own original; retrieving a run hash returns
    # that run's exact source lines.
    run_hash = env["hashes"][0]
    ret = _envelope(await server._handle_retrieve({"hash": run_hash}))
    assert "log line number" in ret["original_content"]
    assert "KEEP header" not in ret["original_content"]


async def test_glob_pattern_matches(server) -> None:
    # A bare glob (invalid as regex) falls back to fnmatch — the whole line
    # must match the glob. Use a wildcard that spans the line.
    content = "*.py touched\n" + "\n".join(f"other line {i}" for i in range(60))
    env = await _compress(server, {"content": content, "exclude_patterns": ["*touched*"]})
    assert env["filtered"] is True
    assert "*.py touched" in env["compressed"]


async def test_non_list_patterns_error(server) -> None:
    env = await _compress(server, {"content": _PAYLOAD, "include_patterns": "notalist"})
    assert env["error"] == "include_patterns must be a list of strings"


async def test_patterns_preserve_line_order(server) -> None:
    # Protected + eligible runs must reassemble in original order.
    content = "PROT one\n" + ("x " * 80) + "\nPROT two\n" + ("y " * 80) + "\nPROT three"
    env = await _compress(server, {"content": content, "exclude_patterns": ["PROT"]})
    out = env["compressed"]
    assert out.index("PROT one") < out.index("PROT two") < out.index("PROT three")
