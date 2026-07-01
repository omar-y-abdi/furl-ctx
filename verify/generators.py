"""Independent, out-of-sample data generators for the verification harness.

These generators DO NOT import, re-run, or reuse anything under ``benchmarks/``.
They seed exclusively from REAL external/local-real data captured under
``verify/data/`` (see ``verify/SOURCES.md``) and then produce *out-of-sample
variations*: rows shaped like the real data but with fresh, per-row-unique
fields (timestamps, ids, uuids, hashes) so no row is artificially clean.

Three entropy tiers per type:

* ``low``    — highly repetitive: small alphabet of values, dittos likely,
               most columns constant or low-cardinality.
* ``medium`` — moderate variety: some repeated structure, but per-row unique
               timestamps/ids; the realistic middle.
* ``high``   — near-unique rows: every row carries a fresh uuid + random hash
               + unique message, defeating dedup/dictionary folds.

Every generator is a pure function of ``(seed, n)`` so a third party can
reproduce byte-for-byte. Randomness is confined to a per-call ``random.Random``
instance — no global state, no time-of-day dependence.

The unit of "distinct item" returned alongside the payload is the list of row
dicts (for structured types) or the list of source strings (for code); the
measurement core hashes and presence-tests against exactly these items.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DATA_DIR = Path(__file__).resolve().parent / "data"
HELDOUT_DATA_DIR = Path(__file__).resolve().parent / "heldout" / "data"

# Entropy tiers.
TIERS = ("low", "medium", "high")
# Two sizes per type (the spec asks for 2+; 90 mirrors the dev claim's
# cardinality, 900 is the 10x out-of-distribution stress size).
SIZES = (90, 900)


@dataclass(frozen=True)
class Case:
    """One generated case ready to feed the measurement core.

    Attributes:
        family: "logs" | "search" | "code" | "multiturn" | "repeated_logs"
                | "disk" — the dev-claim family this case probes.
        tier: entropy tier ("low"/"medium"/"high").
        size: nominal item count.
        seed: RNG seed (re-runnable).
        query: user query string (drives CCR retrieval feedback + relevance).
        items: the distinct input items (row dicts, or source strings).
        messages: the message payload handed to ``compress()``.
        conversation: True when this is a multi-turn payload (per-message
                      scoring + cache-prefix safety apply).
        needle_indices: indices into ``items`` that are planted unique needles.
        cache_prefix_len: for multiturn — number of leading messages that
                          carry the cached prefix (must not be dropped/reordered).
    """

    family: str
    tier: str
    size: int
    seed: int
    query: str
    items: list[Any]
    messages: list[dict[str, Any]]
    conversation: bool = False
    needle_indices: tuple[int, ...] = ()
    cache_prefix_len: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Seed material loaded ONCE from the real captures. We extract vocabulary and
# shapes, then SYNTHESIZE fresh rows — we never replay the real rows verbatim
# as the test payload (that would not be out-of-sample).
# ---------------------------------------------------------------------------


def _load_text(name: str) -> str:
    return (DATA_DIR / name).read_text(encoding="utf-8", errors="replace")


def _load_json(name: str) -> Any:
    return json.loads(_load_text(name))


def _real_authors() -> list[tuple[str, str]]:
    """(name, email) pairs harvested from the real git log / GitHub API."""
    pairs: set[tuple[str, str]] = set()
    for line in _load_text("slugify_gitlog.raw.txt").splitlines():
        parts = line.split("\x1f")
        if len(parts) >= 5:
            pairs.add((parts[1], parts[2]))
    return sorted(pairs) or [("Real Author", "real@example.com")]


def _real_commit_subjects() -> list[str]:
    subs = [
        line.split("\x1f")[4]
        for line in _load_text("slugify_gitlog.raw.txt").splitlines()
        if len(line.split("\x1f")) >= 5
    ]
    return subs or ["Initial commit"]


def _load_heldout_text(name: str) -> str:
    return (HELDOUT_DATA_DIR / name).read_text(encoding="utf-8", errors="replace")


def _real_paths() -> list[str]:
    """Real file paths harvested from the ripgrep JSON capture.

    Uses express_rg.raw.jsonl (verify/heldout/data/) — the committed rg capture.
    slugify_rg.raw.jsonl (verify/data/) is excluded by .gitignore (*.jsonl) and
    was never committed; express_rg shares the identical rg JSONL schema.
    """
    paths: set[str] = set()
    for line in _load_heldout_text("express_rg.raw.jsonl").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "match":
            p = o.get("data", {}).get("path", {}).get("text")
            if p:
                paths.add(p)
    return sorted(paths) or ["./src/index.js"]


def _real_match_lines() -> list[str]:
    """Real match-line text harvested from the ripgrep JSON capture.

    Uses express_rg.raw.jsonl (verify/heldout/data/) — see _real_paths docstring.
    """
    lines: list[str] = []
    for line in _load_heldout_text("express_rg.raw.jsonl").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "match":
            t = o.get("data", {}).get("lines", {}).get("text")
            if t:
                lines.append(t.rstrip("\n"))
    return lines or ["const x = 1;"]


def _real_log_levels() -> list[str]:
    return ["INFO", "DEBUG", "WARN", "ERROR", "TRACE", "NOTICE"]


# ---------------------------------------------------------------------------
# Primitive fresh-field generators (per-row unique — no artificially clean rows)
# ---------------------------------------------------------------------------


def _fresh_hash(rng: random.Random, n: int = 40) -> str:
    return hashlib.sha1(rng.randbytes(20)).hexdigest()[:n]


def _fresh_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128)))


def _iso(ts: datetime) -> str:
    # Strict-shape ISO-8601 with explicit offset (exercises the ISO-delta fold).
    return ts.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


def _iso_z(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# LOGS — git-log-shaped rows (the dev claim: logs@90 93.0%, repeated_logs@90 97.1%)
# ---------------------------------------------------------------------------


def gen_logs(seed: int, n: int, tier: str, *, repeated: bool = False) -> Case:
    """Structured log rows shaped like git-log / app logs.

    low    : a tiny set of repeated messages, one author, steady 1s cadence,
             constant level — maximally foldable (ditto/arith/const/iso-delta).
    medium : a few authors, ~dozen message templates, jittered cadence,
             per-row commit hash + monotonic id.
    high   : every row a fresh uuid + fresh 40-hex hash + unique message body,
             random level, random author — defeats dedup.
    repeated: forces the low-entropy "repeated_logs" shape regardless of tier
              base (used to probe the repeated_logs@90 97.1% claim).
    """
    rng = random.Random(seed)
    authors = _real_authors()
    subjects = _real_commit_subjects()
    levels = _real_log_levels()
    base = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=rng.randint(0, 10_000_000)
    )

    eff_tier = "low" if repeated else tier
    items: list[dict[str, Any]] = []
    for i in range(n):
        if eff_tier == "low":
            author = authors[0]
            msg = subjects[i % min(3, len(subjects))]
            level = "INFO"
            ts = base + timedelta(seconds=i)  # exact 1s progression
            commit = "0" * 40  # constant
            service = "api"
        elif eff_tier == "medium":
            author = authors[i % len(authors)]
            msg = subjects[i % len(subjects)]
            level = levels[i % 3]
            ts = base + timedelta(seconds=i * rng.randint(1, 5))
            commit = _fresh_hash(rng)
            service = ["api", "worker", "scheduler"][i % 3]
        else:  # high
            author = (f"user-{_fresh_hash(rng, 8)}", f"{_fresh_hash(rng, 8)}@ex.com")
            msg = f"{rng.choice(subjects)} {_fresh_uuid(rng)}"
            level = rng.choice(levels)
            ts = base + timedelta(seconds=rng.randint(0, 5_000_000))
            commit = _fresh_hash(rng)
            service = _fresh_hash(rng, 6)
        items.append(
            {
                "id": i,
                "ts": _iso(ts),
                "level": level,
                "service": service,
                "author": author[0],
                "email": author[1],
                "commit": commit,
                "message": msg,
            }
        )

    query = "Find ERROR and WARN log entries and summarize failures"
    payload = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": payload},
    ]
    fam = "repeated_logs" if repeated else "logs"
    return Case(
        family=fam,
        tier=eff_tier,
        size=n,
        seed=seed,
        query=query,
        items=items,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# SEARCH — ripgrep --json-shaped rows (dev claim: search@90 92.7%)
# ---------------------------------------------------------------------------


def gen_search(seed: int, n: int, tier: str) -> Case:
    """ripgrep-style match rows.

    low    : few distinct paths, repeated identical match lines, line numbers
             in a clean progression.
    medium : real paths cycled, real match lines cycled, jittered line numbers.
    high   : per-row unique synthetic path + unique match text + random offsets.
    """
    rng = random.Random(seed)
    paths = _real_paths()
    match_lines = _real_match_lines()

    items: list[dict[str, Any]] = []
    for i in range(n):
        if tier == "low":
            path = paths[0]
            text = match_lines[i % min(2, len(match_lines))]
            line_no = i + 1  # clean progression
            col = 0
        elif tier == "medium":
            path = paths[i % len(paths)]
            text = match_lines[i % len(match_lines)]
            line_no = (i + 1) * rng.randint(1, 4)
            col = rng.randint(0, 12)
        else:  # high
            path = f"./src/{_fresh_hash(rng, 10)}/mod_{i}.ts"
            text = f"const v_{_fresh_hash(rng, 8)} = {_fresh_uuid(rng)!r};"
            line_no = rng.randint(1, 9999)
            col = rng.randint(0, 80)
        items.append(
            {
                "path": path,
                "line_number": line_no,
                "column": col,
                "match": text,
            }
        )

    query = "Where is the slug normalization implemented?"
    payload = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": payload},
    ]
    return Case(
        family="search",
        tier=tier,
        size=n,
        seed=seed,
        query=query,
        items=items,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CODE — real source files (dev claim: code@7 0% — passthrough expected)
# ---------------------------------------------------------------------------


def gen_code(seed: int, n: int, tier: str) -> Case:
    """Source-file payloads built from REAL cloned-repo source.

    The dev claim is code@7 = 0% reduction (passthrough). We probe whether
    that holds out-of-sample by assembling N distinct source blobs:

    low    : the SAME real file repeated N times (a compressor *could* dedup).
    medium : real files lightly perturbed per copy (unique header comment).
    high   : real files heavily perturbed (unique uuid-bearing comments
             scattered through) so each blob is near-unique.
    """
    rng = random.Random(seed)
    real_src = [
        _load_text("slugify_index.js"),
        _load_text("isplainobj_index.js"),
    ]

    items: list[str] = []
    for i in range(n):
        src = real_src[i % len(real_src)]
        if tier == "low":
            blob = src
        elif tier == "medium":
            blob = f"// file copy {i} {_fresh_hash(rng, 8)}\n" + src
        else:  # high
            lines = src.split("\n")
            out: list[str] = []
            for ln in lines:
                out.append(ln)
                if rng.random() < 0.3:
                    out.append(f"  // note {_fresh_uuid(rng)}")
            blob = "\n".join(out)
        items.append(blob)

    query = "Review this code for correctness"
    # Each source blob is its own tool message (mirrors a coding agent that
    # read N files). Default config skips user messages; tool/assistant
    # content is the compression target.
    messages = [{"role": "user", "content": query}]
    for i, blob in enumerate(items):
        messages.append({"role": "tool", "content": blob})
    return Case(
        family="code",
        tier=tier,
        size=n,
        seed=seed,
        query=query,
        items=items,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# MULTITURN — conversation with a cached prefix (dev claim: multiturn@135 70.8%)
# ---------------------------------------------------------------------------


def gen_multiturn(seed: int, n: int, tier: str) -> Case:
    """Multi-turn conversation; index-0 system block bears the cache prefix.

    The cached prefix (a stable system message + a couple of leading turns)
    MUST NOT be dropped or reordered. Each later turn carries a tool result
    of structured rows whose entropy follows the tier. ``items`` are the
    distinct tool-result rows across the whole conversation; ``messages`` is
    the full transcript.
    """
    rng = random.Random(seed)
    authors = _real_authors()
    subjects = _real_commit_subjects()
    base = datetime(2025, 3, 1, tzinfo=timezone.utc)

    # Stable cached prefix: a long system instruction + 1 leading user/assistant
    # pair. These leading messages are the prompt-cache-bearing prefix.
    system_text = (
        "You are a senior site-reliability engineer. Follow these standing "
        "rules for every turn: cite log ids, never fabricate timestamps, and "
        "always surface ERROR rows first. " + ("Context. " * 60)
    )
    cache_prefix_msgs = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "Here is the standing runbook; keep it in mind."},
        {"role": "assistant", "content": "Understood. Runbook cached; ready."},
    ]
    cache_prefix_len = len(cache_prefix_msgs)

    # Distribute n rows across turns.
    n_turns = 6
    per_turn = max(1, n // n_turns)
    items: list[dict[str, Any]] = []
    turn_msgs: list[dict[str, Any]] = []
    row_id = 0
    for t in range(n_turns):
        turn_msgs.append(
            {"role": "user", "content": f"Turn {t}: analyze the latest batch."}
        )
        rows: list[dict[str, Any]] = []
        for _ in range(per_turn):
            if tier == "low":
                row = {
                    "id": row_id,
                    "ts": _iso(base + timedelta(seconds=row_id)),
                    "level": "INFO",
                    "msg": subjects[row_id % min(3, len(subjects))],
                    "who": authors[0][0],
                }
            elif tier == "medium":
                row = {
                    "id": row_id,
                    "ts": _iso(base + timedelta(seconds=row_id * rng.randint(1, 4))),
                    "level": ["INFO", "WARN", "ERROR"][row_id % 3],
                    "msg": subjects[row_id % len(subjects)],
                    "who": authors[row_id % len(authors)][0],
                    "trace": _fresh_hash(rng, 12),
                }
            else:  # high
                row = {
                    "id": row_id,
                    "ts": _iso(base + timedelta(seconds=rng.randint(0, 9_000_000))),
                    "level": rng.choice(_real_log_levels()),
                    "msg": f"{rng.choice(subjects)} {_fresh_uuid(rng)}",
                    "who": f"u-{_fresh_hash(rng, 6)}",
                    "trace": _fresh_hash(rng, 40),
                }
            rows.append(row)
            items.append(row)
            row_id += 1
        turn_msgs.append(
            {"role": "tool", "content": json.dumps(rows, ensure_ascii=False)}
        )
        turn_msgs.append(
            {"role": "assistant", "content": f"Analyzed turn {t}; {len(rows)} rows."}
        )

    messages = cache_prefix_msgs + turn_msgs
    query = "Summarize all ERROR rows across the whole conversation"
    return Case(
        family="multiturn",
        tier=tier,
        size=len(items),
        seed=seed,
        query=query,
        items=items,
        messages=messages,
        conversation=True,
        cache_prefix_len=cache_prefix_len,
        meta={"cache_prefix_texts": [m["content"] for m in cache_prefix_msgs]},
    )


# ---------------------------------------------------------------------------
# DISK — large structured "ls -la / du"-shaped rows (dev claim: disk@9 50% lossless)
# ---------------------------------------------------------------------------


def gen_disk(seed: int, n: int, tier: str) -> Case:
    """Filesystem-listing-shaped rows seeded from a REAL lockfile's deps.

    We harvest real dependency names from a real package-lock.json (a
    DIFFERENT project, local-real) and synthesize directory-entry rows with
    sizes, perms, mtimes — the shape `disk` compression targets.
    """
    rng = random.Random(seed)
    lock = _load_json("threejs_devtools_package-lock.json")
    pkgs = list((lock.get("packages") or {}).keys()) or ["node_modules/x"]
    real_names = [p for p in pkgs if p][:max(50, n)] or ["node_modules/x"]

    perms_pool = ["drwxr-xr-x", "-rw-r--r--", "-rwxr-xr-x", "lrwxr-xr-x"]
    base = datetime(2025, 2, 1, tzinfo=timezone.utc)
    items: list[dict[str, Any]] = []
    for i in range(n):
        name = real_names[i % len(real_names)]
        if tier == "low":
            row = {
                "perms": "drwxr-xr-x",
                "links": 2,
                "owner": "k",
                "group": "staff",
                "size": 4096,
                "mtime": _iso_z(base + timedelta(minutes=i)),
                "name": name,
            }
        elif tier == "medium":
            row = {
                "perms": perms_pool[i % len(perms_pool)],
                "links": rng.randint(1, 4),
                "owner": "k",
                "group": "staff",
                "size": rng.randint(0, 50000),
                "mtime": _iso_z(base + timedelta(minutes=i * rng.randint(1, 3))),
                "name": name,
            }
        else:  # high
            row = {
                "perms": rng.choice(perms_pool),
                "links": rng.randint(1, 64),
                "owner": f"u{rng.randint(0,999)}",
                "group": _fresh_hash(rng, 6),
                "size": rng.randint(0, 9_000_000),
                "mtime": _iso_z(base + timedelta(seconds=rng.randint(0, 9_000_000))),
                "name": f"{name}/{_fresh_hash(rng, 8)}",
                "inode": _fresh_uuid(rng),
            }
        items.append(row)

    query = "Which directories are largest on disk?"
    payload = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": payload},
    ]
    return Case(
        family="disk",
        tier=tier,
        size=n,
        seed=seed,
        query=query,
        items=items,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# Needle planting — inject K KNOWN-UNIQUE rows the model would need to retrieve.
# ---------------------------------------------------------------------------


def plant_needles(
    case: Case, seed: int, k: int = 3
) -> Case:
    """Insert K unique 'needle' rows at start / middle / end of the items.

    Needles are maximally distinctive (a magic marker + fresh uuid) so a
    correct retrieval is unambiguous. We re-serialize the payload after
    insertion. Only applies to single-tool structured families.
    """
    if case.conversation or case.family == "code":
        return case
    rng = random.Random((seed * 2654435761) & 0xFFFFFFFF)
    items = list(case.items)
    n = len(items)
    positions = [0, n // 2, max(0, n - 1)][:k]
    needle_template = dict(items[0]) if items and isinstance(items[0], dict) else {}
    needles: list[dict[str, Any]] = []
    for p_i, _pos in enumerate(positions):
        nd = dict(needle_template)
        marker = f"NEEDLE-{_fresh_uuid(rng)}"
        # Stamp a unique marker into the most text-like field.
        for fld in ("message", "match", "msg", "name"):
            if fld in nd:
                nd[fld] = marker
                break
        else:
            nd["needle"] = marker
        # Make every other field unique too so it cannot fold away.
        if "id" in nd:
            nd["id"] = 10_000_000 + p_i
        if "commit" in nd:
            nd["commit"] = _fresh_hash(rng)
        if "trace" in nd:
            nd["trace"] = _fresh_hash(rng)
        needles.append(nd)

    # Insert needles at their positions (descending so indices stay valid).
    needle_indices: list[int] = []
    for nd, pos in sorted(zip(needles, positions), key=lambda x: -x[1]):
        items.insert(pos, nd)
    # Recompute indices after all insertions by identity.
    id_set = {id(nd) for nd in needles}
    needle_indices = [i for i, it in enumerate(items) if id(it) in id_set]

    payload = json.dumps(items, ensure_ascii=False)
    messages = [
        {"role": case.messages[0]["role"], "content": case.messages[0]["content"]},
        {"role": "tool", "content": payload},
    ]
    return Case(
        family=case.family,
        tier=case.tier,
        size=len(items),
        seed=case.seed,
        query=case.query,
        items=items,
        messages=messages,
        conversation=False,
        needle_indices=tuple(needle_indices),
        meta={"needle_markers": [nd for nd in needles]},
    )


# Registry of generator builders keyed by family.
GENERATORS: dict[str, Callable[[int, int, str], Case]] = {
    "logs": gen_logs,
    "search": gen_search,
    "code": gen_code,
    "multiturn": gen_multiturn,
    "disk": gen_disk,
}
