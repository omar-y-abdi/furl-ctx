# Changelog

All notable changes to Furl will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2](https://github.com/omar-y-abdi/furl/compare/v1.0.1...v1.0.2) (2026-07-11)


### Bug Fixes

* **core:** search preview lines byte-exact — verbatim line tokens and separators ([c1fe4bb](https://github.com/omar-y-abdi/furl/commit/c1fe4bb7e65a1cd22fd4292dc9e880df19a973bd))
* honest headline everywhere, TTL visibility, furl_read + secrets docs, agent-first MCP legend ([19a53bb](https://github.com/omar-y-abdi/furl/commit/19a53bb542aa40e062b64b66f5f970a6e5d383f7))
* **plugin:** hook extracts real Bash/WebFetch/Agent payload shapes — was silent no-op live ([829f4a4](https://github.com/omar-y-abdi/furl/commit/829f4a4d46ca4c0d434d894f37d8392c9b70bd12))

## [1.0.1](https://github.com/omar-y-abdi/furl/compare/v1.0.0...v1.0.1) (2026-07-11)


### Bug Fixes

* **ci:** required checks always conclude — unblock config/release-only PRs ([9a164e1](https://github.com/omar-y-abdi/furl/commit/9a164e1c16e8b936114ee545324e2c807f499b08))
* first-contact polish — honest README numbers, clone guard, community link, drop release-as ([be3b239](https://github.com/omar-y-abdi/furl/commit/be3b239c1f5228df7ae48c79855ec1c668fc68d4))
* **plugin:** hooks.json schema — wrap events under hooks record so plugin hooks load ([ceb8f91](https://github.com/omar-y-abdi/furl/commit/ceb8f9179a6eb8864672a34ff9c253c44b08d849))

## [1.0.0](https://github.com/omar-y-abdi/furl/compare/v0.27.0...v1.0.0) (2026-07-10)


### Features

* CCR spill tier — retention past eviction (Q10, default off) ([#30](https://github.com/omar-y-abdi/furl/issues/30)) ([e29dcc4](https://github.com/omar-y-abdi/furl/commit/e29dcc43dc3c38be775caccc0fe3b80ca4470ad2))
* **ccr:** sliceable retrieve — row-select by value/range for offloaded arrays ([84f0775](https://github.com/omar-y-abdi/furl/commit/84f077546cdf51feb4a62519d4b817f12cd26a85))
* **cli:** furl retrieve slice flags matching the sliceable library retrieve ([6da8796](https://github.com/omar-y-abdi/furl/commit/6da879624218e6042ab18ddfa98e23ccebb07d0e))
* Furl Claude Code plugin — MCP + compression hook + skill (C7-P7) ([#29](https://github.com/omar-y-abdi/furl/issues/29)) ([866d8ee](https://github.com/omar-y-abdi/furl/commit/866d8ee206f918f57ac8f22b6a9e99620cbd918d))
* harness big-bets, perf, and signal-aware CCR offload preview ([4c4e22e](https://github.com/omar-y-abdi/furl/commit/4c4e22e8ad2d9732726830edb5c118594a85f273))
* harness expansion — quick wins (Q1-Q8) ([#36](https://github.com/omar-y-abdi/furl/issues/36)) ([179380f](https://github.com/omar-y-abdi/furl/commit/179380ffbd165725341ed16cdd5c6a7137bfb00e))
* **plugin:** make Furl installable as a Claude Code plugin end-to-end ([#32](https://github.com/omar-y-abdi/furl/issues/32)) ([5d06a6d](https://github.com/omar-y-abdi/furl/commit/5d06a6da0aaff9fa17e2893f1e584987790666e5))
* **plugin:** zero-step install — 2 slash commands, uv self-bootstrap, lean README ([#33](https://github.com/omar-y-abdi/furl/issues/33)) ([baa988c](https://github.com/omar-y-abdi/furl/commit/baa988ce762bc4b22acc6fa4e7e9442c7336ed51))
* **router:** HTML main-content extraction (B1) ([#37](https://github.com/omar-y-abdi/furl/issues/37)) ([8759e32](https://github.com/omar-y-abdi/furl/commit/8759e326ed0eecd8d5e2783a67c910d2b214b728))
* **security:** B3 core — fail-closed redactor + purge ([fefd4ab](https://github.com/omar-y-abdi/furl/commit/fefd4ab1451e782dd9ceaf63cf2a279a90bdbec2))


### Bug Fixes

* **ccr:** close MATRIX-01/02/03 — bracket-pointer re-backing, ccr_hashes totality, deterministic loud decline ([#52](https://github.com/omar-y-abdi/furl/issues/52)) ([58a0aa9](https://github.com/omar-y-abdi/furl/commit/58a0aa96e7b471abff50f270efa2b2ae731ffd21))
* **store:** close audit ship-blockers — ReDoS, silent-loss durability veto, collision phantom, write-path perf ([#54](https://github.com/omar-y-abdi/furl/issues/54)) ([071e675](https://github.com/omar-y-abdi/furl/commit/071e675f98e136c2d3401c92cfb2c628691eed50))
* **store:** per-project isolation by default — close cross-project content leak (audit [#4](https://github.com/omar-y-abdi/furl/issues/4)) ([#55](https://github.com/omar-y-abdi/furl/issues/55)) ([7ef6ac6](https://github.com/omar-y-abdi/furl/commit/7ef6ac6e9dcb5ea1bf11c1448d5b627149562556))


### Dependencies

* bump criterion from 0.5.1 to 0.8.2 ([#12](https://github.com/omar-y-abdi/furl/issues/12)) ([bfe8dc5](https://github.com/omar-y-abdi/furl/commit/bfe8dc574190b4cfa1852f2cf0c1e0f90f1951a1))


### Code Refactoring

* extract ContentRouter.apply() into router_engine (byte-identical) ([#31](https://github.com/omar-y-abdi/furl/issues/31)) ([8f04f73](https://github.com/omar-y-abdi/furl/commit/8f04f73dffe3fd3d8727a67fd5d4add29f27e801))
* repo-wide dead-code excision and LoC reduction (multi-agent audit) ([#46](https://github.com/omar-y-abdi/furl/issues/46)) ([7d3976f](https://github.com/omar-y-abdi/furl/commit/7d3976f05a5ed40b4483674399340f99c1262a10))

## [0.27.0](https://github.com/omar-y-abdi/furl/compare/v0.26.0...v0.27.0) (2026-07-03)


### Features

* **bench:** raw-text datasets for all four text compressors + honest needle control arm (EFF-6/EFF-7, TEST-16) ([797e413](https://github.com/omar-y-abdi/furl/commit/797e4133a2e0efc8cd0a9661f221a1d942909302))
* **bench:** real multi-turn dataset — repeated tool I/O across turns ([06ea5f3](https://github.com/omar-y-abdi/furl/commit/06ea5f3db0931df637d30e59424cc92f5fe4a288))
* **ccr:** durable SQLite CCR store as MCP default + store/stats honesty (engine P1-7, COR-36/37) ([e61d21c](https://github.com/omar-y-abdi/furl/commit/e61d21c74b8c66c85673e0d23d39d81fa191a0cb))
* **ccr:** granular per-row offload for proportional retrieval ([bd1e790](https://github.com/omar-y-abdi/furl/commit/bd1e790e66b1e44a502235f0e22ce920cd65c2f1))
* **code:** restore CodeAwareCompressor as opt-in AST-verified code compression (engine P2-12) ([f8fff18](https://github.com/omar-y-abdi/furl/commit/f8fff18470aa2ef13ea4db8583158d01df2a62a2))
* **compaction:** arithmetic-progression fold for integer columns ([3ce06ce](https://github.com/omar-y-abdi/furl/commit/3ce06cecf72bfe5ce4dda60fd5d4f2202f4c1f04))
* **compaction:** constant-column fold in CSV-schema lossless rendering ([af8e1d5](https://github.com/omar-y-abdi/furl/commit/af8e1d5142c82bab62eaac9c07cebdb2bfb732b1))
* **compaction:** cross-row affix fold for near-unique string columns ([fd48b41](https://github.com/omar-y-abdi/furl/commit/fd48b4193a26312236e090d359fce18be27761de))
* **compaction:** decimal scale-fold for plain-decimal float columns ([1317e7c](https://github.com/omar-y-abdi/furl/commit/1317e7c380921c519faae0c56e87b7ae9b1074fc))
* **compaction:** dictionary encoding for low-cardinality string columns ([ce6c670](https://github.com/omar-y-abdi/furl/commit/ce6c6706c3e3da19932623ad24ef1be9730727fd))
* **compaction:** ditto marks for consecutive repeated cells in CSV-schema ([7ba33f6](https://github.com/omar-y-abdi/furl/commit/7ba33f6e1c3e428dd6c5ae65283a4d066202cb95))
* **compaction:** head-dictionary fold for grouped path/key columns ([51cc399](https://github.com/omar-y-abdi/furl/commit/51cc399cd29768847825b89a58335eb723af967c))
* **compaction:** ISO-8601 timestamp delta encoding (proven round-trip) ([f8331eb](https://github.com/omar-y-abdi/furl/commit/f8331eb76d31d96b8e1bd0fe87362222dbc0e3d9))
* **config:** lossless_only strict mode + delete dead factor_out_constants (engine P1-8) ([1facdd5](https://github.com/omar-y-abdi/furl/commit/1facdd5ea37ceb8565dff356d0d9987366c538dc))
* **core:** small arrays emit a CCR-backed lossy candidate (EFF-3); lossless-gate 0.15 experiment measured-neutral, reverted (EFF-4) ([bb93195](https://github.com/omar-y-abdi/furl/commit/bb931959be9e0f6ea16d4b4b05eeee8d63070236))
* **crusher:** 1A unconditional CCR persist — kill silent loss ([7c27ab2](https://github.com/omar-y-abdi/furl/commit/7c27ab21cfc13c7a8a62708fc23d40993d9af13b))
* **crusher:** 1B novelty-ranked fill + field-value singleton pinning ([2cb7d10](https://github.com/omar-y-abdi/furl/commit/2cb7d1014fb9bf7fb919a1b6c43e404f6dd905b9))
* **crusher:** CCR-backed aggressive lossy budget + query-relevant pinning ([e9630de](https://github.com/omar-y-abdi/furl/commit/e9630de85c976ca5a226daee3dcdca3066adf447))
* **crusher:** degeneracy gate on singleton pinning — majority-singleton arrays skip positional pins ([904cb83](https://github.com/omar-y-abdi/furl/commit/904cb837e647622e7ce699319e32422bcb64ca9e))
* **crusher:** entropy-floor crushability override — deterministic aggressive drop on near-unique data ([a8d49a2](https://github.com/omar-y-abdi/furl/commit/a8d49a276e123fd104fc3685dfe2105c1670f088))
* **crusher:** Imp2 field-aware stable-projection hash for dedup/cluster/fill ([9b0ee92](https://github.com/omar-y-abdi/furl/commit/9b0ee92b72a31b9f6d91eeaa3f43746d4e2ad39a))
* **crusher:** route-by-min-tokens — ship the fewer-token recoverable render ([3718707](https://github.com/omar-y-abdi/furl/commit/37187071e2ee2faa56275e034a64bec6a834ba38))
* **crusher:** small-array lossless attempt inside the tier-1 passthrough zone ([5b1ad2b](https://github.com/omar-y-abdi/furl/commit/5b1ad2b4dc6920a03135d5aacca515a632090c77))
* **crusher:** survivor compaction — lossy keep-set ships as CSV-schema table + sentinel line ([f820028](https://github.com/omar-y-abdi/furl/commit/f820028f7d8a3106c4719146d3ec6f6d8e461de3))
* CSV decode legend via MCP server-level instructions ([0bd06dd](https://github.com/omar-y-abdi/furl/commit/0bd06ddbdf441ba71c9b9a4abc547328668f2386))
* **decode:** reconstruction-aware reference decoder + decode-and-compare retention ([e74eb9a](https://github.com/omar-y-abdi/furl/commit/e74eb9ae6c26cfc7de50154a86e7289147461c2c))
* **dedup:** cross-message exact dedup — later duplicate tool outputs become recoverable pointers ([fb1fa01](https://github.com/omar-y-abdi/furl/commit/fb1fa013fde70f7df3b63afd8699cb730c6edc84))
* **dedup:** near-duplicate tier — differing rows + recoverable pointer, counterfactual-gated ([3da2e12](https://github.com/omar-y-abdi/furl/commit/3da2e12fc51d7f6b7b7ff16630414dfbd6e01148))
* **feedback:** local-only retrieval-signal feedback loop (engine P2-13) ([39cbf05](https://github.com/omar-y-abdi/furl/commit/39cbf05f1f7ebbb9d762aa9cb6909236fa2cfc98))
* reject unknown kwargs in ContentRouter.apply() — typos fail loud (critique item 5) ([5060d98](https://github.com/omar-y-abdi/furl/commit/5060d987103cfc7fdac1f755e5f519a0e73bc19e))
* **router:** reversible CCR-offload fallback — corpus reduction 36% -&gt; 95% ([4b820c7](https://github.com/omar-y-abdi/furl/commit/4b820c72f0a37c009461a7dc60e13cebb9ad80d8))
* secret-keep rail in TextCrusher (default on) ([67670c3](https://github.com/omar-y-abdi/furl/commit/67670c324815f19d602ad02087eb6eed8a5aca22))
* surface by-design as deliberate-choices + full-strength critique of recent session decisions in adversarial-critique ([d000a8f](https://github.com/omar-y-abdi/furl/commit/d000a8f6190d6ce74222482805f3f1b7fe37a8d3))
* tabular CSV ingestion — sniff, records, SmartCrusher ([0bdb522](https://github.com/omar-y-abdi/furl/commit/0bdb5229f2a5bf76f1f45c1fc367b1e499dc2c18))
* **text:** TextCrusher — deterministic ML-free prose compression + tag_protector restoration (engine P2-11, PERF-15) ([cfcbae1](https://github.com/omar-y-abdi/furl/commit/cfcbae1083c5345666b064b9b28fb20585bd45aa))


### Bug Fixes

* **amputate:** commit straggler edits from D5 (tracker relocation + provider trim) ([3ee7c02](https://github.com/omar-y-abdi/furl/commit/3ee7c029ea3af4a615f4fdfd69c44aadae3e0d52))
* **amputate:** guard dead rtk/lean-ctx lazy imports in proxy.helpers ([e97d7aa](https://github.com/omar-y-abdi/furl/commit/e97d7aa895e2cb8c5482c3db78cf9190042f1649))
* **bench:** isolate committed baseline from run_bench, fix run_final multiturn scorer ([63c2d3e](https://github.com/omar-y-abdi/furl/commit/63c2d3ee702b67c1dd5b2ec4006c11aa694da79c))
* **cache_aligner:** block-format text-part awareness (COR-53) ([f3f799d](https://github.com/omar-y-abdi/furl/commit/f3f799d930610f070899deb296e5903485a114d9))
* **cache_aligner:** injective stable_prefix_hash ([#5](https://github.com/omar-y-abdi/furl/issues/5)) + lock warning surfacing ([#6](https://github.com/omar-y-abdi/furl/issues/6)) ([433589e](https://github.com/omar-y-abdi/furl/commit/433589e2d782a1ce036b2e4c130ead9bdf3865bd))
* **cache:** thread-safe router cache + fail-open pipeline construction + waste-scan cap (COR-21/PERF-11/COR-43, engine P1-9) ([91e4ee7](https://github.com/omar-y-abdi/furl/commit/91e4ee7580e84c569c2d96310207f2c7c4901663))
* **ccr:** cause-honest capacity-eviction miss; reframe Cluster G (not silent loss) ([91a3d41](https://github.com/omar-y-abdi/furl/commit/91a3d417924dacb188956fb7c36140bb53e2ad75))
* **ccr:** make MCP retrieve miss loud + cause-honest (second model-facing surface) ([a15f3e7](https://github.com/omar-y-abdi/furl/commit/a15f3e7944204a1ea4fc424b069e66fdd8cb3291))
* **ccr:** persist only dropped rows + capacity-gate granular chunking (COR-4), honest marker count (COR-20) ([ded2002](https://github.com/omar-y-abdi/furl/commit/ded20024ab4970aa29f2cc189e7d2fa4ab9fc1c2))
* **ccr:** session-scale default TTL 1800s + no orphan store-writes on lossless-win (engine P0-3/P0-4) ([831fc4c](https://github.com/omar-y-abdi/furl/commit/831fc4cfab46185642e7bb8ee116d76d776c957a))
* **ccr:** typed-hash store-miss fail-open (COR-5) + de-vacuum typed-parity test ([b6ec119](https://github.com/omar-y-abdi/furl/commit/b6ec119e50bcbfe50533efc197bb22d0e694421d))
* **compaction:** decline lossless compaction for grammar-breaking column keys (COR-15) ([62481f9](https://github.com/omar-y-abdi/furl/commit/62481f9bad7dfe3d5d7306b611aa96359aff8dc2))
* **compaction:** gate lossless-accept to decoder-verifiable shapes + json-cell decode (COR-13) ([7b175f7](https://github.com/omar-y-abdi/furl/commit/7b175f7b4f08d8f090c174258195e623192b63ab))
* **compaction:** single ccr_store field so walk_array opaque cells persist (COR-19) ([683f16c](https://github.com/omar-y-abdi/furl/commit/683f16c2f0796363dfe428da10f6e0ce2b56d9ae))
* **compression_store:** make [#23](https://github.com/omar-y-abdi/furl/issues/23) eviction cap robust to under-counted staleness ([3365dec](https://github.com/omar-y-abdi/furl/commit/3365decc0a20e892554cef3ed51641f7fb02ff9e))
* **compression_store:** redact JWT in plain-text Authorization header ([#20](https://github.com/omar-y-abdi/furl/issues/20)) + regression test ([4cd431d](https://github.com/omar-y-abdi/furl/commit/4cd431d201a516148fe232484a84aba1f6a46513))
* **compression_store:** store guards — explicit_hash floor, ttl&gt;0, eviction capacity ([#21](https://github.com/omar-y-abdi/furl/issues/21)/[#22](https://github.com/omar-y-abdi/furl/issues/22)/[#23](https://github.com/omar-y-abdi/furl/issues/23)) + tests ([fca2a61](https://github.com/omar-y-abdi/furl/commit/fca2a61c8286692fb531a1ced68093c72ab5e519))
* **compressors:** traceback termination (COR-25), search separator parse (COR-26), binary-diff regex (COR-27) ([c675e6a](https://github.com/omar-y-abdi/furl/commit/c675e6a5388498841147ccf0095995e03faf4fde))
* **compress:** stop mutating caller's CompressConfig; warn on unknown kwargs ([b4bb24d](https://github.com/omar-y-abdi/furl/commit/b4bb24ddd57deb1a481316c574b2a025ed88d0d5))
* **compress:** tool_calls None-guard + frozen-prefix cache_control warnings (COR-46/49/50) ([b4fa956](https://github.com/omar-y-abdi/furl/commit/b4fa956b867907818ca738d92501a37bafd7f79f))
* **content_router:** de-duplicate kompress in strategy_chain ([#13](https://github.com/omar-y-abdi/furl/issues/13)) + test ([f675e10](https://github.com/omar-y-abdi/furl/commit/f675e10e90ecff61a98cd9b30bec4a735d7012fb))
* **content_router:** empty-output guard reports passthrough, not phantom savings ([#11](https://github.com/omar-y-abdi/furl/issues/11)) + test ([77d0ea4](https://github.com/omar-y-abdi/furl/commit/77d0ea4d1a97dcc8ad34908ef9b0a2ccd80f394f))
* **content_router:** options+collision-safe cache key, nested tool_result compression, honest token units (COR-17/18/30/31/39/47/48) ([bc9ec03](https://github.com/omar-y-abdi/furl/commit/bc9ec0366de20ba9a8f5c91b7b6569e920d9d950))
* **content_router:** propagate per-request runtime options into worker threads ([#10](https://github.com/omar-y-abdi/furl/issues/10)) + test ([7904058](https://github.com/omar-y-abdi/furl/commit/79040589eccf82577e22217267de6deb1bddd582))
* **content_router:** propagate real compressor bugs loud at strategy net ([#4](https://github.com/omar-y-abdi/furl/issues/4)-upstream) + test ([2ac507c](https://github.com/omar-y-abdi/furl/commit/2ac507c339d73a6553634ac6bf29414b97d1fed7))
* **content_router:** stop re-swallowing propagated Kompress bugs ([#4](https://github.com/omar-y-abdi/furl/issues/4)-upstream) ([64df08c](https://github.com/omar-y-abdi/furl/commit/64df08c101e758ed54b96ead7756dbcd0d47d100))
* **content_router:** un-invert adaptive min_ratio so high pressure compresses more ([#12](https://github.com/omar-y-abdi/furl/issues/12)) + test ([5b61503](https://github.com/omar-y-abdi/furl/commit/5b61503ab6b41a1f185bfb4bbdc3ba67ebb46efd))
* COR-1/COR-2 critical lossless-decoder data-loss — tables decoded to 0 rows ([b694feb](https://github.com/omar-y-abdi/furl/commit/b694feb163147ed771bdfbd2a6f985896a56b351))
* **core:** make ONNX deps (fastembed, magika) optional, default-off ([5281d75](https://github.com/omar-y-abdi/furl/commit/5281d7595f14e8eba0e35464e565a28961e09962))
* **core:** migrate hash_field_name hex encoding to digest-0.11 API ([f052dd7](https://github.com/omar-y-abdi/furl/commit/f052dd7efe92e22f8f36eea5bd175e86708cd52d))
* **core:** temporal both-ends check, TopN budget cap, store/compressor stat honesty (COR-34/35/41) ([6b13ae9](https://github.com/omar-y-abdi/furl/commit/6b13ae9ae2bd8f802ccb58dfec2ca3ac8de8e9f1))
* **crusher:** enforce CCR recovery invariant on public compress() path ([0881f79](https://github.com/omar-y-abdi/furl/commit/0881f79020ddf7e0872bd25a067303cf4ab5160e))
* **crusher:** extend 1A unconditional CCR persist to non-dict array paths ([b9f26db](https://github.com/omar-y-abdi/furl/commit/b9f26db72b3a35f6e99d260d430e5f0e24f66ff4))
* **crusher:** mixed-array persist-skip + lossless-win (COR-28), dup_count stamp gating (COR-33), walker no-op guard (COR-45) ([a48c08d](https://github.com/omar-y-abdi/furl/commit/a48c08d8559dae22a4cadc4ed8467c686b071c90))
* **csv_schema_decoder:** decode const+arith zero-var-column tables ([#25](https://github.com/omar-y-abdi/furl/issues/25)) + test ([792eb4a](https://github.com/omar-y-abdi/furl/commit/792eb4a6e6541711968496b0e76c4a0bba9be0ce))
* **csv_schema_decoder:** recover empty sole-var-column rows ([#24](https://github.com/omar-y-abdi/furl/issues/24)) + round-trip test ([0a8b8ee](https://github.com/omar-y-abdi/furl/commit/0a8b8eef57d102fb90f8e307a398dc21f4a1344e))
* **cut:** commit cache/__init__ + config.py export/field edits (B4-B6 completion) ([03544fa](https://github.com/omar-y-abdi/furl/commit/03544faa23eb745c59b6b6b8d9f13bb4d64c0da9))
* **decoder:** unquote affix cells before re-applying prefix/suffix (Cluster A gap) ([54c54a0](https://github.com/omar-y-abdi/furl/commit/54c54a0b9493f565f0ee8156f6d505d629256a17))
* **dedup:** surrogate-safe hashing (COR-42), honor protect_recent contract (COR-52) + re-baseline multiturn ([4e1197e](https://github.com/omar-y-abdi/furl/commit/4e1197e5d36192084baecf97babf704a4fe8d7c3))
* **detection:** make _detect_content docstring tell the truth + pin the two-stage rule ([23d954f](https://github.com/omar-y-abdi/furl/commit/23d954fe924c9c6ef1fb33a354187d0075e46a69))
* **docs:** reconcile Proof table with the backed 60-95% headline + exact compress.py map anchors ([7ec7ea9](https://github.com/omar-y-abdi/furl/commit/7ec7ea9cf39441b160bb0dfdf1b2ceff0e2c37c9))
* **docs:** round-3 re-recon batch — README benchmark integrity + phantom/dead-doc cleanup ([516d515](https://github.com/omar-y-abdi/furl/commit/516d51562cd7dd640f9f831ea282d2dc7b9c6831))
* **ffi:** contain Rust panics — catch_unwind-&gt;PyRuntimeError + BaseException fail-open (COR-7) ([39a758c](https://github.com/omar-y-abdi/furl/commit/39a758c60a2b2c70ddc944190e5ceb6e3b60e56b))
* **ffi:** panic containment on detect bridge + tiktoken hardening (engine P0-1/P0-6) ([bf238cd](https://github.com/omar-y-abdi/furl/commit/bf238cd1a5a3dc687950a42340daa18ff70104b0))
* **gate:** G2 silently skipped the whole suite + fix the test it masked ([0fc516e](https://github.com/omar-y-abdi/furl/commit/0fc516ee8255e6660d8f7414a5a80e74e8ee6041))
* **gate:** G4/G5 honesty — exit-code keying + stale-capture guard (TEST-1/2) ([2ddb15d](https://github.com/omar-y-abdi/furl/commit/2ddb15d42595908e5a65c0a0a0752ea03c3acdb6))
* guard MCP retrieve against malformed ccr hashes; dedup the spoofing check (cycle-4 item 3) ([2990f87](https://github.com/omar-y-abdi/furl/commit/2990f87e368eed229969f46b25b38613ac8a0e2f))
* guard trafilatura import and add html optional extra ([736698e](https://github.com/omar-y-abdi/furl/commit/736698eb02abac091bb6ca23d66d86694c8b44c2))
* isolate per-request runtime options with thread-local storage ([e940cf6](https://github.com/omar-y-abdi/furl/commit/e940cf68c227be34af2db5c292bb3a7f8a62f517))
* **kompress:** global top-k so kept-count matches target_ratio across chunks ([#3](https://github.com/omar-y-abdi/furl/issues/3)) + test ([9165cbd](https://github.com/omar-y-abdi/furl/commit/9165cbd0531f178f3e4fea755239717f466e78ce))
* **kompress:** honor config.score_threshold on the default keep-mask path ([#1](https://github.com/omar-y-abdi/furl/issues/1)) ([974f567](https://github.com/omar-y-abdi/furl/commit/974f5674bb56c11a9b7aa7f485b8681844bce6b3))
* **kompress:** make [0.8,0.9) compression band CCR-recoverable ([#2](https://github.com/omar-y-abdi/furl/issues/2)) + round-trip test ([b334636](https://github.com/omar-y-abdi/furl/commit/b334636b6dc15507ad537558d9c50cbb0612f17f))
* **kompress:** narrow blanket except so model/tokenizer bugs propagate ([#4](https://github.com/omar-y-abdi/furl/issues/4)) + test ([de42936](https://github.com/omar-y-abdi/furl/commit/de42936f97e814c30f66a80f8076373031847eac))
* **kompress:** re-chunk truncated tail words (COR-22), honor frozen_message_count + str-only token count (COR-29) ([6555c69](https://github.com/omar-y-abdi/furl/commit/6555c69e8d8ca29183676c40979fb2abd9b10b7d))
* **kompress:** store-fail veto-&gt;passthrough (COR-6), onnx_coreml backend (COR-11), mid-batch KeyError guard (COR-12) ([6360ea1](https://github.com/omar-y-abdi/furl/commit/6360ea13220ee6099fe50314b01ade3c83135caf))
* make compress() fail-open loud + honest (C1, cycle 2) ([d78dc38](https://github.com/omar-y-abdi/furl/commit/d78dc38cf96af4f3608c4e01459b65c12649c679))
* make the CCR Rust→Python mirror fail loud, never silent (A1) ([d0e3fb4](https://github.com/omar-y-abdi/furl/commit/d0e3fb4fc9187a43661005dda1ce512c464e2329))
* **mcp_server:** non-matching query on a live entry is not an eviction error ([#18](https://github.com/omar-y-abdi/furl/issues/18)) + test ([77355da](https://github.com/omar-y-abdi/furl/commit/77355dabae0850becb23b9da149b152eab50ff71))
* **mcp_server:** savings_percent agrees with tokens_saved ([#19](https://github.com/omar-y-abdi/furl/issues/19)) + test ([4ef5d62](https://github.com/omar-y-abdi/furl/commit/4ef5d62f7c6a23785d5ffb0272f411b9e6198b8f))
* **mcp/store:** case-insensitive retrieve, session-TTL marker rows, numeric-fidelity query path, collision keep-first (COR-32/51/54/56) ([187ff76](https://github.com/omar-y-abdi/furl/commit/187ff76f201441d16ebea7da0fcca6de713f534f))
* **p7:** make CacheAligner stateless — kill cross-request prefix latch ([225e5eb](https://github.com/omar-y-abdi/furl/commit/225e5ebf13f01904e35c59dc3ae8be514628a5d5))
* **p7:** reject unknown compress() kwargs + guard csv phantom-row ([9c97b46](https://github.com/omar-y-abdi/furl/commit/9c97b46d0d4f9bf26d2c409fcc7b7e034216f4b2))
* **parser:** coerce explicit None text in content blocks ([#17](https://github.com/omar-y-abdi/furl/issues/17)) + test ([ee83730](https://github.com/omar-y-abdi/furl/commit/ee83730fd9e772ceb29f029d89d520d01f712ccf))
* **parser:** correct waste-signal metrics [#14](https://github.com/omar-y-abdi/furl/issues/14) whitespace, [#15](https://github.com/omar-y-abdi/furl/issues/15) html comments, [#16](https://github.com/omar-y-abdi/furl/issues/16) json bloat ([0c7f878](https://github.com/omar-y-abdi/furl/commit/0c7f878efc8fc7f872f8470a2642dd36137f8f48))
* **py:** lint debt, env-var validation, and silent-swallow cleanup ([decbb47](https://github.com/omar-y-abdi/furl/commit/decbb475ef964c3a9b23bc22bd07f4f6647cb9c9))
* **py:** log best-effort failures instead of bare except-pass ([acf908f](https://github.com/omar-y-abdi/furl/commit/acf908f5dd465bb065aedc7aa4b08e328a5043d0))
* **py:** raise ValueError on invalid JSON input instead of panicking ([178b169](https://github.com/omar-y-abdi/furl/commit/178b169c0322f0ad0d952cf3f7c299be7de52555))
* Release PR dry-run gates dist against pyproject version ([#15](https://github.com/omar-y-abdi/furl/issues/15)) ([4002c25](https://github.com/omar-y-abdi/furl/commit/4002c2515da9adeff414f7db56c0531dfb6d8eb9))
* round-4 doc-integrity + config batch — honest citations, real hook cmd, scoped headline ([ad16506](https://github.com/omar-y-abdi/furl/commit/ad165067ccc43b1cdb27e2263e449e45b3cb89ba))
* round-5 high [#1](https://github.com/omar-y-abdi/furl/issues/1) — CSV-schema lossless null/missing data-loss (sentinels) ([dd3c131](https://github.com/omar-y-abdi/furl/commit/dd3c131f1757e5df32984a69fe9c39c8dcfdf017))
* round-5 highs (4/5) — strip phantom compressors + close retrieval-log redaction gaps ([74cfa7f](https://github.com/omar-y-abdi/furl/commit/74cfa7f9b8e6d68cf5a82037f4509871afb3da9e))
* **router:** compress Bash outputs + glob/case tool exclusion + retrieval-loop guard (engine P0-2/P0-5) ([f7ac835](https://github.com/omar-y-abdi/furl/commit/f7ac8358cdc22529a632220568e3a7a4196573f6))
* **router:** ContentType total-fn + Optional sigs + drop dead cache_hit field ([45811a1](https://github.com/omar-y-abdi/furl/commit/45811a11b8b8064c00aaa2625963467948a1a378))
* **router:** re-mirror CCR entries on result-cache hit to prevent unbacked sentinels ([67d7ebc](https://github.com/omar-y-abdi/furl/commit/67d7ebc24fecf86911b7fa7efddded97a0648233))
* **router:** recompute on unbackable result-cache hit (both-CCR-store-expired hole) ([b483baf](https://github.com/omar-y-abdi/furl/commit/b483baf10008c39fda2b87abfe026bec6f9d0f62))
* **router:** word-boundary analysis-intent + copy-on-write frozen aliasing (COR-16/COR-55) ([85d0ab4](https://github.com/omar-y-abdi/furl/commit/85d0ab420a8d41b4ea0837ec973c810f03c40eb8))
* **security:** jail headroom_read + size caps + log redaction (mcp) ([4e03a35](https://github.com/omar-y-abdi/furl/commit/4e03a35a5c07a51a2bc6f8a838a2ff2262407e75))
* **security:** pyo3 0.24.2 -&gt; 0.29.0 (RUSTSEC-2026-0176/0177) + search grouping + diff noise elision (engine P2-14/P2-15) ([83c4238](https://github.com/omar-y-abdi/furl/commit/83c42381bbae9ba5fd401a46e782cf701aadcfd5))
* **security:** redact mcp logs + fd-pin headroom_read; strengthen 3 tests ([c608d0f](https://github.com/omar-y-abdi/furl/commit/c608d0ff8d773e973480c93b63ae4ad42e5a8701))
* **security:** redaction gaps, jail dir_fd walk, shared-stats atomicity, live paths, hard-fail supply-chain CI (SEC-4/5/6/7/8) ([c19eb8f](https://github.com/omar-y-abdi/furl/commit/c19eb8fbab0cbb97868d0157eeba22b73bbb9eec))
* **serde:** fail-closed guard against serde_json private magic-keys (COR-44) ([083d43e](https://github.com/omar-y-abdi/furl/commit/083d43ea903e496254543202d25432ba3cf5b61c))
* **smart_crusher:** reject constant columns as score fields (COR-24), char-count anchor length (COR-23) ([925e4dc](https://github.com/omar-y-abdi/furl/commit/925e4dc3405edd481188cbb2918fe8e452493f19))
* **tag_protector:** balanced nested marker-mode close (COR-8), unquoted-/ not self-closing (COR-9) ([a1f64d5](https://github.com/omar-y-abdi/furl/commit/a1f64d5ea60e9031a5bf2a2ef72ab2234314e67e))
* **tokenizers:** base64 count explosion, case-insensitive resolve, non-poisoning negative cache (COR-40) ([a6139e8](https://github.com/omar-y-abdi/furl/commit/a6139e8e9377c771fd4340ba64b61f89455f133b))
* **U1:** read_lifecycle store=None: stop emitting a recovery pointer to a hash stored nowhere ([d192dc4](https://github.com/omar-y-abdi/furl/commit/d192dc427382360de0be0eaa425b5dc8fbd1be66))
* **U2:** RFC-4180 quote-aware line reader in csv_schema_decoder + cross-language round-trip fuzz test ([50dbd64](https://github.com/omar-y-abdi/furl/commit/50dbd641c0d889b1fcc1c3234f3df104d09089e1))
* **U3:** Public compress() computes a message-level frozen prefix count (mirror Rust compute_frozen_count) ([50a39f5](https://github.com/omar-y-abdi/furl/commit/50a39f585bd6ef193c14553e8c811ba218d38946))
* **U4:** Accept SmartCrusher's 12-char hash and &lt;&lt;ccr:HASH&gt;&gt; marker form in the retrieval-tool path ([51ac42a](https://github.com/omar-y-abdi/furl/commit/51ac42a74a2071bedd2d06354c942b1c242be422))
* **U5:** Generation-counter eviction for in-memory CCR store (fix FIFO unbacked-sentinel, tombstone growth, ABA) ([72464a6](https://github.com/omar-y-abdi/furl/commit/72464a65bcfe60762ade4b4b021a9904969018d1))
* **U6:** Persist the diff sidecar cache_key (compress_with_stats) into the Python CCR store ([aef901d](https://github.com/omar-y-abdi/furl/commit/aef901dbef6abf3dd1b96f426dd66fd4eac86767))
* **U8:** Eliminate the redundant double compaction run on crush_array's hot path ([25db3ab](https://github.com/omar-y-abdi/furl/commit/25db3ab6b9bb4f118495c4d869942e2ab6448449))
* **U9:** adaptive_sizer n&lt;=8 fast path must respect max_k (Python + Rust parity) ([dc11c82](https://github.com/omar-y-abdi/furl/commit/dc11c829ad384e8f5d8f915253b0472bfeafe423))
* **verify:** repoint search generator to committed capture, restore multiturn auto-flag ([4a8fa18](https://github.com/omar-y-abdi/furl/commit/4a8fa18a609ee4e0b60442552c629437cbcf1189))
* veto CCR marker on store-write failure in diff/log/search (P0) ([28d87f2](https://github.com/omar-y-abdi/furl/commit/28d87f2ce573ead404d7733d9b17d11f23f0944b))


### Performance Improvements

* **core:** eliminate hot-path re-serialization, clones, MD5 windows, throwaway stores (PERF-3/4/5/6/7/8, DOC-12, TEST-9) ([3d18417](https://github.com/omar-y-abdi/furl/commit/3d184172596f4f4a4df7ce6735f74d9573671997))
* **crusher:** precompute novelty hashes; borrow region slice in density scan ([0dbbaa4](https://github.com/omar-y-abdi/furl/commit/0dbbaa45a3f1bef886b547cdc5ffc65d2142012f))
* cut hot-path token counts, lazy version, prefix-sampled estimation, context caps (PERF-1/2/12/13/14, API-8) ([646988a](https://github.com/omar-y-abdi/furl/commit/646988a26ea60a4376ebf0c66455db87b9166bf7))
* **router:** compute content detection once per compress, not twice ([15a3936](https://github.com/omar-y-abdi/furl/commit/15a39365205a3c99948e72d1e306930e893d3c18))


### Code Refactoring

* **amputate:** sever compression core from deleted-module coupling ([b4c03e9](https://github.com/omar-y-abdi/furl/commit/b4c03e96ce853534ad60057e0f417177f571bd37))
* bundle prioritize_indices args into PrioritizeParams struct (clippy) ([860f077](https://github.com/omar-y-abdi/furl/commit/860f077dd98316a7ab601ac73733e99b9fcc113f))
* **ccr:** delete dead SQLite/Redis backend + from_config factory ([817a2f1](https://github.com/omar-y-abdi/furl/commit/817a2f165a59936ce3a12716c2ea89636353b893))
* **ccr:** refactor precursors — clock object, wire-form hash vectors, seam typing, persist consolidation (TEST-21/33, TYPE-3, ARCH-5) ([f792c4b](https://github.com/omar-y-abdi/furl/commit/f792c4bae8b1c1ac9ea978e7fcd8b7a59604723b))
* **ccr:** typed dropped-refs across the FFI, R1-R5 (ARCH-2/TYPE-2, §4.2) ([958a452](https://github.com/omar-y-abdi/furl/commit/958a452fc5cd4772bfa7233479edee9fd796bfb8))
* collapse dead OTel telemetry ceremony in pipeline.apply() ([62ec035](https://github.com/omar-y-abdi/furl/commit/62ec035f50c94ef58d9de25365d19a7dfaf326b4))
* complete ML→Kompress vocab rename (_try_ml_compressor → _try_kompress) ([69097d5](https://github.com/omar-y-abdi/furl/commit/69097d5d819dcf3fe9029410b9341bb7adbbcd2d))
* **core:** split crusher.rs monolith into walk/route/persist modules (ARCH-4) ([005a2b5](https://github.com/omar-y-abdi/furl/commit/005a2b5dfa4f12133daaff2c1a100d491c918d90))
* crush() recovers row-drops from TYPED Rust fields, not a text-scrape (cycle-3 item 1a) ([89b2391](https://github.com/omar-y-abdi/furl/commit/89b23910510881e876ef5028826a3b3f0f319773))
* delete dead embedding/ML subsystem (P4) ([00062e5](https://github.com/omar-y-abdi/furl/commit/00062e524b5298f95146f1fc66d0c7164be9c332))
* delete dead fork-era paths.py helpers + refresh map drift ([737a84d](https://github.com/omar-y-abdi/furl/commit/737a84dc36ee5a7eecd461a546cbf08e98a3c96d))
* delete dead Python algorithm twins (Rust-only live path) ([6bd9b66](https://github.com/omar-y-abdi/furl/commit/6bd9b66782ad99c737beaa9655c178e1947f52fb))
* delete the 8 dead pipeline lifecycle stages + extension discovery (Part A) ([c010a2c](https://github.com/omar-y-abdi/furl/commit/c010a2cdacca296cf9409503072b0cfd676acd46))
* delete the orphaned Rust cache_control module (Part B) ([57e18fe](https://github.com/omar-y-abdi/furl/commit/57e18fe86c88399203335835b6ca7064a410888e))
* drop dead lazy-export ceremony from providers/ (cycle-3 item 4) ([78ead3b](https://github.com/omar-y-abdi/furl/commit/78ead3b5538a8c40736143e7f1205a9e79a9849d))
* excise dead compression_policy proxy vestige (A3) ([89285ef](https://github.com/omar-y-abdi/furl/commit/89285efce463c465169af017e64d4d0c3f33b6c1))
* **excision:** delete dead Rust subsystems — content_detector mirror, HF tokenizer, inert TOIN fields (Chunk 5) ([7dab530](https://github.com/omar-y-abdi/furl/commit/7dab530e75a0d5ce586cf56ffa8efe86eaf9dc85))
* **excision:** delete HTML extraction subsystem + [html]/trafilatura dep (Chunk 2) ([b572245](https://github.com/omar-y-abdi/furl/commit/b572245a3a254521a81ec70eea2760411a7cb6b9))
* **excision:** delete Kompress ML compressor + ONNX runtime + [ml] deps (Chunk 1) ([7d66319](https://github.com/omar-y-abdi/furl/commit/7d66319aba0709cfdec5218849a0b98fb1fadeb2))
* **excision:** delete tag_protector (dead FFI slice) + CODE_AWARE vestigial strategy (Chunk 7) ([d1d444c](https://github.com/omar-y-abdi/furl/commit/d1d444cede86c6d69f4ccb3c2a7db3e9ad994cf0))
* **excision:** delete telemetry/TOIN/compression-feedback plane (Chunk 3, -5285 LOC) ([219a621](https://github.com/omar-y-abdi/furl/commit/219a62123c2d3fc5284eb7e9b9f5868305a12340))
* **excision:** drop unused ast-grep-cli dep (Chunk 6) ([5dd2019](https://github.com/omar-y-abdi/furl/commit/5dd2019af342786a72858b114a408a65fb95fc9d))
* **excision:** tokenizers to tiktoken-only — delete HuggingFace + Mistral backends (Chunk 4) ([3c87dc4](https://github.com/omar-y-abdi/furl/commit/3c87dc47febf98e04373b389b0d41ff7c396009f))
* extract _lookup_cached_disposition — one home for the two-tier cache lookup ([8df5136](https://github.com/omar-y-abdi/furl/commit/8df51364df55c241ea5a7e1b3e4c0bc1b536a90c))
* extract 4 clean seams from ContentRouter god-object ([dcd6ec4](https://github.com/omar-y-abdi/furl/commit/dcd6ec44b782e91cc7a1f6aceed2ac265ccf33a8))
* extract CcrMirror seam from ContentRouter (Part F) ([e1c7288](https://github.com/omar-y-abdi/furl/commit/e1c7288447c65d2ac5f039a337a657f41835b086))
* extract CompressorRegistry from the ContentRouter god-object (cycle-3 item 2) ([3be4d66](https://github.com/omar-y-abdi/furl/commit/3be4d6692dada6cae00104879362427736b3be14))
* extract StrategyDispatcher seam from ContentRouter (Part E) ([0b634d2](https://github.com/omar-y-abdi/furl/commit/0b634d2202e20959547d291a7d72f64a8feb10a8))
* introduce frozen CompressRequest at the pipeline boundary (T1) ([09ef3a6](https://github.com/omar-y-abdi/furl/commit/09ef3a655940d4b437d1d4e3c9ae6a1a41e0cd99))
* **mcp_server:** [#18](https://github.com/omar-y-abdi/furl/issues/18) uses side-effect-free exists() (no retrieval over-count) ([0f1fb2c](https://github.com/omar-y-abdi/furl/commit/0f1fb2cd24ab00fc738ce999d8f56008c47b709d))
* **pipeline:** freeze PipelineEvent — kill the in-place-mutation sanction ([fc92ab4](https://github.com/omar-y-abdi/furl/commit/fc92ab4affa43526c3100d82473cef99f6075804))
* read _runtime_* properties directly, drop dead getattr defaults (cycle-4 item 2) ([77f9610](https://github.com/omar-y-abdi/furl/commit/77f96100d1f4b425f908345088879769aa07956a))
* remove dead legacy-helper ring from log/search compressors ([9106ef5](https://github.com/omar-y-abdi/furl/commit/9106ef5b003436a83ced8142914548158afb13b3))
* rename project headroom -&gt; Furl (furl-ctx / furl_ctx / furl-core) ([3daf09c](https://github.com/omar-y-abdi/furl/commit/3daf09ce6ecdbf2b86a3abe67c6abfff81679176))
* replace runtime thread-local with a frozen RouterRuntime value (Part G) ([82f43eb](https://github.com/omar-y-abdi/furl/commit/82f43eb1e4d6fc2f1a4582062864a62aa2ff01c1))
* **router:** collapse the two near-identical content-block branches (god-object step 2) ([5b63ac4](https://github.com/omar-y-abdi/furl/commit/5b63ac4b52502999b34c35aa2bccf9ab317cba33))
* **router:** decompose ContentRouter god-object into 5 modules + facade (ARCH-1/TYPE-4/SIMP-9-part, §4.1 S0-S7) ([815751a](https://github.com/omar-y-abdi/furl/commit/815751ab5db8d5f5a4fe6b478f07add11bef96ba))
* **router:** delete dead eager_load_compressors + route_and_compress (god-object step 1) ([5095c87](https://github.com/omar-y-abdi/furl/commit/5095c8711c17039b5946729487d16a03b5e9959f))
* **rust:** excise the never-shipped magika/embeddings/redis feature-gates ([0211b06](https://github.com/omar-y-abdi/furl/commit/0211b06627ad6604aaeec05164d4ba2d7dc36591))
* scrub deleted-proxy + fork-era residue from source comments ([da519af](https://github.com/omar-y-abdi/furl/commit/da519afc555bf09cc4e8030442600963b39bf5f9))
* single-owner CCR marker grammar spec (consumer side) ([206f93b](https://github.com/omar-y-abdi/furl/commit/206f93bd030775f493b401ef5150f378e52f673f))
* single-owner Rust CCR marker family (marker_for_*) ([e7a617f](https://github.com/omar-y-abdi/furl/commit/e7a617f8acc823735b4ec245dcbd24b7fd82594c))
* single-source SmartCrusherConfig forwarding + name the CSV-codec grammar (cycle-3 item 3) ([233e02d](https://github.com/omar-y-abdi/furl/commit/233e02da6830b976b4823664cd9091101339cf7a))
* **tokenizer:** unify duplicate TokenCounter Protocol, fold providers/ ([ba572de](https://github.com/omar-y-abdi/furl/commit/ba572de832c37cc92e5df6aab0a41bff785cbdcf))
* typed core contracts, dead-surface excision, config honesty (TYPE-1/5, ARCH-6/8/9/10/11/12, SIMP-4/5/7/8/11, API-14, TEST-8) ([5d83857](https://github.com/omar-y-abdi/furl/commit/5d83857b446479780caad36ce37dce2d5370b117))
* un-couple mcp_server from the proxy route (proxy retirement 1/4) ([3bad4cf](https://github.com/omar-y-abdi/furl/commit/3bad4cf4019d9cb68c3df783cb9e0a00bc96f575))
* **workflow:** headroom-implement = Plan+Build only; integration moved to orchestrator (no agent git on main) ([f877e5c](https://github.com/omar-y-abdi/furl/commit/f877e5c065ea38e22c6418b27eef749ad46abcaa))

## [Unreleased]

Nothing yet.

## [0.26.0] - 2026-07-02

### Rename and history note

This is the first release under the **Furl** name. The project was previously
named Headroom; the PyPI package is now `furl-ctx` and the Python import is
`furl_ctx`. The git history was squashed on 2026-07-02 — all pre-rename commits
were collapsed into a single root commit ("Initial commit (inherited Headroom SDK
base)"). The pre-rename release log no longer maps to this repository's commits
and has been retired.

### What ships today

**Public API** (`from furl_ctx import compress, CompressConfig, CompressResult`)

- `compress(messages, model=..., config=...)` — single entry point for context compression
- `CompressConfig` — typed configuration for compression behaviour
- `CompressResult` — typed result carrying compressed messages and stats

**Pipeline transforms**

- `CacheAligner` — aligns content to prompt-cache prefix boundaries
- `CrossMessageDeduper` — cross-message deduplication with byte-verified CCR
  pointer replacement
- `ContentRouter` — strategy-based routing of content to compressors

**Compressors**

- `SmartCrusher` — Rust-backed (pyo3) compressor: JSON-array crushing and
  lossless column-encoding compaction suite
- `SearchCompressor` — optimised for tool-output search result blocks
- `LogCompressor` — log and trace output compressor
- `DiffCompressor` — diff and patch output compressor

**CCR (Compressed-Content Recovery)**

- Byte-exact recovery of compressed content via `<<ccr:HASH>>` inline markers
- In-memory CCR store; no external persistence required

**MCP server**

- Entrypoint: `python -m furl_ctx.ccr.mcp_server`
- Tools: `furl_compress`, `furl_retrieve`, `furl_stats`
- Opt-in: `furl_read` (behind a feature flag)

**Tokenization**

- tiktoken only (cl100k_base / o200k_base)

**Rust extension**

- `furl_ctx._core` built with maturin; crates `furl-core` / `furl-py`
- Distributed as a single wheel — no separate Rust toolchain required at
  install time

### Removed from the pre-rename codebase

The following capabilities were excised before this release and do not ship:

- Reverse proxy and upstream routing (Vertex AI, OAuth2, multi-provider)
- Web dashboard and CLI
- ML-based compressor (Kompress)
- HTML extraction
- Telemetry and usage reporting
- Non-tiktoken tokenizer backends (Hugging Face, Mistral)

Furl is now a focused context-compression engine and MCP server for Claude Code
tool-output compression, with no network-facing proxy surface.
