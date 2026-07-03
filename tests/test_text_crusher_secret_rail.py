"""Secret-mask keep rail: Python surface of the TextCrusher rail.

The rail itself (detection + mandatory-keep promotion) is implemented and
unit-tested in Rust (``crates/furl-core/src/transforms/text_crusher.rs``).
These tests pin the FFI plumb: the ``secret_keep_rail`` config flag rides
the dataclass → PyO3 kwargs → Rust config path, the
``secret_keep_segments`` stat surfaces in ``TextCrushResult.stats``, and
the drop-protection contract holds through the production wrapper.

All secret fixtures are obviously fake and assembled at runtime from
parts (scanner hygiene — no contiguous token-shaped literal in source).
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.text_crusher import TextCrusher, TextCrusherConfig

# ─── Fixtures (mirror the Rust test corpus) ──────────────────────────────────

_SUBJECTS = [
    "The scheduler",
    "Our ingestion service",
    "The billing worker",
    "A background daemon",
    "The metrics exporter",
    "The auth gateway",
    "This migration script",
    "The cache layer",
]
_VERBS = [
    "processed",
    "rejected",
    "queued",
    "archived",
    "replicated",
    "validated",
    "throttled",
    "reindexed",
]
_OBJECTS = [
    "customer records",
    "audit events",
    "payment batches",
    "session tokens",
    "search documents",
    "webhook deliveries",
    "schema versions",
    "trace spans",
]
_TAILS = [
    "before the morning deadline without operator intervention",
    "while the standby region absorbed the overflow traffic",
    "although the retry queue kept growing steadily",
    "and the on-call engineer confirmed the dashboards stayed green",
    "despite intermittent packet loss on the private link",
]


def _varied_filler(i: int) -> str:
    return (
        f"{_SUBJECTS[i % 8]} {_VERBS[(i * 3 + 1) % 8]} "
        f"{_OBJECTS[(i * 5 + 2) % 8]} {_TAILS[i % 5]} in batch {i}."
    )


def _fake_secrets() -> list[str]:
    gh_token = "ghp_" + "abcdEFGH1234" * 3
    aws_key = "AKIA" + "IOSFODNN7EXAMPLE"  # the AWS docs example key (public)
    sk_key = "sk-" + "abcdefghijklmnopqrstuvwx"
    jwt = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjMifQ", "fAkEsIgNaTuRe123456"])
    hex_run = "d4f2a9c1b8e35f7a" + "6d0c4b2e9f1a8c3d"
    return [
        f"The old deploy key {sk_key} was rotated by the operator.",
        f"An AWS credential {aws_key} leaked into the staging environment.",
        f"A contractor pasted {gh_token} into the shared channel.",
        f"The session cookie carried {jwt} as a bearer token.",
        f"The webhook signing secret {hex_run} sat in plain text.",
    ]


def _prose_with_secrets() -> tuple[str, list[str]]:
    sentences = [_varied_filler(i) for i in range(60)]
    secrets = _fake_secrets()
    for k, secret in enumerate(secrets):
        sentences.insert(20 + 3 * k, secret)
    return " ".join(sentences), secrets


def _crusher(**overrides) -> TextCrusher:
    crusher = TextCrusher(TextCrusherConfig(target_ratio=0.10, **overrides))
    return crusher


@pytest.fixture
def _no_store_io(monkeypatch):
    # Keep the test hermetic: skip the production CompressionStore write
    # (the veto seam is pinned by test_ccr_persist_failure_vetoes.py).
    monkeypatch.setattr(TextCrusher, "_persist_to_python_ccr", lambda self, o, c, k: True)


# ─── Config surface ──────────────────────────────────────────────────────────


def test_config_default_is_on() -> None:
    assert TextCrusherConfig().secret_keep_rail is True


# ─── Drop protection through the FFI ─────────────────────────────────────────


def test_secret_segments_survive_aggressive_crush(_no_store_io) -> None:
    content, secrets = _prose_with_secrets()
    result = _crusher().compress(content)
    assert result.cache_key is not None, "fixture must actually crush"
    assert result.stats["secret_keep_segments"] == len(secrets)
    for secret in secrets:
        assert secret in result.compressed, f"secret dropped: {secret}"


def test_rail_off_restores_old_behavior(_no_store_io) -> None:
    content, secrets = _prose_with_secrets()
    result = _crusher(secret_keep_rail=False).compress(content)
    assert result.cache_key is not None, "fixture must actually crush"
    assert result.stats["secret_keep_segments"] == 0
    assert any(secret not in result.compressed for secret in secrets), (
        "rail-off must reproduce the pre-rail drops"
    )


def test_byte_identity_when_no_secrets(_no_store_io) -> None:
    content = " ".join(_varied_filler(i) for i in range(60))
    on = _crusher().compress(content)
    off = _crusher(secret_keep_rail=False).compress(content)
    assert on.stats["secret_keep_segments"] == 0
    assert on.compressed == off.compressed
    assert on.cache_key == off.cache_key
