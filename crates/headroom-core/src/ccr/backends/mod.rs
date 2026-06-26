//! Pluggable CCR backends — in-memory (test default) and SQLite (prod
//! default).
//!
//! Selection is driven by [`CcrBackendConfig`]. The [`from_config`]
//! factory surfaces every backend-init failure to the caller — there is
//! no silent fallback to the in-memory backend. A `redis` backend was
//! removed (never shipped, never CI-tested); requesting it now fails
//! loudly with `UnsupportedBackend` rather than being silently dropped.

pub mod in_memory;
pub mod sqlite;

use std::path::PathBuf;

use thiserror::Error;

use crate::ccr::CcrStore;

pub use in_memory::InMemoryCcrStore;
pub use sqlite::SqliteCcrStore;

/// Operator-visible configuration for the CCR backend. Mirrors the
/// shape the runtime passes in from the runtime config
/// (`CcrConfig.backend = "sqlite" | "redis" | "in_memory"`).
#[derive(Debug, Clone)]
pub enum CcrBackendConfig {
    /// In-memory (test default). Bounded LRU; lost on restart.
    InMemory { capacity: usize, ttl_seconds: u64 },
    /// SQLite-backed (prod default). DB file at `path`; persistent.
    Sqlite { path: PathBuf, ttl_seconds: u64 },
    /// Redis-backed (multi-worker). The redis backend was removed (never
    /// shipped, never CI-tested); `from_config` always rejects this
    /// variant with `UnsupportedBackend`. Kept so a `backend = "redis"`
    /// config fails loudly rather than being silently dropped.
    Redis {
        url: String,
        ttl_seconds: u64,
        /// Key prefix; defaults to `"ccr"` when `None`.
        key_prefix: Option<String>,
    },
}

impl CcrBackendConfig {
    /// Production default: SQLite at `path`, 5-minute TTL.
    pub fn sqlite_default(path: PathBuf) -> Self {
        Self::Sqlite {
            path,
            ttl_seconds: crate::ccr::DEFAULT_TTL.as_secs(),
        }
    }

    /// In-memory with library defaults. Useful in tests.
    pub fn in_memory_default() -> Self {
        Self::InMemory {
            capacity: crate::ccr::DEFAULT_CAPACITY,
            ttl_seconds: crate::ccr::DEFAULT_TTL.as_secs(),
        }
    }
}

/// Reasons `from_config` may fail. Each variant is loud and recoverable
/// at the startup boundary — the operator is told exactly what
/// went wrong rather than silently degrading to in-memory.
#[derive(Debug, Error)]
pub enum CcrBackendInitError {
    /// SQLite open / schema-create failed.
    #[error("ccr sqlite backend init failed: {0}")]
    Sqlite(#[from] rusqlite::Error),
    /// Operator selected a backend whose feature flag was not compiled
    /// in. Loud failure rather than silent fallback.
    #[error(
        "ccr backend `{backend}` is not compiled in; rebuild with `--features {feature}` \
         or pick a different backend"
    )]
    UnsupportedBackend {
        backend: &'static str,
        feature: &'static str,
    },
}

/// Construct a CCR backend from `config`. Errors surface — never falls
/// back silently. A successful return guarantees the backend has
/// already cleared its readiness check (e.g. SQLite schema is in place,
/// Redis PING returned PONG).
pub fn from_config(config: &CcrBackendConfig) -> Result<Box<dyn CcrStore>, CcrBackendInitError> {
    match config {
        CcrBackendConfig::InMemory {
            capacity,
            ttl_seconds,
        } => {
            let store = InMemoryCcrStore::with_capacity_and_ttl(
                *capacity,
                std::time::Duration::from_secs(*ttl_seconds),
            );
            tracing::info!(
                target = "ccr.backend",
                backend = "in_memory",
                capacity = *capacity,
                ttl_seconds = *ttl_seconds,
                "ccr_backend_initialized"
            );
            Ok(Box::new(store))
        }
        CcrBackendConfig::Sqlite { path, ttl_seconds } => {
            let store = SqliteCcrStore::open(path, *ttl_seconds)?;
            tracing::info!(
                target = "ccr.backend",
                backend = "sqlite",
                path = %path.display(),
                ttl_seconds = *ttl_seconds,
                "ccr_backend_initialized"
            );
            Ok(Box::new(store))
        }
        CcrBackendConfig::Redis { .. } => Err(CcrBackendInitError::UnsupportedBackend {
            backend: "redis",
            feature: "redis",
        }),
    }
}
