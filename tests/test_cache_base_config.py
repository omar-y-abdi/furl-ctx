"""Tests for furl_ctx/cache/base.py — CacheConfig and CacheStrategy.

CacheConfig had 0% coverage (it's a dataclass — all fields default-initialized;
no existing test imported it). This minimal pass covers:
  CB-B1a: pin each default field value.
  CB-bnd: strategy enum members, field overrides affect behavior.

Mutation-sensitive: any default value change flips a pin test.
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.base import CacheConfig, CacheStrategy

# ---------------------------------------------------------------------------
# CB-B1a  Pin all default field values
#
# Defaults carry mutation value — a changed default flips exactly one pin here.
# Parametrized (one case per field) rather than unrolled into ~14 near-identical
# functions; reading CacheConfig() exercises every default + both default_factory
# lambdas, holding cache/base coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name,expected",
    [
        ("enabled", True),
        ("strategy", None),
        ("min_cacheable_tokens", 1024),
        ("max_breakpoints", 4),
        ("normalize_whitespace", True),
        ("collapse_blank_lines", True),
        ("dynamic_separator", "\n\n---\n\n"),
        ("dynamic_detection_tiers", ["regex"]),
        ("semantic_cache_enabled", False),
        ("semantic_similarity_threshold", 0.95),
        ("semantic_cache_ttl_seconds", 300),
        (
            "date_patterns",
            [
                r"Today is \w+ \d{1,2},? \d{4}\.?",
                r"Current date: \d{4}-\d{2}-\d{2}",
                r"The current time is .+\.",
            ],
        ),
    ],
)
def test_b1a_default_field_value(field_name: str, expected: object) -> None:
    """Pin each default — a changed production default flips exactly one case."""
    assert getattr(CacheConfig(), field_name) == expected


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
# CB-bnd  Mutable-default isolation (behavioral — not a constructor readback)
#
# The five constructor-readback overrides (CacheConfig(field=X); assert
# cfg.field == X) were removed: they survive every production mutation, so they
# have no mutation-detection value. The default pins above already exercise the
# fields. This behavioral test survives only if default_factory builds a fresh
# list per instance, which a real regression (e.g. a shared class-level list)
# would break.
# ---------------------------------------------------------------------------


def test_fresh_instances_are_independent() -> None:
    """Each CacheConfig() is a distinct instance — mutable list fields don't share."""
    c1 = CacheConfig()
    c2 = CacheConfig()
    c1.date_patterns.append("extra")
    assert len(c2.date_patterns) == 3, "default_factory must create new list per instance"
