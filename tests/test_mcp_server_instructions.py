"""MCP server-level instructions: the CSV decode legend (engine P1-10).

Owner-approved alternative (b) of QUESTIONS-FOR-USER.md item 15: the
compacted-table decode legend ships once per conversation via the MCP
SDK's server-level ``instructions=`` parameter — zero wire-byte change to
any table render — gated by ``FURL_MCP_LEGEND`` (default ON).

Two test planes:

1. **Flag plumbing** — the legend is present on the constructed
   ``mcp.server.Server`` (and in ``create_initialization_options()``)
   by default and absent for every recognized OFF spelling.
2. **Grammar pins** — every decode claim the legend makes is verified
   EXECUTABLY against the reference decoder
   (``furl_ctx.transforms.csv_schema_decoder``), so the legend can never
   drift from the grammar without a test failing here. The CRITICAL pin:
   a cell ``53`` under ``time_ms:float%3`` decodes to 0.053, not 53.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from furl_ctx.ccr.mcp_server import (  # noqa: E402
    CSV_DECODE_LEGEND,
    FurlMCPServer,
    _legend_enabled,
)
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows  # noqa: E402

# ─── Flag plumbing ───────────────────────────────────────────────────────────


def test_legend_present_by_default(monkeypatch) -> None:
    monkeypatch.delenv("FURL_MCP_LEGEND", raising=False)
    server = FurlMCPServer()
    assert server.server.instructions == CSV_DECODE_LEGEND


def test_legend_present_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("FURL_MCP_LEGEND", "on")
    server = FurlMCPServer()
    assert server.server.instructions == CSV_DECODE_LEGEND


@pytest.mark.parametrize("off_value", ["off", "OFF", "false", "0", "no", "disabled", " off "])
def test_legend_absent_when_flag_off(monkeypatch, off_value: str) -> None:
    monkeypatch.setenv("FURL_MCP_LEGEND", off_value)
    server = FurlMCPServer()
    assert server.server.instructions is None


def test_unrecognized_value_keeps_legend_on(monkeypatch) -> None:
    # Default-ON flag: a typo must not silently disable the legend.
    monkeypatch.setenv("FURL_MCP_LEGEND", "of")
    assert _legend_enabled() is True


def test_initialization_options_carry_the_legend(monkeypatch) -> None:
    # The SDK threads Server.instructions into the initialize response via
    # InitializationOptions — the surface an MCP host actually receives.
    monkeypatch.delenv("FURL_MCP_LEGEND", raising=False)
    server = FurlMCPServer()
    options = server.server.create_initialization_options()
    assert options.instructions == CSV_DECODE_LEGEND

    monkeypatch.setenv("FURL_MCP_LEGEND", "off")
    gated = FurlMCPServer()
    assert gated.server.create_initialization_options().instructions is None


def test_legend_flag_read_at_construction_not_import(monkeypatch) -> None:
    # SEC-7 discipline: the env is re-read per construction, never frozen
    # at import — two servers built under different env values disagree.
    monkeypatch.setenv("FURL_MCP_LEGEND", "off")
    off_server = FurlMCPServer()
    monkeypatch.setenv("FURL_MCP_LEGEND", "on")
    on_server = FurlMCPServer()
    assert off_server.server.instructions is None
    assert on_server.server.instructions == CSV_DECODE_LEGEND


# ─── Legend content sanity ───────────────────────────────────────────────────


def test_legend_names_every_shipped_encoding() -> None:
    # One mention per shipped encoding family (source of truth:
    # formatter.rs / csv_schema_decoder.py). Substring pins, not prose
    # equality — wording may evolve, coverage may not.
    for needle in (
        "[N]{col:type,...}",  # declaration line
        "type=V",  # constant fold
        "int=B+S",  # arithmetic fold
        "float%k",  # decimal scale-fold
        "0.053, not 53",  # the CRITICAL %k example
        "string~",  # ISO-delta
        "string^",  # affix fold
        "__affix:",
        "string@",  # head-dict fold
        "__head:",
        "__dict:",  # dictionary column
        "__null__",  # null sentinel
        "__missing__",  # missing-key sentinel
        "<<ccr:HASH>>",  # recovery marker
        "furl_retrieve",  # the retrieval verb
    ):
        assert needle in CSV_DECODE_LEGEND, f"legend lost its {needle!r} claim"


def test_legend_is_agent_first_structured() -> None:
    # The restructure (agent-first): a plain-English summary and ONE worked
    # example come BEFORE the compact grammar reference, so a fresh agent can
    # decode a table without reverse-engineering the grammar first.
    legend = CSV_DECODE_LEGEND
    assert "GRAMMAR:" in legend, "grammar reference section is missing"
    grammar_at = legend.index("GRAMMAR:")
    # Plain-English summary + worked example precede the grammar block.
    assert legend.index("read one before you reason") < grammar_at
    assert legend.index("Example:") < grammar_at
    # Marker meaning + retrieve-before-reasoning rule are stated up front.
    assert legend.index("furl_retrieve") < grammar_at
    assert "offloaded, not lost" in legend
    # The critical %k read-back is in the worked example, before the grammar.
    assert legend.index("0.053, not 53") < grammar_at


def test_legend_stays_within_token_budget() -> None:
    # Server-level instructions cost tokens on every conversation — a guard
    # against future bloat. The cap was 255 for the agent-first grammar legend
    # (which actually measured ~248, not the "~190" an older comment claimed).
    # Naming the INPUT SHAPE each output comes from (JSON array -> table;
    # line-oriented text -> head+tail + marker) — the grammar-truth fix that
    # stops an agent assuming a log was tabled when it was offloaded — costs
    # ~25 tokens, so the cap is 280. Still a tight bloat guard.
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("o200k_base")
    assert len(enc.encode(CSV_DECODE_LEGEND)) <= 280


def test_legend_names_input_shape_for_each_output() -> None:
    # Grammar-truth pin (docs-to-reality): the legend must tie each output form
    # to the INPUT that produces it, or an agent reasons wrongly about what is
    # safe to skip retrieving. Verified live: a JSON array routes to
    # SmartCrusher's `[N]{col:type,...}` table, while line-oriented text
    # (a ping log) routes to router:ccr_offload — head+tail + a <<ccr:HASH>>
    # marker, NOT a table, in BOTH normal and aggressive modes.
    legend = CSV_DECODE_LEGEND
    # The table is attributed to a structured JSON array, not "tool outputs".
    assert "structured JSON array" in legend
    # Line-oriented text is explicitly called out as NOT tabled + offloaded.
    assert "NOT tabled" in legend
    assert "head+tail" in legend
    # The worked example is labelled a JSON-array ROW, not a raw log line, so
    # `[1]{lvl:string=INFO,ms:float%3}` is not mistaken for a log-line grammar.
    assert "array row" in legend
    # The old, over-general claim that Furl tables long tool outputs is gone.
    assert "packs long tool outputs into small tables" not in legend


# ─── Grammar pins: every legend claim decoded by the reference decoder ──────
#
# The legend is prose; these decode fixtures make each claim executable.
# If the grammar (or the decoder) ever changes shape, the legend must be
# re-verified — these tests are that forcing function.


def test_legend_claim_decimal_scale_critical_example() -> None:
    # "float%k: cell/10^k (53 at %3 = 0.053, not 53)"
    rows = decode_csv_schema_rows("[2]{time_ms:float%3}\n53\n1200")
    assert rows == [{"time_ms": 0.053}, {"time_ms": 1.2}]


def test_legend_claim_constant_fold() -> None:
    # "type=V: constant V" — the column is re-attached to every row.
    rows = decode_csv_schema_rows("[2]{status:string=ok,name:string}\nalice\nbob")
    assert rows == [
        {"status": "ok", "name": "alice"},
        {"status": "ok", "name": "bob"},
    ]


def test_legend_claim_arithmetic_fold_zero_based() -> None:
    # "int=B+S: row i = B+S*i (i from 0)".
    rows = decode_csv_schema_rows("[3]{seq:int=7+2,name:string}\na\nb\nc")
    assert [r["seq"] for r in rows] == [7, 9, 11]


def test_legend_claim_ditto_repeats_cell_above() -> None:
    # "= alone repeats the cell above" (same column, previous row).
    rows = decode_csv_schema_rows("[2]{name:string}\nalpha\n=")
    assert rows == [{"name": "alpha"}, {"name": "alpha"}]


def test_legend_claim_iso_delta() -> None:
    # "string~: full ISO timestamp first, then ±seconds[/tz] deltas".
    rows = decode_csv_schema_rows("[2]{ts:string~}\n2024-01-02T03:04:05Z\n+60")
    assert rows == [
        {"ts": "2024-01-02T03:04:05Z"},
        {"ts": "2024-01-02T03:05:05Z"},
    ]


def test_legend_claim_affix_fold() -> None:
    # "string^ + __affix:col=P,S: value = P+cell+S".
    rows = decode_csv_schema_rows("[2]{path:string^}\n__affix:path=api/,.json\nusers\norders")
    assert rows == [{"path": "api/users.json"}, {"path": "api/orders.json"}]


def test_legend_claim_head_dict_fold() -> None:
    # "string@ + __head:col=<d>h0,h1: cell 1<d>tail = h1+tail" — heads
    # carry their trailing delimiter; the cell's index picks the head.
    rows = decode_csv_schema_rows(
        "[2]{url:string@}\n__head:url=/api/v1/,static/\n0/users\n1/app.js"
    )
    assert rows == [{"url": "api/v1/users"}, {"url": "static/app.js"}]


def test_legend_claim_dict_column() -> None:
    # "__dict:col=v0,v1: cells index the list".
    rows = decode_csv_schema_rows("[3]{env:string}\n__dict:env=prod,dev\n0\n1\n0")
    assert [r["env"] for r in rows] == ["prod", "dev", "prod"]


def test_legend_claim_null_missing_and_nullable() -> None:
    # "__null__ null, __missing__ absent key, ? nullable".
    rows = decode_csv_schema_rows("[3]{v:string?}\n__null__\n__missing__\nreal")
    assert rows == [{"v": None}, {}, {"v": "real"}]
