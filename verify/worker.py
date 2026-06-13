"""Single-case worker — runs ONE case in a FRESH PROCESS for cold CCR state.

The engine's CCR store has two layers: a Python ``CompressionStore``
(``reset_compression_store()`` clears it) and a Rust process-local CCR store
living on the singleton pipeline's ``SmartCrusher`` instance (capacity 1000,
FIFO eviction, NO Python reset surface). To guarantee genuinely cold CCR state
per case — as the verification mandate requires — each case is measured in its
own freshly-spawned Python process via this worker. A fresh process means a
fresh pipeline singleton and therefore a fresh, empty Rust CCR store.

Invoked as::

    python -m verify.worker '<json-spec>'

where the spec is ``{"family","size","tier","seed","needles"}``. Emits a single
JSON line on stdout: the ``CaseResult`` as a dict. Nothing else is printed
(engine logs go to stderr).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from verify import generators as gen
from verify.measure import measure


def build_case(spec: dict) -> object:
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
    result = measure(case)
    # Single clean JSON line on stdout.
    sys.stdout.write(json.dumps(asdict(result)) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
