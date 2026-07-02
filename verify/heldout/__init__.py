"""Held-out (SECOND) independent verification harness.

This package is a SECOND, out-of-sample verification run, fully disjoint from
the first run under ``verify/`` (which seeded from sindresorhus/slugify,
is-plain-obj, the GitHub commits API for slugify, and the npm registry for
slugify). THIS run seeds exclusively from DIFFERENT real external sources —
expressjs/express, chalk/chalk, the GitHub commits API for express, the npm
registry for express, the npm/cli package-lock.json, and a fresh local-real
macOS install log — with DIFFERENT seeds, so the engine improvers (who measured
against ``verify/``'s data during round-3 work) provably never saw this data.

Nothing here imports, re-runs, or reuses anything under ``benchmarks/`` or the
first run's generators/data. The measurement core uses ONLY the engine's own
public surface (``furl_ctx.compress``, the documented CSV-schema decoder, the
CCR compression store, and the real gpt-4o tiktoken tokenizer).
"""
