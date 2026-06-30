"""Canonical filesystem contract for Headroom.

This module defines the single source of truth for where Headroom reads and
writes files. The canonical read-write root is ``HEADROOM_WORKSPACE_DIR``
(defaults to ``~/.headroom``); it holds runtime caches, telemetry outputs,
savings history, and anything else the hook and CLI write to.

Precedence for every per-resource helper is::

    explicit argument > per-resource env var > derived from canonical root >
    default

Adding the canonical root env var is strictly additive: every existing
per-resource override (``HEADROOM_TOIN_PATH``, ...) continues to take
precedence with identical semantics.

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

HEADROOM_WORKSPACE_DIR_ENV = "HEADROOM_WORKSPACE_DIR"

# ---------------------------------------------------------------------------
# Legacy per-resource env vars (kept for backward compatibility)
# ---------------------------------------------------------------------------

HEADROOM_TOIN_PATH_ENV = "HEADROOM_TOIN_PATH"

# ---------------------------------------------------------------------------
# Default sub-path fragments
# ---------------------------------------------------------------------------

_WORKSPACE_DIR_DEFAULT = ".headroom"

# Resource file/sub-dir names (kept here so nothing else has to hardcode them)
_TOIN_FILE = "toin.json"
_SESSION_STATS_FILE = "session_stats.jsonl"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    """Return a trimmed environment value, or ``""`` when unset/blank."""

    return os.environ.get(name, "").strip()


def _resolve(explicit: str | os.PathLike[str] | None, env_var: str, derived: Path) -> Path:
    """Apply the standard precedence: explicit > env > derived.

    ``explicit`` and the env-var value are both passed through ``expanduser()``
    so that callers can pass ``"~/foo/bar"`` and have it resolve naturally.
    """

    if explicit is not None and str(explicit) != "":
        return Path(explicit).expanduser()
    env_value = _env(env_var)
    if env_value:
        return Path(env_value).expanduser()
    return derived


# ---------------------------------------------------------------------------
# Canonical roots
# ---------------------------------------------------------------------------


def workspace_dir() -> Path:
    """Return the workspace (read-write state) root directory.

    Resolution order:

    1. ``$HEADROOM_WORKSPACE_DIR`` (trimmed, tilde-expanded) if set.
    2. ``~/.headroom`` otherwise.
    """

    env_value = _env(HEADROOM_WORKSPACE_DIR_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / _WORKSPACE_DIR_DEFAULT


# ---------------------------------------------------------------------------
# Per-resource helpers -- workspace bucket
# ---------------------------------------------------------------------------


def toin_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the path for the TOIN telemetry JSON file.

    TOIN is classified as workspace state because it is actively written by
    the running engine (it's a compression feedback loop). The default stays
    ``~/.headroom/toin.json`` to preserve byte-for-byte backward compat.
    """

    return _resolve(
        explicit,
        HEADROOM_TOIN_PATH_ENV,
        workspace_dir() / _TOIN_FILE,
    )


def session_stats_path() -> Path:
    """Return the path for the per-session stats JSONL file."""

    return workspace_dir() / _SESSION_STATS_FILE


__all__ = [
    "HEADROOM_WORKSPACE_DIR_ENV",
    "HEADROOM_TOIN_PATH_ENV",
    "workspace_dir",
    "toin_path",
    "session_stats_path",
]
