"""Cycle-2 A1 fix (c): the CCR Rust→Python mirror must obey the Python
store's own "no SILENT loss" contract (compression_store.py:234-244).

Defect being pinned
-------------------
A SmartCrusher row-drop commits the original to the EPHEMERAL, process-local
Rust store and emits a ``<<ccr:HASH>>`` pointer in the output. The store that
production ``/v1/retrieve`` reads is the *Python* ``compression_store``; the
mirror is the only thing that copies the dropped rows from Rust into Python.

Before this fix, the mirror swallowed a Python-store write failure at
``logger.debug`` and returned. When that fired, the rows were dropped from the
output (lossy), the Rust copy was ephemeral, and the Python copy never landed —
so a later ``retrieve()`` returned ``None`` and the recovery data was GONE,
silently. That is exactly the silent loss the store's contract forbids.

The fix makes the loss-causing branch FAIL-SAFE: the mirror raises
``CcrMirrorError``, which propagates to ``compress()``'s fail-open boundary
(compress.py:386). Fail-open discards the lossy output and returns the ORIGINAL
uncompressed messages — so the lossy drop never stands without a recovery copy.

Call-stack (verified; §4.2 moved the mirror onto typed refs — the
raise still rides the same boundary)::

    _mirror_single_hash_to_python_store  (store.store() except -> raise)
      -> _mirror_typed_refs
      -> _smart_crush_content              (smart_crusher.py, NOT wrapped)
      -> SmartCrusher.apply                (smart_crusher.py:1040/1067, NOT wrapped)
      -> TransformPipeline.apply           (pipeline.py:287 -> _breaker_record_failure(); raise)
      -> pipeline.apply
      -> compress()                        (compress.py:386 fail-open -> returns ORIGINAL)

Bite evidence
-------------
``test_store_write_failure_reverts_to_original`` was confirmed RED against the
pre-fix ``logger.debug``-and-return code: with the store patched to raise, the
old mirror swallowed the failure, compression PROCEEDED, and the output carried
the ``<<ccr:>>`` marker with rows dropped (``error`` was ``None`` — fail-open
never fired). After the fix it is GREEN: the output equals the original input.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest

from furl_ctx.cache import compression_store as cs
from furl_ctx.compress import compress
from furl_ctx.transforms.smart_crusher import (
    CcrMirrorError,
    SmartCrusher,
    SmartCrusherConfig,
)

# TEST-19: shared single-copy raising-store wrapper (was duplicated here).
from tests._fixtures import FailingStore as _FailingStore
from tests._fixtures import make_fail_open_sqlite_backend

# Row-drop fixture: the same 1000 distinct strings the recovery-invariant
# suite uses (``_NON_DICT_CASES["strings"]``). A homogeneous flat array this
# large takes SmartCrusher's lossy row-drop path and emits a ``<<ccr:>>``
# pointer — empirically confirmed before writing this test.
_ROW_DROP_ITEMS = [f"log-line-{i}-payload" for i in range(1000)]


@pytest.fixture
def store_writes_fail(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Patch ``get_compression_store`` so the singleton's ``.store`` raises.

    The mirror imports ``get_compression_store`` *inside* the function via
    ``from ..cache.compression_store import get_compression_store``; that name
    resolves to ``furl_ctx.cache.compression_store.get_compression_store`` at
    call time, so patching the attribute on that module intercepts it. The
    ``calls`` counter lets the test assert the patch actually fired (guards
    against a false GREEN from a wrong patch target).
    """
    real_get = cs.get_compression_store
    calls = {"n": 0}

    def fake_get() -> Any:
        calls["n"] += 1
        return _FailingStore(real_get())

    monkeypatch.setattr(cs, "get_compression_store", fake_get)
    return calls


def _tool_message(items: list[str]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "t1", "content": json.dumps(items)}


def _build_messages(tool_msg: dict[str, Any]) -> list[dict[str, Any]]:
    """A two-message conversation (user query + the tool output to crush).
    Returns fresh dict copies so the caller's ``tool_msg`` stays pristine for
    before/after comparison."""
    return [
        {"role": "user", "content": "find log-line-7-payload"},
        dict(tool_msg),
    ]


def test_store_write_failure_reverts_to_original(
    store_writes_fail: dict[str, int],
) -> None:
    """BEHAVIOR-LEVEL bite: when the Python store write fails during a
    row-drop crush, the mirror must NOT let the lossy output stand. The
    full ``compress()`` path reverts to the ORIGINAL messages (fail-safe),
    so nothing is silently lost.

    RED against the pre-fix debug-swallow (compression proceeded, marker in
    output, rows dropped); GREEN after (output == input)."""
    tool_msg = _tool_message(_ROW_DROP_ITEMS)

    result = compress(_build_messages(tool_msg))

    # The patch actually intercepted the store — without this, the assertions
    # below could pass for the wrong reason (no mirror attempted at all).
    assert store_writes_fail["n"] > 0, "store patch never fired; test target wrong"

    # FAIL-SAFE: the tool message content is byte-for-byte the original. No
    # rows dropped, no <<ccr:>> marker — compression reverted at the fail-open
    # boundary because the recovery copy could not be persisted.
    assert result.messages[1]["content"] == tool_msg["content"], (
        "row-drop output stood despite the recovery write failing — silent loss"
    )
    rendered = json.dumps(result.messages)
    assert "<<ccr:" not in rendered, "lossy CCR marker survived a failed recovery write"

    # And the failure was surfaced, not swallowed: fail-open records the error.
    assert result.error is not None, "fail-open did not fire; failure was swallowed"


def test_successful_mirror_still_compresses() -> None:
    """Control: with the real (working) store, the SAME fixture still takes
    the row-drop path and emits a recovery marker. This proves the bite test
    above asserts a behavior *change on failure*, not that compression is
    globally broken — and that the success path (the 23 recovery-invariant
    tests exercise) is untouched."""
    tool_msg = _tool_message(_ROW_DROP_ITEMS)

    result = compress(_build_messages(tool_msg))

    assert result.error is None
    assert result.messages[1]["content"] != tool_msg["content"], "row-drop did not fire"
    assert "<<ccr:" in json.dumps(result.messages), "recovery marker missing on success path"
    assert result.tokens_after < result.tokens_before


def test_mirror_raises_ccr_mirror_error_on_store_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNIT-level pin: ``_mirror_single_hash_to_python_store`` raises
    ``CcrMirrorError`` (not a silent return) when the store write fails for a
    hash that IS present in the Rust store. This is the exact branch
    (smart_crusher.py store.store() except) the fix converted from
    ``logger.debug`` + fall-through to a loud, fail-safe raise."""
    crusher = SmartCrusher(config=SmartCrusherConfig())

    # Seed the Rust store with the store UNPATCHED so ``ccr_get`` returns a
    # canonical payload (the mirror only attempts the Python write when Rust
    # has it). The seeding crush itself mirrors fine; we patch AFTER.
    crushed = crusher.crush_array_json(json.dumps(_ROW_DROP_ITEMS), query="x")
    ccr_hash = crushed.get("ccr_hash")
    assert ccr_hash, "fixture did not produce a row-drop hash"

    # Now make the Python store write fail and re-mirror the seeded hash.
    real_get = cs.get_compression_store
    monkeypatch.setattr(cs, "get_compression_store", lambda: _FailingStore(real_get()))

    with pytest.raises(CcrMirrorError):
        crusher._mirror_single_hash_to_python_store(
            ccr_hash,
            strategy="smart_crusher_row_drop",
            query_context="x",
            tool_name=None,
        )


def test_mirror_module_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNIT-level pin: when the compression_store module cannot be imported,
    the mirror raises ``CcrMirrorError`` rather than returning silently —
    the dropped rows would otherwise be unrecoverable in the Python store.

    Simulates the stripped-build ImportError branch by making the in-function
    ``from ..cache.compression_store import get_compression_store`` fail."""
    import builtins

    crusher = SmartCrusher(config=SmartCrusherConfig())
    crushed = crusher.crush_array_json(json.dumps(_ROW_DROP_ITEMS), query="x")
    ccr_hash = crushed.get("ccr_hash")
    assert ccr_hash, "fixture did not produce a row-drop hash"

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "furl_ctx.cache.compression_store" or name.endswith("cache.compression_store"):
            raise ImportError("simulated stripped build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(CcrMirrorError):
        crusher._mirror_single_hash_to_python_store(
            ccr_hash,
            strategy="smart_crusher_row_drop",
            query_context="x",
            tool_name=None,
        )


def test_logging_level_check_is_quiet_on_success(caplog: pytest.LogCaptureFixture) -> None:
    """On the success path the mirror logs nothing at ERROR — guards against a
    regression that makes every compression noisy."""
    tool_msg = _tool_message(_ROW_DROP_ITEMS)
    with caplog.at_level(logging.ERROR):
        compress(_build_messages(tool_msg))
    mirror_errors = [r for r in caplog.records if "mirror" in r.getMessage().lower()]
    assert not mirror_errors, f"unexpected mirror ERROR logs on success: {mirror_errors}"


# ─── COR-5: a TYPED hash missing from the Rust store is loss, not "leaked" ──
#
# The mirror's store-miss branch used to debug-skip EVERY miss as "marker
# leaked from elsewhere" — an excuse valid only for SCRAPED hashes (substring-
# scanned out of rendered text, where a foreign marker really can appear).
# For a TYPED hash (``CrushResult.ccr_hashes`` / ``crush_array_json``'s
# ``ccr_hash``) the engine ITSELF reported the drop, so a miss means the
# entry was already evicted/expired: the surfaced ``<<ccr:HASH>>`` marker
# dangles and the dropped rows are gone — silent loss. COR-4 bounds the
# store flood at the producer, but in_memory.rs documents the residual
# window "cannot be fully eliminated"; Python is the last place to catch it.


class _CcrGetMissing:
    """Wrap a real Rust SmartCrusher so every ``ccr_get`` MISSES (returns
    ``None``) while every other call (``crush``, ``crush_array_json``, …)
    delegates to the real engine.

    Forces the COR-5 window deterministically: a typed row-drop hash the engine
    surfaced whose Rust-store entry is gone by the time the mirror reads it.
    That window used to open by accident under the per-row chunk store flood
    (F8) — six sub-arrays writing ~1450 entries into the 1000-slot FIFO evicted
    the first arrays' whole-blobs mid-crush. Design A closed the flood at the
    producer (one whole-blob per drop, never ~240 chunks), so no fixture can
    evict a just-written entry within one crush of the fixed-capacity store.
    The fail-safe still matters (TTL expiry, a genuine >capacity aggregate), so
    the miss is induced directly instead of by flooding — no wall-clock, no
    load sensitivity."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.ccr_get_calls = 0

    def ccr_get(self, _hash: str) -> None:
        self.ccr_get_calls += 1
        return None

    def __getattr__(self, name: str) -> Any:
        # Everything except ccr_get / _real / ccr_get_calls delegates, so the
        # real crush still runs and real dropped refs are still produced.
        return getattr(self._real, name)


def test_typed_hash_evicted_before_mirror_raises() -> None:
    """COR-5 bite (deterministic, real crush): when a TYPED row-drop hash misses
    the Rust store at mirror time, ``crush()`` must raise ``CcrMirrorError`` —
    never debug-skip the miss as a marker "leaked from elsewhere" (impossible
    for a hash the engine itself surfaced).

    The real crush produces real dropped refs; ``_CcrGetMissing`` then makes
    every ``ccr_get`` miss — exactly a typed entry evicted/expired between the
    drop and the mirror. Under the old per-row chunk flood this window opened by
    accident; Design A closes it at the producer, so the miss is induced
    directly rather than by flooding the fixed-capacity (1000) Rust store. RED
    if the ``if typed: raise`` branch is dropped: the miss would debug-skip and
    ``crush()`` would hand back a payload whose ``<<ccr:HASH>>`` marker dangles."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    crusher._rust = _CcrGetMissing(crusher._rust)

    with pytest.raises(CcrMirrorError):
        crusher.crush(json.dumps(_ROW_DROP_ITEMS), query="x")

    # Guard against a vacuous pass: the crush must have produced a typed
    # row-drop ref whose mirror actually reached the (forced-miss) ccr_get.
    assert crusher._rust.ccr_get_calls > 0, (
        "ccr_get was never called — the crush produced no typed row-drop ref, "
        "so the miss branch under test never ran"
    )


def test_scraped_hash_store_miss_stays_debug_skip() -> None:
    """The COR-5 escalation is TYPED-only: a SCRAPED hash missing from the
    Rust store keeps the graceful debug-skip — "marker leaked from
    elsewhere" is a legitimate explanation only when the hash was substring-
    scanned out of rendered text. GREEN before AND after the fix; pins the
    typed-vs-scraped asymmetry so the fix cannot over-reach."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    # Valid 12-hex shape, deliberately absent from the fresh Rust store.
    # Scraped call sites pass no ``typed`` flag — the default must skip.
    crusher._mirror_single_hash_to_python_store(
        "deadbeef1234",
        strategy="smart_crusher",
        query_context="x",
        tool_name=None,
    )  # must NOT raise


# ─── store-concurrency-honesty: the surfaced veto text must not self-contradict ─
#
# When the durable write loses the WHOLE lock-contention retry budget on the
# row-drop (array) path, ``store.store(require_durable=True)`` raises
# ``DurableWriteError`` whose text is already precise and honest: the original
# IS retrievable from this process right now (volatile tier, named hash); it
# just is not durable. The mirror's ``except Exception`` wrapper used to append
# "; dropped rows would be unrecoverable" — producing ONE user-visible string
# claiming BOTH "retrievable now" AND "unrecoverable". Confirmed live by an
# external evaluator (two MCP servers sharing one namespace store).


def test_durable_veto_on_array_path_is_honest_not_unrecoverable(tmp_path: Any) -> None:
    """The final user-visible error keeps the DurableWriteError's honest text
    (hash + retrievable-now) WITHOUT the contradictory "unrecoverable" suffix;
    the existing fail-open semantics are unchanged (original intact, no
    ``<<ccr:`` marker); and the hash in the message really resolves from this
    process at that moment — proving the retained claim true."""
    from furl_ctx.transforms import TransformPipeline

    store = cs.CompressionStore(
        backend=make_fail_open_sqlite_backend(tmp_path / "veto.sqlite3"),
        enable_feedback=False,
        durable_retry_attempts=1,
        durable_retry_base_backoff_seconds=0.001,
        durable_retry_max_backoff_seconds=0.001,
    )
    cs.set_request_compression_store(store)
    try:
        tool_msg = _tool_message(_ROW_DROP_ITEMS)
        # A FRESH default pipeline (identical transforms) instead of the shared
        # singleton: this test's injected failure must not feed the singleton's
        # circuit breaker (3 consecutive failures → 60 s open → every later
        # compression test in the suite silently no-ops and fails).
        result = compress(_build_messages(tool_msg), pipeline=TransformPipeline())
    finally:
        cs.clear_request_compression_store()

    # Existing fail-open assertions kept: the veto still fires and reverts to
    # the ORIGINAL rows — nothing shipped lossy, no dangling marker.
    assert result.error is not None, "durability veto did not surface through compress()"
    assert result.messages[1]["content"] == tool_msg["content"], (
        "row-drop output stood despite the durability veto"
    )
    assert "<<ccr:" not in json.dumps(result.messages)

    # HONESTY: one string, no self-contradiction.
    lowered = result.error.lower()
    assert "retrievable now" in lowered, f"honest inner text missing: {result.error!r}"
    assert "unrecoverable" not in lowered, (
        f"'retrievable now' contradicted by 'unrecoverable' in one message: {result.error!r}"
    )
    # The surfaced hash resolves RIGHT NOW from this process — the claim holds.
    match = re.search(r"for hash ([0-9a-f]{12,24})", result.error)
    assert match, f"no retrieval hash surfaced in: {result.error!r}"
    assert store.retrieve(match.group(1)) is not None, (
        "the surfaced hash did not resolve in-process; the honest text would be false"
    )
