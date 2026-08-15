"""A single whole JSON ARRAY must never be classified MIXED (F2 / F-1).

Report finding F2: a 2.7 MB single-line ``jq -c`` trace array produced zero furl
value. Root cause confirmed here at the routing layer: ``is_mixed_content`` ran
BEFORE content detection and the unanchored ``_PROSE_PATTERN`` scanned INSIDE the
array's string VALUES (event names, sentences, URLs). Combined with
``has_json_blocks`` (the content opens with ``[``), that was 2 indicators = MIXED,
so a structurally pure array took the char-by-char ``_extract_json_block`` mixed
splitter instead of routing straight to SmartCrusher.

The fix is a cheap structural short-circuit at the top of ``is_mixed_content``:
one whole JSON ARRAY is pure structured content, never mixed. It is ARRAYS ONLY
(review F-1): a top-level OBJECT is not detected as ``json_array`` and its only
route to compression today is the MIXED path, so excusing it from MIXED would
strand it at zero savings. These tests pin BOTH directions so neither regression
can return green: (a) a prose-laden single-line ARRAY routes not-mixed and reaches
SMART_CRUSHER, (b) a prose-laden single OBJECT still routes MIXED and actually
compresses through the production ``compress`` path, and genuinely mixed
prose+fence+array+search content still routes MIXED.
"""

from __future__ import annotations

import json

from furl_ctx.transforms.content_router import ContentRouter
from furl_ctx.transforms.router_debug import _mixed_indicators
from furl_ctx.transforms.router_policy import CompressionStrategy
from furl_ctx.transforms.router_split import _is_single_json_array, is_mixed_content

# A single-line array whose STRING VALUES are prose-like (each name matches ``_PROSE_PATTERN`` =
# ``[A-Z][a-z]+\s+\w+\s+\w+``), exactly the shape that a ``jq -c`` Chrome-trace slice produces.
_PROSE_EVENT_NAMES = [
    "Parse HTML Document Tree",
    "Recalculate Style Rules",
    "Layout Shift Computed Region",
    "Paint Composited Layer Tree",
    "Evaluate Script Module Body",
    "Run Microtasks Queue Now",
    "Update Layer Tree Structure",
    "Fire Animation Frame Callback",
]
_SINGLE_LINE_ARRAY = json.dumps(
    [
        {
            "pid": 1000 + i,
            "ts": 100 + i,
            "name": _PROSE_EVENT_NAMES[i % len(_PROSE_EVENT_NAMES)],
            "url": "https://cdn.example.com/assets/app.bundle.js",
        }
        for i in range(30)
    ],
    separators=(",", ":"),
)

# A prose-laden single JSON OBJECT (review F-1). It trips the same 2 indicators as the array, but a
# top-level object is NOT detected as json_array, so it must stay on the MIXED path to reach SmartCrusher.
_PROSE_LADEN_OBJECT = json.dumps(
    {
        f"field_{i}": {
            "name": "Recalculate Style Rules For The Document Tree",
            "detail": "Background thread compiled the parser cache entry here now",
            "url": "https://cdn.example.com/assets/app.bundle.js",
            "note": "This span records the main thread doing layout work again",
        }
        for i in range(9)
    },
    separators=(",", ":"),
)

# Genuinely mixed: prose + a ``` fenced code block + an inline array + grep lines.
_GENUINELY_MIXED = "\n".join(
    [
        "Intro paragraph With Several Words for prose detection to fire.",
        "Another line With Enough Words to read as normal prose today.",
        "Third line Adds More Prose so the detector sees real text here.",
        "Fourth sentence Keeps The Count moving higher for prose patterns.",
        "Fifth sentence Does The Same for mixed content identification now.",
        "Sixth sentence Seals The Threshold for the prose helper cleanly.",
        "```python",
        "def main():",
        "    return 1",
        "```",
        '[{"id": 1}]',
        "src/app.py:10:def main():",
    ]
)


def test_prose_laden_single_line_array_would_have_tripped_the_heuristic() -> None:
    """The raw indicators confirm the pre-fix MIXED trap: prose INSIDE the JSON
    string values plus the opening bracket were the two indicators that misrouted
    a pure array. This is what the structural short-circuit now overrides."""
    indicators = _mixed_indicators(_SINGLE_LINE_ARRAY)
    assert indicators["has_json_blocks"] is True
    assert indicators["has_prose"] is True
    # 2 indicators => the bare heuristic would have said "mixed".
    assert sum(indicators.values()) >= 2


def test_single_line_array_is_not_mixed() -> None:
    """Direction (a): the structural short-circuit — a whole JSON array is pure,
    never mixed."""
    assert _is_single_json_array(_SINGLE_LINE_ARRAY) is True
    assert is_mixed_content(_SINGLE_LINE_ARRAY) is False


def test_single_line_array_routes_smart_crusher_not_mixed() -> None:
    """Direction (a), end to end through the real router: the pure ARRAY reaches
    SmartCrusher, not the MIXED split path. Fails if the short-circuit is removed
    (the array would route MIXED again)."""
    result = ContentRouter().compress(_SINGLE_LINE_ARRAY)
    assert result.strategy_used is CompressionStrategy.SMART_CRUSHER
    assert result.strategy_used is not CompressionStrategy.MIXED


def test_prose_laden_object_still_mixed_and_compresses() -> None:
    """Direction (b), the F-1 anti-regression pin: a prose-laden single JSON
    OBJECT is NOT short-circuited (arrays only), so it keeps the MIXED route and
    actually compresses through the production ``compress`` path. This is the exact
    silent regression the reviewer found — widening the short-circuit type set back
    to accept objects sends this to text/PASSTHROUGH at ratio 1.0 and fails here."""
    assert _is_single_json_array(_PROSE_LADEN_OBJECT) is False
    assert is_mixed_content(_PROSE_LADEN_OBJECT) is True
    result = ContentRouter().compress(_PROSE_LADEN_OBJECT)
    # The object genuinely shrinks; a short-circuit-to-text regression would leave
    # it at ratio 1.0.
    assert result.compression_ratio < 0.5, result.strategy_used


def test_genuinely_mixed_content_still_classified_mixed() -> None:
    """The MIXED contract is preserved for real prose + fence + array + search
    output — it is not one whole JSON array, so the heuristics still decide."""
    assert _is_single_json_array(_GENUINELY_MIXED) is False
    assert is_mixed_content(_GENUINELY_MIXED) is True


def test_json_array_followed_by_trailing_prose_is_not_short_circuited() -> None:
    """An array with trailing prose is NOT one whole JSON array (it does not end
    with ``]``), so the cheap structural guard declines and the heuristics govern
    — the short-circuit never swallows genuinely trailing content."""
    trailing = '[{"id": 1}, {"id": 2}] and then Some Trailing Prose Words appear.'
    assert _is_single_json_array(trailing) is False
