"""Contract tests for ``furl_ctx.transforms.base``: the ``Transform`` ABC.

The ``Transform`` ABC's own contract (not instantiable without ``apply``;
``should_apply`` defaults True) is pinned here.
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.base import Transform


def test_transform_abc_cannot_be_instantiated_without_apply() -> None:
    """``Transform`` is abstract: instantiating it directly is a TypeError."""
    with pytest.raises(TypeError):
        Transform()  # type: ignore[abstract]


def test_concrete_transform_instantiates_and_should_apply_defaults_true() -> None:
    """A subclass that provides ``apply`` instantiates; ``should_apply`` defaults True."""

    class _NoopTransform(Transform):
        name = "noop"

        def apply(self, messages, tokenizer, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("apply is not exercised by this contract test")

    transform = _NoopTransform()
    assert transform.name == "noop"
    assert transform.should_apply([{"role": "user", "content": "hi"}], tokenizer=None) is True
