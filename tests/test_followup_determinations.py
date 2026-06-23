"""Determination locks for the two follow-ups that need NO code change:

#7  (compression_policy TLS leak): the gate at SmartCrusher._record_toin reads
    ``self._runtime_compression_policy``, set only from
    ``kwargs.get("compression_policy")``. After the proxy route was removed, no
    live caller passes that kwarg, so the policy is always ``None`` and the
    ``toin_read_only`` skip never fires — TOIN recording stays enabled on the
    direct path. MOOT post-proxy-removal. This test locks that a default crush
    still records to TOIN (policy None => write-enabled).

#22-env (env TTL ≤ 0): ``_get_env_default_ttl_seconds`` already rejects a
    non-positive / non-integer / empty env value and falls back to the default.
    ALREADY GUARDED. This test locks the guard so a regression that drops the
    ``ttl_seconds <= 0`` check is caught.

Both are determination locks — no production change in either commit.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import headroom.cache.compression_store as cs
from headroom.cache.compression_store import (
    DEFAULT_CCR_TTL_SECONDS,
    _get_env_default_ttl_seconds,
)
from headroom.telemetry.toin import TOINConfig, get_toin, reset_toin
from headroom.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig

_ENV = "HEADROOM_CCR_TTL_SECONDS"


# ── #7: compression_policy is moot (always None on the live path) ─────────


@pytest.fixture
def fresh_toin():
    reset_toin()
    with tempfile.TemporaryDirectory() as tmpdir:
        toin = get_toin(
            TOINConfig(storage_path=str(Path(tmpdir) / "toin.json"), auto_save_interval=0)
        )
        yield toin
        reset_toin()


def _bigger_array(n: int = 60) -> str:
    import json

    return json.dumps([{"status": "ok", "tag": "x", "n": i} for i in range(n)])


def test_default_crush_records_to_toin_policy_is_none(fresh_toin) -> None:
    # No live caller passes compression_policy, so the policy stays None and the
    # toin_read_only gate never fires — the direct crush path records normally.
    crusher = SmartCrusher(SmartCrusherConfig())
    assert crusher._runtime_compression_policy is None

    pre = sum(p.total_compressions for p in fresh_toin._patterns.values())
    result = crusher.crush(_bigger_array(60), query="", bias=1.0)
    if not result.was_modified:
        pytest.skip("payload did not trigger compression")
    post = sum(p.total_compressions for p in fresh_toin._patterns.values())
    assert post > pre, "policy None must leave TOIN recording enabled (#7 moot)"


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
