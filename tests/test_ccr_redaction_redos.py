"""Audit #1 — redaction ReDoS pin (major×high, the top ship-blocker).

``_SECRET_KEY_VALUE_RE`` is O(N^2) on a long unbroken base64url/hex/minified run
(a ``-`` is inside the ``[A-Z0-9_-]`` secret-key class yet is a word boundary, so
``\\b([A-Z0-9_-]*KEYWORD...)`` opens O(N) anchor positions each scanning O(N)).
Empirically: 8 KB dashed→1.2 s, 16 KB→5.6 s, 32 KB→19 s, 64 KB→82 s. It fired
UNCONDITIONALLY over the full original on every ``retrieve`` (full-body log
redact) and every furl_search/furl_list/cross-store preview, so a single
base64url blob hung a tool call for minutes.

Fix: SLICE-before-REDACT — every preview/log surface redacts only the bounded
window it can show, never the whole multi-MB original. This pins BOTH directions:
FAST on a 256 KB base64url body (pre-fix ≈ 20 min), AND still fail-closed — a
secret inside the kept window is still masked.
"""

from __future__ import annotations

import time

import pytest

from furl_ctx.cache.compression_store import (
    CompressionStore,
    _payload_for_retrieval_log,
    _redact_retrieval_log_payload,
)

# ``_cross_store_preview`` is a staticmethod on the store — call it directly.
_cross_store_preview_for_test = CompressionStore._cross_store_preview

# A 256 KB continuous base64url-style run: the exact shape that detonated the
# quadratic (``-`` word boundaries inside the secret-key class, no keyword).
_BASE64URL_256K = ("aB3-xY7z_Qw9-" * ((256 * 1024) // 13 + 1))[: 256 * 1024]
# Hook-safe secret literal (no verbatim token in source) — caught by the bare
# ``sk-`` rule wherever it sits, so it does not depend on a key name.
_SECRET = "sk-" + "abcdefghijklmnopqrstuvwx"

# Generous vs. the pre-fix quadratic (~20 min at 256 KB); the fixed window
# redaction is ~0.3 s, so 1.0 s cleanly separates linear-window from O(N^2).
_FAST = 1.0


def _elapsed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def test_payload_log_redact_is_fast_on_large_base64() -> None:
    dt = _elapsed(lambda: _payload_for_retrieval_log(_BASE64URL_256K))
    assert dt < _FAST, f"retrieval-log redaction still quadratic: {dt:.2f}s on 256KB"


def test_cross_store_preview_is_fast_on_large_base64() -> None:
    dt = _elapsed(lambda: _cross_store_preview_for_test(_BASE64URL_256K))
    assert dt < _FAST, f"cross-store preview still quadratic: {dt:.2f}s on 256KB"


def test_store_retrieve_large_body_is_fast_end_to_end() -> None:
    # retrieve() calls the full-body log-redact unconditionally on every hit.
    store = CompressionStore(max_entries=10)
    h = store.store(original=_BASE64URL_256K, compressed="<<ccr:x>>")
    dt = _elapsed(lambda: store.retrieve(h))
    assert dt < _FAST, f"retrieve() hung on a 256KB base64url body: {dt:.2f}s"
    # And retrieval is byte-exact — the PAYLOAD to the caller is untouched
    # (only the LOG preview is redacted/sliced).
    entry = store.retrieve(h)
    assert entry is not None
    assert entry.original_content == _BASE64URL_256K


def test_payload_log_still_redacts_secret_inside_kept_window() -> None:
    # Fail-closed: a secret INSIDE the kept window (here at the very head) must
    # still be masked even though the tail is a huge un-redacted base64url run.
    payload = f'{{"api_key": "{_SECRET}"}} ' + _BASE64URL_256K
    dt = _elapsed(lambda: _payload_for_retrieval_log(payload))
    assert dt < _FAST, f"redaction still quadratic with a head secret: {dt:.2f}s"
    preview = _payload_for_retrieval_log(payload)["payload_preview"]
    assert _SECRET not in preview, f"secret leaked into log preview: {preview[:80]!r}"
    assert "[REDACTED]" in preview


def test_cross_store_preview_still_redacts_secret_inside_window() -> None:
    # cross-store preview budget is 200 chars — put the secret at the head.
    content = f"{_SECRET} " + _BASE64URL_256K
    preview = _cross_store_preview_for_test(content)
    assert _SECRET not in preview
    assert "[REDACTED]" in preview


@pytest.mark.parametrize(
    "make",
    [
        # bare secret at the head (within every preview budget)
        lambda: f"{_SECRET} " + _BASE64URL_256K,
    ],
)
def test_mcp_previews_fast_and_fail_closed(make) -> None:
    from furl_ctx.ccr.mcp_server import _list_preview, _match_preview

    content = make()
    # A needle deep after the secret so the match window is far from the head —
    # proves _match_preview does not redact the whole original to locate it.
    needle = "xY7z".lower()
    assert _elapsed(lambda: _match_preview(content, needle)) < _FAST
    assert _elapsed(lambda: _list_preview(content)) < _FAST
    # _list_preview shows the head → the secret must be masked there.
    lp = _list_preview(content)
    assert _SECRET not in lp and "[REDACTED]" in lp


def test_redact_primitive_itself_unchanged_on_small_input() -> None:
    # The redaction PRIMITIVE is untouched (its direct tests pin exact outputs);
    # only the call sites slice. A small credential still redacts identically.
    out = _redact_retrieval_log_payload(f'{{"token": "{_SECRET}"}}')
    assert _SECRET not in out and "[REDACTED]" in out


# ── review F4: _match_preview window-edge straddle (partial-secret leak) ─────
#
# The first slice-before-redact cut of ``_match_preview`` redacted ONLY the
# bare radius window (no margin, unlike the list/cross/log surfaces). A
# prefix-anchored secret straddling a window edge then leaked a fragment:
#   * FAR edge — the window truncated an ``sk-`` key below the 12-char body
#     minimum, so the rule no-matched and the head (``sk-`` + entropy) leaked;
#   * NEAR edge — the ``sk-`` anchor sat before the window start, so the
#     un-anchored body tail inside the window leaked.
# The fix redacts a MARGIN-widened window (straddling secrets are seen whole)
# and re-finds the needle inside the REDACTED text before display-windowing.

_STRADDLE_BODY = "Zq7Rw2Xt9Kp4Mn6B"  # 16 entropy chars (>= the 12-char rule floor)
_STRADDLE_SECRET = "sk-" + _STRADDLE_BODY
_STRADDLE_NEEDLE = "needleword"

# Fragments that must NEVER appear in a preview: whole key, head, tail.
_STRADDLE_FRAGMENTS = (_STRADDLE_SECRET, _STRADDLE_BODY[:5], _STRADDLE_BODY[-6:])


def test_match_preview_masks_secret_straddling_far_edge() -> None:
    # Secret STARTS inside the display window; its tail crosses the far edge
    # (only 8 of its chars fit). Pre-fix, the redactor saw just "sk-Zq7Rw" —
    # below the rule's 12-char body floor → no match → the head LEAKED. The
    # secret is space-delimited (real content gives the ``\b`` the sk- rule
    # anchors on); the leak was purely the window truncation.
    from furl_ctx.ccr.mcp_server import _MATCH_PREVIEW_RADIUS, _match_preview

    prefix = "A" * 100
    raw_idx = len(prefix)
    display_end = raw_idx + len(_STRADDLE_NEEDLE) + _MATCH_PREVIEW_RADIUS
    secret_start = display_end - 8  # 8 chars of the key sit in-window
    pad = secret_start - (raw_idx + len(_STRADDLE_NEEDLE)) - 1
    original = prefix + _STRADDLE_NEEDLE + ("f" * pad) + " " + _STRADDLE_SECRET + " " + ("t" * 200)
    assert original[secret_start : secret_start + 3] == "sk-", "fixture geometry drifted"

    preview = _match_preview(original, _STRADDLE_NEEDLE)

    for fragment in _STRADDLE_FRAGMENTS:
        assert fragment not in preview, (
            f"far-edge straddling secret leaked fragment {fragment!r}: {preview!r}"
        )
    assert _STRADDLE_NEEDLE in preview, "the match window must still show the needle"


def test_match_preview_masks_secret_straddling_near_edge() -> None:
    # Secret's ``sk-`` anchor sits BEFORE the display-window start; its body
    # tail extends into the window. Pre-fix, the redactor saw only the
    # un-anchored 14-char tail → no rule matched → the tail LEAKED. Space-
    # delimited for the same ``\b`` reason as the far-edge twin.
    from furl_ctx.ccr.mcp_server import _MATCH_PREVIEW_RADIUS, _match_preview

    head = "B" * 100 + " "
    secret_start = len(head)
    # Put the match so the display window starts 5 chars INTO the secret.
    raw_idx = secret_start + 5 + _MATCH_PREVIEW_RADIUS
    gap = raw_idx - (secret_start + len(_STRADDLE_SECRET) + 1)
    original = (
        head + _STRADDLE_SECRET + " " + ("g" * gap) + _STRADDLE_NEEDLE + " tail " + ("C" * 100)
    )
    assert original.lower().find(_STRADDLE_NEEDLE) == raw_idx, "fixture geometry drifted"

    preview = _match_preview(original, _STRADDLE_NEEDLE)

    for fragment in _STRADDLE_FRAGMENTS:
        assert fragment not in preview, (
            f"near-edge straddling secret leaked fragment {fragment!r}: {preview!r}"
        )
    assert _STRADDLE_NEEDLE in preview, "the match window must still show the needle"
