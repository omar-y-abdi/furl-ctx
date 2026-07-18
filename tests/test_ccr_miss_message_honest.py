"""F-beta3 pin: the never-stored miss message states possibilities, it does not
assert a specific cause that did not occur (audit R1#9).

The old ``missing``-status text led with "it was evicted under capacity pressure"
as an asserted fact, so an agent reading a miss for a hash that was simply never
stored concluded an eviction had happened. The reworded message frames every
cause as a possibility. It still NAMES eviction, capacity, and the configured size
that ``test_ccr_eviction_loud_miss`` already pins, but no longer states eviction
as fact, and it carries no em/en dash and no round-bracket aside.
"""

from __future__ import annotations

from furl_ctx.cache.compression_store import (
    DEFAULT_CCR_TTL_SECONDS,
    format_retrieval_miss_detail,
)


def _missing_msg(max_entries: int | None = 1000) -> str:
    return format_retrieval_miss_detail(
        {
            "hash": "deadbeefdeadbeefdeadbeef",
            "status": "missing",
            "default_ttl_seconds": DEFAULT_CCR_TTL_SECONDS,
            "max_entries": max_entries,
        }
    )


def test_missing_miss_does_not_assert_eviction_as_fact() -> None:
    msg = _missing_msg()
    # The old fact-assertion phrasing must be gone.
    assert "it was evicted under capacity pressure" not in msg, (
        f"miss still asserts eviction as fact: {msg!r}"
    )
    # Causes are framed as possibilities, not one definite cause.
    assert "may" in msg.lower(), f"miss must frame causes as possibilities: {msg!r}"
    # The honest set is present: never stored, purged, evicted, expired.
    lo = msg.lower()
    for cause in ("never", "stored", "purge", "evict", "expired"):
        assert cause in lo, f"miss must name the {cause!r} possibility: {msg!r}"


def test_missing_miss_is_ai_tell_free() -> None:
    msg = _missing_msg()
    assert "—" not in msg and "–" not in msg, f"miss must carry no em/en dash: {msg!r}"
    assert "(" not in msg and ")" not in msg, f"miss must carry no round-bracket aside: {msg!r}"


def test_missing_miss_stays_cause_honest_for_capacity() -> None:
    # Consistency with the hardened cause-honesty pin: eviction, capacity, and the
    # configured size are all still named.
    msg = _missing_msg(1000)
    lo = msg.lower()
    assert "evict" in lo and "capacity" in lo and "1000" in msg


def test_expired_miss_unchanged() -> None:
    # The accurate 'expired' branch is untouched by the reword.
    msg = format_retrieval_miss_detail(
        {
            "hash": "cafe",
            "status": "expired",
            "ttl_seconds": 300,
            "default_ttl_seconds": 300,
            "age_seconds": 412.0,
        }
    )
    assert "expired" in msg.lower() and "300" in msg and "412" in msg
