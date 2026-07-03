"""CCR-backing seam for the content router (result-cache HIT path).

Owns the two methods that guard the product's core recovery invariant on a
Tier-2 *result-cache* HIT, where ``smart_crush_content`` is short-circuited and
the normal Rust->Python CCR mirror never runs:

* :meth:`CcrMirror.ensure_ccr_backed` — re-mirrors every ``<<ccr:HASH>>``
  pointer in the cached output back into the Python ``compression_store`` and
  verifies each one resolves; returns ``False`` (-> caller recomputes) if any
  sentinel is unbackable.
* :meth:`CcrMirror.extract_ccr_hashes` — parses the distinct hashes out of the
  cached output, delegating the marker grammar to ``SmartCrusher``.

This is a TRUE leaf module: it imports nothing from ``content_router`` (so there
is no import cycle) and never receives the router. Dependencies are injected
explicitly:

* The shared ``logger`` rides the constructor — it is stable for the router's
  lifetime, and passing it (rather than minting one here) keeps the debug
  records emitting under the router's logger name, byte-for-byte unchanged.
* The SmartCrusher getter (``get_smart_crusher``) is supplied to
  :meth:`ensure_ccr_backed` *per call*. The router's thin ``_ensure_ccr_backed``
  delegator resolves ``self._get_smart_crusher`` fresh on every invocation and
  passes it in, so monkeypatching ``router._get_smart_crusher`` (or the
  underlying registry) still bites — a construction-time capture would have
  been stale.

The two heavy imports stay DEFERRED inside their methods exactly as they were on
the router: ``from .smart_crusher import SmartCrusher`` and
``from ..cache.compression_store import get_compression_store``. content_router
now imports this module at top level; hoisting either import here would risk a
load-time cycle (content_router -> router_ccr_mirror -> smart_crusher -> ...).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

# Type alias for the injected SmartCrusher getter (documentation, not
# enforcement). Mirrors ``_GetCompressor`` in router_dispatch.py.
_GetSmartCrusher = Callable[[], Any]


class CcrMirror:
    """Re-mirrors and verifies CCR sentinels on a result-cache HIT.

    Holds no reference to the :class:`ContentRouter`. Constructed once with the
    router's ``logger``; the SmartCrusher getter is supplied to
    :meth:`ensure_ccr_backed` on each call so monkeypatching the router's
    ``_get_smart_crusher`` still takes effect.
    """

    def __init__(self, *, logger: logging.Logger) -> None:
        self._logger = logger

    def ensure_ccr_backed(
        self,
        cached_compressed: str,
        context: str,
        *,
        get_smart_crusher: _GetSmartCrusher,
    ) -> bool:
        """Ensure every ``<<ccr:HASH>>`` pointer in *cached_compressed* resolves
        in the Python ``compression_store`` (the store the MCP ``furl_retrieve``
        tool reads).

        Returns ``True`` iff, after a best-effort re-mirror, EVERY referenced
        hash is backed by a live store entry. Returns ``False`` if any sentinel
        is unbackable — the caller MUST then refuse to serve the stale cached
        output and recompute instead.

        Why this is needed: the Tier-2 result cache (30-min TTL) and BOTH CCR
        stores (Rust + Python, each 1800-s TTL by default) have INDEPENDENT
        lifetimes — and the CCR stores also evict under capacity pressure. A
        result-cache HIT short-circuits ``smart_crush_content``, so the normal
        Rust->Python mirror never runs. Once BOTH CCR stores expire (or evict)
        while the result cache still holds the crushed output, serving it would
        emit a ``<<ccr:HASH>>`` pointing to nothing — a signalled-but-
        unrecoverable drop (silent data loss).

        The re-mirror (``SmartCrusher._mirror_ccr_to_python_store``) refreshes
        the Python store from the Rust store when the Rust entry is still live.
        If the Rust entry is ALSO gone, the re-mirror is a no-op and the hash
        stays unbacked — this method reports that via ``False`` so the caller
        can recompute (a fresh compress re-creates + re-stores the backing).

        Best-effort on errors: a failure to verify is treated as unbacked
        (``False``) so the caller falls back to the safe recompute path.

        Args:
            cached_compressed: The cached compressor output served on the HIT.
            context: User query context, forwarded to the re-mirror.
            get_smart_crusher: Router getter for the SmartCrusher compressor,
                resolved fresh by the caller per invocation.
        """
        logger = self._logger
        hashes = self.extract_ccr_hashes(cached_compressed)
        if not hashes:
            # No sentinels → nothing to back → trivially safe to serve.
            return True

        crusher = get_smart_crusher()
        if crusher is not None:
            try:
                crusher._mirror_ccr_to_python_store(
                    rendered=cached_compressed,
                    strategy="result_cache_hit",
                    query_context=context,
                    tool_name=None,
                )
            except Exception as e:  # pragma: no cover - best effort
                logger.debug("_ensure_ccr_backed: mirror raised (non-fatal): %s", e)

        # Verify against the authoritative Python store: a sentinel is "backed"
        # only if a `furl_retrieve` lookup would resolve it right now.
        try:
            from ..cache.compression_store import get_compression_store

            store = get_compression_store()
        except Exception as e:  # pragma: no cover - defensive
            # Cannot verify → assume unbacked, force the safe recompute path.
            logger.debug("_ensure_ccr_backed: cannot get compression_store (%s)", e)
            return False

        for h in hashes:
            # Backing verification is an ENGINE-INTERNAL read — it must not
            # feed the retrieval-feedback loop as if the model asked for this
            # content back (Engine P2-13).
            if store.retrieve(h, record_feedback_signal=False) is None:
                logger.debug(
                    "_ensure_ccr_backed: hash %s unbackable after re-mirror "
                    "(both CCR stores expired) — recompute required",
                    h,
                )
                return False
        return True

    @staticmethod
    def extract_ccr_hashes(text: str) -> set[str]:
        """Collect every distinct ``<<ccr:HASH...>>`` hash in *text*.

        Reuses SmartCrusher's marker scanner so the parse grammar stays in one
        place (no regex; tolerates the row-drop, opaque-blob, and bare marker
        forms).
        """
        if "<<ccr:" not in text:
            return set()
        from .smart_crusher import SmartCrusher

        hashes: set[str] = set()
        try:
            parsed = json.loads(text)
            SmartCrusher._collect_ccr_hashes(parsed, hashes)
        except (json.JSONDecodeError, ValueError):
            SmartCrusher._collect_ccr_hashes_from_string(text, hashes)
        return hashes
