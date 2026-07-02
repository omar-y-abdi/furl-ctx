"""Canonical filesystem contract for Furl.

This module defines the single source of truth for where Furl reads and
writes files. The canonical read-write root is ``FURL_WORKSPACE_DIR``
(defaults to ``~/.furl``); it holds runtime caches, savings history, and
anything else the hook and CLI write to.

Implementation notes:

* Helpers return ``Path`` (never ``str``). Callers that need a string cast
  at the callsite.
* Helpers are pure -- they never call ``mkdir``.
* No caching. Every call re-reads the environment so that ``monkeypatch``
  in tests works without extra hoops.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical env var names
# ---------------------------------------------------------------------------

FURL_WORKSPACE_DIR_ENV = "FURL_WORKSPACE_DIR"

# ---------------------------------------------------------------------------
# Default sub-path fragments
# ---------------------------------------------------------------------------

_WORKSPACE_DIR_DEFAULT = ".furl"

# Resource file/sub-dir names (kept here so nothing else has to hardcode them)
_SESSION_STATS_FILE = "session_stats.jsonl"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    """Return a trimmed environment value, or ``""`` when unset/blank."""

    return os.environ.get(name, "").strip()


# ---------------------------------------------------------------------------
# Canonical roots
# ---------------------------------------------------------------------------


def workspace_dir() -> Path:
    """Return the workspace (read-write state) root directory.

    Resolution order:

    1. ``$FURL_WORKSPACE_DIR`` (trimmed, tilde-expanded) if set.
    2. ``~/.furl`` otherwise.
    """

    env_value = _env(FURL_WORKSPACE_DIR_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / _WORKSPACE_DIR_DEFAULT


# ---------------------------------------------------------------------------
# Per-resource helpers -- workspace bucket
# ---------------------------------------------------------------------------


def session_stats_path() -> Path:
    """Return the path for the per-session stats JSONL file."""

    return workspace_dir() / _SESSION_STATS_FILE


__all__ = [
    "FURL_WORKSPACE_DIR_ENV",
    "workspace_dir",
    "session_stats_path",
]
