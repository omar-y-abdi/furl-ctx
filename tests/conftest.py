"""Shared pytest configuration for Furl tests.

The suite is fully offline and hermetic: the proxy-era network suites
(and their httpx.ReadTimeout skip-hook) and the proxy file-logging
fixture were removed with the proxy — an exception-to-skip hook can
only mask genuine bugs in an offline suite (TEST-13/TEST-34).
"""

# Defensive default, set before any imports: silences fork-parallelism warnings
# from third-party tokenizer libraries if one happens to be installed in the
# test environment (not a Furl dependency).
import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"
