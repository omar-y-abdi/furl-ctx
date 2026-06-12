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

Raw captures are snapshotted under ``benchmarks/data/`` so a run is
reproducible and the numbers are auditable. The capture commands are
recorded in each snapshot's ``provenance`` field.

Each dataset builder returns a :class:`Dataset`: the chat-style ``messages``
payload to feed ``compress()`` plus the raw ``items`` (list of distinct
objects) used for retention / drop scoring.
"""

from __future__ import annotations

import json
import subprocess
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
    """

    name: str
    query: str
    items: list[Any]
    messages: list[dict[str, Any]]
    provenance: str


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
            f"capture command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stderr[:500]}"
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
    "headroom/compress.py",
    "headroom/tokenizer.py",
    "headroom/config.py",
    "headroom/transforms/smart_crusher.py",
    "headroom/transforms/search_compressor.py",
    "headroom/transforms/log_compressor.py",
    "headroom/cache/compression_store.py",
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
_RG_TARGET = "headroom/"


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


def all_datasets(*, refresh: bool = False) -> list[Dataset]:
    """Build the three real datasets. Deterministic from committed snapshots."""
    return [
        build_code_dataset(refresh=refresh),
        build_logs_dataset(refresh=refresh),
        build_search_dataset(refresh=refresh),
    ]


def search_rows(*, limit: int, refresh: bool = False) -> list[dict[str, Any]]:
    """Return ``limit`` real ripgrep rows — used by the needle harness."""
    return build_search_dataset(limit=limit, refresh=refresh).items
