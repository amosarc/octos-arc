//! Task model: atomic unit of work.

use std::path::PathBuf;

use chrono::{DateTime, Local};
use serde::{Deserialize, Serialize};

use crate::types::{Message, TaskId};

/// A task is an atomic unit of work assigned to an agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    /// Unique identifier.
    pub id: TaskId,
    /// Current status.
    pub status: TaskStatus,
    /// What kind of task this is.
    pub kind: TaskKind,
    /// Context passed to the agent.
    pub context: TaskContext,
    /// When the task was created.
    pub created_at: DateTime<Local>,
    /// When the task was last updated.
    pub updated_at: DateTime<Local>,
}

impl Task {
    /// Create a new task with the given kind and context.
    pub fn new(kind: TaskKind, context: TaskContext) -> Self {
        let now = chrono::Local::now();
        Self {
            id: TaskId::new(),
            status: TaskStatus::Pending,
            kind,
            context,
            created_at: now,
            updated_at: now,
        }
    }
}

/// Task execution status.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum TaskStatus {
    /// Waiting to be assigned.
    Pending,
    /// Successfully completed.
    Completed,
    /// Failed with an error.
    Failed { error: String },
}

/// What kind of work the task represents.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TaskKind {
    /// Plan: decompose a goal into subtasks.
    Plan { goal: String },
    /// Code: write or modify code.
    Code {
        instruction: String,
        files: Vec<PathBuf>,
    },
    /// Review: review code changes.
    Review { diff: String },
    /// Test: write tests for a target or run test verification. For
    /// test-writing tasks `instruction` carries the brief (the spawn
    /// task description), so recall and reporting keep the full context.
    Test { instruction: String },
    /// Custom task type.
    Custom {
        name: String,
        params: serde_json::Value,
    },
}

/// Context passed to an agent when executing a task.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TaskContext {
    /// Working directory for the task.
    pub working_dir: PathBuf,
    /// Recent conversation turns (working memory).
    pub working_memory: Vec<Message>,
}

/// Current durable ABI schema version for [`TaskResult`].
///
/// See `docs/OCTOS_HARNESS_ABI_VERSIONING.md` for the stable and experimental
/// fields per version and the deprecation rules. The harness also exposes
/// this constant as `octos_agent::TASK_RESULT_SCHEMA_VERSION`.
pub const TASK_RESULT_SCHEMA_VERSION: u32 = 1;

fn default_task_result_schema_version() -> u32 {
    TASK_RESULT_SCHEMA_VERSION
}

/// Result of task execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskResult {
    /// Durable ABI schema version for this result. Defaults to
    /// [`TASK_RESULT_SCHEMA_VERSION`] when absent so consumers of older
    /// payloads continue to parse.
    #[serde(default = "default_task_result_schema_version")]
    pub schema_version: u32,
    /// Whether the task succeeded.
    pub success: bool,
    /// Output or summary.
    pub output: String,
    /// Files that were modified.
    pub files_modified: Vec<PathBuf>,
    /// Files explicitly declared for delivery back to the user.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files_to_send: Vec<PathBuf>,
    /// Subtasks created (for Plan tasks).
    pub subtasks: Vec<TaskId>,
    /// Token usage.
    pub token_usage: TokenUsage,
}

/// Token usage statistics.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TokenUsage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    /// Tokens used for internal chain-of-thought (reasoning models).
    #[serde(default, skip_serializing_if = "is_zero")]
    pub reasoning_tokens: u32,
    /// Tokens served from provider cache.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub cache_read_tokens: u32,
    /// Tokens written to provider cache.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub cache_write_tokens: u32,
}

fn is_zero(v: &u32) -> bool {
    *v == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_task_new() {
        let task = Task::new(
            TaskKind::Plan {
                goal: "test goal".to_string(),
            },
            TaskContext::default(),
        );

        assert_eq!(task.status, TaskStatus::Pending);
    }

    #[test]
    fn test_task_status_serialization() {
        let status = TaskStatus::Failed {
            error: "boom".to_string(),
        };
        let json = serde_json::to_string(&status).unwrap();
        assert!(json.contains("failed"));
        assert!(json.contains("boom"));

        let parsed: TaskStatus = serde_json::from_str(&json).unwrap();
        assert!(matches!(parsed, TaskStatus::Failed { .. }));
    }

    #[test]
    fn test_task_kind_serialization() {
        let kind = TaskKind::Code {
            instruction: "fix bug".to_string(),
            files: vec![PathBuf::from("src/main.rs")],
        };
        let json = serde_json::to_string(&kind).unwrap();
        assert!(json.contains("code"));
        assert!(json.contains("fix bug"));

        let parsed: TaskKind = serde_json::from_str(&json).unwrap();
        assert!(matches!(parsed, TaskKind::Code { .. }));
    }

    #[test]
    fn test_token_usage_default() {
        let usage = TokenUsage::default();
        assert_eq!(usage.input_tokens, 0);
        assert_eq!(usage.output_tokens, 0);
    }

    #[test]
    fn test_task_result_serde() {
        let result = TaskResult {
            schema_version: TASK_RESULT_SCHEMA_VERSION,
            success: true,
            output: "all tests pass".to_string(),
            files_modified: vec![PathBuf::from("src/main.rs")],
            files_to_send: vec![PathBuf::from("output/report.md")],
            subtasks: vec![],
            token_usage: TokenUsage {
                input_tokens: 100,
                output_tokens: 50,
                ..Default::default()
            },
        };
        let json = serde_json::to_string(&result).unwrap();
        let parsed: TaskResult = serde_json::from_str(&json).unwrap();
        assert!(parsed.success);
        assert_eq!(parsed.output, "all tests pass");
        assert_eq!(
            parsed.files_to_send,
            vec![PathBuf::from("output/report.md")]
        );
        assert_eq!(parsed.token_usage.input_tokens, 100);
        assert_eq!(parsed.schema_version, TASK_RESULT_SCHEMA_VERSION);
    }

    #[test]
    fn should_default_missing_task_result_schema_version_to_v1() {
        // TaskResult JSON emitted before M4.6 — no schema_version line.
        let legacy = r#"{
            "success": true,
            "output": "ok",
            "files_modified": [],
            "subtasks": [],
            "token_usage": {"input_tokens": 0, "output_tokens": 0}
        }"#;
        let parsed: TaskResult = serde_json::from_str(legacy).expect("legacy result parses");
        assert_eq!(parsed.schema_version, TASK_RESULT_SCHEMA_VERSION);
        assert!(parsed.success);
    }

    #[test]
    fn test_task_context_default() {
        let ctx = TaskContext::default();
        assert_eq!(ctx.working_dir, PathBuf::new());
        assert!(ctx.working_memory.is_empty());
    }
}
