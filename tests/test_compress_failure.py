from __future__ import annotations

import importlib

from furl_ctx.compress import compress


class _FailingPipeline:
    def apply(self, **kwargs):  # noqa: ANN003, ANN201
        raise RuntimeError("boom")


def test_compress_returns_original_messages_when_pipeline_fails(monkeypatch) -> None:
    compress_module = importlib.import_module("furl_ctx.compress")
    monkeypatch.setattr(compress_module, "_get_pipeline", lambda: _FailingPipeline())

    messages = [{"role": "user", "content": "hello world " * 100}]
    result = compress(messages, model="gpt-4o")

    # On pipeline failure the original messages pass through untouched
    # (fail-open: a compression bug must never break the host's request).
    assert result.messages == messages
    # The failure is LOUD and HONEST, not silently masked as a no-op:
    # tokens_before reflects the REAL input so a caller cannot mistake a
    # swallowed failure for "nothing to compress", and `error` carries the
    # underlying exception text.
    from furl_ctx.tokenizers import get_tokenizer

    expected_tokens_before = get_tokenizer("gpt-4o").count_messages(messages)
    assert expected_tokens_before > 0
    assert result.tokens_before == expected_tokens_before
    assert result.tokens_after == 0
    assert result.tokens_saved == 0
    assert result.compression_ratio == 0.0
    assert result.error == "boom"
