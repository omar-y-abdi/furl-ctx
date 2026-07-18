"""Docs-honesty pins for the FURL_PRETOOL_PIPE surfaces (review F2/F4/F5/F8).

The adversarial review of PR #81 falsified the docs' absolute claim that the
pipe's "stderr passes through untouched": stderr is indeed never captured and
flows live, but stdout is buffered to a tempfile and emitted at the end, so in a
merged view ALL stderr precedes ALL stdout — interleaving is NOT preserved
(F2, behavior itself pinned in test_pretool_pipe.py
::test_stderr_stdout_interleaving_not_preserved). These pins keep every doc
surface honest about that, and keep the Known-limitations disclosures (F4
unterminated-heredoc divergence, F5 redaction gaps on fail-open paths + the
0600 tempfile transit, F8 allowlist/uv-resolve/`line N:` notes) from silently
disappearing. Pure text checks — no furl_ctx import.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "plugins" / "furl" / "README.md"
_SKILL = _ROOT / "plugins" / "furl" / "skills" / "furl" / "SKILL.md"
_LIBRARY = _ROOT / "LIBRARY.md"
_PRETOOL = _ROOT / "plugins" / "furl" / "hooks" / "pretool_pipe.py"
_PIPE_COMPRESS = _ROOT / "plugins" / "furl" / "hooks" / "pipe_compress.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_stderr_ordering_claim_is_honest_everywhere() -> None:
    """F2: the false absolute claim is gone; the honest interleaving caveat is
    present on every pipe doc surface (README, LIBRARY, SKILL, and the rewrite
    script's own docstrings)."""
    for path in (_README, _LIBRARY, _SKILL):
        text = _read(path)
        assert "stderr passes through untouched" not in text, path
        assert "stderr untouched" not in text, path
        assert "interleav" in text.lower(), f"{path}: missing the interleaving caveat"
    src = _read(_PRETOOL)
    assert "untouched" not in src.lower(), "pretool_pipe.py still claims stderr untouched"
    assert "interleav" in src.lower(), "pretool_pipe.py: missing the interleaving caveat"


def test_known_limitations_documented() -> None:
    """F4/F5/F8 + T2 + review-84 G6: the plugin README carries a Known-limitations
    section naming the heredoc divergence, the trailing-odd-backslash stdout
    caveat (line continuation; bare bash itself version-divergent), the redaction
    gaps (incl. the tempfile transit), the Bash(...) allowlist mismatch, the
    cold-start uv resolves, the cosmetic `line N:` prefix, and the
    PERMISSION-rule visibility bounds of the deny/ask guard (settings files yes;
    CLI --permission-mode flags, enterprise managed policy, session state no —
    with the FURL_PRETOOL_PIPE=0 escape hatch)."""
    text = _read(_README)
    assert "Known limitations" in text
    section = text.split("Known limitations", 1)[1].lower()
    needles = (
        "heredoc",
        "backslash",
        "redaction",
        "tempfile",
        "allowlist",
        "uv",
        "line n",
        "permission",
        "--permission-mode",
        "enterprise",
    )
    for needle in needles:
        assert needle in section, f"README Known limitations: missing {needle!r}"


def test_redaction_claim_is_qualified() -> None:
    """F5: no surface may claim unqualified 'same redaction as PostToolUse' — the
    fail-open paths (binary/undecodable stdout, furl_ctx unavailable) pass through
    unredacted, and the compressor's own docstring must say so."""
    src = _read(_PIPE_COMPRESS)
    assert "undecodable" in src.lower()
    assert "unredacted" in src.lower()
    # LIBRARY's pipe paragraph must qualify its redaction mention too.
    library = _read(_LIBRARY)
    assert "same TTL and redaction as the PostToolUse path" not in library


def test_pretool_pipe_env_row_has_where_to_set_guidance() -> None:
    """D2: the FURL_PRETOOL_PIPE env-table row tells the user WHERE the flag must
    be set. The gate runs via ``sh -c``, a non-login shell, so an export belongs
    in the environment Claude Code launches from, not in a login profile. This
    mirrors the FURL_STATUS_LINE row's where-to-set pattern."""
    text = _read(_README)
    rows = [line for line in text.splitlines() if line.startswith("| `FURL_PRETOOL_PIPE`")]
    assert rows, "README env table lost its FURL_PRETOOL_PIPE row"
    row = rows[0]
    assert "sh -c" in row, "row lacks the non-login-shell `sh -c` gate detail"
    assert "launch environment" in row, "row lacks launch-environment where-to-set guidance"


def test_pipe_counters_named_in_observability_section() -> None:
    """D3: once the pipe has run, its counters appear in the same furl_stats
    ``store.hook_activity`` block — the Observability counters section must name
    them so a pipe user knows where to look."""
    text = _read(_README)
    assert "### Observability counters" in text
    section = text.split("### Observability counters", 1)[1].split("### ", 1)[0]
    for name in ("pipe_invocations_seen", "pipe_compressions_applied", "pipe_noop_reasons"):
        assert name in section, f"Observability section missing {name}"
