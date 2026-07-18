"""F-beta1 pins: the CCR marker scan is DoS-bounded (audit R1#13).

``GENERIC_BRACKET_PATTERN`` carries two lazy ``.*?`` wildcards and used to run via
a plain backtracking ``re`` finditer over agent/tool text up to the 10 MiB read
cap. That scan is quadratic-or-worse on adversarial input and un-interruptible on
the MCP worker thread. It now routes through ``finditer_within_budget`` and
``sub_within_budget``, which use RE2's linear-time automaton. These pins lock:

1. an adversarial bracket-marker input near the cap completes well under a
   generous wall-clock budget with zero hang, surfacing no bogus hash;
2. a legitimate marker embedded in adversarial noise is still found;
3. ``resolve_markers`` (the substitution path) is likewise bounded and expands;
4. the bounded scan is byte-for-byte identical to the raw ``re`` union it
   replaces over the marker characterization corpus, so no producer-parity drift.

The wall-clock pins use a SIGALRM budget on the main thread. CPython checks
signals during a match, so a pre-fix backtracking scan is interrupted cleanly and
these FAIL fast instead of hanging. They run only when the ``re2`` extra is
installed; a base install falls back to the residual ``re`` engine, which is
unbounded by design and would overrun, so they skip there and say so.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import Any

import pytest

from furl_ctx import resolve_markers
from furl_ctx.cache.compression_store import CompressionStore
from furl_ctx.ccr import marker_grammar as mg
from furl_ctx.ccr.marker_grammar import (
    GENERIC_BRACKET_PATTERN,
    finditer_within_budget,
    hashes_in_text,
    sub_within_budget,
)

_H24 = "abcdef0123456789abcdef01"
_HAS_SIGALRM = hasattr(signal, "SIGALRM")


def _adversarial(target_bytes: int) -> str:
    """Many ``[`` starts, each followed by ``compressed`` but never completing a
    valid ``hash=<24hex>]``. This is the input that makes the two lazy wildcards
    backtrack quadratically under the plain ``re`` engine.
    """
    unit = "[x compressed y "
    return unit * (target_bytes // len(unit))


class _ScanTimeout(Exception):
    pass


def _run_within_walltime(fn: Callable[[], Any], seconds: float) -> tuple[bool, Any]:
    """Run ``fn`` on the main thread under a SIGALRM wall-clock budget.

    Returns ``(completed, value)``. A backtracking ``re`` scan that overruns is
    interrupted cleanly by the alarm, so a pre-fix regression fails this fast
    instead of hanging the suite. The previous SIGALRM handler and interval timer
    are restored unconditionally.
    """
    if not _HAS_SIGALRM or threading.current_thread() is not threading.main_thread():
        return True, fn()

    def _raise(signum: int, frame: Any) -> None:
        raise _ScanTimeout()

    prev_handler = signal.signal(signal.SIGALRM, _raise)
    prev_remaining, prev_interval = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return True, fn()
    except _ScanTimeout:
        return False, None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev_handler)
        if prev_remaining:
            signal.setitimer(signal.ITIMER_REAL, prev_remaining, prev_interval)


def test_adversarial_bracket_scan_completes_within_budget() -> None:
    if mg._RE2 is None:
        pytest.skip(
            "marker-scan DoS bound relies on the re2 extra (mcp); residual re path is unbounded"
        )
    text = _adversarial(10 * 1024 * 1024)  # ~10 MiB, the MCP read cap
    completed, value = _run_within_walltime(lambda: hashes_in_text(text), seconds=5.0)
    assert completed, "adversarial marker scan did not complete within 5s; the DoS is not bounded"
    assert value == [], f"adversarial input carries no valid hash; got {value!r}"


def test_legitimate_marker_found_amid_adversarial_noise() -> None:
    if mg._RE2 is None:
        pytest.skip("bound relies on the re2 extra (mcp)")
    text = _adversarial(4 * 1024 * 1024) + f"[1 x compressed y hash={_H24}]"
    completed, value = _run_within_walltime(lambda: hashes_in_text(text), seconds=5.0)
    assert completed, "scan did not complete within budget"
    assert _H24 in value, "a legitimate marker after adversarial noise must still be surfaced"


def test_resolve_markers_is_bounded_and_still_expands() -> None:
    if mg._RE2 is None:
        pytest.skip("bound relies on the re2 extra (mcp)")
    store = CompressionStore(max_entries=100)
    original = "the recovered original payload"
    hash_key = store.store(original=original, compressed="<compressed>")
    # An adversarial bracket blob plus a genuine bracket marker whose hash the
    # store resolves. The scan and substitution must complete AND expand it.
    marker = f"[1 items compressed to 0. Retrieve more: hash={hash_key}]"
    content = _adversarial(2 * 1024 * 1024) + " " + marker
    messages = [{"role": "tool", "content": content}]
    completed, value = _run_within_walltime(
        lambda: resolve_markers(messages, store=store), seconds=5.0
    )
    assert completed, (
        "resolve_markers did not complete within budget; the substitution path is unbounded"
    )
    assert value is not None
    assert original in value[0]["content"], "the genuine marker was not expanded"


def test_bounded_scan_is_identical_to_raw_re_union() -> None:
    """The bounded scan must surface exactly what the raw ``re`` union would, so
    the DoS fix introduces no producer-parity drift. Uses the same corpus the
    marker characterization test pins.
    """
    corpus = [
        f"[120 lines compressed to 12. Retrieve full diff: hash={_H24}]",
        f"[5 items compressed. hash={_H24}]",
        f"[7 Lines COMPRESSED to 1. retrieve MORE: hash={_H24}]",
        f"\n[400 items compressed to 40. Retrieve more: hash={_H24}]",
        "<<ccr:9f3a2b1c4d5e>>",
        f"<<ccr:{_H24}>>",
        "<<ccr:9f3a2b1c4d5e 3_rows_offloaded>>",
        "<<ccr:9f3a2b1c4d5e#rows 7_chunks>>",
        "<<ccr:9f3a2b1c4d5e,base64,1.1kB>>",
        f"<<ccr:{_H24} 5_bytes_duplicate>>",
        "<<ccr:ABCDEF012345 9_rows_offloaded>>",
        "<<ccr:9f3a2b1c4d5e~junk>>",
        "first <<ccr:9f3a2b1c4d5e 3_rows_offloaded>> then <<ccr:9f3a2b1c4d5e,base64,1.1kB>> end",
        f"mixed [a compressed b hash={_H24}] and <<ccr:9f3a2b1c4d5e>> together",
    ]

    def raw_union(text: str) -> list[str]:
        seen: dict[str, None] = {}
        for pattern in mg.marker_patterns():
            for match in pattern.finditer(text):
                seen.setdefault(mg.hash_of_match(match), None)
        return list(seen)

    for text in corpus:
        assert hashes_in_text(text) == raw_union(text), f"bounded scan diverged on {text!r}"


def test_finditer_within_budget_matches_re_finditer_for_generic() -> None:
    """The generic-pattern helper yields the same spans and hashes as
    ``re.finditer`` (RE2 twin parity), which is what keeps ``hashes_in_text``
    output stable across the engine swap.
    """
    for text in (
        f"[a compressed b hash={_H24}]",
        f"x [1 compressed to 2. Retrieve more: hash={_H24}] y",
        "no markers here",
        f"[a compressed b hash={_H24}] [c compressed d hash={'f' * 24}]",
    ):
        got = [m.group(m.lastindex) for m in finditer_within_budget(GENERIC_BRACKET_PATTERN, text)]
        exp = [m.group(m.lastindex) for m in GENERIC_BRACKET_PATTERN.finditer(text)]
        assert got == exp, f"RE2 twin diverged from re on {text!r}: {got} != {exp}"

    # Character-offset invariant that sub_within_budget depends on: it splices the
    # str by match.start and match.end, correct only if the RE2 twin returns
    # CHARACTER offsets like google-re2, not byte offsets. With non-ASCII before
    # the marker a byte-offset twin would slice mid-character and silently corrupt
    # resolve_markers while the group-parity checks above stayed green.
    multibyte = f"café 🚀 [x compressed y hash={_H24}] 日本"
    twin_spans = [
        (m.start(), m.end()) for m in finditer_within_budget(GENERIC_BRACKET_PATTERN, multibyte)
    ]
    re_spans = [(m.start(), m.end()) for m in GENERIC_BRACKET_PATTERN.finditer(multibyte)]
    assert twin_spans == re_spans, (
        f"twin span diverged from re on multibyte: {twin_spans} != {re_spans}"
    )
    got_sub = sub_within_budget(GENERIC_BRACKET_PATTERN, lambda m: "<X>", multibyte)
    exp_sub = GENERIC_BRACKET_PATTERN.sub(lambda m: "<X>", multibyte)
    assert got_sub == exp_sub, "sub_within_budget diverged from re.sub on multibyte input"


def test_scan_is_total_on_lone_surrogate() -> None:
    """RE2 encodes to UTF-8 and refuses a lone surrogate, which has no UTF-8
    encoding. The scan must fall back to the ``re`` engine rather than raise, so
    totality and the compressor's fail-open contract hold. Regression: the RE2
    twin used to propagate ``UnicodeEncodeError`` for such input.
    """
    text = "abc\ud800 [x compressed y hash=" + "a" * 24 + "]"
    # No raise, and the genuine marker is still surfaced via the re fallback.
    assert hashes_in_text(text) == ["a" * 24]
    surfaced = [m.group(m.lastindex) for m in finditer_within_budget(GENERIC_BRACKET_PATTERN, text)]
    assert surfaced == ["a" * 24]
