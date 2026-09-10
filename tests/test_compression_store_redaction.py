"""Regression tests for retrieval-log credential redaction (#20, SEC-4).

The ``furl_retrieve`` log path previews the retrieved payload. Any
credential in that preview must be redacted. Bug #20: a plain-text
``Authorization: Bearer <JWT>`` header leaked the JWT because the
secret-key rule consumed the ``Bearer`` scheme word as its value,
destroying the anchor the auth-scheme rule needed. The fix runs the
auth-scheme rule first.

SEC-4 (round 6) closed four live-probed gaps on top of that suite:
URL-embedded credentials (``scheme://user:pass@host``), PEM private-key
blocks, bare JWTs with no ``Bearer`` prefix, and multi-word quoted secret
values (the old value class stopped at the first space, leaking the tail).

These tests assert the FIXED behavior (credential absent) and are
mutation-sensitive: reverting the regex order, or removing any of the
redaction passes, makes the corresponding credential reappear.
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.compression_store import _redact_retrieval_log_payload

# A structurally-valid JWT (header.payload.signature). Load-bearing literal:
# the test's whole point is that this exact string never appears in the output.
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
# Constructed so the literal does not appear verbatim in source (hook-safe).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"
# Non-``sk-`` secrets in JSON quoted-key form: these leaked before the group-2 ``["']?`` fix because the
# key's closing quote broke the ``[:=]`` adjacency and, lacking an ``sk-`` prefix, nothing else caught them.
_GH_TOKEN = "ghp" + "_" + "A" * 36
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_AWS_SECRET = "wJalr" + "XUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
_OPAQUE_PW = "hunter2" + "correcthorsebattery"

# ─── SEC-4 shapes ──────────────────────────────────────────────────────────── URL-embedded password
# (``scheme://user:pass@host``). No trigger substring (``secret``/``password``/...) so ONLY the URL-credential rule can catch it.
_URL_PASSWORD = "p4ss-w0rd-9x"
# A quoted secret whose value spans MULTIPLE words: the leak mode is the tail ("battery staple") surviving
# after the first word was redacted, so the parametrized ``secret`` below is the TAIL, not the full phrase.
_MULTI_WORD_SECRET = "correct horse battery staple"
_MULTI_WORD_TAIL = "battery staple"
# PEM private-key block. The body line is the ``secret`` that must never survive into the log preview.
_PEM_BODY_LINE = "MIIEfakebodyLINEONE"


def _pem_block(label: str, newline: str = "\n") -> str:
    armor = label + "PRIVATE" + " KEY-----"
    return (
        "-----BEGIN "
        + armor
        + newline
        + _PEM_BODY_LINE
        + newline
        + "MIIEfakebodyLINETWO"
        + newline
        + "-----END "
        + armor
    )


@pytest.mark.parametrize(
    "label,payload,secret",
    [
        # The #20 repro: plain-text Authorization header. MUST redact the JWT.
        ("plain_bearer", f"Authorization: Bearer {_JWT}", _JWT),
        # The path that already worked (JSON-quoted header) — must stay redacted.
        ("json_bearer", f'{{"Authorization": "Bearer {_JWT}"}}', _JWT),
        # Bare token with no scheme word — secret-key rule grabs it directly.
        ("noscheme", f"Authorization: {_JWT}", _JWT),
        # Basic scheme.
        (
            "basic",
            "Authorization: Basic dXNlcjpwYXNzd29yZGxvbmdlbm91Z2g=",
            "dXNlcjpwYXNzd29yZGxvbmdlbm91Z2g=",
        ),
        # API key in a JSON value.
        ("api_key_json", f'{{"api_key": "{_API_KEY}"}}', _API_KEY),
        # token=<value> key/value form.
        ("token_kv", f"token={_JWT}", _JWT),
        # JSON quoted-key secrets whose VALUE is not an ``sk-`` key.
        ("json_token_ghp", f'{{"token": "{_GH_TOKEN}"}}', _GH_TOKEN),
        ("json_password", f'{{"password": "{_OPAQUE_PW}"}}', _OPAQUE_PW),
        ("json_aws_secret", f'{{"aws_secret_access_key": "{_AWS_SECRET}"}}', _AWS_SECRET),
        ("json_apikey_camel", f'{{"apiKey":"{_OPAQUE_PW}"}}', _OPAQUE_PW),
        ("nested_json_api_key", f'{{"cfg": {{"api_key": "{_API_KEY}"}}}}', _API_KEY),
        # Provider-prefixed tokens with NO surrounding key name (bare in text) —
        # caught by the prefix rule, not the key/value rule.
        ("bare_aws_key_id", f"cred {_AWS_KEY_ID} end", _AWS_KEY_ID),
        ("bare_gh_token", f"{_GH_TOKEN} loose", _GH_TOKEN),
        # ── SEC-4 (a): URL-embedded credentials. The userinfo password leaked:
        # no key name precedes it, so the key/value rule never fired.
        (
            "url_creds_postgres",
            f"postgres://admin:{_URL_PASSWORD}@db.internal:5432/app",
            _URL_PASSWORD,
        ),
        (
            "url_creds_https",
            f"https://deploy:{_URL_PASSWORD}@git.example.com/repo.git",
            _URL_PASSWORD,
        ),
        (
            "url_creds_in_json_value",
            f'{{"db": "postgres://svc:{_URL_PASSWORD}@10.0.0.5/prod"}}',
            _URL_PASSWORD,
        ),
        # ── SEC-4 (b): PEM private-key blocks — the whole block must go.
        ("pem_pkcs8", "config:\n" + _pem_block("") + "\ndone", _PEM_BODY_LINE),
        ("pem_rsa", _pem_block("RSA "), _PEM_BODY_LINE),
        # JSON-embedded PEM: newlines are the two-char ``\n`` escape sequence.
        (
            "pem_json_escaped",
            '{"key_material": "' + _pem_block("OPENSSH ", newline="\\n") + '"}',
            _PEM_BODY_LINE,
        ),
        # ── SEC-4 (c): bare JWTs (no ``Bearer`` prefix, no sensitive key name).
        ("bare_jwt_text", f"cookie {_JWT} tail", _JWT),
        ("bare_jwt_json_value", f'{{"data": "{_JWT}"}}', _JWT),
        # ── SEC-4 (d): multi-word QUOTED secrets.
        ("quoted_multiword_json", f'{{"password": "{_MULTI_WORD_SECRET}"}}', _MULTI_WORD_TAIL),
        ("quoted_multiword_plain", f"password = '{_MULTI_WORD_SECRET}'", _MULTI_WORD_TAIL),
        ("quoted_multiword_token", f'client_secret: "{_MULTI_WORD_SECRET}"', _MULTI_WORD_TAIL),
    ],
)
def test_credential_is_redacted(label: str, payload: str, secret: str) -> None:
    redacted = _redact_retrieval_log_payload(payload)
    assert secret not in redacted, f"{label}: credential leaked into log preview: {redacted!r}"
    assert "[REDACTED]" in redacted, f"{label}: nothing was redacted: {redacted!r}"


def test_benign_json_structure_is_untouched() -> None:
    # Over-redaction guard: ordinary JSON with no credential — and specifically the store's
    # own SHA-256 hash keys that the retrieval log emits — must survive so logs stay useful.
    payload = f'{{"hash": "{"a" * 24}", "tool_name": "search", "count": 7}}'
    assert _redact_retrieval_log_payload(payload) == payload


def test_plain_bearer_redacts_both_scheme_and_token() -> None:
    # The exact #20 fix: the JWT after `Bearer` must be gone. Pin the literal
    # output so the fix can't silently regress to leaking the token.
    out = _redact_retrieval_log_payload(f"Authorization: Bearer {_JWT}")
    assert out == "Authorization: [REDACTED] [REDACTED]"


def test_non_credential_text_is_untouched() -> None:
    # Compression-neutral: ordinary content with no credential is unchanged.
    payload = "the quick brown fox jumps over the lazy dog 12345"
    assert _redact_retrieval_log_payload(payload) == payload


# ─── SEC-4 no-false-positive controls ────────────────────────────────────────


def test_url_without_credentials_is_untouched() -> None:
    # ``host:port`` is not userinfo: a normal URL (no ``user:pass@``) must survive byte-exact,
    # including ``@`` in the path and a bare username (no password, no colon) in the authority.
    for payload in (
        "connect to https://db.example.com:5432/app?sslmode=require please",
        "https://example.com:8080/path?q=1",
        "https://example.com:8080/x@y",
        "ftp://user@host/file",
    ):
        assert _redact_retrieval_log_payload(payload) == payload


def test_quoted_prose_is_untouched() -> None:
    # Trigger words INSIDE prose values (no ``:``/``=`` separator after them)
    # must not fire the key/value rule — quoted ordinary text stays intact.
    payload = '{"description": "the auth flow is documented", "note": "a password manager"}'
    assert _redact_retrieval_log_payload(payload) == payload


def test_public_certificate_block_is_untouched() -> None:
    # Only PRIVATE KEY armor is redacted; a public CERTIFICATE block is not a
    # secret and stays readable in the log preview.
    payload = "-----BEGIN CERTIFICATE-----\nMIICpublicbody\n-----END CERTIFICATE-----"
    assert _redact_retrieval_log_payload(payload) == payload


def test_url_credential_keeps_user_and_host_readable() -> None:
    # Pin the redaction SHAPE: only the password is cut, user and host survive
    # so the log line stays operationally useful.
    out = _redact_retrieval_log_payload(f"postgres://admin:{_URL_PASSWORD}@db.internal/app")
    assert out == "postgres://admin:[REDACTED]@db.internal/app"


def test_quoted_multiword_value_is_fully_redacted() -> None:
    # Pin the full-value redaction shape: the whole quoted phrase collapses to
    # one [REDACTED] with the surrounding JSON structure intact.
    out = _redact_retrieval_log_payload(f'{{"password": "{_MULTI_WORD_SECRET}"}}')
    assert out == '{"password": "[REDACTED]"}'
