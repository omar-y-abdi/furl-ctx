"""Determination lock for the env-TTL follow-up that needs NO code change:

#22-env (env TTL ≤ 0): ``_get_env_default_ttl_seconds`` already rejects a
    non-positive / non-integer / empty env value and falls back to the default.
    ALREADY GUARDED. This test locks the guard so a regression that drops the
    ``ttl_seconds <= 0`` check is caught.

A determination lock — no production change in the commit.
"""

from __future__ import annotations

import pytest

import furl_ctx.cache.compression_store as cs
from furl_ctx.cache.compression_store import (
    DEFAULT_CCR_TTL_SECONDS,
    _get_env_default_ttl_seconds,
)

_ENV = "FURL_CCR_TTL_SECONDS"


# ── #22-env: non-positive / invalid env TTL is already guarded ────────────


@pytest.mark.parametrize(
    "raw",
    ["0", "-5", "not-a-number", "   "],
    ids=["zero", "negative", "non-integer", "blank"],
)
def test_invalid_env_ttl_falls_back_to_default(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(_ENV, raw)
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS


def test_env_ttl_valid_positive_is_honored(monkeypatch) -> None:
    # The guard must NOT clobber a legitimate positive override.
    monkeypatch.setenv(_ENV, "900")
    assert _get_env_default_ttl_seconds() == 900


def test_env_ttl_unset_uses_default(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS
    # Sanity: the module constant is the documented session-scale 1800s default (Engine P0-3: raised from 300s because agentic sessions outlive 5 minutes
    # — an expired entry silently converts "lossless + retrieval" into lossy). Must agree with Rust `DEFAULT_TTL`.
    assert cs.DEFAULT_CCR_TTL_SECONDS == 1800
