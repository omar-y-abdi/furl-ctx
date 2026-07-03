"""Exception surface for Furl — one reserved base class.

Furl's real error contract does not use typed exceptions today:

* ``compress()`` is FAIL-OPEN — content and pipeline failures never raise.
  The original messages come back unchanged with ``result.error`` set to a
  string describing the swallowed failure.
* Bad usage (an unknown keyword argument) raises plain ``TypeError`` at the
  public boundary, matching Python's own unexpected-kwarg behavior.

``FurlError`` is kept as the RESERVED base for future typed errors so
embedders can write ``except FurlError`` today and stay forward-compatible;
no current API raises it. The eight subclasses this module used to define
(``ConfigurationError``, ``ProviderError``, ``StorageError``,
``CompressionError``, ``TokenizationError``, ``CacheError``,
``ValidationError``, ``TransformError``) were raised nowhere in the package
and were removed in the API-1 prune.
"""

from __future__ import annotations

from typing import Any


class FurlError(Exception):
    """Base exception for all Furl errors.

    Reserved: any typed error a future Furl version raises will inherit
    from this class, so a broad ``except FurlError`` written today keeps
    working. No current API raises it — see the module docstring for the
    fail-open contract.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
        return self.message
