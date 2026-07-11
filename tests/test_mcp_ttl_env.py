"""FURL_CCR_TTL_SECONDS governs MCP-stored entry lifetimes (round-6 finding A).

The MCP server used to pass a hard-coded ``ttl=MCP_SESSION_TTL`` (3600 s) at
every store site, OVERRIDING the store default built from the plugin's
``FURL_CCR_TTL_SECONDS=86400`` env — so furl_compress / furl_read entries died
at 1 h while hook-compressed entries in the very same per-project store lived
24 h (proven on a live evaluator store: ttl=3600 rows beside ttl=86400 rows).

Pinned here:

1. env set + valid  → MCP-stored entries carry the env TTL (compress + read).
2. env unset        → the bare-server default 3600 s, byte-identical to before
   env support existed (no regression for bare-MCP users).
3. env invalid      → 3600 s + one logged WARNING, never a crash.
4. Lifetime lockstep: the wrapper hash (explicit ttl) and the store default
   that governs embedded dropped-row entries resolve to the SAME value — on
   the singleton path, and on the plugin's namespace path where the marker
   entries themselves are checked.
"""

from __future__ import annotations

import logging
import re

import pytest

pytest.importorskip("mcp")

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import (  # noqa: E402
    MCP_SESSION_TTL,
    FurlMCPServer,
    _mcp_session_ttl,
)

# Small, distinct content — enough for a real compress() pass; the wrapper hash
# is stored regardless of savings.
_TINY = "The quick brown fox jumps over the lazy dog near the river bank at dawn."

# Big enough to force at least one CCR offload (embedded ``<<ccr:HASH>>``
# markers) — the same shape tests/test_cli_persistence.py relies on.
_OFFLOAD_PAYLOAD = "\n".join(f"line {i} padding-token-{i} more-filler-{i}" for i in range(1, 401))

_MARKER_RE = re.compile(r"<<ccr:([0-9a-f]{12,24})>>")


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    """Sandbox the durable store and clear every TTL/namespace knob.

    FURL_CCR_BACKEND=sqlite (in a scratch FURL_WORKSPACE_DIR, so the real
    ``~/.furl`` is never touched): ``_compress_content`` and ``_handle_read``
    store with ``require_durable=True``, which vetoes the volatile in-memory
    backend — the durable default IS the deployment under test.
    """
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.delenv("FURL_CCR_TTL_SECONDS", raising=False)
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    reset_compression_store()
    yield monkeypatch
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _stored_entry(server: FurlMCPServer, hash_key: str):
    entry = server._get_local_store()._backend.get(hash_key)
    assert entry is not None, f"stored hash {hash_key} not found in the active store"
    return entry


# ─── the resolver itself ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, MCP_SESSION_TTL),  # unset → bare-server default
        ("", MCP_SESSION_TTL),  # blank → default
        ("   ", MCP_SESSION_TTL),  # whitespace → default
        ("7200", 7200),  # valid → env wins
        ("86400", 86400),  # the plugin's shipped value
        ("1", 1),  # smallest valid
        ("not-a-number", MCP_SESSION_TTL),  # non-int → default
        ("1.5", MCP_SESSION_TTL),  # int() rejects floats → default
        ("0", MCP_SESSION_TTL),  # non-positive → default
        ("-5", MCP_SESSION_TTL),  # negative → default
    ],
)
def test_mcp_session_ttl_resolution(monkeypatch, raw: str | None, expected: int) -> None:
    if raw is None:
        monkeypatch.delenv("FURL_CCR_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("FURL_CCR_TTL_SECONDS", raw)
    assert _mcp_session_ttl() == expected


def test_invalid_env_logs_a_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("FURL_CCR_TTL_SECONDS", "banana")
    with caplog.at_level(logging.WARNING, logger="furl_ctx.ccr.mcp"):
        assert _mcp_session_ttl() == MCP_SESSION_TTL
    assert any("FURL_CCR_TTL_SECONDS" in record.message for record in caplog.records), (
        "an invalid TTL env var must be surfaced with a WARNING, not swallowed"
    )


# ─── furl_compress store path ────────────────────────────────────────────────


def test_env_set_wins_for_compress_entries(_isolate_store, server) -> None:
    """Env set → the wrapper hash carries the env TTL, and the store default
    (which governs embedded dropped-row entries) matches it — lockstep."""
    _isolate_store.setenv("FURL_CCR_TTL_SECONDS", "7200")
    out = server._compress_content(_TINY)
    assert "error" not in out, out
    entry = _stored_entry(server, out["hash"])
    assert entry.ttl == 7200
    assert server._get_local_store().default_ttl_seconds == 7200


def test_env_unset_keeps_the_3600_session_default(server) -> None:
    """No env → exactly the pre-fix bare-MCP behavior: 3600 s everywhere."""
    out = server._compress_content(_TINY)
    assert "error" not in out, out
    entry = _stored_entry(server, out["hash"])
    assert entry.ttl == MCP_SESSION_TTL == 3600
    assert server._get_local_store().default_ttl_seconds == MCP_SESSION_TTL


@pytest.mark.parametrize("bad", ["not-a-number", "-5", "0"])
def test_env_invalid_falls_back_to_3600_and_warns(_isolate_store, server, caplog, bad) -> None:
    _isolate_store.setenv("FURL_CCR_TTL_SECONDS", bad)
    with caplog.at_level(logging.WARNING, logger="furl_ctx.ccr.mcp"):
        out = server._compress_content(_TINY)
    assert "error" not in out, out
    assert _stored_entry(server, out["hash"]).ttl == MCP_SESSION_TTL
    assert any("FURL_CCR_TTL_SECONDS" in record.message for record in caplog.records)


# ─── furl_read store path ────────────────────────────────────────────────────


async def test_env_set_wins_for_read_entries(_isolate_store, server, tmp_path) -> None:
    _isolate_store.setenv("FURL_CCR_TTL_SECONDS", "7200")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    sample = ws / "sample.txt"
    sample.write_text("hello\nworld\n", encoding="utf-8")

    result = await server._handle_read({"file_path": str(sample)})
    assert "error" not in result[0].text.lower() or "hello" in result[0].text

    assert server._file_cache, "a durable read must seed the file cache"
    (_, ccr_hash, _, _) = next(iter(server._file_cache.values()))
    assert _stored_entry(server, ccr_hash).ttl == 7200


async def test_env_unset_read_entries_keep_3600(server, tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    sample = ws / "sample.txt"
    sample.write_text("hello\nworld\n", encoding="utf-8")

    await server._handle_read({"file_path": str(sample)})

    assert server._file_cache, "a durable read must seed the file cache"
    (_, ccr_hash, _, _) = next(iter(server._file_cache.values()))
    assert _stored_entry(server, ccr_hash).ttl == MCP_SESSION_TTL


# ─── namespace path: the exact evaluator scenario ────────────────────────────


def test_namespace_wrapper_and_dropped_rows_share_the_env_lifetime(
    _isolate_store, server, tmp_path
) -> None:
    """Plugin deployment shape: per-project namespace + env TTL 86400.

    Pre-fix, the wrapper hash was stored with an explicit ttl=3600 while the
    namespace store's env-derived default gave dropped-row marker entries
    86400 — the forensically observed 3600-beside-86400 split. Post-fix every
    surface of one compress call carries the SAME env lifetime.
    """
    _isolate_store.setenv("FURL_CCR_TTL_SECONDS", "86400")
    _isolate_store.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))

    out = server._compress_content(_OFFLOAD_PAYLOAD)
    assert "error" not in out, out

    store = server._get_local_store()
    assert store.default_ttl_seconds == 86400  # governs dropped-row entries

    wrapper = _stored_entry(server, out["hash"])
    assert wrapper.ttl == 86400  # env wins over the old hard-coded 3600

    markers = _MARKER_RE.findall(out["compressed"])
    assert markers, "offload payload must embed at least one <<ccr:HASH>> marker"
    for marker_hash in markers:
        dropped = store._backend.get(marker_hash)
        assert dropped is not None, f"marker {marker_hash} has no stored original"
        assert dropped.ttl == wrapper.ttl == 86400, (
            "dropped-row entries and the wrapper hash must share one lifetime"
        )
