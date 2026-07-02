"""Encoding-fire probe — READ-ONLY observation of the engine's output grammar.

Runs ONE case in a fresh process (cold CCR) with DEFAULT params and reports
which lossless structural encodings the engine emitted into the rendered
compressed output: the cross-row affix fold (``__affix:``), the head-dictionary
fold (``__head:``), the value dictionary (``__dict:``), and whether a
``<<ccr:HASH>>`` drop sentinel was emitted. We change NOTHING — we only read
the engine's own rendered text to verify the round-3 affix/head claims fire on
structured columns and decline on genuine-entropy columns.

Invoked as::

    python -m verify.heldout.encprobe '<json-spec>'
"""

from __future__ import annotations

import json
import sys

from furl_ctx import compress
from furl_ctx.cache.compression_store import reset_compression_store
from verify.heldout import generators as gen

MARKERS = {
    "affix": "__affix:",
    "head": "__head:",
    "dict": "__dict:",
    "ccr_drop": "<<ccr:",
}


def _stringify(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def build_case(spec: dict):
    family = spec["family"]
    n = int(spec["size"])
    tier = spec["tier"]
    seed = int(spec["seed"])
    repeated = family == "repeated_logs"
    if family in ("logs", "repeated_logs"):
        case = gen.gen_logs(seed, n, tier, repeated=repeated)
    elif family == "search":
        case = gen.gen_search(seed, n, tier)
    elif family == "code":
        case = gen.gen_code(seed, n, tier)
    elif family == "multiturn":
        case = gen.gen_multiturn(seed, n, tier)
    elif family == "disk":
        case = gen.gen_disk(seed, n, tier)
    else:
        raise ValueError(f"unknown family {family!r}")
    if spec.get("needles") and not case.conversation and family != "code":
        case = gen.plant_needles(case, seed, k=3)
    return case


def main() -> int:
    spec = json.loads(sys.argv[1])
    case = build_case(spec)
    reset_compression_store()
    result = compress(case.messages, model="gpt-4o")  # DEFAULT params only
    # Concatenate every rendered message so we observe encodings across the
    # whole (possibly multi-message) output.
    out = "\n".join(_stringify(m.get("content")) for m in result.messages)
    fired = {k: (mark in out) for k, mark in MARKERS.items()}
    fired["used_default_params"] = True  # no config / kwargs passed
    sys.stdout.write(json.dumps(fired) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
