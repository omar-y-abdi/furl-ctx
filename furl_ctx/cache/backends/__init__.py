"""Storage backends for CompressionStore.

This module provides pluggable storage backends for CCR (Compress-Cache-Retrieve).
The default is in-memory storage, but alternative backends can be implemented for:
- Persistence (MongoDB, Redis, etc.)
- Distributed caching
- Custom storage solutions

Usage:
    from furl_ctx.cache.backends import InMemoryBackend, CompressionStoreBackend
    from furl_ctx.cache.compression_store import CompressionStore

    # Use default in-memory backend
    store = CompressionStore()

    # Use custom backend
    class MyBackend:
        # Implement CompressionStoreBackend protocol
        ...
    store = CompressionStore(backend=MyBackend())
"""

from .base import CompressionStoreBackend
from .memory import InMemoryBackend

__all__ = [
    "CompressionStoreBackend",
    "InMemoryBackend",
]
