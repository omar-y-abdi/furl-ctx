> ✅ STATUS: refactor + cycle-4 close-out COMPLETE (HEAD 94b714a4). All 7 parts done (C not shipped =
> net-negative). Typo-rejection `9107e749` closed the last critique item. Cycle-5 critique rerun LAUNCHED
> (runId wf_d705ee60-a48) — see handoff.md TOP for the next-session result-processing recipe. This PLAN is
> the historical record of the refactor; the live thread is the rerun.

# PLAN — God-object split + lifecycle excise (user-authorized large refactor, 2026-06-26)

User: "Ta de stora refaktoreringarna nu, använd subagent för DELAR. Sequential with
verifications, near-zero regression. >3% regression → revert that part (discuss tradeoffs
after all done). <3% → keep, but discuss after what caused it." + "Radera 11-stage lifecycle."

## Protocol (per part)
1. ONE subagent (edit-only, NO git) per part — never the whole job at once (context rot).
2. I gate independently: G1-G5 (cargo/pytest/surface/recovery/bench) + circuit-breaker tests.
3. Regression = bench vs the FIXED committed baseline_results.json (NEVER commit a refreshed
   baseline — gate.sh restores from HEAD; cumulative drift measured against original start).
4. HARD revert (overrides 3%): recovery G4 "23 passed" drop OR needle < 100%.
5. Behavior-NEUTRAL parts (A,B,D,E,F): any compression delta = BUG → fix or revert, NOT a tradeoff.
6. Behavior-CHANGING parts (C,G): 3% gate applies; >3% → revert; <3% → keep + log for end discussion.
7. Test moved by an extraction → re-verify it still FAILS on the bug it was written to catch (bite preserved).
8. Commit each kept part (`git add -u <files>`, guard uv.lock|critique). Revert = `git checkout HEAD -- <files>`.
9. Bench determinism confirmed on clean HEAD before trusting any <3% reading.

## Sequence (banked-neutral-first, behavior-changers isolated last — advisor-ranked by BEHAVIOR risk)
- [x] **A** DONE `5bf0a303` (0% delta) — Delete 11-stage lifecycle dead code — `headroom/pipeline.py` (8 dead PipelineStage values:
      SETUP/PRE_START/POST_START/INPUT_CACHED/INPUT_REMEMBERED/PRE_SEND/POST_SEND/RESPONSE_RECEIVED;
      discover_pipeline_extensions + ENTRY_POINT_GROUP + canonical-11 tuple), README:52-72 overstatement.
      KEEP INPUT_RECEIVED/ROUTED/COMPRESSED + emit mechanism compress.py:270/333/346 uses. NEUTRAL.
- [x] **B** DONE `95fd63a6` (0% delta) — Deleted the WHOLE orphaned cache_control.rs module (compute_frozen_count
      was its only pub entry; all helpers private to it; no PyO3 binding, no Rust caller) + tests/cache_control.rs +
      lib.rs re-export + Python docstring cross-refs. NOTE: removed a critique-praised but DEAD proptest.
      [orig: Delete orphaned Rust `compute_frozen_count` — cache_control.rs:108 fn + its inline+file tests +
      lib.rs:15 re-export. No PyO3 binding, no prod caller; Python mirror compress.py:163 is the live path.
      Rust change → pre-warm cargo, rebuild, cargo test. NEUTRAL.
- [x] **D** ALREADY DONE (no-op) — StrategySelector is already extracted: router_policy.py owns the pure fns
      (strategy_from_detection/_type, content_type_from_strategy, adaptive_min_ratio, 115 LOC); ContentRouter's
      _strategy_from_detection*/_content_type_from_strategy/_adaptive_min_ratio/_determine_strategy are thin
      delegators that double as STABLE TEST SEAMS (adaptive_min_ratio: 8 asserts; determine_strategy:243).
      Removing them breaks the seams for zero structural gain → lazy-dev STOP, concern already relieved.
      [orig: move _determine_strategy:842,
      _strategy_from_detection:859, _strategy_from_detection_type:1315, _content_type_from_strategy:1322;
      delete the inline wrappers, keep the re-import shim working). NEUTRAL.
- [x] **E** DONE `f0b7db1d` (0% delta) — StrategyDispatcher extracted to router_dispatch.py (true leaf, no router
      ref; getters/callables injected per-call so monkeypatch bites). ContentRouter 2566→2365. strategy_chain +
      kompress_exception bite preserved (no test touched).
      [orig: Extract StrategyDispatcher (_apply_strategy_to_content:982-1241 + fallback chain 1142-1184).
      Circuit breaker: test_content_router_strategy_chain (must stay green AND keep its bite). NEUTRAL.
- [x] **F** DONE `7b47568d` (0% delta, needle 100%, recovery 23) — CcrMirror extracted to router_ccr_mirror.py
      (true leaf; logger constructor-injected, smart-crusher getter per-call). extract logic byte-identical.
      ContentRouter ~2368→2314. Only decoupling (internal extract no longer via router._extract_ccr_hashes) is
      unobserved — divergence test uses its own helper, verified.
      [orig: Extract CcrMirror (_ensure_ccr_backed:1338 cache-hit path + _extract_ccr_hashes:1402 +
      smart_crusher mirror methods). HARD GATE: recovery 23 + needle 100% (any drop = revert). NEUTRAL.
- [~] **C** ATTEMPTED → NOT A CLEAN WIN (nothing shipped, logged for end discussion). Characterization test
      `f577d47f` KEPT (pins the 50-vs-250 landmine). Why net-negative: reject-unknown must run in apply() (raw
      callers bypass the boundary) AND config-defaults need self.config there, but the boundary already builds
      the request for the min_tokens split → only options are build-twice (not "single"), move-all-to-apply
      (trips the 50→250 landmine), or None-sentinel fields (more verbose than `kwargs.get(k, self.config.x)`).
      Plus kwarg/field name mismatch (protect_recent vs protect_recent_code) + tri-state `is not True` semantics.
      Verified: subagent made zero edits, landmine 3/3 + recovery 23 green.
      [orig: Typed CompressRequest replacing apply()'s 18-key kwargs bag. LANDMINE: bench is BLIND to the
      min_tokens 50-fallback (direct callers, content_router.py:1658 kwargs.get(...,50)) vs 250 default —
      preserve each caller's EFFECTIVE default exactly. Keep thread-local quartet separate (thread_safety
      test pins the mechanism). BEHAVIOR-CHANGING → 3% gate + test updates with preserved bite.
- [x] **G** DONE `142cde0c` (0% delta, needle 100%, recovery 23) — TLS→frozen RouterRuntime value object.
      Deleted self._tls + 4 _runtime_* property pairs + snapshot + replay + import threading (51 grep→0).
      Threaded by argument apply→compress→dispatcher closures→_try_ml/_record_to_toin. 3 mechanism tests
      rewritten, BITE INDEPENDENTLY RE-VERIFIED (I injected a shared-state leak → isolation test went RED
      'foreign target_ratio leaked', then reverted). Aligns with type-driven RULES (no hidden mutable state).
- [x] **fmt** `23084658` — cargo fmt the cycle-3 A-1/A-5 Rust test drift (whitespace; ci-precheck-rust clean).

## OUTCOME (all parts done; batch tradeoff discussion below)
Kept (each 0.000% bench, needle 100%, recovery 23): A B D(no-op) E F G. ContentRouter 2566→2344 (−222);
2 new clean leaf seams (router_dispatch 343, router_ccr_mirror 154); orphaned Rust cache_control module
gone; 8 dead lifecycle stages gone; TLS hidden-state eliminated.
NOT shipped: C (typed-request) = NOT A CLEAN WIN (net-negative, evidence-backed); characterization test
f577d47f kept as the landmine guard. ZERO regressions across the whole refactor (no <3% to weigh — all 0%).
