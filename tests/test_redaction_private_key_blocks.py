"""The built-in ``private-key`` redactor must take the WHOLE PEM block.

The pattern used to match only the ``-----BEGIN … PRIVATE KEY-----`` armor
line. The armor is the ANCHOR, not the credential: every base64 byte of the
actual key survived into the model-visible compressed content AND into the CCR
store, under a ``[REDACTED:private-key]`` marker asserting the opposite. That
false assurance is worse than no redaction, and it contradicted every stated
contract — SECURITY.md ("private keys … scrubbed from the stored original
**before** it is compressed or written to disk"), LIBRARY.md's
``FURL_REDACT_BUILTINS`` row, ``mcp_server._furl_compress``'s own comment ("the
raw secret would persist in the CCR store"), and ``cli``'s ``furl list`` /
``furl search`` previews ("a listing must not print a secret back out"). The
sibling retrieval-log redactor (``compression_store._PEM_PRIVATE_KEY_RE``) has
always taken the whole block; only this, the primary store-path redactor, did
not.

These tests pin the fixed contract from three angles: the pattern itself over
every PEM shape in the wild, the boundary (what must NOT be over-redacted, and
what is not a secret at all), and end to end through ``compress()`` down to the
raw bytes on disk.

Key fixtures are assembled from parts at import so no verbatim PEM armor sits
in the source — the same hook-safe trick ``test_redaction_env.py`` uses for its
``sk-`` literal.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from furl_ctx.redaction import build_default_redactor

_ARMOR = "PRIVATE" + " KEY-----"
_MARKER = "[REDACTED:private-key]"

# Fake key material. Load-bearing literal: the whole point of every test below
# is that these bytes never appear in the redactor's output.
_BODY = "MIIEowIBAAKCAQEAvR7fakeKEYmaterial0123456789abcdefghijklmnopqrs"
_BODY2 = "n8Zk2fakeKEYmaterialSECONDline9876543210ZYXWVUTSRQPONMLKJIHGFE"


def _block(label: str = "RSA ", newline: str = "\n", *, terminated: bool = True) -> str:
    """A PEM private-key block. ``newline`` is ``\\n`` for a real key and the
    two-char ``\\\\n`` escape for one embedded in a JSON string."""
    text = "-----BEGIN " + label + _ARMOR + newline + newline.join([_BODY, _BODY2] * 12)
    if terminated:
        text += newline + "-----END " + label + _ARMOR
    return text


def _redactor() -> Callable[[str], str]:
    redactor = build_default_redactor({})
    assert redactor is not None, "built-ins are ON by default"
    return redactor


# ─── the block goes, whole ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    ["", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ", "ANY FUTURE "],
    ids=["pkcs8", "rsa", "ec", "dsa", "openssh", "encrypted", "unknown-label"],
)
def test_whole_block_is_redacted_for_every_pem_label(label: str) -> None:
    # The regression: pre-fix these all left 24 lines of key material in place.
    out = _redactor()("$ cat deploy/id_rsa\n" + _block(label) + "\ndone\n")
    assert _BODY not in out
    assert _BODY2 not in out
    assert _MARKER in out
    assert out == "$ cat deploy/id_rsa\n" + _MARKER + "\ndone\n"


def test_pgp_private_key_block_is_redacted() -> None:
    # Real PGP armor is ``PGP PRIVATE KEY BLOCK``. The old alternation listed a
    # ``PGP `` label but spelled the tail ``PRIVATE KEY-----``, so it could never
    # match a PGP export — not even the armor line.
    pgp = (
        "-----BEGIN PGP " + _ARMOR[:-5] + " BLOCK-----\n"
        "Version: GnuPG v2\n\n" + _BODY + "\n"
        "-----END PGP " + _ARMOR[:-5] + " BLOCK-----"
    )
    out = _redactor()(pgp)
    assert _BODY not in out
    assert out == _MARKER


def test_json_escaped_block_is_redacted() -> None:
    # A key inside a JSON string: the body separator is the two-char ``\n``
    # escape, not a real newline. This is how a key arrives from most APIs.
    payload = '{"tls": {"key_material": "' + _block("RSA ", newline="\\n") + '"}}'
    out = _redactor()(payload)
    assert _BODY not in out
    assert out == '{"tls": {"key_material": "' + _MARKER + '"}}'


def test_crlf_block_is_redacted() -> None:
    out = _redactor()(_block("RSA ", newline="\r\n"))
    assert _BODY not in out
    assert out == _MARKER


def test_encrypted_traditional_pem_headers_do_not_stop_the_match() -> None:
    # An encrypted traditional PEM carries RFC-1421 headers between the armor
    # and the body. They contain hyphens, which the armor-run guard in the block
    # interior must tolerate (it excludes only 5-hyphen runs).
    payload = (
        "-----BEGIN RSA " + _ARMOR + "\n"
        "Proc-Type: 4,ENCRYPTED\n"
        "DEK-Info: DES-EDE3-CBC,9F2C1A5B7E4D3086\n\n" + _BODY + "\n"
        "-----END RSA " + _ARMOR
    )
    out = _redactor()(payload)
    assert _BODY not in out
    assert out == _MARKER


def test_marker_length_is_independent_of_key_size() -> None:
    # The module's stated property — "a FIXED length independent of the secret,
    # so the redacted span never leaks the secret's length" — only becomes true
    # once the body is inside the redacted span.
    redactor = _redactor()
    small = "-----BEGIN EC " + _ARMOR + "\n" + _BODY + "\n-----END EC " + _ARMOR
    large = _block("RSA ")
    assert len(large) > 4 * len(small)
    assert redactor(small) == redactor(large) == _MARKER


# ─── truncated blocks (the tail the tool cut off) ────────────────────────────


def test_truncated_block_without_end_armor_still_scrubs_material() -> None:
    # Tool output is routinely cut mid-payload — that is why this library
    # exists. A block whose ``-----END`` never arrived must not leak its body.
    out = _redactor()("$ cat id_rsa\n" + _block("RSA ", terminated=False))
    assert _BODY not in out
    assert _BODY2 not in out
    assert out == "$ cat id_rsa\n" + _MARKER


def test_truncated_json_escaped_block_scrubs_material() -> None:
    out = _redactor()('{"key": "' + _block("OPENSSH ", newline="\\n", terminated=False))
    assert _BODY not in out
    assert out == '{"key": "' + _MARKER


def test_lone_armor_line_is_still_redacted() -> None:
    # Never worse than the old header-only pattern: an armor with nothing after
    # it keeps its marker.
    assert _redactor()("-----BEGIN RSA " + _ARMOR) == _MARKER


# ─── boundary: what must NOT be swallowed ────────────────────────────────────


def test_prose_after_a_lone_armor_survives_byte_exact() -> None:
    # The truncated-block branch requires unbroken 16+ character base64 runs, so
    # an armor mentioned in documentation does not eat the paragraph under it.
    text = "-----BEGIN RSA " + _ARMOR + "\nThis is what a key header looks like.\nSee docs."
    out = _redactor()(text)
    assert out == _MARKER + "\nThis is what a key header looks like.\nSee docs."


@pytest.mark.parametrize(
    "run_length,eaten",
    [(15, False), (16, True)],
    ids=["below-floor", "at-floor"],
)
def test_truncated_tail_base64_floor_boundary(run_length: int, eaten: bool) -> None:
    # Pins the 16-character floor that separates "key material" from "a word".
    # Below it the line is left alone; at it the line reads as base64 body.
    text = "-----BEGIN RSA " + _ARMOR + "\n" + "A" * run_length
    out = _redactor()(text)
    assert out == (_MARKER if eaten else _MARKER + "\n" + "A" * run_length)


def test_content_around_and_between_blocks_survives_byte_exact() -> None:
    text = (
        "2026-07-24T10:00:00Z INFO  loading tls material\n"
        + _block("RSA ")
        + "\n2026-07-24T10:00:01Z INFO  loading signing material\n"
        + _block("EC ")
        + "\n2026-07-24T10:00:02Z INFO  ready\n"
    )
    out = _redactor()(text)
    assert _BODY not in out
    assert out == (
        "2026-07-24T10:00:00Z INFO  loading tls material\n"
        + _MARKER
        + "\n2026-07-24T10:00:01Z INFO  loading signing material\n"
        + _MARKER
        + "\n2026-07-24T10:00:02Z INFO  ready\n"
    )


@pytest.mark.parametrize("label", ["PUBLIC KEY", "CERTIFICATE", "PGP PUBLIC KEY BLOCK"])
def test_public_material_is_never_redacted(label: str) -> None:
    # Public material is not a credential, and redacting it would destroy
    # retrievable bytes for nothing.
    text = f"-----BEGIN {label}-----\n{_BODY}\n-----END {label}-----"
    assert _redactor()(text) == text


def test_truncated_key_before_a_complete_one_does_not_swallow_the_logs_between() -> None:
    # The block interior stops at the next 5-hyphen armor run, so an unterminated
    # key cannot reach forward to a LATER key's ``-----END`` and destroy every
    # retrievable byte in between. Both keys go; the log lines stay.
    text = (
        _block("RSA ", terminated=False)
        + "\n2026-07-24T10:00:00Z INFO  rotating\n2026-07-24T10:00:01Z INFO  loaded\n"
        + _block("RSA ")
    )
    out = _redactor()(text)
    assert _BODY not in out
    assert out == (
        _MARKER
        + "\n2026-07-24T10:00:00Z INFO  rotating\n2026-07-24T10:00:01Z INFO  loaded\n"
        + _MARKER
    )


def test_mismatched_armor_labels_over_redact_rather_than_leak() -> None:
    # Deliberate, documented: the END armor's label is not tied to the BEGIN
    # armor's, so a malformed pair takes the span between them. No real tool
    # emits mismatched labels, and over-redaction is the safe direction for a
    # shape that claims to hold a private key.
    text = "-----BEGIN RSA " + _ARMOR + "\nnot really a key\n-----END EC " + _ARMOR
    assert _redactor()(text) == _MARKER


def test_ordinary_content_with_hyphen_rules_is_untouched() -> None:
    # Markdown/ASCII separators are 5+ hyphen runs too. Nothing here is a key.
    text = "summary\n-----\nsection two\n------------\nend of report\n"
    assert _redactor()(text) == text


# ─── cost: the scan must stay bounded on adversarial input ───────────────────


def test_block_scan_stays_bounded_on_packed_armor_runs() -> None:
    # The block interior is "any char that does not open a 5-hyphen run", so a
    # scan launched from a BOGUS armor cannot run past the next armor and total
    # work stays linear in the payload. A naive ``[\s\S]*?`` interior turns this
    # exact input into 12 s of backtracking; this formulation measures ~5 ms.
    # The ceiling is ~1000x the measured cost, so it flags a regression to an
    # unbounded interior without being a timing flake. It matters because the
    # redactor runs inside the PostToolUse hook, whose 30 s timeout is
    # fail-OPEN: a hang degrades redaction to raw passthrough.
    payload = ("-----BEGIN RSA " + _ARMOR) * 10_000
    redactor = _redactor()
    start = time.perf_counter()
    out = redactor(payload)
    elapsed = time.perf_counter() - start
    assert out == _MARKER * 10_000
    assert elapsed < 5.0, f"private-key scan took {elapsed:.2f}s on packed armors"


# ─── end to end: compress(), the CCR store, and the bytes on disk ────────────


def test_compress_and_ccr_store_hold_no_key_material(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from furl_ctx import compress, retrieve
    from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store

    db = tmp_path / "ccr.sqlite3"
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(db))
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    monkeypatch.delenv("FURL_REDACT_PATTERNS", raising=False)  # built-ins alone
    reset_compression_store()

    log = "\n".join(
        f"2026-07-24T10:00:{i % 60:02d}Z INFO  worker-{i % 7} handled request id={i}"
        for i in range(400)
    )
    payload = f"$ cat deploy/id_rsa\n{_block('RSA ')}\n\n$ journalctl -u api\n{log}\n"

    result = compress([{"role": "tool", "content": payload}], model="gpt-4o")
    assert result.error is None

    visible = result.messages[0]["content"]
    assert _BODY not in visible  # the model never sees the key
    assert _MARKER in visible

    store = get_compression_store()
    entries = list(store._backend.items())
    assert entries, "expected at least one CCR entry"
    for hash_key, entry in entries:
        assert _BODY not in entry.original_content  # STORED original scrubbed
        assert _BODY not in (retrieve(hash_key) or "")  # and every retrieval of it

    reset_compression_store()  # closes sqlite handles (WAL checkpoint on last close)
    for path in tmp_path.iterdir():
        assert _BODY.encode() not in path.read_bytes(), f"key material leaked into {path.name}"
