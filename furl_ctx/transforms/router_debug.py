"""Debug-introspection helpers for the content router (pure functions).

Extracted from ``content_router.py`` (§4.1 S1) as a pure move. Everything here
is a side-effect-free function of its arguments except
:func:`_log_router_debug`, whose only effect is emitting one DEBUG record.

``content_router`` re-imports every name below as a module global, so:

* existing ``from ...content_router import _json_shape``-style imports keep
  resolving,
* the helpers stay REBINDABLE as ``content_router`` module globals
  (``monkeypatch.setattr(content_router_module, "_json_shape", ...)`` keeps
  biting for callers that resolve them through ``content_router``), and
* in-router callers keep referencing them as bare module globals.

This is a TRUE leaf module: it imports only ``router_split`` (a sibling leaf)
and the stdlib — never ``content_router`` — so there is no import cycle.

Logger note: records must keep emitting under the ROUTER's logger name
(``furl_ctx.transforms.content_router``) byte-for-byte — tests and deployments
filter on it, and ``StrategyDispatcher`` already receives that same logger for
the same reason. ``logging.getLogger`` returns the identical shared logger
object, so binding it here by name changes nothing observable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .router_split import (
    _CODE_FENCE_PATTERN,
    _JSON_BLOCK_START,
    _PROSE_PATTERN,
    _SEARCH_RESULT_PATTERN,
    ContentSection,
)

# The router's logger, NOT this module's: debug records keep their historical
# ``furl_ctx.transforms.content_router`` logger name (see module docstring).
logger = logging.getLogger("furl_ctx.transforms.content_router")


def _router_debug_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _log_router_debug(event: str, **payload: Any) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    payload = {"event": event, **payload}
    logger.debug("event=%s %s", event, _router_debug_dumps(payload))


def _json_shape(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except Exception as exc:
        return {"is_json": False, "error": type(exc).__name__}
    if isinstance(parsed, dict):
        return {
            "is_json": True,
            "kind": "object",
            "keys": list(parsed.keys()),
            "length": len(parsed),
        }
    if isinstance(parsed, list):
        return {"is_json": True, "kind": "array", "length": len(parsed)}
    return {"is_json": True, "kind": type(parsed).__name__}


def _mixed_indicators(content: str) -> dict[str, bool]:
    return {
        "has_code_fences": bool(_CODE_FENCE_PATTERN.search(content)),
        "has_json_blocks": bool(_JSON_BLOCK_START.search(content)),
        "has_prose": len(_PROSE_PATTERN.findall(content)) > 5,
        "has_search_results": bool(_SEARCH_RESULT_PATTERN.search(content)),
    }


def _section_debug(section: ContentSection, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "content_type": section.content_type.value,
        "language": getattr(section, "language", None),
        "start_line": getattr(section, "start_line", None),
        "end_line": getattr(section, "end_line", None),
        "is_code_fence": getattr(section, "is_code_fence", False),
        "chars": len(section.content),
        "bytes": len(section.content.encode("utf-8", errors="replace")),
        "tokens_estimate": len(section.content.split()),
        "json_shape": _json_shape(section.content),
        "content": section.content,
    }
