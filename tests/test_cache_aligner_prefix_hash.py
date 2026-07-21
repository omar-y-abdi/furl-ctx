"""Regression tests for #5 (stable_prefix_hash collision) and #6 (warnings
surfacing) in the CacheAligner.

#5: the prefix hash was computed over ``"\\n---\\n".join(system_contents)``.
That delimiter join is not injective — a single system message that itself
contains the delimiter collides with two separate messages, so a structural
change to the system-prompt set produced the SAME hash and ``prefix_changed``
stayed False. The hash is meant to uniquely identify the ordered prompt set.

Fix: length-prefix-frame each message's bytes before hashing so the
serialization is injective. Test the PROPERTY (collision pair now differs,
determinism, prefix_changed flips), not a brittle hash literal.

#6: detection warnings are NOT discarded — they are surfaced via
``logger.warning`` and via the exported ``CacheAligner.apply().warnings``.
This locks that contract.

Compression-neutral: CacheAligner never rewrites messages (transforms_applied
is always empty).
"""

from __future__ import annotations

import logging

from furl_ctx.config import CacheAlignerConfig
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.cache_aligner import CacheAligner


def _aligner(enabled: bool = True) -> CacheAligner:
    return CacheAligner(CacheAlignerConfig(enabled=enabled))


def _tok() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())  # type: ignore[arg-type]


def _sys(content: str) -> dict:
    return {"role": "system", "content": content}


def _hash(messages: list[dict]) -> str:
    return _aligner().apply(messages, _tok()).cache_metrics.stable_prefix_hash


# ── #5: injective prefix hash ────────────────────────────────────────────


def test_delimiter_collision_now_yields_distinct_hashes() -> None:
    # One message containing the delimiter vs two separate messages: these are
    # structurally different prompt sets and MUST hash differently. Under the
    # bare-join bug both produced the same hash.
    one_msg = _hash([_sys("a\n---\nb")])
    two_msgs = _hash([_sys("a"), _sys("b")])
    assert one_msg != two_msgs, "non-injective prefix hash collided (#5)"


def test_prefix_changed_flips_on_structural_change() -> None:
    # Threading the prior turn's hash back in, the colliding pair must report
    # prefix_changed=True on the second call (the set really did change).
    aligner = _aligner()
    tok = _tok()
    first = aligner.apply([_sys("a\n---\nb")], tok)
    second = aligner.apply(
        [_sys("a"), _sys("b")],
        tok,
        previous_prefix_hash=first.cache_metrics.stable_prefix_hash,
    )
    assert second.cache_metrics.prefix_changed is True


def test_prefix_hash_is_deterministic() -> None:
    msgs = [_sys("alpha"), _sys("beta")]
    assert _hash(msgs) == _hash(msgs)


def test_identical_set_reports_no_change() -> None:
    aligner = _aligner()
    tok = _tok()
    first = aligner.apply([_sys("stable system prompt")], tok)
    second = aligner.apply(
        [_sys("stable system prompt")],
        tok,
        previous_prefix_hash=first.cache_metrics.stable_prefix_hash,
    )
    assert second.cache_metrics.prefix_changed is False


def test_no_cross_request_leak_on_shared_instance() -> None:
    # The aligner is a process-wide singleton in the pipeline. Two DIFFERENT
    # prompts through the SAME instance with no threaded hash must NOT latch
    # one request's prefix into the next: both report prefix_changed=False /
    # previous_hash=None. (Pre-fix the instance latched _previous_prefix_hash,
    # so the 2nd call falsely reported prefix_changed=True and leaked the 1st
    # request's hash — a cross-request observability corruption + data race.)
    aligner = _aligner()
    tok = _tok()
    a = aligner.apply([_sys("prompt A")], tok)
    b = aligner.apply([_sys("totally different prompt B")], tok)
    assert a.cache_metrics.prefix_changed is False
    assert a.cache_metrics.previous_hash is None
    assert b.cache_metrics.prefix_changed is False
    assert b.cache_metrics.previous_hash is None


# ── #6: warnings are surfaced, not swallowed ─────────────────────────────

_VOLATILE = "Current time: 2026-06-23T18:00:00Z session=550e8400-e29b-41d4-a716-446655440000"


def test_warnings_surface_via_exported_class_api() -> None:
    # The exported CacheAligner.apply() exposes detection warnings on the
    # result, so callers using the public class see them.
    result = _aligner().apply([_sys(_VOLATILE)], _tok())
    assert result.warnings, "volatile content produced no surfaced warning (#6)"
    assert any("volatile" in w.lower() for w in result.warnings)


def test_warnings_are_logged(caplog) -> None:
    # And they reach the log channel (logger.warning), the second surfacing path.
    with caplog.at_level(logging.WARNING, logger="furl_ctx.transforms.cache_aligner"):
        _aligner().apply([_sys(_VOLATILE)], _tok())
    assert any("volatile" in rec.getMessage().lower() for rec in caplog.records)
