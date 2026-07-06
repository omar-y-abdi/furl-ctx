"""LogTemplate (NR2-3a): machine-verified losslessness for the Drain-style miner.

The bar mirrors the CSV grammar suites: every encoding feature is exercised
through the INDEPENDENT reference decoder (`log_template_decoder`), the
roundtrip `decode(encode(x)) == x` is asserted byte-exact across hand-built
edges and seeded fuzz corpora, and malformed wire must raise the typed
`LogTemplateDecodeError` — never produce wrong output.

`encode()` returning ``None`` (no exploitable structure / would not shrink) is
CONTRACT, not failure: those inputs pass through verbatim upstream, so tests
assert `None` where the contract requires it and assert *successful* encoding
on corpora that must compress (otherwise the roundtrip tests would be vacuous).
"""

from __future__ import annotations

import random

import pytest

from furl_ctx.transforms import log_template_format as fmt
from furl_ctx.transforms.log_template import (
    MIN_TEMPLATE_SUPPORT,
    LogTemplateEncoding,
    encode,
    encode_verified,
)
from furl_ctx.transforms.log_template_decoder import (
    LogTemplateDecodeError,
    decode,
)

# ─── corpus builders (deterministic) ────────────────────────────────────────


def ip_port_log(n: int = 50) -> str:
    """A single-template corpus every encoder run MUST compress."""
    return (
        "\n".join(
            f"INFO connection established from 10.0.{i % 256}.{(i * 3) % 256} port {40000 + i}"
            for i in range(n)
        )
        + "\n"
    )


def multi_template_log(lines_per_template: int = 20) -> str:
    """Three distinct templates interleaved with occasional noise lines."""
    lines: list[str] = []
    for i in range(lines_per_template):
        lines.append(f"INFO user login accepted user=u{i:03d} session=sess-{i * 7:05d}")
        lines.append(f"WARN cache miss for key blob:{i * 13:06x} shard {i % 8}")
        # Variable fields sit BEYOND the Drain prefix depth (4 leading
        # tokens) — a varying token inside the prefix fragments the tree
        # into per-line buckets and no template can reach min support.
        lines.append(f"DEBUG request done for id=req-{i:04d} bytes={i * 991} elapsed_us={i * 37}")
        if i % 7 == 0:
            lines.append(f"totally unstructured noise #{i} !!")
    return "\n".join(lines) + "\n"


# ─── mining + header grammar (unit) ─────────────────────────────────────────


class TestMiningAndHeader:
    def test_encode_is_deterministic(self) -> None:
        text = multi_template_log()
        first = encode(text)
        second = encode(text)
        assert first is not None and second is not None
        assert first.wire == second.wire

    def test_single_template_extracted_with_wildcards(self) -> None:
        enc = encode(ip_port_log())
        assert enc is not None
        assert enc.template_count == 1
        header = enc.wire.split("\n")
        assert header[0] == "LT1"
        # The mined template keeps the literal skeleton and wildcards the
        # two variable positions (ip, port).
        assert (
            f"0{fmt.FIELD_SEPARATOR}INFO connection established from "
            f"{fmt.WILDCARD} port {fmt.WILDCARD}" in header[1]
        )
        assert "--RECORDS--" in enc.wire

    def test_multi_template_corpus_mines_each_shape(self) -> None:
        enc = encode(multi_template_log())
        assert enc is not None
        assert enc.template_count >= 3
        assert enc.templated_lines >= 3 * 20
        assert enc.verbatim_lines >= 1  # the noise lines

    def test_stats_partition_all_lines(self) -> None:
        text = multi_template_log()
        enc = encode(text)
        assert enc is not None
        total_lines = len(text.splitlines())
        assert enc.templated_lines + enc.verbatim_lines == total_lines

    @pytest.mark.parametrize(
        ("line_count", "should_encode"),
        [
            (MIN_TEMPLATE_SUPPORT - 1, False),  # 2 lines: below support → no template
            (MIN_TEMPLATE_SUPPORT, True),  # 3 lines: exactly at support → template forms
        ],
    )
    def test_min_support_boundary(self, line_count: int, should_encode: bool) -> None:
        # Straddles `count >= MIN_TEMPLATE_SUPPORT`: a `>` mutation would stop the
        # exactly-at-threshold corpus from encoding. Lines share a fixed skeleton
        # with two variable positions (id, port) so a wildcard template can form.
        text = (
            "\n".join(
                f"INFO connection established from host alpha id={1000 + i} port {40000 + i}"
                for i in range(line_count)
            )
            + "\n"
        )
        enc = encode(text)
        if should_encode:
            assert enc is not None
            assert enc.template_count == 1
            assert decode(enc.wire) == text  # still lossless at the boundary
        else:
            assert enc is None

    def test_params_with_structural_bytes_are_escaped(self) -> None:
        # Params containing the param separator, tabs, backslashes and the
        # escape sequences' own spellings must survive byte-exact.
        rows = [f"EVT payload=a|b|{i} raw=C:\\logs\\n{i}\ttail" for i in range(6)]
        text = "\n".join(rows) + "\n"
        enc = encode_verified(text)
        if enc is not None:  # roundtrip proven by encode_verified already
            assert decode(enc.wire) == text
            assert f"{fmt.ESCAPE_CHAR}p" in enc.wire  # escaped "|"

    def test_literal_wildcard_in_fixed_text_roundtrips(self) -> None:
        # A literal "<*>" in otherwise-fixed template text must not be
        # mistaken for a real slot (WILDCARD_SENTINEL protection).
        # Long constant tail + enough rows so the encoding actually SHRINKS
        # (short corpora decline on the growth gate — that's contract).
        rows = [
            f"NOTE marker <*> stays literal in this fixed template tail, seq={i}" for i in range(20)
        ]
        text = "\n".join(rows) + "\n"
        enc = encode_verified(text)
        assert enc is not None
        assert decode(enc.wire) == text


# ─── roundtrip edges: None (passthrough) is allowed, wrong bytes are not ────

EDGE_INPUTS: list[str] = [
    "",
    "one line",
    "one line no terminator",
    "prose without structure. sentences vary wildly, nothing repeats here.",
    "\n".join(f"noise-{i * 7919 % 100}-{i}" for i in range(10)) + "\n",
    ip_port_log(),
    multi_template_log(),
    # unicode: emoji, combining chars
    "\n".join(f"INFO café rëady 🎉 user=ü{i} ñ-{i}" for i in range(12)) + "\n",
    # CRLF endings
    "\r\n".join(f"GET /api/x id={i}" for i in range(10)) + "\r\n",
    # bare-CR endings
    "\r".join(f"PUT /api/y id={i}" for i in range(10)) + "\r",
    # mixed terminators across lines
    "a first=1\nb second=2\r\nc third=3\rd fourth=4",
    # no trailing newline on a templated corpus
    "\n".join(f"JOB done id={i} rc=0" for i in range(15)),
    # trailing spaces and tabs preserved per line
    "\n".join(f"ROW value={i}  \t" for i in range(9)) + "\n",
    # very long single line
    "X" * 10_000,
    # lines that spell the wire format's own markers (adversarial)
    "LT1\n--RECORDS--\nT\t0\tfake|params\t\\n\nV\tfake\t\\n\n" * 3,
    "\n".join(f"T\t{i}\tinjected|{i}\t\\n" for i in range(8)) + "\n",
    # escape-sequence spellings as literal content
    "\n".join(f"P path=C:\\temp\\new\\t{i} v=\\p{i}" for i in range(7)) + "\n",
]


class TestRoundtripEdges:
    @pytest.mark.parametrize("text", EDGE_INPUTS, ids=range(len(EDGE_INPUTS)))
    def test_encode_verified_roundtrips_or_declines(self, text: str) -> None:
        enc = encode_verified(text)
        if enc is not None:
            assert decode(enc.wire) == text

    def test_raw_encode_matches_verified_when_both_encode(self) -> None:
        # encode_verified is encode + self-check; on success they agree.
        text = ip_port_log()
        raw, verified = encode(text), encode_verified(text)
        assert raw is not None and verified is not None
        assert raw.wire == verified.wire

    def test_templated_corpora_must_actually_encode(self) -> None:
        # Guards against a vacuous suite: the flagship corpora MUST compress.
        for text in (ip_port_log(), multi_template_log()):
            enc = encode_verified(text)
            assert enc is not None, "flagship corpus unexpectedly declined"
            assert len(enc.wire) < len(text)
            assert decode(enc.wire) == text


# ─── encode() None-contract ─────────────────────────────────────────────────


class TestEncodeContract:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "single line only",
            "two lines\nof nothing alike",
            "prose one.\nutterly different two!\nthird thing entirely?",
        ],
        ids=["empty", "single", "two-distinct", "prose"],
    )
    def test_structureless_input_declines(self, text: str) -> None:
        assert encode(text) is None

    def test_no_shared_structure_declines(self) -> None:
        # Every line's FIRST token differs (distinct hex ids), so each lands in
        # its own prefix bucket, no cluster reaches MIN_TEMPLATE_SUPPORT, and the
        # support gate declines. Deterministically None — pinning the branch (not
        # `is None or smaller`) so a broken support gate that emitted an encoding
        # would fail here.
        text = (
            "\n".join(
                f"{i:08x} {i * 3:08x} {i * 5:08x} {i * 7:08x} {i * 11:08x}" for i in range(20)
            )
            + "\n"
        )
        assert encode(text) is None

    def test_size_gate_declines_when_wire_would_grow(self) -> None:
        # A template DOES form here (3 lines share the 4-token prefix "A B C D",
        # so support == MIN_TEMPLATE_SUPPORT), but the per-record framing overhead
        # makes the wire larger than the tiny input. This reaches the SIZE gate
        # (`len(wire) >= len(text)`) — distinct from the support-gate path above —
        # which must decline rather than ship an inflated encoding.
        text = "\n".join(f"A B C D {i:02x} {(i * 2) & 0xFF:02x}" for i in range(3)) + "\n"
        assert encode(text) is None

    def test_encoding_carries_honest_stats(self) -> None:
        enc = encode(ip_port_log(50))
        assert isinstance(enc, LogTemplateEncoding)
        assert enc.template_count == 1
        assert enc.templated_lines == 50
        assert enc.verbatim_lines == 0


# ─── decoder negatives: malformed wire raises, never wrong output ───────────

MALFORMED_WIRES: dict[str, str] = {
    "missing-magic": "0\tINFO x <*>\n--RECORDS--\nT\t0\t1\t\\n\n",
    "wrong-magic": "LT9\n0\tINFO x <*>\n--RECORDS--\nT\t0\t1\t\\n\n",
    "missing-records-marker": "LT1\n0\tINFO x <*>\nT\t0\t1\t\\n\n",
    "unknown-record-kind": "LT1\n0\tA <*>\n--RECORDS--\nX\t0\t1\t\\n\n",
    "non-numeric-template-id": "LT1\n0\tA <*>\n--RECORDS--\nT\tzero\t1\t\\n\n",
    "undefined-template-id": "LT1\n0\tA <*>\n--RECORDS--\nT\t7\t1\t\\n\n",
    "too-few-record-fields": "LT1\n0\tA <*>\n--RECORDS--\nT\t0\n",
    "param-count-mismatch": "LT1\n0\tA <*> B <*>\n--RECORDS--\nT\t0\t1\t\\n\n",
    "bad-escape-sequence": "LT1\n0\tA <*>\n--RECORDS--\nT\t0\t\\q\t\\n\n",
    "bad-header-id": "LT1\nnope\tA <*>\n--RECORDS--\nT\t0\t1\t\\n\n",
}


class TestDecoderNegatives:
    @pytest.mark.parametrize("wire", MALFORMED_WIRES.values(), ids=MALFORMED_WIRES.keys())
    def test_malformed_wire_raises_typed_error(self, wire: str) -> None:
        with pytest.raises(LogTemplateDecodeError):
            decode(wire)


# ─── seeded fuzz: 4 corpus shapes × 80 seeds = 320 cases ────────────────────

_WORDS = "alpha beta gamma delta epsilon zeta theta iota kappa mu".split()
_TERMS = ["\n", "\r\n", "\r"]
_SPICE = ["|", "\t", "\\", "<*>", "🎉", "é", "  ", "\\p", "\\n", "LT1", "--RECORDS--"]


def _template_shaped(rng: random.Random) -> str:
    # Three constant words after the level keep every variable field BEYOND
    # Drain's prefix depth (4) — variance inside the prefix fragments the
    # tree into per-line buckets and nothing reaches min support.
    templates = [
        (
            f"{rng.choice(['INFO', 'WARN', 'DEBUG'])} "
            f"{rng.choice(_WORDS)} {rng.choice(_WORDS)} {rng.choice(_WORDS)} "
            "id={} value={} tag={}"
        )
        for _ in range(rng.randint(1, 4))
    ]
    lines = [
        rng.choice(templates).format(
            rng.randrange(10_000),
            rng.choice(_SPICE) if rng.random() < 0.15 else rng.randrange(1_000_000),
            f"{rng.choice(_WORDS)}-{rng.randrange(100)}",
        )
        for _ in range(rng.randint(8, 40))
    ]
    term = rng.choice(_TERMS)
    return term.join(lines) + (term if rng.random() < 0.7 else "")


def _near_template(rng: random.Random) -> str:
    # Template-ish lines mutated: token dropped/duplicated/reordered so the
    # similarity threshold is exercised right at its boundary.
    base = f"GET /api/{rng.choice(_WORDS)} status={{}} took={{}}ms"
    lines = []
    for _ in range(rng.randint(6, 25)):
        line = base.format(rng.choice([200, 404, 500]), rng.randrange(900))
        tokens = line.split(" ")
        op = rng.random()
        if op < 0.3:
            tokens.insert(rng.randrange(len(tokens)), rng.choice(_WORDS))
        elif op < 0.5 and len(tokens) > 2:
            tokens.pop(rng.randrange(len(tokens)))
        lines.append(" ".join(tokens))
    return "\n".join(lines) + rng.choice(["", "\n"])


def _noise(rng: random.Random) -> str:
    lines = [
        "".join(rng.choice("abcdefghij0123456789 |\\\t<>*é🎉") for _ in range(rng.randint(0, 60)))
        for _ in range(rng.randint(0, 15))
    ]
    return rng.choice(_TERMS).join(lines)


def _mixed(rng: random.Random) -> str:
    parts = [rng.choice([_template_shaped, _near_template, _noise])(rng) for _ in range(3)]
    return "\n".join(parts)


class TestFuzzRoundtrip:
    @pytest.mark.parametrize(
        "shape", [_template_shaped, _near_template, _noise, _mixed], ids=lambda f: f.__name__
    )
    def test_seeded_corpora_roundtrip_byte_exact(self, shape) -> None:
        encoded = 0
        for seed in range(80):
            # str seeds hash via SHA-512 in random.seed — deterministic
            # across processes (tuple.__hash__ is PYTHONHASHSEED-dependent).
            rng = random.Random(f"{shape.__name__}-{seed}")
            text = shape(rng)
            enc = encode_verified(text)
            if enc is not None:
                encoded += 1
                assert decode(enc.wire) == text, f"{shape.__name__} seed={seed}"
        if shape is _template_shaped:
            # The template-shaped generator must not be vacuous.
            assert encoded >= 20, f"only {encoded}/80 template-shaped cases encoded"
