# `signals/` — detection traits

Cross-cutting classifiers used by transforms. Lives at the crate root because the same classifier feeds many transforms; nesting under `transforms/` would imply ownership by one consumer.

## Trait family

| Trait | Granularity | Status |
|---|---|---|
| `LineImportanceDetector` | one line at a time | shipped (Phase 3e.1) |
| `ContentTypeDetector` | whole blob | future generalization of `transforms::detection` |
| `ItemImportanceDetector<I>` | `&[I]` ranking | future, for SmartCrusher cells / search hits |

## Tiering — composition, not inheritance

A `Tiered<dyn Trait>` chaining combinator (ordered stack, first tier whose
signal exceeds `ESCALATE_THRESHOLD` (0.7) confidence wins, highest-confidence
fallback otherwise) previously lived here. It had zero production
constructions and was **deleted (SIMP-5b)** — see `signals/mod.rs`'s doc
comment for the record. Composition of a future second tier now happens **at
the call site**, not through a shared combinator type in this module.

Today `KeywordDetector` is the only tier registered — there is no chaining to
demonstrate yet. When a real second tier lands, wire the ordered-fallback
logic where the caller consumes both detectors' signals, per the current
`signals/mod.rs` guidance, rather than reintroducing a shared `Tiered<T>`.

## How to add a new detector

1. **Confirm granularity.** A line classifier implements `LineImportanceDetector`. A blob classifier gets a new trait. Don't shoehorn cross-granularity work into one trait.
2. **Implement `score(&self, ...) -> ImportanceSignal`.** Set `confidence` honestly: 0.7+ if the detector is the right authority for this input, lower if you want the next tier to override on disagreement.
3. **No silent fallbacks.** Per project conventions, return `ImportanceSignal::neutral()` when you have no information — never fabricate a positive answer with low confidence to "fail open".
4. **Compose at the consumer**, not in this module — there is no shared `Tiered` combinator (deleted, SIMP-5b; see above). The detector itself doesn't know about other tiers; ordering/fallback between tiers is the caller's responsibility.
5. **Add parity fixtures** if the detector replaces or augments an existing one. Mark divergence lines with `// fixed_in_<phase>` markers.

## Canonical future ML extension — BGE classifier head

The most likely next tier is a classification head on a `bge-small-en-v1.5` embedder (the prior `relevance::EmbeddingScorer` was removed with the never-shipped `embeddings` feature; this tier would reintroduce an ONNX embedder):

```rust
pub struct BgeClassifierDetector {
    embedder: Arc<dyn Embedder>,        // shared with relevance scoring
    classifier: LogisticRegression,     // 384-dim → 4-class softmax
    threshold: f32,                     // calibrated on validation set
}

impl LineImportanceDetector for BgeClassifierDetector { ... }
```

Why this would be the cheapest ML path (note: the core is ML-free today — this tier must first REINTRODUCE an ONNX embedder, per the caveat above):

- Once an embedder is (re)introduced and shared with relevance scoring, the classification head itself adds only ~1.5 KB of weights and ~1 ms inference per line (batchable) on top of it.
- One shared model file and runtime would then serve both relevance scoring and this detector — no second model download.
- Calibrated confidence lets the head short-circuit `KeywordDetector` on high-confidence positives but step aside on borderlines (where the keyword automaton is reliable anyway).

Two alternatives kept open in case BGE-head underfits:

- **Distilled tinyBERT (ONNX)** — more accurate, +10–20 MB model, +3 ms latency, new `ort` dependency.
- **Logistic regression on lexical features** — caps ratio, line length, structural markers, stack-frame heuristics. ~5 KB model, fastest of the three. Good A/B baseline.

The trait shape accepts all three without changes.

## What does NOT live here

- Concrete transforms — they go in `crates/furl-core/src/transforms/`.
- Static keyword data tables — they're configuration for `KeywordDetector`, not detection logic. They live alongside the detector that consumes them (`signals/keyword_detector.rs::KeywordRegistry`).
- Tag protection (`<furl:keep>` markers) — that's user intent, not classification.
