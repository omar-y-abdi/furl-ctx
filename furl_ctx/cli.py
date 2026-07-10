"""``furl`` — command-line interface. A thin argparse wrapper over the library.

    furl compress [FILE]   # FILE (or - / omitted for stdin) -> compressed stdout
    furl retrieve HASH     # print the original content for a CCR marker hash
    furl retrieve HASH --pattern REGEX [--context-lines N] [--line-range START:END]
    furl retrieve HASH --fields f1 f2  # project named keys out of a JSON array
    furl retrieve HASH --select-field NAME --select-equals VALUE [--limit N]
    furl retrieve HASH --select-field NAME --select-min 0 --select-max 99 [--limit N]
    furl purge HASH        # delete the stored original for a CCR hash from the store
    furl eval CORPUS --recall  # corpus compression ratio + needle-recall gate
    furl doctor            # check the install: native core, tokenizer, CCR store
    furl mcp               # run the stdio MCP server (for Claude Code, Cursor, etc.)

Shell-native access to the same engine the library and MCP server use — for
pipelines (``psql … | furl compress``), CI log reduction, and offline evaluation
with no LLM harness.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _read_input(path: str) -> str:
    if path in ("-", ""):
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _cmd_compress(args: argparse.Namespace) -> int:
    from furl_ctx import compress

    text = _read_input(args.file)
    result = compress([{"role": "tool", "content": text}], model=args.model)
    compressed = result.messages[0]["content"] if result.messages else text
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "compressed": compressed,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                    "compression_ratio": result.compression_ratio,
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


def _cmd_retrieve(args: argparse.Namespace) -> int:
    from furl_ctx import retrieve

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
        sys.stderr.write(
            f"furl: hash {args.hash} not found in the CCR store window "
            "(never stored, evicted, or expired)\n"
        )
        return 1
    sys.stdout.write(result)
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    from furl_ctx import purge

    if purge(args.hash):
        sys.stdout.write(f"furl: purged {args.hash} from the CCR store\n")
        return 0
    sys.stderr.write(
        f"furl: hash {args.hash} not found in the CCR store "
        "(never stored, already purged, evicted, or expired)\n"
    )
    return 1


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
    recall = _engine_needle_recall()

    sys.stdout.write(
        f"files: {len(files)}  tokens: {tokens_before} -> {tokens_after}\n"
        f"corpus compression ratio: {ratio * 100:.1f}%\n"
    )
    if recall < 0:
        sys.stdout.write("engine needle-recall (trust gate): unavailable (benchmarks not found)\n")
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
    parser = argparse.ArgumentParser(prog="furl", description="Furl context-compression CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compress = sub.add_parser("compress", help="compress FILE or stdin to stdout")
    p_compress.add_argument("file", nargs="?", default="-", help="input file, or - for stdin")
    p_compress.add_argument("--model", default="claude-sonnet-4-5-20250929")
    p_compress.add_argument(
        "--json", action="store_true", help="emit compressed text + stats as JSON"
    )
    p_compress.set_defaults(func=_cmd_compress)

    p_retrieve = sub.add_parser("retrieve", help="print the original content for a CCR hash")
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

    p_eval = sub.add_parser("eval", help="compression ratio + needle recall over a corpus")
    p_eval.add_argument("corpus", help="a file or a directory of files to evaluate")
    p_eval.add_argument(
        "--recall", action="store_true", required=True, help="report needle-recall %%"
    )
    p_eval.add_argument("--model", default="claude-sonnet-4-5-20250929")
    p_eval.set_defaults(func=_cmd_eval)

    p_doctor = sub.add_parser("doctor", help="check the install (core, tokenizer, store)")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_mcp = sub.add_parser("mcp", help="run the stdio MCP server for AI coding tools")
    p_mcp.add_argument("--debug", action="store_true", help="enable debug logging")
    p_mcp.set_defaults(func=_cmd_mcp)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
