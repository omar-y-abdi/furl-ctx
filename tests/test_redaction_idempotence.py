"""Redaction idempotence — CHARACTERISED, not fixed (#28).

Production applies ``build_store_redactor()`` TWICE to the same bytes on three
paths, all measured:

* ``furl_ctx/ccr/mcp_server.py::_compress_content`` — ``content = _redactor(content)``
  and then ``compress(content)`` in the same function;
* ``plugins/furl/hooks/compress_tool_output.py::main`` — ``redacted =
  _apply_env_redaction(text)``, then ``_compress_text(redacted)``;
* ``plugins/furl/hooks/pipe_compress.py::_decide`` — the same two-step.

Neither pass can simply be deleted. ``compress()`` returns only the COMPRESSED
view — ``CompressResult`` exposes no redacted original — so the MCP layer cannot
obtain ``R(x)`` for its own ``store.store(original=content)`` write without
running ``R`` itself; dropping the ``compress()`` pass instead would strip
redaction from every non-MCP caller. That is a design question, not cleanup,
and this file does not attempt to answer it. It PINS the behaviour that any
answer has to preserve.

Two halves, and they assert opposite things on purpose:

1. The BUILT-IN patterns are idempotent, so the second pass cannot change
   output. This is the half with teeth: it is what makes ``mcp_server``'s pass
   provably safe to keep and NOT safe to delete as "redundant".

2. The OPERATOR patterns (``FURL_REDACT_PATTERNS``) are NOT idempotent in
   general, by design and documented here rather than fixed. Direction of harm
   matters and is why this is characterised instead of repaired: pass 1 removes
   the secret, so pass 2 can only over-redact or mangle a marker. It is an
   output-fidelity defect that fails SAFE — never a leak.

WHY NO BUILD-TIME GUARD WARNS ABOUT (2). The obvious check is "warn if an
operator pattern matches ``[REDACTED:<n>]``". It was tried and it is neither
sound nor complete, both directions measured:

* NOT COMPLETE — ``test_seam_pattern_diverges_matching_no_marker_in_isolation``
  below. A pattern can match only the SEAM between a marker and the text around
  it, matching no marker on its own, and still diverge.
* NOT SOUND — ``compression_store._URL_CREDENTIAL_RE`` matches its own output
  ``://u:[REDACTED]@`` and is nonetheless idempotent, because its replacement
  maps ``[REDACTED]`` back to ``[REDACTED]``: a fixed point, not a growth step.
  A marker-keyed guard would flag that correct, shipping pattern.

So membership in the divergent set is a RUNTIME property — decided by applying
the composed redactor twice to the actual content — and is not statically
decidable from the pattern list. Anyone tempted to add the warning should read
those two tests first.

Secret fixtures are assembled from fragments and use obviously-fake material, so
commit scanners do not flag this file (same convention as
``test_redaction_private_key_blocks.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from furl_ctx.redaction import (
    _DEFAULT_CREDENTIAL_PATTERNS,
    build_default_redactor,
    build_store_redactor,
)

# ─── fixtures: split literals so scanners ignore this file ───────────────────

_ARMOR_TAIL = "PRIVATE" + " KEY-----"
_FAKE_BODY = "MIIEowIBAAKCAQEAvR7fakeKEYmaterial0123456789abcdefghijklmnopqrs"

_PEM_BLOCK = "-----BEGIN RSA " + _ARMOR_TAIL + "\n" + _FAKE_BODY + "\n-----END RSA " + _ARMOR_TAIL
_PEM_TRUNCATED = "-----BEGIN OPENSSH " + _ARMOR_TAIL + "\nAAAAB3NzaC1yc2EAAAAD"

# One specimen per built-in pattern name. The mapping is checked for EXHAUSTIVENESS below, so
# a newly added built-in fails loudly here instead of quietly escaping the idempotence proof.
_SPECIMENS: dict[str, str] = {
    "private-key": _PEM_BLOCK,
    "aws-access-key": "A" + "KIA" + "FAKEKEY012345678",
    "gcp-api-key": "AI" + "za" + "FAKEfake0123456789abcdefghijklmnopq",
    "openai-key": "s" + "k-" + "FAKEfake0123456789abcdef",
    "github-token": "gh" + "p_" + "FAKEfake0123456789abcdef",
    "slack-token": "xo" + "xb-" + "0123456789" + "-fakefake",
    "jwt": "ey" + "J" + "hbGciOiJIUzI1NiJ9." + "ey" + "J" + "zdWIiOiIxIn0.fakeSIGfake",
}


def _builtins() -> Callable[[str], str]:
    redactor = build_default_redactor({})
    assert redactor is not None, "built-ins are ON by default"
    return redactor


# ─── half 1: the built-ins are idempotent (LOAD-BEARING) ─────────────────────


def test_every_builtin_pattern_has_a_specimen() -> None:
    """The idempotence proof below is only as wide as ``_SPECIMENS``.

    Without this, adding an eighth built-in would leave it unproven while the
    suite stayed green — the proof would still pass, over a subset, and nobody
    would learn that its scope had stopped matching the table.
    """
    table = {name for name, _ in _DEFAULT_CREDENTIAL_PATTERNS}
    assert table == set(_SPECIMENS), (
        f"built-in credential patterns and idempotence specimens have diverged.\n"
        f"  in the table but unproven here: {sorted(table - set(_SPECIMENS))}\n"
        f"  specimens for patterns that no longer exist: {sorted(set(_SPECIMENS) - table)}\n"
        f"Add a specimen that the new pattern actually matches, then re-run this "
        f"file. Do NOT delete the specimen to make this pass."
    )


@pytest.mark.parametrize("name", sorted(_SPECIMENS), ids=sorted(_SPECIMENS))
def test_builtin_redaction_is_idempotent(name: str) -> None:
    """``R(R(x)) == R(x)`` for every built-in shape.

    This is what makes the double application on the three production paths
    SAFE, and it is the assertion that must go red before anyone deletes
    ``mcp_server._compress_content``'s redactor call as redundant: that call is
    load-bearing because ``store.store(original=content)`` writes the variable
    directly, bypassing ``compress()``'s message-copy redaction entirely.
    """
    redactor = _builtins()
    specimen = _SPECIMENS[name]
    once = redactor(specimen)

    # ANTI-VACUITY. The cheapest input satisfying "R(R(x)) == R(x)" is content that matches NOTHING: then R is the identity and idempotence is trivially
    # true while proving nothing about redaction. Require the specimen to have actually fired before the real assertion is allowed to mean anything.
    assert once != specimen, (
        f"specimen for {name!r} was not redacted at all, so the idempotence "
        f"assertion below would hold trivially for the identity function. The "
        f"specimen has stopped matching its pattern — fix the specimen, not the "
        f"assertion."
    )
    assert f"[REDACTED:{name}]" in once, (
        f"specimen for {name!r} was modified but did not produce its own marker; "
        f"it is being caught by a DIFFERENT pattern, so this test is no longer "
        f"exercising {name!r}"
    )

    assert redactor(once) == once, (
        f"built-in {name!r} is NOT idempotent: a second redaction pass changed "
        f"the output.\n  pass1: {once[:160]!r}\n  pass2: {redactor(once)[:160]!r}\n"
        f"Production applies this redactor TWICE to the same bytes (mcp_server."
        f"_compress_content, and both plugin hooks), so the stored original and "
        f"the compressed view now DIVERGE. See this file's module docstring."
    )


def test_all_builtin_shapes_are_idempotent_when_combined() -> None:
    """Every shape in one blob — patterns run in list order over each other's
    output, so a combination can diverge where each part alone does not."""
    redactor = _builtins()
    blob = "\n".join(f"line {i}: {text}" for i, text in enumerate(_SPECIMENS.values()))
    once = redactor(blob)
    assert once != blob, "combined blob was not redacted — specimens have gone stale"
    assert redactor(once) == once, (
        f"the built-ins are idempotent individually but NOT combined.\n"
        f"  pass1: {once[:200]!r}\n  pass2: {redactor(once)[:200]!r}"
    )


def test_truncated_pem_branch_is_idempotent() -> None:
    """The private-key pattern has TWO branches and the parametrized test above
    only exercises the full-block one.

    The truncated branch (armor plus a hanging base64 run, for output cut
    mid-payload) reaches a different alternation and consumes a different span,
    so its idempotence does not follow from the full block's.
    """
    redactor = _builtins()
    once = redactor(_PEM_TRUNCATED)
    assert once == "[REDACTED:private-key]", (
        f"the truncated-PEM specimen no longer redacts to a bare marker: {once!r}"
    )
    assert redactor(once) == once, (
        f"the TRUNCATED private-key branch is not idempotent.\n"
        f"  pass1: {once!r}\n  pass2: {redactor(once)!r}"
    )


def test_no_builtin_pattern_matches_any_builtin_marker() -> None:
    """The MECHANISM behind idempotence, pinned directly.

    A second pass can only differ if some pattern matches text a marker
    produced. Checking all ordered pairs states the reason the tests above pass,
    so a future pattern that breaks the property fails HERE with a precise
    cause rather than as a puzzling output mismatch.

    Note this is a sufficient condition, not a necessary one — a pattern can
    match its own marker and still be idempotent when the replacement is a fixed
    point (``_URL_CREDENTIAL_RE``, see the module docstring). It is pinned
    because it is the reason THESE patterns are safe, not as a general law.

    The markers are OBSERVED, not assumed. An earlier draft hardcoded
    ``f"[REDACTED:{name}]"`` and stayed green under a deliberate break that
    changed the emitted marker to an AWS-key shape — the test was checking a
    string production had stopped producing. Each specimen here is exactly its
    secret, so redacting it yields precisely the marker production emits.
    """
    emitted = {name: _builtins()(text) for name, text in _SPECIMENS.items()}
    for name, marker in emitted.items():
        assert marker.startswith("[") and marker.endswith("]"), (
            f"specimen for {name!r} is no longer exactly one secret — it "
            f"redacted to {marker!r} rather than a bare marker, so the "
            f"cross-check below would test the wrong string"
        )
    collisions = [
        (pattern_name, marker_name)
        for marker_name, marker in emitted.items()
        for pattern_name, pattern in _DEFAULT_CREDENTIAL_PATTERNS
        if re.search(pattern, marker)
    ]
    assert collisions == [], (
        f"a built-in pattern now matches a built-in marker: {collisions}. "
        f"Redaction may no longer be idempotent, and production applies it twice."
    )


# ─── half 2: operator patterns are NOT idempotent, by design ───────────────── These are CHARACTERISATION tests. If one goes red
# because the divergence is gone, that is a real change and the module docstring above needs rewriting — see each test's failure message.


def _operator(spec: str) -> Callable[[str], str]:
    """Operator patterns ONLY (built-ins off), so the divergence under test is
    unambiguously the operator pattern's and not a built-in's."""
    redactor = build_store_redactor({"FURL_REDACT_PATTERNS": spec, "FURL_REDACT_BUILTINS": "0"})
    assert redactor is not None
    return redactor


def test_operator_pattern_matching_its_own_marker_is_not_idempotent() -> None:
    """``[A-Z]{4,}`` matches ``REDACTED`` inside the marker it just emitted."""
    redactor = _operator(r"[A-Z]{4,}")
    text = "hello SECRETVALUE world"
    once = redactor(text)
    twice = redactor(once)

    assert once == "hello [REDACTED:1] world", f"pass 1 shape changed: {once!r}"
    assert twice == "hello [[REDACTED:1]:1] world", f"pass 2 shape changed: {twice!r}"
    assert once != twice, (
        "the operator redactor became IDEMPOTENT for a marker-matching pattern. "
        "That is a real behaviour change (an improvement), not a test bug: the "
        "module docstring and #28's conclusion both describe this divergence as "
        "present. Update them rather than deleting this test."
    )


def test_operator_divergence_does_not_converge() -> None:
    """Each pass GROWS the output — there is no fixed point to settle into.

    Matters because "just run it until stable" is the obvious repair and it does
    not terminate for this class of pattern.
    """
    redactor = _operator(r"[A-Z]{4,}")
    seen = ["x SECRETVALUE y"]
    for _ in range(6):
        seen.append(redactor(seen[-1]))
    lengths = [len(s) for s in seen]
    assert len(set(seen)) == len(seen), (
        f"the sequence reached a fixed point: {lengths}. If redaction now "
        f"converges, 'apply until stable' becomes a viable repair and #28's "
        f"conclusion changes."
    )
    assert lengths == sorted(lengths) and lengths[-1] > lengths[0], (
        f"expected monotonically growing output, measured {lengths}"
    )


def test_seam_pattern_diverges_matching_no_marker_in_isolation() -> None:
    """THE reason no build-time guard is shipped for this (#28).

    Pattern 1 matches only the SEAM between a marker and its surrounding text.
    It cannot fire on pass 1 — the marker does not exist yet — and does fire on
    pass 2. It matches NEITHER marker in isolation, so the natural static check
    ("does an operator pattern match ``[REDACTED:<n>]``?") reports SAFE while
    the composed redactor measurably diverges.

    Delete this test only together with the module docstring's claim that the
    divergent set is not statically decidable.
    """
    redactor = _operator("c\\s\\[R\nSECRETVALUE")
    text = "abc SECRETVALUE def"
    once = redactor(text)
    twice = redactor(once)

    assert once == "abc [REDACTED:2] def", f"pass 1 shape changed: {once!r}"
    assert twice == "ab[REDACTED:1]EDACTED:2] def", f"pass 2 shape changed: {twice!r}"
    assert once != twice, "the seam divergence is gone — see this test's docstring"

    # The load-bearing half: the static guard would have missed it.
    seam = re.compile(r"c\s\[R")
    assert not seam.search("[REDACTED:1]") and not seam.search("[REDACTED:2]"), (
        "this pattern now matches a marker in isolation, so it no longer "
        "demonstrates that marker-keyed detection is incomplete. Find a pattern "
        "that still matches only the seam, or drop the docstring's claim."
    )


def test_secrets_are_gone_after_the_first_pass_even_when_diverging() -> None:
    """Direction of harm: divergence over-redacts, it never re-exposes.

    This is why #28 is characterised rather than fixed. If it ever goes red,
    double redaction has become a SECURITY defect and the priority changes
    completely.
    """
    redactor = _operator(r"[A-Z]{4,}")
    text = "hello SECRETVALUE world"
    current = text
    for _ in range(5):
        current = redactor(current)
        assert "SECRETVALUE" not in current, (
            f"a redaction pass RE-EXPOSED the secret: {current!r}. This is no "
            f"longer a fidelity issue — treat as a security defect."
        )
