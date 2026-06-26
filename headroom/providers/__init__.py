"""Provider protocols retained for the compression core.

The concrete provider implementations and agent integrations were removed in
the compression-only amputation. This package now exposes only the abstract
protocol the compression library depends on:

- :class:`headroom.providers.base.TokenCounter` — token-counting protocol used
  by :mod:`headroom.tokenizer`.
"""

from __future__ import annotations

# lazy dev: direct re-export, not the `_LAZY_EXPORTS`/`__getattr__` ceremony the
# multi-symbol packages (`transforms`, `cache`, …) use to defer heavy imports —
# this package exposes one zero-dependency `typing.Protocol`, so deferral buys
# nothing. Upgrade path: restore lazy machinery only if a heavy export lands here.
from headroom.providers.base import TokenCounter

__all__ = ["TokenCounter"]
