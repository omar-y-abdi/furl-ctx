"""Direct behavioral tests for ``furl_ctx.transforms.router_debug``.

The router-debug helpers are the content router's introspection surface (the
module docstring documents them as re-exported router globals). They had zero
direct tests — covered only incidentally. Here each pure function is pinned to
its documented output shape with fixed literals derived from the input string,
plus the one effectful helper (``_log_router_debug``) is checked at both sides
of its DEBUG-enabled gate.
"""

from __future__ import annotations

import logging

import pytest

from furl_ctx.transforms import router_debug as rd
from furl_ctx.transforms.content_detector import ContentType
from furl_ctx.transforms.router_split import ContentSection

_ROUTER_LOGGER = "furl_ctx.transforms.content_router"


def test_json_shape_object_reports_kind_keys_and_length() -> None:
    """A JSON object reports kind='object', its keys in order, and its size."""
    assert rd._json_shape('{"a": 1, "b": 2}') == {
        "is_json": True,
        "kind": "object",
        "keys": ["a", "b"],
        "length": 2,
    }


def test_json_shape_array_reports_kind_and_length_only() -> None:
    """A JSON array reports kind='array' and length, with no keys field."""
    assert rd._json_shape("[10, 20, 30]") == {"is_json": True, "kind": "array", "length": 3}


@pytest.mark.parametrize(
    ("raw", "kind"),
    [("42", "int"), ('"hi"', "str"), ("true", "bool"), ("null", "NoneType"), ("3.5", "float")],
)
def test_json_shape_scalar_reports_python_type_name(raw: str, kind: str) -> None:
    """A JSON scalar reports kind = the parsed Python value's type name."""
    assert rd._json_shape(raw) == {"is_json": True, "kind": kind}


def test_json_shape_invalid_reports_exception_class_name() -> None:
    """Unparseable content reports is_json=False and the exception class name."""
    assert rd._json_shape("{not valid") == {"is_json": False, "error": "JSONDecodeError"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"x": 1, "y": [1, 2]}, '{"x":1,"y":[1,2]}'),
        ({"u": "café"}, '{"u":"café"}'),  # ensure_ascii=False keeps unicode literal
    ],
)
def test_router_debug_dumps_is_compact_and_unicode_preserving(
    value: dict[str, object], expected: str
) -> None:
    """Debug payloads serialize with no spaces (compact separators) and raw unicode."""
    assert rd._router_debug_dumps(value) == expected


def test_router_debug_dumps_falls_back_to_str_for_nonserializable() -> None:
    """A non-JSON-serializable value is rendered via ``str`` (default=str), not raised."""
    assert rd._router_debug_dumps({"s": {7}}) == '{"s":"{7}"}'


def test_section_debug_captures_full_section_shape() -> None:
    """``_section_debug`` reports the section's type, span, sizes and raw content.

    char/byte counts and the token estimate are derived from the content, and the
    per-section json_shape reflects that the content is not valid JSON.
    """
    section = ContentSection(
        content='{"k": 1}\nsecond',
        content_type=ContentType.PLAIN_TEXT,
        language="py",
        start_line=3,
        end_line=5,
        is_code_fence=True,
    )
    assert rd._section_debug(section, 7) == {
        "index": 7,
        "content_type": "text",
        "language": "py",
        "start_line": 3,
        "end_line": 5,
        "is_code_fence": True,
        "chars": 15,
        "bytes": 15,
        "tokens_estimate": 3,
        "json_shape": {"is_json": False, "error": "JSONDecodeError"},
        "content": '{"k": 1}\nsecond',
    }


@pytest.mark.parametrize(
    ("content", "flag", "expected"),
    [
        ("```python\nx = 1\n```", "has_code_fences", True),
        ("plain words no fence", "has_code_fences", False),
        ('{\n  "a": 1\n}', "has_json_blocks", True),
        ("no leading bracket here", "has_json_blocks", False),
        ("src/app.py:12:hit\nsrc/x.py:3:hit", "has_search_results", True),
        ("not a grep line", "has_search_results", False),
        ("\n".join("Alpha beta gamma" for _ in range(6)), "has_prose", True),
        ("\n".join("Alpha beta gamma" for _ in range(5)), "has_prose", False),
    ],
)
def test_mixed_indicators_flags(content: str, flag: str, expected: bool) -> None:
    """Each mixed-content indicator fires exactly on its own signal.

    ``has_prose`` uses a strict ``> 5`` match threshold: 5 prose matches is False,
    6 is True (the boundary is pinned on both sides).
    """
    assert rd._mixed_indicators(content)[flag] is expected


def test_log_router_debug_emits_one_record_when_debug_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At DEBUG, one record is emitted on the ROUTER's logger with the event payload."""
    with caplog.at_level(logging.DEBUG, logger=_ROUTER_LOGGER):
        rd._log_router_debug("evt", k="v")
    records = [r for r in caplog.records if r.name == _ROUTER_LOGGER]
    assert len(records) == 1
    assert records[0].getMessage() == 'event=evt {"event":"evt","k":"v"}'


def test_log_router_debug_is_silent_when_debug_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Below DEBUG, the helper returns early and emits nothing (the isEnabledFor gate)."""
    with caplog.at_level(logging.INFO, logger=_ROUTER_LOGGER):
        rd._log_router_debug("evt", k="v")
    assert not [r for r in caplog.records if r.name == _ROUTER_LOGGER]
