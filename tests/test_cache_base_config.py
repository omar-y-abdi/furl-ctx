"""Tests for headroom/cache/base.py — CacheConfig and CacheStrategy.

CacheConfig had 0% coverage (it's a dataclass — all fields default-initialized;
no existing test imported it). This minimal pass covers:
  CB-B1a: pin each default field value.
  CB-bnd: strategy enum members, field overrides affect behavior.

Mutation-sensitive: any default value change flips a pin test.
"""
from __future__ import annotations

import pytest

from headroom.cache.base import CacheConfig, CacheStrategy


# ---------------------------------------------------------------------------
# CB-B1a  Pin all default field values
# ---------------------------------------------------------------------------


def test_b1a_default_enabled() -> None:
    assert CacheConfig().enabled is True


def test_b1a_default_strategy_is_none() -> None:
    assert CacheConfig().strategy is None


def test_b1a_default_min_cacheable_tokens() -> None:
    assert CacheConfig().min_cacheable_tokens == 1024


def test_b1a_default_max_breakpoints() -> None:
    assert CacheConfig().max_breakpoints == 4


def test_b1a_default_normalize_whitespace() -> None:
    assert CacheConfig().normalize_whitespace is True


def test_b1a_default_collapse_blank_lines() -> None:
    assert CacheConfig().collapse_blank_lines is True


def test_b1a_default_dynamic_separator() -> None:
    assert CacheConfig().dynamic_separator == "\n\n---\n\n"


def test_b1a_default_dynamic_detection_tiers() -> None:
    assert CacheConfig().dynamic_detection_tiers == ["regex"]


def test_b1a_default_semantic_cache_enabled() -> None:
    assert CacheConfig().semantic_cache_enabled is False


def test_b1a_default_semantic_similarity_threshold() -> None:
    assert CacheConfig().semantic_similarity_threshold == 0.95


def test_b1a_default_semantic_cache_ttl_seconds() -> None:
    assert CacheConfig().semantic_cache_ttl_seconds == 300


def test_b1a_default_date_patterns_nonempty() -> None:
    # date_patterns is a non-empty list (default_factory); pin count and first entry.
    patterns = CacheConfig().date_patterns
    assert len(patterns) == 3
    assert patterns[0] == r"Today is \w+ \d{1,2},? \d{4}\.?"


# ---------------------------------------------------------------------------
# CacheStrategy enum — pin member values (strategy members used as keys)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member,value",
    [
        (CacheStrategy.PREFIX_STABILIZATION, "prefix_stabilization"),
        (CacheStrategy.EXPLICIT_BREAKPOINTS, "explicit_breakpoints"),
        (CacheStrategy.CACHED_CONTENT, "cached_content"),
        (CacheStrategy.NONE, "none"),
    ],
)
def test_cache_strategy_values(member: CacheStrategy, value: str) -> None:
    """Pin enum string values — mutations to any .value are caught."""
    assert member.value == value


def test_cache_strategy_has_four_members() -> None:
    assert len(CacheStrategy) == 4


# ---------------------------------------------------------------------------
# CB-bnd  Field override changes observable value
# ---------------------------------------------------------------------------


def test_field_override_enabled_false() -> None:
    cfg = CacheConfig(enabled=False)
    assert cfg.enabled is False


def test_field_override_min_cacheable_tokens() -> None:
    cfg = CacheConfig(min_cacheable_tokens=512)
    assert cfg.min_cacheable_tokens == 512
    # Different from default
    assert cfg.min_cacheable_tokens != CacheConfig().min_cacheable_tokens


def test_field_override_max_breakpoints() -> None:
    cfg = CacheConfig(max_breakpoints=2)
    assert cfg.max_breakpoints == 2


def test_field_override_strategy() -> None:
    cfg = CacheConfig(strategy=CacheStrategy.EXPLICIT_BREAKPOINTS)
    assert cfg.strategy is CacheStrategy.EXPLICIT_BREAKPOINTS


def test_field_override_detection_tiers() -> None:
    cfg = CacheConfig(dynamic_detection_tiers=["regex", "ner"])
    assert cfg.dynamic_detection_tiers == ["regex", "ner"]
    assert len(cfg.dynamic_detection_tiers) == 2


def test_fresh_instances_are_independent() -> None:
    """Each CacheConfig() is a distinct instance — mutable list fields don't share."""
    c1 = CacheConfig()
    c2 = CacheConfig()
    c1.date_patterns.append("extra")
    assert len(c2.date_patterns) == 3, "default_factory must create new list per instance"
