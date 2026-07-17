"""``furl`` — command-line interface. A thin argparse wrapper over the library.

    furl compress [FILE]   # FILE (or - / omitted for stdin) -> compressed stdout
    furl retrieve HASH     # print the original content for a CCR marker hash
    furl retrieve HASH --pattern REGEX [--context-lines N] [--line-range START:END]
    furl retrieve HASH --fields f1 f2  # project named keys out of a JSON array
    furl retrieve HASH --select-field NAME --select-equals VALUE [--limit N]
    furl retrieve HASH --select-field NAME --select-min 0 --select-max 99 [--limit N]
    furl purge HASH        # delete the stored original for a CCR hash from the store
    furl list              # list stored CCR entries, newest first (furl_list twin)
    furl search QUERY      # find a stored original by content (furl_search twin)
    furl stats             # live store stats + the active CCR profile (furl_stats twin)
    furl eval CORPUS --recall  # corpus compression ratio + needle-recall gate
    furl doctor            # check the install: native core, tokenizer, CCR store
    furl mcp               # run the stdio MCP server (for Claude Code, Cursor, etc.)

Shell-native access to the same engine the library and MCP server use — for
pipelines (``psql … | furl compress``), CI log reduction, and offline evaluation
with no LLM harness.

The CLI's CCR store is the GLOBAL durable one (``~/.furl/ccr.sqlite3``) with a
24 h retention default (``FURL_CCR_TTL_SECONDS=86400``, matching the Claude Code
plugin; the bare library default is 1800 s). The plugin's hook/MCP surfaces write
to PER-PROJECT stores instead — set ``FURL_CCR_PROJECT_DIR=<project root>`` to
point the CLI at a project's store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _read_input(path: str) -> str:
    """Read UTF-8 text from *path* (or stdin for ``-``/``""``), STRICT.

    Raises ``UnicodeDecodeError`` on non-UTF-8 bytes so callers decide: ``eval``
    skips such files, while ``compress`` decodes lossily (see
    :func:`_read_input_tolerant`)."""
    if path in ("-", ""):
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _read_input_tolerant(path: str) -> tuple[str, str | None]:
    """Read input, decoding non-UTF-8 bytes LOSSILY instead of crashing.

    Returns ``(text, warning_or_None)``. A binary or otherwise undecodable file
    OR stdin yields a replacement-decoded string plus a warning, so
    ``furl compress`` on binary input degrades gracefully (audit High-7 / A7) —
    a clean warning and pass-through, never a raw ``UnicodeDecodeError``
    traceback — and the file-arg and stdin paths behave identically. Reads the
    raw bytes (``sys.stdin.buffer`` for stdin, binary ``open`` for a file); a
    text-only stdin double that exposes no ``buffer`` is already decoded and
    passes straight through.
    """
    if path in ("-", ""):
        buffer = getattr(sys.stdin, "buffer", None)
        raw: bytes | str = buffer.read() if buffer is not None else sys.stdin.read()
    else:
        with open(path, "rb") as handle:
            raw = handle.read()
    if isinstance(raw, str):
        return raw, None  # a text stream double already decoded it
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        warning = (
            f"input is not valid UTF-8 text ({exc.reason} at byte {exc.start}); "
            "decoded with replacement; furl compresses text, so binary is passed "
            "through lossily"
        )
        return raw.decode("utf-8", errors="replace"), warning


def _cmd_compress(args: argparse.Namespace) -> int:
    from furl_ctx import compress

    text, decode_warning = _read_input_tolerant(args.file)
    if decode_warning is not None:
        sys.stderr.write(f"furl: {decode_warning}\n")
    result = compress([{"role": "tool", "content": text}], model=args.model)
    compressed = result.messages[0]["content"] if result.messages else text
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "compressed": compressed,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                    # Crit-2: the headline number is VISIBLE-TOKEN reduction (how
                    # much the model no longer sees), which INCLUDES CCR-offloaded
                    # content — hidden but byte-retrievable, not lossless byte
                    # compression. Labeled explicitly so a high number on
                    # high-entropy input is not mistaken for lossless shrinkage.
                    "visible_token_reduction": result.compression_ratio,
                    # Deprecated alias kept for compatibility — same value.
                    "compression_ratio": result.compression_ratio,
                    "reduction_note": (
                        "visible_token_reduction is context reduction, meaning tokens "
                        "hidden from the model, and it includes content offloaded to "
                        "the CCR store and retrievable by hash; it is NOT lossless "
                        "byte compression. See BENCHMARKS.md for the lossless band."
                    ),
                    "ccr_hashes": result.ccr_hashes,
                    "error": result.error,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        sys.stdout.write(compressed)
    return 0


def _parse_line_range(value: str) -> list[int | None]:
    """Parse ``"START:END"`` into ``[start|None, end|None]``.

    Both sides are 1-based and optional (``":50"`` → ``[None, 50]``,
    ``"5:"`` → ``[5, None]``).  Raises ``argparse.ArgumentTypeError`` on
    non-integer tokens so argparse can surface a clean usage error.
    """
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"--line-range must be 'START:END' (e.g. '10:20', ':50', '5:'), got: {value!r}"
        )
    result: list[int | None] = []
    for token in parts:
        if token == "":
            result.append(None)
        else:
            try:
                result.append(int(token))
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    f"--line-range bounds must be integers, got: {token!r}"
                ) from exc
    return result


def _parse_select_equals(value: str) -> Any:
    """Parse ``--select-equals`` with ``json.loads`` → fall back to raw string.

    ``"123"`` → ``123`` (int), ``"true"`` → ``True`` (bool),
    ``"null"`` → ``None``, ``"DroppedFrame"`` → ``"DroppedFrame"`` (str).
    """
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _resolve_active_profile() -> dict[str, str]:
    """The effective CCR profile for THIS run — backend, TTL, store scope, and
    the compress floor — read from the already-resolved environment.

    High-8: each surface (library / CLI / plugin) ships different CCR defaults
    on purpose (the library stays in-memory + 30 min; the CLI opts into durable
    sqlite + 24 h; the plugin pins per-project sqlite). That divergence silently
    surprised users, so every ``furl`` run now surfaces the profile it is
    actually using. Read from the environment ONLY (no store construction), so
    building the banner never creates or opens a store as a side effect. Total:
    never raises.
    """
    from .config import DEFAULT_MIN_TOKENS_TO_COMPRESS

    scope = os.environ.get("FURL_CCR_PROJECT_DIR") or os.environ.get("FURL_CCR_NAMESPACE")
    return {
        "backend": os.environ.get("FURL_CCR_BACKEND", "memory"),
        "ttl_seconds": os.environ.get("FURL_CCR_TTL_SECONDS", "1800"),
        "scope": scope or "global (~/.furl)",
        "min_tokens_to_compress": str(DEFAULT_MIN_TOKENS_TO_COMPRESS),
    }


def _profile_banner_enabled() -> bool:
    """Whether to emit the one-line profile banner (High-8). Default ON;
    ``FURL_PROFILE_BANNER=0`` (or false/no/off/disabled) silences it so clean
    pipelines stay quiet."""
    raw = os.environ.get("FURL_PROFILE_BANNER", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "disabled")


def _emit_profile_banner() -> None:
    """Write the active-profile banner to STDERR (never stdout, so it never
    corrupts piped compressed output).

    Fail-open: a diagnostic banner must never break the command it precedes.
    """
    if not _profile_banner_enabled():
        return
    try:
        profile = _resolve_active_profile()
        sys.stderr.write(
            f"furl profile: backend={profile['backend']} ttl={profile['ttl_seconds']}s "
            f"scope={profile['scope']} min_tokens={profile['min_tokens_to_compress']} "
            "(FURL_PROFILE_BANNER=0 to silence)\n"
        )
    except Exception:  # noqa: BLE001 — observability must never be fatal
        pass


def _invalid_hash_message(hash_value: str) -> str | None:
    """Return an error string iff *hash_value* is not a syntactically valid CCR
    hash (12 or 24 lowercase-hex), else ``None``.

    Med-11: the MCP ``furl_retrieve`` / ``furl_purge`` handlers reject a
    malformed hash up front via ``marker_grammar.is_valid_ccr_hash``; the CLI
    used to pass any string straight to the store and report a generic MISS,
    conflating "you typed a bad hash" with "that hash expired or was evicted".
    This gives the CLI the same guard, and the same message, the MCP server uses.
    """
    from furl_ctx.ccr.marker_grammar import is_valid_ccr_hash

    if is_valid_ccr_hash(hash_value):
        return None
    return f"invalid hash format: {hash_value!r} (expected 12 or 24 lowercase-hex chars)"


def _ccr_store_search_target() -> tuple[str, bool]:
    """Describe WHERE ``furl retrieve`` just looked, and whether that store is
    volatile (process-local memory).

    Introspects the SAME store the library ``retrieve`` consults —
    ``_active_ccr_store(None, None)``, the resolution seam ``compress()``/
    ``retrieve()`` share (F2): the isolated per-namespace store when one is
    active (``FURL_CCR_PROJECT_DIR`` / ``FURL_CCR_NAMESPACE``), else the
    request-scoped/global singleton — already initialized by the retrieve
    attempt that just missed. Under a namespace the miss therefore names the
    ``ccr-ns-<hash>.sqlite3`` file that was actually searched, never the
    global ``ccr.sqlite3`` it did not touch. Returns
    ``(descriptor, is_process_local_memory)``:

    * durable sqlite backend → ``("backend=sqlite store=<db path>", False)``
    * in-memory backend      → ``("in-memory (process-local)", True)``
    * any other backend      → ``("backend=<ClassName>", False)``

    Total: never raises. A store/introspection failure degrades to a generic
    ``("the CCR store window", False)`` so the loud miss message is always
    emitted (an honest miss must never itself fail).
    """
    try:
        from furl_ctx.cache.compression_store import _active_ccr_store

        backend = _active_ccr_store(None, None)._backend
        # SqliteBackend exposes its resolved database path as ``_db_path``; the
        # in-memory backend has none. Report the concrete file so a user knows
        # exactly which store was searched (and can inspect/point another
        # process at it).
        db_path = getattr(backend, "_db_path", None)
        if db_path is not None:
            return (f"backend=sqlite store={db_path}", False)
        if type(backend).__name__ == "InMemoryBackend":
            return ("in-memory (process-local)", True)
        return (f"backend={type(backend).__name__}", False)
    except Exception:
        return ("the CCR store window", False)


def _cmd_retrieve(args: argparse.Namespace) -> int:
    from furl_ctx import retrieve

    # Reject a malformed hash up front with the SAME guard/message the MCP
    # server uses (Med-11), so "you typed a bad hash" (exit 2, usage error) is
    # never silently reported as an expired/evicted MISS (exit 1).
    hash_error = _invalid_hash_message(args.hash)
    if hash_error is not None:
        sys.stderr.write(f"furl: {hash_error}\n")
        return 2

    # Build kwargs only from flags the user actually passed (SUPPRESS default).
    # This keeps the no-flag path a plain full retrieve with no extra arguments.
    kwargs: dict[str, Any] = {}
    if hasattr(args, "pattern"):
        kwargs["pattern"] = args.pattern
    if hasattr(args, "context_lines"):
        kwargs["context_lines"] = args.context_lines
    if hasattr(args, "line_range"):
        kwargs["line_range"] = args.line_range
    if hasattr(args, "fields"):
        kwargs["fields"] = args.fields
    if hasattr(args, "select_field"):
        kwargs["select_field"] = args.select_field
    if hasattr(args, "select_equals"):
        kwargs["select_equals"] = args.select_equals
    if hasattr(args, "select_min"):
        kwargs["select_min"] = args.select_min
    if hasattr(args, "select_max"):
        kwargs["select_max"] = args.select_max
    if hasattr(args, "limit"):
        kwargs["limit"] = args.limit

    try:
        result = retrieve(args.hash, **kwargs)
    except ValueError as exc:
        sys.stderr.write(f"furl: {exc}\n")
        return 2

    if result is None:
        # Honest miss: name the store that was actually searched (backend +
        # resolved path for sqlite; "in-memory (process-local)" otherwise) so a
        # user is never left guessing WHERE the hash was looked for. When the
        # store is the volatile in-memory backend, say plainly that its entries
        # do not survive across processes — the exact reason a compress in one
        # `furl` invocation cannot be retrieved by a later one — and point at the
        # durable opt-out.
        target, volatile = _ccr_store_search_target()
        sys.stderr.write(
            f"furl: hash {args.hash} not found in {target} (never stored, evicted, or expired)\n"
        )
        if volatile:
            sys.stderr.write(
                "furl: in-memory CCR entries do not survive across processes, so a hash "
                "stored by one `furl` command cannot be retrieved by a later one. Set "
                "FURL_CCR_BACKEND=sqlite for a durable store that persists across processes.\n"
            )
        return 1
    sys.stdout.write(result)
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    from furl_ctx import purge

    # Same up-front hash-format guard as retrieve / the MCP server (Med-11).
    hash_error = _invalid_hash_message(args.hash)
    if hash_error is not None:
        sys.stderr.write(f"furl: {hash_error}\n")
        return 2

    if purge(args.hash):
        sys.stdout.write(f"furl: purged {args.hash} from the CCR store\n")
        return 0
    sys.stderr.write(
        f"furl: hash {args.hash} not found in the CCR store "
        "(never stored, already purged, evicted, or expired)\n"
    )
    return 1


# furl_search preview bounds (mirrors the MCP furl_search tool's slice-before-
# redact discipline). The credential regexes are O(N^2) on long token runs, so
# the redactor must never see a whole multi-MB original — only a MARGIN-widened
# window around the match, so a secret straddling a display edge is masked whole
# before any truncation.
_SEARCH_PREVIEW_RADIUS = 60  # chars of context each side of the match
_SEARCH_PREVIEW_MARGIN = 256  # extra chars redacted (edge-straddling secrets)
_SEARCH_PREVIEW_MAX = 200  # hard char cap on a displayed preview
_SEARCH_SCAN_CAP = 5000  # bound the linear scan over live entries


_LIST_PREVIEW_MAX = 80  # hard char cap on a `furl list` preview


def _redacted_list_preview(original: str, redactor: Any) -> str:
    """The leading, credential-redacted window of *original* for ``furl list``.

    Same discipline as :func:`_redacted_search_preview` (RG4): slice-before-redact
    with a margin, so the O(N^2)-on-long-hex-runs credential regexes never see the
    whole original, and a secret straddling the 80-char display edge is still seen
    whole and masked before truncation. Total: never raises — a redactor error
    yields the raw window rather than crashing the listing.
    """
    window = original[: _LIST_PREVIEW_MAX + _SEARCH_PREVIEW_MARGIN]
    if redactor is not None:
        try:
            window = redactor(window)
        except Exception:  # noqa: BLE001 — a preview must never break the listing
            pass
    return window.replace("\n", " ")[:_LIST_PREVIEW_MAX]


def _redacted_search_preview(original: str, match_idx: int, needle_len: int, redactor: Any) -> str:
    """A short, credential-redacted window of *original* around a substring match.

    Slice-before-redact: redact only a margin-widened window (never the whole
    original — the credential regexes are O(N^2) on long base64/hex runs), so a
    secret straddling a display edge is seen whole and masked before truncation.
    Total: never raises (redaction is fail-open — a redactor error yields the
    raw window rather than crashing the search).
    """
    lo = max(0, match_idx - _SEARCH_PREVIEW_RADIUS - _SEARCH_PREVIEW_MARGIN)
    hi = min(
        len(original), match_idx + needle_len + _SEARCH_PREVIEW_RADIUS + _SEARCH_PREVIEW_MARGIN
    )
    window = original[lo:hi]
    if redactor is not None:
        try:
            window = redactor(window)
        except Exception:  # noqa: BLE001 — a preview must never break the search
            pass
    return window.replace("\n", " ").strip()[:_SEARCH_PREVIEW_MAX]


def _live_store_entries(limit: int) -> list[tuple[str, Any]]:
    """Newest-first ``(hash, entry)`` pairs from the active CLI store, expired
    rows dropped, capped at *limit*.

    Reads the SAME store seam ``furl retrieve`` consults
    (``_active_ccr_store(None, None)``) and mirrors the MCP ``_live_entries``
    read: snapshot ``backend.items()`` UNDER the store lock (an unlocked
    ``list(dict.items())`` racing a concurrent write raises "dictionary changed
    size during iteration" on the in-memory backend), then filter expiry and
    sort on the local snapshot outside the lock.
    """
    import time

    from furl_ctx.cache.compression_store import _active_ccr_store

    store = _active_ccr_store(None, None)
    now = time.time()
    with store._lock:
        snapshot = list(store._backend.items())
    live = [(h, e) for h, e in snapshot if not e.is_expired(now)]
    live.sort(key=lambda pair: pair[1].created_at, reverse=True)
    return live[:limit]


def _cmd_list(args: argparse.Namespace) -> int:
    """``furl list`` — newest-first directory of stored CCR entries (MCP parity).

    The CLI twin of the ``furl_list`` MCP tool: inspect what originals the store
    is holding (hash, age, size, kind, a short credential-redacted preview) so a
    lost ``<<ccr:HASH>>`` marker can be recovered by eye, then retrieved by hash.
    The preview is redacted exactly like ``furl search``'s (RG4): the store holds
    whatever the agent compressed, so a listing must not print a secret back out.
    """
    import time

    from furl_ctx.redaction import build_store_redactor

    entries = _live_store_entries(args.limit)
    if args.json:
        now = time.time()
        payload = [
            {
                "hash": hash_key,
                "age_seconds": round(now - entry.created_at),
                "size": len(entry.original_content),
                "content_kind": entry.tool_name,
            }
            for hash_key, entry in entries
        ]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0
    if not entries:
        sys.stdout.write("furl: no live entries in the CCR store\n")
        return 0
    now = time.time()
    redactor = build_store_redactor()
    for hash_key, entry in entries:
        preview = _redacted_list_preview(entry.original_content, redactor)
        age = round(now - entry.created_at)
        sys.stdout.write(
            f"{hash_key}  age={age}s  size={len(entry.original_content)}  "
            f"kind={entry.tool_name}  {preview!r}\n"
        )
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    """``furl search`` — find a stored original by content, get its hash back.

    The CLI twin of the ``furl_search`` MCP tool: case-insensitive SUBSTRING
    match (no regex — no ReDoS/injection surface) over live stored originals,
    newest first, each with a short credential-redacted preview around the
    match so a lost ``<<ccr:HASH>>`` marker is recoverable without leaking a
    secret. Bounded: scans at most ``_SEARCH_SCAN_CAP`` entries, returns at most
    ``--limit`` hits.
    """
    from furl_ctx.redaction import build_store_redactor

    if not args.query.strip():
        sys.stderr.write("furl: search query must be a non-empty string\n")
        return 2

    needle = args.query.lower()
    redactor = build_store_redactor()
    matches: list[tuple[str, Any, str]] = []
    for hash_key, entry in _live_store_entries(_SEARCH_SCAN_CAP):
        original = entry.original_content
        idx = original.lower().find(needle)
        if idx < 0:
            continue
        preview = _redacted_search_preview(original, idx, len(args.query), redactor)
        matches.append((hash_key, entry, preview))
        if len(matches) >= args.limit:
            break

    if args.json:
        payload = [
            {"hash": hash_key, "content_kind": entry.tool_name, "preview": preview}
            for hash_key, entry, preview in matches
        ]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0
    if not matches:
        sys.stdout.write(f"furl: no stored original matches {args.query!r}\n")
        return 0
    for hash_key, entry, preview in matches:
        sys.stdout.write(f"{hash_key}  kind={entry.tool_name}  {preview!r}\n")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    """``furl stats`` — live store stats + the active CCR profile (MCP parity).

    The CLI twin of the ``furl_stats`` MCP tool, scoped to what the CLI can
    honestly report: the live, cross-process store figures (``get_stats``, which
    also prunes expired rows) plus the resolved profile (High-8) so the numbers
    are never read against the wrong backend/TTL.
    """
    from furl_ctx.cache.compression_store import _active_ccr_store

    store = _active_ccr_store(None, None)
    stats = store.get_stats()
    profile = _resolve_active_profile()
    store_desc, _volatile = _ccr_store_search_target()
    if args.json:
        sys.stdout.write(
            json.dumps(
                {"profile": profile, "store_descriptor": store_desc, "store": stats}, indent=2
            )
            + "\n"
        )
        return 0
    sys.stdout.write(
        "furl store stats\n"
        f"  store: {store_desc}\n"
        f"  scope: {profile['scope']}\n"
        f"  entries: {stats['entry_count']} (max {stats['max_entries']})\n"
        f"  default TTL: {stats['default_ttl_seconds']}s\n"
        f"  original tokens: {stats['total_original_tokens']}\n"
        f"  compressed tokens: {stats['total_compressed_tokens']}\n"
        f"  retrievals: {stats['total_retrievals']}\n"
    )
    return 0


def _corpus_files(path: str) -> list[str]:
    """Return the sorted list of files under *path* (a single file or a dir).

    Directories are walked recursively; the order is deterministic so the
    pooled ratio/recall over a dir does not depend on filesystem iteration.
    """
    if os.path.isdir(path):
        found: list[str] = []
        for root, _dirs, names in os.walk(path):
            found.extend(os.path.join(root, name) for name in names)
        return sorted(found)
    return [path]


def _engine_needle_recall() -> float:
    """Engine-wide needle-recall trust gate, or ``-1.0`` if unavailable.

    Reuses ``benchmarks/needle_recall.py`` — the audited harness that injects a
    known needle into REAL row arrays and asks whether it survives compression
    (visible OR CCR-recoverable). ``recalled = in_output or ccr_recoverable``,
    so a *silently* dropped needle (no marker, unrecoverable) lowers the rate —
    the exact failure a mere "were surfaced markers recoverable?" count is blind
    to. Runs a trimmed grid (both regimes, one cell) to stay a fast canary.

    Note: this is an ENGINE property, not a property of the user's corpus — the
    harness's ``NeedleFamily`` is hardwired to its own base rows (frozen +
    raises for other families), so corpus-fed needle-recall is not reachable
    from ``cli.py`` alone. lazy: ceiling is the internal SEARCH/LOGS families;
    upgrade path is a corpus-parameterizable ``NeedleFamily`` in benchmarks/.
    """
    try:
        from benchmarks.needle_recall import (
            LOGS_FAMILY,
            SEARCH_FAMILY,
            recall_rate,
            run_needle_recall,
        )
    except ImportError:
        return -1.0
    results = run_needle_recall(
        families=(SEARCH_FAMILY, LOGS_FAMILY),
        cardinalities=(30,),
        positions=("middle",),
        arms=("naming",),
    )
    return recall_rate(results)


def _cmd_eval(args: argparse.Namespace) -> int:
    """Report a corpus's compression ratio + the engine's needle-recall gate.

    Two honestly-scoped numbers: the ratio is measured on the user's own corpus
    (``CompressResult.compression_ratio`` pooled across files — the same figure
    ``compress --json`` reports), and the recall is the engine-wide needle-recall
    trust gate (see ``_engine_needle_recall``). measure.py's ``measure`` /
    ``effective_savings`` are skipped for the ratio: they need bench-case
    scaffolding (a granular per-row ``chunk_tokens`` index, ``n_dropped_rows``)
    that arbitrary corpus text does not carry.
    """
    from furl_ctx import compress

    if not os.path.exists(args.corpus):
        sys.stderr.write(f"furl: corpus not found: {args.corpus}\n")
        return 1
    if not os.access(args.corpus, os.R_OK):
        sys.stderr.write(f"furl: corpus not readable: {args.corpus}\n")
        return 1

    files = _corpus_files(args.corpus)
    if not files:
        sys.stderr.write(f"furl: no files found in corpus {args.corpus}\n")
        return 1

    tokens_before = 0
    tokens_after = 0
    for path in files:
        try:
            text = _read_input(path)
        except (OSError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"furl: skipping {path}: {exc}\n")
            continue
        result = compress([{"role": "tool", "content": text}], model=args.model)
        tokens_before += result.tokens_before
        tokens_after += result.tokens_after

    ratio = (tokens_before - tokens_after) / tokens_before if tokens_before else 0.0

    sys.stdout.write(
        f"files: {len(files)}  tokens: {tokens_before} -> {tokens_after}\n"
        f"corpus visible-token reduction: {ratio * 100:.1f}% "
        "(context hidden, not lossless compression; originals stay CCR-retrievable)\n"
    )
    # The needle-recall gate runs an extra benchmark grid, so it is opt-in via
    # --recall (the flag now conveys a real choice, not a mandatory no-op).
    if args.recall:
        recall = _engine_needle_recall()
        if recall < 0:
            sys.stdout.write(
                "engine needle-recall (trust gate): unavailable (benchmarks not found)\n"
            )
        else:
            sys.stdout.write(f"engine needle-recall (trust gate): {recall * 100:.1f}%\n")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    try:
        import furl_ctx

        checks.append(("furl_ctx import", True, getattr(furl_ctx, "__version__", "?")))
    except Exception as exc:  # pragma: no cover - import failure is the diagnostic
        checks.append(("furl_ctx import", False, str(exc)))

    try:
        import furl_ctx._core as core  # native extension — load-bearing

        checks.append(("native _core", True, os.path.basename(core.__file__)))
    except Exception as exc:
        checks.append(("native _core", False, f"{exc} — compression fails open to 0%"))

    try:
        import tiktoken  # noqa: F401

        checks.append(("tiktoken", True, "available"))
    except Exception as exc:
        checks.append(("tiktoken", False, f"{exc} — token counts fall back to estimation"))

    try:
        from furl_ctx.cache.compression_store import get_compression_store

        store = get_compression_store()
        checks.append(("CCR store", True, type(store._backend).__name__))
    except Exception as exc:
        checks.append(("CCR store", False, str(exc)))

    for name, passed, detail in checks:
        sys.stdout.write(f"[{'OK' if passed else 'FAIL'}] {name}: {detail}\n")

    # High-8: the resolved CCR profile this install will use, so `doctor` answers
    # "which backend/TTL/scope am I actually on?" — the surface-specific defaults
    # that otherwise surprise users.
    profile = _resolve_active_profile()
    sys.stdout.write(
        f"[--] profile: backend={profile['backend']} ttl={profile['ttl_seconds']}s "
        f"scope={profile['scope']} min_tokens={profile['min_tokens_to_compress']}\n"
    )
    return 0 if all(passed for _, passed, _ in checks) else 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Launch the stdio MCP server (equivalent of ``python -m furl_ctx.ccr.mcp_server``)."""
    import asyncio

    from furl_ctx.ccr.mcp_server import main as mcp_main

    try:
        asyncio.run(mcp_main(["--debug"] if args.debug else []))
    except ImportError as exc:
        # The MCP SDK ships in the optional ``mcp`` extra; surface the missing
        # dependency as a clean furl-style error instead of a traceback.
        sys.stderr.write(f"furl: {exc}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # CLI-vs-library backend split. The `furl` CLI is a command-line tool: its
    # `compress` and `retrieve` run in SEPARATE processes, so its CCR store must
    # be durable — the library's in-memory default dies with the process, which
    # would make every cross-process `retrieve` a false miss (the exact "nothing
    # is truly lost" contradiction this closes). Default the CLI's store to the
    # durable sqlite backend BEFORE any engine/store init below.
    #
    # This opts in ONLY the CLI entrypoint — the LIBRARY default stays in-memory
    # on purpose (a library must not write to disk unbidden; callers opt in via
    # FURL_CCR_BACKEND, and the Claude Code plugin already pins sqlite). setdefault
    # means an explicit user FURL_CCR_BACKEND (memory, sqlite, a plugin name, ...)
    # is always respected.
    os.environ.setdefault("FURL_CCR_BACKEND", "sqlite")

    # TTL parity with the Claude Code plugin (round-6 evaluator finding): the
    # library's 30-minute default made CLI-stored originals die mid-session
    # while the plugin's hook and MCP tools (FURL_CCR_TTL_SECONDS=86400, set
    # by hooks/compress_tool_output.py and .mcp.json) kept theirs for 24 h.
    # Mirror the hook's setdefault so a `furl compress` hash lives as long as
    # a hook-compressed one; an explicit user FURL_CCR_TTL_SECONDS still wins.
    #
    # Store SCOPE stays global (~/.furl/ccr.sqlite3) on purpose. The plugin's
    # per-project stores are keyed by FURL_CCR_PROJECT_DIR, which the hook and
    # MCP server derive in-process from CLAUDE_PROJECT_DIR — an env var Claude
    # Code does not export to Bash-run commands — and inferring a project root
    # from cwd here would silo data under whatever subdirectory the user ran
    # from. Users target a project store explicitly:
    # FURL_CCR_PROJECT_DIR=<project root> furl retrieve <hash>.
    os.environ.setdefault("FURL_CCR_TTL_SECONDS", "86400")

    parser = argparse.ArgumentParser(prog="furl", description="Furl context-compression CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compress = sub.add_parser(
        "compress",
        help="compress FILE or stdin to stdout",
        epilog=(
            "Offloaded originals are stored in the CLI's CCR store — durable sqlite by "
            "default (FURL_CCR_BACKEND=sqlite), at the GLOBAL ~/.furl/ccr.sqlite3 "
            "(FURL_WORKSPACE_DIR / FURL_CCR_SQLITE_PATH override) — and stay retrievable by "
            "hash via `furl retrieve` for FURL_CCR_TTL_SECONDS (CLI default 86400s / 24 h, "
            "matching the Claude Code plugin; the bare library default is 1800s). The plugin "
            "writes to PER-PROJECT stores instead; set FURL_CCR_PROJECT_DIR=<project root> "
            "to target a project's store here."
        ),
    )
    p_compress.add_argument("file", nargs="?", default="-", help="input file, or - for stdin")
    p_compress.add_argument("--model", default="claude-sonnet-4-5-20250929")
    p_compress.add_argument(
        "--json", action="store_true", help="emit compressed text + stats as JSON"
    )
    p_compress.set_defaults(func=_cmd_compress)

    p_retrieve = sub.add_parser(
        "retrieve",
        help="print the original content for a CCR hash",
        epilog=(
            "Looks the hash up in the CLI's CCR store — durable sqlite by default "
            "(FURL_CCR_BACKEND=sqlite), at the GLOBAL ~/.furl/ccr.sqlite3 "
            "(FURL_WORKSPACE_DIR / FURL_CCR_SQLITE_PATH override). Entries persist for "
            "FURL_CCR_TTL_SECONDS (CLI default 86400s / 24 h); a miss names the store that "
            "was searched. Hashes minted inside Claude Code live in that project's "
            "PER-PROJECT store — set FURL_CCR_PROJECT_DIR=<project root> to look there."
        ),
    )
    p_retrieve.add_argument("hash")
    # Slice flags — all optional; omitting them all gives a plain full retrieve.
    # SUPPRESS keeps unset flags off args.* so _cmd_retrieve can distinguish
    # "not passed" from an explicit value (critical for --select-equals).
    p_retrieve.add_argument(
        "--pattern",
        default=argparse.SUPPRESS,
        metavar="REGEX",
        help="regex to filter lines (shows matching lines with context)",
    )
    p_retrieve.add_argument(
        "--context-lines",
        dest="context_lines",
        type=int,
        default=argparse.SUPPRESS,
        metavar="N",
        help="lines of context around each --pattern match (default 0)",
    )
    p_retrieve.add_argument(
        "--line-range",
        dest="line_range",
        type=_parse_line_range,
        default=argparse.SUPPRESS,
        metavar="START:END",
        help="1-based line window, either side blank for open (e.g. '10:20', ':50', '5:')",
    )
    p_retrieve.add_argument(
        "--fields",
        nargs="+",
        default=argparse.SUPPRESS,
        metavar="FIELD",
        help="project named keys out of a JSON array of objects",
    )
    p_retrieve.add_argument(
        "--select-field",
        dest="select_field",
        default=argparse.SUPPRESS,
        metavar="FIELD",
        help="field name for row-select (use with --select-equals or --select-min/--select-max)",
    )
    p_retrieve.add_argument(
        "--select-equals",
        dest="select_equals",
        type=_parse_select_equals,
        default=argparse.SUPPRESS,
        metavar="VALUE",
        help=(
            "keep rows where --select-field equals VALUE; "
            "parsed as JSON (so 123→int, true→bool, null→None) then falls back to raw string"
        ),
    )
    p_retrieve.add_argument(
        "--select-min",
        dest="select_min",
        type=float,
        default=argparse.SUPPRESS,
        metavar="NUM",
        help="keep rows where --select-field >= NUM (numeric range, inclusive)",
    )
    p_retrieve.add_argument(
        "--select-max",
        dest="select_max",
        type=float,
        default=argparse.SUPPRESS,
        metavar="NUM",
        help="keep rows where --select-field <= NUM (numeric range, inclusive)",
    )
    p_retrieve.add_argument(
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        metavar="N",
        help="maximum number of rows to return from a row-select",
    )
    p_retrieve.set_defaults(func=_cmd_retrieve)

    p_purge = sub.add_parser("purge", help="delete a stored original by CCR hash")
    p_purge.add_argument("hash")
    p_purge.set_defaults(func=_cmd_purge)

    # CLI parity with the MCP tools (Med-11): list / search / stats over the same
    # store `furl compress` writes to and `furl retrieve` reads from.
    p_list = sub.add_parser("list", help="list stored CCR entries, newest first")
    p_list.add_argument(
        "--limit", type=int, default=20, metavar="N", help="max entries to show (default 20)"
    )
    p_list.add_argument("--json", action="store_true", help="emit entries as JSON")
    p_list.set_defaults(func=_cmd_list)

    p_search = sub.add_parser(
        "search", help="find a stored original by content and get its hash back"
    )
    p_search.add_argument("query", help="text to search for across stored originals")
    p_search.add_argument(
        "--limit", type=int, default=10, metavar="N", help="max ranked hits (default 10)"
    )
    p_search.add_argument("--json", action="store_true", help="emit matches as JSON")
    p_search.set_defaults(func=_cmd_search)

    p_stats = sub.add_parser("stats", help="show live CCR store stats + active profile")
    p_stats.add_argument("--json", action="store_true", help="emit stats as JSON")
    p_stats.set_defaults(func=_cmd_stats)

    p_eval = sub.add_parser("eval", help="compression ratio + needle recall over a corpus")
    p_eval.add_argument("corpus", help="a file or a directory of files to evaluate")
    p_eval.add_argument(
        "--recall",
        action="store_true",
        help="also run the engine needle-recall trust gate (extra work; off by default)",
    )
    p_eval.add_argument("--model", default="claude-sonnet-4-5-20250929")
    p_eval.set_defaults(func=_cmd_eval)

    p_doctor = sub.add_parser("doctor", help="check the install (core, tokenizer, store)")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_mcp = sub.add_parser("mcp", help="run the stdio MCP server for AI coding tools")
    p_mcp.add_argument("--debug", action="store_true", help="enable debug logging")
    p_mcp.set_defaults(func=_cmd_mcp)

    args = parser.parse_args(argv)
    # High-8: surface the active CCR profile (backend/TTL/scope) on every run so
    # a surface's non-obvious defaults never silently surprise the user. STDERR
    # only (stdout stays pure compressed output); opt out with FURL_PROFILE_BANNER=0.
    _emit_profile_banner()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
