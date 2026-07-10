"""Public format contract for ``furl_ctx.exceptions.FurlError``.

``FurlError`` is the reserved base exception (see the module docstring: no current
API raises it, but embedders write ``except FurlError`` today). Its only real
behavior is ``__str__`` — it renders the bare message, and appends any structured
``details`` as ``msg (k=v, ...)``. These tests pin that rendering, the attribute
surface, and the ``details=None`` default, all through the public constructor.

Every expected string here is derived from the INPUT (message + details), never
read back from the object under test.
"""

from __future__ import annotations

import pytest

from furl_ctx.exceptions import FurlError


def test_str_is_bare_message_when_no_details() -> None:
    """No details -> ``str`` is exactly the message, with no parenthetical."""
    assert str(FurlError("disk full")) == "disk full"


def test_empty_details_dict_renders_as_bare_message() -> None:
    """Boundary: an EMPTY details dict is falsy, so no ``(...)`` is appended.

    This is the ``if self.details:`` edge — 0 details renders identically to the
    no-details case, distinct from the >=1-detail branch tested below.
    """
    assert str(FurlError("disk full", {})) == "disk full"


@pytest.mark.parametrize(
    ("message", "details", "expected"),
    [
        ("bad request", {"code": 42}, "bad request (code=42)"),
        ("bad request", {"code": 42, "field": "email"}, "bad request (code=42, field=email)"),
        ("null seen", {"value": None, "ok": True}, "null seen (value=None, ok=True)"),
        ("nested", {"path": "/a/b", "n": 3}, "nested (path=/a/b, n=3)"),
    ],
)
def test_str_appends_details_in_insertion_order(
    message: str, details: dict[str, object], expected: str
) -> None:
    """One-or-more details render as ``msg (k=v, ...)`` in dict-insertion order.

    Multi-key details join with ``", "``; each value is ``str()``-formatted
    (so ``None`` -> ``None``, ``True`` -> ``True``). Deterministic because dicts
    preserve insertion order.
    """
    assert str(FurlError(message, details)) == expected


def test_message_and_details_attributes_are_preserved_from_construction() -> None:
    """The message and details passed in are exposed verbatim as attributes."""
    err = FurlError("boom", {"a": 1})
    assert err.message == "boom"
    assert err.details == {"a": 1}
    # args carries the message (Exception contract), not the details.
    assert err.args == ("boom",)


def test_details_defaults_to_empty_dict_when_omitted_or_none() -> None:
    """Omitting details (or passing ``None``) yields an empty dict, never ``None``.

    Guards the ``details or {}`` normalization so callers can always index
    ``err.details`` without a ``None`` check.
    """
    assert FurlError("x").details == {}
    assert FurlError("x", None).details == {}


def test_is_exception_and_catchable_as_furlerror_and_exception() -> None:
    """A raised ``FurlError`` is catchable both as ``FurlError`` and ``Exception``.

    This is the forward-compatibility promise: ``except FurlError`` (and the
    broader ``except Exception``) both catch it.
    """
    with pytest.raises(FurlError) as exc_info:
        raise FurlError("kaboom", {"where": "here"})
    assert str(exc_info.value) == "kaboom (where=here)"
    assert isinstance(exc_info.value, Exception)
