//! furl-core: foundation crate for the Rust port of Furl.

pub mod ccr;
pub mod relevance;
pub mod signals;
pub mod tokenizer;
pub mod transforms;

/// Identity stub used by downstream crates and the Python binding to verify
/// linkage end-to-end.
pub fn hello() -> &'static str {
    "furl-core"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hello_returns_crate_name() {
        assert_eq!(hello(), "furl-core");
    }
}
