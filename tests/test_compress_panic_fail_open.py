"""COR-7: the fail-open boundary must catch a Rust panic.

Defect being pinned
-------------------
A Rust panic crosses the PyO3 FFI as ``pyo3_runtime.PanicException`` — which is a
``BaseException``, NOT an ``Exception``. ``compress()``'s fail-open handler used
``except Exception``, so a panic escaped it entirely: the ONE failure class the
fail-open architecture exists for (an engine bug that panics mid-compress) was the
one class it could not catch. A panic would crash the host's request instead of
reverting to the original messages.

The fix works at both ends (belt-and-braces, one change):

* Rust (``crates/headroom-py/src/lib.rs``): the hot bridge methods wrap their
  compute in ``std::panic::catch_unwind`` and convert a caught panic into a
  ``PyRuntimeError`` — a normal ``Exception`` on the Python side, which the
  existing fail-open path already handles.
* Python (``headroom/compress.py``): the fail-open handler catches
  ``BaseException`` (re-raising ``KeyboardInterrupt``/``SystemExit`` immediately so
  those are never swallowed), so any panic that still surfaces as
  ``PanicException`` — e.g. from a bridge entry point not on the wrapped list — is
  still caught and reverted.

This test exercises the Python belt directly. ``pyo3_runtime.PanicException`` is
not importable cold (the abi3 extension registers it lazily, only on the first
real panic), and the ``headroom-py`` crate disables the cargo test harness
(``test = false``), so a genuine Rust panic cannot be triggered from Python in a
unit test. Instead we stand in a ``BaseException`` subclass: catching a panic by
its ``BaseException`` supertype is a *type* guarantee
(``PanicException`` ⊂ ``BaseException``), so a supertype stand-in faithfully
reproduces the escape — and proves the fix without needing a live panic.

Bite evidence
-------------
RED against the pre-fix ``except Exception``: a ``BaseException`` raised inside
``pipeline.apply`` propagated straight out of ``compress()`` (the intermediate
``except Exception`` layers cannot intercept a ``BaseException``, which is exactly
why a real panic bites). GREEN after the fix: ``compress()`` returns the ORIGINAL
messages, records the failure in ``result.error``, and never propagates.
"""

from __future__ import annotations

import importlib
import json

import pytest

import headroom._core as _core
from headroom.compress import compress


class SimulatedPanicException(BaseException):
    """Stands in for ``pyo3_runtime.PanicException``.

    A ``BaseException`` (NOT an ``Exception``), mirroring how PyO3 surfaces a
    Rust panic across the FFI. Catching this by its ``BaseException`` supertype
    is what the fix must do; a real panic is not needed to prove the type
    relationship holds.
    """


class _PanickingPipeline:
    """A pipeline whose ``apply`` raises a panic-shaped ``BaseException`` —
    the FFI-panic analogue of ``test_compress_failure._FailingPipeline`` (which
    raises a plain ``Exception``)."""

    def apply(self, **kwargs):  # noqa: ANN003, ANN201
        raise SimulatedPanicException("simulated Rust panic across the FFI")


def _messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "hello world " * 100}]


def test_compress_catches_panic_and_returns_original(monkeypatch) -> None:
    """A panic (``BaseException``) surfacing from the pipeline must be caught by
    the fail-open boundary: original messages returned, failure recorded, nothing
    propagated.

    RED pre-fix (``except Exception``): the ``BaseException`` escaped ``compress()``.
    GREEN post-fix (``except BaseException``): fail-open reverts to the original."""
    compress_module = importlib.import_module("headroom.compress")
    monkeypatch.setattr(compress_module, "_get_pipeline", lambda: _PanickingPipeline())

    messages = _messages()

    # Must NOT propagate the BaseException — that is the whole point of the fix.
    result = compress(messages, model="gpt-4o")

    # Fail-open: the host's request survives with the ORIGINAL messages intact.
    assert result.messages == messages
    # The failure is surfaced, not swallowed as a benign no-op: honest token
    # count on the untouched input + the panic text in ``error``.
    from headroom.tokenizers import get_tokenizer

    expected_tokens_before = get_tokenizer("gpt-4o").count_messages(messages)
    assert expected_tokens_before > 0
    assert result.tokens_before == expected_tokens_before
    assert result.tokens_after == 0
    assert result.tokens_saved == 0
    assert result.compression_ratio == 0.0
    assert result.error is not None
    assert "simulated Rust panic" in result.error


def test_fail_open_still_reraises_keyboard_interrupt(monkeypatch) -> None:
    """The ``BaseException`` widening must NOT swallow ``KeyboardInterrupt`` or
    ``SystemExit`` — a Ctrl-C or interpreter shutdown during compression must
    still tear down as the operator intended, never be masked as a fail-open."""
    compress_module = importlib.import_module("headroom.compress")

    class _InterruptingPipeline:
        def apply(self, **kwargs):  # noqa: ANN003, ANN201
            raise KeyboardInterrupt

    monkeypatch.setattr(
        compress_module, "_get_pipeline", lambda: _InterruptingPipeline()
    )

    with pytest.raises(KeyboardInterrupt):
        compress(_messages(), model="gpt-4o")


def test_fail_open_still_reraises_system_exit(monkeypatch) -> None:
    """Companion to the KeyboardInterrupt guard: ``SystemExit`` must propagate."""
    compress_module = importlib.import_module("headroom.compress")

    class _ExitingPipeline:
        def apply(self, **kwargs):  # noqa: ANN003, ANN201
            raise SystemExit(1)

    monkeypatch.setattr(compress_module, "_get_pipeline", lambda: _ExitingPipeline())

    with pytest.raises(SystemExit):
        compress(_messages(), model="gpt-4o")


# ─── Full-stack bite: a panic at the ACTUAL bridge position ─────────────────
#
# The pipeline-level tests above prove the outermost catch. This one proves the
# harder thing COR-7 is really about: a ``BaseException`` raised at the Rust
# BRIDGE (``SmartCrusher.crush``) must survive EVERY intermediate
# ``except Exception`` layer between it and ``compress()`` — smart_crusher.py,
# router_dispatch.py:220, content_router.py:754, pipeline.py:120 — and reach the
# fail-open. If any of those layers were ``except BaseException`` (or if the
# outer catch were ``except Exception``), the panic would be swallowed
# mid-pipeline and the lossy output would stand: silent loss. This is the exact
# stack a real ``pyo3_runtime.PanicException`` traverses.

# A 1000-distinct-string row-drop fixture: a homogeneous flat array this large
# takes SmartCrusher's lossy row-drop path via ``SmartCrusher.crush`` — confirmed
# to route through the patched bridge method exactly once in the full
# ``compress()`` path. The ``cor7panicbridge`` nonce makes this content UNIQUE to
# this test, so the router's process-singleton result-cache (keyed on
# ``hash(content)``, 30-min TTL) can never serve it from a prior test's run and
# skip ``SmartCrusher.crush`` — which would make the bridge patch a no-op and the
# ``calls["n"] > 0`` guard fire under the full suite (it did, before this nonce).
_PANIC_NONCE = "cor7panicbridge"
_ROW_DROP_ITEMS = [f"{_PANIC_NONCE}-row-{i}-payload" for i in range(1000)]


def _row_drop_messages() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": f"find {_PANIC_NONCE}-row-7-payload"},
        {"role": "tool", "tool_call_id": "t1", "content": json.dumps(_ROW_DROP_ITEMS)},
    ]


def test_panic_at_bridge_propagates_through_pipeline_to_fail_open(monkeypatch) -> None:
    """A ``BaseException`` raised inside the ``SmartCrusher.crush`` FFI bridge
    must NOT be caught by any intermediate ``except Exception`` on its way up —
    it must reach ``compress()``'s fail-open, which reverts to the ORIGINAL
    messages and records the error.

    RED against a pre-fix ``except Exception`` in ``compress()``: the
    ``BaseException`` sailed past every intermediate ``except Exception`` (they
    cannot catch a ``BaseException`` — the exact reason a real panic bites) AND
    past the outermost handler, propagating out of ``compress()``. GREEN after:
    ``except BaseException`` catches it, output == input.

    Patches the compiled ``SmartCrusher.crush`` at the CLASS so the pipeline's
    OWN internal crusher raises — driving the real
    SmartCrusher → router → pipeline → compress stack, not a mock."""
    fixture = _row_drop_messages()
    original_tool_content = fixture[1]["content"]

    calls = {"n": 0}

    def panicking_crush(self, content, query="", bias=1.0):  # noqa: ANN001, ANN003, ANN201
        calls["n"] += 1
        raise SimulatedPanicException("simulated Rust panic inside the crush bridge")

    # setattr on the pyo3 heap type; monkeypatch restores the original after.
    monkeypatch.setattr(_core.SmartCrusher, "crush", panicking_crush)

    result = compress(fixture)

    # The patched bridge actually fired — guards against a false GREEN where the
    # fixture stopped routing through SmartCrusher.crush (e.g. a routing change).
    assert calls["n"] > 0, "SmartCrusher.crush bridge was never reached; test target wrong"

    # FAIL-OPEN across the full stack: the lossy output did NOT stand; the tool
    # message is byte-for-byte the original and carries no dangling CCR marker.
    assert result.messages[1]["content"] == original_tool_content, (
        "a bridge panic let lossy output stand — it was swallowed mid-pipeline "
        "or escaped compress() instead of reverting (silent loss / host crash)"
    )
    assert "<<ccr:" not in json.dumps(result.messages), (
        "lossy CCR marker survived a bridge panic"
    )
    # The failure is surfaced, not masked as a benign no-op.
    assert result.error is not None, "fail-open did not fire; the panic was swallowed"
    assert "simulated Rust panic" in result.error
