"""Improvement-2 (field-aware dedup) honest A/B — stable hash ON vs OFF.

DESIGN.md Improvement 2 replaces the whole-item dedup hash with a *stable
projection* hash that excludes high-cardinality "VaryingIdentity" columns
(per-row timestamps / UUIDs / commit hashes / monotone counters). One such
column makes every row's whole-item hash unique, silently defeating dedup,
clustering, and fill-diversity. Excluding it lets rows that share their
value-bearing content collapse onto one stable hash.

There is **no runtime config flag** that disables the field-aware hash: the
engine derives the identity exclude-set unconditionally from the array's
field statistics (``SmartCrusherConfig`` exposes no ``field_aware`` /
``stable_hash`` toggle — verified). So the honest A/B holds the *data* fixed
and compares the engine's TWO documented hash primitives:

- **Imp2 OFF** — ``compute_item_hash``: ``md5(json.dumps(item,
  sort_keys=True))[:16]`` over the WHOLE item (the pre-Imp2 dedup key).
- **Imp2 ON**  — ``stable_item_hash``: the same hash over the item with the
  identity columns projected out (the post-Imp2 dedup key).

Both are reproduced here byte-for-byte (the engine documents the hash input
as Python ``json.dumps(item, sort_keys=True, ensure_ascii=True)`` — see
``anchor_selector.rs`` ``python_json_dumps_sort_keys`` / ``compute_item_hash``
/ ``stable_item_hash``). The reproduction is *validated* against the engine's
own parity contract (empty exclude-set ⇒ stable == whole-item) before any
A/B number is reported, and corroborated end-to-end by the engine's real
``_dup_count`` annotation on the lossy path.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import furl_ctx._core as core

# Engine identity-gate constants, mirrored from field_role.rs.
IDENTITY_RATIO_THRESHOLD = 0.9  # field_role.rs:45 — near-unique ⇒ identity candidate.

# ISO-8601 (T or space separated) datetime / date, mirrored from analyzer.rs.
_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_ISO_D_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# ---------------------------------------------------------------------------
# Engine hash primitives, reproduced byte-for-byte (anchor_selector.rs).
# ---------------------------------------------------------------------------


def _engine_canonical(item: Any) -> str:
    """``python_json_dumps_sort_keys``: json.dumps(sort_keys, ensure_ascii)."""
    return json.dumps(item, sort_keys=True, ensure_ascii=True)


def whole_item_hash(item: Any) -> str:
    """``compute_item_hash`` — the pre-Imp2 (whole-item) dedup key."""
    digest = hashlib.md5(_engine_canonical(item).encode("utf-8")).hexdigest()
    return digest[:16]


def stable_item_hash(item: Any, exclude: frozenset[str]) -> str:
    """``stable_item_hash`` — the post-Imp2 (field-aware projection) dedup key.

    Empty exclude-set ⇒ byte-identical to ``whole_item_hash`` (the engine's
    parity contract). Otherwise the TOP-LEVEL excluded keys are projected out
    before hashing — exactly the engine's ``write_python_json_filtered``.
    """
    if not exclude or not isinstance(item, dict):
        return whole_item_hash(item)
    projected = {k: v for k, v in item.items() if k not in exclude}
    return whole_item_hash(projected)


# ---------------------------------------------------------------------------
# Identity exclude-set, mirrored from field_role.rs::compute_exclude_set.
# ---------------------------------------------------------------------------


def _is_sequential(values: list[Any]) -> bool:
    """Monotone integer counter (numeric identity), per detect_sequential."""
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(nums) < 3:
        return False
    diffs = {round(b - a, 9) for a, b in zip(nums, nums[1:])}
    return len(diffs) == 1 and next(iter(diffs)) != 0


def _is_identity_shaped(values: list[Any]) -> bool:
    """A clear majority of the sample shape-matches an identity pattern."""
    strs = [v for v in values if isinstance(v, str)]
    if strs:
        shaped = sum(
            1
            for s in strs
            if _ISO_DT_RE.match(s) or _ISO_D_RE.match(s) or _UUID_RE.match(s) or _is_hex_run(s)
        )
        return shaped / len(strs) >= 0.8  # IDENTITY_SHAPE_FRACTION
    return _is_sequential(values)


def _is_hex_run(s: str) -> bool:
    body = s[2:] if s[:2] in ("0x", "0X") else s
    return len(body) >= 8 and all(c in "0123456789abcdefABCDEF" for c in body)


def compute_exclude_set(items: list[dict[str, Any]]) -> frozenset[str]:
    """Mirror ``compute_exclude_set``: identity columns (ratio≥0.9 + shape).

    Derived from the SAME field statistics the engine uses: a field is
    excluded only when it is near-unique (``unique_ratio >= 0.9``) AND its
    values shape-match an identity pattern (ISO datetime/date, UUID, hex run,
    or a monotone numeric counter). Constant and low-cardinality fields, and
    high-cardinality *content* (natural-language messages, file paths), are
    kept — exactly as the engine classifies them.
    """
    if not items:
        return frozenset()
    n = len(items)
    keys: list[str] = []
    for it in items:
        for k in it:
            if k not in keys:
                keys.append(k)
    exclude: set[str] = set()
    for k in keys:
        vals = [it[k] for it in items if k in it and it[k] is not None]
        if not vals:
            continue
        unique_ratio = len(set(map(_freeze, vals))) / n
        if unique_ratio < IDENTITY_RATIO_THRESHOLD:
            continue
        if _is_identity_shaped(vals[:20]):  # SAMPLE_LIMIT
            exclude.add(k)
    return frozenset(exclude)


def _freeze(v: Any) -> Any:
    """Hashable view of a value for set-cardinality counting."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    return v


# ---------------------------------------------------------------------------
# A/B result + the validated measurement.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Imp2AB:
    """The field-aware dedup A/B on one repeated-content dataset."""

    dataset: str
    n_rows: int
    exclude_set: tuple[str, ...]
    families_off: int  # distinct whole-item-hash families (Imp2 OFF)
    families_on: int  # distinct stable-projection-hash families (Imp2 ON)
    collapse_ratio: float  # (off - on) / off  — redundancy Imp2 recovers
    largest_family_on: int  # biggest field-aware family size (the _dup_count)
    engine_max_dup_count: int  # engine's real _dup_count on the lossy path
    engine_strategy: str  # engine strategy on the lossy (no-compaction) path


def validate_parity() -> None:
    """Fail loudly unless the reproduced hashes honour the engine contract.

    Two invariants from ``anchor_selector.rs`` tests:
    - empty exclude-set ⇒ stable == whole-item (parity / non-identity safety);
    - projecting key ``b`` ⇒ stable({a,b,c}\\{b}) == whole-item({a,c}).
    """
    sample = {"a": 1, "b": "x", "c": [1, 2, 3], "ts": "2026-06-10 21:00:00+02"}
    assert stable_item_hash(sample, frozenset()) == whole_item_hash(sample), (
        "parity broken: empty exclude must equal whole-item hash"
    )
    assert stable_item_hash({"a": 1, "b": "drop", "c": 3}, frozenset({"b"})) == (
        whole_item_hash({"a": 1, "c": 3})
    ), "projection broken: stable must equal whole-item of the projected object"


def measure_imp2_ab(dataset: str, items: list[dict[str, Any]]) -> Imp2AB:
    """Compute the field-aware dedup A/B for ``items`` and corroborate it.

    Reports the distinct hash-family count under the whole-item key (Imp2 OFF)
    vs the field-aware stable key (Imp2 ON), and corroborates with the engine's
    real ``_dup_count`` annotation on its lossy path (``without_compaction``,
    which bypasses the lossless-table short-circuit so the dedup path — where
    Imp2 lives — actually runs).
    """
    validate_parity()
    exclude = compute_exclude_set(items)

    families_off = Counter(whole_item_hash(it) for it in items)
    families_on = Counter(stable_item_hash(it, exclude) for it in items)
    n = len(items)
    n_off = len(families_off)
    n_on = len(families_on)
    collapse = (n_off - n_on) / n_off if n_off else 0.0
    largest_on = max(families_on.values(), default=0)

    # Engine corroboration on the lossy path.
    sc = core.SmartCrusher.without_compaction(core.SmartCrusherConfig())
    res = sc.crush_array_json(json.dumps(items, ensure_ascii=False), f"benchmark {dataset}")
    kept = res["items"]
    rendered = json.dumps(kept) if not isinstance(kept, str) else kept
    dup_vals = [int(d) for d in re.findall(r'"_dup_count"\s*:\s*(\d+)', rendered)]

    return Imp2AB(
        dataset=dataset,
        n_rows=n,
        exclude_set=tuple(sorted(exclude)),
        families_off=n_off,
        families_on=n_on,
        collapse_ratio=collapse,
        largest_family_on=largest_on,
        engine_max_dup_count=max(dup_vals, default=0),
        engine_strategy=str(res["strategy_info"]),
    )
