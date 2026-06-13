"""INDEPENDENT recheck — does NOT trust the harness's _present_in_text scalar
fallback. Reconstructs the original item multiset using ONLY:

  (a) rows visible verbatim in a JSON-array rendering of the compressed output,
  (b) rows decoded by the documented decoder decode_csv_schema_rows, and
  (c) rows retrieved from the CCR store via the <<ccr:HASH>> sentinel.

Then sha256-compares the reconstructed multiset to the original multiset. This
is the STRICT round-trip the mandate demands. If a case is byte_exact under the
harness but NOT here, the harness's substring fallback is inflating recovery.

Usage: python -m verify.independent_recheck '<json-spec>'
"""
from __future__ import annotations

import hashlib
import json
import sys

from headroom import compress
from headroom.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from headroom.transforms.csv_schema_decoder import decode_csv_schema_rows

from verify import generators as gen
from verify.worker import build_case

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


def _visible_sigs(output_text: str):
    try:
        parsed = json.loads(output_text)
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


def _decoded_sigs(output_text: str):
    text = output_text
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, str):
        text = parsed
    rows = decode_csv_schema_rows(text)
    if rows is None:
        return set()
    return {_canonical(r) for r in rows}


def _ccr_sigs(recovered):
    sigs = set()
    for blob in recovered.values():
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        rows = parsed if isinstance(parsed, list) else [parsed]
        for row in rows:
            sigs.add(_canonical(row))
    return sigs


def recheck(spec: dict) -> dict:
    case = build_case(spec)
    reset_compression_store()
    result = compress(case.messages, model="gpt-4o")

    # gather output text across all result messages (covers multiturn/code too)
    texts = [_stringify(m.get("content")) for m in result.messages]
    emitted = set()
    for t in texts:
        emitted |= _emitted_drop_hashes(t)

    store = get_compression_store()
    recovered = {}
    for h in emitted:
        entry = store.retrieve(h, query=case.query)
        if entry is not None and entry.original_content:
            recovered[h] = entry.original_content

    # STRICT reconstructable set: visible + decoded + CCR ONLY. No scalar fallback.
    reconstructable = set()
    for t in texts:
        reconstructable |= _visible_sigs(t)
        reconstructable |= _decoded_sigs(t)
    reconstructable |= _ccr_sigs(recovered)

    if case.family == "code":
        # code items are strings; presence = exact substring of joined output+CCR
        joined = "\n".join(texts) + "\n" + "\n".join(recovered.values())
        orig = [_sha(s) for s in case.items]
        recon, missing = [], []
        for src, sig in zip(case.items, orig):
            (recon if src in joined else missing).append(sig)
        oh = _multiset_sha(orig)
        rh = _multiset_sha(recon)
        return {
            "spec": spec, "family": "code",
            "original_sha": oh, "reconstructed_sha": rh,
            "strict_byte_exact": (oh == rh and not missing),
            "n_items": len(case.items), "n_missing": len(missing),
            "emitted_ccr": len(emitted), "ccr_resolved": len(recovered),
        }

    orig = [_canonical(it) for it in case.items]
    recon, missing = [], []
    for sig in orig:
        if sig in reconstructable:
            recon.append(sig)
        else:
            missing.append(sig)
    oh = _multiset_sha(orig)
    rh = _multiset_sha(recon)
    return {
        "spec": spec, "family": case.family,
        "original_sha": oh, "reconstructed_sha": rh,
        "strict_byte_exact": (oh == rh and not missing),
        "n_items": len(case.items), "n_missing": len(missing),
        "missing_examples": missing[:3],
        "emitted_ccr": len(emitted), "ccr_resolved": len(recovered),
        "transforms": list(result.transforms_applied),
    }


def main() -> int:
    spec = json.loads(sys.argv[1])
    print(json.dumps(recheck(spec)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
