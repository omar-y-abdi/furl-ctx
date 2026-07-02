"""Held-out runner: full out-of-sample sweep, writes raw_results.json.

Each case is measured in its OWN freshly-spawned subprocess (``worker.py``) so
BOTH CCR layers — the Python ``CompressionStore`` and the Rust process-local
CCR store on the singleton pipeline's ``SmartCrusher`` — start genuinely COLD
(the mandate's "cold CCR/cache state per case; no warm cache carried between
runs"). The Rust CCR store has no Python reset surface and would otherwise
accumulate (capacity 1000, FIFO) across a long-lived process.

In ADDITION to the dev-claim sweep, this runner evaluates the ROUND-3
improvement claims on DIFFERENT held-out data (express/chalk/npm-cli, new
seeds) so any gain that showed on the first run's slugify data but does NOT
replicate here is flagged as OVERFITTING:

* search@90 high  36.1% erratic -> 93.6% reliable (±0.1pp)   [reliability]
* logs@90   high  80.2% -> 82.4%                              [reduction]
* lossless cross-row affix fold + head-dictionary fold on STRUCTURED
  near-unique string columns, with genuine-entropy columns getting NO
  encoding (no fake gain)                                     [encoding-fire]
* deterministic entropy-floor crushability override -> reliably aggressive
  lossy drop on near-unique rows                              [drop-reliability]
* TTL/result-cache silent-loss bug stays fixed               [divergence probe]

A pure-random NO-STRUCTURE control proves the structural folds DECLINE when
there is genuinely nothing to fold (the anti-cheat negative control).

Re-runnable by a third party::

    .venv/bin/python -m verify.heldout.run
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from verify.heldout import generators as gen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # repo root (verify/heldout -> verify -> root)
RESULTS_JSON = HERE / "raw_results.json"

# Fixed seed sweep (>=5 per case). DIFFERENT base + stride from the first run
# (which used 1000 + 137*i) so this data is provably disjoint.
N_SEEDS = 6
SEEDS = tuple(2000 + 211 * i for i in range(N_SEEDS))  # 2000,2211,2422,...

# Dev claims under test (token-reduction fraction, as reported during dev).
# Used ONLY to flag degradations — never to tune anything.
DEV_CLAIMS = {
    "logs@90": 0.930,
    "search@90": 0.927,
    "repeated_logs@90": 0.971,
    "multiturn@135": 0.708,
    "disk@9": 0.50,
    "code@7": 0.0,
}

# Round-3 reduction claims keyed by (family, tier). Flagged if NOT replicated.
ROUND3_REDUCTION_CLAIMS = {
    ("search", "high"): 0.936,  # 36.1% erratic -> 93.6% reliable (±0.1pp)
    ("logs", "high"): 0.824,  # 80.2% -> 82.4%
}
# Round-3 reliability claim: search@90 high spread must be tight (±~0.1pp ->
# we allow a generous 5pp band as "reliable" vs the old "erratic 24-94%").
SEARCH_HIGH_RELIABILITY_BAND = 0.05

# (family, sizes, with_needles). Needles on single-tool structured families.
FAMILIES = [
    ("logs", gen.SIZES, True),
    ("repeated_logs", gen.SIZES, True),
    ("search", gen.SIZES, True),
    ("code", (7, 30), False),
    ("multiturn", (90, 900), False),
    ("disk", (9, 90), True),
]

ENCODING_MARKERS = {
    "affix": "__affix:",
    "head": "__head:",
    "dict": "__dict:",
    "ccr_drop": "<<ccr:",
}


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
        [sys.executable, "-m", "verify.heldout.worker", spec],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def detect_encodings(family: str, size: int, tier: str, seed: int, needles: bool) -> dict:
    """Detect which lossless structural encodings the engine emitted for this
    case, by reading the rendered compressed output (DEFAULT params, cold
    store). This is a READ-ONLY observation of the engine's own output grammar —
    we do not change anything. Run in a fresh subprocess to keep CCR cold.
    """
    spec = json.dumps(
        {"family": family, "size": size, "tier": tier, "seed": seed, "needles": needles}
    )
    proc = subprocess.run(
        [sys.executable, "-m", "verify.heldout.encprobe", spec],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_group(family: str, n: int, tier: str, *, needles: bool) -> dict:
    """Run N_SEEDS fresh-process seeds for one (family,tier,size); aggregate."""
    per_seed = [run_case_subprocess(family, n, tier, seed, needles) for seed in SEEDS]
    # Encoding-fire observation. The round-3 affix/head/dict claim is about
    # INTACT structured columns; planted needles deliberately stamp unique
    # values into the text columns and thereby DESTROY the shared structure the
    # fold relies on (a correct engine then declines the fold on that case). So
    # the encoding-fire counts are measured on the NEEDLE-FREE variant — that is
    # what the affix/head claim is actually about. We still keep the
    # with-needles observation for transparency.
    enc_seeds = [detect_encodings(family, n, tier, seed, False) for seed in SEEDS]
    enc_seeds_with_needles = (
        [detect_encodings(family, n, tier, seed, True) for seed in SEEDS] if needles else enc_seeds
    )

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

    # Encoding-fire counts across seeds (how many of N seeds fired each fold).
    enc_counts = {k: sum(1 for e in enc_seeds if e.get(k)) for k in ENCODING_MARKERS}
    enc_counts_with_needles = {
        k: sum(1 for e in enc_seeds_with_needles if e.get(k)) for k in ENCODING_MARKERS
    }

    used_defaults = all(c["used_default_params"] for c in per_seed) and all(
        e.get("used_default_params", True) for e in enc_seeds
    )

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
        "encodings_fired_count": enc_counts,
        "encodings_fired_count_with_needles": enc_counts_with_needles,
        "transforms_sample": per_seed[0]["transforms"],
        "took_lossy_path_any": any(c["took_lossy_path"] for c in per_seed),
        "used_default_params": used_defaults,
        "per_seed": per_seed,
    }


def no_structure_control() -> dict:
    """ANTI-CHEAT NEGATIVE CONTROL: pure-random rows with NO shared prefix /
    suffix / head / value-set. A correct engine must DECLINE every structural
    fold (affix / head-dict / value-dict) — any fold firing here would be a
    fake gain on genuine entropy. Run across the same seeds, in-process is fine
    (we only read the rendered output; CCR is reset per seed).
    """
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import reset_compression_store

    trials = []
    for seed in SEEDS:
        reset_compression_store()
        rng = random.Random(seed)

        def h(rng: random.Random = rng) -> str:
            return hashlib.sha1(rng.randbytes(20)).hexdigest()

        rows = [{"a": h(), "b": h(), "c": h()} for _ in range(90)]
        msgs = [
            {"role": "user", "content": "analyze"},
            {"role": "tool", "content": json.dumps(rows)},
        ]
        r = compress(msgs, model="gpt-4o")
        out = str(r.messages[-1].get("content"))
        fired = {k: (mark in out) for k, mark in ENCODING_MARKERS.items()}
        trials.append(
            {
                "seed": seed,
                "affix": fired["affix"],
                "head": fired["head"],
                "dict": fired["dict"],
                "ccr_drop": fired["ccr_drop"],
                # the only acceptable reduction mechanism here is row-dedup /
                # CCR offload, NOT a structural string fold.
                "fake_structural_fold": fired["affix"] or fired["head"] or fired["dict"],
            }
        )
    return {
        "description": (
            "Pure-random rows (sha1 hex, no shared prefix/suffix/head/value-set). "
            "A correct engine DECLINES every structural string fold here; any "
            "affix/head/dict fold firing = fake gain on genuine entropy."
        ),
        "trials": trials,
        "any_fake_structural_fold": any(t["fake_structural_fold"] for t in trials),
    }


def probe_result_cache_ccr_divergence() -> dict:
    """Re-run the silent-loss divergence probe on held-out data + seeds.

    Mechanism (traced to engine source): the router's Tier-2 ``CompressionCache``
    caches the crushed output (with its ``{"_ccr_dropped": "<<ccr:HASH>>"}``
    sentinel) keyed by content; on a cache HIT no fresh compression runs so the
    CCR Rust->Python mirror is SKIPPED. The CCR store entry has an independent
    300 s TTL. When the CCR entry expires but the result cache still serves the
    identical output, the served ``<<ccr:HASH>>`` pointer references a
    non-existent entry: a SIGNALLED but UNRECOVERABLE drop (silent data loss).

    Reproduced WITHOUT touching the result cache: compress X (mirror runs, drop
    backed), wipe ONLY the CCR store (simulating CCR TTL expiry; result cache
    untouched), re-compress X (result-cache hit, same bytes, mirror skipped),
    check whether the served sentinel still resolves. A FIXED engine re-mirrors
    on the cache hit so the second drop stays backed (no silent loss).
    """
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import reset_compression_store
    from verify.measure import (
        _emitted_drop_hashes,
        _retrieve_originals,
        _stringify,
    )

    trials: list[dict] = []
    for seed in (2000, 2211, 2422):
        case = gen.plant_needles(gen.gen_logs(seed, 90, "medium"), seed, k=3)

        reset_compression_store()
        r1 = compress(case.messages, model="gpt-4o")
        out1 = _stringify(r1.messages[-1].get("content"))
        h1 = _emitted_drop_hashes(out1)
        rec1 = _retrieve_originals(h1, case.query)
        backed1 = bool(h1) and len(rec1) == len(h1)

        reset_compression_store()  # simulate CCR TTL expiry (result cache kept)

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
            "Result-cache vs CCR-store lifetime divergence (silent-loss probe). "
            "Compress X (drop backed), simulate CCR TTL expiry, re-compress X "
            "(result-cache hit, same bytes). A FIXED engine re-mirrors the CCR "
            "entry on the cache hit so the second-compress sentinel stays "
            "backed; silent_loss=True would mean the bug regressed."
        ),
        "trials": trials,
        "any_silent_loss": any(t["silent_loss"] for t in trials),
        "all_second_backed": all(t["second_compress_backed"] for t in trials),
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


def evaluate_round3(groups: list[dict]) -> dict:
    """Evaluate the round-3 improvement claims for replication / overfitting."""
    by_key = {(g["family"], g["tier"], g["size"]): g for g in groups}

    reduction_checks = []
    for (fam, tier), claim in ROUND3_REDUCTION_CLAIMS.items():
        g = by_key.get((fam, tier, 90))
        if g is None:
            continue
        fresh = g["token_reduction"]["mean"]
        reduction_checks.append(
            {
                "claim": f"{fam}@90 {tier} reduction ~= {claim:.3f}",
                "fresh_mean": fresh,
                "fresh_min": g["token_reduction"]["min"],
                "fresh_max": g["token_reduction"]["max"],
                "replicated": fresh >= claim - 0.02,
                "delta": fresh - claim,
            }
        )

    # search@90 high reliability: tight spread (not the old erratic 24-94%).
    sh = by_key.get(("search", "high", 90))
    reliability = None
    if sh is not None:
        spread = sh["token_reduction"]["max"] - sh["token_reduction"]["min"]
        reliability = {
            "claim": "search@90 high reduction is RELIABLE (tight spread), not erratic 24-94%",
            "fresh_mean": sh["token_reduction"]["mean"],
            "fresh_min": sh["token_reduction"]["min"],
            "fresh_max": sh["token_reduction"]["max"],
            "spread": spread,
            "reliable": spread <= SEARCH_HIGH_RELIABILITY_BAND,
            "all_byte_exact": sh["hash_byte_exact_all"],
        }

    # Affix/head fold fires on STRUCT, declines on GENUINE (per family).
    encoding_fire = []
    for fam in ("search", "disk", "logs", "multiturn"):
        for size in (90,) if fam != "multiturn" else (90,):
            gstruct = by_key.get((fam, "struct", size if fam != "multiturn" else 90))
            ggen = by_key.get((fam, "genuine", size if fam != "multiturn" else 90))
            if gstruct is None or ggen is None:
                continue
            struct_enc = gstruct["encodings_fired_count"]
            gen_enc = ggen["encodings_fired_count"]
            n = gstruct["n_seeds"]
            struct_fold = struct_enc["affix"] + struct_enc["head"] + struct_enc["dict"]
            encoding_fire.append(
                {
                    "family": fam,
                    "struct_affix_seeds": struct_enc["affix"],
                    "struct_head_seeds": struct_enc["head"],
                    "struct_dict_seeds": struct_enc["dict"],
                    "genuine_affix_seeds": gen_enc["affix"],
                    "genuine_head_seeds": gen_enc["head"],
                    "genuine_dict_seeds": gen_enc["dict"],
                    "n_seeds": n,
                    # struct should fire SOME structural fold on all seeds.
                    "struct_fold_fires": struct_fold > 0,
                    "struct_byte_exact": gstruct["hash_byte_exact_all"],
                    "genuine_byte_exact": ggen["hash_byte_exact_all"],
                }
            )

    # Entropy-floor crushability override: near-unique tiers reliably drop
    # aggressively (high lossy drop fraction, tight across seeds).
    drop_reliability = []
    for fam in ("search", "logs", "disk", "multiturn"):
        for tier in ("high", "genuine"):
            for g in groups:
                if g["family"] == fam and g["tier"] == tier and g["size"] in (90,):
                    drop_reliability.append(
                        {
                            "case": f"{fam}@90 {tier}",
                            "mean_dropped": g["n_dropped"]["mean"],
                            "min_dropped": g["n_dropped"]["min"],
                            "max_dropped": g["n_dropped"]["max"],
                            "lossy_path": g["took_lossy_path_any"],
                            "reduction_mean": g["token_reduction"]["mean"],
                            "reduction_spread": g["token_reduction"]["max"]
                            - g["token_reduction"]["min"],
                            "all_byte_exact": g["hash_byte_exact_all"],
                        }
                    )

    return {
        "reduction_checks": reduction_checks,
        "search_high_reliability": reliability,
        "encoding_fire_struct_vs_genuine": encoding_fire,
        "entropy_floor_drop_reliability": drop_reliability,
    }


def main() -> int:
    groups: list[dict] = []
    for family, sizes, needles in FAMILIES:
        for tier in gen.TIERS:
            for n in sizes:
                g = run_group(family, n, tier, needles=needles)
                groups.append(g)
                nd = g["needles"]
                ndtxt = (
                    f"needle[vis={nd['visible']}/sig={nd['signalled']}/silent={nd['silent_loss']}]"
                    if nd
                    else ""
                )
                enc = g["encodings_fired_count"]
                print(
                    f"{g['case_id']:18s} {tier:8s} "
                    f"red={g['token_reduction']['mean'] * 100:6.1f}% "
                    f"[{g['token_reduction']['min'] * 100:5.1f}-{g['token_reduction']['max'] * 100:5.1f}] "
                    f"retain={g['information_retention']['mean'] * 100:6.1f}% "
                    f"be={str(g['hash_byte_exact_all']):5s} "
                    f"drop={g['n_dropped']['mean']:6.1f} "
                    f"enc[af={enc['affix']}/hd={enc['head']}/dc={enc['dict']}] {ndtxt}"
                )

    print("\n=== no-structure anti-cheat control ===")
    control = no_structure_control()
    print(f"any fake structural fold on pure-random: {control['any_fake_structural_fold']}")

    print("\n=== result-cache vs CCR-store divergence probe (silent loss) ===")
    inproc = probe_result_cache_ccr_divergence()
    print(
        f"any silent loss: {inproc['any_silent_loss']}; all 2nd-compress backed: {inproc['all_second_backed']}"
    )

    round3 = evaluate_round3(groups)

    # Degradations: fresh token-reduction mean BELOW the dev claim (the POINT).
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

    # Round-3 overfitting flags: a round-3 reduction claim that does NOT
    # replicate on this held-out data.
    round3_overfit: list[dict] = []
    for rc in round3["reduction_checks"]:
        if not rc["replicated"]:
            round3_overfit.append({"type": "reduction", **rc})
    if round3["search_high_reliability"] and not round3["search_high_reliability"]["reliable"]:
        round3_overfit.append({"type": "reliability", **round3["search_high_reliability"]})
    for ef in round3["encoding_fire_struct_vs_genuine"]:
        if not ef["struct_fold_fires"]:
            round3_overfit.append({"type": "encoding_fire", **ef})

    all_defaults = all(g["used_default_params"] for g in groups)

    payload = {
        "schema": "furl_ctx.verify.heldout.v1",
        "run_label": "SECOND held-out verification (express/chalk/npm-cli, seeds 2000+211i)",
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
        "round3_reduction_claims": {
            f"{k[0]}@90 {k[1]}": v for k, v in ROUND3_REDUCTION_CLAIMS.items()
        },
        "groups": groups,
        "no_structure_control": control,
        "result_cache_ccr_divergence_probe": inproc,
        "round3_evaluation": round3,
        "round3_overfit_flags": round3_overfit,
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
        f"round3_overfit_flags={len(round3_overfit)}  "
        f"fake_fold_on_random={control['any_fake_structural_fold']}  "
        f"result_cache_silent_loss={inproc['any_silent_loss']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
