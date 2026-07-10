"""Tests for read_lifecycle.py store=None phantom-hash fix.

When no CCR store is configured (store=None), _replace_content must NOT emit
a 'Retrieve original: hash=<ccr_hash>' pointer to an unbacked hash. The
original Read content must remain recoverable from the output alone — either
still present verbatim, or no lossy substitution occurred.

When a real store IS configured, the backed-marker behavior must be unchanged:
a marker is produced, and store.retrieve(hash) returns the original.

Contract: #1 — CCR recovery invariant, no silent loss.
"""

from __future__ import annotations

import re
from typing import Any

from furl_ctx.cache.compression_store import CompressionStore
from furl_ctx.config import ReadLifecycleConfig
from furl_ctx.transforms.read_lifecycle import ReadLifecycleManager
from tests._fixtures import make_fail_open_sqlite_backend

# Detect the phantom pattern: "Retrieve original: hash=<something>"
_PHANTOM_HASH_RE = re.compile(r"Retrieve original:\s*hash=")


def _build_messages_with_stale_read(
    file_path: str = "src/main.py",
    read_content: str | None = None,
) -> list[dict[str, Any]]:
    """Construct a minimal message stream with a stale Read output.

    The stream has:
      1. assistant tool_use (Read)
      2. user tool_result with the read content
      3. assistant tool_use (Edit — makes the read stale)
      4. user tool_result for the Edit
    """
    if read_content is None:
        # Large enough to exceed min_size_bytes (default 200)
        read_content = "x" * 500

    tool_call_id = "call_read_1"
    edit_tool_call_id = "call_edit_1"

    messages: list[dict[str, Any]] = [
        # Message 0: assistant calls Read
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_call_id,
                    "name": "Read",
                    "input": {"file_path": file_path},
                }
            ],
        },
        # Message 1: user returns Read result
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": read_content,
                }
            ],
        },
        # Message 2: assistant calls Edit (makes the read stale)
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": edit_tool_call_id,
                    "name": "Edit",
                    "input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
                }
            ],
        },
        # Message 3: user returns Edit result
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": edit_tool_call_id,
                    "content": "Edit applied successfully.",
                }
            ],
        },
    ]
    return messages


def _build_messages_with_superseded_read(
    file_path: str = "src/main.py",
    read_content: str | None = None,
) -> list[dict[str, Any]]:
    """Construct a minimal message stream with a superseded Read output.

    The stream has:
      1. assistant tool_use (Read — first, becomes superseded)
      2. user tool_result with the read content
      3. assistant tool_use (Read — second, makes first superseded)
      4. user tool_result for the second Read
    """
    if read_content is None:
        read_content = "y" * 500

    tool_call_id_1 = "call_read_1"
    tool_call_id_2 = "call_read_2"

    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_call_id_1,
                    "name": "Read",
                    "input": {"file_path": file_path},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id_1,
                    "content": read_content,
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_call_id_2,
                    "name": "Read",
                    "input": {"file_path": file_path},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id_2,
                    "content": "updated content " * 30,
                }
            ],
        },
    ]
    return messages


def _get_all_text_from_messages(messages: list[dict[str, Any]]) -> str:
    """Collect all string content from all messages for inspection."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_content = block.get("content", "")
                    if isinstance(block_content, str):
                        parts.append(block_content)
    return "\n".join(parts)


class TestStoreNoneNoPhantomHash:
    """When store=None, no unbacked 'Retrieve original: hash=...' must be emitted."""

    def _make_manager(self) -> ReadLifecycleManager:
        config = ReadLifecycleConfig(
            enabled=True,
            min_size_bytes=200,
        )
        return ReadLifecycleManager(config=config, compression_store=None)

    def test_stale_read_no_phantom_hash(self) -> None:
        """With store=None, a stale Read must not emit a phantom hash pointer."""
        original_content = "z" * 600  # well above min_size_bytes=200
        messages = _build_messages_with_stale_read(read_content=original_content)
        manager = self._make_manager()

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        # PRIMARY CONTRACT: No phantom hash pointer emitted
        assert not _PHANTOM_HASH_RE.search(all_text), (
            "store=None path must NOT emit 'Retrieve original: hash=...' — "
            "the hash is not backed anywhere and makes the original unrecoverable. "
            f"Output text was: {all_text!r}"
        )

    def test_stale_read_content_recoverable_from_output(self) -> None:
        """With store=None a detected-stale Read stays VERBATIM.

        TEST-11: the old OR-shaped assert (`content_present or reads_stale
        == 0`) also passed when staleness detection never fired at all. Pin
        both halves: the fixture's read IS classified stale (the feature
        fired), and the store-less substitution path declined — content
        ships verbatim, zero data loss.
        """
        original_content = "unique_stale_content_" * 30  # ~630 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        manager = self._make_manager()

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        assert result.reads_stale == 1, (
            "precondition: the fixture's read must be CLASSIFIED stale — "
            "otherwise this test proves nothing about the store=None path"
        )
        assert original_content in all_text, (
            "store=None must skip substitution and ship the content verbatim"
        )

    def test_superseded_read_no_phantom_hash(self) -> None:
        """With store=None, a superseded Read must not emit a phantom hash pointer."""
        original_content = "w" * 600
        messages = _build_messages_with_superseded_read(read_content=original_content)
        # Enable superseded compression to exercise that code path
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200, compress_superseded=True)
        manager = ReadLifecycleManager(config=config, compression_store=None)

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        assert not _PHANTOM_HASH_RE.search(all_text), (
            "store=None path must NOT emit 'Retrieve original: hash=...' for "
            f"superseded reads. Output was: {all_text!r}"
        )

    def test_superseded_read_content_recoverable_from_output(self) -> None:
        """With store=None a detected-superseded Read stays VERBATIM.

        TEST-11: same OR-shape hole as the stale twin — pin detection AND
        the verbatim skip separately.
        """
        original_content = "unique_superseded_content_" * 25  # ~650 bytes
        messages = _build_messages_with_superseded_read(read_content=original_content)
        # Enable superseded compression to exercise that code path
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200, compress_superseded=True)
        manager = ReadLifecycleManager(config=config, compression_store=None)

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        assert result.reads_superseded == 1, (
            "precondition: the fixture's read must be CLASSIFIED superseded"
        )
        assert original_content in all_text, (
            "store=None must skip substitution and ship the content verbatim"
        )

    def test_small_read_below_min_size_unchanged(self) -> None:
        """Reads below min_size_bytes must pass through unchanged regardless of store."""
        small_content = "tiny"  # well below 200 bytes
        messages = _build_messages_with_stale_read(read_content=small_content)
        manager = self._make_manager()

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        # Small content must be unchanged
        assert small_content in all_text, "Content below min_size_bytes must not be replaced"
        assert not _PHANTOM_HASH_RE.search(all_text)


class TestStoreConfiguredBackedMarkerUnchanged:
    """When a real store is configured, the backed-marker behavior is unchanged."""

    def _make_manager_with_store(self) -> tuple[ReadLifecycleManager, CompressionStore]:
        store = CompressionStore()
        config = ReadLifecycleConfig(
            enabled=True,
            min_size_bytes=200,
        )
        manager = ReadLifecycleManager(config=config, compression_store=store)
        return manager, store

    def test_stale_read_with_store_emits_backed_marker(self) -> None:
        """With a real store, stale Read emits 'Retrieve original: hash=...'
        and the original is recoverable via store.retrieve(hash)."""
        original_content = "backed_stale_content_" * 30  # ~630 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        manager, store = self._make_manager_with_store()

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        # A backed marker should be present
        match = _PHANTOM_HASH_RE.search(all_text)
        assert match, (
            "With a real store, stale Read must emit 'Retrieve original: hash=...' pointer. "
            f"Output: {all_text!r}"
        )

        # Extract the hash from the marker
        # The marker looks like: "Retrieve original: hash=<HASH>"
        hash_match = re.search(r"Retrieve original:\s*hash=([a-f0-9]+)", all_text)
        assert hash_match, f"Could not parse hash from marker in: {all_text!r}"
        ccr_hash = hash_match.group(1)

        # The original must be retrievable from the store
        entry = store.retrieve(ccr_hash)
        assert entry is not None, (
            f"store.retrieve({ccr_hash!r}) returned None — the hash is not backed. "
            "This indicates a phantom hash."
        )
        retrieved_content = entry.original_content
        assert retrieved_content == original_content, (
            f"Retrieved content does not match original. "
            f"Expected length {len(original_content)}, got {len(retrieved_content)}"
        )

    def test_superseded_read_with_store_emits_backed_marker(self) -> None:
        """With a real store, superseded Read emits 'Retrieve original: hash=...'
        and the original is recoverable via store.retrieve(hash)."""
        original_content = "backed_superseded_content_" * 25  # ~650 bytes
        messages = _build_messages_with_superseded_read(read_content=original_content)
        # Enable superseded compression to exercise that code path
        store = CompressionStore()
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200, compress_superseded=True)
        manager = ReadLifecycleManager(config=config, compression_store=store)

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        match = _PHANTOM_HASH_RE.search(all_text)
        assert match, (
            "With a real store, superseded Read must emit 'Retrieve original: hash=...' pointer. "
            f"Output: {all_text!r}"
        )

        hash_match = re.search(r"Retrieve original:\s*hash=([a-f0-9]+)", all_text)
        assert hash_match, f"Could not parse hash from marker in: {all_text!r}"
        ccr_hash = hash_match.group(1)

        entry = store.retrieve(ccr_hash)
        assert entry is not None, (
            f"store.retrieve({ccr_hash!r}) returned None — backed pointer is broken."
        )
        assert entry.original_content == original_content

    def test_ccr_hashes_populated_in_result_with_store(self) -> None:
        """With a real store, result.ccr_hashes should contain the stored hash."""
        original_content = "hashed_content_" * 40  # ~640 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        manager, store = self._make_manager_with_store()

        result = manager.apply(messages)

        assert len(result.ccr_hashes) > 0, (
            "With a real store, result.ccr_hashes must be populated after replacing a stale read. "
            f"Got: {result.ccr_hashes}"
        )

    def test_ccr_hashes_not_populated_without_store(self) -> None:
        """With store=None, result.ccr_hashes should remain empty (no phantom hashes)."""
        original_content = "phantom_check_" * 45  # ~630 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200)
        manager = ReadLifecycleManager(config=config, compression_store=None)

        result = manager.apply(messages)

        # If no substitution occurred (correct behavior), ccr_hashes is empty
        # If substitution occurred with phantom hash, ccr_hashes would be non-empty
        # Either way, any hashes in ccr_hashes should be backed:
        if result.ccr_hashes:
            # With store=None there's nowhere to back a hash — so none should exist
            raise AssertionError(
                f"With store=None, result.ccr_hashes must be empty but got: {result.ccr_hashes}"
            )


class TestDurableWriteVetoServesVerbatim:
    """Review F1 (audit #3, the fifth marker seam): when the store's DURABLE
    backend falls open to volatile in-process memory (degraded, or the write
    lost the sqlite lock race), the substitution must be DECLINED exactly like
    the store=None guard — content ships verbatim, no marker, no hash.

    Before the fix the ``[Read content stale/superseded ... Retrieve original:
    hash=H]`` marker replaced the content while the original's only copy died
    with the process — a second process's ``furl_retrieve`` missed, and the
    replaced bytes were unrecoverable (the exact silent loss audit #3 forbids;
    every other marker seam already vetoes via ``require_durable=True``).
    """

    def _make_manager(self, tmp_path) -> ReadLifecycleManager:
        store = CompressionStore(backend=make_fail_open_sqlite_backend(tmp_path / "veto.sqlite3"))
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200)
        return ReadLifecycleManager(config=config, compression_store=store)

    def test_stale_read_served_verbatim_no_marker(self, tmp_path) -> None:
        original_content = "durable_veto_stale_" * 35  # ~665 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        manager = self._make_manager(tmp_path)

        result = manager.apply(messages)
        all_text = _get_all_text_from_messages(result.messages)

        assert result.reads_stale == 1, (
            "precondition: the fixture's read must be CLASSIFIED stale — "
            "otherwise this proves nothing about the veto path"
        )
        assert original_content in all_text, (
            "a failed durable write must decline the substitution and serve verbatim"
        )
        assert not _PHANTOM_HASH_RE.search(all_text), (
            "no 'Retrieve original: hash=' marker may ship when the original's "
            "only backing is volatile process-local memory (review F1)"
        )
        assert not result.ccr_hashes

    def test_superseded_read_served_verbatim_no_marker(self, tmp_path) -> None:
        original_content = "durable_veto_superseded_" * 28  # ~670 bytes
        messages = _build_messages_with_superseded_read(read_content=original_content)
        store = CompressionStore(backend=make_fail_open_sqlite_backend(tmp_path / "veto2.sqlite3"))
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200, compress_superseded=True)
        manager = ReadLifecycleManager(config=config, compression_store=store)

        result = manager.apply(messages)
        all_text = _get_all_text_from_messages(result.messages)

        assert result.reads_superseded == 1, (
            "precondition: the fixture's read must be CLASSIFIED superseded"
        )
        assert original_content in all_text
        assert not _PHANTOM_HASH_RE.search(all_text)
        assert not result.ccr_hashes

    def test_healthy_durable_store_still_replaces(self, tmp_path) -> None:
        # Control: with a HEALTHY sqlite-backed store the SAME fixture still
        # substitutes and the marker's hash resolves to the original — proving
        # the veto tests assert a change on failure, not that replacement is
        # globally off for durable backends.
        from furl_ctx.cache.backends.sqlite import SqliteBackend

        store = CompressionStore(backend=SqliteBackend(db_path=tmp_path / "ok.sqlite3"))
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200)
        manager = ReadLifecycleManager(config=config, compression_store=store)

        original_content = "durable_ok_stale_" * 40  # ~680 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        result = manager.apply(messages)
        all_text = _get_all_text_from_messages(result.messages)

        hash_match = re.search(r"Retrieve original:\s*hash=([a-f0-9]+)", all_text)
        assert hash_match, "healthy durable store must still substitute"
        entry = store.retrieve(hash_match.group(1))
        assert entry is not None and entry.original_content == original_content


class TestMinSizeBytesBoundary:
    """At/below/above triple for the min_size_bytes floor (TEST-12).

    The gate is ``content_bytes < min_size_bytes`` (read_lifecycle.py) →
    the boundary value itself IS substituted. Only a far-below case
    ("tiny") existed before; an off-by-one (`<=`) was invisible.
    """

    def _apply(self, size: int) -> tuple[bool, object]:
        content = "y" * size
        store = CompressionStore()
        manager = ReadLifecycleManager(
            config=ReadLifecycleConfig(enabled=True, min_size_bytes=200),
            compression_store=store,
        )
        result = manager.apply(_build_messages_with_stale_read(read_content=content))
        all_text = _get_all_text_from_messages(result.messages)
        substituted = content not in all_text
        return substituted, result

    def test_below_floor_stays_verbatim(self) -> None:
        substituted, result = self._apply(199)
        assert not substituted, "199 bytes < 200 floor: content must ship verbatim"
        assert result.reads_stale == 1, "the read is still CLASSIFIED stale"

    def test_at_floor_is_substituted(self) -> None:
        substituted, result = self._apply(200)
        assert substituted, "exactly 200 bytes must substitute (gate is `<`)"
        assert result.ccr_hashes, "the substitution must be CCR-backed"

    def test_above_floor_is_substituted(self) -> None:
        substituted, result = self._apply(201)
        assert substituted
        assert result.ccr_hashes
