"""Size-guard: huge blocks offload immediately (bounded + recoverable); normal content unchanged.

The exact mixed/crush path is super-linear (a 33 MB trace took ~68 s). Above a byte
ceiling the router offloads straight to CCR — O(n) and byte-exact recoverable — instead
of paying the crush. Content below the ceiling must route exactly as before.
"""

from __future__ import annotations

import re

from furl_ctx import compress, retrieve
from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.transforms.router_engine import _HUGE_CONTENT_BYTES_DEFAULT, _huge_content_bytes


def test_huge_content_offloads_and_recovers_byte_exact(monkeypatch) -> None:
    monkeypatch.setenv("FURL_MAX_COMPRESS_BYTES", "50000")  # 50 KB ceiling for the test
    reset_compression_store()
    try:
        content = "\n".join(
            f'{{"row": {i}, "payload": "data-{i}-abcdefghij"}}' for i in range(4000)
        )
        assert len(content) >= 50_000

        result = compress([{"role": "tool", "content": content}], model="gpt-4o")

        assert "ccr_offload" in " ".join(result.transforms_applied)
        match = re.search(r"hash=([0-9a-f]{24})", str(result.messages))
        assert match is not None, f"no recovery marker: {result.messages!r}"
        assert retrieve(match.group(1)) == content  # full original recoverable byte-exact
    finally:
        reset_compression_store()


def test_below_ceiling_does_not_early_offload(monkeypatch) -> None:
    # Small content (< the 4 KB post-hoc-offload floor) can offload via NEITHER
    # the pre-existing fallback NOR this guard, so it isolates that the guard does
    # not fire below its ceiling.
    monkeypatch.setenv("FURL_MAX_COMPRESS_BYTES", "10000000")  # 10 MB ceiling
    content = "[" + ",".join(f'{{"id":{i}}}' for i in range(100)) + "]"
    assert len(content) < 4000
    result = compress([{"role": "tool", "content": content}], model="gpt-4o")
    assert "ccr_offload" not in " ".join(result.transforms_applied)  # guard did not fire


def test_ceiling_reads_env_with_safe_fallback(monkeypatch) -> None:
    monkeypatch.delenv("FURL_MAX_COMPRESS_BYTES", raising=False)
    assert _huge_content_bytes() == _HUGE_CONTENT_BYTES_DEFAULT
    monkeypatch.setenv("FURL_MAX_COMPRESS_BYTES", "12345")
    assert _huge_content_bytes() == 12345
    monkeypatch.setenv("FURL_MAX_COMPRESS_BYTES", "not-a-number")
    assert _huge_content_bytes() == _HUGE_CONTENT_BYTES_DEFAULT  # invalid → default, never raises
