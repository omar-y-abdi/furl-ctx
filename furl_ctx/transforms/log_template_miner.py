"""Drain-style log-template miner (pure, deterministic).

Given a list of tokenised log lines, this module groups structurally similar
lines into *templates*: a fixed token sequence with some positions marked as
variable (wildcard) parameters.  It is a faithful-in-spirit port of the Drain
algorithm (He et al., "Drain: An Online Log Parsing Approach", ICWS 2017),
adapted for a LOSSLESS setting:

* Classic Drain normalises whitespace and discards it; we do NOT.  Tokenisation
  is ``re.findall(r"\\s+|\\S+", content)`` (done by the caller) so that
  ``"".join(tokens) == content`` byte-exactly.  Whitespace runs are ordinary
  tokens and participate in matching like any other token.
* Classic Drain keeps only the template; we additionally record, per matched
  line, the EXACT token at every wildcard position, so the original line is
  reconstructable from ``(template, params)``.

Purity / determinism:
* No randomness, no wall-clock, no I/O, no global mutable state.
* Output depends ONLY on the input token lists and their order.
* Template ids are assigned in first-appearance order.
* The Drain heuristics (length bucket, fixed-depth prefix tree, similarity
  threshold) only decide WHICH existing cluster a line is compared against.
  They can affect the compression ratio but never correctness: losslessness is
  guaranteed downstream by exact per-position parameter capture plus the
  encoder's decode-and-compare self-check.

Design of the constants (module-level, with rationale):
* ``PREFIX_TREE_DEPTH = 4`` — Drain's fixed parse-tree depth.  The first
  ``depth`` non-empty leading tokens index the tree, bounding per-line search to
  clusters that share a length bucket AND a leading-token prefix.  Depth 4 is
  the Drain paper's default and balances tree fan-out against cluster purity.
* ``SIMILARITY_THRESHOLD = 0.5`` — a candidate line joins a cluster when at
  least this FRACTION of same-index positions hold identical tokens; otherwise a
  new template is created.  0.5 is the Drain default ``st``.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Constants (see module docstring for rationale) --------------------------

# Fixed depth of the Drain prefix tree: number of leading non-whitespace tokens
# used to index a line into a cluster group.  Drain paper default.
PREFIX_TREE_DEPTH: int = 4

# Fraction (0.0-1.0) of identical same-index tokens required for a line to join
# an existing cluster rather than spawn a new template.  Drain default ``st``.
SIMILARITY_THRESHOLD: float = 0.5

# Placeholder used INTERNALLY to mark a variable position in a template's token
# list.  This is a sentinel object identity, never compared against real string
# tokens by value, so it can never collide with log content.
_WILDCARD_TOKEN: object = object()


@dataclass(frozen=True)
class Template:
    """A mined template: a fixed token sequence with variable positions.

    ``tokens`` holds the fixed literal tokens; positions that are variable hold
    the module-private wildcard sentinel.  ``param_count`` is the number of
    wildcard positions (kept as a field so it is computed once, at construction,
    from the immutable token tuple).

    Reconstruction of a matched line: walk ``tokens`` left to right, emitting the
    literal token at fixed positions and the next unused parameter at wildcard
    positions.  ``"".join(...) == original_line`` because tokenisation is
    concatenation-preserving.
    """

    template_id: int
    tokens: tuple[object, ...]
    param_count: int

    def render_with_params(self, params: tuple[str, ...]) -> str:
        """Reconstruct the original line content from ordered ``params``.

        Total for well-formed inputs; raises ``ValueError`` if the parameter
        count does not match the template's wildcard count (a programming error,
        not domain data — the encoder guarantees the counts line up).
        """
        if len(params) != self.param_count:
            raise ValueError(
                f"template {self.template_id} expects {self.param_count} params, got {len(params)}"
            )
        out: list[str] = []
        next_param = 0
        for tok in self.tokens:
            if tok is _WILDCARD_TOKEN:
                out.append(params[next_param])
                next_param += 1
            else:
                out.append(tok)  # type: ignore[arg-type]  # fixed tokens are always str
        return "".join(out)

    def wildcard_positions(self) -> tuple[int, ...]:
        """Indices (into ``tokens``) that are variable.  Deterministic order."""
        return tuple(i for i, tok in enumerate(self.tokens) if tok is _WILDCARD_TOKEN)


@dataclass(frozen=True)
class MatchedLine:
    """One source line resolved against a template, with its exact params."""

    template_id: int
    params: tuple[str, ...]


@dataclass(frozen=True)
class MiningResult:
    """Outcome of mining a corpus of tokenised lines.

    ``templates`` is ordered by ``template_id`` (== first-appearance order).
    ``matches`` is parallel to the input line list: ``matches[i]`` is the
    template + params for input line ``i``.  Together they let the encoder decide
    which lines are worth templating and emit records in original order.
    """

    templates: tuple[Template, ...]
    matches: tuple[MatchedLine, ...]

    def template_by_id(self) -> dict[int, Template]:
        """Deterministic id -> template map (ids are unique by construction)."""
        return {t.template_id: t for t in self.templates}


@dataclass
class _Cluster:
    """Mutable working cluster used only inside the miner.

    Not exposed.  Holds the evolving fixed/variable token pattern and the count
    of lines assigned to it.  Immutability rules apply to inputs and to the
    public frozen types above; this is local scratch state that never escapes
    :func:`mine`, and it is only ever replaced field-wise, never aliased.
    """

    template_id: int
    tokens: list[object]  # str for fixed positions, _WILDCARD_TOKEN for variable
    count: int = 0


def token_count_key(tokens: tuple[str, ...]) -> int:
    """Length bucket key: number of tokens.  Drain groups by this first."""
    return len(tokens)


def prefix_key(tokens: tuple[str, ...], depth: int) -> tuple[str, ...]:
    """Leading non-whitespace tokens (up to ``depth``) indexing the prefix tree.

    Whitespace tokens are skipped for the *index* only (a leading indentation run
    should not consume a tree level), matching Drain's intent that the tree keys
    on meaningful leading tokens.  The full token list — whitespace included — is
    still what gets matched for similarity and stored in the template, so this
    skipping affects only cluster lookup, never losslessness.
    """
    lead: list[str] = []
    for tok in tokens:
        if tok.strip() == "":
            continue
        lead.append(tok)
        if len(lead) >= depth:
            break
    return tuple(lead)


def _similarity(pattern: list[object], tokens: tuple[str, ...]) -> float:
    """Fraction of positions where ``pattern`` already agrees with ``tokens``.

    A wildcard position in ``pattern`` counts as NON-matching for the purpose of
    the join decision (Drain's ``simSeq`` counts only identical concrete tokens),
    which biases toward reusing clusters whose *fixed* structure matches.  Both
    sequences are the same length (caller guarantees same length bucket); guard
    the empty case to keep the function total.
    """
    if not tokens:
        # Two zero-token lines are trivially identical in structure.
        return 1.0
    same = 0
    for pat_tok, tok in zip(pattern, tokens):
        if pat_tok is not _WILDCARD_TOKEN and pat_tok == tok:
            same += 1
    return same / len(tokens)


def _merged_pattern(pattern: list[object], tokens: tuple[str, ...]) -> list[object]:
    """New pattern after absorbing ``tokens``: positions that differ become wild.

    Returns a NEW list (never mutates ``pattern``): a position stays fixed only
    where the existing fixed token equals the incoming token; every other
    position becomes the wildcard sentinel.  Monotonic — a position, once
    wildcarded, never reverts — which keeps the template stable as more lines
    join.
    """
    merged: list[object] = []
    for pat_tok, tok in zip(pattern, tokens):
        if pat_tok is not _WILDCARD_TOKEN and pat_tok == tok:
            merged.append(pat_tok)
        else:
            merged.append(_WILDCARD_TOKEN)
    return merged


def _extract_params(pattern: tuple[object, ...], tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Exact tokens at the template's wildcard positions, in left-to-right order.

    This is the crux of losslessness: for every variable slot we keep the actual
    source token verbatim, so ``template.render_with_params(params)`` rebuilds the
    original content byte-for-byte.
    """
    return tuple(tok for pat_tok, tok in zip(pattern, tokens) if pat_tok is _WILDCARD_TOKEN)


def mine(lines: tuple[tuple[str, ...], ...]) -> MiningResult:
    """Mine templates from tokenised ``lines`` (order-preserving, deterministic).

    ``lines[i]`` is the concatenation-preserving token tuple of source line ``i``.
    Returns a :class:`MiningResult` whose ``matches`` is parallel to ``lines``.

    Algorithm (single deterministic pass, first-appearance cluster order):
      1. Bucket by token count.
      2. Within a bucket, index by the depth-``PREFIX_TREE_DEPTH`` leading-token
         prefix.
      3. Among clusters under that prefix, pick the best (highest similarity);
         if it meets ``SIMILARITY_THRESHOLD`` the line joins it (pattern merged),
         else a new cluster/template is created.
      4. After all lines are placed, freeze clusters into :class:`Template`
         objects and resolve each line's exact params against its final template.

    Two passes over the params are required because a cluster's wildcard set can
    grow as later lines join; params are therefore extracted against the FINAL
    pattern, not the pattern at join time.
    """
    # Working clusters keyed by (length_bucket, prefix).  A plain dict preserves
    # insertion order; we additionally track first-appearance order explicitly for
    # id assignment so the wire is independent of dict internals.
    buckets: dict[tuple[int, tuple[str, ...]], list[_Cluster]] = {}
    ordered_clusters: list[_Cluster] = []
    # Parallel to `lines`: which cluster each line landed in (by identity index).
    assignment: list[_Cluster] = []

    for tokens in lines:
        key = (token_count_key(tokens), prefix_key(tokens, PREFIX_TREE_DEPTH))
        candidates = buckets.get(key)
        chosen: _Cluster | None = None
        best_sim = -1.0
        if candidates is not None:
            for cluster in candidates:
                sim = _similarity(cluster.tokens, tokens)
                if sim > best_sim:
                    best_sim = sim
                    chosen = cluster
        if chosen is not None and best_sim >= SIMILARITY_THRESHOLD:
            chosen.tokens = _merged_pattern(chosen.tokens, tokens)
            chosen.count += 1
            assignment.append(chosen)
        else:
            new_cluster = _Cluster(
                template_id=len(ordered_clusters),
                tokens=list(tokens),
                count=1,
            )
            ordered_clusters.append(new_cluster)
            buckets.setdefault(key, []).append(new_cluster)
            assignment.append(new_cluster)

    # Freeze clusters into immutable templates (ids already first-appearance).
    templates: list[Template] = []
    for cluster in ordered_clusters:
        frozen_tokens = tuple(cluster.tokens)
        templates.append(
            Template(
                template_id=cluster.template_id,
                tokens=frozen_tokens,
                param_count=sum(1 for t in frozen_tokens if t is _WILDCARD_TOKEN),
            )
        )
    frozen_pattern_by_id = {t.template_id: t.tokens for t in templates}

    # Resolve each line's exact params against its cluster's FINAL pattern.
    matches: list[MatchedLine] = []
    for tokens, cluster in zip(lines, assignment):
        final_pattern = frozen_pattern_by_id[cluster.template_id]
        params = _extract_params(final_pattern, tokens)
        matches.append(MatchedLine(template_id=cluster.template_id, params=params))

    return MiningResult(templates=tuple(templates), matches=tuple(matches))


# Re-export the private wildcard sentinel under a name the encoder can use to
# render templates without reaching into a private symbol at call sites.
def is_wildcard(token: object) -> bool:
    """True iff ``token`` is the internal variable-position sentinel."""
    return token is _WILDCARD_TOKEN


# Keep `field`/`replace` imported-and-used signal honest for linters: `_Cluster`
# uses `field` default; `replace` is part of the immutable-update vocabulary
# exposed for the encoder's convenience when it needs a template copy.
__all__ = [
    "PREFIX_TREE_DEPTH",
    "SIMILARITY_THRESHOLD",
    "Template",
    "MatchedLine",
    "MiningResult",
    "mine",
    "is_wildcard",
    "token_count_key",
    "prefix_key",
]
