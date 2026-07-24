"""A bare ``` fence must not fabricate a language tag on the wire.

``split_into_sections`` used to record ``language = match.group(1) or
"unknown"``, so a fence with no language annotation (a bare ``` ... ```
block) was tagged "unknown" even though the source never contained that
word. ``_compress_mixed``'s reassembly guard, ``if section.is_code_fence
and section.language:``, was then unconditionally true for every fenced
section (since "unknown" is truthy), so once any section in the mixed
content changed, the reassembled output shipped a fabricated ```` ```unknown
```` fence — an identifier invented by the router, not present in what the
caller sent. Reproduced end to end through ``ContentRouter().compress`` with
a bare fence next to an 80-row JSON array (compressible, so MIXED reassembly
fires): the output carried ```` ```unknown ````.

Pinned here:
* ``split_into_sections`` records "" (not "unknown") for a bare fence;
* an explicit language tag is still captured unchanged;
* ``_compress_mixed`` reassembles a bare fence back to bare ``` ```` (no
  language word), and a tagged fence still carries its real language;
* the end-to-end ``compress()`` repro no longer contains "unknown".
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.transforms.content_router import CompressionStrategy, ContentRouter
from furl_ctx.transforms.router_split import split_into_sections


def test_bare_fence_records_empty_language_not_unknown() -> None:
    content = "\n".join(["intro", "```", "print(1)", "```", "tail"])
    sections = split_into_sections(content)

    fenced = [s for s in sections if s.is_code_fence]
    assert len(fenced) == 1
    assert fenced[0].language == ""


def test_tagged_fence_still_captures_its_language() -> None:
    content = "\n".join(["intro", "```python", "print(1)", "```", "tail"])
    sections = split_into_sections(content)

    fenced = [s for s in sections if s.is_code_fence]
    assert len(fenced) == 1
    assert fenced[0].language == "python"


def _identity_strategy_but_changed(monkeypatch: pytest.MonkeyPatch, router: ContentRouter) -> None:
    # Return content different from the input so any_section_changed is True
    # and reassembly (rather than verbatim passthrough) actually runs.
    monkeypatch.setattr(
        router,
        "_apply_strategy_to_content",
        lambda content, strategy, context, language=None, question=None, bias=1.0, **kwargs: (
            content.replace("alpha", "ALPHA"),
            len(content.split()),
            [strategy.value],
        ),
    )


def test_compress_mixed_reassembles_bare_fence_without_language_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = ContentRouter()
    _identity_strategy_but_changed(monkeypatch, router)
    content = (
        "Intro prose describing The Overall Result Of The Run in several words.\n"
        "\n"
        "```\n"
        "print('alpha')\n"
        "```\n"
        "\n"
        "Tail prose Summarizing What Happened After The Run in more alpha words."
    )

    result = router._compress_mixed(content, "ctx")

    assert result.strategy_used is CompressionStrategy.MIXED
    assert "```unknown" not in result.compressed
    assert "```\nprint('ALPHA')\n```" in result.compressed


def test_compress_mixed_reassembles_tagged_fence_with_its_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = ContentRouter()
    _identity_strategy_but_changed(monkeypatch, router)
    content = (
        "Intro prose describing The Overall Result Of The Run in several words.\n"
        "\n"
        "```python\n"
        "print('alpha')\n"
        "```\n"
        "\n"
        "Tail prose Summarizing What Happened After The Run in more alpha words."
    )

    result = router._compress_mixed(content, "ctx")

    assert result.strategy_used is CompressionStrategy.MIXED
    assert "```python\nprint('ALPHA')\n```" in result.compressed


def test_end_to_end_compress_never_fabricates_unknown_fence_language() -> None:
    """Live repro (no mocking): a bare fence beside a compressible JSON
    array, routed through the real ContentRouter().compress()."""
    rows = [{"id": i, "name": f"item-{i}", "status": "ok"} for i in range(80)]
    content = (
        "Intro prose describing The Overall Result Of The Run in several words.\n"
        "\n"
        "```\n"
        "print(1)\n"
        "```\n"
        "\n" + json.dumps(rows) + "\n"
        "\n"
        "Tail prose Summarizing What Happened After The Run in more words."
    )

    result = ContentRouter().compress(content, context="ctx")

    assert result.strategy_used is CompressionStrategy.MIXED
    assert "unknown" not in result.compressed
    assert "```\nprint(1)\n```" in result.compressed
