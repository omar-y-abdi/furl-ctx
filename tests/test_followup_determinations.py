"""Determination lock for the env-TTL follow-up that needs NO code change:

#22-env (env TTL ≤ 0): ``_get_env_default_ttl_seconds`` already rejects a
    non-positive / non-integer / empty env value and falls back to the default.
    ALREADY GUARDED. This test locks the guard so a regression that drops the
    ``ttl_seconds <= 0`` check is caught.

A determination lock — no production change in the commit.
"""

from __future__ import annotations

import furl_ctx.cache.compression_store as cs
from furl_ctx.cache.compression_store import (
    DEFAULT_CCR_TTL_SECONDS,
    _get_env_default_ttl_seconds,
)

_ENV = "FURL_CCR_TTL_SECONDS"


# ── #22-env: non-positive / invalid env TTL is already guarded ────────────


def test_env_ttl_zero_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "0")
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS


def test_env_ttl_negative_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "-5")
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS


def test_env_ttl_non_integer_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "not-a-number")
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS


def test_env_ttl_empty_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "   ")
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS


def test_env_ttl_valid_positive_is_honored(monkeypatch) -> None:
    # The guard must NOT clobber a legitimate positive override.
    monkeypatch.setenv(_ENV, "900")
    assert _get_env_default_ttl_seconds() == 900


def test_env_ttl_unset_uses_default(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert _get_env_default_ttl_seconds() == DEFAULT_CCR_TTL_SECONDS
    # Sanity: the module constant is the documented 300s default.
    assert cs.DEFAULT_CCR_TTL_SECONDS == 300
