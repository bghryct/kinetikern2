//! `Instant` where there is a clock, a zero-time stand-in on WebAssembly
//! (`std::time::Instant::now` panics on wasm32-unknown-unknown): timings
//! read 0 ms there, nothing else changes.

#[cfg(not(target_arch = "wasm32"))]
pub use std::time::Instant;

#[cfg(target_arch = "wasm32")]
#[derive(Clone, Copy, Debug)]
pub struct Instant;

#[cfg(target_arch = "wasm32")]
impl Instant {
    pub fn now() -> Instant {
        Instant
    }

    pub fn elapsed(&self) -> std::time::Duration {
        std::time::Duration::ZERO
    }
}
