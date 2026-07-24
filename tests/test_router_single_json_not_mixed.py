"""A single whole JSON value must never be classified MIXED (F2).

Report finding F2: a 2.7 MB single-line ``jq -c`` trace array produced zero furl
value. Root cause confirmed here at the routing layer: ``is_mixed_content`` ran
BEFORE content detection and the unanchored ``_PROSE_PATTERN`` scanned INSIDE the
array's string VALUES (event names, sentences, URLs). Combined with
``has_json_blocks`` (the content opens with ``[``), that was 2 indicators = MIXED,
so a structurally pure array took the char-by-char ``_extract_json_block`` mixed
splitter instead of routing straight to SmartCrusher.

The fix is a cheap structural short-circuit at the top of ``is_mixed_content``:
one whole JSON value (array or object) is pure structured content, never mixed.
These tests pin (a) a pure prose-laden single-line array routes PURE (SmartCrusher),
and (b) genuinely mixed prose+fence+array+search content still routes MIXED.
"""

from __future__ import annotations

import json

from furl_ctx.transforms.content_router import ContentRouter
from furl_ctx.transforms.router_debug import _mixed_indicators
from furl_ctx.transforms.router_policy import CompressionStrategy
from furl_ctx.transforms.router_split import _is_single_json_value, is_mixed_content

# A single-line array whose STRING VALUES are prose-like (each name matches
# ``_PROSE_PATTERN`` = ``[A-Z][a-z]+\s+\w+\s+\w+``), exactly the shape that a
# ``jq -c`` Chrome-trace slice produces.
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
    """The structural short-circuit: a whole JSON array is pure, never mixed."""
    assert _is_single_json_value(_SINGLE_LINE_ARRAY) is True
    assert is_mixed_content(_SINGLE_LINE_ARRAY) is False


def test_single_line_array_routes_smart_crusher_not_mixed() -> None:
    """End to end through the real router: the pure array reaches SmartCrusher,
    not the MIXED split path."""
    result = ContentRouter().compress(_SINGLE_LINE_ARRAY)
    assert result.strategy_used is CompressionStrategy.SMART_CRUSHER
    assert result.strategy_used is not CompressionStrategy.MIXED


def test_pure_json_object_is_not_mixed() -> None:
    """A whole JSON object (e.g. a trace ``{metadata, traceEvents:[...]}`` shape)
    is likewise pure structured content, never mixed."""
    obj = json.dumps(
        {
            "metadata": {"source": "Trace From The Browser Session"},
            "traceEvents": [{"name": "Parse HTML Document Tree", "ts": 1}],
        }
    )
    assert _is_single_json_value(obj) is True
    assert is_mixed_content(obj) is False


def test_genuinely_mixed_content_still_classified_mixed() -> None:
    """The MIXED contract is preserved for real prose + fence + array + search
    output — it does not parse as one JSON value, so the heuristics still decide."""
    assert _is_single_json_value(_GENUINELY_MIXED) is False
    assert is_mixed_content(_GENUINELY_MIXED) is True


def test_json_array_followed_by_trailing_prose_is_not_short_circuited() -> None:
    """An array with trailing prose is NOT one whole JSON value (it does not end
    with ``]``), so the cheap structural guard declines and the heuristics govern
    — the short-circuit never swallows genuinely trailing content."""
    trailing = '[{"id": 1}, {"id": 2}] and then Some Trailing Prose Words appear.'
    assert _is_single_json_value(trailing) is False
