//! Byte-identity pin harness for the PERF-3/4/5/7/8 refactors.
//!
//! Runs a battery of deterministic fixtures through the public
//! SmartCrusher / LogCompressor / SearchCompressor surfaces and prints
//! `name<TAB>sha256(output)` lines. Capture the output BEFORE a
//! behavior-preserving refactor, re-run AFTER, and `diff` — any drift
//! is a byte-identity violation.
//!
//! Temporary verification tooling — not part of the shipped API.

use std::sync::Arc;

use furl_core::ccr::{CcrStore, InMemoryCcrStore};
use furl_core::transforms::smart_crusher::{SmartCrusher, SmartCrusherBuilder, SmartCrusherConfig};
use furl_core::transforms::{
    LogCompressor, LogCompressorConfig, SearchCompressor, SearchCompressorConfig,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn h(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn pin(name: &str, payload: &str) {
    println!("{name}\t{}", h(payload.as_bytes()));
}

/// Serialize a crush result (all observable fields) for pinning.
fn crush_repr(c: &SmartCrusher, content: &str, query: &str, bias: f64) -> String {
    let r = c.crush(content, query, bias);
    format!(
        "compressed={}\nmodified={}\nstrategy={}\ndropped={:?}",
        r.compressed, r.was_modified, r.strategy, r.dropped
    )
}

/// Store state after a crush: length + every hash-addressed entry the
/// result advertises (proves P0-4 deferred-write behavior unchanged).
fn store_repr(store: &InMemoryCcrStore, hashes: &[String]) -> String {
    let mut out = format!("len={}", store.len());
    for hash in hashes {
        out.push_str(&format!(
            "\n{}={:?}\n{}#rows={:?}",
            hash,
            store.get(hash),
            hash,
            store.get(&format!("{hash}#rows"))
        ));
    }
    out
}

fn crusher_with_store() -> (SmartCrusher, Arc<InMemoryCcrStore>) {
    let store = Arc::new(InMemoryCcrStore::new());
    let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
    let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
        .with_default_oss_setup()
        .with_default_compaction()
        .with_ccr_store(store_dyn)
        .build();
    (c, store)
}

fn lossy_only_with_store() -> (SmartCrusher, Arc<InMemoryCcrStore>) {
    let store = Arc::new(InMemoryCcrStore::new());
    let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
    let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
        .with_default_oss_setup()
        .with_ccr_store(store_dyn)
        .build();
    (c, store)
}

fn main() {
    // ── SmartCrusher fixtures ──────────────────────────────────────────

    // (a) Over-budget dict array: errors + rare status + numeric anomaly
    // + duplicates → exercises the full prioritizer (dedup, fill,
    // error/outlier/anomaly pins, first3/last2, novelty fill).
    let mut rows: Vec<Value> = (0..60)
        .map(|i| {
            json!({
                "id": i,
                "status": if i == 41 { "TIMEOUT" } else { "ok" },
                "latency_ms": if i == 33 { 9500 } else { 40 + (i % 7) },
                "msg": format!("routine event {}", i % 12),
            })
        })
        .collect();
    rows.push(json!({"id": 60, "status": "error", "msg": "FATAL: out of memory"}));
    let doc_a = serde_json::to_string(&Value::Array(rows.clone())).unwrap();

    let (c, store) = crusher_with_store();
    let repr_a = crush_repr(&c, &doc_a, "", 1.0);
    pin("crush.over_budget.noquery", &repr_a);
    let hashes: Vec<String> = c
        .crush(&doc_a, "", 1.0)
        .dropped
        .iter()
        .filter_map(|d| match d {
            furl_core::transforms::smart_crusher::DroppedRef::RowDrop { hash, .. } => {
                Some(hash.clone())
            }
            _ => None,
        })
        .collect();
    pin(
        "crush.over_budget.noquery.store",
        &store_repr(&store, &hashes),
    );

    // (b) Same array with a query context → anchor + relevance pinning.
    let (c, _s) = crusher_with_store();
    pin(
        "crush.over_budget.query",
        &crush_repr(&c, &doc_a, "find the TIMEOUT event id 41", 1.0),
    );

    // (c) Uniform tabular rows → lossless table win.
    let tabular: Vec<Value> = (0..50)
        .map(|i| {
            json!({
                "filesystem": format!("/dev/disk1s{i}"),
                "kilobytes_total": 971350180,
                "kilobytes_used": 543210 + i,
                "capacity_percent": "85%",
                "mounted_on": format!("/Volumes/vol_{i}"),
            })
        })
        .collect();
    let doc_c = serde_json::to_string(&Value::Array(tabular)).unwrap();
    let (c, store) = crusher_with_store();
    pin("crush.lossless_table", &crush_repr(&c, &doc_c, "", 1.0));
    pin("crush.lossless_table.storelen", &format!("{}", store.len()));

    // (d) Near-unique no-signal shape (CCR-backed override → crush).
    let unique: Vec<Value> = (0..30)
        .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
        .collect();
    let doc_d = serde_json::to_string(&Value::Array(unique)).unwrap();
    let (c, _s) = lossy_only_with_store();
    pin(
        "crush.no_signal.ccr_backed",
        &crush_repr(&c, &doc_d, "", 1.0),
    );

    // (d2) Same shape with NO store → skip passthrough.
    let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
        .with_default_oss_setup()
        .build();
    pin("crush.no_signal.skip", &crush_repr(&c, &doc_d, "", 1.0));

    // (e) Mixed array: dict subgroup + strings + numbers.
    let mut mixed: Vec<Value> = (0..25).map(|i| json!({"id": i, "status": "ok"})).collect();
    for i in 0..9 {
        mixed.push(json!(format!("note_{i}")));
    }
    for i in 0..9 {
        mixed.push(json!(i * 7));
    }
    let doc_e = serde_json::to_string(&Value::Array(mixed)).unwrap();
    let (c, store) = crusher_with_store();
    pin("crush.mixed", &crush_repr(&c, &doc_e, "", 1.0));
    pin("crush.mixed.storelen", &format!("{}", store.len()));

    // (f) Nested doc: stringified JSON + opaque blob + wrapper object.
    let inner: Vec<Value> = (0..50)
        .map(|i| json!({"id": i, "level": "info", "msg": "ok"}))
        .collect();
    let doc_f = serde_json::to_string(&json!({
        "payload": serde_json::to_string(&Value::Array(inner)).unwrap(),
        "blob": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8),
        "note": "short",
    }))
    .unwrap();
    let (c, _s) = crusher_with_store();
    pin("crush.nested_string_json", &crush_repr(&c, &doc_f, "", 1.0));

    // (g) Small array passthrough + small lossless zone.
    let small: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
    let doc_g = serde_json::to_string(&Value::Array(small)).unwrap();
    let (c, _s) = crusher_with_store();
    pin("crush.small_passthrough", &crush_repr(&c, &doc_g, "", 1.0));

    let small8: Vec<Value> = (0..8)
        .map(|i| {
            json!({
                "filesystem": format!("/dev/disk1s{i}"),
                "kilobytes_total": 971350180,
                "kilobytes_used": 543210 + i,
                "capacity_percent": "85%",
                "mounted_on": format!("/Volumes/vol_{i}"),
            })
        })
        .collect();
    let doc_g2 = serde_json::to_string(&Value::Array(small8)).unwrap();
    let (c, _s) = crusher_with_store();
    pin("crush.small_lossless", &crush_repr(&c, &doc_g2, "", 1.0));

    // (h) lossless_only strict mode over a lossy-tempting doc.
    let strict_doc = serde_json::to_string(&json!({
        "rows": (0..50).map(|_| json!({"status": "ok"})).collect::<Vec<_>>(),
        "lines": (0..100).map(|i| format!("line-{i}")).collect::<Vec<_>>(),
        "attachment": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(16),
    }))
    .unwrap();
    let store = Arc::new(InMemoryCcrStore::new());
    let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
    let c = SmartCrusherBuilder::new(SmartCrusherConfig {
        lossless_only: true,
        ..SmartCrusherConfig::default()
    })
    .with_default_oss_setup()
    .with_default_compaction()
    .with_ccr_store(store_dyn)
    .build();
    pin("crush.lossless_only", &crush_repr(&c, &strict_doc, "", 1.0));
    pin("crush.lossless_only.storelen", &format!("{}", store.len()));

    // (i) Duplicate-heavy identity-column rows → dedup + _dup_count path.
    let dup_msgs = ["disk full", "auth expired", "cache miss"];
    let dupheavy: Vec<Value> = (0..40)
        .map(|i| {
            let msg = dup_msgs[i % 3];
            json!({
                "ts": format!("2026-06-12T10:00:{:02}Z", i % 60),
                "req_id": format!("{i:040x}"),
                "msg": msg,
                "level": "warn",
            })
        })
        .collect();
    let doc_i = serde_json::to_string(&Value::Array(dupheavy)).unwrap();
    let (c, _s) = crusher_with_store();
    pin("crush.dup_identity", &crush_repr(&c, &doc_i, "", 1.0));

    // ── LogCompressor ──────────────────────────────────────────────────
    let mut log_lines: Vec<String> = Vec::new();
    for i in 0..180 {
        log_lines.push(format!("INFO: Processing item {i}"));
    }
    log_lines.push("ERROR: Failed at item 100".to_string());
    log_lines.push("Traceback (most recent call last):".to_string());
    log_lines.push("  File \"job.py\", line 12, in run".to_string());
    log_lines.push("    do_work()".to_string());
    log_lines.push("ValueError: bad input".to_string());
    for i in 0..12 {
        log_lines.push(format!(
            "WARNING: disk usage at {}% on /dev/sda1",
            80 + (i % 3)
        ));
    }
    log_lines.push("=== 3 passed, 1 failed in 2.31s ===".to_string());
    let log_content = log_lines.join("\n");

    let store = InMemoryCcrStore::new();
    let lc = LogCompressor::new(LogCompressorConfig::default());
    let (lr, ls) = lc.compress_with_store(&log_content, 1.0, Some(&store));
    pin(
        "log.default",
        &format!(
            "compressed={}\ncache_key={:?}\ncounts={}/{}\nratio={}\nstats={:?}\nsidecar={:?}",
            lr.compressed,
            lr.cache_key,
            lr.original_line_count,
            lr.compressed_line_count,
            lr.compression_ratio,
            lr.stats,
            (
                ls.stack_traces_seen,
                ls.stack_traces_kept,
                ls.warnings_dropped_by_dedupe,
                ls.ccr_emitted,
                ls.ccr_skip_reason
            )
        ),
    );
    pin("log.default.storelen", &format!("{}", store.len()));

    // ── SearchCompressor ───────────────────────────────────────────────
    let mut search_lines: Vec<String> = Vec::new();
    for f in 0..12 {
        for l in 0..25 {
            search_lines.push(format!(
                "src/module_{f}.py:{}:def handler_{l}(request): # error path {}",
                l * 7 + 3,
                (f + l) % 5
            ));
        }
    }
    let search_content = search_lines.join("\n");
    let store = InMemoryCcrStore::new();
    let sc = SearchCompressor::new(SearchCompressorConfig::default());
    let (sr, ss) = sc.compress_with_store(&search_content, "handler error", 1.0, Some(&store));
    pin(
        "search.default",
        &format!(
            "compressed={}\ncache_key={:?}\ncounts={}/{}\nfiles={}\nratio={}\nsummaries={:?}\nsidecar={:?}",
            sr.compressed,
            sr.cache_key,
            sr.original_match_count,
            sr.compressed_match_count,
            sr.files_affected,
            sr.compression_ratio,
            sr.summaries,
            (
                ss.lines_unparsed,
                ss.files_dropped,
                ss.matches_dropped_by_dedup,
                ss.matches_dropped_by_per_file_cap,
                ss.matches_dropped_by_global_cap,
                ss.ccr_emitted,
                ss.ccr_skip_reason
            )
        ),
    );
    pin("search.default.storelen", &format!("{}", store.len()));
}
