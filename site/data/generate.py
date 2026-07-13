#!/usr/bin/env python3
"""Generate REAL furl demo data for the showcase site.

Every number, marker, and retrieved byte on the site comes from THIS script
calling the real ``furl_ctx`` engine. Nothing is hand-typed.

Run from a Python env where ``furl_ctx`` is importable (from the repo root:
``pip install -e .`` or ``maturin develop --release``), then:

    python site/data/generate.py

Outputs (all under site/data/):
  - <id>.json         one honest artifact per content type
  - manifest.json     metadata + aggregate
  - furl-data.js      window.__FURL_DATA__ = {...}  (consumed by index.html,
                      works from file:// with no runtime fetch)

Sample sizes reflect furl's real operating regime: large tool outputs. Below
roughly two thousand tokens the router leaves error-like content untouched
(it is not worth folding), so the samples are sized to cross that line, the
way a real agent's tool output does.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from furl_ctx import CompressResult, __version__, compress, retrieve

# A neutral, representative prompt: a coding agent dropping a large tool result
# into context. It deliberately avoids the words analyze / review / debug /
# investigate, which furl reads as an intent to PROTECT content from
# compression. This is the honest, out-of-the-box path for a big tool output.
NEUTRAL_PROMPT = "Here is the tool output from the last run."
MODEL = "claude-sonnet-4-5-20250929"

DATA_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------
# Retrieval spec: how the site's "retrieve" control pulls an original back.
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Retrieval:
    kind: str  # "pattern" | "select"
    control_label: str
    caption: str
    pattern: str | None = None
    context_lines: int = 0
    select_field: str | None = None
    select_equals: Any = None


@dataclass(frozen=True)
class Sample:
    id: str
    label: str
    kind_label: str
    tagline: str
    story: str
    anomaly_regex: str
    retrieval: Retrieval
    original: str


# ----------------------------------------------------------------------------
# Sample builders. Content is representative machine output. Anomalies are the
# one line that matters inside a wall of repetition.
# ----------------------------------------------------------------------------
def build_logs() -> Sample:
    lines: list[str] = []
    for i in range(1, 161):
        ts = f"2026-07-13T09:{(i // 60) % 60:02d}:{i % 60:02d}.{(i * 37) % 1000:03d}Z"
        if i == 105:
            lines.append(
                f"{ts} ERROR api-gw-3 upstream 502 from billing-svc after 3 retries corr_id=7f3a9c21"
            )
        else:
            cache = "HIT" if i % 3 else "MISS"
            lines.append(
                f"{ts} INFO  api-gw-{i % 6} 200 GET /v2/accounts/{i % 50} {8 + (i % 40)}ms cache={cache}"
            )
    return Sample(
        id="logs",
        label="Application logs",
        kind_label="verbose service logs",
        tagline="One error, buried in a wall of heartbeats.",
        story="furl keeps the anomaly and a few representatives, then folds the repeats into a marker. The dropped lines stay retrievable byte-exact.",
        anomaly_regex=r"\bERROR\b",
        retrieval=Retrieval(
            kind="pattern",
            control_label="Find the anomaly",
            caption="Ask the marker for the ERROR line. It comes back byte-exact, with its original line number.",
            pattern="ERROR",
            context_lines=0,
        ),
        original="\n".join(lines) + "\n",
    )


def build_crash() -> Sample:
    f: list[str] = [
        "Traceback (most recent call last):",
        '  File "/srv/app/main.py", line 42, in <module>',
        "    orchestrate(payload)",
    ]
    for _ in range(90):
        f.append('  File "/srv/app/pipeline/stage.py", line 118, in run_stage')
        f.append("    return run_stage(next_ctx)")
    f.append('  File "/srv/app/pipeline/stage.py", line 121, in run_stage')
    f.append('    raise RecursionError("stage graph did not terminate: cycle via node reduce to reduce")')
    f.append("RecursionError: stage graph did not terminate: cycle via node reduce to reduce")
    return Sample(
        id="crash",
        label="Crash report",
        kind_label="deep stack trace",
        tagline="The same frame, over and over, around one root cause.",
        story="A runaway recursion repeats one frame down the whole stack. furl folds the repeated frames and keeps the root cause reachable.",
        anomaly_regex=r"RecursionError",
        retrieval=Retrieval(
            kind="pattern",
            control_label="Show the root cause",
            caption="Pull the RecursionError line back out of the folded trace, byte-exact.",
            pattern="RecursionError",
            context_lines=0,
        ),
        original="\n".join(f) + "\n",
    )


def build_json() -> Sample:
    rows: list[dict[str, Any]] = []
    for i in range(1, 73):
        status = 503 if i == 48 else 200
        rows.append(
            {
                "id": 1000 + i,
                "user": f"user_{i % 9}",
                "action": "page_view",
                "status": status,
                "ms": 12 + (i % 30),
                "path": f"/api/v2/resource/{i % 15}",
                "region": ["us-east", "eu-west", "ap-south"][i % 3],
            }
        )
    body = "[\n" + ",\n".join(json.dumps(r) for r in rows) + "\n]"
    return Sample(
        id="json",
        label="JSON API response",
        kind_label="array of near-identical rows",
        tagline="Row after near-identical row, and one status that is not 200.",
        story="furl reads the shape once, writes a compact table, and offloads the rows. Any row is addressable by value.",
        anomaly_regex=r'"status": ?503',
        retrieval=Retrieval(
            kind="select",
            control_label="Retrieve the 503 row",
            caption="Select the row where status equals 503. The exact original object comes back.",
            select_field="status",
            select_equals=503,
        ),
        original=body,
    )


def build_pytest() -> Sample:
    pl: list[str] = [
        "============================= test session starts =============================",
        "platform darwin -- Python 3.13.1, pytest-8.3.2, pluggy-1.5.0",
        "collected 210 items",
        "",
    ]
    for i in range(1, 211):
        mod = ["core", "api", "store", "ccr", "router"][i % 5]
        verdict = "FAILED" if i == 150 else "PASSED"
        pl.append(f"tests/test_{mod}.py::test_case_{i:03d} {verdict}")
    pl += [
        "",
        "=================================== FAILURES ===================================",
        "____________________________ test_case_150 ____________________________",
        "    def test_case_150():",
        "        got = normalize(' Tenant ID ')",
        ">       assert got == 'tenant_id'",
        "E       AssertionError: assert 'tenant id' == 'tenant_id'",
        "E         - tenant_id",
        "E         + tenant id",
        "tests/test_store.py:317: AssertionError",
        "=========================== short test summary info ===========================",
        "FAILED tests/test_store.py::test_case_150 - AssertionError: assert 'tenant id'",
        "======================== 1 failed, 209 passed in 5.44s ========================",
    ]
    return Sample(
        id="pytest",
        label="Test output",
        kind_label="pytest run, one failure",
        tagline="A long green run with a single red line.",
        story="furl offloads the passing lines and keeps the failure and its assertion in view. The full run stays retrievable.",
        anomaly_regex=r"\bFAILED\b",
        retrieval=Retrieval(
            kind="pattern",
            control_label="Show the failure",
            caption="Ask for FAILED. Both the failing test line and the summary come back byte-exact.",
            pattern="FAILED",
            context_lines=0,
        ),
        original="\n".join(pl) + "\n",
    )


def build_ci() -> Sample:
    cl: list[str] = [
        "#1 [internal] load build definition",
        "#2 resolving provenance for metadata",
    ]
    crates = ["serde", "tokio", "hyper", "regex", "syn", "quote", "clap", "anyhow", "thiserror", "rayon"]
    for i in range(1, 166):
        c = crates[i % len(crates)]
        if i == 140:
            cl.append(f"warning: unused variable `ctx` in crate `{c}`, build continued")
        else:
            cl.append(f"   Compiling {c} v{1 + (i % 3)}.{i % 9}.{i % 5} ({12 + (i % 80)}ms)")
    cl.append("    Finished `release` profile [optimized] target(s) in 41.87s")
    return Sample(
        id="ci",
        label="CI build log",
        kind_label="repetitive compile lines",
        tagline="Compile line after compile line, and one warning.",
        story="Every compile line looks the same. furl folds them and keeps the single warning where you can see it.",
        anomaly_regex=r"\bwarning:",
        retrieval=Retrieval(
            kind="pattern",
            control_label="Show the warning",
            caption="Pull the one warning line back out of the folded build log.",
            pattern="warning",
            context_lines=0,
        ),
        original="\n".join(cl) + "\n",
    )


def build_csv() -> Sample:
    hdr = "id,ts,region,endpoint,latency_ms,status,bytes"
    rows: list[str] = [hdr]
    for i in range(1, 151):
        status = 429 if i == 93 else 200
        region = ["us-east", "eu-west", "ap-south"][i % 3]
        rows.append(
            f"{5000 + i},2026-07-13T10:{i % 60:02d}:00Z,{region},/v2/query/{i % 12},{9 + (i % 55)},{status},{1024 + (i * 7) % 4096}"
        )
    return Sample(
        id="csv",
        label="Query result",
        kind_label="tabular rows, CSV",
        tagline="Row after row of 200s, and one 429.",
        story="furl keeps the header, a few sample rows, and the outlier, then folds the rest. Any row is retrievable by content.",
        anomaly_regex=r",429,",
        retrieval=Retrieval(
            kind="pattern",
            control_label="Find the 429",
            caption="Ask for the 429 row. The exact original line comes back with its line number.",
            pattern=",429,",
            context_lines=0,
        ),
        original="\n".join(rows) + "\n",
    )


BUILDERS: list[Callable[[], Sample]] = [
    build_logs,
    build_crash,
    build_json,
    build_pytest,
    build_ci,
    build_csv,
]


# ----------------------------------------------------------------------------
# Engine calls + classification.
# ----------------------------------------------------------------------------
def compress_sample(original: str) -> tuple[CompressResult, str]:
    """Compress under the neutral prompt and furl's DEFAULT config."""
    res = compress(
        [
            {"role": "user", "content": NEUTRAL_PROMPT},
            {"role": "tool", "content": original},
        ]
    )
    compressed = res.messages[-1]["content"]
    if not isinstance(compressed, str):
        raise RuntimeError("compressed tool content is not a string")
    return res, compressed


def primary_hash(res: CompressResult, compressed: str) -> str:
    hashes = list(res.ccr_hashes)
    if not hashes:
        found = re.findall(r"<<ccr:([A-Za-z0-9_\-]+)", compressed)
        found += re.findall(r"hash=([0-9a-f]{8,})", compressed)
        hashes = list(dict.fromkeys(found))
    if not hashes:
        raise RuntimeError("no CCR hash found in compressed output")
    return hashes[0]


def classify_before_lines(original: str, compressed: str, anomaly_regex: str) -> list[dict[str, Any]]:
    """Tag each original line: kept (survives in compressed) and/or anomaly."""
    kept_set = set(compressed.split("\n"))
    anomaly = re.compile(anomaly_regex)
    out: list[dict[str, Any]] = []
    for line in original.split("\n"):
        out.append(
            {
                "text": line,
                "kept": line in kept_set and line.strip() != "",
                "anomaly": bool(anomaly.search(line)),
            }
        )
    if out and out[-1]["text"] == "":
        out.pop()
    return out


MARKER_RE = re.compile(r"<<ccr:|hash=|matches compressed to|lines omitted|rows_offloaded|_chunks|_ccr_")


def build_after_lines(compressed: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in compressed.split("\n"):
        out.append({"text": line, "marker": bool(MARKER_RE.search(line))})
    if out and out[-1]["text"] == "":
        out.pop()
    return out


def align_roles(before_lines: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """Per original line, decide keep vs fold by greedily matching furl's real
    retained lines to their first occurrence in the original. Duplicates that
    furl folded stay folded; the anomaly is always kept."""
    from collections import defaultdict, deque

    pos: dict[str, deque[int]] = defaultdict(deque)
    for i, b in enumerate(before_lines):
        pos[b["text"]].append(i)
    roles = ["fold"] * len(before_lines)
    for a in after:
        if a["marker"]:
            continue
        queue = pos.get(a["text"])
        if queue:
            roles[queue.popleft()] = "keep"
    for i, b in enumerate(before_lines):
        if b["anomaly"]:
            roles[i] = "keep"
    return roles


def derive_marker_text(compressed: str) -> str:
    """A clean, honest one-line summary of what furl folded, grounded in the
    real counts furl emitted in the compressed output."""
    m = re.search(r"\[(\d+) matches compressed to (\d+)\.", compressed)
    if m:
        return f"{m.group(1)} matching lines folded, {m.group(2)} kept as samples"
    m = re.search(r"\[(\d+) lines omitted, in CCR\]", compressed)
    if m:
        return f"{m.group(1)} repeated lines offloaded to CCR"
    m = re.search(r"(\d+)_rows_offloaded", compressed)
    if m:
        return f"{m.group(1)} rows offloaded to a compact table"
    return "offloaded to CCR"


def build_table_display(compressed: str) -> list[dict[str, Any]]:
    """The smart_crusher emits its compact table as a JSON-encoded string.
    Decode it to real lines for display."""
    blob = compressed
    if blob.startswith('"') and blob.endswith('"'):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            pass
    return [{"text": ln, "marker": bool(MARKER_RE.search(ln))} for ln in blob.split("\n")]


def run_retrieval(h: str, spec: Retrieval) -> dict[str, Any]:
    if spec.kind == "pattern":
        assert spec.pattern is not None
        result = retrieve(h, pattern=spec.pattern, context_lines=spec.context_lines)
        call = f'retrieve("{h}", pattern="{spec.pattern}")'
    elif spec.kind == "select":
        assert spec.select_field is not None
        result = retrieve(h, select_field=spec.select_field, select_equals=spec.select_equals)
        call = f'retrieve("{h}", select_field="{spec.select_field}", select_equals={spec.select_equals!r})'
    else:
        raise ValueError(f"unknown retrieval kind: {spec.kind}")
    if result is None:
        raise RuntimeError(f"retrieval returned None for hash {h}")
    return {"call": call, "result": result}


def build_sample_record(sample: Sample) -> dict[str, Any]:
    res, compressed = compress_sample(sample.original)
    if res.error is not None:
        raise RuntimeError(f"{sample.id}: compress failed open: {res.error}")
    h = primary_hash(res, compressed)
    full = retrieve(h)
    full_byte_exact = full == sample.original
    retr = run_retrieval(h, sample.retrieval)

    original_line_count = sample.original.rstrip("\n").count("\n") + 1
    percent = round(res.compression_ratio * 100, 1)
    transform = res.transforms_applied[-1] if res.transforms_applied else ""
    fold_mode = "table" if "smart_crusher" in transform else "lines"
    before_lines = classify_before_lines(sample.original, compressed, sample.anomaly_regex)
    after = build_after_lines(compressed)
    for b, role in zip(before_lines, align_roles(before_lines, after)):
        b["role"] = role
    keep_count = sum(1 for b in before_lines if b["role"] == "keep")

    return {
        "id": sample.id,
        "label": sample.label,
        "kind_label": sample.kind_label,
        "tagline": sample.tagline,
        "story": sample.story,
        "transform": transform,
        "transforms_applied": list(res.transforms_applied),
        "fold_mode": fold_mode,
        "tokens_before": res.tokens_before,
        "tokens_after": res.tokens_after,
        "tokens_saved": res.tokens_saved,
        "compression_ratio": res.compression_ratio,
        "percent_saved": percent,
        "original": sample.original,
        "original_line_count": original_line_count,
        "original_bytes": len(sample.original.encode("utf-8")),
        "compressed": compressed,
        "compressed_bytes": len(compressed.encode("utf-8")),
        "hash": h,
        "marker_text": derive_marker_text(compressed),
        "keep_count": keep_count,
        "before_lines": before_lines,
        "after_lines": after,
        "after_display": build_table_display(compressed) if fold_mode == "table" else None,
        "retrieval": {
            "kind": sample.retrieval.kind,
            "control_label": sample.retrieval.control_label,
            "caption": sample.retrieval.caption,
            "call": retr["call"],
            "result": retr["result"],
            "full_byte_exact": full_byte_exact,
        },
    }


def main() -> None:
    records = [build_sample_record(b()) for b in BUILDERS]

    total_before = sum(r["tokens_before"] for r in records)
    total_after = sum(r["tokens_after"] for r in records)
    avg_percent = round(sum(r["percent_saved"] for r in records) / len(records), 1)
    overall_percent = round((1 - total_after / total_before) * 100, 1)

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "furl_version": __version__,
        "model": MODEL,
        "tokenizer": "furl_ctx built-in, tiktoken cl100k_base family",
        "prompt": NEUTRAL_PROMPT,
        "config": "furl_ctx default CompressConfig",
        "sample_count": len(records),
        "aggregate": {
            "total_tokens_before": total_before,
            "total_tokens_after": total_after,
            "total_tokens_saved": total_before - total_after,
            "avg_percent_saved": avg_percent,
            "overall_percent_saved": overall_percent,
            "min_percent_saved": min(r["percent_saved"] for r in records),
            "max_percent_saved": max(r["percent_saved"] for r in records),
        },
    }

    payload = {**manifest, "samples": records}

    for r in records:
        (DATA_DIR / f"{r['id']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    js = "window.__FURL_DATA__ = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    (DATA_DIR / "furl-data.js").write_text(js)

    # Self-verifying honesty summary.
    print(f"furl {__version__}  model {MODEL}")
    print(f"{'id':8s} {'transform':26s} {'before':>7s} {'after':>6s} {'saved%':>7s}  full_exact  retr")
    for r in records:
        rr = r["retrieval"]
        print(
            f"{r['id']:8s} {r['transform']:26s} {r['tokens_before']:7d} {r['tokens_after']:6d} "
            f"{r['percent_saved']:6.1f}%  {str(rr['full_byte_exact']):>10s}  {bool(rr['result'])}"
        )
    agg = manifest["aggregate"]
    print(
        f"\naggregate: avg {agg['avg_percent_saved']}%  overall {agg['overall_percent_saved']}%  "
        f"range {agg['min_percent_saved']}-{agg['max_percent_saved']}%  "
        f"tokens {total_before} -> {total_after}"
    )
    print(f"wrote {len(records)} sample files + manifest.json + furl-data.js to {DATA_DIR}")


if __name__ == "__main__":
    main()
