# FAT GROUND-TRUTH (tools, 2026-06-20) — branch verify/phase2-audit-report @ 9d5a09be
Scope: headroom/ + crates/ (archive/, target/, .venv*, .claude/worktrees/ EXCLUDED). Triage these against 4-vector reachability.

## [VULTURE] dead code (unused funcs/classes/methods/vars/imports/unreachable), min-confidence 70
headroom/ccr/mcp_server.py:905: unused variable 'direct_mode' (100% confidence)
headroom/transforms/_telemetry_noop.py:47: unused variable 'attributes' (100% confidence)

## [RUFF] unused import/var/redef (F401/F811/F841), commented-out code (ERA001), unused noqa (RUF100)
headroom/cache/__init__.py:34:67: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/__init__.py:35:40: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/__init__.py:45:68: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/__init__.py:46:52: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/__init__.py:53:61: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/__init__.py:54:61: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/__init__.py:55:65: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/cache/compression_store.py:1097:13: ERA001 Found commented-out code
headroom/ccr/batch_processor.py:220:13: ERA001 Found commented-out code
headroom/ccr/response_handler.py:347:13: ERA001 Found commented-out code
headroom/pipeline.py:80:31: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
headroom/pipeline.py:87:35: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
headroom/pipeline.py:94:39: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
headroom/pipeline.py:167:39: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
headroom/telemetry/models.py:292:17: ERA001 Found commented-out code
headroom/telemetry/toin.py:659:17: ERA001 Found commented-out code
headroom/telemetry/toin.py:960:41: RUF100 [*] Unused `noqa` directive (non-enabled: `ARG002`)
headroom/telemetry/toin.py:961:44: RUF100 [*] Unused `noqa` directive (non-enabled: `ARG002`)
headroom/tokenizers/tiktoken_counter.py:53:5: ERA001 Found commented-out code
headroom/tokenizers/tiktoken_counter.py:57:5: ERA001 Found commented-out code
headroom/transforms/__init__.py:11:56: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:19:53: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:20:65: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:21:78: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:22:56: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:31:57: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:36:55: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:42:56: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:53:55: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:58:65: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:59:58: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/__init__.py:64:85: RUF100 [*] Unused `noqa` directive (unused: `F401`)
headroom/transforms/_telemetry_noop.py:33:61: RUF100 [*] Unused `noqa` directive (non-enabled: `D401`)
headroom/transforms/_telemetry_noop.py:57:60: RUF100 [*] Unused `noqa` directive (non-enabled: `D401`)
headroom/transforms/content_router.py:1457:55: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
headroom/transforms/cross_message_dedup.py:533:35: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
Found 36 errors.
[*] 29 fixable with the `--fix` option.

## [RUFF stats] by rule across F,ERA,RUF,B,SIM,PLR,C90
154	PLR2004	[ ] magic-value-comparison
 53	C901   	[ ] complex-structure
 36	PLR0912	[ ] too-many-branches
 29	RUF100 	[*] unused-noqa
 24	PLR0913	[ ] too-many-arguments
 24	PLR0915	[ ] too-many-statements
 14	PLR0911	[ ] too-many-return-statements
 14	RUF022 	[-] unsorted-dunder-all
 12	SIM102 	[ ] collapsible-if
  9	SIM108 	[ ] if-else-block-instead-of-if-exp
  8	RUF012 	[ ] mutable-class-default
  7	ERA001 	[ ] commented-out-code
  7	PLR5501	[*] collapsible-else-if
  5	B905   	[ ] zip-without-explicit-strict
  4	PLR1730	[*] if-stmt-min-max
  4	SIM105 	[ ] suppressible-exception
  3	SIM114 	[*] if-with-same-arms
  3	SIM118 	[ ] in-dict-keys
  2	RUF001 	[ ] ambiguous-unicode-character-string
  2	RUF002 	[ ] ambiguous-unicode-character-docstring
  2	RUF005 	[ ] collection-literal-concatenation
  2	SIM103 	[ ] needless-bool
  2	SIM110 	[ ] reimplemented-builtin
  1	PLR0124	[ ] comparison-with-itself
  1	PLR0402	[*] manual-from-import
  1	PLR1711	[*] useless-return
  1	RUF003 	[ ] ambiguous-unicode-character-comment
  1	RUF010 	[*] explicit-f-string-type-conversion
  1	RUF015 	[ ] unnecessary-iterable-allocation-for-first-element
  1	RUF019 	[*] unnecessary-key-check
  1	RUF046 	[ ] unnecessary-cast-to-int
  1	SIM113 	[ ] enumerate-for-loop
  1	SIM116 	[ ] if-else-block-instead-of-dict-lookup
  1	SIM401 	[ ] if-else-block-instead-of-dict-get
Found 431 errors.
[*] 54 fixable with the `--fix` option (31 hidden fixes can be enabled with the `--unsafe-fixes` option).

## [DEPTRY] unused / transitive / misplaced dependencies
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_argcomplete.py[m[36m:[m104[36m:[m16[36m:[m [1m[31mDEP001[m 'argcomplete' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_code/code.py[m[36m:[m34[36m:[m8[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_code/code.py[m[36m:[m51[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_io/terminalwriter.py[m[36m:[m13[36m:[m8[36m:[m [1m[31mDEP003[m 'pygments' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_io/terminalwriter.py[m[36m:[m14[36m:[m1[36m:[m [1m[31mDEP003[m 'pygments' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_io/terminalwriter.py[m[36m:[m15[36m:[m1[36m:[m [1m[31mDEP003[m 'pygments' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_io/terminalwriter.py[m[36m:[m16[36m:[m1[36m:[m [1m[31mDEP003[m 'pygments' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_io/terminalwriter.py[m[36m:[m17[36m:[m1[36m:[m [1m[31mDEP003[m 'pygments' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/_io/terminalwriter.py[m[36m:[m80[36m:[m24[36m:[m [1m[31mDEP001[m 'colorama' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/capture.py[m[36m:[m78[36m:[m20[36m:[m [1m[31mDEP001[m 'colorama' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/compat.py[m[36m:[m24[36m:[m5[36m:[m [1m[31mDEP001[m 'annotationlib' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m42[36m:[m1[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m43[36m:[m1[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m44[36m:[m1[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m45[36m:[m1[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m46[36m:[m1[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m1455[36m:[m13[36m:[m [1m[31mDEP003[m 'packaging' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m1472[36m:[m9[36m:[m [1m[31mDEP003[m 'packaging' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m1473[36m:[m9[36m:[m [1m[31mDEP003[m 'packaging' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/__init__.py[m[36m:[m1474[36m:[m9[36m:[m [1m[31mDEP003[m 'packaging' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/config/findpaths.py[m[36m:[m13[36m:[m8[36m:[m [1m[31mDEP003[m 'iniconfig' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/fixtures.py[m[36m:[m81[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/hookspec.py[m[36m:[m14[36m:[m1[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/legacypath.py[m[36m:[m14[36m:[m1[36m:[m [1m[31mDEP003[m 'iniconfig' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/main.py[m[36m:[m25[36m:[m8[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/monkeypatch.py[m[36m:[m346[36m:[m20[36m:[m [1m[31mDEP001[m 'pkg_resources' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/monkeypatch.py[m[36m:[m347[36m:[m13[36m:[m [1m[31mDEP001[m 'pkg_resources' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/nodes.py[m[36m:[m22[36m:[m8[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/outcomes.py[m[36m:[m279[36m:[m9[36m:[m [1m[31mDEP003[m 'packaging' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/pytester.py[m[36m:[m39[36m:[m1[36m:[m [1m[31mDEP003[m 'iniconfig' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/pytester.py[m[36m:[m40[36m:[m1[36m:[m [1m[31mDEP003[m 'iniconfig' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/raises.py[m[36m:[m64[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/raises.py[m[36m:[m65[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/reports.py[m[36m:[m40[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/runner.py[m[36m:[m41[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/subtests.py[m[36m:[m19[36m:[m8[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/terminal.py[m[36m:[m32[36m:[m8[36m:[m [1m[31mDEP003[m 'pluggy' imported but it is a transitive dependency
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/threadexception.py[m[36m:[m24[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/unittest.py[m[36m:[m44[36m:[m5[36m:[m [1m[31mDEP001[m 'exceptiongroup' imported but missing from the dependency definitions
[1m.claude/worktrees/wf_ad2e78a5-43b-10/.venv-eval/lib/python3.13/site-packages/_pytest/unittest.py[m[36m:[m535[36m:[m9[36m:[m [1m[31mDEP001[m 'twisted' imported but missing from the dependency definitions

## [GREP] markers: TODO/FIXME/XXX/HACK/DEPRECATED (counts by file, top 25)

## [GREP] suspicious filenames (_old/_legacy/_deprecated/_v2/_bak/backup/unused/.orig)
rg: error parsing flag -E: grep config error: unknown encoding: _old|_legacy|_deprecated|_v2|_v3|_bak|backup|unused|\.orig|\.tmp|copy

## [GREP] Rust #[allow(dead_code)] / #[allow(unused)] (masking real dead code)
crates/headroom-core/src/transforms/live_zone.rs:1299:    #[allow(dead_code)]

## [CARGO] dead_code/never-used warnings (headroom-core)

## [GIT] 20 oldest-untouched tracked files (stale-candidate)
2026-01-06 headroom/providers/base.py
2026-01-06 headroom/relevance/base.py
2026-01-06 headroom/tokenizer.py
2026-01-06 tests/__init__.py
2026-01-07 headroom/py.typed
2026-01-10 headroom/cache/anthropic.py
2026-01-10 headroom/cache/compression_feedback.py
2026-01-10 headroom/cache/google.py
2026-01-10 headroom/cache/registry.py
2026-01-10 headroom/relevance/__init__.py
2026-01-10 headroom/relevance/hybrid.py
2026-01-10 headroom/tokenizers/__init__.py
2026-01-10 headroom/tokenizers/estimator.py
2026-01-10 headroom/tokenizers/huggingface.py
2026-01-10 headroom/tokenizers/mistral.py
2026-01-10 headroom/tokenizers/registry.py
2026-01-18 headroom/telemetry/models.py
2026-01-20 headroom/cache/backends/__init__.py
2026-01-20 headroom/cache/backends/base.py
2026-01-20 headroom/cache/backends/memory.py

## [LOC] 25 biggest source files (fat hides in big files)
    3149 crates/headroom-core/src/transforms/smart_crusher/crusher.rs
    2931 headroom/proxy/helpers.py
    2919 headroom/transforms/content_router.py
    2899 crates/headroom-core/src/transforms/live_zone.rs
    2036 headroom/transforms/code_compressor.py
    1942 crates/headroom-core/src/transforms/smart_crusher/compaction/formatter.rs
    1685 crates/headroom-core/src/transforms/diff_compressor.rs
    1683 crates/headroom-core/src/transforms/smart_crusher/compaction/compactor.rs
    1657 crates/headroom-py/src/lib.rs
    1608 headroom/telemetry/toin.py
    1451 crates/headroom-core/src/transforms/log_compressor.rs
    1322 crates/headroom-core/src/transforms/anchor_selector.rs
    1310 headroom/transforms/kompress_compressor.py
    1292 headroom/cache/compression_store.py
    1272 crates/headroom-core/src/transforms/tag_protector.rs
    1262 crates/headroom-core/src/transforms/smart_crusher/analyzer.rs
    1109 headroom/transforms/smart_crusher.py
    1034 headroom/cache/dynamic_detector.py
     975 crates/headroom-core/src/transforms/smart_crusher/planning.rs
     956 headroom/ccr/mcp_server.py
     896 headroom/ccr/response_handler.py
     884 headroom/cache/google.py
     880 headroom/telemetry/models.py
     877 crates/headroom-core/src/transforms/search_compressor.rs
     865 crates/headroom-core/src/transforms/smart_crusher/crushers.rs

## CORRECTIONS + KEY SIGNALS (read these; some tool sections above are scope-polluted)
- IGNORE every `DEP00*` line above that points into `.claude/worktrees/...` — deptry scope bug crawled stale eval venvs. RE-RUN scoped: `.venv/bin/deptry headroom --known-first-party headroom` (Bash-capable lenses do this).
- markers + suspicious-filenames greps FAILED (rg `-E` = encoding flag). RE-RUN: `rg -n -i 'TODO|FIXME|XXX|HACK' headroom crates -g'!archive/**'` and `rg --files | rg -i '_old|_legacy|_deprecated|_bak|backup|unused|\.orig'`.
- HUGE: `.claude/worktrees/` = 19GB, 8 dead `wf_ad2e78a5-*` worktrees (gitignored, from a killed 5-day-old run). `git worktree prune` + rm. Biggest disk-fat in the repo.
- vulture conservative (exported syms look "used"); a Bash lens should re-run `vulture headroom --min-confidence 60` + a whitelist pass.
- ruff: 36 dead-code/unused (incl 7 ERA001 commented-out code, ~22 RUF100 unused-noqa), 431 total lint incl complexity (PLR2004×154 magic-values, C901×53 complex, PLR0912×36 branches, PLR0913×24 args, PLR0915×24 statements, RUF012×8 mutable-default, RUF022×14 unsorted __all__).
- Rust: 1 `#[allow(dead_code)]` masking (live_zone.rs:1299); cargo dead_code check inconclusive (cached) — rust lens should run `cargo check -p headroom-core` fresh and grep warnings.
- Biggest files (fat hides here): crusher.rs 3149, proxy/helpers.py 2931, content_router.py 2919, live_zone.rs 2899, code_compressor.py 2036, telemetry/toin.py 1608, kompress_compressor.py 1310, compression_store.py 1292.
