//! `SmartCrusherBuilder` Error-item and structural-outlier preservation are hardwired into the planner (see `the Rust module`), not composed here.

use std::sync::Arc;

use crate::ccr::{CcrStore, InMemoryCcrStore};
use crate::relevance::{HybridScorer, RelevanceScorer};
use crate::transforms::anchor_selector::{AnchorConfig, AnchorSelector};

use super::analyzer::SmartAnalyzer;
use super::compaction::CompactionStage;
use super::config::SmartCrusherConfig;
use super::crusher::SmartCrusher;

/// Builder for `SmartCrusher`. See module docs.
pub struct SmartCrusherBuilder {
    config: SmartCrusherConfig,
    scorer: Option<Box<dyn RelevanceScorer + Send + Sync>>,
    compaction: Option<CompactionStage>,
    ccr_store: Option<Arc<dyn CcrStore>>,
    tokenizer: Option<Box<dyn crate::tokenizer::Tokenizer>>,
}

impl SmartCrusherBuilder {
    /// Empty builder — no scorer, no compaction stage.
    pub fn new(config: SmartCrusherConfig) -> Self {
        SmartCrusherBuilder {
            config,
            scorer: None,
            compaction: None,
            ccr_store: None,
            tokenizer: None,
        }
    }

    /// Set the relevance scorer. The Enterprise plug-in point — pass a `LoopScorer`, a `HybridScorer`, or any other `RelevanceScorer` impl.
    pub fn with_scorer(mut self, scorer: Box<dyn RelevanceScorer + Send + Sync>) -> Self {
        self.scorer = Some(scorer);
        self
    }

    /// Apply the OSS default setup: `HybridScorer`. Use this when starting from the OSS preset and adding a few enterprise components.
    pub fn with_default_oss_setup(self) -> Self {
        self.with_scorer(Box::<HybridScorer>::default())
    }

    /// Plug in a compaction stage. When set, `crush_array` runs the stage before the lossy pipeline; if it
    /// produces a non-`Untouched` compaction the rendered bytes are returned via [`CrushArrayResult::compacted`].
    pub fn with_compaction(mut self, stage: CompactionStage) -> Self {
        self.compaction = Some(stage);
        self
    }

    /// Convenience: enable the OSS compaction preset (CSV+schema formatter, default
    /// `CompactConfig`). Equivalent to `with_compaction(CompactionStage::default_csv_schema())`.
    pub fn with_default_compaction(self) -> Self {
        self.with_compaction(CompactionStage::default_csv_schema())
    }

    /// Plug in a CCR store. Both lossy and lossless paths stash their originals here keyed by hash.
    pub fn with_ccr_store(mut self, store: Arc<dyn CcrStore>) -> Self {
        self.ccr_store = Some(store);
        self
    }

    /// Convenience: install the default in-memory CCR store
    /// (1000 entries, 5-minute TTL — matches Python).
    pub fn with_default_ccr_store(self) -> Self {
        self.with_ccr_store(Arc::new(InMemoryCcrStore::new()))
    }

    /// Construct the `SmartCrusher`. If `with_scorer` was not called, falls back to
    /// `HybridScorer::default()` so a builder with no other customization still produces a working crusher.
    pub fn build(self) -> SmartCrusher {
        let analyzer = SmartAnalyzer::new(self.config.clone());
        let anchor_selector = AnchorSelector::new(AnchorConfig::default());
        let scorer = self
            .scorer
            .unwrap_or_else(|| Box::<HybridScorer>::default());
        // Defect 2: propagate the CCR store into the compaction stage so lossless opaque-blob substitutions persist their originals
        // under the marker hash. Done here (not in `with_compaction` / `with_ccr_store`) so the two builder calls compose in any order.
        let compaction = match (self.compaction, &self.ccr_store) {
            (Some(stage), Some(store)) => Some(stage.with_ccr_store(Arc::clone(store))),
            (stage, _) => stage,
        };
        // Strict lossless-or-passthrough (`lossless_only`): opaque substitution replaces visible bytes with a `<<ccr:` pointer
        // AND writes the store EAGERLY inside `compact()`. Composed here for the same any-order reason as the store wiring above.
        let compaction = match compaction {
            Some(mut stage) if self.config.lossless_only => {
                stage.config.substitute_opaque = false;
                Some(stage)
            }
            stage => stage,
        };
        // Default the routing tokenizer to a gpt-4o tiktoken counter when the caller did not supply one.
        let tokenizer = self
            .tokenizer
            .unwrap_or_else(|| crate::tokenizer::get_tokenizer(DEFAULT_ROUTING_TOKENIZER_MODEL));
        SmartCrusher::from_parts(
            self.config,
            anchor_selector,
            scorer,
            analyzer,
            compaction,
            self.ccr_store,
            tokenizer,
        )
    }
}

/// Default model name handed to `get_tokenizer` for the `MinTokens` routing decision when no tokenizer is supplied to the
/// builder. The choice only compares two renders relative to each other so this default never changes WHICH render is correct
pub const DEFAULT_ROUTING_TOKENIZER_MODEL: &str = "gpt-4o";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_builder_builds_with_default_scorer() {
        // An all-defaults builder still produces a working crusher via
        // the build-time scorer/tokenizer fallbacks.
        let crusher = SmartCrusherBuilder::new(SmartCrusherConfig::default()).build();
        let _ = crusher.crush(r#"[1, 2, 3]"#, "", 1.0);
    }

    #[test]
    fn with_default_oss_setup_builds() {
        let crusher = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .build();
        let _ = crusher.crush(r#"[1, 2, 3]"#, "", 1.0);
    }
}
