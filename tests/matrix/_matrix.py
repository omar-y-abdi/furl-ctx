"""Shared foundation for the Furl blind-spot MATRIX (golden edge-case register).

The MATRIX proves the core promise on content families with ZERO prior coverage:

    "Anything the agent reads is either passed through byte-exact, or offloaded
     under a ``<<ccr:HASH>>`` pointer whose ``retrieve(hash)`` reconstructs it —
     no silent loss."

Every generator here produced the behavior asserted by the consuming tests when
run through the PUBLIC ``furl_ctx.compress`` / ``furl_ctx.retrieve`` API on
``model="gpt-4o"`` (the tokenizer the rest of the suite uses). The contract
helpers encode the THREE documented recovery shapes discovered empirically and
cross-checked against source:

* **TEXT lossless** (``assert_text_lossless_byte_exact``) — content is either a
  byte-exact passthrough (below ``CompressConfig.min_tokens_to_compress`` = 250,
  or the router declines) OR a whole-content offload whose surfaced hash
  reconstructs the original BYTE-EXACT. The store persists the raw original for
  the text/code/log/diff routes (``code_aware_compressor._persist_to_python_ccr``
  stores ``content``; ``csv_ingest``/``envelope_ingest`` likewise store the raw
  bytes — ``envelope_ingest.py:99-100`` "restores it byte-exact").

* **TEXT fail-open** (``assert_text_failopen_byte_exact``) — a content shape the
  pipeline cannot tokenize/encode (multi-MB single line → tiktoken catastrophic
  backtracking; a lone surrogate → UTF-8 encode error at the Rust FFI). ``compress``
  is FAIL-OPEN (``furl_ctx/__init__.py:33-46``): it returns the ORIGINAL unchanged
  with ``result.error`` set. The byte-exact + LOUD-error guarantee holds; the
  "gets compressed" sub-promise does not (a documented caveat, not a silent loss).

* **JSON-array distinct recovery** (``assert_array_distinct_recovery``) — a
  top-level JSON array of rows/scalars routes to the documented LOSSY:table
  row-drop path: a CSV survivor table ships inline and the dropped rows are
  offloaded COMPACT-re-serialized under a ``<<ccr:HASH N_rows_offloaded>>``
  pointer. The contract (pinned by ``tests/test_ccr_recovery_invariant.py``) is
  SET-BASED distinct-item recovery from (survivors ∪ retrieved-drop), NOT
  whole-input byte-exactness — the row JSON is normalized to compact separators.

All secret fixtures are assembled at runtime from parts so no verbatim
token-shaped literal sits in source (scanner-hygiene, mirroring
``tests/test_b3_redact_purge.py`` and ``tests/test_compression_store_redaction.py``).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from furl_ctx import compress, retrieve
from furl_ctx.cache.compression_store import get_compression_store

# Reuse the canonical recovery-comparison helpers instead of re-deriving them —
# same helpers ``test_ccr_recovery_invariant.py`` uses for the lossy:table path.
from tests._fixtures import canonical_repr, decode_csv_schema_into

_MODEL = "gpt-4o"


# ─── public-API driver ───────────────────────────────────────────────────────


def run(content: Any, *, config: Any = None):
    """Compress a single tool message through the public API (one seam, one place)."""
    kwargs: dict[str, Any] = {"model": _MODEL}
    if config is not None:
        kwargs["config"] = config
    return compress([{"role": "tool", "content": content}], **kwargs)


def output_of(result) -> Any:
    return result.messages[0].get("content")


def salted(content: str, salt: str) -> str:
    """Append a unique trailing token so no two tests compress identical bytes.

    Required for isolation: ``compress()`` reuses process-global caches (the
    Python Tier-2 result cache AND the Rust crusher's store) that a Python store
    reset does NOT clear — so a SECOND compress of identical content in the same
    session hits the cache and skips the Python re-persist, leaving ``retrieve``
    empty (this is the very divergence pinned in ``test_result_cache_divergence``).
    A per-test salt keeps every family compress a genuine cold pass. The token is
    plain text (no ``<<``/``hash=``) so it never looks like a CCR marker and does
    not change routing; verified to preserve whole-offload byte-exactness.
    """
    return f"{content}\nmatrix-salt-{salt}\n" if salt else content


# ─── contract-assertion helpers (the three documented recovery shapes) ───────


def assert_text_lossless_byte_exact(content: str, *, salt: str = ""):
    """TEXT contract: byte-exact passthrough OR byte-exact whole-offload.

    Path-agnostic on purpose — it proves the PROMISE (no silent loss + byte-exact
    recovery), not a specific route, so it stays green if a family's routing
    shifts as long as recovery remains byte-exact. Returns the ``CompressResult``.
    """
    content = salted(content, salt)
    result = run(content)
    assert result.error is None, f"unexpected fail-open error: {result.error!r}"
    out = output_of(result)
    hashes = result.ccr_hashes
    if not hashes:
        # Passthrough: the exact input bytes come back, nothing offloaded.
        assert out == content, "passthrough must be byte-identical to the input"
        return result
    # Offload: no surfaced pointer may dangle, and the WHOLE original must be
    # byte-exact recoverable under one of the surfaced hashes.
    payloads = {h: retrieve(h) for h in hashes}
    dangling = [h for h, v in payloads.items() if v is None]
    assert not dangling, f"dangling CCR pointer(s), original unrecoverable: {dangling}"
    assert content in set(payloads.values()), (
        "whole original not byte-exact recoverable under any surfaced hash "
        f"(hashes={hashes})"
    )
    return result


def assert_text_failopen_byte_exact(content: str, *, error_contains: str | None = None):
    """FAIL-OPEN contract: original returned byte-exact AND the failure is LOUD.

    Pins that a shape the pipeline cannot process is not silently mangled: the
    input comes back byte-identical, nothing is offloaded, and ``result.error``
    is set (surfaced, not swallowed). This documents a real caveat to the
    "everything gets compressed" claim without any silent loss.

    ``error_contains`` optionally pins WHY it failed open (a stable substring of
    ``result.error``), so an unrelated future decline cannot pass as this case.
    """
    result = run(content)
    assert output_of(result) == content, "fail-open must return the original byte-exact"
    assert not result.ccr_hashes, "fail-open must not offload anything"
    assert result.error is not None, (
        "compression declined but the failure was NOT surfaced in result.error — "
        "that would be a silent no-op indistinguishable from success"
    )
    if error_contains is not None:
        assert error_contains.lower() in result.error.lower(), (
            f"fail-open fired for an unexpected reason: {result.error!r} "
            f"(expected substring {error_contains!r})"
        )
    return result


def assert_array_distinct_recovery(items: list):
    """LOSSY:table contract: every DISTINCT input row/scalar is recoverable.

    Mirrors ``test_ccr_recovery_invariant._recover_from_output``: union the
    survivor rows visible in the output (raw scalars + decoded CSV-schema rows)
    with the rows parsed out of each surfaced drop pointer, and require that set
    to cover every distinct input item. A drop MUST actually happen (else the
    test is vacuously green). Returns (result, recovered_set).
    """
    content = json.dumps(items, ensure_ascii=False)
    result = run(content)
    assert result.error is None, f"unexpected fail-open error: {result.error!r}"
    out = output_of(result)

    recovered: set[str] = set()

    def _collect(node: object) -> None:
        if isinstance(node, list):
            for child in node:
                _collect(child)
        elif isinstance(node, dict):
            for value in node.values():
                _collect(value)
        elif isinstance(node, str):
            if "<<ccr:" not in node:
                recovered.add(canonical_repr(node))
        else:
            recovered.add(canonical_repr(node))

    try:
        tree = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        tree = out
    _collect(tree)
    if isinstance(tree, str):
        decode_csv_schema_into(tree, recovered)

    assert result.ccr_hashes, (
        "no CCR drop pointer surfaced — the fixture did not route lossy, so this "
        "recovery assertion is vacuous; grow/retune the fixture"
    )
    for h in result.ccr_hashes:
        payload = retrieve(h)
        assert payload is not None, f"drop pointer {h} dangles — dropped rows unrecoverable"
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            recovered.add(canonical_repr(payload))
            continue
        if isinstance(parsed, list):
            recovered.update(canonical_repr(x) for x in parsed)
        else:
            recovered.add(canonical_repr(parsed))

    distinct = {canonical_repr(x) for x in items}
    lost = distinct - recovered
    assert not lost, (
        f"{len(lost)} of {len(distinct)} distinct items unrecoverable from "
        f"output+store (silent loss); first: {list(lost)[:3]}"
    )
    return result, recovered


# ─── store introspection + fail-closed-redaction helpers (secrets rail) ──────


def store_entries():
    """Every live (hash, entry) in the active CCR store — for leak scans."""
    return list(get_compression_store()._backend.items())


def store_contains(substr: str) -> bool:
    """True iff any live stored original still carries *substr* (a leak)."""
    return any(substr in entry.original_content for _h, entry in store_entries())


def scrubbing_redactor(secret: str):
    """A pure ``raw -> redacted`` function that removes *secret* — the opt-in,
    fail-closed ``CompressConfig.redactor`` contract (``compress.py:141-156``)."""

    def _redact(raw: str) -> str:
        return raw.replace(secret, "[REDACTED]")

    return _redact


# ─── fake-secret builders (hook-safe: no verbatim token literal in source) ───


def fake_openai_key() -> str:
    return "sk-" + "MATRIXblindspot0123456789abcdefZ"


def fake_github_token() -> str:
    return "ghp" + "_" + "M" * 36


def fake_aws_key_id() -> str:
    return "AKIA" + "IOSFODNN7EXAMPLE"


def fake_password_kv() -> str:
    return "password=" + "hunter2-correct-horse-battery-staple"


def fake_pem_block() -> str:
    armor = "RSA " + "PRIVATE" + " KEY-----"
    return "-----BEGIN " + armor + "\nMIIEfakeBODYone\nMIIEfakeBODYtwo\n-----END " + armor


# ─── content-family generators (deterministic; sizes tuned past the offload floor) ──


def yaml_document(n: int = 120, *, secret: str | None = None) -> str:
    lines = ["---", "services:"]
    for i in range(n):
        api = secret if (secret is not None and i == 40) else f"token-{i}"
        lines += [
            f"  svc_{i}:",
            f"    image: registry.example.com/app:{i}.0.{i % 9}",
            f"    api_key: {api}",
            f"    replicas: {i % 5 + 1}",
            "    env:",
            f"      - NAME=service-{i}",
            f"      - TIMEOUT={i * 100}",
            f"    healthcheck: curl -f http://localhost:{8000 + i}/health || exit 1",
        ]
    return "\n".join(lines) + "\n"


def xml_document(n: int = 120, *, secret: str | None = None) -> str:
    rows = []
    for i in range(n):
        token_attr = f' token="{secret}"' if (secret is not None and i == 40) else ""
        rows.append(
            f'  <record id="{i}" ts="2026-07-{i % 28 + 1:02d}T00:00:{i % 60:02d}Z"{token_attr}>'
            f"<user>user_{i}</user><action>login</action>"
            f"<ip>10.0.{i % 256}.{(i * 7) % 256}</ip>"
            f"<status>{200 if i % 4 else 500}</status></record>"
        )
    return '<?xml version="1.0" encoding="UTF-8"?>\n<audit>\n' + "\n".join(rows) + "\n</audit>\n"


def sql_dump(n: int = 200, *, secret: str | None = None) -> str:
    head = "-- migration dump\nBEGIN;\n"
    inserts = []
    for i in range(n):
        val = secret if (secret is not None and i == 40) else f"x{i}"
        inserts.append(
            "INSERT INTO events (id, user_id, kind, payload, created_at) VALUES "
            f"({i}, {i % 50}, 'evt_{i % 9}', "
            f"'{{\"k\": {i}, \"v\": \"{val}\"}}', '2026-07-09 00:00:{i % 60:02d}');"
        )
    return head + "\n".join(inserts) + "\nCOMMIT;\n"


def typescript_source(n: int = 60, *, secret: str | None = None) -> str:
    parts = ["import { Foo } from './foo';\n"]
    for i in range(n):
        cmt = f"  // key: {secret}\n" if (secret is not None and i == 20) else ""
        parts.append(
            f"export function handler{i}(req: Request, ctx: Ctx): Promise<Resp{i}> {{\n"
            f"{cmt}"
            f"  const x{i} = req.body.field{i} ?? {i};\n"
            f"  return ctx.db.query<Resp{i}>('select * from t{i} where a = $1', [x{i}]);\n"
            f"}}\n"
        )
    return "".join(parts)


def go_source(n: int = 60, *, secret: str | None = None) -> str:
    parts = ["package main\n\nimport (\n\t\"fmt\"\n\t\"net/http\"\n)\n"]
    for i in range(n):
        cmt = f"\t// secret {secret}\n" if (secret is not None and i == 20) else ""
        parts.append(
            f"func Handler{i}(w http.ResponseWriter, r *http.Request) error {{\n"
            f"{cmt}"
            f"\tvar v{i} int = {i}\n"
            f'\tfmt.Fprintf(w, "handler {i}: %d", v{i})\n'
            f"\treturn nil\n}}\n"
        )
    return "".join(parts)


def rust_source(n: int = 60, *, secret: str | None = None) -> str:
    parts = ["use std::collections::HashMap;\n"]
    for i in range(n):
        cmt = f"    // token {secret}\n" if (secret is not None and i == 20) else ""
        parts.append(
            f"pub fn transform_{i}(input: &[u8]) -> Result<Vec<u8>, Error> {{\n"
            f"{cmt}"
            f"    let mut out{i} = Vec::with_capacity({i});\n"
            f"    out{i}.extend_from_slice(input);\n"
            f"    Ok(out{i})\n}}\n"
        )
    return "".join(parts)


def java_source(n: int = 50, *, secret: str | None = None) -> str:
    parts = ["package com.example.app;\n\npublic class Handlers {\n"]
    for i in range(n):
        cmt = f"    // cred {secret}\n" if (secret is not None and i == 20) else ""
        parts.append(
            f"    public Response handle{i}(Request req{i}) throws IOException {{\n"
            f"{cmt}"
            f"        int v{i} = req{i}.getField({i});\n"
            f"        return new Response(v{i} * {i});\n"
            f"    }}\n"
        )
    return "".join(parts) + "}\n"


def c_source(n: int = 60, *, secret: str | None = None) -> str:
    parts = ["#include <stdio.h>\n#include <stdlib.h>\n"]
    for i in range(n):
        cmt = f"    /* key {secret} */\n" if (secret is not None and i == 20) else ""
        parts.append(
            f"int compute_{i}(int a, int b) {{\n"
            f"{cmt}"
            f"    int r{i} = a * {i} + b;\n"
            f"    return r{i};\n}}\n"
        )
    return "".join(parts)


def cpp_source(n: int = 60, *, secret: str | None = None) -> str:
    parts = ["#include <vector>\n#include <string>\n"]
    for i in range(n):
        cmt = f"    // secret {secret}\n" if (secret is not None and i == 20) else ""
        parts.append(
            f"template <typename T> T process_{i}(const std::vector<T>& in) {{\n"
            f"{cmt}"
            f"    T acc{i} = T{{}};\n"
            f"    for (const auto& x : in) acc{i} += x * {i};\n"
            f"    return acc{i};\n}}\n"
        )
    return "".join(parts)


def ansi_log(n: int = 200, *, secret: str | None = None) -> str:
    out = []
    for i in range(n):
        tail = f" key=\x1b[33m{secret}\x1b[0m" if (secret is not None and i == 40) else ""
        out.append(
            f"\x1b[3{i % 8}m[{i:04d}]\x1b[0m \x1b[1mINFO\x1b[0m worker-{i % 9} "
            f"processed batch {i} in {i * 3}ms status=\x1b[32mOK\x1b[0m{tail}"
        )
    return "\n".join(out) + "\n"


def crlf_log(n: int = 300, *, secret: str | None = None) -> str:
    out = []
    for i in range(n):
        extra = f" secret={secret}" if (secret is not None and i == 40) else ""
        out.append(f"line {i} field_a=val_{i} field_b={i * 7} status={'ok' if i % 3 else 'err'}{extra}")
    return "\r\n".join(out) + "\r\n"


def cjk_emoji_combining(n: int = 150) -> str:
    # CJK + astral emoji (ZWJ family) + combining marks + bidi override + astral math.
    return "".join(
        f"记录{i}: ユーザー{i} \U0001f468‍\U0001f469‍\U0001f467‍\U0001f466 "
        f"café☕ Ω≈ç√ \U0001d54f\U0001d550\U0001d551 "
        f"‮RTL{i}‬ combining á̈ line {i}\n"
        for i in range(n)
    )


def astral_only(n: int = 300) -> str:
    return "".join(f"\U0001f600\U0001d400\U00020000 {i}\n" for i in range(n))


def bidi_override(n: int = 300) -> str:
    return "".join(f"file{i} ‮{i}txt.exe‬ normal{i}\n" for i in range(n))


def null_byte_text(n: int = 300) -> str:
    return "".join(f"row{i}\x00field{i}\x00end{i}\n" for i in range(n))


def latin1_blob() -> str:
    # A str whose code points came from a non-UTF-8 (latin-1) byte source: every
    # byte 0x00..0xFF as a character, repeated past the offload floor.
    raw = bytes(i % 256 for i in range(20000))
    return raw.decode("latin-1")


def base64_lines(n: int = 400) -> str:
    return "".join(
        base64.b64encode(bytes((i * j) % 256 for j in range(48))).decode() + "\n" for i in range(n)
    )


def base64_blob_single_line(size: int = 60000) -> str:
    return base64.b64encode(bytes((i * 7) % 256 for i in range(size))).decode()


def markdown_with_fences(n: int = 40) -> str:
    parts = ["# Title\n\nSome intro text.\n"]
    for i in range(n):
        parts.append(
            f"## Section {i}\n\nProse before code block {i} explaining behavior in detail.\n\n"
            f"```python\ndef fn_{i}(a, b):\n    return a * {i} + b  # inline\n```\n\n"
            f"More prose after block {i} with additional descriptive commentary text.\n"
        )
    return "".join(parts)


def deeply_nested_json(depth: int = 150) -> str:
    # A padded leaf keeps this comfortably above the token floor while staying
    # >=100 levels deep (the brief's edge). ``depth`` structural levels.
    node = '"' + "x" * 2000 + '"'
    for _ in range(depth):
        node = '{"n":' + node + "}"
    return node


def dotted_key_object(n: int = 300) -> str:
    return json.dumps({f"a.b.c.key_{i}": {"x.y": i, "z.w": f"val.{i}"} for i in range(n)})


def huge_single_line(megabytes: int = 2) -> str:
    return "x" * (megabytes * 1024 * 1024)


def lone_surrogate_text(n: int = 300) -> str:
    # A lone surrogate (U+D800) makes the str un-encodable as UTF-8 at the FFI.
    return "".join(f"row{i} \ud800 tail{i}\n" for i in range(n))
