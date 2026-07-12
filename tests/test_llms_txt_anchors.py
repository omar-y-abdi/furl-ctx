"""Guards llms.txt's ``<doc>.md#anchor`` links against the linked files' real headings.

llms.txt exists for agent orientation — coding agents read it first to find their way
around the repo. Its "Docs" section links into README.md and LIBRARY.md by heading
anchor (``README.md#install``, ``LIBRARY.md#how-it-works``). GitHub computes those
anchors from heading text at render time (lowercase, spaces to hyphens, punctuation
stripped); nothing enforces that a linked anchor still exists after the target file is
edited, so a heading rename or removal silently breaks the link — llms.txt would then
fail its own purpose.

This test parses every heading GitHub would render for each linked file, computes the
anchor slug for each exactly as GitHub does, and asserts every ``README.md#...`` /
``LIBRARY.md#...`` reference in llms.txt resolves in its target file. The ``lint`` job
in ``.github/workflows/ci.yml`` runs the same check in pure Python with no repo import
(the "llms.txt anchor guard" step), so a broken anchor fails fast on every PR, not
just when this suite runs. Keep the two implementations in sync — a behavioral
difference between them is itself a bug.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LLMS_TXT = _ROOT / "llms.txt"
# The files llms.txt links into by #anchor. Extend this tuple (and the mirrored
# ci.yml step) if llms.txt ever gains anchored links into another document —
# _ANCHOR_REF_RE is derived from it, so refs to a new doc cannot be silently
# skipped by a forgotten regex edit.
_DOC_NAMES = ("README.md", "LIBRARY.md")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]")
_ANCHOR_REF_RE = re.compile(
    "(" + "|".join(re.escape(name) for name in _DOC_NAMES) + ")#([A-Za-z0-9_-]+)"
)


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


def _anchor_slugs(markdown: str) -> set[str]:
    """Every anchor GitHub would generate for a Markdown document's headings.

    Skips fenced code blocks so a ``#`` shell/Python comment inside a snippet is
    never mistaken for a Markdown heading. Repeats GitHub's duplicate-slug
    suffixing (``-1``, ``-2``, ...) for headings that render to the same slug
    twice.
    """
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(line)
        if heading is None:
            continue
        base = _github_slug(heading.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def _anchor_refs_in_llms_txt() -> list[tuple[str, str]]:
    """``(target file, anchor)`` for every anchored doc link in llms.txt."""
    return _ANCHOR_REF_RE.findall(_read(_LLMS_TXT))


def test_llms_txt_has_at_least_one_doc_anchor_reference() -> None:
    # Non-vacuous guard: if every anchored doc link were ever removed from
    # llms.txt, the resolution test below would trivially pass on an empty list.
    # Fail loud instead, so removing the last one is a visible, deliberate edit
    # (update _DOC_NAMES and the mirrored ci.yml step together) rather than a
    # silent pass.
    assert _anchor_refs_in_llms_txt(), (
        "expected at least one README.md#.../LIBRARY.md#... anchor reference in llms.txt"
    )


def test_llms_txt_doc_anchors_exist_in_their_target_files() -> None:
    slugs_by_doc = {name: _anchor_slugs(_read(_ROOT / name)) for name in _DOC_NAMES}
    refs = _anchor_refs_in_llms_txt()
    missing = sorted({f"{doc}#{anchor}" for doc, anchor in refs if anchor not in slugs_by_doc[doc]})
    assert not missing, (
        f"llms.txt references anchors that do not exist in their target file: {missing}. "
        + " ".join(f"Valid {doc} anchors: {sorted(slugs_by_doc[doc])}." for doc in _DOC_NAMES)
    )
