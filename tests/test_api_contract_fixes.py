"""Contract fixes for the public API surface (FABLE items API-1/2/4/5/12/15/16).

Pins the behavior changes of the API batch:

* API-1  — the nine never-raised exception classes and the dead
  ``CacheConfig``/``CacheStrategy`` pair are no longer exported at the top
  level; ``FurlError`` (the reserved base) survives.
* API-2  — ``CompressContext.user_query`` is populated BEFORE the hook
  invocations; ``post_compress`` fires on every success-path outcome
  (including zero savings and the inflation-guard revert); ``ccr_hashes``
  carries the CCR recovery hashes newly surfaced by this compression.
* API-4  — ``previous_prefix_hash`` (CacheAligner's documented kwarg) passes
  the pipeline broadcast without a ContentRouter TypeError; the stale
  ``record_metrics`` allowlist entry is gone.
* API-5  — the env-selected CCR backend loader raises on explicit-backend
  failure instead of silently downgrading to the in-memory store, and takes
  factory kwargs from ``FURL_CCR_BACKEND_OPTS`` (JSON) instead of
  hardcoded Redis-shaped ones.
* API-12 — ``furl_ctx.transforms.CompressionStrategy`` lazy-binds to the
  115-line ``router_policy`` owner, not the whole ``content_router`` chain.
* API-15 — MCP non-string params get a parameter error (mirroring the hash
  guard); genuine handler failures re-raise (sanitized) so the SDK marks
  ``isError=True`` instead of returning success-shaped ``{"error": ...}``.
* API-16 — ``output_buffer`` / ``tool_profiles`` are no longer silently
  ignored pipeline kwargs: the router rejects them loudly.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import furl_ctx
from furl_ctx import compress
from furl_ctx.cache.compression_store import (
    _create_default_ccr_backend,
    reset_compression_store,
)
from furl_ctx.ccr.marker_grammar import is_valid_ccr_hash
from furl_ctx.hooks import CompressEvent, CompressionHooks


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    yield
    reset_compression_store()


class _RecordingHooks(CompressionHooks):
    """Records the context seen by each hook and every post_compress event."""

    def __init__(self) -> None:
        self.pre_query: str | None = None
        self.bias_query: str | None = None
        self.events: list[CompressEvent] = []

    def pre_compress(self, messages, ctx):
        self.pre_query = ctx.user_query
        return messages

    def compute_biases(self, messages, ctx):
        self.bias_query = ctx.user_query
        return {}

    def post_compress(self, event: CompressEvent) -> None:
        self.events.append(event)


def _droppable_tool_messages() -> list[dict]:
    """A conversation whose tool output reliably takes the lossy CCR path."""
    rows = [{"id": i, "msg": f"record-{i}-distinct-payload"} for i in range(1000)]
    return [
        {"role": "user", "content": "find the fatal error in these records"},
        {"role": "tool", "content": json.dumps(rows)},
    ]


# ─── API-2: hooks population ────────────────────────────────────────────────


def test_hooks_see_user_query_before_compression() -> None:
    hooks = _RecordingHooks()
    compress(_droppable_tool_messages(), model="gpt-4o", hooks=hooks)
    assert hooks.pre_query == "find the fatal error in these records"
    assert hooks.bias_query == "find the fatal error in these records"


def test_post_compress_fires_on_zero_savings() -> None:
    """The negative class (nothing compressed) must be observable."""
    hooks = _RecordingHooks()
    result = compress(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        model="gpt-4o",
        hooks=hooks,
    )
    assert result.error is None
    assert result.tokens_saved == 0
    assert len(hooks.events) == 1
    assert hooks.events[0].tokens_saved == 0


def test_post_compress_fires_on_inflation_revert(monkeypatch) -> None:
    """The inflation-guard revert is a success-path outcome hooks must see."""
    import importlib

    # The package binds the FUNCTION at ``furl_ctx.compress`` (deliberate
    # shadowing); resolve the module object explicitly.
    compress_mod = importlib.import_module("furl_ctx.compress")
    from furl_ctx.config import TransformResult

    class _InflatingPipeline:
        def apply(self, messages, model, **kwargs):
            return TransformResult(
                messages=messages,
                tokens_before=10,
                tokens_after=20,
                transforms_applied=["stub:inflate"],
            )

    monkeypatch.setattr(compress_mod, "_pipeline", _InflatingPipeline())
    hooks = _RecordingHooks()
    result = compress_mod.compress(
        [{"role": "user", "content": "q"}, {"role": "tool", "content": "x" * 2000}],
        model="gpt-4o",
        hooks=hooks,
    )
    assert result.transforms_applied == ["inflation_guard:reverted"]
    assert len(hooks.events) == 1
    assert hooks.events[0].tokens_saved == 0
    assert hooks.events[0].tokens_before == 10


def test_post_compress_event_carries_surfaced_ccr_hashes() -> None:
    hooks = _RecordingHooks()
    result = compress(_droppable_tool_messages(), model="gpt-4o", hooks=hooks)
    assert result.error is None
    assert result.tokens_saved > 0
    assert len(hooks.events) == 1
    event = hooks.events[0]
    assert event.ccr_hashes, "lossy CCR compression must surface its recovery hashes"
    output_text = json.dumps(result.messages, ensure_ascii=False)
    for h in event.ccr_hashes:
        assert is_valid_ccr_hash(h)
        assert h in output_text, f"event hash {h} not present in the returned messages"


# ─── API-4 / API-16: pipeline kwargs broadcast ──────────────────────────────


def test_previous_prefix_hash_passes_the_pipeline_broadcast() -> None:
    from furl_ctx.transforms import TransformPipeline

    result = TransformPipeline().apply(
        [{"role": "user", "content": "hello there"}],
        model="gpt-4o",
        model_limit=200000,
        previous_prefix_hash="deadbeef",
    )
    assert result.messages  # no TypeError from the ContentRouter allowlist


@pytest.mark.parametrize("stale_kwarg", ["record_metrics", "output_buffer", "tool_profiles"])
def test_router_rejects_stale_or_unread_kwargs(stale_kwarg: str) -> None:
    """Dead allowlist entries are gone: passing them now fails loudly."""
    from furl_ctx.tokenizer import Tokenizer
    from furl_ctx.tokenizers import get_tokenizer
    from furl_ctx.transforms.content_router import ContentRouter

    router = ContentRouter()
    tokenizer = Tokenizer(get_tokenizer("gpt-4o"), "gpt-4o")
    with pytest.raises(TypeError, match=stale_kwarg):
        router.apply(
            [{"role": "user", "content": "hi"}],
            tokenizer,
            **{stale_kwarg: object()},
        )


def test_simulate_still_consumes_record_metrics() -> None:
    """apply(record_metrics=False) must keep popping the flag (the old
    simulate() path)."""
    from furl_ctx.transforms import TransformPipeline

    result = TransformPipeline().apply(
        [{"role": "user", "content": "hello there"}],
        model="gpt-4o",
        record_metrics=False,
        model_limit=200000,
    )
    assert result.messages


# ─── API-5: explicit CCR backend must not silently downgrade ────────────────


def test_unknown_explicit_backend_raises(monkeypatch) -> None:
    monkeypatch.setenv("FURL_CCR_BACKEND", "definitely-not-registered")
    with pytest.raises(ValueError, match="definitely-not-registered"):
        _create_default_ccr_backend()


def test_malformed_backend_opts_raise(monkeypatch) -> None:
    monkeypatch.setenv("FURL_CCR_BACKEND", "someplugin")
    monkeypatch.setenv("FURL_CCR_BACKEND_OPTS", "{not json")
    with pytest.raises(ValueError, match="FURL_CCR_BACKEND_OPTS"):
        _create_default_ccr_backend()


def test_backend_opts_must_be_a_json_object(monkeypatch) -> None:
    monkeypatch.setenv("FURL_CCR_BACKEND", "someplugin")
    monkeypatch.setenv("FURL_CCR_BACKEND_OPTS", '["not", "a", "dict"]')
    with pytest.raises(ValueError, match="JSON object"):
        _create_default_ccr_backend()


def test_failing_entry_point_factory_raises(monkeypatch) -> None:
    class _BoomEp:
        name = "boomplugin"

        def load(self):
            def factory(**kwargs):
                raise OSError("cannot reach store")

            return factory

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_BoomEp()] if group == "furl_ctx.ccr_backend" else [],
    )
    monkeypatch.setenv("FURL_CCR_BACKEND", "boomplugin")
    with pytest.raises(RuntimeError, match="boomplugin"):
        _create_default_ccr_backend()


def test_entry_point_factory_receives_opts_kwargs(monkeypatch) -> None:
    captured: dict = {}

    class _GoodEp:
        name = "goodplugin"

        def load(self):
            def factory(**kwargs):
                captured.update(kwargs)

                class _Backend:
                    pass

                return _Backend()

            return factory

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_GoodEp()] if group == "furl_ctx.ccr_backend" else [],
    )
    monkeypatch.setenv("FURL_CCR_BACKEND", "goodplugin")
    monkeypatch.setenv("FURL_CCR_BACKEND_OPTS", '{"url": "sqlite:///x", "tenant": "t1"}')
    backend = _create_default_ccr_backend()
    assert backend is not None
    assert captured == {"url": "sqlite:///x", "tenant": "t1"}


def test_memory_and_unset_still_mean_default(monkeypatch) -> None:
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    assert _create_default_ccr_backend() is None
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    assert _create_default_ccr_backend() is None


# ─── API-1: decorative exports pruned ───────────────────────────────────────


def test_never_raised_exceptions_are_not_exported() -> None:
    for name in (
        "ConfigurationError",
        "ProviderError",
        "StorageError",
        "CompressionError",
        "TokenizationError",
        "CacheError",
        "ValidationError",
        "TransformError",
        "CacheConfig",
        "CacheStrategy",
    ):
        assert name not in furl_ctx.__all__
        with pytest.raises(AttributeError):
            getattr(furl_ctx, name)


def test_furl_error_base_survives() -> None:
    assert "FurlError" in furl_ctx.__all__
    from furl_ctx import FurlError

    assert issubclass(FurlError, Exception)


# ─── API-12: CompressionStrategy binds to router_policy ────────────────────


def test_compression_strategy_lazy_bind_skips_content_router() -> None:
    """Touching the enum must not import the 1,586-line router + Rust chain."""
    code = (
        "import sys\n"
        "import furl_ctx.transforms as t\n"
        "t.CompressionStrategy\n"
        "assert 'furl_ctx.transforms.router_policy' in sys.modules\n"
        "assert 'furl_ctx.transforms.content_router' not in sys.modules\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_compression_strategy_is_the_single_canonical_enum() -> None:
    from furl_ctx.transforms import CompressionStrategy as exported
    from furl_ctx.transforms.content_router import CompressionStrategy as via_router
    from furl_ctx.transforms.router_policy import CompressionStrategy as canonical

    assert exported is canonical
    assert via_router is canonical


# ─── API-15: MCP parameter guards + isError signaling ───────────────────────


mcp = pytest.importorskip("mcp")


def _envelope(result) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


@pytest.fixture()
def _server(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    return FurlMCPServer()


async def test_compress_rejects_non_string_content(_server) -> None:
    env = _envelope(await _server._handle_compress({"content": 123}))
    assert "error" in env
    assert "string" in env["error"]
    assert "Internal error" not in env["error"]


async def test_retrieve_rejects_non_string_query(_server) -> None:
    env = _envelope(await _server._handle_retrieve({"hash": "a" * 24, "query": 123}))
    assert "error" in env
    assert "string" in env["error"]
    assert "Internal error" not in env["error"]


async def test_read_rejects_non_string_file_path(_server) -> None:
    env = _envelope(await _server._handle_read({"file_path": 123}))
    assert "error" in env
    assert "string" in env["error"]
    assert "Internal error" not in env["error"]


def _get_call_tool(server):
    """Recover the registered user ``call_tool`` closure from the SDK handler."""
    import mcp.types as mt

    sdk_handler = server.server.request_handlers[mt.CallToolRequest]
    for cell in sdk_handler.__closure__ or []:
        candidate = cell.cell_contents
        if callable(candidate) and getattr(candidate, "__name__", "") == "call_tool":
            return candidate
    raise AssertionError("user call_tool closure not found in SDK handler")


async def test_genuine_failure_reraises_sanitized(_server, monkeypatch) -> None:
    """Unexpected handler failures re-raise so the SDK signals isError=True.

    The re-raised message must be the generic envelope, never the internal
    detail (which is logged server-side only).
    """
    from furl_ctx.ccr.mcp_server import STATS_TOOL_NAME

    async def _boom():
        raise OSError("secret internal detail")

    monkeypatch.setattr(_server, "_handle_stats", _boom)
    call_tool = _get_call_tool(_server)
    with pytest.raises(RuntimeError) as excinfo:
        await call_tool(STATS_TOOL_NAME, {})
    assert "secret internal detail" not in str(excinfo.value)
    assert STATS_TOOL_NAME in str(excinfo.value)


async def test_unknown_tool_stays_a_model_visible_error(_server) -> None:
    """Unknown tool name is a model-addressable mistake, not a server failure."""
    call_tool = _get_call_tool(_server)
    env = _envelope(await call_tool("no_such_tool", {}))
    assert "error" in env
