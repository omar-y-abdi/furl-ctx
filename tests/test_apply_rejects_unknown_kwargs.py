"""ContentRouter.apply() must REJECT unknown kwargs instead of dropping them.

Before this guard, ``apply()`` read its options with ``kwargs.get(...)`` and
silently ignored anything it did not recognise. A typo — e.g. ``protect_recents``
for ``protect_recent`` — was therefore a no-op: the caller believed recent
messages were protected, but the misspelled key fell on the floor and the
default applied. This file pins the strict-rejection contract.

The allow-list (``_APPLY_ALLOWED_KWARGS``) is the union of:
  1. keys ``apply()`` READS (directly + via ``RouterRuntime.from_kwargs``), and
  2. keys a real caller PASSES through the pipeline broadcast but ``apply()``
     never reads (pipeline public surface + sibling transforms + positionals).

A false rejection that breaks a real caller is a regression, so the
``test_real_call_site_key_is_accepted`` parametrization pins every key any real
caller passes, and the end-to-end ``compress()`` smoke test exercises the live
pipeline path.
"""
from __future__ import annotations

import pytest

from headroom import compress
from headroom.tokenizers import get_tokenizer
from headroom.transforms.content_router import (
    _APPLY_ALLOWED_KWARGS,
    ContentRouter,
    ContentRouterConfig,
)


def _router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig())


def _messages() -> list[dict]:
    return [{"role": "tool", "content": "alpha " + "data point one " * 40}]


# The exact kwarg set ``ContentRouter.apply()`` receives on the real
# production path: ``compress()`` forwards its options to
# ``TransformPipeline.apply``, which injects ``compress_request`` and then
# broadcasts ``**kwargs`` to every transform. ``model`` / ``messages`` /
# ``tokenizer`` bind to positional params and never land in this transform's
# ``**kwargs``, so they are intentionally absent from the post-pipeline bag.
_POST_PIPELINE_KWARGS: dict = {
    "model_limit": 200000,
    "context": "",
    "biases": None,
    "compress_user_messages": False,
    "compress_system_messages": True,
    "target_ratio": None,
    "protect_recent": 4,
    "protect_analysis_context": True,
    "min_tokens_to_compress": 250,
    "kompress_model": None,
    "frozen_message_count": 0,
    "compress_request": None,
}


def test_unknown_kwarg_raises_type_error() -> None:
    """A bogus kwarg — ``protect_recents``, a typo of ``protect_recent`` — must
    raise TypeError rather than being silently dropped."""
    tokenizer = get_tokenizer("gpt-4o")
    with pytest.raises(TypeError):
        _router().apply(_messages(), tokenizer, protect_recents=2)


def test_raise_message_names_the_offending_key() -> None:
    """The error must name the offending key so the caller can find the typo."""
    tokenizer = get_tokenizer("gpt-4o")
    with pytest.raises(TypeError, match="protect_recents"):
        _router().apply(_messages(), tokenizer, protect_recents=2)


def test_known_good_call_does_not_raise() -> None:
    """The exact kwarg set the production pipeline hands to apply() must pass."""
    tokenizer = get_tokenizer("gpt-4o")
    # Must not raise TypeError for any key in the post-pipeline bag.
    _router().apply(_messages(), tokenizer, **_POST_PIPELINE_KWARGS)


# Union of EVERY key a real caller passes to ContentRouter.apply(), gathered
# from grepping every call site in headroom/ AND tests/:
#   * the production path (``_POST_PIPELINE_KWARGS``, via compress()), plus
#   * the test-only direct ``router.apply(...)`` call sites, which pass
#     ``force_kompress`` (test_content_router_worker_options.py),
#   * plus the keys the pipeline's documented public surface broadcasts
#     (``request_id`` / ``output_buffer`` / ``tool_profiles`` / ``record_metrics``).
_REAL_CALLER_KEYS = set(_POST_PIPELINE_KWARGS) | {
    "force_kompress",
    "request_id",
    "output_buffer",
    "tool_profiles",
    "record_metrics",
}


@pytest.mark.parametrize("key", sorted(_REAL_CALLER_KEYS))
def test_real_call_site_key_is_accepted(key: str) -> None:
    """Every key a real caller passes (production path + test-side direct
    apply() call sites + documented pipeline public surface) is in the
    allow-list — pinning intent so a future edit cannot drop a live key and
    start rejecting real traffic."""
    assert key in _APPLY_ALLOWED_KWARGS


def test_compress_end_to_end_does_not_raise() -> None:
    """Real end-to-end path through compress() -> pipeline -> apply() must not
    raise from the new guard (the integration check for the broadcast set)."""
    result = compress(
        [{"role": "user", "content": "hi " * 200}],
        model="gpt-4o",
    )
    assert result is not None
    assert result.messages is not None
