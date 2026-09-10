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
    # Real PGP armor is ``PGP PRIVATE KEY BLOCK``.
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
    # An encrypted traditional PEM carries RFC-1421 headers between the armor and the body.
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
    # The module's stated property — "a FIXED length independent of the secret, so the redacted span
    # never leaks the secret's length" — only becomes true once the body is inside the redacted span.
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


@pytest.mark.parametrize(
    "separator",
    ["", " ", "\t", "\n", "\r\n", "\\n", "  \t"],
    ids=["none", "space", "tab", "newline", "crlf", "escaped-newline", "mixed-blanks"],
)
def test_truncated_body_is_scrubbed_whatever_separates_it_from_the_armor(
    separator: str,
) -> None:
    # Review F-1: every run used to require a leading NEWLINE, including the first, so a truncated body abutting the
    # armor with no separator, or with only a space or a tab, kept its key material in cleartext under the marker.
    out = _redactor()("-----BEGIN RSA " + _ARMOR + separator + _BODY)
    assert _BODY not in out
    assert out == _MARKER


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
@pytest.mark.parametrize("separator", ["\n", ""], ids=["newline", "abutting"])
def test_truncated_tail_base64_floor_boundary(separator: str, run_length: int, eaten: bool) -> None:
    # Pins the 16-character floor that separates "key material" from "a word", on BOTH the newline-separated run and the F-1 abutting run.
    text = "-----BEGIN RSA " + _ARMOR + separator + "A" * run_length
    out = _redactor()(text)
    assert out == (_MARKER if eaten else _MARKER + separator + "A" * run_length)


def test_prose_abutting_the_armor_survives_byte_exact() -> None:
    # The precision cost of the F-1 widening is bounded by the 16-character floor: ordinary prose beside an armor, with
    # or without a separator, is still left alone because its first word is far short of 16 unbroken base64 characters.
    for tail in (" see the docs for details", "see docs", "\tnotes below"):
        text = "-----BEGIN RSA " + _ARMOR + tail
        assert _redactor()(text) == _MARKER + tail


def test_abutting_run_at_or_past_the_floor_is_the_disclosed_over_redaction() -> None:
    # The honest other half of the trade-off, pinned so it stays deliberate: a 16+ character unbroken alphanumeric word abutting the armor IS eaten.
    text = "-----BEGIN RSA " + _ARMOR + "Supercalifragilistic and the rest survives"
    assert _redactor()(text) == _MARKER + " and the rest survives"


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
    # The block interior stops at the next 5-hyphen armor run, so an unterminated key cannot
    # reach forward to a LATER key's ``-----END`` and destroy every retrievable byte in between.
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
    # Deliberate, documented: the END armor's label is not tied to the BEGIN armor's, so a malformed pair takes the span between them.
    text = "-----BEGIN RSA " + _ARMOR + "\nnot really a key\n-----END EC " + _ARMOR
    assert _redactor()(text) == _MARKER


def test_ordinary_content_with_hyphen_rules_is_untouched() -> None:
    # Markdown/ASCII separators are 5+ hyphen runs too. Nothing here is a key.
    text = "summary\n-----\nsection two\n------------\nend of report\n"
    assert _redactor()(text) == text


# ─── the two bounds, pinned so a future edit is deliberate ───────────────────


def test_block_body_bound_falls_back_to_truncated_branch() -> None:
    # ``_PEM_BLOCK_BODY``'s 100k ceiling is ~30x a 4096-bit RSA PEM. A block that exceeds it cannot match the full-block branch, and the point of
    # this pin is that the fallback still takes the armor and the body: less than the whole block (the trailing END armor survives), never nothing.
    oversized = "\n".join([_BODY] * 2000)  # ~126k chars, past the 100k bound
    assert len(oversized) > 100_000
    end_armor = "-----END RSA " + _ARMOR
    out = _redactor()("-----BEGIN RSA " + _ARMOR + "\n" + oversized + "\n" + end_armor)
    assert _BODY not in out, "key material must never survive either branch"
    assert out == _MARKER + "\n" + end_armor


def test_truncated_run_bound_is_2048_runs() -> None:
    # ``_PEM_TRUNCATED_BODY``'s {0,2048} ceiling. 2048 base64 runs is ~128 KB of body, far past
    # any real key, but the residual is honest: past the bound the remaining runs survive.
    redactor = _redactor()
    armor = "-----BEGIN RSA " + _ARMOR
    at_bound = "\n".join([_BODY] * 2048)
    assert redactor(armor + "\n" + at_bound) == _MARKER

    past_bound = "\n".join([_BODY] * 2049)
    out = redactor(armor + "\n" + past_bound)
    assert out == _MARKER + "\n" + _BODY, "exactly the 2049th run should survive"


# ─── cost: the scan must stay bounded on adversarial input ───────────────────


def test_block_scan_stays_bounded_on_packed_armor_runs() -> None:
    # The block interior is "any char that does not open a 5-hyphen run" A naive ``[\s\S]*?`` interior turns this exact input into 12 s of backtracking;
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
