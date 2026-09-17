//! Authentication module: paste-token and keychain flows.

pub mod keychain;
pub mod store;

pub use keychain::KEYCHAIN_MARKER;
pub use store::{AuthCredential, AuthStore};
