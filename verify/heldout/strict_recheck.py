"""Phase-2 STRICT independent spot-recheck of the held-out harness.

The Phase-1 harness's ``hash_compare_structured`` has a lenient fallback
(``_present_in_text``: an item counts as reconstructed if every scalar value
appears verbatim ANYWHERE in the output, even scattered). That can mark an item
"reconstructed" without the documented decoder / CCR actually round-tripping it.

This recheck REFUSES that fallback. An original item counts as reconstructed
ONLY when its canonical signature is produced by one of the engine's documented
recovery surfaces:

  (a) a row visible verbatim in a JSON-array rendering of the compressed output,
  (b) a row decoded by ``decode_csv_schema_rows`` (the documented decoder), or
  (c) a row retrieved from the CCR store via the ``<<ccr:HASH>>`` sentinel.

Then it sha256-compares the reconstructed multiset to the original multiset.
If a case is ``byte_exact`` under the harness but NOT here, the harness's
substring fallback is inflating recovery -> we flag it.

Cold CCR per case (``reset_compression_store`` + this runs as a one-shot
process). DEFAULT params only. Re-runnable by a third party::

    .venv/bin/python -m verify.heldout.strict_recheck logs 90 high 2000
    .venv/bin/python -m verify.heldout.strict_recheck search 90 high 2000 --needles
"""

from __future__ import annotations

import hashlib
import json
import sys

from furl_ctx import compress
from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows
from verify.heldout import generators as gen

CCR_PREFIX = "<<ccr:"
CCR_SENTINEL_KEY = "_ccr_dropped"


def _canonical(item) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _multiset_sha(sigs) -> str:
    return _sha("\n".join(sorted(sigs)))


def _stringify(content) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _collect_ccr_hashes(text: str):
    hashes = set()
    idx, n = 0, len(text)
    while True:
        start = text.find(CCR_PREFIX, idx)
        if start == -1:
            return hashes
        cur = start + len(CCR_PREFIX)
        end = cur
        while end < n and text[end] in "0123456789abcdefABCDEF":
            end += 1
        if end > cur:
            hashes.add(text[cur:end].lower())
        idx = max(end, cur + 1)


def _emitted_drop_hashes(output_text: str):
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        h = set()
        if CCR_SENTINEL_KEY in output_text:
            h |= _collect_ccr_hashes(output_text)
        return h
    if isinstance(parsed, str):
        h = set()
        for line in parsed.split("\n"):
            if CCR_SENTINEL_KEY not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get(CCR_SENTINEL_KEY), str):
                h |= _collect_ccr_hashes(obj[CCR_SENTINEL_KEY])
        return h
    rows = parsed if isinstance(parsed, list) else [parsed]
    h = set()
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get(CCR_SENTINEL_KEY), str):
            h |= _collect_ccr_hashes(row[CCR_SENTINEL_KEY])
    return h


def _visible_sigs(text: str):
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(parsed, list):
        return set()
    sigs = set()
    for row in parsed:
        if isinstance(row, dict) and CCR_SENTINEL_KEY in row and len(row) == 1:
            continue
        sigs.add(_canonical(row))
    return sigs


def _decoded_sigs(text: str):
    inner = text
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, str):
        inner = parsed
    rows = decode_csv_schema_rows(inner)
    if rows is None:
        return set()
    return {_canonical(r) for r in rows}


def _ccr_sigs(hashes, query):
    store = get_compression_store()
    sigs = set()
    for h in hashes:
        entry = store.retrieve(h, query=query)
        if entry is not None and entry.original_content:
            try:
                parsed = json.loads(entry.original_content)
            except json.JSONDecodeError:
                continue
            rows = parsed if isinstance(parsed, list) else [parsed]
            for r in rows:
                sigs.add(_canonical(r))
    return sigs


def build_case(family, n, tier, seed, needles):
    repeated = family == "repeated_logs"
    if family in ("logs", "repeated_logs"):
        case = gen.gen_logs(seed, n, tier, repeated=repeated)
    elif family == "search":
        case = gen.gen_search(seed, n, tier)
    elif family == "disk":
        case = gen.gen_disk(seed, n, tier)
    elif family == "multiturn":
        case = gen.gen_multiturn(seed, n, tier)
    else:
        raise ValueError(family)
    if needles and not case.conversation and family != "code":
        case = gen.plant_needles(case, seed, k=3)
    return case


def main() -> int:
    args = sys.argv[1:]
    needles = "--needles" in args
    args = [a for a in args if a != "--needles"]
    family, n, tier, seed = args[0], int(args[1]), args[2], int(args[3])

    case = build_case(family, n, tier, seed, needles)

    reset_compression_store()  # cold CCR
    result = compress(case.messages, model="gpt-4o")  # DEFAULT params only

    texts = [_stringify(m.get("content")) for m in result.messages]
    emitted = set()
    for t in texts:
        emitted |= _emitted_drop_hashes(t)

    # STRICT reconstructable set: visible + decoded + CCR. NO scalar fallback.
    recon = set()
    for t in texts:
        recon |= _visible_sigs(t)
        recon |= _decoded_sigs(t)
    recon |= _ccr_sigs(emitted, case.query)

    original_sigs = [_canonical(it) for it in case.items]
    recon_sigs = []
    missing = []
    for sig in original_sigs:
        if sig in recon:
            recon_sigs.append(sig)
        else:
            missing.append(sig)

    original_sha = _multiset_sha(original_sigs)
    strict_sha = _multiset_sha(recon_sigs)
    strict_byte_exact = strict_sha == original_sha and not missing

    out = {
        "case": f"{family}@{n}",
        "tier": tier,
        "seed": seed,
        "needles": needles,
        "n_items": len(case.items),
        "n_emitted_ccr_drops": len(emitted),
        "n_recon_strict": len(recon_sigs),
        "n_missing_strict": len(missing),
        "original_sha": original_sha,
        "strict_recon_sha": strict_sha,
        "strict_byte_exact": strict_byte_exact,
        "missing_examples": missing[:3],
        "transforms": list(result.transforms_applied),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
