"""agent_utility_eval.py — Measurement harness for furl agent-utility evaluation.

Measures whether furl's compressed output retains enough information for an agent
to answer realistic questions WITHOUT re-reading the source. If the agent must
re-grep the file, the tool is net-negative.

Assembles 6 corpora, computes deterministic ground-truth, compresses each with
furl, and emits benchmarks/agent_utility_baseline.json + a summary table.

Usage:
    uv run python benchmarks/agent_utility_eval.py [--out PATH] [--seed N]

Corpora (see _eval_corpora.py for builders + GT logic):
    1. chrome_trace_full  — 33MB Chrome trace (offload path: above 8MB guard)
    2. chrome_trace_slice — first 8000 events, in-memory (crush path: ~4MB)
    3. app_log            — ~5000-line app log, deterministically generated
    4. source_file        — real furl source file (compress.py)
    5. json_api           — paginated API dump, 500 records
    6. stacktrace         — realistic deep Python traceback
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# furl import (compile native extension if missing)
# ---------------------------------------------------------------------------
try:
    from furl_ctx import compress, retrieve
except ImportError:
    import subprocess

    subprocess.run(["uv", "run", "maturin", "develop", "--release"], check=True)
    from furl_ctx import compress, retrieve  # type: ignore[assignment]

from benchmarks._eval_corpora import CORPUS_BUILDERS

MODEL = "gpt-4o"
TOKEN_METHOD = "len//4 estimate"
MAX_BLOB_EMBED = 200_000  # chars; >200KB blob → truncate to 50KB + note
BENCHMARKS_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = BENCHMARKS_DIR / "agent_utility_baseline.json"


# ---------------------------------------------------------------------------
# Per-corpus query text (3 archetypes × 6 corpora)
# ---------------------------------------------------------------------------

CORPUS_QUERIES: dict[str, dict[str, str]] = {
    "chrome_trace_full": {
        "anomaly": "Find all DroppedFrame events and list their timestamps.",
        "locality": "What events occur immediately around the middle DroppedFrame event?",
        "aggregate": "What is the per-name event histogram (top 10) over all trace events?",
    },
    "chrome_trace_slice": {
        "anomaly": "Find all DroppedFrame events in this trace slice.",
        "locality": "What events occur immediately around the middle DroppedFrame in this slice?",
        "aggregate": "What is the per-name event count distribution (top 10) in this 8000-event slice?",
    },
    "app_log": {
        "anomaly": "Find all ERROR and WARN log entries and report their line numbers.",
        "locality": "What log lines appear immediately around line 2001?",
        "aggregate": "How many log lines are there per log level (INFO, DEBUG, WARN, ERROR)?",
    },
    "source_file": {
        "anomaly": "List all class definitions in this source file.",
        "locality": "Show the lines immediately surrounding the middle class or function definition.",
        "aggregate": "How many function (def) and class definitions are in the file?",
    },
    "json_api": {
        "anomaly": "Find all records that are both suspended and on the enterprise plan.",
        "locality": "Show the records immediately surrounding the middle suspended+enterprise record.",
        "aggregate": "What is the plan and status distribution across all 500 records?",
    },
    "stacktrace": {
        "anomaly": "What exception types are raised in this traceback (root and chained)?",
        "locality": "Show the lines immediately around the middle stack frame.",
        "aggregate": "How many stack frames appear across both exception chains?",
    },
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def est_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 chars (documented in output)."""
    return max(1, len(text) // 4)


def blob_str(messages: list[dict]) -> str:
    """Extract first message content as string (mirrors furl's ccr_hashes logic)."""
    c = messages[0]["content"]
    return c if isinstance(c, str) else json.dumps(c)


_CCR_RE = re.compile(r"<<ccr:([0-9a-f]{24})")


def check_gt_present(archetype: str, gt: dict, blob: str) -> bool:
    """Proxy presence check: is key GT information visible in the blob?

    Returns True if the GT is likely answerable from the blob alone.
    False means the agent probably needs CCR retrieve() or re-reads source.
    This is a proxy — final grading is done by an LLM in a separate step.
    """
    if archetype == "anomaly":
        # Check if any sample anomaly identifier appears
        if gt.get("sample_ts"):
            return any(str(ts) in blob for ts in gt["sample_ts"][:3])
        if gt.get("sample"):
            snippet = (
                gt["sample"][0].get("content", "")
                if isinstance(gt["sample"][0], dict)
                else str(gt["sample"][0])
            )[:60]
            return snippet in blob
        if gt.get("records"):
            r = gt["records"][0]
            return str(r.get("id", "")) in blob and "suspended" in blob
        if gt.get("exceptions"):
            return gt["exceptions"][0].get("content", "")[:40] in blob
        if gt.get("classes"):
            return gt["classes"][0].get("name", "") in blob
        return False

    if archetype == "locality":
        anchor_ts = gt.get("anchor_ts")
        if anchor_ts is not None:
            return str(anchor_ts) in blob
        window = gt.get("window", [])
        if window:
            mid = window[len(window) // 2]
            content = mid.get("content", "") if isinstance(mid, dict) else str(mid)
            return content[:50] in blob if content else False
        anchor_id = gt.get("anchor_id")
        if anchor_id is not None:
            return str(anchor_id) in blob
        return False

    if archetype == "aggregate":
        top10 = gt.get("top10_names", [])
        if top10:
            return top10[0]["name"] in blob and str(top10[0]["count"]) in blob
        level_counts = gt.get("level_counts", {})
        if level_counts:
            return all(k in blob for k in list(level_counts.keys())[:3])
        if "total_defs" in gt:
            return str(gt["total_defs"]) in blob
        plan_dist = gt.get("plan_distribution", {})
        if plan_dist:
            return all(k in blob for k in plan_dist)
        if "frame_count" in gt:
            return str(gt["frame_count"]) in blob
        return False

    return False


# ---------------------------------------------------------------------------
# Core: compress one corpus, return 3 records (one per archetype)
# ---------------------------------------------------------------------------


def run_corpus(
    corpus_id: str,
    corpus_str: str,
    corpus_meta: dict,
    source_label: str,
    gt_all: dict,
) -> list[dict]:
    """Compress corpus once; emit one record per (corpus, archetype)."""
    messages = [{"role": "tool", "content": corpus_str}]
    result = compress(messages, model=MODEL)

    blob = blob_str(result.messages)
    blob_chars = len(blob)
    offloaded = len(result.ccr_hashes) > 0

    retrieve_chars: int | None = None
    if offloaded and result.ccr_hashes:
        retrieved = retrieve(result.ccr_hashes[0], query=None)
        retrieve_chars = len(retrieved) if retrieved else None

    blob_embed = blob
    blob_truncated = False
    if len(blob) > MAX_BLOB_EMBED:
        blob_embed = blob[:50_000] + f"\n[...TRUNCATED: full blob is {len(blob)} chars...]"
        blob_truncated = True

    corpus_bytes = len(corpus_str.encode("utf-8"))
    records = []
    for archetype in ("anomaly", "locality", "aggregate"):
        gt = gt_all[archetype]
        records.append(
            {
                "corpus_id": corpus_id,
                "corpus_bytes": corpus_bytes,
                "source": source_label,
                "archetype": archetype,
                "query": CORPUS_QUERIES[corpus_id][archetype],
                "ground_truth": gt,
                "blob": blob_embed,
                "blob_chars": blob_chars,
                "blob_tokens": est_tokens(blob),
                "token_method": TOKEN_METHOD,
                "transforms": result.transforms_applied,
                "compression_ratio": result.compression_ratio,
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "offloaded": offloaded,
                "ccr_hashes": result.ccr_hashes,
                "retrieve_chars_if_offloaded": retrieve_chars,
                "gt_present_in_blob": check_gt_present(archetype, gt, blob),
                "blob_truncated": blob_truncated,
            }
        )
    return records


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def print_summary(all_records: list[dict]) -> None:
    """Print human-readable summary table to stdout."""
    corpora: dict[str, dict] = {}
    for r in all_records:
        cid = r["corpus_id"]
        if cid not in corpora:
            corpora[cid] = {
                "corpus_bytes": r["corpus_bytes"],
                "transforms": r["transforms"],
                "compression_ratio": r["compression_ratio"],
                "blob_chars": r["blob_chars"],
                "offloaded": r["offloaded"],
                "retrieve_chars": r["retrieve_chars_if_offloaded"],
                "by_archetype": {},
            }
        corpora[cid]["by_archetype"][r["archetype"]] = r["gt_present_in_blob"]

    hdr = (
        f"{'Corpus':<22} {'Bytes':>11} {'Ratio':>7} {'BlobCh':>8} "
        f"{'Off?':>5} {'RetCh':>10}  {'anom':>5} {'loc':>5} {'agg':>5}"
    )
    sep = "-" * len(hdr)
    print("\n=== furl Agent-Utility Baseline Summary ===")
    print(hdr)
    print(sep)
    for cid, info in corpora.items():
        by_arch = info["by_archetype"]
        anom = "YES" if by_arch.get("anomaly") else "NO"
        loc = "YES" if by_arch.get("locality") else "NO"
        agg = "YES" if by_arch.get("aggregate") else "NO"
        off = "YES" if info["offloaded"] else "NO"
        ret = f"{info['retrieve_chars']:,}" if info["retrieve_chars"] else "N/A"
        ratio_pct = f"{info['compression_ratio'] * 100:.1f}%"
        print(
            f"{cid:<22} {info['corpus_bytes']:>11,} {ratio_pct:>7} "
            f"{info['blob_chars']:>8,} {off:>5} {ret:>10}  "
            f"{anom:>5} {loc:>5} {agg:>5}"
        )
    print(sep)
    print("GT-present proxy: substring check only — final grading is by LLM.")
    print(f"Token method: {TOKEN_METHOD}")
    print("Off? = CCR hash present. RetCh = all-or-nothing recovery cost (chars).\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="furl agent-utility baseline harness")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for generated corpora")
    args = parser.parse_args()

    all_records: list[dict] = []
    print("Building corpora and compressing...")

    for corpus_id, builder_fn, gt_fn in CORPUS_BUILDERS:
        print(f"  [{corpus_id}] ", end="", flush=True)
        try:
            # Pass seed to generated corpora
            if corpus_id in ("app_log", "json_api", "stacktrace"):
                corpus_str, corpus_meta, source_label = builder_fn(args.seed)
            else:
                corpus_str, corpus_meta, source_label = builder_fn()

            print(f"{len(corpus_str):,} chars  ", end="", flush=True)
            gt_all = gt_fn(corpus_str, corpus_meta)
            records = run_corpus(corpus_id, corpus_str, corpus_meta, source_label, gt_all)
            all_records.extend(records)
            r0 = records[0]
            print(
                f"ratio={r0['compression_ratio']:.4f}  "
                f"blob={r0['blob_chars']:,}ch  "
                f"off={r0['offloaded']}  "
                f"transforms={r0['transforms']}"
            )
        except Exception as exc:
            import traceback

            print(f"ERROR: {exc}")
            traceback.print_exc()
            continue

    # Emit JSON
    out_path: Path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, indent=2, default=str)
    print(f"\nBaseline written: {out_path}  ({out_path.stat().st_size:,} bytes)")

    print_summary(all_records)

    # Blob previews for the two trace corpora (required by spec)
    for cid in ("chrome_trace_full", "chrome_trace_slice"):
        recs = [r for r in all_records if r["corpus_id"] == cid]
        if recs:
            print(f"\n=== BLOB PREVIEW: {cid} (first 1500 chars) ===")
            print(recs[0]["blob"][:1500])


if __name__ == "__main__":
    main()
