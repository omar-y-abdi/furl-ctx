"""Audit #10 — the SmartCrusher mirror's `except ValueError` must veto, not swallow.

The CCR mirror's generic ``except Exception`` correctly raised ``CcrMirrorError``
(→ ``compress()`` reverts to the uncompressed original, so a lossy drop never
stands unbacked). But the sibling ``except ValueError`` only logged a warning and
returned normally — while the Rust row-drop was ALREADY committed and the
``<<ccr:HASH>>`` marker shipped. ``store.store()`` raises ``ValueError`` on a bad
``explicit_hash`` / non-positive ``ttl``; if it ever fired the marker pointed at
nothing = silent loss (an asymmetry sitting right next to the invariant it broke).

Fix: the ``ValueError`` branch raises ``CcrMirrorError`` too. This pins it in both
forms: the mirror UNIT raises (not a silent return), and the full ``compress()``
path reverts to the ORIGINAL messages (no marker, failure surfaced).

RED against the pre-fix log-and-return: the mirror swallowed the ValueError,
compression proceeded, and the output carried the ``<<ccr:>>`` marker with rows
dropped. GREEN after: the raise reaches ``compress()``'s fail-open boundary.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from furl_ctx.cache import compression_store as cs
from furl_ctx.compress import compress
from furl_ctx.transforms.smart_crusher import CcrMirrorError, SmartCrusher, SmartCrusherConfig

# 1000 distinct homogeneous strings take the lossy row-drop path and emit a
# <<ccr:>> pointer the mirror must back. A DISTINCT prefix (not the
# ``log-line-{i}`` the no-silent-loss suite uses) keeps this content unique to
# this file: the process-wide result cache is content-keyed and the global store
# is not reset between the main suite's tests, so sharing content would let an
# earlier successful compress serve a cached, still-backed crush on a cache hit
# (bypassing this file's injected-failure store entirely).
_ROW_DROP_ITEMS = [f"veto10-audit-line-{i}-payload" for i in range(1000)]


class _ValueErrorStore:
    """A store whose ``store()`` always raises ``ValueError`` — the exact
    exception ``compression_store.store()`` raises on a malformed
    ``explicit_hash`` / non-positive ``ttl``. Every other attribute delegates to
    a real store so the mirror's other reads still behave."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.store_calls = 0

    def store(self, *args: Any, **kwargs: Any) -> str:
        self.store_calls += 1
        raise ValueError("INJECTED explicit_hash/ttl rejection")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _messages(items: list[str]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "find log-line-7-payload"},
        {"role": "tool", "tool_call_id": "t1", "content": json.dumps(items)},
    ]


def test_mirror_valueerror_raises_ccr_mirror_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # UNIT: seed the Rust store (unpatched) so ccr_get returns the canonical,
    # then make the Python store's store() raise ValueError and re-mirror.
    crusher = SmartCrusher(config=SmartCrusherConfig())
    crushed = crusher.crush_array_json(json.dumps(_ROW_DROP_ITEMS), query="x")
    ccr_hash = crushed.get("ccr_hash")
    assert ccr_hash, "fixture did not produce a row-drop hash"

    real_get = cs.get_compression_store
    monkeypatch.setattr(cs, "get_compression_store", lambda: _ValueErrorStore(real_get()))

    with pytest.raises(CcrMirrorError):
        crusher._mirror_single_hash_to_python_store(
            ccr_hash,
            strategy="smart_crusher_row_drop",
            query_context="x",
            tool_name=None,
        )


def test_compress_reverts_to_original_on_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    # BEHAVIOR: the full compress() path must revert to the ORIGINAL messages
    # when the mirror's store write is rejected with ValueError.
    real_get = cs.get_compression_store
    calls = {"n": 0}

    def fake_get() -> Any:
        calls["n"] += 1
        return _ValueErrorStore(real_get())

    monkeypatch.setattr(cs, "get_compression_store", fake_get)

    tool_content = json.dumps(_ROW_DROP_ITEMS)
    result = compress(_messages(_ROW_DROP_ITEMS))

    assert calls["n"] > 0, "store patch never fired; test target wrong"
    assert result.messages[1]["content"] == tool_content, (
        "row-drop output stood despite the ValueError persist failure — silent loss"
    )
    assert "<<ccr:" not in json.dumps(result.messages), "lossy marker survived a ValueError"
    assert result.error is not None, "fail-open did not fire; ValueError was swallowed"
