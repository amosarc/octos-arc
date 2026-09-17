//! Session persistence, resume sanitization, and file-handle resolution for
//! the octos chat/serve runtime.

pub mod file_handle;
pub mod session;

pub use session::{
    MessageCommitObserver, Session, SessionHandle, SessionManager,
    persist_message_through_canonical_path, set_scoped_message_commit_observer,
    validate_topic_name,
};
