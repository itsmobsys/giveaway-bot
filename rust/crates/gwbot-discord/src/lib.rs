//! Discord gateway adapter: slash commands, buttons, the tick loop.
//!
//! Everything that decides *what* happens lives in `gwbot-core`; this crate
//! only translates gateway events into those calls and the results back into
//! Discord payloads.

pub mod bot;
pub mod convert;

pub use bot::run_bot;
