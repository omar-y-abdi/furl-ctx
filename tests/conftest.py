"""Shared pytest fixtures for Headroom tests."""

# CRITICAL: Must be set before ANY imports that could trigger sentence_transformers
# The Rust tokenizers use parallelism that deadlocks with pytest-asyncio
import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"

import pytest

# Import httpx for timeout handling (will be available since it's a dependency)
try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


# =============================================================================
# Global test hooks
# =============================================================================


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Wrap test execution to catch httpx.ReadTimeout and skip instead of fail.

    This handles flaky network timeouts that occur when:
    - HuggingFace Hub is slow during model downloads (sentence-transformers)
    - External embedding APIs timeout
    - Network connectivity issues in CI
    """
    outcome = yield

    if HTTPX_AVAILABLE and outcome.excinfo is not None:
        exc_type, exc_value, exc_tb = outcome.excinfo
        if isinstance(exc_value, httpx.ReadTimeout):
            pytest.skip("Skipped due to network timeout (flaky CI)")


@pytest.fixture(autouse=True)
def _reset_headroom_logger_propagation():
    """Keep `headroom.*` log records flowing to pytest's caplog handler.

    `headroom.proxy.helpers._setup_file_logging` sets
    ``logging.getLogger("headroom").propagate = False`` once any test
    triggers a proxy startup with `--log-file`. After that, every
    subsequent test's `caplog` fixture stops capturing `headroom.*`
    log records (caplog attaches to root, propagation is now blocked
    at the headroom-logger boundary). Reset before every test so the
    capture is deterministic regardless of run order.
    """
    import logging as _logging

    _logging.getLogger("headroom").propagate = True
    yield
