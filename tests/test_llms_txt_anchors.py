"""Guards llms.txt's ``README.md#anchor`` links against README.md's real headings.

llms.txt exists for agent orientation — coding agents read it first to find their way
around the repo. Its "Docs" section links into README.md by heading anchor
(``README.md#some-heading``). GitHub computes those anchors from heading text at
render time (lowercase, spaces to hyphens, punctuation stripped); nothing enforces
that a linked anchor still exists after README.md is edited, so a heading rename or
removal silently breaks the link — llms.txt would then fail its own purpose.

This test parses every heading GitHub would render for README.md, computes the anchor
slug for each exactly as GitHub does, and asserts every ``README.md#...`` reference in
llms.txt resolves to one of them. The ``lint`` job in ``.github/workflows/ci.yml`` runs
the same check in pure Python with no repo import (the "llms.txt anchor guard" step),
so a broken anchor fails fast on every PR, not just when this suite runs. Keep the two
implementations in sync.

Scope: README.md anchors only, matching what llms.txt actually links to. llms.txt also
links a couple of headings into LIBRARY.md (e.g. ``LIBRARY.md#how-it-works``) — those
aren't covered here, since GitHub anchor drift is invisible until you go looking file
by file, and README.md is the one llms.txt was found citing incorrectly.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"
_LLMS_TXT = _ROOT / "llms.txt"

_FENCE_RE = re.compile(r"^(```|~~~)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]")
_README_ANCHOR_REF_RE = re.compile(r"README\.md#([A-Za-z0-9_-]+)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _github_slug(heading_text: str) -> str:
    """Reproduce GitHub's heading-to-anchor slug algorithm.

    Lowercase, strip anything that isn't a word character/space/hyphen, then turn
    spaces into hyphens (each consecutive space becomes its own hyphen — a heading
    like "When to use · When to skip" strips the middle dot and collapses to a
    double hyphen: ``when-to-use--when-to-skip``). Matches observed github.com
    rendering for the plain-ASCII headings this repo's docs use.
    """
    slug = heading_text.strip().lower()
    slug = _SLUG_STRIP_RE.sub("", slug)
    return slug.replace(" ", "-")


def _readme_anchor_slugs() -> set[str]:
    """Every anchor GitHub would generate for README.md's current headings.

    Skips fenced code blocks so a ``#`` shell comment inside a snippet is never
    mistaken for a Markdown heading. Repeats GitHub's duplicate-slug suffixing
    (``-1``, ``-2``, ...) for headings that render to the same slug twice.
    """
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in _read(_README).splitlines():
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        base = _github_slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def _readme_anchor_refs_in_llms_txt() -> list[str]:
    return _README_ANCHOR_REF_RE.findall(_read(_LLMS_TXT))


def test_llms_txt_has_at_least_one_readme_anchor_reference() -> None:
    # Non-vacuous guard: if every README.md#... link were ever removed from
    # llms.txt, the anchor-resolution test below would trivially pass on an
    # empty list. Fail loud instead, so removing the last one is a visible,
    # deliberate edit rather than a silent pass.
    assert _readme_anchor_refs_in_llms_txt(), (
        "expected at least one README.md#... anchor reference in llms.txt"
    )


def test_llms_txt_readme_anchors_exist_in_readme() -> None:
    valid = _readme_anchor_slugs()
    refs = _readme_anchor_refs_in_llms_txt()
    missing = sorted({ref for ref in refs if ref not in valid})
    assert not missing, (
        f"llms.txt references README.md anchors that do not exist: {missing}. "
        f"Valid README.md anchors: {sorted(valid)}"
    )
