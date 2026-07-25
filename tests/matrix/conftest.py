"""Per-test CCR store isolation for the MATRIX suite.

Mirrors ``test_retrieve_slice.py``'s ``_isolate_store``: every matrix test runs
against a fresh in-memory CCR store scoped to a throwaway workspace, so offloads
and retrievals never leak across tests and the register stays deterministic
(rule: no network/time/randomness-without-fixed-seed).
"""

from __future__ import annotations

import pytest

import furl_ctx.compress as _compress_mod
from furl_ctx.cache.compression_store import reset_compression_store


@pytest.fixture
def ccr_backend(request) -> str:
    """The CCR store backend the isolation fixture pins for a matrix test.

    Default ``"memory"`` — fast, and the raw-``str`` identity reference. An
    INDIVIDUAL test that must also prove the promise against the durable backend
    production runs (``FURL_CCR_BACKEND=sqlite``, the ``surrogatepass`` BLOB
    round-trip) opts in with an indirect parametrize::

        @pytest.mark.parametrize("ccr_backend", ["memory", "sqlite"], indirect=True)

    Per-TEST rather than per-file on purpose: only a test that actually WRITES
    the CCR store can observe its backend at all, so fanning a whole file would
    duplicate passthrough/fail-open cases whose sqlite leg is unobservable —
    cost with no coverage. See ``test_encoding_shapes.py``, where exactly the
    offloading tests opt in.
    """
    return getattr(request, "param", "memory")


@pytest.fixture(autouse=True)
def _isolate_ccr_store(tmp_path, monkeypatch, ccr_backend):
    """Fresh store AND fresh compression pipeline per test.

    Resetting only the store is NOT enough: ``compress()`` reuses a process-wide
    singleton pipeline (``furl_ctx.compress._pipeline``) whose ``ContentRouter``
    carries a Tier-2 result cache that outlives a store reset. Dropping the
    singleton gives each test fresh-process semantics (empty cache), so a family
    test measures byte-exact recovery on a single clean compress rather than
    tripping the cross-call result-cache/store divergence pinned separately in
    ``test_result_cache_divergence.py``.

    The backend comes from the ``ccr_backend`` fixture (memory by default). Each
    test gets its OWN ``FURL_WORKSPACE_DIR`` (``tmp_path``), so the sqlite leg
    lands in a per-test ``ccr.sqlite3`` file and isolation holds for either
    backend — the reason the memory pin existed (isolation + determinism) is
    preserved, not traded away.
    """
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", ccr_backend)
    _compress_mod._pipeline = None
    reset_compression_store()
    yield
    reset_compression_store()
    _compress_mod._pipeline = None


@pytest.fixture
def salt(request) -> str:
    """A stable, unique-per-test token (the test's node name).

    Threaded into text-family fixtures via ``_matrix.salted`` so every offload is
    a genuine cold compress, immune to the process-global cache carryover the
    Python store reset cannot clear (see ``_isolate_ccr_store`` and
    ``test_result_cache_divergence``). Deterministic: same test → same salt.
    """
    return request.node.name
