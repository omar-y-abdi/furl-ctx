"""Mixed-content detection and splitting for the ContentRouter.

Extracted verbatim from ``content_router.py``. These are pure, module-level
functions over strings — no dependency on ``ContentRouter`` or its runtime
state. The orchestration method ``_compress_mixed`` stays on the router; only
the pure split helpers and their supporting data live here.

Dependency-light by design: this module imports only ``ContentType`` from
``content_detector`` (never ``content_router``), so re-importing these symbols
back into ``content_router`` introduces no import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .content_detector import ContentType


@dataclass
class ContentSection:
    """A typed section of content."""

    content: str
    content_type: ContentType
    language: str | None = None
    start_line: int = 0
    end_line: int = 0
    is_code_fence: bool = False


# Patterns for detecting mixed content
_CODE_FENCE_PATTERN = re.compile(r"^```(\w*)\s*$", re.MULTILINE)
_JSON_BLOCK_START = re.compile(r"^\s*[\[{]", re.MULTILINE)
_SEARCH_RESULT_PATTERN = re.compile(r"^\S+:\d+:", re.MULTILINE)
_PROSE_PATTERN = re.compile(r"[A-Z][a-z]+\s+\w+\s+\w+")

# A markdown task-list checkbox: "[ ] todo", "[x] done", "[X] done", optionally
# bulleted ("- [ ] …"). It starts with "[" but is NOT JSON (Bug-10) — the JSON
# block detector only balances brackets, it never parses, so an unguarded "[x]
# done" was extracted as a JSON_ARRAY section and handed to the JSON compressor.
_CHECKLIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+)?\[[ xX]\]")


def _starts_json_block(line: str) -> bool:
    """True when *line* begins a JSON array/object section (starts with ``[`` or
    ``{``) and is NOT a markdown checklist item (Bug-10)."""
    return line.strip().startswith(("[", "{")) and not _CHECKLIST_ITEM_RE.match(line)


def is_mixed_content(content: str) -> bool:
    """Detect if content contains multiple distinct types.

    Args:
        content: Content to analyze.

    Returns:
        True if content appears to be mixed (multiple types).
    """
    indicators = {
        "has_code_fences": bool(_CODE_FENCE_PATTERN.search(content)),
        "has_json_blocks": bool(_JSON_BLOCK_START.search(content)),
        "has_prose": len(_PROSE_PATTERN.findall(content)) > 5,
        "has_search_results": bool(_SEARCH_RESULT_PATTERN.search(content)),
    }

    # Mixed if 2+ indicators are true
    return sum(indicators.values()) >= 2


def split_into_sections(content: str) -> list[ContentSection]:
    """Parse mixed content into typed sections.

    Args:
        content: Mixed content to split.

    Returns:
        List of ContentSection objects.
    """
    sections: list[ContentSection] = []
    lines = content.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i]

        # Code fence: ```language
        if match := _CODE_FENCE_PATTERN.match(line):
            # A bare ``` fence has no language tag in the source; recording
            # it as the empty string (rather than a fabricated "unknown")
            # lets reassembly reproduce the original bare fence exactly.
            language = match.group(1)
            code_lines = []
            start_line = i
            i += 1

            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1

            sections.append(
                ContentSection(
                    content="\n".join(code_lines),
                    content_type=ContentType.SOURCE_CODE,
                    language=language,
                    start_line=start_line,
                    end_line=i,
                    is_code_fence=True,
                )
            )
            i += 1  # Skip closing ```
            continue

        # JSON block (a markdown checklist "[x] done" starts with "[" but is not
        # JSON — Bug-10 — so _starts_json_block excludes it).
        if _starts_json_block(line):
            json_content, end_i = _extract_json_block(lines, i)
            if json_content:
                sections.append(
                    ContentSection(
                        content=json_content,
                        content_type=ContentType.JSON_ARRAY,
                        start_line=i,
                        end_line=end_i,
                    )
                )
                i = end_i + 1
                continue

        # Search result lines
        if _SEARCH_RESULT_PATTERN.match(line):
            search_lines = []
            start_line = i
            while i < len(lines) and _SEARCH_RESULT_PATTERN.match(lines[i]):
                search_lines.append(lines[i])
                i += 1
            sections.append(
                ContentSection(
                    content="\n".join(search_lines),
                    content_type=ContentType.SEARCH_RESULTS,
                    start_line=start_line,
                    end_line=i - 1,
                )
            )
            continue

        # Collect text until next special section
        text_lines = [line]
        start_line = i
        i += 1

        while i < len(lines):
            next_line = lines[i]
            # Stop if we hit a special section
            if (
                _CODE_FENCE_PATTERN.match(next_line)
                or _starts_json_block(next_line)
                or _SEARCH_RESULT_PATTERN.match(next_line)
            ):
                break
            text_lines.append(next_line)
            i += 1

        # Only add non-empty text sections
        text_content = "\n".join(text_lines)
        if text_content.strip():
            sections.append(
                ContentSection(
                    content=text_content,
                    content_type=ContentType.PLAIN_TEXT,
                    start_line=start_line,
                    end_line=i - 1,
                )
            )

    return sections


def _extract_json_block(lines: list[str], start: int) -> tuple[str | None, int]:
    """Extract a complete JSON block from lines.

    Args:
        lines: All lines of content.
        start: Starting line index.

    Returns:
        Tuple of (json_content, end_line_index) or (None, start) if invalid.
    """
    bracket_count = 0
    brace_count = 0
    json_lines = []
    in_string = False
    escaped = False

    for i in range(start, len(lines)):
        line = lines[i]
        json_lines.append(line)

        # Count brackets/braces, but ignore any that appear inside a JSON
        # string literal — a naive line.count() treats e.g. the "]" in
        # {"path": "a]b"} as a closing bracket and terminates the block
        # early, splitting one array across multiple sections.
        for ch in line:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                if in_string:
                    escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                bracket_count += 1
            elif ch == "]":
                bracket_count -= 1
            elif ch == "{":
                brace_count += 1
            elif ch == "}":
                brace_count -= 1

        if bracket_count <= 0 and brace_count <= 0 and json_lines:
            return "\n".join(json_lines), i

    # Didn't find complete JSON
    return None, start
