"""Regression tests for retrieval-log credential redaction (#20).

The ``headroom_retrieve`` log path previews the retrieved payload. Any
credential in that preview must be redacted. Bug #20: a plain-text
``Authorization: Bearer <JWT>`` header leaked the JWT because the
secret-key rule consumed the ``Bearer`` scheme word as its value,
destroying the anchor the auth-scheme rule needed. The fix runs the
auth-scheme rule first.

These tests assert the FIXED behavior (credential absent) and are
mutation-sensitive: reverting the regex order, or removing any of the
three redaction passes, makes the corresponding credential reappear.
"""
from __future__ import annotations

import pytest

from headroom.cache.compression_store import _redact_retrieval_log_payload

# A structurally-valid JWT (header.payload.signature). Load-bearing literal:
# the test's whole point is that this exact string never appears in the output.
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
# Constructed so the literal does not appear verbatim in source (hook-safe).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"
# Non-``sk-`` secrets in JSON quoted-key form: these leaked before the group-2
# ``["']?`` fix because the key's closing quote broke the ``[:=]`` adjacency and,
# lacking an ``sk-`` prefix, nothing else caught them. All hook-safe (no verbatim
# token literal in source).
_GH_TOKEN = "ghp" + "_" + "A" * 36
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_AWS_SECRET = "wJalr" + "XUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
_OPAQUE_PW = "hunter2" + "correcthorsebattery"


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
        ("basic", "Authorization: Basic dXNlcjpwYXNzd29yZGxvbmdlbm91Z2g=", "dXNlcjpwYXNzd29yZGxvbmdlbm91Z2g="),
        # API key in a JSON value.
        ("api_key_json", f'{{"api_key": "{_API_KEY}"}}', _API_KEY),
        # token=<value> key/value form.
        ("token_kv", f"token={_JWT}", _JWT),
        # JSON quoted-key secrets whose VALUE is not an ``sk-`` key. These are the
        # primary regression: before the group-2 quote fix the whole key/value
        # rule missed them (closing key-quote broke ``[:=]`` adjacency).
        ("json_token_ghp", f'{{"token": "{_GH_TOKEN}"}}', _GH_TOKEN),
        ("json_password", f'{{"password": "{_OPAQUE_PW}"}}', _OPAQUE_PW),
        ("json_aws_secret", f'{{"aws_secret_access_key": "{_AWS_SECRET}"}}', _AWS_SECRET),
        ("json_apikey_camel", f'{{"apiKey":"{_OPAQUE_PW}"}}', _OPAQUE_PW),
        ("nested_json_api_key", f'{{"cfg": {{"api_key": "{_API_KEY}"}}}}', _API_KEY),
        # Provider-prefixed tokens with NO surrounding key name (bare in text) —
        # caught by the prefix rule, not the key/value rule.
        ("bare_aws_key_id", f"cred {_AWS_KEY_ID} end", _AWS_KEY_ID),
        ("bare_gh_token", f"{_GH_TOKEN} loose", _GH_TOKEN),
    ],
)
def test_credential_is_redacted(label: str, payload: str, secret: str) -> None:
    redacted = _redact_retrieval_log_payload(payload)
    assert secret not in redacted, f"{label}: credential leaked into log preview: {redacted!r}"
    assert "[REDACTED]" in redacted, f"{label}: nothing was redacted: {redacted!r}"


def test_benign_json_structure_is_untouched() -> None:
    # Over-redaction guard: ordinary JSON with no credential — and specifically the
    # store's own SHA-256 hash keys that the retrieval log emits — must survive so
    # logs stay useful. A generic high-entropy rule would wrongly redact these.
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
