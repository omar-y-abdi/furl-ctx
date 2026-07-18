"""Banner-honesty guard for README performance claims (review v10 S2).

The FIRST performance claim a reader meets must lead with the realistic range
("typically 0–54%..."), never the best-case 95% ceiling. The 95% figure may
appear only AFTER the realistic range has been stated. This was satisfied when
the guard landed (PR #83's banner rewrite already led realistic-first); the test
exists so a future banner edit cannot reintroduce a 95%-first claim. The plugin
README carries no standalone performance percentages (verified by the second
test) — if one is ever added, it inherits the same realistic-first rule.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"
_PLUGIN_README = _ROOT / "plugins" / "furl" / "README.md"

_REALISTIC_RANGE_MARKERS = ("0–54%", "0-54%")


def _first_index(text: str, needles: tuple[str, ...]) -> int:
    hits = [text.find(needle) for needle in needles if needle in text]
    return min(hits) if hits else -1


def test_readme_first_perf_claim_leads_with_realistic_range() -> None:
    text = _README.read_text(encoding="utf-8")
    realistic_at = _first_index(text, _REALISTIC_RANGE_MARKERS)
    ceiling_at = text.find("95%")
    assert realistic_at != -1, "README lost the realistic 0–54% range entirely"
    assert ceiling_at != -1, "README lost the 95% ceiling figure (with its honest read)"
    assert realistic_at < ceiling_at, (
        "the 95% ceiling appears before the realistic 0–54% range — the first "
        "performance claim must lead with the realistic band (v10 S2)"
    )


def test_plugin_readme_has_no_ceiling_first_claim() -> None:
    text = _PLUGIN_README.read_text(encoding="utf-8")
    if "95%" not in text:
        return  # no ceiling claim at all — trivially compliant
    realistic_at = _first_index(text, _REALISTIC_RANGE_MARKERS)
    assert realistic_at != -1 and realistic_at < text.find("95%"), (
        "plugin README states the 95% ceiling without leading with the realistic range"
    )
