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

import pytest

from headroom.cache.compression_store import CompressionStore
from headroom.config import ReadLifecycleConfig
from headroom.transforms.read_lifecycle import ReadLifecycleManager, ReadState


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
        """With store=None and a stale Read, original content must be recoverable
        from the output alone — either verbatim or no substitution occurred."""
        original_content = "unique_stale_content_" * 30  # ~630 bytes
        messages = _build_messages_with_stale_read(read_content=original_content)
        manager = self._make_manager()

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        # Either the original content is still present verbatim in the output,
        # OR no substitution occurred at all. Either way, no data loss.
        content_present = original_content in all_text
        no_substitution = result.reads_stale == 0
        assert content_present or no_substitution, (
            "With store=None, original Read content must remain recoverable. "
            "Either the content is still in the output or no substitution happened. "
            f"reads_stale={result.reads_stale}, content_present={content_present}"
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
        """With store=None and a superseded Read, original content is recoverable."""
        original_content = "unique_superseded_content_" * 25  # ~650 bytes
        messages = _build_messages_with_superseded_read(read_content=original_content)
        # Enable superseded compression to exercise that code path
        config = ReadLifecycleConfig(enabled=True, min_size_bytes=200, compress_superseded=True)
        manager = ReadLifecycleManager(config=config, compression_store=None)

        result = manager.apply(messages)

        all_text = _get_all_text_from_messages(result.messages)

        content_present = original_content in all_text
        no_substitution = result.reads_superseded == 0
        assert content_present or no_substitution, (
            "With store=None, superseded Read content must remain recoverable. "
            f"reads_superseded={result.reads_superseded}, content_present={content_present}"
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
        for h in result.ccr_hashes:
            # With store=None there's nowhere to back a hash — so none should exist
            assert False, (
                f"With store=None, result.ccr_hashes must be empty but got: {result.ccr_hashes}"
            )
