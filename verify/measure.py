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
from collections import Counter
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

# Round-trip cost model for effective-savings-under-retrieval, MEASURED against the production `furl_retrieve` surface rather than assumed.
# JSON-escaped inside the response's `original_content` field, not the raw bytes `CompressionStore.retrieve` hands a library caller.
RETRIEVE_CALL_OVERHEAD_TOKENS = 31  # {"name":"furl_retrieve","arguments":{"hash":…}}; 28-34
MCP_RESPONSE_SCAFFOLD_TOKENS = 68  # response minus escaped payload; 64-73 over 40, median 68
TOOL_RESULT_ENVELOPE_TOKENS = 7  # role/framing of the tool message; 7 in every case
# What ONE retrieval costs beyond the payload itself.
RETRIEVE_ROUND_TRIP_TOKENS = (
    RETRIEVE_CALL_OVERHEAD_TOKENS + MCP_RESPONSE_SCAFFOLD_TOKENS + TOOL_RESULT_ENVELOPE_TOKENS
)


def _tok() -> Tokenizer:
    return Tokenizer(get_tokenizer(BENCH_MODEL), BENCH_MODEL)


def retrieved_blob_tokens(blob: str, tok: Tokenizer) -> int:
    """Tokens the model pays to READ one retrieved blob.

    NOT ``count_text(blob)``. The model never sees the raw original: the MCP
    handler serialises its response with ``json.dumps(..., indent=2)``, so the
    payload arrives JSON-ESCAPED — every newline a ``\\n``, every quote a
    ``\\"`` — and escaping is not token-neutral. ``json.dumps(blob)`` performs
    the identical escaping (``indent`` does not affect string values, and both
    default to ``ensure_ascii=True``), so this term is EXACT rather than an
    approximation of the handler, and it cannot drift from the handler without
    the handler abandoning JSON.

    Measured cost of getting this wrong: charging the raw bytes undercharges by
    82 to 2185 tokens per blob, 1.94%-6.95%, always in the same direction.
    """
    return tok.count_text(json.dumps(blob))


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
    """Per-case exactness proof for the reconstruction.

    ``byte_exact`` is a CANONICAL-MULTISET equality (order-independent
    sha256 over sorted canonical row signatures) — CCR-retrieved rows carry
    no position, so a sequence claim is impossible in general. Label-honesty
    (TEST-16d): whenever the output alone yields a FULL ORDERED
    reconstruction (a visible JSON array or a decoded CSV-schema table
    covering the entire multiset with no CCR fill), the original row ORDER
    is additionally checked (``order_checked``/``order_exact``) and a
    reordered reconstruction flips ``byte_exact`` to False.
    """

    original_sha: str
    reconstructed_sha: str
    byte_exact: bool
    n_items: int
    n_reconstructed: int  # rows the output alone reproduces (visible+decoded+CCR)
    n_missing: int  # items neither visible/decoded nor CCR-recoverable
    missing_examples: tuple[str, ...]
    order_checked: bool = False  # an ordered full-surface reconstruction existed
    order_exact: bool = False  # ...and it reproduced the original sequence


def _multiset_sha(sigs: list[str]) -> str:
    """Order-independent multiset hash: sha256 over sorted canonical sigs."""
    joined = "\n".join(sorted(sigs))
    return _sha(joined)


def _ordered_surface_sigs(output_text: str) -> list[str] | None:
    """Canonical sigs of the output's rows IN OUTPUT ORDER, when the output
    has an ordered row surface: a visible JSON array (sentinel row excluded)
    or a decodable CSV-schema table. ``None`` for any other rendering."""
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, list):
        return [
            _canonical(row)
            for row in parsed
            if not (isinstance(row, dict) and CCR_SENTINEL_KEY in row and len(row) == 1)
        ]
    if isinstance(parsed, str):
        rows = decode_csv_schema_rows(parsed)
        if rows is not None:
            return [_canonical(r) for r in rows]
    return None


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
    to the original — AND, when the output alone provides a full ORDERED
    reconstruction (no CCR fill needed), the row sequence matches the original
    too (TEST-16d): a multiset-identical but REORDERED visible/decoded output
    is not byte-exact and fails here.
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

    # Ordering check (TEST-16d): only claimable when the ordered surface alone reproduces the ENTIRE
    # multiset (CCR-retrieved rows carry no position, so partial surfaces stay multiset-only).
    ordered = _ordered_surface_sigs(output_text)
    order_checked = ordered is not None and Counter(ordered) == Counter(original_sigs)
    order_exact = order_checked and ordered == original_sigs

    reconstructed_sha = _multiset_sha(recon_sigs)
    multiset_exact = reconstructed_sha == original_sha and not missing
    byte_exact = multiset_exact and (order_exact if order_checked else True)
    return HashCompare(
        original_sha=original_sha,
        reconstructed_sha=reconstructed_sha,
        byte_exact=byte_exact,
        n_items=len(items),
        n_reconstructed=len(recon_sigs),
        n_missing=len(missing),
        missing_examples=tuple(missing[:3]),
        order_checked=order_checked,
        order_exact=order_exact,
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


def effective_savings(
    tokens_before: int,
    tokens_after: int,
    recovered: dict[str, str],
    tok: Tokenizer,
    rates: tuple[float, ...] = (0.0, 0.25, 0.50),
) -> dict[str, float]:
    """Effective savings ratio once the model retrieves offloaded content,
    INCLUDING the round-trip cost.

    Every offload the engine makes is emitted behind a
    ``{"_ccr_dropped": "<<ccr:HASH>>"}`` sentinel, so ``_retrieve_originals``
    puts the WHOLE payload into ``recovered`` for every one of them. That
    sentinel invariant is load-bearing: it is why a single ``recovered``-based
    charge is COMPLETE, with no separate opaque path to read — there is no
    offload the harness sees that is absent from ``recovered``.

    ``_ccr_dropped`` is a CROSS-MODULE contract, not one function's private
    detail. THREE independent emitters construct it, one per offload path:

    * row-drop (Rust)   — ``crates/furl-core/src/transforms/smart_crusher/
      persist.rs::ccr_sentinel_map``, appended to the crushed rendering.
    * cross-message dedup — ``furl_ctx/transforms/cross_message_dedup.py::
      duplicate_sentinel`` (exact duplicate) and ``::near_duplicate_rendering``
      (shared rows elided).
    * opaque whole-blob  — ``furl_ctx/transforms/router_engine.py::_ccr_offload``.

    and the key is READ back by ``furl_ctx/transforms/smart_crusher.py`` and
    ``furl_ctx/transforms/csv_schema_decoder.py``. All three emitters are pinned
    by real ``compress()`` calls in
    ``tests/test_effective_savings_offload_cost.py``, so a bare-marker offload
    on ANY of the three turns that suite RED instead of being silently
    mispriced here.

    Each offload sits behind ONE marker with no granular row index, so ANY
    non-zero retrieval pulls the ENTIRE payload back:

        retrieval_cost(r > 0) = recovered_payload_tokens
                              + n_offloaded_blobs * per_call_overhead
        retrieval_cost(0)     = 0

    effective_after = tokens_after + retrieval_cost; savings =
    (before - effective_after) / before. At r=0 cost is 0 (savings == raw
    reduction); at r>0 you pay back the whole offloaded payload.

    The gate is ``r > 0``, not the old ``k > 0``. A family that offloads with
    ZERO dropped rows — cross-message dedup, or an opaque code blob — has k=0 at
    every rate, so the old gate charged it nothing and it read flat across rates
    (raw reduction reported as the effective number at 25% and 50%, a retrieval
    that costs a full payload priced at zero). For a row-DROPPING family
    ``k > 0`` iff ``r > 0``, so those families' numbers are unchanged.

    A granular per-row model was once also computed here (charging only the
    ``k`` retrieved chunks), but the unconsumable ``_ccr_rows`` index it read was
    removed (F8, PR #168): no output emits a row index and no per-row chunk is
    stored, so whole-blob is the only honest cost. This is why eff@25 / eff@50
    read lower than pre-removal figures — those measured a per-row retrieval the
    model could never perform.

    THE CALL TERM FOLLOWS THE SAME RULE, and until now it did not. #179 fixed the
    CONTENT half and left ``call_cost = k * overhead`` — k SEPARATE retrievals —
    alive next to a docstring saying only whole-blob retrieval exists. One marker
    is one call, whatever ``r`` is, so the charge is ONE call per offloaded blob.
    Measured consequences of the old term: it OVERCHARGED row-drop families
    (logs@900 paid k=300 calls at r=0.5, 3600 tokens for retrievals that cannot
    be issued separately) and UNDERCHARGED zero-drop ones (k=0, so dedup and
    opaque paid for no call at all).

    A VISIBLE CONSEQUENCE: eff@25 now EQUALS eff@50 for every family. That is not
    a bug in the table, it is the rate axis telling the truth — any non-zero
    fraction pulls the whole blob through one call, so 25% and 50% cost the same
    and only 0-vs-non-zero is a real distinction. The old spread between the two
    columns was entirely the granular call term.

    THE OTHER HALF OF THE SAME ERROR, AND IT PUSHES THE OPPOSITE WAY. The call
    term above overcharged row-dropping families; the CONTENT term undercharged
    everyone, because ``recovered`` holds what ``CompressionStore.retrieve``
    returns — the byte-exact original — and no model ever receives that. A model
    receives the MCP ``furl_retrieve`` response, in which the payload sits
    JSON-ESCAPED inside a field, wrapped in scaffolding, inside a tool message,
    after the model has spent tokens emitting the call. Measured over 18 blobs
    from 76 to 75530 content tokens, charging the raw bytes undercharged by
    82-2185 tokens per blob (1.94%-6.95%), never once in the other direction.
    ``retrieved_blob_tokens`` now charges the escaped form, and
    ``RETRIEVE_ROUND_TRIP_TOKENS`` the rest.

    A fixed fudge factor was NOT good enough here and the numbers say why: the
    gap ranges over 2103 tokens across those blobs and a least-squares fit in
    content size still leaves 945 tokens of residual, because escaping cost
    depends on how many quotes and newlines a payload contains, not on its size.
    Recomputing the escaping is what makes the term exact.

    REMAINING KNOWN BIAS, stated because it is not modelled: the charge assumes a
    hash-only retrieve, the call that works. The marker text, the tool schema and
    this harness all invite a ``query`` argument, and on the MCP surface that
    argument currently returns nothing (#47) — so a model following the prompt
    pays for one wasted round trip before the one charged here. That is an open
    product question about which surface is correct, not a constant to add, so
    these figures remain an upper bound on savings by one round trip per blob.
    """
    out: dict[str, float] = {}
    # The WHOLE retrieved payload, priced as the model receives it (JSON-escaped inside the response) rather than as the library hands it over.
    recovered_payload_tokens = sum(retrieved_blob_tokens(b, tok) for b in recovered.values())
    for r in rates:
        if r > 0.0:
            content_cost = recovered_payload_tokens
            # ONE round trip per offloaded blob.
            call_cost = len(recovered) * RETRIEVE_ROUND_TRIP_TOKENS
        else:
            content_cost = 0
            call_cost = 0
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
    eff = effective_savings(tb, ta, recovered, tok)

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

    eff = effective_savings(tb, ta, recovered, tok)

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
    eff = effective_savings(tb, ta, recovered, tok)
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
