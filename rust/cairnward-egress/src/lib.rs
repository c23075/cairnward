//! Cairnward's egress data path in Rust (ADR-0020): the address checks (stage 1) and the proxy,
//! which asks the Python decision service for policy, approvals and audit records (stage 2).

pub mod addresses;
pub mod decisions;
pub mod http;
pub mod inspect;
pub mod proxy;
