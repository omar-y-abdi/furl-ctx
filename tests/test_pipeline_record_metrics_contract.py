"""Pin the `record_metrics` / `simulate()` contract after the OTel collapse.

The OpenTelemetry/Langfuse stack was removed; the per-transform span/metric
ceremony in ``TransformPipeline.apply()`` was no-op and is gone. The ONE
load-bearing thing the old ceremony depended on survives: the
``kwargs.pop("record_metrics", True)`` at the top of ``apply()``.

``simulate()`` is ``apply(record_metrics=False, ...)``. The contract these tests
pin:

* ``record_metrics`` is consumed by ``apply()`` and NEVER forwarded to a
  transform's ``should_apply`` / ``apply`` (the pop is selective — other
  kwargs like ``model_limit`` still flow through);
* because the flag never reaches the transforms AND ``apply()`` always works on
  a ``deep_copy`` of the messages, ``simulate()`` and ``apply()`` produce
  byte-identical output and leave the caller's messages untouched.

If a future edit drops the pop (e.g. while removing telemetry), ``record_metrics``
would leak into every transform call and these tests fail.
"""
from __future__ import annotations

from typing import Any

from headroom.config import TransformResult
from headroom.tokenizer import Tokenizer
from headroom.tokenizers import EstimatingTokenCounter
from headroom.transforms.base import Transform
from headroom.transforms.pipeline import TransformPipeline

_MODEL = "gpt-4o"
_MODEL_LIMIT = 128_000


def _tok() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())  # type: ignore[arg-type]


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "hello there, this is a normal message"},
    ]


class _SpyTransform(Transform):
    """Records the kwargs it is handed and rewrites content deterministically.

    The rewrite makes the output differ from the input so an equality check
    between ``apply()`` and ``simulate()`` is meaningful (both must apply the
    same transformation), and proves the message-list copy is in effect.
    """

    name = "spy"

    def __init__(self) -> None:
        self.apply_kwargs: list[dict[str, Any]] = []
        self.should_apply_kwargs: list[dict[str, Any]] = []

    def should_apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> bool:
        self.should_apply_kwargs.append(dict(kwargs))
        return True

    def apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> TransformResult:
        self.apply_kwargs.append(dict(kwargs))
        before = tokenizer.count_messages(messages)
        rewritten = [
            {**m, "content": f"{m['content']} [spy]"} if isinstance(m.get("content"), str) else m
            for m in messages
        ]
        after = tokenizer.count_messages(rewritten)
        return TransformResult(
            messages=rewritten,
            tokens_before=before,
            tokens_after=after,
            transforms_applied=["spy:rewrite"],
        )


def test_record_metrics_never_reaches_transforms_via_apply() -> None:
    """apply() (record_metrics defaults True) must not forward the flag."""
    spy = _SpyTransform()
    pipeline = TransformPipeline(transforms=[spy])

    pipeline.apply(_messages(), _MODEL, model_limit=_MODEL_LIMIT)

    assert spy.apply_kwargs, "spy transform was never applied"
    for kw in spy.apply_kwargs + spy.should_apply_kwargs:
        assert "record_metrics" not in kw
        # The pop is selective — unrelated kwargs still flow through.
        assert kw.get("model_limit") == _MODEL_LIMIT


def test_record_metrics_false_never_reaches_transforms_via_simulate() -> None:
    """simulate() passes record_metrics=False; it must be consumed, not forwarded."""
    spy = _SpyTransform()
    pipeline = TransformPipeline(transforms=[spy])

    pipeline.simulate(_messages(), _MODEL, model_limit=_MODEL_LIMIT)

    assert spy.apply_kwargs, "spy transform was never applied"
    for kw in spy.apply_kwargs + spy.should_apply_kwargs:
        assert "record_metrics" not in kw
        assert kw.get("model_limit") == _MODEL_LIMIT


def test_simulate_matches_apply_output() -> None:
    """simulate() is apply() with the dry-run marker — output must be identical."""
    messages = _messages()

    applied = TransformPipeline(transforms=[_SpyTransform()]).apply(
        messages, _MODEL, model_limit=_MODEL_LIMIT
    )
    simulated = TransformPipeline(transforms=[_SpyTransform()]).simulate(
        messages, _MODEL, model_limit=_MODEL_LIMIT
    )

    assert simulated.messages == applied.messages
    assert simulated.tokens_before == applied.tokens_before
    assert simulated.tokens_after == applied.tokens_after
    assert simulated.transforms_applied == applied.transforms_applied
    # Output is actually transformed (the rewrite ran), so equality is meaningful.
    assert applied.transforms_applied == ["spy:rewrite"]
    assert applied.messages != messages


def test_simulate_does_not_mutate_caller_messages() -> None:
    """The deep copy (not the flag) is what makes simulate() non-destructive."""
    messages = _messages()
    snapshot = [dict(m) for m in messages]

    TransformPipeline(transforms=[_SpyTransform()]).simulate(
        messages, _MODEL, model_limit=_MODEL_LIMIT
    )

    assert messages == snapshot
