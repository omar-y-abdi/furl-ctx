"""B3 SECURITY: fail-closed content redactor + purge surface.

These tests PROVE the security invariant end to end:

* a configured redactor scrubs content BEFORE it is compressed/offloaded, so a
  stored (and later retrievable) entry never contains the secret;
* a RAISING redactor makes ``compress()`` raise and leaks nothing — no entry is
  stored, and the caller's input is untouched (fail-closed);
* with no redactor, output is byte-identical to today (regression guard);
* ``purge`` deletes a stored original by hash, is a loud False on a miss, and
  respects the active CCR namespace.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx import CompressConfig, compress, purge, retrieve
from furl_ctx.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from furl_ctx.transforms.csv_ingest import raw_recovery_hash

# A high-entropy secret that must never survive into the store. Assembled so no
# verbatim credential literal sits in source (hook-safe, mirrors the redaction
# suite's ``sk-`` trick).
_SECRET = "sk-" + "SUPERSECRETvalue0123456789abcdef"


@pytest.fixture(autouse=True)
def _builtins_off(monkeypatch):
    """This file pins the CONFIGURED-redactor + byte-exact-default contract in
    isolation. The ``sk-``-shaped ``_SECRET`` would otherwise also be caught by
    the ON-by-default built-in credential redactor (audit Crit-4), scrubbing it
    to a different marker and changing the stored hash. Opt the built-ins out so
    these tests exercise exactly the configured redactor / raw-store path they
    were written for; the default-on behavior is pinned in test_redaction_env.py."""
    monkeypatch.setenv("FURL_REDACT_BUILTINS", "0")


def _envelope(secret: str, n: int = 200) -> str:
    """Offload-sized JSON envelope carrying ``secret`` in every row's value.

    Same ``{"data": [...], "total": n}`` shape ``test_retrieve_exports.py`` uses
    to drive a real CCR offload through ``compress()``.
    """
    return json.dumps(
        {"data": [{"id": i, "value": f"row-{i}-{secret}"} for i in range(n)], "total": n}
    )


def _redactor(raw: str) -> str:
    return raw.replace(_SECRET, "[REDACTED]")


def _store_contains_secret() -> bool:
    """True iff any LIVE entry in the active store still carries the secret."""
    store = get_compression_store()
    return any(_SECRET in entry.original_content for _h, entry in store._backend.items())


# --------------------------------------------------------------------------- #
# Redactor — fail-closed + scrubs stored content
# --------------------------------------------------------------------------- #


def test_redactor_scrubs_content_before_store() -> None:
    """The stored (retrievable) original is the REDACTED one — secret gone."""
    reset_compression_store()
    try:
        raw_env = _envelope(_SECRET)
        redacted_env = _redactor(raw_env)
        assert _SECRET in raw_env  # precondition: the secret is really present
        assert _SECRET not in redacted_env

        result = compress(
            [{"role": "tool", "content": raw_env}],
            model="gpt-4o",
            config=CompressConfig(redactor=_redactor),
        )

        # The offloaded entry is keyed by the REDACTED bytes (compression only
        # ever saw redacted content), and retrieving it returns the redacted
        # original — the secret is not recoverable.
        h = raw_recovery_hash(redacted_env)
        assert h in result.ccr_hashes
        recovered = retrieve(h)
        assert recovered == redacted_env
        assert recovered is not None and _SECRET not in recovered

        # And no live entry anywhere in the store carries the secret.
        assert not _store_contains_secret()
    finally:
        reset_compression_store()


def test_raising_redactor_fails_closed_and_leaks_nothing() -> None:
    """A redactor that raises => compress() RAISES; nothing is stored."""
    reset_compression_store()
    try:
        raw_env = _envelope(_SECRET)
        original = [{"role": "tool", "content": raw_env}]
        snapshot = json.dumps(original)  # to prove the input is untouched

        def _boom(_content: str) -> str:
            raise RuntimeError("redactor exploded")

        # Fail-closed: the exception propagates out of compress() rather than
        # being swallowed by the fail-open BaseException handler.
        with pytest.raises(RuntimeError, match="redactor exploded"):
            compress(original, model="gpt-4o", config=CompressConfig(redactor=_boom))

        # Nothing unredacted (or redacted) reached the store — no compression ran.
        assert not _store_contains_secret()
        assert retrieve(raw_recovery_hash(raw_env)) is None
        # The caller's input list/dicts were not mutated.
        assert json.dumps(original) == snapshot
    finally:
        reset_compression_store()


def test_no_redactor_is_byte_identical() -> None:
    """No redactor configured => output matches the un-redacted baseline exactly."""
    reset_compression_store()
    try:
        env = _envelope(_SECRET)
        baseline = compress([{"role": "tool", "content": env}], model="gpt-4o")
        reset_compression_store()
        with_default_cfg = compress(
            [{"role": "tool", "content": env}],
            model="gpt-4o",
            config=CompressConfig(),
        )
        assert with_default_cfg.messages == baseline.messages
        assert with_default_cfg.ccr_hashes == baseline.ccr_hashes
        assert with_default_cfg.error is None and baseline.error is None
    finally:
        reset_compression_store()


def test_redactor_does_not_mutate_caller_messages() -> None:
    """Redaction builds new dicts; the caller's list and dicts are untouched."""
    reset_compression_store()
    try:
        raw_env = _envelope(_SECRET)
        original = [{"role": "tool", "content": raw_env}]
        original_dict = original[0]

        compress(original, model="gpt-4o", config=CompressConfig(redactor=_redactor))

        # Same object identity, same (unredacted) content — nothing mutated.
        assert original[0] is original_dict
        assert original[0]["content"] == raw_env
        assert _SECRET in original[0]["content"]
    finally:
        reset_compression_store()


def test_non_string_content_passes_through_redactor() -> None:
    """Block-format (list) content is not a str — it must pass through untouched."""
    reset_compression_store()
    try:
        block_msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        calls: list[str] = []

        def _recording_redactor(raw: str) -> str:
            calls.append(raw)
            return raw

        result = compress(
            [block_msg],
            model="gpt-4o",
            config=CompressConfig(redactor=_recording_redactor),
        )
        # The redactor was never handed the list content.
        assert calls == []
        assert result.error is None
    finally:
        reset_compression_store()


# --------------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------------- #


def test_purge_deletes_stored_entry() -> None:
    """After purge, the hash is no longer retrievable and purge returned True."""
    reset_compression_store()
    try:
        env = _envelope(_SECRET)
        compress([{"role": "tool", "content": env}], model="gpt-4o")
        h = raw_recovery_hash(env)
        assert retrieve(h) == env  # stored and retrievable first

        assert purge(h) is True
        assert retrieve(h) is None  # gone
        # Purging again is a loud False (already absent).
        assert purge(h) is False
    finally:
        reset_compression_store()


def test_purge_absent_hash_is_false() -> None:
    reset_compression_store()
    try:
        assert purge("0" * 24) is False
    finally:
        reset_compression_store()


def test_purge_respects_namespace() -> None:
    """purge() acts on the SAME namespace-scoped store retrieve() reads.

    Namespaced isolation is realized by binding the tenant store onto the
    ``_request_ccr_store`` ContextVar (the seam ``compress()`` uses); ``purge``
    resolves the active store via ``get_compression_store()`` exactly like the
    retrieve path, so a purge issued under tenant-A's bound store cannot touch
    tenant-B's isolated entry.
    """
    from furl_ctx.cache.compression_store import (
        _request_ccr_store,
        resolve_ccr_namespace_store,
    )

    reset_compression_store()
    try:
        env = _envelope(_SECRET)
        h = raw_recovery_hash(env)
        store_a = resolve_ccr_namespace_store("tenant-a", None)
        store_b = resolve_ccr_namespace_store("tenant-b", None)
        assert store_a is not None and store_b is not None

        # Store the entry in tenant-a's isolated store under the raw-recovery
        # key (``explicit_hash=h``) so it is addressed exactly like the offload
        # path does — the store's default key is SHA-256[:24], not this MD5[:24].
        store_a.store(env, env, explicit_hash=h)
        assert store_a.retrieve(h) is not None

        # Bind tenant-B's store as the active request store, then purge(h): it
        # resolves store_b (not store_a), finds nothing, returns False — and
        # tenant-a's entry is untouched.
        token = _request_ccr_store.set(store_b)
        try:
            assert purge(h) is False
            assert retrieve(h) is None  # store_b never had it
        finally:
            _request_ccr_store.reset(token)

        # Now bind tenant-A's store: purge(h) resolves store_a and deletes it.
        token = _request_ccr_store.set(store_a)
        try:
            assert retrieve(h) == env
            assert purge(h) is True
            assert retrieve(h) is None
        finally:
            _request_ccr_store.reset(token)
    finally:
        reset_compression_store()
