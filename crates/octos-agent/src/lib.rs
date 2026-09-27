//! Agent runtime, tool execution, and coordination for octos.
//!
//! This crate provides:
//! - Agent struct that runs the agent loop
//! - Tool router for dispatching tool calls
//! - Command policy for approval before execution
//! - Progress reporting for real-time updates
//! - Integration with codex sandboxing (when enabled)

pub mod abi_schema;
mod agent;
pub mod agents;
pub mod approval;
pub mod behaviour;
pub mod compaction;
pub mod context_log;
pub mod file_state_cache;
pub mod format;
pub mod harness_errors;
pub mod harness_events;
pub mod hooks;
pub mod loop_detect;
pub mod mcp;
pub mod mcp_auth;
pub mod memory_segment;
pub mod policy;
pub mod profile;
pub mod progress;
pub mod prompt_context;
pub mod prompt_guard;
pub mod role_template;
pub mod sandbox;
mod sanitize;
mod shell_analysis;
pub mod skills;
pub mod snapshot;
pub mod subagent_output;
pub mod subagent_summary;
mod subprocess_env;
pub use subprocess_env::{register_secret_env_names, sanitize_default_subprocess_env};
pub mod summarizer;
pub mod task_supervisor;

#[cfg(test)]
#[path = "test_support.rs"]
pub(crate) mod test_support;
pub mod tools;
pub mod workspace_contract;
pub mod workspace_policy;
/// #48b — stable prefix marking that a turn terminated because the
/// malformed tool-call self-correction budget was exhausted. The CLI's
/// terminal-error path `starts_with` this marker to emit the
/// `malformed_exhausted` OLP event INSTEAD of a generic turn_error row.
pub const MALFORMED_TOOLCALL_EXHAUSTED_MARKER: &str =
    "malformed tool-call feedback budget exhausted";

pub use abi_schema::{
    COMPACTION_POLICY_SCHEMA_VERSION, HARNESS_ERROR_SCHEMA_VERSION,
    HARNESS_PROGRESS_EVENT_SCHEMA_VERSION, HOOK_PAYLOAD_SCHEMA_VERSION,
    SUB_AGENT_DISPATCH_SCHEMA_VERSION, UnsupportedSchemaVersionError,
    WORKSPACE_POLICY_SCHEMA_VERSION, check_supported,
};
pub use agent::{
    Agent, AgentConfig, AssistantSegmentProvenance, ConversationResponse,
    DEFAULT_SESSION_TIMEOUT_SECS, DEFAULT_TOOL_TIMEOUT_SECS, DEFAULT_WORKER_PROMPT,
    IncompleteResponseError, MAX_TOOL_TIMEOUT_SECS, PartialTurnUsage, PromptSegmentProvider,
    TASK_REPORTER, TokenTracker,
    loop_state::{
        LoopDecision, LoopRetryCounters, LoopRetryLimits, LoopRetryState, OCTOS_LOOP_RETRY_TOTAL,
        SHELL_SPIRAL_VARIANT,
    },
    memory::MIN_EPISODE_SIMILARITY,
    normalize_tool_call_id,
};
pub use approval::{
    ApprovalRequestEnvelope, ApprovalRequestSpec, ApprovalRiskLevel, ApprovalRule,
    ApprovalTimeoutBehavior, HumanApprovalRules, PendingApprovalDraft,
};
pub use file_state_cache::{
    FILE_UNCHANGED_STUB_PREFIX, FileStateCache, format_file_unchanged_stub,
};
pub use harness_errors::{HarnessError, HarnessErrorEvent, OCTOS_LOOP_ERROR_TOTAL, RecoveryHint};
pub use harness_events::{
    HARNESS_EVENT_SCHEMA_V1, HarnessEvent, HarnessEventError, HarnessEventPayload,
    HarnessEventSink, HarnessFailureEvent, HarnessPhaseEvent, HarnessProgressEvent,
    HarnessRetryEvent, HarnessSubAgentDispatchEvent, HarnessSubagentProgressEvent,
    MAX_HARNESS_EVENT_LINE_BYTES,
};
pub use hooks::{
    HookConfig, HookContext, HookDeniedError, HookEvent, HookExecutor, HookPayload, HookResult,
};
pub use mcp::{McpClient, McpServerConfig};
pub use memory_segment::{
    MEMORY_CAPTURE_POLICY, MEMORY_SEGMENT_NAME, MemorySegmentProvider, compose_memory_segment,
    stable_memory_instructions, volatile_memory_content,
};
pub use policy::{
    ApprovalPolicy, EffectivePermissions, FileAccessMode, FilesystemScope, NetworkPolicy,
    PermissionProfile, PermissionProfileError, RuntimeMode,
};
pub use progress::{ProgressEvent, ProgressReporter, SilentReporter};
pub use prompt_context::{
    PromptContextManager, PromptContextPhase, PromptContextReport, PromptContextRequest,
};
pub use role_template::{
    APPROVAL_ASK, APPROVAL_NEVER, ModelPreference, ROLE_EXPLORER, ROLE_IMPLEMENTER, ROLE_REVIEWER,
    ROLE_TEST_WORKER, RoleTemplate, RoleTemplateSummary, SANDBOX_AUTO, SANDBOX_NONE,
    UnknownModelPreference,
};
pub use sandbox::{Sandbox, SandboxConfig, SandboxMode, create_sandbox};
pub use skills::{SkillFilter, SkillInfo, SkillsLoader};
pub use snapshot::{
    DEFAULT_SNAPSHOT_KEEP_LAST, SnapshotConfig, SnapshotId, SnapshotInfo, SnapshotManager,
};
pub use subagent_output::{
    AppendResult, DEFAULT_MAX_BYTES_PER_TASK, DEFAULT_MAX_BYTES_TOTAL, DEFAULT_PREVIEW_BYTES,
    SubAgentOutputRouter,
};
pub use subagent_summary::{
    AgentSummaryGenerator, DEFAULT_SUBAGENT_SUMMARY_MIN_RUNTIME, DEFAULT_SUBAGENT_SUMMARY_TICK,
    DEFAULT_SUBAGENT_SUMMARY_WINDOW, SubAgentSummaryRegistry, SubAgentSummaryWatcher,
};
pub use summarizer::{ExtractiveSummarizer, Summarizer};
pub use task_supervisor::{
    BackgroundTask, RegisterTaskError, RelaunchOpts, SpawnOnlyFailureSignal, TaskCancelError,
    TaskCancelToken, TaskLifecycleState, TaskRelaunchError, TaskRuntimeState, TaskStatus,
    TaskSupervisor, TaskTerminalGuard, parse_alternatives,
};
pub use tools::{
    AskUserQuestionTool, BackgroundResultKind, BackgroundResultPayload, CheckBackgroundTasksTool,
    ConcurrencyClass, DEFAULT_DISPATCH_TIMEOUT_SECS, DEFAULT_HTTP_CONNECT_TIMEOUT_SECS,
    DEFAULT_HTTP_READ_TIMEOUT_SECS, DiffEditTool, DispatchContextContract, DispatchOutcome,
    DispatchRequest, DispatchResponse, EditFileTool, GlobTool, GrepTool, HttpMcpAgent, ListDirTool,
    McpAgentBackend, McpAgentBackendConfig, MemoryNoteTool, PolicyDecision, ReadFileTool,
    ReadTaskOutputTool, RecallMemoryTool, RecordMemoryUseTool, SaveMemoryTool, SendFileTool,
    SharedBackend, ShellTool, SpawnTool, StdioMcpAgent, Tool, ToolApprovalDecision,
    ToolApprovalRequest, ToolApprovalRequester, ToolPolicy, ToolRegistry, ToolResult,
    TurnAttachmentContext, UserQuestionOutcome, UserQuestionRequest, UserQuestionRequester,
    WriteFileTool, build_backend_from_config, build_dispatch_event_payload, dispatch_with_metrics,
    record_dispatch,
};
pub use workspace_policy::{
    CompactionPolicy, CompactionSummarizerKind, WORKSPACE_POLICY_FILE, WorkspaceArtifactsPolicy,
    WorkspacePolicy, WorkspacePolicyKind, WorkspaceSpawnTaskPolicy, read_workspace_policy,
    workspace_policy_path, write_workspace_policy,
};

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn setup_test_dir() -> TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("test.rs"),
            "fn main() {\n    println!(\"hello\");\n}\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("lib.rs"),
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
        )
        .unwrap();
        std::fs::create_dir(dir.path().join("src")).unwrap();
        std::fs::write(
            dir.path().join("src/module.rs"),
            "// Module\npub struct Foo;\n",
        )
        .unwrap();
        dir
    }

    #[tokio::test]
    async fn test_glob_tool() {
        let dir = setup_test_dir();
        let tool = GlobTool::new(dir.path());

        let result = tool
            .execute(&serde_json::json!({"pattern": "*.rs"}))
            .await
            .unwrap();

        assert!(result.success);
        assert!(result.output.contains("test.rs"));
        assert!(result.output.contains("lib.rs"));
    }

    #[tokio::test]
    async fn test_grep_tool() {
        let dir = setup_test_dir();
        let tool = GrepTool::new(dir.path());

        let result = tool
            .execute(&serde_json::json!({"pattern": "println"}))
            .await
            .unwrap();

        assert!(result.success);
        assert!(result.output.contains("test.rs"));
        assert!(result.output.contains("println"));
    }

    #[tokio::test]
    async fn test_read_file_tool() {
        let dir = setup_test_dir();
        let tool = ReadFileTool::new(dir.path());

        let result = tool
            .execute(&serde_json::json!({"path": "test.rs"}))
            .await
            .unwrap();

        assert!(result.success);
        assert!(result.output.contains("fn main()"));
    }

    #[tokio::test]
    async fn test_write_file_tool() {
        let dir = setup_test_dir();
        let tool = WriteFileTool::new(dir.path());

        let result = tool
            .execute(&serde_json::json!({
                "path": "new_file.rs",
                "content": "// New file\n"
            }))
            .await
            .unwrap();

        assert!(result.success);
        assert!(result.file_modified.is_some());
        assert!(dir.path().join("new_file.rs").exists());
    }

    #[tokio::test]
    async fn test_registry_execute() {
        let dir = setup_test_dir();
        let registry = ToolRegistry::with_builtins(dir.path());

        let result = registry
            .execute("read_file", &serde_json::json!({"path": "test.rs"}))
            .await
            .unwrap();

        assert!(result.success);
        assert!(result.output.contains("fn main()"));
    }

    #[tokio::test]
    async fn test_oversized_args_rejected() {
        let dir = setup_test_dir();
        let registry = ToolRegistry::with_builtins(dir.path());

        // Create args larger than 1MB
        let big_string = "x".repeat(1_100_000);
        let result = registry
            .execute("read_file", &serde_json::json!({"path": big_string}))
            .await;

        match result {
            Err(e) => assert!(e.to_string().contains("too large")),
            Ok(_) => panic!("should reject oversized args"),
        }
    }
}
