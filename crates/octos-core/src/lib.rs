//! Core types, task model, and protocols for octos.
//!
//! This crate defines the foundational types used across all octos crates:
//! - Task model (Task, TaskStatus, TaskKind)
//! - Agent roles and identifiers
//! - Message protocol between agents
//! - Context and result types

pub mod app_ui_codec;
pub mod env_hygiene;
pub mod gateway;
pub mod git_worktree;
pub mod secret_redaction;
pub mod session_scope;
mod task;
mod types;
pub mod ui_protocol;
mod utils;

pub use env_hygiene::{
    BLOCKED_ENV_VARS, is_registered_secret_env_name, is_secret_env_name, register_secret_env_names,
    sanitize_git_command_env,
};
pub use gateway::{InboundMessage, MessageOrigin, OutboundMessage};
pub use git_worktree::clear_worktree_admin_entry;
pub use session_scope::{
    DEFAULT_MULTI_TENANT_SHARED_ZONE_NAMES, MULTI_TENANT_USERS_DIR_NAME,
    MULTI_TENANT_WORKSPACE_DIR_NAME, PathClassification, ScopeMode, SessionScope,
    SessionScopeError, canonical_root_lossy, canonicalize_lossy, canonicalize_skill_read_zones,
    is_safe_session_id,
};
pub use task::{
    TASK_RESULT_SCHEMA_VERSION, Task, TaskContext, TaskKind, TaskResult, TaskStatus, TokenUsage,
};
pub use types::{
    AgentId, ClientMessageId, MAIN_PROFILE_ID, Message, MessageRole, SessionKey, TaskId, ThreadId,
    ToolCall, TurnId, is_reserved_channel_name,
};
pub use utils::{
    TruncatedBy, TruncationReport, safe_filename, tool_output_limit, truncate_head_tail_report,
    truncate_utf8, truncated_utf8,
};
