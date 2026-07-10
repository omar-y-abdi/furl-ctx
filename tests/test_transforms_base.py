"""Contract tests for ``furl_ctx.transforms.base``: ``split_frozen`` + the ABC.

``split_frozen`` partitions messages into a frozen (cached-prefix) head and a
mutable tail; transforms use it to avoid invalidating the provider prefix cache.
These tests enumerate the count boundary on both guard edges (``<= 0`` and
``>= len``) and pin the partition + object-identity invariants. The ``Transform``
ABC's own contract (not instantiable without ``apply``; ``should_apply`` defaults
True) is pinned alongside.
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.base import Transform, split_frozen

_MESSAGES = [{"i": 0}, {"i": 1}, {"i": 2}, {"i": 3}]  # len == 4


@pytest.mark.parametrize(
    ("frozen_count", "expected_frozen_len"),
    [
        (-1, 0),  # below the lower guard (< 0)
        (0, 0),  # the <= 0 boundary (== 0)
        (1, 1),  # just inside: first message frozen
        (2, 2),
        (3, 3),  # len-1: still a real split
        (4, 0),  # the >= len boundary (== len) collapses back to all-mutable
        (5, 0),  # above the upper guard (> len)
    ],
)
def test_split_frozen_count_boundaries(frozen_count: int, expected_frozen_len: int) -> None:
    """The frozen head has the expected size across every count boundary.

    Out-of-range counts (``<= 0`` or ``>= len``) yield an empty frozen head and
    the whole list as mutable; in-range counts split at exactly ``frozen_count``.
    """
    frozen, mutable = split_frozen(_MESSAGES, frozen_count)
    assert len(frozen) == expected_frozen_len
    assert len(mutable) == len(_MESSAGES) - expected_frozen_len


@pytest.mark.parametrize("frozen_count", [-1, 0, 1, 2, 3, 4, 5])
def test_split_frozen_is_a_total_partition(frozen_count: int) -> None:
    """frozen ++ mutable always reconstructs the input, in order, losslessly."""
    frozen, mutable = split_frozen(_MESSAGES, frozen_count)
    assert frozen + mutable == _MESSAGES


def test_split_frozen_preserves_object_identity_and_does_not_mutate_input() -> None:
    """The split shares the original message objects and leaves the input list intact.

    Frozen messages "must not be modified", so the partition must not copy them
    (identity preserved) nor disturb the caller's list.
    """
    before = list(_MESSAGES)
    frozen, mutable = split_frozen(_MESSAGES, 1)
    assert frozen[0] is _MESSAGES[0]
    assert mutable[0] is _MESSAGES[1]
    assert _MESSAGES == before  # input list untouched


def test_split_frozen_on_empty_message_list_returns_two_empty_lists() -> None:
    """Edge: with no messages, any count yields ([], []) — nothing to freeze or mutate."""
    frozen, mutable = split_frozen([], 3)
    assert frozen == []
    assert mutable == []


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
