"""Measurement core for the independent verifier.

EVERY number here is produced by the engine's OWN public surface:

* compression           — ``furl_ctx.compress`` (default config / params).
* token counting        — ``furl_ctx.tokenizer.Tokenizer`` over
                          ``furl_ctx.tokenizers.get_tokenizer`` (gpt-4o => real
                          tiktoken BPE, the tokenizer the dev numbers used).
* CSV-schema decode     — ``furl_ctx.transforms.csv_schema_decoder
                          .decode_csv_schema_rows`` (the documented decoder).
* CCR retrieve          — ``furl_ctx.cache.compression_store`` retrieve, keyed
                          by the ``<<ccr:HASH>>`` pointer parsed out of the
                          compressed output.

We DO NOT re-implement compression or hand-roll a decoder. We DO NOT tune
anything to the data. Cold CCR state per case via ``reset_compression_store()``.

The reconstruction contract under test: a consumer holding ONLY the compressed
output reconstructs every original row. "recoverable=100%" is TRUE only when
the sha256 of the canonicalized reconstruction equals the sha256 of the
canonicalized original — proven per case, never asserted.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from furl_ctx import compress
from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows

# Same tokenizer the dev numbers used.
BENCH_MODEL = "gpt-4o"
CCR_PREFIX = "<<ccr:"
CCR_SENTINEL_KEY = "_ccr_dropped"

# Round-trip overhead model for effective-savings-under-retrieval. A retrieval
# the model issues costs (a) a tool-call to fetch the dropped blob and (b) the
# retrieved content's tokens re-entering context. We charge a fixed per-call
# overhead plus the real token cost of the retrieved original.
RETRIEVE_CALL_OVERHEAD_TOKENS = 12  # tool name + hash argument, conservative


def _tok() -> Tokenizer:
    return Tokenizer(get_tokenizer(BENCH_MODEL), BENCH_MODEL)


def _canonical(item: Any) -> str:
    """Canonical JSON for hashing/presence — key order independent."""
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CCR pointer parsing (mirrors the engine's substring grammar) + retrieval.
# ---------------------------------------------------------------------------


def _collect_ccr_hashes(text: str) -> set[str]:
    """Extract every ``<<ccr:HEX...>>`` hash from ``text`` (engine grammar)."""
    hashes: set[str] = set()
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


def _emitted_drop_hashes(output_text: str) -> set[str]:
    """CCR hashes the ENGINE emitted as DROP SENTINELS only.

    A drop is SIGNALLED only by a ``{"_ccr_dropped": "<<ccr:HASH ...>>"}``
    sentinel — markers that merely appear inside an input value do not count.
    This is exactly the engine's own sentinel grammar.
    """
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        hashes: set[str] = set()
        if CCR_SENTINEL_KEY in output_text:
            hashes |= _collect_ccr_hashes(output_text)
        return hashes
    if isinstance(parsed, str):
        hashes = set()
        for line in parsed.split("\n"):
            if CCR_SENTINEL_KEY not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get(CCR_SENTINEL_KEY), str):
                hashes |= _collect_ccr_hashes(obj[CCR_SENTINEL_KEY])
        return hashes
    rows = parsed if isinstance(parsed, list) else [parsed]
    hashes = set()
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get(CCR_SENTINEL_KEY), str):
            hashes |= _collect_ccr_hashes(row[CCR_SENTINEL_KEY])
    return hashes


def _find_sentinel_object(output_text: str) -> dict[str, Any] | None:
    """Locate the ``{"_ccr_dropped": ..., "_ccr_rows": ...}`` sentinel object
    in a compressed output (a JSON array/object tree, or a JSON-string-wrapped
    CSV-schema table whose final line is the sentinel)."""

    def walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            if "_ccr_dropped" in node:
                return node
            for v in node.values():
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for x in node:
                found = walk(x)
                if found is not None:
                    return found
        return None

    try:
        tree = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        return None
    found = walk(tree)
    if found is not None:
        return found
    if isinstance(tree, str):
        last_line = tree.strip().rsplit("\n", 1)[-1]
        try:
            obj = json.loads(last_line)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(obj, dict) and "_ccr_dropped" in obj:
            return obj
    return None


def _marker_key(marker: str) -> str | None:
    """Pull the key out of ``<<ccr:KEY <sep>...>>`` (KEY may carry a ``#rows``
    suffix for the granular row index)."""
    start = marker.find(CCR_PREFIX)
    if start == -1:
        return None
    rest = marker[start + len(CCR_PREFIX) :]
    end = len(rest)
    for sep in (" ", ",", ">>"):
        pos = rest.find(sep)
        if pos != -1:
            end = min(end, pos)
    key = rest[:end]
    return key or None


def _active_crusher() -> Any:
    """The SmartCrusher on the live singleton pipeline ``compress()`` just used
    — its Rust CCR store holds the per-row chunks Frontier A wrote. Reaching it
    via the engine's own public ``ccr_get`` is exactly what the engine's
    proportional-retrieval test does. Returns ``None`` if unreachable."""
    try:
        from furl_ctx.compress import _get_pipeline

        pipeline = _get_pipeline()
        for transform in getattr(pipeline, "transforms", []):
            if type(transform).__name__ == "ContentRouter":
                return transform._get_smart_crusher()
    except Exception:  # pragma: no cover - defensive; falls back to whole-blob
        return None
    return None


def _retrieve_originals(hashes: set[str], query: str | None) -> dict[str, str]:
    """Retrieve original content per hash from the engine's CCR store."""
    store = get_compression_store()
    out: dict[str, str] = {}
    for h in hashes:
        entry = store.retrieve(h, query=query)
        if entry is not None and entry.original_content:
            out[h] = entry.original_content
    return out


# ---------------------------------------------------------------------------
# Reconstruct distinct rows from the compressed output ALONE.
# ---------------------------------------------------------------------------


def _visible_row_sigs(output_text: str) -> set[str] | None:
    """Canonical sigs of rows visible in a JSON-array rendering (or None)."""
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    sigs: set[str] = set()
    for row in parsed:
        if isinstance(row, dict) and CCR_SENTINEL_KEY in row and len(row) == 1:
            continue
        sigs.add(_canonical(row))
    return sigs


def _decoded_row_sigs(output_text: str) -> set[str] | None:
    """Canonical sigs reconstructed from a CSV-schema rendering (or None)."""
    text = output_text
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, str):
        text = parsed
    rows = decode_csv_schema_rows(text)
    if rows is None:
        return None
    return {_canonical(r) for r in rows}


def _recovered_row_sigs(recovered: dict[str, str]) -> set[str]:
    """Canonical sigs of every row recoverable from CCR-retrieved blobs."""
    sigs: set[str] = set()
    for blob in recovered.values():
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        rows = parsed if isinstance(parsed, list) else [parsed]
        for row in rows:
            sigs.add(_canonical(row))
    return sigs


@dataclass(frozen=True)
class HashCompare:
    """Per-case byte-exactness proof for the reconstruction."""

    original_sha: str
    reconstructed_sha: str
    byte_exact: bool
    n_items: int
    n_reconstructed: int  # rows the output alone reproduces (visible+decoded+CCR)
    n_missing: int  # items neither visible/decoded nor CCR-recoverable
    missing_examples: tuple[str, ...]


def _multiset_sha(sigs: list[str]) -> str:
    """Order-independent multiset hash: sha256 over sorted canonical sigs."""
    joined = "\n".join(sorted(sigs))
    return _sha(joined)


def hash_compare_structured(
    items: list[Any], output_text: str, recovered: dict[str, str]
) -> HashCompare:
    """Reconstruct the original item multiset from the compressed output ALONE
    (visible rows + CSV-schema-decoded rows + CCR-retrieved rows) and compare
    its sha256 against the original item multiset's sha256.

    STRICT by default: a row counts as reconstructed ONLY when its canonical
    signature is produced by a documented recovery surface — visible verbatim,
    decoded by ``decode_csv_schema_rows``, or retrieved from the CCR store via
    the ``<<ccr:HASH>>`` pointer. There is NO lenient scalar-substring fallback:
    an item whose scalars merely appear scattered in the text does NOT count, so
    a non-round-tripping item FAILS (it lands in ``missing`` and flips
    ``byte_exact`` to ``False``). This makes the harness's headline lossless
    measurement the same strict round-trip ``strict_recheck.py`` performs.

    byte_exact is True ONLY when the reconstructed multiset hashes identically
    to the original — the strict "recoverable=100%" proof.
    """
    original_sigs = [_canonical(it) for it in items]
    original_sha = _multiset_sha(original_sigs)

    visible = _visible_row_sigs(output_text)
    decoded = _decoded_row_sigs(output_text)
    ccr_sigs = _recovered_row_sigs(recovered)

    reconstructable: set[str] = set()
    reconstructable |= ccr_sigs
    if visible is not None:
        reconstructable |= visible
    if decoded is not None:
        reconstructable |= decoded

    # Match each original item ONLY to a documented-recovery signature. No
    # scalar-substring fallback: a non-round-tripping item lands in `missing`.
    recon_sigs: list[str] = []
    missing: list[str] = []
    for sig in original_sigs:
        if sig in reconstructable:
            recon_sigs.append(sig)
        else:
            missing.append(sig)

    reconstructed_sha = _multiset_sha(recon_sigs)
    byte_exact = reconstructed_sha == original_sha and not missing
    return HashCompare(
        original_sha=original_sha,
        reconstructed_sha=reconstructed_sha,
        byte_exact=byte_exact,
        n_items=len(items),
        n_reconstructed=len(recon_sigs),
        n_missing=len(missing),
        missing_examples=tuple(missing[:3]),
    )


def hash_compare_code(items: list[str], result_messages: list[dict[str, Any]]) -> HashCompare:
    """Code case: each source blob must survive byte-exact across the output.

    Code rows are strings; presence is exact-substring of the full source in
    SOME compressed message (a passthrough keeps them verbatim). CCR is not
    expected for code; if the engine dropped a blob it must be substring-
    recoverable from a sentinel-retrieved original (handled by caller via the
    recovered map merged into the joined text).
    """
    joined = "\n".join(_stringify(m.get("content")) for m in result_messages)
    original_sigs = [_sha(s) for s in items]
    original_sha = _multiset_sha(original_sigs)
    recon_sigs: list[str] = []
    missing: list[str] = []
    for src, sig in zip(items, original_sigs):
        if src in joined:
            recon_sigs.append(sig)
        else:
            missing.append(sig)
    reconstructed_sha = _multiset_sha(recon_sigs)
    byte_exact = reconstructed_sha == original_sha and not missing
    return HashCompare(
        original_sha=original_sha,
        reconstructed_sha=reconstructed_sha,
        byte_exact=byte_exact,
        n_items=len(items),
        n_reconstructed=len(recon_sigs),
        n_missing=len(missing),
        missing_examples=tuple(missing[:3]),
    )


# ---------------------------------------------------------------------------
# Effective savings under retrieval at {0%, 25%, 50%}.
# ---------------------------------------------------------------------------


def per_row_chunk_tokens(output_text: str, tok: Tokenizer) -> list[int] | None:
    """REAL per-row retrieval cost (Frontier A's granular offload).

    The granular CCR offload (commit cbf16a85) stores every dropped row as its
    own individually-addressable canonical 1-element chunk and surfaces a single
    ``_ccr_rows`` index marker (``<<ccr:HASH#rows N_chunks>>``). Retrieving ONE
    row now fetches exactly that one row's chunk — not the whole blob — so the
    retrieval cost is the sum of the ACTUAL chunk payloads pulled, not a
    proportional slice of one monolithic blob.

    We resolve the index and each per-row chunk through the engine's OWN
    ``ccr_get`` surface (identical to ``tests/test_ccr_proportional_retrieval``),
    and token-count the chunk bytes the engine would actually serve back.
    Returns the per-chunk token sizes (one per dropped row), or ``None`` if the
    output carries no granular row index (nothing offloaded / no store).
    """
    sentinel = _find_sentinel_object(output_text)
    if sentinel is None or "_ccr_rows" not in sentinel:
        return None
    index_key = _marker_key(sentinel["_ccr_rows"])
    if index_key is None:
        return None
    crusher = _active_crusher()
    if crusher is None:
        return None
    index_raw = crusher.ccr_get(index_key)
    if index_raw is None:
        return None
    try:
        row_hashes = json.loads(index_raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(row_hashes, list):
        return None
    sizes: list[int] = []
    for rh in row_hashes:
        if not isinstance(rh, str):
            continue
        chunk = crusher.ccr_get(rh)
        if chunk is None:
            return None  # an unresolvable chunk => cannot model granular cost
        sizes.append(tok.count_text(chunk))
    return sizes


def effective_savings(
    tokens_before: int,
    tokens_after: int,
    recovered: dict[str, str],
    tok: Tokenizer,
    rates: tuple[float, ...] = (0.0, 0.25, 0.50),
    n_dropped_rows: int = 0,
    chunk_tokens: list[int] | None = None,
) -> dict[str, float]:
    """Effective savings ratio once the model retrieves a fraction of the
    DROPPED ROWS, INCLUDING round-trip overhead — REAL granular-retrieval cost.

    Now that the engine offloads each dropped row as its own addressable chunk
    (Frontier A, commit cbf16a85), retrieving ``k`` rows costs only those ``k``
    chunks. We charge the ACTUAL bytes of the ``k`` LARGEST chunks (worst-case
    the model needs the biggest rows first) plus a per-call overhead — exactly
    the model the engine's own ``test_ccr_proportional_retrieval`` asserts:

        k = ceil(r * n_dropped_rows)
        retrieval_cost(r) = sum(sorted(chunk_tokens, desc)[:k])
                            + k * per_call_overhead

    effective_after = tokens_after + retrieval_cost; savings =
    (before - effective_after) / before. At r=0 cost is 0 (savings == raw
    reduction); at r=1 you pay back every offloaded chunk.

    Fallback: if the granular row index is unavailable (``chunk_tokens is
    None``) we fall back to the conservative WHOLE-BLOB model — ANY non-zero
    retrieval pays the entire offloaded payload — which is the pre-granular
    worst case and never flatters the engine.
    """
    out: dict[str, float] = {}
    if chunk_tokens is not None:
        ordered = sorted(chunk_tokens, reverse=True)
        for r in rates:
            k = int(math.ceil(r * n_dropped_rows))
            content_cost = sum(ordered[:k])
            call_cost = k * RETRIEVE_CALL_OVERHEAD_TOKENS
            effective_after = tokens_after + content_cost + call_cost
            savings = (tokens_before - effective_after) / tokens_before if tokens_before else 0.0
            out[f"{int(r * 100)}"] = savings
        return out

    total_offloaded_tokens = sum(tok.count_text(blob) for blob in recovered.values())
    for r in rates:
        k = int(math.ceil(r * n_dropped_rows))
        content_cost = total_offloaded_tokens if k > 0 else 0
        call_cost = k * RETRIEVE_CALL_OVERHEAD_TOKENS
        effective_after = tokens_after + content_cost + call_cost
        savings = (tokens_before - effective_after) / tokens_before if tokens_before else 0.0
        out[f"{int(r * 100)}"] = savings
    return out


# ---------------------------------------------------------------------------
# Needle survival + signal detection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NeedleOutcome:
    index: int
    marker: str
    visible: bool  # survives uncompressed (verbatim in output)
    recoverable: bool  # retrievable from CCR
    signalled: bool  # a <<ccr:HASH>> sentinel the model would SEE points to it
    silent_loss: bool  # dropped, not visible, not signalled  => SILENT data loss


def needle_outcomes(
    case_items: list[Any],
    needle_indices: tuple[int, ...],
    needle_markers: list[dict[str, Any]],
    output_text: str,
    recovered: dict[str, str],
    emitted_hashes: set[str],
) -> list[NeedleOutcome]:
    """Per-needle survival/signal classification.

    A needle is:
      visible      — its unique marker string appears verbatim in the output.
      recoverable  — a CCR-retrieved original contains the needle row.
      signalled    — the output carries a {"_ccr_dropped": "<<ccr:HASH>>"}
                     sentinel whose HASH resolves to a blob containing it.
      silent_loss  — NOT visible AND NOT signalled (the model can neither see
                     it nor know to retrieve it) => unsignalled drop.
    """
    out: list[NeedleOutcome] = []
    recovered_blobs = list(recovered.values())
    for nd in needle_markers:
        marker = _extract_marker(nd)
        sig = _canonical(nd)
        visible = marker in output_text
        # recoverable: needle row reconstructs from some retrieved blob
        recoverable = any(_row_in_blob(sig, blob) for blob in recovered_blobs)
        # signalled: the needle is inside a blob whose hash is an emitted
        # drop sentinel present in the output.
        signalled = False
        for h in emitted_hashes:
            blob = recovered.get(h)
            if blob is not None and _row_in_blob(sig, blob):
                signalled = True
                break
        silent_loss = (not visible) and (not signalled)
        out.append(
            NeedleOutcome(
                index=-1,
                marker=marker,
                visible=visible,
                recoverable=recoverable,
                signalled=signalled,
                silent_loss=silent_loss,
            )
        )
    return out


def _extract_marker(nd: dict[str, Any]) -> str:
    for fld in ("message", "match", "msg", "name", "needle"):
        if fld in nd and isinstance(nd[fld], str) and nd[fld].startswith("NEEDLE-"):
            return nd[fld]
    # fall back to any NEEDLE- value
    for v in nd.values():
        if isinstance(v, str) and v.startswith("NEEDLE-"):
            return v
    return _canonical(nd)


def _row_in_blob(sig: str, blob: str) -> bool:
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return sig in blob
    rows = parsed if isinstance(parsed, list) else [parsed]
    return any(_canonical(r) == sig for r in rows)


# ---------------------------------------------------------------------------
# Multiturn cache-prefix safety.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachePrefixCheck:
    prefix_len: int
    preserved_in_order: bool
    index0_intact: bool
    dropped_indices: tuple[int, ...]
    reordered: bool


def check_cache_prefix(
    original_messages: list[dict[str, Any]],
    result_messages: list[dict[str, Any]],
    prefix_texts: list[str],
) -> CachePrefixCheck:
    """Verify the cached prefix (leading messages) is neither dropped nor
    reordered in the compressed output.

    The prefix is identified by the EXACT content strings of the leading
    messages captured at generation time. We require each prefix text to
    appear, in order, at the SAME leading positions of the output.
    """
    out_texts = [_stringify(m.get("content")) for m in result_messages]
    dropped: list[int] = []
    positions: list[int] = []
    for i, ptext in enumerate(prefix_texts):
        # exact-match (prefix messages are not compressed targets when intact)
        found = -1
        for j, ot in enumerate(out_texts):
            if ot == ptext:
                found = j
                break
        if found == -1:
            dropped.append(i)
        else:
            positions.append(found)
    index0_intact = bool(out_texts) and bool(prefix_texts) and out_texts[0] == prefix_texts[0]
    reordered = positions != sorted(positions) or (positions and positions[0] != 0)
    preserved = not dropped and not reordered
    return CachePrefixCheck(
        prefix_len=len(prefix_texts),
        preserved_in_order=preserved,
        index0_intact=index0_intact,
        dropped_indices=tuple(dropped),
        reordered=reordered,
    )


# ---------------------------------------------------------------------------
# Top-level per-case measurement.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    family: str
    tier: str
    size: int
    seed: int
    transforms: tuple[str, ...]
    took_lossy_path: bool
    tokens_before: int
    tokens_after: int
    token_reduction: float
    n_items: int
    n_visible: int
    n_dropped: int
    n_ccr_recoverable: int
    information_retention: float
    hash_byte_exact: bool
    hash_original: str
    hash_reconstructed: str
    n_missing: int
    missing_examples: tuple[str, ...]
    effective_savings: dict[str, float] = field(default_factory=dict)
    needles: list[dict[str, Any]] = field(default_factory=list)
    cache_prefix: dict[str, Any] | None = None
    used_default_params: bool = True


def measure(case: Any) -> CaseResult:
    """Run one case end-to-end on a COLD CCR store with DEFAULT params.

    No config object, no kwargs => committed CompressConfig defaults and the
    committed RoutingPolicy default (MinTokens). Any deviation would show in
    transforms / a non-default would have to be passed explicitly (we pass
    none).
    """
    reset_compression_store()  # cold cache, no warm state carried in
    tok = _tok()

    result = compress(case.messages, model=BENCH_MODEL)  # DEFAULT params only
    transforms = tuple(result.transforms_applied)

    tokens_before = result.tokens_before or tok.count_messages(case.messages)
    tokens_after = result.tokens_after or tok.count_messages(result.messages)
    token_reduction = (tokens_before - tokens_after) / tokens_before if tokens_before else 0.0

    if case.family == "code":
        return _measure_code(
            case, result, transforms, tokens_before, tokens_after, token_reduction, tok
        )
    if case.conversation:
        return _measure_conversation(
            case, result, transforms, tokens_before, tokens_after, token_reduction, tok
        )
    return _measure_structured(
        case, result, transforms, tokens_before, tokens_after, token_reduction, tok
    )


def _measure_structured(case, result, transforms, tb, ta, tr, tok) -> CaseResult:
    output_text = _stringify(result.messages[-1].get("content"))
    emitted = _emitted_drop_hashes(output_text)
    recovered = _retrieve_originals(emitted, case.query)

    visible = _visible_row_sigs(output_text)
    decoded = _decoded_row_sigs(output_text)
    recon = set()
    if visible is not None:
        recon |= visible
    if decoded is not None:
        recon |= decoded
    ccr_sigs = _recovered_row_sigs(recovered)

    n_visible = n_dropped = n_recoverable = 0
    for it in case.items:
        sig = _canonical(it)
        if sig in recon:
            n_visible += 1
        else:
            n_dropped += 1
            if sig in ccr_sigs:
                n_recoverable += 1

    n = len(case.items)
    retention = (n_visible + n_recoverable) / n if n else 1.0

    hc = hash_compare_structured(case.items, output_text, recovered)
    chunks = per_row_chunk_tokens(output_text, tok)
    eff = effective_savings(tb, ta, recovered, tok, n_dropped_rows=n_dropped, chunk_tokens=chunks)

    needles: list[dict[str, Any]] = []
    markers = case.meta.get("needle_markers", [])
    if markers:
        outcomes = needle_outcomes(
            case.items, case.needle_indices, markers, output_text, recovered, emitted
        )
        needles = [
            {
                "marker": o.marker,
                "visible": o.visible,
                "recoverable": o.recoverable,
                "signalled": o.signalled,
                "silent_loss": o.silent_loss,
            }
            for o in outcomes
        ]

    return CaseResult(
        family=case.family,
        tier=case.tier,
        size=case.size,
        seed=case.seed,
        transforms=transforms,
        took_lossy_path=n_dropped > 0,
        tokens_before=tb,
        tokens_after=ta,
        token_reduction=tr,
        n_items=n,
        n_visible=n_visible,
        n_dropped=n_dropped,
        n_ccr_recoverable=n_recoverable,
        information_retention=retention,
        hash_byte_exact=hc.byte_exact,
        hash_original=hc.original_sha,
        hash_reconstructed=hc.reconstructed_sha,
        n_missing=hc.n_missing,
        missing_examples=hc.missing_examples,
        effective_savings=eff,
        needles=needles,
    )


def _measure_conversation(case, result, transforms, tb, ta, tr, tok) -> CaseResult:
    texts = [_stringify(m.get("content")) for m in result.messages]
    emitted: set[str] = set()
    for t in texts:
        emitted |= _emitted_drop_hashes(t)
    recovered = _retrieve_originals(emitted, case.query)

    views = []
    for t in texts:
        views.append((t, _visible_row_sigs(t), _decoded_row_sigs(t)))
    ccr_sigs = _recovered_row_sigs(recovered)

    n_visible = n_dropped = n_recoverable = 0
    for it in case.items:
        sig = _canonical(it)
        seen = False
        for _t, vs, ds in views:
            if vs is not None and sig in vs:
                seen = True
                break
            if ds is not None and sig in ds:
                seen = True
                break
        if seen:
            n_visible += 1
        else:
            n_dropped += 1
            if sig in ccr_sigs:
                n_recoverable += 1

    n = len(case.items)
    retention = (n_visible + n_recoverable) / n if n else 1.0

    # Hash-compare across the whole transcript (visible+decoded+CCR per msg).
    joined_recon: set[str] = set(ccr_sigs)
    for _t, vs, ds in views:
        if vs is not None:
            joined_recon |= vs
        if ds is not None:
            joined_recon |= ds
    original_sigs = [_canonical(it) for it in case.items]
    recon_sigs: list[str] = []
    missing: list[str] = []
    for sig in original_sigs:
        if sig in joined_recon:
            recon_sigs.append(sig)
        else:
            missing.append(sig)
    original_sha = _multiset_sha(original_sigs)
    reconstructed_sha = _multiset_sha(recon_sigs)
    byte_exact = reconstructed_sha == original_sha and not missing

    chunks = None
    for t in texts:
        chunks = per_row_chunk_tokens(t, tok)
        if chunks is not None:
            break
    eff = effective_savings(tb, ta, recovered, tok, n_dropped_rows=n_dropped, chunk_tokens=chunks)

    cp = check_cache_prefix(case.messages, result.messages, case.meta.get("cache_prefix_texts", []))
    cache_prefix = {
        "prefix_len": cp.prefix_len,
        "preserved_in_order": cp.preserved_in_order,
        "index0_intact": cp.index0_intact,
        "dropped_indices": list(cp.dropped_indices),
        "reordered": cp.reordered,
    }

    return CaseResult(
        family=case.family,
        tier=case.tier,
        size=case.size,
        seed=case.seed,
        transforms=transforms,
        took_lossy_path=n_dropped > 0,
        tokens_before=tb,
        tokens_after=ta,
        token_reduction=tr,
        n_items=n,
        n_visible=n_visible,
        n_dropped=n_dropped,
        n_ccr_recoverable=n_recoverable,
        information_retention=retention,
        hash_byte_exact=byte_exact,
        hash_original=original_sha,
        hash_reconstructed=reconstructed_sha,
        n_missing=len(missing),
        missing_examples=tuple(missing[:3]),
        effective_savings=eff,
        needles=[],
        cache_prefix=cache_prefix,
    )


def _measure_code(case, result, transforms, tb, ta, tr, tok) -> CaseResult:
    # Code: merge any CCR-recovered originals into the joined text so dropped-
    # but-recoverable blobs count as reconstructed.
    texts = [_stringify(m.get("content")) for m in result.messages]
    emitted: set[str] = set()
    for t in texts:
        emitted |= _emitted_drop_hashes(t)
    recovered = _retrieve_originals(emitted, case.query)
    merged_messages = list(result.messages) + [
        {"role": "tool", "content": blob} for blob in recovered.values()
    ]
    hc = hash_compare_code(case.items, merged_messages)
    n = len(case.items)
    n_visible = hc.n_reconstructed
    n_dropped = n - n_visible
    retention = n_visible / n if n else 1.0
    eff = effective_savings(tb, ta, recovered, tok, n_dropped_rows=n_dropped)
    return CaseResult(
        family=case.family,
        tier=case.tier,
        size=case.size,
        seed=case.seed,
        transforms=transforms,
        took_lossy_path=n_dropped > 0,
        tokens_before=tb,
        tokens_after=ta,
        token_reduction=tr,
        n_items=n,
        n_visible=n_visible,
        n_dropped=n_dropped,
        n_ccr_recoverable=max(0, n_visible - sum(1 for s in case.items if s in "\n".join(texts))),
        information_retention=retention,
        hash_byte_exact=hc.byte_exact,
        hash_original=hc.original_sha,
        hash_reconstructed=hc.reconstructed_sha,
        n_missing=hc.n_missing,
        missing_examples=hc.missing_examples,
        effective_savings=eff,
        needles=[],
    )
