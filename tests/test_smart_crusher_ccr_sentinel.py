"""Test-gap closure for #9: the public CCR-sentinel API was entirely untested.

``is_ccr_sentinel`` / ``strip_ccr_sentinels`` are the contract downstream
consumers rely on to skip the ``{"_ccr_dropped": "<<ccr:HASH N>>"}`` marker
SmartCrusher's lossy path appends to a kept-items array. With no tests, a
mutation of ``CCR_SENTINEL_KEY`` or the filter predicate survived silently —
which would make consumers treat the sentinel as a record (``e["level"]`` etc.)
or, worse, fail to strip it before recovery iteration.

No production change here — this only adds the missing coverage. Tests are
mutation-sensitive: they pin the exact sentinel KEY and the filter behavior so
changing either is caught.
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.smart_crusher import (
    CCR_SENTINEL_KEY,
    is_ccr_sentinel,
    strip_ccr_sentinels,
)

_SENTINEL = {CCR_SENTINEL_KEY: "<<ccr:abc123 7 rows_offloaded>>"}
_RECORD = {"level": "INFO", "msg": "ok"}


# ── is_ccr_sentinel ──────────────────────────────────────────────────────


def test_sentinel_object_is_detected() -> None:
    assert is_ccr_sentinel(_SENTINEL) is True


def test_literal_sentinel_key_is_load_bearing() -> None:
    # Pin the exact wire key so a mutation of CCR_SENTINEL_KEY is caught: the
    # detector must key off "_ccr_dropped" specifically.
    assert CCR_SENTINEL_KEY == "_ccr_dropped"
    assert is_ccr_sentinel({"_ccr_dropped": "x"}) is True


def test_plain_record_is_not_a_sentinel() -> None:
    assert is_ccr_sentinel(_RECORD) is False


@pytest.mark.parametrize("value", [None, "string", 42, ["_ccr_dropped"], {"other": 1}])
def test_non_sentinel_values_are_rejected(value) -> None:
    # Only a dict CONTAINING the sentinel key qualifies; a list whose element is
    # the key string, or a dict with a different key, must not match.
    assert is_ccr_sentinel(value) is False


def test_sentinel_detected_even_with_extra_keys() -> None:
    # Presence of the key is sufficient (membership test, not exact-shape).
    assert is_ccr_sentinel({CCR_SENTINEL_KEY: "m", "stray": 1}) is True


# ── strip_ccr_sentinels ──────────────────────────────────────────────────


def test_strip_removes_only_sentinels() -> None:
    items = [_RECORD, _SENTINEL, {"level": "WARN"}]
    result = strip_ccr_sentinels(items)
    assert result == [_RECORD, {"level": "WARN"}]
    assert all(not is_ccr_sentinel(x) for x in result)


def test_strip_preserves_order_and_records() -> None:
    a, b, c = {"i": 1}, {"i": 2}, {"i": 3}
    assert strip_ccr_sentinels([a, _SENTINEL, b, c]) == [a, b, c]


def test_strip_returns_empty_when_only_sentinel() -> None:
    assert strip_ccr_sentinels([_SENTINEL]) == []


def test_strip_is_noop_without_sentinels() -> None:
    items = [_RECORD, {"level": "WARN"}]
    assert strip_ccr_sentinels(items) == items


@pytest.mark.parametrize("value", [None, "not a list", 42, {"_ccr_dropped": "x"}])
def test_strip_passes_through_non_list(value) -> None:
    # Documented contract: non-list inputs pass through unchanged so callers can
    # wrap whatever json.loads returned without first checking the shape.
    assert strip_ccr_sentinels(value) is value
