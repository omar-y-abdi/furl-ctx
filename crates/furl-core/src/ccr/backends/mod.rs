//! In-memory CCR backend.
//!
//! The CCR store keeps the original payload of every dropped/substituted row
//! keyed by the hash that lands in the prompt, so the runtime can serve it
//! back on a retrieval call. One backend ships: [`InMemoryCcrStore`], a
//! process-local sharded `DashMap`, constructed once at startup and shared
//! across worker threads behind an `Arc`. Entries are lost on restart — CCR
//! recovery is scoped to the process / request window (see `CCR-RETENTION.md`
//! for the deferred durable-backend epic).

pub mod in_memory;

pub use in_memory::InMemoryCcrStore;
