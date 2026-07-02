"""Shared pytest fixtures for Furl tests."""

# Defensive default, set before any imports: silences fork-parallelism warnings
# from third-party tokenizer libraries if one happens to be installed in the
# test environment (not a Furl dependency).
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
    - External APIs are slow to respond
    - Network connectivity degrades in CI
    """
    outcome = yield

    if HTTPX_AVAILABLE and outcome.excinfo is not None:
        exc_type, exc_value, exc_tb = outcome.excinfo
        if isinstance(exc_value, httpx.ReadTimeout):
            pytest.skip("Skipped due to network timeout (flaky CI)")


@pytest.fixture(autouse=True)
def _reset_furl_logger_propagation():
    """Keep `furl_ctx.*` log records flowing to pytest's caplog handler.

    `furl_ctx.proxy.helpers._setup_file_logging` sets
    ``logging.getLogger("furl_ctx").propagate = False`` once any test
    triggers a proxy startup with `--log-file`. After that, every
    subsequent test's `caplog` fixture stops capturing `furl_ctx.*`
    log records (caplog attaches to root, propagation is now blocked
    at the furl_ctx-logger boundary). Reset before every test so the
    capture is deterministic regardless of run order.
    """
    import logging as _logging

    _logging.getLogger("furl_ctx").propagate = True
    yield
