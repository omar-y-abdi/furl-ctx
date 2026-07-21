"""Corpus builders and ground-truth computers for agent_utility_eval.py.

Each corpus builder returns: (corpus_str, corpus_meta, source_label).
Each GT function returns: dict with keys "anomaly", "locality", "aggregate",
each being a dict with "description" and the deterministic answers.

All corpora are deterministic: generated ones use a fixed seed (SEED=42),
real ones are read from fixed paths and produce consistent GT by parsing.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACE_PATH = Path("/Users/k/Downloads/Trace-20260602T162358.json")
SOURCE_FILE_PATH = REPO_ROOT / "furl_ctx" / "compress.py"
SEED = 42


# ---------------------------------------------------------------------------
# Corpus 1: chrome_trace_full
# ---------------------------------------------------------------------------


def build_chrome_trace_full() -> tuple[str, dict, str]:
    """Full 33MB Chrome DevTools trace (offload path — above 8MB size-guard)."""
    if not TRACE_PATH.exists():
        raise FileNotFoundError(f"Chrome trace not found: {TRACE_PATH}")
    corpus_str = TRACE_PATH.read_text(encoding="utf-8")
    return corpus_str, {"path": str(TRACE_PATH)}, f"path:{TRACE_PATH}"


def gt_chrome_trace_full(corpus_str: str, _meta: dict) -> dict:
    """GT computed from full 140k-event trace.

    anomaly:   all DroppedFrame event timestamps.
    locality:  5 events around the MIDDLE DroppedFrame (not head/tail).
    aggregate: per-name event histogram top-10 over all events.
    """
    data = json.loads(corpus_str)
    events = data["traceEvents"]

    dropped = [e for e in events if e.get("name") == "DroppedFrame"]
    dropped_ts = sorted(e["ts"] for e in dropped if "ts" in e)

    mid_idx = len(dropped) // 2
    mid_event = dropped[mid_idx]
    mid_pos = next(i for i, e in enumerate(events) if e is mid_event)
    ws, we = max(0, mid_pos - 2), min(len(events), mid_pos + 3)
    locality_ts = [e.get("ts") for e in events[ws:we] if "ts" in e]

    name_counts: dict[str, int] = {}
    for e in events:
        n = e.get("name", "")
        name_counts[n] = name_counts.get(n, 0) + 1
    top10 = sorted(name_counts.items(), key=lambda x: -x[1])[:10]

    return {
        "anomaly": {
            "description": "All DroppedFrame event timestamps (full corpus)",
            "count": len(dropped_ts),
            "sample_ts": dropped_ts[:5],
            "total_dropped_frames": len(dropped),
        },
        "locality": {
            "description": f"5 events around middle DroppedFrame (index {mid_pos} of {len(events)})",
            "anchor_ts": mid_event.get("ts"),
            "anchor_name": mid_event.get("name"),
            "window_ts": locality_ts,
        },
        "aggregate": {
            "description": "Per-name event histogram top-10 over all 140k events",
            "total_events": len(events),
            "top10_names": [{"name": n, "count": c} for n, c in top10],
        },
    }


# ---------------------------------------------------------------------------
# Corpus 3: app_log
# ---------------------------------------------------------------------------


def build_app_log(seed: int = SEED) -> tuple[str, dict, str]:
    """~5000-line realistic app log. ERROR/WARN anomalies at FIXED MIDDLE positions."""
    rng = random.Random(seed)
    services = [
        "auth-svc",
        "api-gateway",
        "db-pool",
        "cache-svc",
        "worker",
        "scheduler",
        "notifier",
        "metrics-svc",
    ]
    info_msgs = [
        "Request processed successfully",
        "Cache hit for key {key}",
        "DB query completed in {ms}ms",
        "Heartbeat ok",
        "Token refreshed for user {uid}",
        "Batch job started",
        "Connection pool: {n}/{max} active",
        "Config loaded from environment",
        "Health check passed",
        "Rate limit check: {req}/min",
    ]
    debug_msgs = [
        "Entering handler {handler}",
        "SQL: SELECT * FROM {table} WHERE id={id}",
        "Cache lookup: {key}",
        "Retry attempt {n} of 3",
        "Payload size: {sz} bytes",
        "Span {span_id} started",
        "GC pause: {ms}ms",
    ]
    error_templates = [
        ("ERROR", "Connection refused to {host}:5432 after {n} retries"),
        ("ERROR", "Unhandled exception in {svc}: NullPointerError at line {line}"),
        ("ERROR", "Queue overflow: dropped {n} messages from {svc}"),
        ("ERROR", "Authentication failed for user {uid}: invalid token"),
        ("WARN", "Slow query detected: {ms}ms for {table}"),
        ("WARN", "Memory usage high: {pct}% of limit"),
        ("WARN", "Deprecated endpoint /v1/{ep} called by {host}"),
        ("WARN", "Circuit breaker OPEN for {svc}"),
        ("ERROR", "Disk I/O error on /var/data/{file}: {errno}"),
        ("ERROR", "SSL certificate expiry in {days} days for {host}"),
        ("WARN", "Rate limit exceeded for client {uid}: {req}/min"),
        ("ERROR", "Failed to flush write-ahead log: timeout after {ms}ms"),
    ]
    hosts = ["db-01.prod", "db-02.prod", "cache-01.prod", "svc-mesh.internal"]
    tables = ["users", "events", "sessions", "audit_log"]

    # Anomalies at MIDDLE positions (1500–3500), deterministic
    anomaly_positions = sorted(set(rng.sample(range(1500, 3500), 10)) | {2001, 2750})
    anomaly_entries: dict[int, tuple[str, str]] = {}
    for i, pos in enumerate(anomaly_positions):
        level, tmpl = error_templates[i % len(error_templates)]
        fmt_args = {
            "host": rng.choice(hosts),
            "n": rng.randint(3, 10),
            "svc": rng.choice(services),
            "line": rng.randint(100, 999),
            "uid": f"u{rng.randint(1000, 9999)}",
            "ms": rng.randint(5000, 30000),
            "table": rng.choice(tables),
            "pct": rng.randint(85, 99),
            "ep": rng.choice(["search", "export", "bulk"]),
            "file": f"wal_{rng.randint(1, 99):04d}.log",
            "errno": rng.choice(["EIO", "ENOSPC", "EACCES"]),
            "days": rng.randint(1, 14),
            "req": rng.randint(1000, 5000),
            "key": f"k:{rng.randint(1, 999)}",
            "sz": rng.randint(100, 9999),
            "span_id": f"{rng.randint(1000, 9999):x}",
            "max": 50,
            "id": rng.randint(1, 99),
            "handler": f"handle_{rng.choice(['get', 'post', 'delete'])}",
        }
        anomaly_entries[pos] = (level, tmpl.format(**fmt_args))

    base_ts = 0
    lines = []
    for i in range(5000):
        base_ts += rng.randint(10, 500)
        ms = base_ts % 1000
        ts = f"2026-06-01T{(i // 3600):02d}:{((i % 3600) // 60):02d}:{(i % 60):02d}.{ms:03d}Z"
        svc = rng.choice(services)
        if i in anomaly_entries:
            level, msg = anomaly_entries[i]
        else:
            level = rng.choices(["INFO", "DEBUG"], weights=[60, 40])[0]
            tmpl = rng.choice(info_msgs if level == "INFO" else debug_msgs)
            msg = tmpl.format(
                key=f"k:{rng.randint(1, 999)}",
                ms=rng.randint(1, 200),
                uid=f"u{rng.randint(1000, 9999)}",
                n=rng.randint(1, 3),
                sz=rng.randint(100, 9999),
                span_id=f"{rng.randint(1000, 9999):x}",
                max=50,
                id=rng.randint(1, 99),
                req=rng.randint(10, 500),
                handler=f"handle_{rng.choice(['get', 'post', 'delete'])}",
                table=rng.choice(tables),
            )
        lines.append(f"{ts} [{level:5s}] {svc}: {msg}")

    meta = {
        "total_lines": len(lines),
        "anomaly_positions": anomaly_positions,
        "anomaly_entries": {str(k): list(v) for k, v in anomaly_entries.items()},
    }
    return "\n".join(lines), meta, "generated:app_log"


def gt_app_log(corpus_str: str, corpus_meta: dict) -> dict:
    """GT: anomaly = ERROR/WARN lines at planted positions; locality = window around line 2001."""
    lines = corpus_str.splitlines()
    anomaly_positions = [int(p) for p in corpus_meta["anomaly_positions"]]
    anomaly_lines = [
        {"line_number": p, "content": lines[p]} for p in anomaly_positions if p < len(lines)
    ]

    anchor = 2001
    ws, we = max(0, anchor - 2), min(len(lines), anchor + 3)
    locality_window = [{"line_number": i, "content": lines[i]} for i in range(ws, we)]

    level_re = re.compile(r"\[(\w+)\s*\]")
    level_counts: dict[str, int] = {}
    for line in lines:
        m = level_re.search(line)
        if m:
            lvl = m.group(1)
            level_counts[lvl] = level_counts.get(lvl, 0) + 1

    return {
        "anomaly": {
            "description": "ERROR/WARN lines at planted anomaly positions (middle of log)",
            "count": len(anomaly_lines),
            "sample": anomaly_lines[:5],
            "all_positions": anomaly_positions,
        },
        "locality": {
            "description": f"5 lines around line {anchor} (known middle anomaly)",
            "anchor_line": anchor,
            "window": locality_window,
        },
        "aggregate": {
            "description": "Per-log-level counts over all 5000 lines",
            "total_lines": len(lines),
            "level_counts": level_counts,
        },
    }


# ---------------------------------------------------------------------------
# Corpus 4: source_file
# ---------------------------------------------------------------------------


def build_source_file() -> tuple[str, dict, str]:
    """Real furl source file: furl_ctx/compress.py."""
    corpus_str = SOURCE_FILE_PATH.read_text(encoding="utf-8")
    return corpus_str, {"path": str(SOURCE_FILE_PATH)}, f"path:{SOURCE_FILE_PATH}"


def gt_source_file(corpus_str: str, _meta: dict) -> dict:
    """GT: anomaly = class defs; locality = middle def; aggregate = def+class counts."""
    lines = corpus_str.splitlines()
    def_re = re.compile(r"^\s*(class|def)\s+(\w+)")
    defs = [
        (i, m.group(1), m.group(2)) for i, ln in enumerate(lines) for m in [def_re.match(ln)] if m
    ]
    classes = [{"line": i, "kind": k, "name": n} for i, k, n in defs if k == "class"]

    mid = len(defs) // 2
    anchor_line = defs[mid][0] if defs else len(lines) // 2
    ws, we = max(0, anchor_line - 2), min(len(lines), anchor_line + 3)
    locality_window = [{"line_number": i, "content": lines[i]} for i in range(ws, we)]

    func_count = sum(1 for _, k, _ in defs if k == "def")
    class_count = len(classes)
    return {
        "anomaly": {
            "description": "Class definitions in the source file",
            "count": class_count,
            "classes": classes,
        },
        "locality": {
            "description": f"5 lines around middle def/class (line {anchor_line})",
            "anchor_line": anchor_line,
            "anchor_name": defs[mid][2] if defs else "",
            "window": locality_window,
        },
        "aggregate": {
            "description": "Count of def and class definitions in the file",
            "total_lines": len(lines),
            "function_defs": func_count,
            "class_defs": class_count,
            "total_defs": func_count + class_count,
        },
    }


# ---------------------------------------------------------------------------
# Corpus 5: json_api
# ---------------------------------------------------------------------------


def build_json_api(seed: int = SEED) -> tuple[str, dict, str]:
    """Paginated API dump: 500 records. Rare combo: suspended+enterprise (5 planted)."""
    rng = random.Random(seed + 1)
    statuses = ["active", "inactive", "pending", "suspended"]
    plans = ["free", "starter", "pro", "enterprise"]
    regions = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]
    anomaly_ids = sorted(rng.sample(range(500), 5))

    records = []
    for i in range(500):
        if i in anomaly_ids:
            status, plan = "suspended", "enterprise"
        else:
            status = rng.choices(statuses[:3], weights=[70, 20, 10])[0]
            plan = rng.choices(plans[:3], weights=[50, 30, 20])[0]
        records.append(
            {
                "id": i + 1,
                "org": f"org-{rng.randint(1000, 9999)}",
                "plan": plan,
                "status": status,
                "region": rng.choice(regions),
                "seats": rng.randint(1, 500),
                "mrr_usd": round(rng.uniform(0, 50000), 2),
                "created_at": f"202{rng.randint(2, 5)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            }
        )

    plan_dist = {p: sum(1 for r in records if r["plan"] == p) for p in plans}
    status_dist = {s: sum(1 for r in records if r["status"] == s) for s in statuses}
    meta = {
        "total_records": 500,
        "anomaly_ids": anomaly_ids,
        "plan_distribution": plan_dist,
        "status_distribution": status_dist,
    }
    payload = {"data": records, "meta": {"total": 500, "page": 1, "per_page": 500, "pages": 1}}
    return json.dumps(payload), meta, "generated:json_api"


def gt_json_api(corpus_str: str, corpus_meta: dict) -> dict:
    """GT: anomaly = 5 suspended+enterprise records; locality = neighbors; agg = distributions."""
    data = json.loads(corpus_str)
    records = data["data"]
    anomaly_ids = corpus_meta["anomaly_ids"]
    anomaly_records = [
        r for r in records if r["status"] == "suspended" and r["plan"] == "enterprise"
    ]

    mid_anomaly_id = anomaly_ids[len(anomaly_ids) // 2]
    mid_pos = next((i for i, r in enumerate(records) if r["id"] == mid_anomaly_id + 1), 250)
    ws, we = max(0, mid_pos - 1), min(len(records), mid_pos + 2)

    return {
        "anomaly": {
            "description": "Records with status=suspended AND plan=enterprise (5 planted, rare combo)",
            "count": len(anomaly_records),
            "records": anomaly_records,
        },
        "locality": {
            "description": f"3 records around middle anomaly (id {mid_anomaly_id + 1})",
            "anchor_id": mid_anomaly_id + 1,
            "window": records[ws:we],
        },
        "aggregate": {
            "description": "Plan and status distribution over all 500 records",
            "total_records": len(records),
            "plan_distribution": corpus_meta["plan_distribution"],
            "status_distribution": corpus_meta["status_distribution"],
        },
    }


# ---------------------------------------------------------------------------
# Corpus 6: stacktrace
# ---------------------------------------------------------------------------


def build_stacktrace(seed: int = SEED) -> tuple[str, dict, str]:
    """Realistic deep multi-frame Python traceback with chained exception."""
    rng = random.Random(seed + 2)
    frames = [
        ("django/core/handlers/exception.py", 55, "inner", "response = get_response(request)"),
        (
            "django/core/handlers/base.py",
            197,
            "_get_response",
            "response = wrapped_callback(request, *callback_args, **callback_kwargs)",
        ),
        (
            "myapp/views/api.py",
            142,
            "dispatch",
            "return super().dispatch(request, *args, **kwargs)",
        ),
        (
            "rest_framework/views.py",
            509,
            "dispatch",
            "response = handler(request, *args, **kwargs)",
        ),
        ("myapp/views/api.py", 218, "post", "result = self.process_request(request.data)"),
        ("myapp/services/processor.py", 87, "process_request", "return self._run_pipeline(data)"),
        ("myapp/services/processor.py", 134, "_run_pipeline", "step_result = step.execute(ctx)"),
        ("myapp/pipeline/steps/validate.py", 56, "execute", "schema.validate(ctx.payload)"),
        ("myapp/pipeline/steps/validate.py", 89, "validate", "self._check_nested(value, path)"),
        (
            "myapp/pipeline/steps/validate.py",
            112,
            "_check_nested",
            "raise ValidationError(f'Invalid field at {path!r}: {value!r}')",
        ),
    ]
    chained_frames = [
        ("django/core/handlers/exception.py", 55, "inner", "response = get_response(request)"),
        (
            "myapp/middleware/error_handler.py",
            34,
            "process_exception",
            "return self.format_error(exc)",
        ),
        (
            "myapp/middleware/error_handler.py",
            67,
            "format_error",
            "body = json.dumps(self.serialize(exc))",
        ),
        (
            "myapp/middleware/error_handler.py",
            91,
            "serialize",
            "detail = exc.detail if hasattr(exc, 'detail') else str(exc)",
        ),
        (
            "myapp/middleware/error_handler.py",
            95,
            "serialize",
            "raise SerializationError('Cannot serialize exception detail') from exc",
        ),
    ]

    lines = ["Traceback (most recent call last):"]
    for path, lineno, func, code in frames:
        lines.append(f'  File "{path}", line {lineno + rng.randint(-2, 5)}, in {func}')
        lines.append(f"    {code}")

    field_path = f"data.items[{rng.randint(0, 49)}].attributes.config.rules[{rng.randint(0, 9)}]"
    bad_val = rng.choice(["null", '"undefined"', "[]", "-1", '""'])
    lines.append(f"myapp.exceptions.ValidationError: Invalid field at {field_path!r}: {bad_val}")
    lines.extend(
        [
            "",
            "During handling of the above exception, another exception occurred:",
            "",
            "Traceback (most recent call last):",
        ]
    )
    for path, lineno, func, code in chained_frames:
        lines.append(f'  File "{path}", line {lineno + rng.randint(-1, 3)}, in {func}')
        lines.append(f"    {code}")
    lines.append("myapp.exceptions.SerializationError: Cannot serialize exception detail")

    meta = {
        "frame_count": len(frames) + len(chained_frames),
        "root_exception": "myapp.exceptions.ValidationError",
        "chained_exception": "myapp.exceptions.SerializationError",
        "deepest_frame": "myapp/pipeline/steps/validate.py",
        "field_path": field_path,
    }
    return "\n".join(lines), meta, "generated:stacktrace"


def gt_stacktrace(corpus_str: str, corpus_meta: dict) -> dict:
    """GT: anomaly = exception type lines; locality = middle frame; agg = frame count."""
    lines = corpus_str.splitlines()
    frame_re = re.compile(r'^\s+File ".+", line \d+, in \w+')
    frame_indices = [i for i, ln in enumerate(lines) if frame_re.match(ln)]
    exc_lines = [(i, ln) for i, ln in enumerate(lines) if ln.startswith("myapp.exceptions.")]

    mid_frame_line = frame_indices[len(frame_indices) // 2] if frame_indices else len(lines) // 2
    ws, we = max(0, mid_frame_line - 1), min(len(lines), mid_frame_line + 2)
    locality_window = [{"line_number": i, "content": lines[i]} for i in range(ws, we)]

    return {
        "anomaly": {
            "description": "Exception type lines in the traceback (root + chained)",
            "exceptions": [{"line_number": i, "content": ln} for i, ln in exc_lines],
            "root_exception": corpus_meta["root_exception"],
            "chained_exception": corpus_meta["chained_exception"],
        },
        "locality": {
            "description": f"3 lines around middle stack frame (line {mid_frame_line})",
            "anchor_line": mid_frame_line,
            "window": locality_window,
        },
        "aggregate": {
            "description": "Total frame count across both exception chains",
            "total_lines": len(lines),
            "frame_count": len(frame_indices),
            "expected_frames": corpus_meta["frame_count"],
        },
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CORPUS_BUILDERS = [
    ("chrome_trace_full", build_chrome_trace_full, gt_chrome_trace_full),
    ("app_log", build_app_log, gt_app_log),
    ("source_file", build_source_file, gt_source_file),
    ("json_api", build_json_api, gt_json_api),
    ("stacktrace", build_stacktrace, gt_stacktrace),
]
