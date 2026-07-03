"""Characterization lock for ContentRouter.apply()'s min-tokens floor split.

Two PUBLIC entry shapes deliberately resolve a DIFFERENT effective min-token
floor (see ``content_router.py``: the ``compress_request`` branch vs the
raw-kwargs branch):

* a :class:`CompressRequest` (built at the ``TransformPipeline`` boundary, e.g.
  by ``compress()``) carries the unified default floor of 250;
* a raw ``ContentRouter.apply(**kwargs)`` with neither ``compress_request`` nor
  an explicit ``min_tokens_to_compress`` preserves the HISTORICAL direct-caller
  floor of 50.

This split is **bench-invisible** — the benchmark always passes ``min_tokens``
explicitly, so it never exercises either default. It is pinned here instead. A
154-token compressible array is the discriminator: ABOVE 50 but BELOW 250, so it
compresses on the raw path and is left untouched on the request path. If a
refactor unifies the floors (raw 50 → 250), ``test_raw_apply_uses_floor_50``
goes red — the landmine becomes visible rather than silently shipping.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.config import CompressRequest
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


def _array(nrows: int) -> str:
    return json.dumps([{"id": i, "status": "ok", "msg": "request handled"} for i in range(nrows)])


@pytest.fixture
def tok():
    return get_tokenizer("gpt-4o")


def _router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig())


def test_raw_apply_uses_floor_50_not_250(tok) -> None:
    """Direct ``apply()`` (no ``compress_request``, no ``min_tokens``) compresses
    a 154-token array — proving the raw-caller floor is 50, not the boundary's
    250. THIS is the bench-blind landmine: a 50→250 unification fails here."""
    payload = _array(8)
    assert 50 < tok.count_text(payload) < 250, "fixture must sit in the 50–250 band"
    out = _router().apply([{"role": "tool", "content": payload}], tok)
    assert out.messages[0]["content"] != payload, (
        "raw ContentRouter.apply() must compress a 154-token array — its floor is "
        "50; if this fails the direct-caller floor silently shifted toward 250"
    )


def test_request_path_uses_floor_250(tok) -> None:
    """A :class:`CompressRequest` (default floor 250) leaves the same 154-token
    array untouched — pinning the boundary-path floor and the deliberate split."""
    payload = _array(8)
    out = _router().apply(
        [{"role": "tool", "content": payload}], tok, compress_request=CompressRequest()
    )
    assert out.messages[0]["content"] == payload, (
        "a CompressRequest with the default 250 floor must leave a 154-token array "
        "unchanged (154 < 250)"
    )


def test_control_both_paths_compress_above_250(tok) -> None:
    """Control: a 306-token array compresses on BOTH paths — proves the raw-path
    result above is a real floor effect, not 'compression never runs here'."""
    payload = _array(16)
    assert tok.count_text(payload) > 250
    raw = _router().apply([{"role": "tool", "content": payload}], tok)
    req = _router().apply(
        [{"role": "tool", "content": payload}], tok, compress_request=CompressRequest()
    )
    assert raw.messages[0]["content"] != payload
    assert req.messages[0]["content"] != payload


def test_raw_apply_sub_50_stays_untouched(tok) -> None:
    """TEST-12: the raw-path floor's ACTIVATE side was pinned (154 > 50
    compresses) but the deactivate side never was — a floor that silently
    dropped to 0 (compress everything, however tiny) stayed green. A
    sub-50-token array through raw ``apply()`` must pass through verbatim."""
    payload = _array(2)
    tokens = tok.count_text(payload)
    assert tokens < 50, f"fixture must sit under the raw floor, got {tokens}"

    out = _router().apply([{"role": "tool", "content": payload}], tok)

    assert out.messages[0]["content"] == payload, (
        f"a {tokens}-token array is below the raw-caller floor of 50 and must "
        "not be compressed"
    )
