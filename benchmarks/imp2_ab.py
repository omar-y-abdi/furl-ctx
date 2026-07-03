"""Improvement-2 (field-aware dedup) honest A/B — stable hash ON vs OFF.

docs/audits/DESIGN.md (historical Phase-2 design) Improvement 2 replaces the whole-item dedup hash with a *stable
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
/ ``stable_item_hash``). Before any A/B number is reported the reproduction
is *validated*, loudly (TEST-16e — nothing is printed-but-unasserted):

* ``validate_parity`` — the engine's hash-primitive contracts (empty
  exclude-set ⇒ stable == whole-item; projection equivalence).
* ``validate_dup_count_channel`` — the end-to-end corroboration channel
  actually FIRES: on a fixture with real identity-counter duplication the
  engine's lossy path stamps ``_dup_count`` and the stamps equal this
  module's per-kept-row prediction (mirrored exclude-set + stable hash +
  first-kept-representative rule, ``route.rs::annotate_dup_counts``).
* ``measure_imp2_ab`` itself asserts the engine's stamps on the measured
  dataset equal the mirror's prediction row-for-row — including the
  "no stamps" case (no kept row represents a >1 family), which previously
  printed ``engine_max_dup_count=0`` with nothing checking it.
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


# ---------------------------------------------------------------------------
# Engine corroboration: `_dup_count` stamps vs the mirror's prediction.
# ---------------------------------------------------------------------------


def _crush_kept_rows(items: list[dict[str, Any]], context: str) -> tuple[list[dict[str, Any]], str]:
    """Run the engine's lossy path and return (kept row dicts, strategy).

    ``without_compaction`` bypasses the lossless-table short-circuit so the
    dedup path — where Imp2 lives — actually runs. The trailing
    ``{"_ccr_dropped": ...}`` sentinel row is not a kept row and is removed.
    """
    sc = core.SmartCrusher.without_compaction(core.SmartCrusherConfig())
    res = sc.crush_array_json(json.dumps(items, ensure_ascii=False), context)
    kept_payload = res["items"]
    rendered = kept_payload if isinstance(kept_payload, str) else json.dumps(kept_payload)
    rows = json.loads(rendered)
    assert isinstance(rows, list), f"engine kept-rows payload is not an array: {type(rows)}"
    kept = [r for r in rows if isinstance(r, dict) and "_ccr_dropped" not in r]
    return kept, str(res["strategy_info"])


def predict_dup_stamps(
    kept_rows: list[dict[str, Any]],
    all_items: list[dict[str, Any]],
    exclude: frozenset[str],
) -> list[int | None]:
    """Mirror of ``route.rs::annotate_dup_counts``: expected ``_dup_count``
    per kept row (``None`` = no stamp).

    Family sizes are counted over the WHOLE original array with the identity
    columns projected out; only the FIRST kept member of a family with more
    than one member is stamped, with the family size. Kept rows are hashed
    with any engine-added ``_dup_count`` key removed (the engine computes the
    row's hash BEFORE inserting the key).
    """
    family_size = Counter(stable_item_hash(it, exclude) for it in all_items)
    stamped: set[str] = set()
    out: list[int | None] = []
    for row in kept_rows:
        bare = {k: v for k, v in row.items() if k != "_dup_count"}
        h = stable_item_hash(bare, exclude)
        count = family_size.get(h, 1)
        if count > 1 and h not in stamped:
            stamped.add(h)
            out.append(count)
        else:
            out.append(None)
    return out


def assert_dup_count_corroboration(
    kept_rows: list[dict[str, Any]],
    all_items: list[dict[str, Any]],
    exclude: frozenset[str],
    *,
    dataset: str,
) -> list[int]:
    """Assert the engine's ``_dup_count`` stamps equal the mirror's per-row
    prediction; return the stamped values (may legitimately be empty when no
    kept row represents a >1 family — that too is now asserted, not assumed).
    """
    predicted = predict_dup_stamps(kept_rows, all_items, exclude)
    actual = [row.get("_dup_count") for row in kept_rows]
    assert actual == predicted, (
        f"imp2 corroboration failed on {dataset}: engine _dup_count stamps "
        f"{actual} != mirror prediction {predicted} "
        f"(exclude={sorted(exclude)}) — the mirrored exclude-set/stable-hash/"
        "representative rule has drifted from the engine"
    )
    return [v for v in actual if isinstance(v, int)]


def validate_dup_count_channel() -> None:
    """Prove the corroboration channel FIRES: on a fixture with genuine
    identity-counter duplication the engine must stamp ``_dup_count`` and the
    stamps must match the mirror's prediction (a channel that silently never
    fires — as it does on datasets whose kept rows are all singleton families
    — would make every downstream assertion vacuous).
    """
    fixture: list[dict[str, Any]] = []
    for i in range(120):
        if i < 60:
            content = {"host": f"web-{i % 3}", "msg": "heartbeat ok", "status": "ok"}
        else:
            content = {
                "host": f"web-{i % 3}",
                "msg": f"request r{i} served from shard {i % 7}",
                "status": "ok",
            }
        fixture.append({**content, "seq": i})

    exclude = compute_exclude_set(fixture)
    assert "seq" in exclude, (
        f"exclude-set mirror failed the identity-counter fixture: {sorted(exclude)}"
    )
    kept, _strategy = _crush_kept_rows(fixture, "benchmark imp2 channel validation")
    assert kept, "engine kept no rows on the channel-validation fixture"
    stamps = assert_dup_count_corroboration(kept, fixture, exclude, dataset="channel_validation")
    assert stamps and max(stamps) >= 2, (
        f"_dup_count channel did not fire on a fixture with 20x duplication: stamps={stamps}"
    )


def measure_imp2_ab(dataset: str, items: list[dict[str, Any]]) -> Imp2AB:
    """Compute the field-aware dedup A/B for ``items`` and corroborate it.

    Reports the distinct hash-family count under the whole-item key (Imp2 OFF)
    vs the field-aware stable key (Imp2 ON). The engine's real ``_dup_count``
    stamps on its lossy path are ASSERTED against the mirror's per-kept-row
    prediction (TEST-16e) — after first proving on a synthetic fixture that
    the stamp channel fires at all (``validate_dup_count_channel``), so a
    zero-stamp measurement is a checked fact, not a silent degradation.
    """
    validate_parity()
    validate_dup_count_channel()
    exclude = compute_exclude_set(items)

    families_off = Counter(whole_item_hash(it) for it in items)
    families_on = Counter(stable_item_hash(it, exclude) for it in items)
    n = len(items)
    n_off = len(families_off)
    n_on = len(families_on)
    collapse = (n_off - n_on) / n_off if n_off else 0.0
    largest_on = max(families_on.values(), default=0)

    # Engine corroboration on the lossy path — asserted, then reported.
    kept, strategy = _crush_kept_rows(items, f"benchmark {dataset}")
    stamps = assert_dup_count_corroboration(kept, items, exclude, dataset=dataset)

    return Imp2AB(
        dataset=dataset,
        n_rows=n,
        exclude_set=tuple(sorted(exclude)),
        families_off=n_off,
        families_on=n_on,
        collapse_ratio=collapse,
        largest_family_on=largest_on,
        engine_max_dup_count=max(stamps, default=0),
        engine_strategy=strategy,
    )
