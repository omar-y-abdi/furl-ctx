"""Runner: full out-of-sample sweep with fixed seeds, writes raw_results.json.

Each case is measured in its OWN freshly-spawned subprocess (see
``verify/worker.py``) so that BOTH CCR layers — the Python ``CompressionStore``
and the Rust process-local CCR store on the singleton pipeline's
``SmartCrusher`` — start genuinely COLD. This is the only way to honor the
mandate's "cold CCR/cache state per case; no warm cache carried between runs",
because the Rust CCR store has no Python reset surface and would otherwise
accumulate (capacity 1000, FIFO) across a long-lived process.

A SEPARATE in-process probe (``probe_result_cache_ccr_divergence``) documents a
silent-data-loss condition: the router's Tier-2 result cache serves a crushed
output (with its ``<<ccr:HASH>>`` drop sentinel) on a content cache-hit without
re-running the CCR mirror, while the CCR store entry has an independent 300 s
TTL — so an expired CCR entry leaves the still-served sentinel UNBACKED (a
signalled but unrecoverable drop). A real failure mode of the public
``compress()`` surface in a long-running server.

Sweeps every (family x tier x size) over N_SEEDS fixed seeds on the engine's
committed DEFAULT params (no config, no kwargs => CompressConfig defaults +
RoutingPolicy default MinTokens). Aggregates mean + min/max. Re-runnable::

    .venv/bin/python -m verify.run
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from verify import generators as gen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS_JSON = HERE / "raw_results.json"

# Fixed seed sweep (>=5 per case, no cherry-picking).
N_SEEDS = 6
SEEDS = tuple(1000 + 137 * i for i in range(N_SEEDS))  # deterministic, fixed

# Dev claims under test (token-reduction %, as reported during dev). Used ONLY
# to flag degradations — never to tune anything.
DEV_CLAIMS = {
    "logs@90": 0.930,
    "search@90": 0.927,
    "repeated_logs@90": 0.971,
    # Key is @90 (the actual generated size); threshold 0.708 was measured at
    # 135 items — intentional approximation to restore the auto-flag at @90.
    "multiturn@90": 0.708,
    "disk@9": 0.50,
    "code@7": 0.0,
}

# (family, sizes, with_needles). Needles on single-tool structured families.
FAMILIES = [
    ("logs", gen.SIZES, True),
    ("repeated_logs", gen.SIZES, True),
    ("search", gen.SIZES, True),
    ("code", (7, 30), False),
    ("multiturn", (90, 900), False),
    ("disk", (9, 90), True),
]


def _agg(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def run_case_subprocess(family: str, size: int, tier: str, seed: int, needles: bool) -> dict:
    """Measure ONE case in a fresh subprocess (cold Rust+Python CCR)."""
    spec = json.dumps(
        {"family": family, "size": size, "tier": tier, "seed": seed, "needles": needles}
    )
    proc = subprocess.run(
        [sys.executable, "-m", "verify.worker", spec],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def run_group(family: str, n: int, tier: str, *, needles: bool) -> dict:
    """Run N_SEEDS fresh-process seeds for one (family,tier,size); aggregate."""
    per_seed = [run_case_subprocess(family, n, tier, seed, needles) for seed in SEEDS]

    token_red = [c["token_reduction"] for c in per_seed]
    retention = [c["information_retention"] for c in per_seed]
    byte_exact = [c["hash_byte_exact"] for c in per_seed]
    eff0 = [c["effective_savings"].get("0", 0.0) for c in per_seed]
    eff25 = [c["effective_savings"].get("25", 0.0) for c in per_seed]
    eff50 = [c["effective_savings"].get("50", 0.0) for c in per_seed]
    dropped = [float(c["n_dropped"]) for c in per_seed]

    all_needles = [nd for c in per_seed for nd in c["needles"]]
    needle_summary = None
    if all_needles:
        needle_summary = {
            "total": len(all_needles),
            "visible": sum(1 for nd in all_needles if nd["visible"]),
            "recoverable": sum(1 for nd in all_needles if nd["recoverable"]),
            "signalled": sum(1 for nd in all_needles if nd["signalled"]),
            "silent_loss": sum(1 for nd in all_needles if nd["silent_loss"]),
        }

    cps = [c["cache_prefix"] for c in per_seed if c.get("cache_prefix") is not None]
    cache_prefix_summary = None
    if cps:
        cache_prefix_summary = {
            "all_preserved_in_order": all(c["preserved_in_order"] for c in cps),
            "all_index0_intact": all(c["index0_intact"] for c in cps),
            "any_reordered": any(c["reordered"] for c in cps),
            "any_dropped": any(c["dropped_indices"] for c in cps),
            "n": len(cps),
        }

    used_defaults = all(c["used_default_params"] for c in per_seed)

    return {
        "family": family,
        "tier": tier,
        "size": n,
        "case_id": f"{family}@{n}",
        "seeds": list(SEEDS),
        "n_seeds": len(SEEDS),
        "token_reduction": _agg(token_red),
        "information_retention": _agg(retention),
        "n_dropped": _agg(dropped),
        "hash_byte_exact_all": all(byte_exact),
        "hash_byte_exact_count": sum(1 for b in byte_exact if b),
        "effective_savings": {"0": _agg(eff0), "25": _agg(eff25), "50": _agg(eff50)},
        "needles": needle_summary,
        "cache_prefix": cache_prefix_summary,
        "transforms_sample": per_seed[0]["transforms"],
        "took_lossy_path_any": any(c["took_lossy_path"] for c in per_seed),
        "used_default_params": used_defaults,
        "per_seed": per_seed,
    }


def probe_result_cache_ccr_divergence() -> dict:
    """Document the RESULT-CACHE vs CCR-STORE lifetime-divergence silent loss.

    Mechanism (traced to the engine source): the router's Tier-2
    ``CompressionCache`` (``headroom/transforms/content_router.py`` /
    ``cache/compression_cache.py``) caches the *crushed output* — including its
    ``{"_ccr_dropped": "<<ccr:HASH ...>>"}`` sentinel — keyed by content. On a
    cache HIT no fresh compression runs, so the CCR Rust→Python mirror
    (``SmartCrusher._mirror_ccr_to_python_store``, only reached inside
    ``smart_crush_content``) is SKIPPED. The two caches have INDEPENDENT
    lifetimes: the CCR store entry has its own 300 s TTL. Once the CCR entry is
    gone (TTL expiry) but the result cache still serves the identical crushed
    output, the served ``<<ccr:HASH>>`` pointer references a NON-EXISTENT entry
    — a SIGNALLED but UNRECOVERABLE drop (silent data loss): the model is told
    "retrieve hash=H" and gets nothing.

    This probe reproduces it in-process WITHOUT touching the result cache: it
    compresses content X (fresh compute, mirror runs, drop is backed), then
    wipes ONLY the CCR store (faithfully simulating CCR TTL expiry — the result
    cache is untouched), then compresses the SAME X again (result-cache HIT,
    same crushed bytes, mirror skipped) and checks whether the served sentinel
    still resolves.
    """
    from headroom import compress
    from headroom.cache.compression_store import reset_compression_store
    from verify.measure import (  # imported lazily; runs in THIS process
        _emitted_drop_hashes,
        _retrieve_originals,
        _stringify,
    )

    trials: list[dict] = []
    for seed in (1000, 1137, 1274):
        case = gen.plant_needles(gen.gen_logs(seed, 90, "medium"), seed, k=3)

        reset_compression_store()
        r1 = compress(case.messages, model="gpt-4o")
        out1 = _stringify(r1.messages[-1].get("content"))
        h1 = _emitted_drop_hashes(out1)
        rec1 = _retrieve_originals(h1, case.query)
        backed1 = bool(h1) and len(rec1) == len(h1)

        # Simulate the CCR entry's 300 s TTL expiring while the result cache
        # (separate lifetime) still holds the crushed output.
        reset_compression_store()

        r2 = compress(case.messages, model="gpt-4o")
        out2 = _stringify(r2.messages[-1].get("content"))
        h2 = _emitted_drop_hashes(out2)
        rec2 = _retrieve_originals(h2, case.query)
        backed2 = bool(h2) and len(rec2) == len(h2)

        trials.append(
            {
                "seed": seed,
                "first_compress_emitted": sorted(h1),
                "first_compress_backed": backed1,
                "ccr_expiry_simulated": True,
                "second_compress_emitted": sorted(h2),
                "second_compress_backed": backed2,
                "same_crushed_output": out1 == out2,
                "silent_loss": bool(h2) and not backed2,
            }
        )

    return {
        "description": (
            "Result-cache vs CCR-store lifetime divergence. The router's Tier-2 "
            "CompressionCache serves the crushed output (with its <<ccr:HASH>> "
            "drop sentinel) on a content cache-hit WITHOUT re-running the CCR "
            "mirror. The CCR store entry has an independent 300 s TTL. When the "
            "CCR entry expires but the result cache still serves the identical "
            "output, the <<ccr:HASH>> pointer references a non-existent entry: "
            "a SIGNALLED but UNRECOVERABLE drop (silent data loss). Reproduced "
            "by compressing X (drop backed), simulating CCR TTL expiry, then "
            "re-compressing X (result-cache hit, same bytes, sentinel now "
            "unbacked)."
        ),
        "trials": trials,
        "any_silent_loss": any(t["silent_loss"] for t in trials),
        "all_second_unbacked": all(
            (not t["second_compress_backed"]) and t["same_crushed_output"] for t in trials
        ),
    }


def git_commit() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def main() -> int:
    groups: list[dict] = []
    for family, sizes, needles in FAMILIES:
        for tier in gen.TIERS:
            for n in sizes:
                g = run_group(family, n, tier, needles=needles)
                groups.append(g)
                nd = g["needles"]
                ndtxt = (
                    f"needles[vis={nd['visible']}/sig={nd['signalled']}/silent={nd['silent_loss']}]"
                    if nd
                    else ""
                )
                print(
                    f"{g['case_id']:18s} {tier:6s} "
                    f"red={g['token_reduction']['mean'] * 100:6.1f}% "
                    f"retain={g['information_retention']['mean'] * 100:6.1f}% "
                    f"byte_exact={str(g['hash_byte_exact_all']):5s} "
                    f"drop={g['n_dropped']['mean']:6.1f} "
                    f"lossy={str(g['took_lossy_path_any']):5s} {ndtxt}"
                )

    print("\n=== result-cache vs CCR-store divergence probe (silent loss) ===")
    inproc = probe_result_cache_ccr_divergence()
    print(
        f"any silent loss: {inproc['any_silent_loss']}; "
        f"all 2nd-compress sentinels unbacked on cache-hit: {inproc['all_second_unbacked']}"
    )

    # Degradations: fresh token-reduction mean BELOW the dev claim. Flags every
    # tier where a case_id matches; all tiers recorded for transparency.
    degradations: list[dict] = []
    for g in groups:
        claim = DEV_CLAIMS.get(g["case_id"])
        if claim is None:
            continue
        fresh = g["token_reduction"]["mean"]
        if fresh < claim - 0.01:
            degradations.append(
                {
                    "case_id": g["case_id"],
                    "tier": g["tier"],
                    "dev_claim": claim,
                    "fresh_mean": fresh,
                    "delta": fresh - claim,
                    "fresh_min": g["token_reduction"]["min"],
                    "fresh_max": g["token_reduction"]["max"],
                }
            )

    hash_failures: list[dict] = []
    for g in groups:
        if not g["hash_byte_exact_all"]:
            hash_failures.append(
                {
                    "case_id": g["case_id"],
                    "tier": g["tier"],
                    "size": g["size"],
                    "byte_exact_count": g["hash_byte_exact_count"],
                    "n_seeds": g["n_seeds"],
                    "missing_examples": [ex for c in g["per_seed"] for ex in c["missing_examples"]][
                        :5
                    ],
                }
            )

    silent_loss_findings: list[dict] = []
    for g in groups:
        nd = g["needles"]
        if nd and nd["silent_loss"] > 0:
            silent_loss_findings.append(
                {
                    "case_id": g["case_id"],
                    "tier": g["tier"],
                    "silent_loss": nd["silent_loss"],
                    "total_needles": nd["total"],
                    "visible": nd["visible"],
                    "signalled": nd["signalled"],
                }
            )

    cache_prefix_violations: list[dict] = []
    for g in groups:
        cp = g["cache_prefix"]
        if cp and (
            not cp["all_preserved_in_order"]
            or cp["any_reordered"]
            or cp["any_dropped"]
            or not cp["all_index0_intact"]
        ):
            cache_prefix_violations.append({"case_id": g["case_id"], "tier": g["tier"], **cp})

    all_defaults = all(g["used_default_params"] for g in groups)

    payload = {
        "schema": "headroom.verify.outofsample.v2",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "engine_model": "gpt-4o",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "isolation": "each case measured in a fresh subprocess (cold Rust+Python CCR)",
        "n_seeds": N_SEEDS,
        "seeds": list(SEEDS),
        "default_params_confirmed": all_defaults,
        "routing_policy_default": "min-tokens (MinTokens)",
        "dev_claims_under_test": DEV_CLAIMS,
        "groups": groups,
        "result_cache_ccr_divergence_probe": inproc,
        "degradations": degradations,
        "hash_failures": hash_failures,
        "silent_loss_findings": silent_loss_findings,
        "cache_prefix_violations": cache_prefix_violations,
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_JSON}")
    print(
        f"default_params_confirmed={all_defaults}  degradations={len(degradations)}  "
        f"hash_failures={len(hash_failures)}  silent_loss={len(silent_loss_findings)}  "
        f"cache_prefix_violations={len(cache_prefix_violations)}  "
        f"result_cache_silent_loss={inproc['any_silent_loss']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
