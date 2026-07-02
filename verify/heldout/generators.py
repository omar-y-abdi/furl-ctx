"""Held-out (SECOND-run) out-of-sample data generators.

DISJOINT from the first run under ``verify/``: these seed exclusively from
DIFFERENT real external captures under ``verify/heldout/data/`` (see
``verify/heldout/SOURCES.md``) — expressjs/express, chalk/chalk, the GitHub
commits API for express, the npm registry for express, the npm/cli
package-lock.json, and a fresh local-real macOS install log. They DO NOT
import, re-run, or reuse anything under ``benchmarks/`` or ``verify/``'s
generators/data.

Three entropy tiers per type:

* ``low``    — highly repetitive: tiny value alphabet, dittos likely, most
               columns constant/low-cardinality.
* ``medium`` — moderate variety: per-row unique timestamps/ids over a repeated
               structural skeleton — the realistic middle.
* ``high``   — near-unique rows: every row carries a fresh uuid + random sha +
               unique message, defeating dedup/dictionary folds.

Plus two SPECIAL tiers used ONLY to probe the round-3 affix/head-dict claims:

* ``struct`` — STRUCTURED near-unique: per-row values are unique BUT share a
               path root / url root / module-name head (so an affix or
               head-dictionary fold SHOULD fire and yield real, LOSSLESS gain).
* ``genuine``— GENUINE-entropy near-unique: per-row values are random uuid/sha
               with NO shared structure (so a correct engine must DECLINE to
               affix/head-encode — any "gain" here would be fake).

Every generator is a pure function of ``(seed, n)``; randomness is confined to
a per-call ``random.Random`` so a third party reproduces byte-for-byte. No
time-of-day dependence, no global state. ``items`` is the list of distinct
input items (row dicts, or source strings) the measurement core hashes against.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"

# Entropy tiers swept by the runner. ``struct`` and ``genuine`` are the
# round-3 affix/head-dict probes (structured-fold-should-fire vs
# genuine-entropy-must-decline).
TIERS = ("low", "medium", "high", "struct", "genuine")
# Two sizes per type (the spec asks for 2+). 90 mirrors the dev-claim
# cardinality; 900 is the 10x out-of-distribution stress size.
SIZES = (90, 900)


@dataclass(frozen=True)
class Case:
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
# Real seed material loaded ONCE from the held-out captures. We harvest
# vocabulary + row shapes and then SYNTHESIZE fresh out-of-sample rows; we never
# replay a real row verbatim as the test payload.
# ---------------------------------------------------------------------------


def _load_text(name: str) -> str:
    return (DATA_DIR / name).read_text(encoding="utf-8", errors="replace")


def _load_json(name: str) -> Any:
    return json.loads(_load_text(name))


def _real_authors() -> list[tuple[str, str]]:
    """(name, email) pairs harvested from the real express git log."""
    pairs: set[tuple[str, str]] = set()
    for line in _load_text("express_gitlog.raw.txt").splitlines():
        parts = line.split("\x1f")
        if len(parts) >= 5:
            pairs.add((parts[1], parts[2]))
    return sorted(pairs) or [("Real Author", "real@example.com")]


def _real_commit_subjects() -> list[str]:
    subs = [
        line.split("\x1f")[4]
        for line in _load_text("express_gitlog.raw.txt").splitlines()
        if len(line.split("\x1f")) >= 5
    ]
    return subs or ["Initial commit"]


def _real_commit_hashes() -> list[str]:
    hs = [
        line.split("\x1f")[0]
        for line in _load_text("express_gitlog.raw.txt").splitlines()
        if len(line.split("\x1f")) >= 5 and len(line.split("\x1f")[0]) == 40
    ]
    return hs or ["0" * 40]


def _real_paths() -> list[str]:
    """Real file paths harvested from the express ripgrep JSON capture."""
    paths: set[str] = set()
    for line in _load_text("express_rg.raw.jsonl").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "match":
            p = o.get("data", {}).get("path", {}).get("text")
            if p:
                paths.add(p)
    return sorted(paths) or ["lib/express.js"]


def _real_match_lines() -> list[str]:
    lines: list[str] = []
    for line in _load_text("express_rg.raw.jsonl").splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "match":
            t = o.get("data", {}).get("lines", {}).get("text")
            if t:
                lines.append(t.rstrip("\n"))
    return lines or ["const x = 1;"]


def _real_dep_names() -> list[str]:
    """Real dependency keys from the npm/cli package-lock (node_modules/...)."""
    lock = _load_json("npm_cli_package-lock.json")
    pkgs = list((lock.get("packages") or {}).keys())
    names = [p for p in pkgs if p]
    return names or ["node_modules/x"]


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
    return ts.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


def _iso_z(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# A small fixed pool of shared path/url/module roots used by the ``struct``
# tier so that an affix/head-dict fold has a genuine shared structure to find.
_SHARED_PATH_ROOTS = (
    "lib/middleware/",
    "lib/router/",
    "test/support/",
    "lib/utils/",
)
_SHARED_URL_ROOTS = (
    "https://registry.npmjs.org/express/-/",
    "https://registry.npmjs.org/chalk/-/",
)
_SHARED_MODULE_ROOTS = (
    "node_modules/@expressjs/",
    "node_modules/@types/",
)


# ---------------------------------------------------------------------------
# LOGS — git-log / app-log-shaped rows (probes logs@90 93.0%, repeated_logs@90
# 97.1%, and the round-3 logs@90 high 80.2% -> 82.4% claim).
# ---------------------------------------------------------------------------


def gen_logs(seed: int, n: int, tier: str, *, repeated: bool = False) -> Case:
    rng = random.Random(seed)
    authors = _real_authors()
    subjects = _real_commit_subjects()
    real_hashes = _real_commit_hashes()
    levels = _real_log_levels()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randint(0, 10_000_000))

    eff_tier = "low" if repeated else tier
    items: list[dict[str, Any]] = []
    for i in range(n):
        if eff_tier == "low":
            author = authors[0]
            msg = subjects[i % min(3, len(subjects))]
            level = "INFO"
            ts = base + timedelta(seconds=i)
            commit = "0" * 40
            service = "api"
        elif eff_tier == "medium":
            author = authors[i % len(authors)]
            msg = subjects[i % len(subjects)]
            level = levels[i % 3]
            ts = base + timedelta(seconds=i * rng.randint(1, 5))
            commit = _fresh_hash(rng)
            service = ["api", "worker", "scheduler"][i % 3]
        elif eff_tier == "struct":
            # Near-unique but STRUCTURED: message shares a path-root head, the
            # request id shares a fixed prefix; only the tail varies.
            root = _SHARED_PATH_ROOTS[i % len(_SHARED_PATH_ROOTS)]
            author = authors[i % len(authors)]
            msg = f"loaded {root}mod_{_fresh_hash(rng, 10)}.js"
            level = levels[i % 3]
            ts = base + timedelta(seconds=i * 2)
            commit = f"req-{i:08d}-{_fresh_hash(rng, 6)}"  # shared 'req-' prefix
            service = "api"
        elif eff_tier == "genuine":
            # Near-unique GENUINE entropy: random uuid + random sha, NO shared
            # structure — affix/head folds must DECLINE here.
            author = (f"user-{_fresh_hash(rng, 8)}", f"{_fresh_hash(rng, 8)}@ex.com")
            msg = _fresh_uuid(rng)
            level = rng.choice(levels)
            ts = base + timedelta(seconds=rng.randint(0, 5_000_000))
            commit = _fresh_hash(rng)
            service = _fresh_hash(rng, 6)
        else:  # high
            author = (f"user-{_fresh_hash(rng, 8)}", f"{_fresh_hash(rng, 8)}@ex.com")
            msg = f"{rng.choice(subjects)} {_fresh_uuid(rng)}"
            level = rng.choice(levels)
            ts = base + timedelta(seconds=rng.randint(0, 5_000_000))
            commit = rng.choice(real_hashes) if rng.random() < 0.3 else _fresh_hash(rng)
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
# SEARCH — ripgrep --json-shaped rows (probes search@90 92.7% and the round-3
# search@90 high 36.1%-erratic -> 93.6%-reliable claim, plus the structured-
# fold vs genuine-entropy distinction).
# ---------------------------------------------------------------------------


def gen_search(seed: int, n: int, tier: str) -> Case:
    rng = random.Random(seed)
    paths = _real_paths()
    match_lines = _real_match_lines()

    items: list[dict[str, Any]] = []
    for i in range(n):
        if tier == "low":
            path = paths[0]
            text = match_lines[i % min(2, len(match_lines))]
            line_no = i + 1
            col = 0
        elif tier == "medium":
            path = paths[i % len(paths)]
            text = match_lines[i % len(match_lines)]
            line_no = (i + 1) * rng.randint(1, 4)
            col = rng.randint(0, 12)
        elif tier == "struct":
            # STRUCTURED near-unique: every path shares one of a few real path
            # roots; only the file tail is unique -> head-dict / affix SHOULD
            # fire and give real lossless gain.
            root = _SHARED_PATH_ROOTS[i % len(_SHARED_PATH_ROOTS)]
            path = f"{root}{_fresh_hash(rng, 8)}.js"
            text = f"  const handler_{_fresh_hash(rng, 6)} = require('{root}base');"
            line_no = rng.randint(1, 400)
            col = rng.randint(0, 40)
        elif tier == "genuine":
            # GENUINE entropy: random path with NO shared root, uuid match text.
            path = f"{_fresh_hash(rng, 12)}/{_fresh_hash(rng, 12)}.ts"
            text = f"const v_{_fresh_hash(rng, 8)} = {_fresh_uuid(rng)!r};"
            line_no = rng.randint(1, 9999)
            col = rng.randint(0, 80)
        else:  # high
            path = f"src/{_fresh_hash(rng, 10)}/mod_{i}.ts"
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

    query = "Where is the express router middleware dispatch implemented?"
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
# CODE — real source files (probes code@7 0% passthrough out-of-sample).
# ---------------------------------------------------------------------------


def gen_code(seed: int, n: int, tier: str) -> Case:
    rng = random.Random(seed)
    real_src = [
        _load_text("express_application.js"),
        _load_text("chalk_index.js"),
    ]

    items: list[str] = []
    for i in range(n):
        src = real_src[i % len(real_src)]
        if tier == "low":
            blob = src
        elif tier in ("medium", "struct"):
            blob = f"// file copy {i} {_fresh_hash(rng, 8)}\n" + src
        else:  # high / genuine
            lines = src.split("\n")
            out: list[str] = []
            for ln in lines:
                out.append(ln)
                if rng.random() < 0.3:
                    out.append(f"  // note {_fresh_uuid(rng)}")
            blob = "\n".join(out)
        items.append(blob)

    query = "Review this code for correctness"
    messages = [{"role": "user", "content": query}]
    for blob in items:
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
# MULTITURN — conversation with a cached prefix (probes multiturn@135 70.8% and
# prompt-cache prefix safety).
# ---------------------------------------------------------------------------


def gen_multiturn(seed: int, n: int, tier: str) -> Case:
    rng = random.Random(seed)
    authors = _real_authors()
    subjects = _real_commit_subjects()
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)

    system_text = (
        "You are a senior platform engineer reviewing express middleware "
        "deploys. Standing rules every turn: cite request ids, never fabricate "
        "timestamps, surface ERROR rows first. " + ("Context. " * 60)
    )
    cache_prefix_msgs = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "Here is the standing runbook; keep it in mind."},
        {"role": "assistant", "content": "Understood. Runbook cached; ready."},
    ]
    cache_prefix_len = len(cache_prefix_msgs)

    n_turns = 6
    per_turn = max(1, n // n_turns)
    items: list[dict[str, Any]] = []
    turn_msgs: list[dict[str, Any]] = []
    row_id = 0
    for t in range(n_turns):
        turn_msgs.append({"role": "user", "content": f"Turn {t}: analyze the latest batch."})
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
            elif tier == "struct":
                root = _SHARED_PATH_ROOTS[row_id % len(_SHARED_PATH_ROOTS)]
                row = {
                    "id": row_id,
                    "ts": _iso(base + timedelta(seconds=row_id * 2)),
                    "level": ["INFO", "WARN", "ERROR"][row_id % 3],
                    "msg": f"served {root}{_fresh_hash(rng, 8)}",
                    "who": authors[row_id % len(authors)][0],
                    "trace": f"req-{row_id:08d}",
                }
            elif tier == "genuine":
                row = {
                    "id": row_id,
                    "ts": _iso(base + timedelta(seconds=rng.randint(0, 9_000_000))),
                    "level": rng.choice(_real_log_levels()),
                    "msg": _fresh_uuid(rng),
                    "who": f"u-{_fresh_hash(rng, 6)}",
                    "trace": _fresh_hash(rng, 40),
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
        turn_msgs.append({"role": "tool", "content": json.dumps(rows, ensure_ascii=False)})
        turn_msgs.append({"role": "assistant", "content": f"Analyzed turn {t}; {len(rows)} rows."})

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
# DISK — filesystem-listing-shaped rows seeded from the npm/cli lockfile deps
# (probes disk@9 50% lossless and the affix/head fold on module-name columns).
# ---------------------------------------------------------------------------


def gen_disk(seed: int, n: int, tier: str) -> Case:
    rng = random.Random(seed)
    real_names = _real_dep_names()
    perms_pool = ["drwxr-xr-x", "-rw-r--r--", "-rwxr-xr-x", "lrwxr-xr-x"]
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
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
        elif tier == "struct":
            # STRUCTURED near-unique name: shares a module root, unique tail.
            root = _SHARED_MODULE_ROOTS[i % len(_SHARED_MODULE_ROOTS)]
            row = {
                "perms": perms_pool[i % len(perms_pool)],
                "links": rng.randint(1, 4),
                "owner": "k",
                "group": "staff",
                "size": rng.randint(0, 50000),
                "mtime": _iso_z(base + timedelta(minutes=i)),
                "name": f"{root}{_fresh_hash(rng, 8)}",
            }
        elif tier == "genuine":
            row = {
                "perms": rng.choice(perms_pool),
                "links": rng.randint(1, 64),
                "owner": f"u{rng.randint(0, 999)}",
                "group": _fresh_hash(rng, 6),
                "size": rng.randint(0, 9_000_000),
                "mtime": _iso_z(base + timedelta(seconds=rng.randint(0, 9_000_000))),
                "name": _fresh_uuid(rng),
                "inode": _fresh_uuid(rng),
            }
        else:  # high
            row = {
                "perms": rng.choice(perms_pool),
                "links": rng.randint(1, 64),
                "owner": f"u{rng.randint(0, 999)}",
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


def plant_needles(case: Case, seed: int, k: int = 3) -> Case:
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
        for fld in ("message", "match", "msg", "name"):
            if fld in nd:
                nd[fld] = marker
                break
        else:
            nd["needle"] = marker
        if "id" in nd:
            nd["id"] = 10_000_000 + p_i
        if "commit" in nd:
            nd["commit"] = _fresh_hash(rng)
        if "trace" in nd:
            nd["trace"] = _fresh_hash(rng)
        needles.append(nd)

    for nd, pos in sorted(zip(needles, positions), key=lambda x: -x[1]):
        items.insert(pos, nd)
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
        meta={"needle_markers": list(needles)},
    )


GENERATORS: dict[str, Callable[[int, int, str], Case]] = {
    "logs": gen_logs,
    "search": gen_search,
    "code": gen_code,
    "multiturn": gen_multiturn,
    "disk": gen_disk,
}
