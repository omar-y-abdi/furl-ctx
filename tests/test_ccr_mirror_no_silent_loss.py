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

Call-stack (verified) that makes the raise fail-safe::

    _mirror_single_hash_to_python_store  (store.store() except -> raise)
      -> _mirror_ccr_markers_in_text / direct
      -> _mirror_ccr_to_python_store
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
from typing import Any

import pytest

from headroom.cache import compression_store as cs
from headroom.compress import compress
from headroom.transforms.smart_crusher import (
    CcrMirrorError,
    SmartCrusher,
    SmartCrusherConfig,
)

# Row-drop fixture: the same 1000 distinct strings the recovery-invariant
# suite uses (``_NON_DICT_CASES["strings"]``). A homogeneous flat array this
# large takes SmartCrusher's lossy row-drop path and emits a ``<<ccr:>>``
# pointer — empirically confirmed before writing this test.
_ROW_DROP_ITEMS = [f"log-line-{i}-payload" for i in range(1000)]


class _FailingStore:
    """Wraps the real store but makes ``store()`` raise, simulating a Python
    compression_store write failure during the mirror. Every other attribute
    delegates to the real singleton so retrieval/search still behave."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def store(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("INJECTED compression_store write failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def store_writes_fail(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Patch ``get_compression_store`` so the singleton's ``.store`` raises.

    The mirror imports ``get_compression_store`` *inside* the function via
    ``from ..cache.compression_store import get_compression_store``; that name
    resolves to ``headroom.cache.compression_store.get_compression_store`` at
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
    monkeypatch.setattr(
        cs, "get_compression_store", lambda: _FailingStore(real_get())
    )

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
        if name == "headroom.cache.compression_store" or name.endswith(
            "cache.compression_store"
        ):
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
