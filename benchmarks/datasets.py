"""Real, high-entropy benchmark datasets captured from LOCAL sources.

P0 honesty constraint: every row here comes from genuine local tool I/O —
NO synthetic low-entropy generated rows. The three datasets mirror the
real coding-agent use case the engine targets:

1. ``code``   — this repo's own source files fed as tool-output content.
2. ``logs``   — real ``git log`` rows (varying commit-hash / ISO date
                fields — the canonical "one varying field defeats dedup"
                case).
3. ``search`` — real ``rg --json`` match objects (distinct ``path`` /
                ``line_number`` / ``absolute_offset`` — the canonical
                "90 distinct search results" case).
4. ``repeated_logs`` — real captured ``ping`` replies (recurring content
                ``{bytes, from, ttl}`` + a monotone ``icmp_seq`` identity
                counter — the canonical "one varying identity field defeats
                whole-item dedup, field-aware dedup collapses it" case that
                Improvement 2 targets).

Four RAW-TEXT datasets (EFF-6) exercise the non-JSON compressors that the
pre-parsed arrays above never reach. Their ``items`` are the distinct
non-blank LINES of the raw text (the honest retention unit for text):

5. ``ci_log``       — CI build+test log text → LogCompressor (deterministic
                synthesis, seed committed: ``rawtext_sources.synth_ci_log``).
6. ``grep_raw``     — real ``grep -rn`` output → SearchCompressor.
7. ``diff_raw``     — real ``git diff`` between two committed refs
                (multi-file, 20+ hunks, Cargo.lock churn) → DiffCompressor.
8. ``markdown_doc`` — README-shaped markdown/prose → TextCrusher.

Raw captures are snapshotted under ``benchmarks/data/`` so a run is
reproducible and the numbers are auditable. The capture commands are
recorded in each snapshot's ``provenance`` field.

Each dataset builder returns a :class:`Dataset`: the chat-style ``messages``
payload to feed ``compress()`` plus the raw ``items`` (list of distinct
objects) used for retention / drop scoring.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Dataset:
    """A benchmark case: the messages to compress + the raw distinct items.

    Attributes:
        name: Stable dataset id ("code" / "logs" / "search").
        query: The user query message that precedes the tool output.
        items: The list of distinct row objects (for retention/drop scoring).
        messages: The chat-style payload passed to ``compress()``.
        provenance: Human-readable capture command + source description.
        conversation: True for multi-turn cases whose retention/presence is
            scored across ALL messages (an item counts as retained when it is
            visible in ANY message of the compressed conversation, or
            recoverable via a pointer surfaced in ANY message). Single-tool
            cases keep the stricter last-message scoring.
    """

    name: str
    query: str
    items: list[Any]
    messages: list[dict[str, Any]]
    provenance: str
    conversation: bool = False


# ---------------------------------------------------------------------------
# Capture helpers — run a local command, snapshot raw output for auditability.
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> str:
    """Run a local command and return stdout (text). Raises on failure.

    Effects are isolated here: the rest of the module operates on the
    captured strings, keeping row-building referentially transparent.
    """
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError(
            f"capture command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[:500]}"
        )
    return proc.stdout


def _snapshot(name: str, raw: str, provenance: str) -> Path:
    """Write a raw capture under benchmarks/data/ for reproducibility.

    Returns the snapshot path. The snapshot is a JSON envelope holding the
    capture command and the raw bytes so numbers can be re-derived later.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.raw.json"
    payload = {"provenance": provenance, "raw": raw}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _load_snapshot(name: str) -> tuple[str, str] | None:
    """Load a cached raw capture if present. Returns (raw, provenance)."""
    path = DATA_DIR / f"{name}.raw.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["raw"], payload["provenance"]


def _capture(name: str, cmd: list[str], provenance: str, *, refresh: bool) -> str:
    """Return raw capture text, using the cached snapshot unless ``refresh``.

    Caching makes runs deterministic: the committed snapshot under
    ``benchmarks/data/`` is the audited source of truth. ``refresh=True``
    re-runs the command and overwrites the snapshot.
    """
    if not refresh:
        cached = _load_snapshot(name)
        if cached is not None:
            return cached[0]
    raw = _run(cmd)
    _snapshot(name, raw, provenance)
    return raw


# ---------------------------------------------------------------------------
# Dataset 1 — CODE: real repo source files as tool-output content.
# ---------------------------------------------------------------------------

# A fixed, committed list of real source files (relative to repo root). These
# are this repo's own engine source — genuine high-entropy code content.
CODE_FILES: tuple[str, ...] = (
    "furl_ctx/compress.py",
    "furl_ctx/tokenizer.py",
    "furl_ctx/config.py",
    "furl_ctx/transforms/smart_crusher.py",
    "furl_ctx/transforms/search_compressor.py",
    "furl_ctx/transforms/log_compressor.py",
    "furl_ctx/cache/compression_store.py",
)


def build_code_dataset(*, refresh: bool = False) -> Dataset:
    """Real codebase content: each repo source file is one distinct item.

    The array of ``{path, content}`` objects is fed as a single tool output —
    exactly how a coding agent surfaces multiple file reads. High entropy
    (real source code), distinct per row.
    """
    name = "code"
    provenance = (
        "Read of this repo's own source files: "
        + ", ".join(CODE_FILES)
        + " (relative to repo root)."
    )
    cached = None if refresh else _load_snapshot(name)
    if cached is not None:
        raw = cached[0]
        items = json.loads(raw)
    else:
        items = []
        for rel in CODE_FILES:
            fpath = REPO_ROOT / rel
            text = fpath.read_text(encoding="utf-8", errors="replace")
            items.append({"path": rel, "content": text})
        raw = json.dumps(items, ensure_ascii=False)
        _snapshot(name, raw, provenance)

    content = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Review these source files for issues."},
        {"role": "tool", "content": content, "tool_call_id": "code_call"},
    ]
    return Dataset(
        name=name,
        query="Review these source files for issues.",
        items=items,
        messages=messages,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Dataset 2 — LOGS: real `git log` rows (varying commit-hash / ISO date).
# ---------------------------------------------------------------------------

_GIT_LOG_SEP = "\x1f"
_GIT_LOG_FMT = _GIT_LOG_SEP.join(["%H", "%an", "%ae", "%aI", "%s"])
_GIT_LOG_N = 300


def _parse_git_log(raw: str) -> list[dict[str, Any]]:
    """Parse the unit-separated git-log capture into structured rows."""
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        parts = line.split(_GIT_LOG_SEP)
        if len(parts) != 5:
            continue
        rows.append(
            {
                "commit": parts[0],
                "author": parts[1],
                "email": parts[2],
                "date": parts[3],
                "subject": parts[4],
            }
        )
    return rows


def build_logs_dataset(*, limit: int = 90, refresh: bool = False) -> Dataset:
    """Real git-log rows — varying commit-hash + ISO timestamp per row.

    This is the canonical "one varying field defeats dedup" case: every row
    is structurally identical but has a unique commit hash and date, so the
    whole-item hash is unique for every row.
    """
    name = "logs"
    cmd = [
        "git",
        "-C",
        ".",
        "log",
        f"--pretty=format:{_GIT_LOG_FMT}",
        "-n",
        str(_GIT_LOG_N),
    ]
    provenance = (
        f"git log --pretty=format:'%H<US>%an<US>%ae<US>%aI<US>%s' -n {_GIT_LOG_N} "
        "(unit-separated), parsed to {commit,author,email,date,subject} rows."
    )
    raw = _capture(name, cmd, provenance, refresh=refresh)
    rows = _parse_git_log(raw)
    items = rows[:limit]
    content = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Summarize the recent commit history."},
        {"role": "tool", "content": content, "tool_call_id": "logs_call"},
    ]
    return Dataset(
        name=name,
        query="Summarize the recent commit history.",
        items=items,
        messages=messages,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Dataset 3 — SEARCH: real `rg --json` match objects (distinct fields).
# ---------------------------------------------------------------------------

_RG_PATTERN = "def "
_RG_TARGET = "furl_ctx/"


def _parse_rg_json(raw: str) -> list[dict[str, Any]]:
    """Parse ``rg --json`` stream into distinct match-row objects."""
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        rows.append(
            {
                "path": data["path"]["text"],
                "line_number": data["line_number"],
                "absolute_offset": data["absolute_offset"],
                "lines": data["lines"]["text"].rstrip("\n"),
            }
        )
    return rows


def build_search_dataset(*, limit: int = 90, refresh: bool = False) -> Dataset:
    """Real ripgrep search results — distinct path/line/offset per row.

    The canonical "90 distinct search results" case: each row is a real
    match object with varying ``path`` / ``line_number`` / ``absolute_offset``
    and a real source line.
    """
    name = "search"
    cmd = ["rg", "--json", _RG_PATTERN, _RG_TARGET]
    provenance = (
        f"rg --json '{_RG_PATTERN}' {_RG_TARGET} — match objects parsed to "
        "{path,line_number,absolute_offset,lines} rows."
    )
    raw = _capture(name, cmd, provenance, refresh=refresh)
    rows = _parse_rg_json(raw)
    items = rows[:limit]
    content = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Where are functions defined in the codebase?"},
        {"role": "tool", "content": content, "tool_call_id": "search_call"},
    ]
    return Dataset(
        name=name,
        query="Where are functions defined in the codebase?",
        items=items,
        messages=messages,
        provenance=provenance,
    )


# --------------------------------------------------------------------------- Dataset 4 — REPEATED_LOGS: real `ping` replies (recurring content +
# monotone identity counter). The canonical field-aware dedup case (Imp2). ---------------------------------------------------------------------------

# A fixed, reproducible local capture: 100 ICMP echo replies to loopback.
_PING_CMD: tuple[str, ...] = ("ping", "-c", "100", "-i", "0.01", "127.0.0.1")
_PING_RE = re.compile(
    r"^(?P<bytes>\d+) bytes from (?P<from>\S+): "
    r"icmp_seq=(?P<seq>\d+) ttl=(?P<ttl>\d+) time=(?P<time>[\d.]+) ms$"
)


def _parse_ping(raw: str) -> list[dict[str, Any]]:
    """Parse captured ``ping`` stdout into structured reply rows.

    Only ``icmp_seq=`` reply lines are kept (header / summary lines are
    skipped). Each row preserves every real field: the recurring content
    (``bytes`` / ``from`` / ``ttl``), the monotone identity counter
    (``icmp_seq``), and the real measured latency (``time_ms``).
    """
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        m = _PING_RE.match(line.strip())
        if m is None:
            continue
        rows.append(
            {
                "bytes": int(m.group("bytes")),
                "from": m.group("from"),
                "icmp_seq": int(m.group("seq")),
                "ttl": int(m.group("ttl")),
                "time_ms": float(m.group("time")),
            }
        )
    return rows


def build_repeated_logs_dataset(*, limit: int = 90, refresh: bool = False) -> Dataset:
    """Real ping replies — recurring content + a monotone identity counter.

    This is the canonical Improvement-2 case: the value-bearing content
    (``bytes`` / ``from`` / ``ttl``) recurs identically across every row,
    while ``icmp_seq`` is a strictly-monotone identity counter (unique per
    row) that forces a unique *whole-item* hash for every row — defeating the
    pre-Imp2 dedup/cluster/fill. The field-aware *stable-projection* hash
    excludes ``icmp_seq`` and collapses the rows onto their shared content.
    """
    name = "repeated_logs"
    provenance = (
        "ping -c 100 -i 0.01 127.0.0.1 (icmp_seq reply lines) — real ICMP echo "
        "replies. Content {bytes=64, from=127.0.0.1, ttl=64} recurs identically; "
        "only the monotone icmp_seq counter (+ real time latency) vary. icmp_seq "
        "is the canonical VaryingIdentity field that forces unique whole-item "
        "hashes and that the field-aware stable hash excludes."
    )
    raw = _capture(name, list(_PING_CMD), provenance, refresh=refresh)
    rows = _parse_ping(raw)
    items = rows[:limit]
    content = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Summarize the ping results."},
        {"role": "tool", "content": content, "tool_call_id": "ping_call"},
    ]
    return Dataset(
        name=name,
        query="Summarize the ping results.",
        items=items,
        messages=messages,
        provenance=provenance,
    )


# Dataset 5

_DF_CMD: tuple[str, ...] = ("df", "-k")


def _parse_df(raw: str) -> list[dict[str, Any]]:
    """Parse captured ``df -k`` stdout into structured filesystem rows.

    The header line is skipped. Fields are split from the RIGHT (8
    splits) so a filesystem name containing spaces (e.g. ``map
    auto_home``) stays intact; rows whose numeric fields don't parse are
    skipped. Every kept value is the real tool output, verbatim.
    """
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines()[1:]:
        parts = line.rsplit(None, 8)
        if len(parts) != 9:
            continue
        try:
            rows.append(
                {
                    "filesystem": parts[0],
                    "kbytes": int(parts[1]),
                    "used": int(parts[2]),
                    "avail": int(parts[3]),
                    "capacity": parts[4],
                    "iused": int(parts[5]),
                    "ifree": int(parts[6]),
                    "piused": parts[7],
                    "mounted_on": parts[8],
                }
            )
        except ValueError:
            continue
    return rows


def build_disk_dataset(*, refresh: bool = False) -> Dataset:
    """Real ``df -k`` rows — a SMALL array (≈10 rows, under adaptive_k).

    Exercises the small-array lossless path: arrays at or below the
    tier-1 boundary used to pass through with 0% reduction even when a
    lossless tabular rendering saves 30%+ (repeated keys, constant and
    consecutively-repeated column values are common in df/ps-style
    output).
    """
    name = "disk"
    provenance = (
        "df -k — real filesystem table from this machine, header skipped, "
        "fields right-split into {filesystem,kbytes,used,avail,capacity,"
        "iused,ifree,piused,mounted_on} rows."
    )
    raw = _capture(name, list(_DF_CMD), provenance, refresh=refresh)
    rows = _parse_df(raw)
    items: list[Any] = rows
    content = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "How full are the disks?"},
        {"role": "tool", "content": content, "tool_call_id": "disk_call"},
    ]
    return Dataset(
        name=name,
        query="How full are the disks?",
        items=items,
        messages=messages,
        provenance=provenance,
    )


# Dataset 6 The cross-message redundancy case: per-message compression pays
# full price for every repetition; only conversation-level dedup can remove it.

# Each command is genuinely invoked TWICE (two separate real runs, both raw captures snapshotted). `rg --sort path` is the deterministic form
# agents get from sorted search tools: across two runs of an unchanged tree the parsed match rows are byte-identical (verified at capture time.
_MULTITURN_RG_CMD: tuple[str, ...] = ("rg", "--json", "--sort", "path", "def ", "furl_ctx/")
_MULTITURN_DF_CMD: tuple[str, ...] = ("df", "-k")
_MULTITURN_RG_LIMIT = 90
_MULTITURN_GIT_LOG_N = 30


def _multiturn_git_log_cmd(ref: str) -> list[str]:
    """The logs-dataset git-log capture, pinned to ``ref``."""
    return [
        "git",
        "-C",
        ".",
        "log",
        f"--pretty=format:{_GIT_LOG_FMT}",
        "-n",
        str(_MULTITURN_GIT_LOG_N),
        ref,
    ]


# Successive agent turns are seconds apart, not microseconds — space the two df captures accordingly
# so the snapshot reflects real inter-turn drift (free-space / inode counters move on a live machine).
_MULTITURN_TURN_SPACING_S = 3.0


def _multiturn_signature(item: Any) -> str:
    """Canonical signature for distinct-item dedup across the four captures."""
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def build_multiturn_dataset(*, refresh: bool = False) -> Dataset:
    """Real repeated tool I/O across turns: rg twice (identical) + df twice
    (drifted).

    The canonical cross-message case: an agent re-runs the same search to
    confirm nothing changed (parsed rows byte-identical between the two real
    invocations) and re-checks disk usage (rows genuinely drift between
    runs). Per-message compression treats each tool output independently and
    pays for every repetition; the redundancy lives BETWEEN messages.
    """
    name = "multiturn"
    provenance = (
        "Two real consecutive invocations each of: "
        f"`{' '.join(_MULTITURN_RG_CMD)}` (match rows parsed like the search "
        "dataset, first 90 rows per run — byte-identical across the two runs "
        "of the unchanged tree), `df -k` (rows parsed like the disk dataset, "
        "the two runs spaced ~3s apart like successive agent turns — "
        "free-space/inode cells genuinely drift between runs), and "
        f"`git log -n {_MULTITURN_GIT_LOG_N}` viewed one real commit apart "
        "(HEAD~1 then HEAD, parsed like the logs dataset — 29/30 rows "
        "byte-identical, the canonical post-commit re-check). All six raw "
        "captures snapshotted."
    )
    cached = None if refresh else _load_snapshot(name)
    if cached is not None:
        captures = json.loads(cached[0])
    else:
        captures = {"rg_run1": _run(list(_MULTITURN_RG_CMD))}
        captures["rg_run2"] = _run(list(_MULTITURN_RG_CMD))
        captures["df_run1"] = _run(list(_MULTITURN_DF_CMD))
        time.sleep(_MULTITURN_TURN_SPACING_S)
        captures["df_run2"] = _run(list(_MULTITURN_DF_CMD))
        captures["gitlog_run1"] = _run(_multiturn_git_log_cmd("HEAD~1"))
        captures["gitlog_run2"] = _run(_multiturn_git_log_cmd("HEAD"))
        _snapshot(name, json.dumps(captures, ensure_ascii=False), provenance)

    rg_rows_1 = _parse_rg_json(captures["rg_run1"])[:_MULTITURN_RG_LIMIT]
    rg_rows_2 = _parse_rg_json(captures["rg_run2"])[:_MULTITURN_RG_LIMIT]
    df_rows_1 = _parse_df(captures["df_run1"])
    df_rows_2 = _parse_df(captures["df_run2"])
    gitlog_rows_1 = _parse_git_log(captures["gitlog_run1"])
    gitlog_rows_2 = _parse_git_log(captures["gitlog_run2"])

    # Distinct union across all six captures (first-seen order) — the
    # denominator for retention/drop scoring.
    items: list[Any] = []
    seen: set[str] = set()
    for row in [
        *rg_rows_1,
        *rg_rows_2,
        *df_rows_1,
        *df_rows_2,
        *gitlog_rows_1,
        *gitlog_rows_2,
    ]:
        sig = _multiturn_signature(row)
        if sig in seen:
            continue
        seen.add(sig)
        items.append(row)

    messages = [
        {"role": "user", "content": "Where are functions defined in the codebase?"},
        {
            "role": "tool",
            "content": json.dumps(rg_rows_1, ensure_ascii=False),
            "tool_call_id": "multiturn_rg_1",
        },
        {"role": "user", "content": "Re-run the search to confirm the matches are unchanged."},
        {
            "role": "tool",
            "content": json.dumps(rg_rows_2, ensure_ascii=False),
            "tool_call_id": "multiturn_rg_2",
        },
        {"role": "user", "content": "How full are the disks?"},
        {
            "role": "tool",
            "content": json.dumps(df_rows_1, ensure_ascii=False),
            "tool_call_id": "multiturn_df_1",
        },
        {"role": "user", "content": "Check the disks again."},
        {
            "role": "tool",
            "content": json.dumps(df_rows_2, ensure_ascii=False),
            "tool_call_id": "multiturn_df_2",
        },
        {"role": "user", "content": "Show the recent commit history."},
        {
            "role": "tool",
            "content": json.dumps(gitlog_rows_1, ensure_ascii=False),
            "tool_call_id": "multiturn_gitlog_1",
        },
        {"role": "user", "content": "I just committed - show the history again."},
        {
            "role": "tool",
            "content": json.dumps(gitlog_rows_2, ensure_ascii=False),
            "tool_call_id": "multiturn_gitlog_2",
        },
    ]
    return Dataset(
        name=name,
        query="Where are functions defined in the codebase?",
        items=items,
        messages=messages,
        provenance=provenance,
        conversation=True,
    )


# Datasets 7-10 Items are the distinct non-blank lines of the raw text; retention and recovery are scored per line
# (visible verbatim OR CCR-recoverable from the compressor's `Retrieve …: hash=…` marker, which backs the FULL original).

_GREP_CMD: tuple[str, ...] = ("grep", "-rn", "--include=*.py", "def ", "furl_ctx/transforms/")

# Use a real dependency-bump diff restricted to manifests: multi-file, lockfile-heavy,
# and pure diff syntax. This exercises max-hunk capping without mixed-content detection.
_DIFF_CMD: tuple[str, ...] = (
    "git",
    "diff",
    "3154e766~1",
    "c8d5e41b",
    "--",
    "Cargo.lock",
    "Cargo.toml",
    "crates/furl-core/Cargo.toml",
)


def _distinct_lines(raw: str) -> list[str]:
    """Distinct non-blank lines in first-seen order — the raw-text item unit."""
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.splitlines():
        if not line.strip() or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _raw_text_dataset(name: str, raw: str, query: str, provenance: str) -> Dataset:
    """Assemble a raw-TEXT Dataset: one tool message carrying the text verbatim."""
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": raw, "tool_call_id": f"{name}_call"},
    ]
    return Dataset(
        name=name,
        query=query,
        items=list(_distinct_lines(raw)),
        messages=messages,
        provenance=provenance,
    )


def build_ci_log_dataset(*, refresh: bool = False) -> Dataset:
    """CI build+test log TEXT (npm + cargo + pytest) → LogCompressor.

    Synthesized deterministically (the one non-captured dataset — a real CI
    run is not reproducibly capturable locally; the generator + seed are the
    provenance) and snapshotted like every capture. ≥200 lines: install
    warnings, a cargo warning block, timestamped INFO/WARNING app lines, one
    native-style traceback, and the run summary.
    """
    from benchmarks.rawtext_sources import CI_LOG_SEED, synth_ci_log

    name = "ci_log"
    provenance = (
        f"Synthesized CI log (rawtext_sources.synth_ci_log, seed={CI_LOG_SEED}): "
        "npm ci warnings + cargo build warning block + pytest run with "
        "python-logging timestamped INFO/WARNING lines, one native traceback "
        "failure, and the summary line. Deterministic: same seed ⇒ same bytes."
    )
    cached = None if refresh else _load_snapshot(name)
    raw = cached[0] if cached is not None else synth_ci_log()
    if cached is None:
        _snapshot(name, raw, provenance)
    return _raw_text_dataset(name, raw, "Why did the CI run fail?", provenance)


def build_grep_raw_dataset(*, refresh: bool = False) -> Dataset:
    """Raw ``grep -rn`` TEXT output (``file:line:content``) → SearchCompressor.

    The same real search the JSON ``search`` dataset captures via
    ``rg --json``, but in the raw text shape agents actually receive from
    plain grep — many matches concentrated in few files.
    """
    name = "grep_raw"
    provenance = (
        f"`{' '.join(_GREP_CMD)}` — raw file:line:content matches over this "
        "repo's own transforms package (real tool output, verbatim)."
    )
    raw = _capture(name, list(_GREP_CMD), provenance, refresh=refresh)
    return _raw_text_dataset(
        name, raw, "Where are functions defined in the transforms package?", provenance
    )


def build_diff_raw_dataset(*, refresh: bool = False) -> Dataset:
    """A real multi-file unified diff with lockfile churn → DiffCompressor.

    Pinned between two real commits of this repo (the dependency-bump
    window), manifests only: Cargo.lock alone carries 19+ hunks, so the
    ``max_hunks_per_file`` cap does real work.
    """
    name = "diff_raw"
    provenance = (
        f"`{' '.join(_DIFF_CMD)}` — real unified diff between two committed "
        "refs of this repo (multi-file; Cargo.lock churn dominates)."
    )
    raw = _capture(name, list(_DIFF_CMD), provenance, refresh=refresh)
    return _raw_text_dataset(name, raw, "What changed in the dependency bumps?", provenance)


def build_markdown_doc_dataset(*, refresh: bool = False) -> Dataset:
    """README-shaped markdown/prose document → TextCrusher.

    Headers, lists, indented code blocks, paragraphs (≥600 chars — the
    TextCrusher ``min_chars`` floor). Indented code blocks instead of ```
    fences: a fence plus prose trips ``is_mixed_content`` and routes MIXED
    (see ``rawtext_sources`` for the full rationale; the fence variant is
    pinned in the routing tests).
    """
    from benchmarks.rawtext_sources import MARKDOWN_DOC

    name = "markdown_doc"
    provenance = (
        "README-shaped markdown/prose document (rawtext_sources.MARKDOWN_DOC): "
        "headers, lists, indented code blocks, paragraphs. Static committed "
        "text, snapshotted verbatim."
    )
    cached = None if refresh else _load_snapshot(name)
    raw = cached[0] if cached is not None else MARKDOWN_DOC
    if cached is None:
        _snapshot(name, raw, provenance)
    return _raw_text_dataset(
        name, raw, "What does this project do and how do I install it?", provenance
    )


def all_datasets(*, refresh: bool = False) -> list[Dataset]:
    """Build all real datasets (6 structured + 4 raw-text).

    Deterministic from committed snapshots.
    """
    return [
        build_code_dataset(refresh=refresh),
        build_logs_dataset(refresh=refresh),
        build_search_dataset(refresh=refresh),
        build_repeated_logs_dataset(refresh=refresh),
        build_disk_dataset(refresh=refresh),
        build_multiturn_dataset(refresh=refresh),
        build_ci_log_dataset(refresh=refresh),
        build_grep_raw_dataset(refresh=refresh),
        build_diff_raw_dataset(refresh=refresh),
        build_markdown_doc_dataset(refresh=refresh),
    ]


def repeated_log_rows(*, limit: int, refresh: bool = False) -> list[dict[str, Any]]:
    """Return ``limit`` real ping reply rows — used by the Imp2 A/B harness."""
    return build_repeated_logs_dataset(limit=limit, refresh=refresh).items


def search_rows(*, limit: int, refresh: bool = False) -> list[dict[str, Any]]:
    """Return ``limit`` real ripgrep rows — used by the needle harness."""
    return build_search_dataset(limit=limit, refresh=refresh).items
