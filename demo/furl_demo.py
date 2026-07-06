"""Furl demo — the story the README gif tells, as runnable code.

Builds a realistic-but-noisy CI log with a single buried FATAL line, compresses
it with Furl, and shows the token drop plus proof the needle survives. Run it
directly (`python demo/furl_demo.py`) or let `demo/furl-demo.tape` record it
into the README gif via `vhs`.

Deterministic: no wall-clock, no randomness — the numbers are reproducible so
the gif caption can quote them.
"""

from __future__ import annotations

from furl_ctx import compress
from furl_ctx.tokenizers import get_tokenizer

_MODEL = "claude-sonnet-4-5-20250929"
_NEEDLE = "FATAL: migration 0042 failed — column `user.tier` already exists"


def _build_log(lines: int = 220) -> str:
    """A CI log: mostly repetitive INFO/DEBUG noise, one FATAL in the middle."""
    body: list[str] = []
    for i in range(lines):
        if i == lines // 2:
            body.append(f"[worker-3] {_NEEDLE}")
            continue
        kind = "INFO" if i % 3 else "DEBUG"
        body.append(
            f"[worker-{i % 4}] {kind} step {i:03d} ok "
            f"latency={i % 50}ms rows={i * 7 % 1000} cache=hit"
        )
    return "\n".join(body)


def main() -> None:
    log = _build_log()
    messages = [{"role": "tool", "content": log, "tool_call_id": "ci_run"}]

    tok = get_tokenizer(_MODEL)
    before = tok.count_messages(messages)

    result = compress(messages, model=_MODEL)
    after = tok.count_messages(result.messages)

    shipped = result.messages[0]["content"]
    needle_visible = _NEEDLE in shipped
    reduction = (before - after) / before if before else 0.0

    print("  Furl — compress what your agent reads\n")
    print(f"  CI log in     : {before:>6,} tokens")
    print(f"  Furl out      : {after:>6,} tokens   ({reduction:.0%} fewer)")
    print(
        f"  FATAL needle  : {'still visible' if needle_visible else 'recoverable via furl_retrieve'}"
    )
    print("\n  Same answer. Fraction of the tokens. Byte-exact recovery on demand.")

    # The needle is never lost: either shipped verbatim or in the CCR store.
    assert needle_visible or "<<ccr:" in shipped, "needle must survive compression"


if __name__ == "__main__":
    main()
