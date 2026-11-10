//! Cairnward's egress data path in Rust (ADR-0020). Stage 1 holds the destination address
//! checks; the proxy itself arrives in stage 2, behind `[egress] implementation = "rust"`.

pub mod addresses;
