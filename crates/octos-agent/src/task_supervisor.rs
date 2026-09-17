//! Background task lifecycle management for spawn_only tools.
//!
//! The `TaskSupervisor` is a status store that tracks background tasks from
//! spawn to completion. It does NOT enforce workspace contracts — that
//! responsibility belongs to `workspace_contract::enforce_spawn_task_contract`,
//! which runs inline in `execution.rs` BEFORE the supervisor status is
//! updated.
//!
//! The supervisor only sees truth-checked states: `Completed` means the
//! workspace contract was satisfied, `Failed` means it was not.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use chrono::{DateTime, Utc};
use metrics::counter;
use octos_core::TaskId;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::harness_events::{HarnessEvent, HarnessEventPayload};
use crate::progress::{ProgressEvent, ProgressReporter};


/// Cap on the number of child tasks any single parent session may register
/// in the supervisor. Hit by the mini4/river runaway: a pipeline node spawned
/// 65,535 children into a single session before the host disk filled up.
///
/// Beyond this cap [`TaskSupervisor::try_register_with_input`] returns
/// [`RegisterTaskError::ChildFanoutExceeded`], the legacy
/// `register*` entry points return an empty-string sentinel, and every
/// currently-active child of that parent is force-marked `Failed` with a
/// structured reason so the runaway loop's downstream registers see the
/// poisoned state and stop submitting.
///
/// Override at process start by setting the `OCTOS_MAX_CHILDREN_PER_PARENT`
/// env var to a positive integer; the value is parsed once and cached.
pub const MAX_CHILDREN_PER_PARENT: usize = 200;

/// Codex round-2 MAJOR (PR #1324): upper bound on `AckAndPending::pending`
/// entries before the oldest stash is evicted. Sized generously so that
/// even a fully cascaded pipeline (one pending entry per child task,
/// `MAX_CHILDREN_PER_PARENT = 200`) plus a 56-entry headroom for unrelated
/// stashes still fits without eviction in normal operation. The cap is
/// load-bearing in pathological flows where the synth-ack never arrives
/// (sibling-error suppression + the task never completes/cancels), so
/// without it the map would grow until the supervisor is dropped.
///
/// When the cap is exceeded the oldest entry is evicted and a WARN is
/// logged so operators can spot stuck flows.
const MAX_PENDING_FAILURES: usize = 256;

/// Cap on the bytes of worker final-output text stored on a
/// [`BackgroundTask`] (and thus persisted per task-ledger snapshot line).
/// 128 KiB comfortably holds a multi-page report while bounding ledger
/// growth; longer outputs are truncated with a marker by
/// [`TaskSupervisor::record_final_output`].
const FINAL_OUTPUT_CAP_BYTES: usize = 128 * 1024;

/// Codex round-2 MAJOR (PR #1324): upper bound on
/// `AckAndPending::emitted_task_ids` before the oldest entry is evicted.
/// Sized at 4× the pending cap so a long-running supervisor that never
/// shuts down still cannot grow this set without bound. The set's only
/// role is per-task idempotency on the signal callback, so evicting
/// stale entries after thousands of fires is safe — the task has long
/// since terminated and its task_id is not reused.
const MAX_FAILURE_SIGNAL_EMITTED_IDS: usize = 1024;

fn max_children_per_parent() -> usize {
    static CACHE: OnceLock<usize> = OnceLock::new();
    *CACHE.get_or_init(|| {
        std::env::var("OCTOS_MAX_CHILDREN_PER_PARENT")
            .ok()
            .and_then(|raw| raw.parse::<usize>().ok())
            .filter(|cap| *cap > 0)
            .unwrap_or(MAX_CHILDREN_PER_PARENT)
    })
}

/// Error variants for [`TaskSupervisor::try_register_with_input`] and the
/// other strict registration entry points. Currently all callers map this to
/// a structured failure log; the variant stays an enum so we can grow new
/// rejection reasons (e.g. shutdown, quota) without breaking the public API.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RegisterTaskError {
    /// The parent session already has at least `cap` registered children
    /// (active + terminal). The runaway-prevention cap fired; the caller
    /// must surface this as a tool failure rather than re-trying.
    ChildFanoutExceeded {
        parent_session_key: String,
        count: usize,
        cap: usize,
    },
}

impl std::fmt::Display for RegisterTaskError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ChildFanoutExceeded {
                parent_session_key,
                count,
                cap,
            } => write!(
                f,
                "child fanout exceeded ({count} of {cap}) for parent session '{parent_session_key}'"
            ),
        }
    }
}

impl std::error::Error for RegisterTaskError {}

/// Lifecycle status of a background task.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Spawned,
    Running,
    Completed,
    Failed,
    /// M7.9 / W2: task was cancelled mid-flight via the supervisor's
    /// `cancel()` primitive (e.g. `POST /api/tasks/{id}/cancel`).
    /// Terminal — `is_active()` returns false. Distinguished from
    /// `Failed` so dashboards can surface "user cancelled" instead of
    /// "the task crashed".
    Cancelled,
}

impl TaskStatus {
    pub fn is_active(&self) -> bool {
        matches!(self, Self::Spawned | Self::Running)
    }

    /// Whether this status is a terminal (non-recoverable, non-running)
    /// state. Used by the API layer to reject `cancel`/`restart` against
    /// already-terminal tasks with a `409 Conflict` response.
    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Cancelled)
    }

    pub fn as_str(&self) -> &str {
        match self {
            Self::Spawned => "spawned",
            Self::Running => "running",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }
}

/// Structured terminal outcome for a child session.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChildSessionTerminalState {
    Completed,
    RetryableFailure,
    TerminalFailure,
}

/// Join state for a child session contract.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChildSessionJoinState {
    Joined,
    Orphaned,
}

/// Explicit follow-up policy for terminal child-session failures.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChildSessionFailureAction {
    Retry,
    Escalate,
}

/// Fine-grained runtime phase of a background task.
///
/// `status` remains the coarse externally stable summary, while
/// `runtime_state` tracks where the task is inside the workspace/runtime
/// lifecycle. This lets the agent and UI distinguish "tool is still running"
/// from "tool finished but outputs are still being verified/delivered".
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TaskRuntimeState {
    Spawned,
    ExecutingTool,
    ResolvingOutputs,
    VerifyingOutputs,
    DeliveringOutputs,
    CleaningUp,
    Completed,
    Failed,
    /// M7.9 / W2: runtime state for tasks cancelled via the supervisor's
    /// `cancel()` primitive. Surfaced via `mark_cancelled`.
    Cancelled,
}

/// Stable externally-facing lifecycle state for background tasks.
///
/// This is the coarse public contract that callers and UIs should consume.
/// It intentionally groups several internal runtime phases under `verifying`
/// so the runtime can evolve without leaking extra state-machine detail.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TaskLifecycleState {
    Queued,
    Running,
    Verifying,
    Ready,
    Failed,
    /// M7.9 / W2: stable cancelled lifecycle for UI / API dashboards.
    Cancelled,
}

/// A tracked background task spawned by a spawn_only tool.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackgroundTask {
    pub id: String,
    pub tool_name: String,
    pub tool_call_id: String,
    /// Parent session that owns this task.
    pub parent_session_key: Option<String>,
    /// Stable child session key derived from the parent session and task id.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub child_session_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub child_terminal_state: Option<ChildSessionTerminalState>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub child_join_state: Option<ChildSessionJoinState>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub child_joined_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub child_failure_action: Option<ChildSessionFailureAction>,
    pub status: TaskStatus,
    pub runtime_state: TaskRuntimeState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime_detail: Option<String>,
    pub started_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
    pub output_files: Vec<String>,
    pub error: Option<String>,
    /// Full final output text of the worker, recorded via
    /// [`TaskSupervisor::record_final_output`] just before the terminal
    /// transition and capped at [`FINAL_OUTPUT_CAP_BYTES`]. A background
    /// spawn child's transcript never flows through the
    /// `SubAgentOutputRouter` (only spawn_only tools append to it), so
    /// without this field `read_task_output` on such a task read an absent
    /// router file and returned an empty string — models concluded the
    /// child's result "was lost" and re-did or overwrote its work.
    /// `#[serde(default)]` so pre-existing persisted snapshots deserialize
    /// unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_output: Option<String>,
    /// True when the current `Failed` status came from an OBSERVER — the
    /// harness-event bridge (`apply_harness_event`) classifying a mid-run
    /// error as fatal — rather than from the task's owner (the spawn join /
    /// completer that actually watches the worker finish). Observer verdicts
    /// are provisional: the owner's `mark_completed` may override them (the
    /// worker demonstrably survived), while owner-reported failures and
    /// `Cancelled` stay final. `#[serde(default)]` so pre-existing persisted
    /// snapshots deserialize unchanged.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub failed_by_observer: bool,
    /// Session that owns this task (for per-session filtering).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_key: Option<String>,
    /// Original tool arguments — preserved so failure-recovery flows can
    /// surface the exact input the LLM passed when offering alternatives.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_input: Option<Value>,
    /// Issue #738 fix: the `client_message_id` of the user turn that
    /// originated this background task. Captured at register time so the
    /// M8.9 failure-recovery synthetic turn can inherit the same cmid
    /// (instead of the recovery turn minting a fresh server UUIDv7 that
    /// the SPA has no DOM bubble for, leaving the eventual successful
    /// retry's deliverables stranded under an orphan thread_id).
    /// `#[serde(default)]` so tasks persisted before this field was added
    /// still deserialize as `None`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub originating_client_message_id: Option<String>,

    // ── #966 / M13-B projection fields ──────────────────────────────
    // The AppUI TaskListEntry already accepts these optional fields
    // (see octos-cli `TaskListProjection`); populating them here at
    // register-time threads them into `task/list` and `task/updated`
    // payloads so clients can render bounded child-task UX without
    // probing free-form text. All five use `#[serde(default)]` so
    // pre-M13-B persisted snapshots still deserialize as None.
    /// Origin of this task: `"model"` (LLM scheduled via
    /// spawn_agent/spawn/delegate), `"supervisor"` (backend), or
    /// `"user"` (rare).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    /// Role label assigned at spawn — mirrors M14-C role templates
    /// (`"reviewer"`, `"implementer"`, `"test_worker"`, `"test_writer"`, `"explorer"`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// Bounded summary capsule mirroring ChildResultSummary.summary
    /// for terminal children.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    /// Number of artifacts emitted so far so UX can badge tasks
    /// without resolving task/artifact/list.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub artifact_count: Option<u32>,
    /// Effective runtime policy stamp captured at spawn — clients
    /// rendering reconnect hydration should display the stamp the
    /// task originally announced, not the current session policy.
    /// Stored as raw JSON so the agent crate doesn't depend on the
    /// AppUI `runtime_policy_stamp` schema.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub runtime_policy_stamp: Option<Value>,
    /// Opaque host projection metadata. The supervisor persists this value
    /// without interpreting its schema, allowing API adapters to derive
    /// domain-specific views from the canonical task lifecycle.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub projection_metadata: Option<Value>,
    /// #1707 round 5 codex round 2 (board item #13 round 2) — the MASTER
    /// session's workspace root, captured at registration time.
    ///
    /// The background-task agent mirror derives its `cwd` stamp from this
    /// value (falling back to the legacy `output_files[0]` parent-dir
    /// derivation when absent), so the continuation queue's workspace stamps
    /// — which the `/stop` terminal purge matches against the interrupted
    /// turn's `session_runtime.workspace_root` — share ONE source with the
    /// purge argument instead of depending on how the task completed
    /// (`retire_peer_supervised_task` completes with EMPTY output files →
    /// `cwd=None`; orphan adoption completes with `<profile-data>/peers/
    /// <slug>/result.md` → `cwd=<…>/peers/<slug>` — NEITHER equalled the
    /// master's workspace root, so the `/stop` purge matched ZERO
    /// `peer_handoff` items in production).
    ///
    /// `#[serde(default)]` so pre-existing persisted snapshots deserialize
    /// unchanged; `None` preserves the legacy derivation bit-for-bit.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_root: Option<String>,
}

impl BackgroundTask {
    pub fn lifecycle_state(&self) -> TaskLifecycleState {
        match self.status {
            TaskStatus::Spawned => TaskLifecycleState::Queued,
            TaskStatus::Completed => TaskLifecycleState::Ready,
            TaskStatus::Failed => TaskLifecycleState::Failed,
            TaskStatus::Cancelled => TaskLifecycleState::Cancelled,
            // #27c — awaiting client re-attach: not queued (no worker here),
            // not failed (the work is recoverable). Reuse `Cancelled`'s
            // idle lifecycle slot so dashboards read it as "stopped",
            // with the `parked` status string carrying the distinction.
            TaskStatus::Running => match self.runtime_state {
                TaskRuntimeState::Spawned | TaskRuntimeState::ExecutingTool => {
                    TaskLifecycleState::Running
                }
                TaskRuntimeState::ResolvingOutputs
                | TaskRuntimeState::VerifyingOutputs
                | TaskRuntimeState::DeliveringOutputs
                | TaskRuntimeState::CleaningUp
                | TaskRuntimeState::Completed => TaskLifecycleState::Verifying,
                TaskRuntimeState::Failed => TaskLifecycleState::Failed,
                TaskRuntimeState::Cancelled => TaskLifecycleState::Cancelled,
            },
        }
    }
}

/// Callback invoked when a task's status changes.
type OnChangeCallback = Arc<dyn Fn(&BackgroundTask) + Send + Sync>;


/// Payload emitted when a `spawn_only` background task transitions to
/// `Failed`. Consumers (e.g. the session actor) use this to schedule a
/// synthetic recovery turn so the LLM can re-engage with an actionable
/// error and offer alternatives instead of leaving the user stuck on a
/// terminal-only failure notification.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SpawnOnlyFailureSignal {
    /// Background task identifier (matches `BackgroundTask::id`).
    pub task_id: String,
    /// Tool that failed (e.g. `fm_tts`).
    pub tool_name: String,
    /// The original tool arguments passed by the LLM when invoking the tool.
    /// May be `Value::Null` if the input was not captured for this task.
    pub tool_input: Value,
    /// The textual error reported by the tool, contract validator, or wrapper.
    pub error_message: String,
    /// Best-effort list of alternatives extracted from the error text via the
    /// `available: X, Y, Z` pattern. Empty when no alternatives were detected.
    pub suggested_alternatives: Vec<String>,
    /// Owning session, when the failed task is bound to one.
    pub parent_session_key: Option<String>,
    /// Issue #738 fix: the `client_message_id` of the user turn that
    /// originated this spawn_only task. Threaded end-to-end so the
    /// synthetic recovery `InboundMessage` built by the session actor
    /// inherits the original turn's cmid — without it, `process_inbound`
    /// mints a fresh server UUIDv7 and the eventual successful retry's
    /// deliverables (e.g. `_report.md`) land under an orphan thread_id
    /// with no DOM bubble in the SPA. `None` for legacy callers that
    /// pre-date the field; receivers must tolerate that.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub originating_client_message_id: Option<String>,
}

/// Callback invoked when a `spawn_only` task fails. Receives the structured
/// signal payload so consumers can build a recovery prompt without re-parsing
/// the raw `BackgroundTask`.
type OnFailureCallback = Box<dyn Fn(&SpawnOnlyFailureSignal) + Send + Sync>;

/// Options for `TaskSupervisor::relaunch`. Mirrors the
/// `POST /api/tasks/{id}/restart-from-node` request body.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct RelaunchOpts {
    /// When set, the supervisor relaunches starting at this DOT-graph node id
    /// (so upstream cached outputs are reused). When `None` the relaunch
    /// re-runs the entire task from scratch.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub from_node: Option<String>,
}


/// Error variants for [`TaskSupervisor::cancel`]. Mapped to HTTP status
/// codes by the API layer:
/// - `NotFound` → `404`
/// - `AlreadyTerminal` → `409`
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskCancelError {
    NotFound,
    AlreadyTerminal,
}

impl std::fmt::Display for TaskCancelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotFound => write!(f, "task not found"),
            Self::AlreadyTerminal => write!(f, "task is already in a terminal state"),
        }
    }
}

impl std::error::Error for TaskCancelError {}

/// Error variants for [`TaskSupervisor::relaunch`]. Mapped to HTTP status
/// codes by the API layer:
/// - `NotFound` → `404`
/// - `StillActive` → `409`
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskRelaunchError {
    NotFound,
    StillActive,
}

impl std::fmt::Display for TaskRelaunchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotFound => write!(f, "task not found"),
            Self::StillActive => {
                write!(f, "task is still active; cancel it before relaunching")
            }
        }
    }
}

impl std::error::Error for TaskRelaunchError {}

/// Per-task cancel token map. Each entry pairs an `AtomicBool` (loop-poll
/// flag) and an optional `tokio::sync::Notify` so cooperatively cancelable
/// futures (e.g. `select!` on a long-running pipeline) can race against
/// `cancelled.notified()` instead of polling.
#[derive(Default)]
struct CancelTokenStore {
    tokens: Mutex<HashMap<String, Arc<TaskCancelToken>>>,
}

impl CancelTokenStore {
    fn ensure(&self, task_id: &str) -> Arc<TaskCancelToken> {
        let mut guard = self.tokens.lock().unwrap_or_else(|e| e.into_inner());
        guard
            .entry(task_id.to_string())
            .or_insert_with(|| Arc::new(TaskCancelToken::new()))
            .clone()
    }
}

/// Per-task cancel token. Workers poll `is_cancelled()` at safe points and
/// long-running futures can `select!` on `notified()` to short-circuit
/// pending I/O.
pub struct TaskCancelToken {
    cancelled: AtomicBool,
    notify: tokio::sync::Notify,
}

impl TaskCancelToken {
    fn new() -> Self {
        Self {
            cancelled: AtomicBool::new(false),
            notify: tokio::sync::Notify::new(),
        }
    }

    /// Whether the token has been triggered. Safe-point poll for in-loop
    /// pipeline workers.
    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }

    /// Trigger cancellation. Idempotent — a second call is a no-op.
    pub fn cancel(&self) {
        if !self.cancelled.swap(true, Ordering::AcqRel) {
            self.notify.notify_waiters();
        }
    }

    /// Wait for the token to fire. Useful for `select!` against a
    /// long-running future.
    pub async fn cancelled(&self) {
        self.cancelled_after_first_check(|| {}).await;
    }

    async fn cancelled_after_first_check<F>(&self, after_first_check: F)
    where
        F: FnOnce(),
    {
        if self.is_cancelled() {
            return;
        }
        after_first_check();
        let notified = self.notify.notified();
        tokio::pin!(notified);
        if self.is_cancelled() {
            return;
        }
        notified.await;
    }
}

impl std::fmt::Debug for TaskCancelToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TaskCancelToken")
            .field("cancelled", &self.is_cancelled())
            .finish()
    }
}

/// Extract a list of alternatives from a tool error message using the simple
/// `available: X, Y, Z` pattern. Returns an empty vector when no match is
/// found so callers can fall back to surfacing the raw error text.
///
/// This is intentionally conservative — we only handle the canonical
/// "available: ..." phrasing emitted by the fm_tts/voice-skill family. More
/// aggressive parsing belongs in the failure-modes inventory follow-up.
pub fn parse_alternatives(error_text: &str) -> Vec<String> {
    // Use a literal scan rather than a regex so we don't pull in a fresh
    // dependency or risk pathological backtracking. The marker is
    // case-insensitive and matched anywhere in the message.
    let needle = "available:";
    let lower = error_text.to_lowercase();
    let Some(start) = lower.find(needle) else {
        return Vec::new();
    };
    let tail = &error_text[start + needle.len()..];

    // Stop at the first sentence boundary so we don't grab the entire
    // remainder of the error message. Newlines and periods both terminate
    // the alternatives clause.
    let stop = tail.find(['\n', '.', ';']).unwrap_or(tail.len());
    let clause = &tail[..stop];

    clause
        .split(',')
        .map(|item| item.trim().trim_matches(['"', '\'']))
        .filter(|item| !item.is_empty())
        .map(|item| item.to_string())
        .collect()
}

fn record_child_session_lifecycle(kind: &'static str, outcome: &'static str) {
    counter!(
        "octos_child_session_lifecycle_total",
        "kind" => kind.to_string(),
        "outcome" => outcome.to_string()
    )
    .increment(1);
}

fn record_child_session_orphan(reason: &'static str) {
    counter!(
        "octos_child_session_orphan_total",
        "reason" => reason.to_string()
    )
    .increment(1);
}

/// Process-global set of task ids whose DETACHED worker is currently live in
/// THIS process. Armed by [`TaskTerminalGuard`] (insert in the foreground
/// before `tokio::spawn`, cleared on its `Drop` on every worker exit path).
///
/// Sole consumer: the per-parent child-fanout cap in `register_full`, which
/// counts "status-active OR worker-still-running" children — status alone
/// flips to `Cancelled` immediately while the worker keeps executing, so a
/// spawn→cancel-all→spawn cycle would run 2× the cap without this set.
fn live_detached_tasks() -> &'static Mutex<HashSet<String>> {
    static LIVE: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    LIVE.get_or_init(|| Mutex::new(HashSet::new()))
}

/// Record that the detached worker for `task_id` is live in this process.
/// Called by [`TaskTerminalGuard::new`]; idempotent.
fn mark_task_live(task_id: &str) {
    live_detached_tasks()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(task_id.to_string());
}

/// Remove `task_id` from the live-set once its worker terminates. Called by
/// [`TaskTerminalGuard`]'s `Drop` on every exit path; idempotent.
fn clear_task_live(task_id: &str) {
    live_detached_tasks()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .remove(task_id);
}

/// Whether the detached worker for `task_id` is currently live in this process.
/// Used by the orphan-sweep filter to skip still-live detached tasks.
fn is_task_live(task_id: &str) -> bool {
    live_detached_tasks()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .contains(task_id)
}

fn record_workflow_phase_transition(workflow_kind: &str, from_phase: &str, to_phase: &str) {
    counter!(
        "octos_workflow_phase_transition_total",
        "workflow_kind" => workflow_kind.to_string(),
        "from_phase" => from_phase.to_string(),
        "to_phase" => to_phase.to_string()
    )
    .increment(1);
}

fn workflow_labels(detail: Option<&str>) -> (Option<String>, Option<String>) {
    let parsed = detail
        .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
        .unwrap_or(Value::Null);
    let workflow_kind = parsed
        .get("workflow_kind")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    let current_phase = parsed
        .get("current_phase")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    (workflow_kind, current_phase)
}

fn child_terminal_kind_label(state: &ChildSessionTerminalState) -> &'static str {
    match state {
        ChildSessionTerminalState::Completed => "completed",
        ChildSessionTerminalState::RetryableFailure => "retryable_failed",
        ChildSessionTerminalState::TerminalFailure => "terminal_failed",
    }
}

fn child_join_outcome_label(state: &ChildSessionJoinState) -> &'static str {
    match state {
        ChildSessionJoinState::Joined => "joined",
        ChildSessionJoinState::Orphaned => "orphaned",
    }
}

fn child_failure_action_for_terminal_state(
    state: &ChildSessionTerminalState,
) -> Option<ChildSessionFailureAction> {
    match state {
        ChildSessionTerminalState::Completed => None,
        ChildSessionTerminalState::RetryableFailure => Some(ChildSessionFailureAction::Retry),
        ChildSessionTerminalState::TerminalFailure => Some(ChildSessionFailureAction::Escalate),
    }
}

// Background-task artifact validation lives in `workspace_contract.rs` (the
// per-skill workspace contract layer) and in the skill itself. The
// supervisor used to second-guess that result with its own size/magic/
// silence/duration checks, but the duplication produced false positives
// (mini5 serena-TTS, 2026-05-12: real speech rejected because the 4 KB
// leading-window silence sampler only saw the TTS preamble silence) and
// was a layer violation — the supervisor only needs to know whether the
// skill reported success or failure, not whether the bytes look right.

impl std::fmt::Debug for TaskSupervisor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let progress_reporter_attached = self
            .progress_reporter
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_some();
        f.debug_struct("TaskSupervisor")
            .field("tasks", &self.tasks)
            .field("on_change", &"<callback>")
            .field("on_failure", &"<callback>")
            .field("progress_reporter", &progress_reporter_attached)
            .finish()
    }
}

/// Human-readable label for a [`TaskRuntimeState`] used by the supervisor's
/// `ProgressReporter` bridge. The text is suffixed onto `<tool>: ` so the
/// chat UI can anchor a single bubble per tool_call_id and surface what the
/// background task is currently doing without inventing per-tool plumbing.
fn runtime_state_label(state: &TaskRuntimeState) -> &'static str {
    match state {
        TaskRuntimeState::Spawned => "spawned",
        TaskRuntimeState::ExecutingTool => "running",
        TaskRuntimeState::ResolvingOutputs => "resolving outputs",
        TaskRuntimeState::VerifyingOutputs => "verifying outputs",
        TaskRuntimeState::DeliveringOutputs => "delivering outputs",
        TaskRuntimeState::CleaningUp => "cleaning up",
        TaskRuntimeState::Completed => "completed",
        TaskRuntimeState::Failed => "failed",
        TaskRuntimeState::Cancelled => "cancelled",
    }
}

/// Supervisor that tracks background task lifecycle.
///
/// Thread-safe via interior `Mutex`. Cloning shares the same underlying state.
#[derive(Clone)]
pub struct TaskSupervisor {
    tasks: Arc<Mutex<HashMap<String, BackgroundTask>>>,
    /// Set of parent session keys that have hit the per-parent child cap
    /// (see [`MAX_CHILDREN_PER_PARENT`]). Once a parent is poisoned every
    /// subsequent register call short-circuits to refuse so the runaway
    /// loop cannot keep adding children.
    poisoned_parents: Arc<Mutex<HashSet<String>>>,
    on_change: Arc<Mutex<Option<OnChangeCallback>>>,
    /// Named observers supplement (and never replace) the primary callback.
    on_change_listeners: Arc<Mutex<HashMap<String, OnChangeCallback>>>,
    on_failure: Arc<Mutex<Option<OnFailureCallback>>>,
    /// Optional reporter that receives a [`ProgressEvent::ToolProgress`]
    /// for every supervised state transition. Wired by the agent's
    /// spawn_only branch so chat UIs can anchor progress strictly to the
    /// originating `tool_call_id` (the chat-bubble contract enforced by
    /// the SSE `tool_call_id` field on `tool_progress` frames).
    ///
    /// Synchronous tool calls never go through the supervisor, so this
    /// bridge naturally fires only on background-task transitions —
    /// there is no double-emission to worry about for the normal tool
    /// path that already reports its own ToolStarted/ToolCompleted.
    progress_reporter: Arc<Mutex<Option<Arc<dyn ProgressReporter>>>>,
    /// M7.9: per-task cancellation tokens. The `cancel(task_id)` primitive
    /// flips the matching token so cooperative pipeline / spawn workers can
    /// short-circuit at their next safe point. Tokens are created lazily on
    /// `register*` and dropped on terminal transitions to keep memory usage
    /// proportional to active tasks.
    cancel_tokens: Arc<CancelTokenStore>,
    /// Codex round-2 BLOCKER (PR #1324 follow-up): unified state for the
    /// synth-ack gate, pending failure stash, and per-task idempotency
    /// guard. All three were previously separate `Mutex`es, which left a
    /// narrow ack/pending interleaving race:
    ///
    /// 1. `notify_failure` checks `synth_ack_emitted.contains` → false.
    /// 2. `mark_synth_ack_emitted` inserts the ack AND drains the (still
    ///    empty) pending map.
    /// 3. `notify_failure` inserts its pending entry — too late to be
    ///    drained.
    /// 4. The pending stash sits forever; recovery signal lost.
    ///
    /// Folding all three collections under one mutex makes the
    /// "check-ack-then-stash" pair atomic with the "record-ack-then-
    /// drain" pair. The hot path is recovery signaling, which is
    /// infrequent, so the mutex contention is not a perf concern.
    ///
    /// See [`AckAndPending`] for the field-level documentation that
    /// previously lived on the individual fields.
    ack_and_pending: Arc<Mutex<AckAndPending>>,
}

/// Combined state guarded by a single mutex (Codex round-2 BLOCKER):
/// the synth-ack set, the deferred-failure stash, and the per-task
/// idempotency set must move together so the ack/pending interleaving
/// race is impossible.
#[derive(Debug, Default)]
struct AckAndPending {
    /// Set of `tool_call_id`s for which the spawn_only "Background work
    /// started for `<tool>`." synth-ack was actually emitted to the LLM
    /// (see `loop_runner.rs` synth-ack gate).
    ///
    /// This is the load-bearing gate for the post-spawn failure feedback
    /// loop: `notify_failure` only fires `SpawnOnlyFailureSignal` for tasks
    /// whose `tool_call_id` is in this set. Two failure modes that skip
    /// the signal correctly:
    ///
    /// 1. **Pre-flight validation failures** (`Tool::pre_flight_validate`
    ///    returned `Err`) early-return BEFORE supervisor registration, so
    ///    `mark_failed` is never called for them. The LLM already saw the
    ///    synchronous `[VALIDATION FAILED]` tool_result — no recovery
    ///    needed.
    /// 2. **Sibling-error suppression**: a spawn_only sibling tool in the
    ///    same batch errored, so the synth-ack gate suppressed the ack
    ///    (see `any_tool_invocation_errored`). The spawn_only task IS
    ///    registered (tokio::spawn happened) but the LLM already saw the
    ///    sibling's error and will react on its next iteration. Injecting
    ///    a recovery prompt for the spawn_only's eventual post-spawn
    ///    failure would double-signal the LLM.
    ///
    /// When the synth-ack DID fire, the LLM was told success — the
    /// post-spawn failure is the only way it can learn the truth. That
    /// is the gap this set closes.
    synth_ack_emitted_tool_call_ids: HashSet<String>,
    /// Codex round-4 BLOCKER (PR #1324 follow-up): two-phase failure
    /// emission.
    ///
    /// `tokio::spawn` in `execution.rs::handle_spawn_only_branch` (line
    /// ~493) dispatches the background task BEFORE `loop_runner.rs`
    /// records the synth-ack at line ~1356. A fast post-spawn failure
    /// (e.g. plugin binary missing, instant validator rejection) can
    /// fire `notify_failure` while `synth_ack_emitted_tool_call_ids`
    /// is still empty. Holding both collections under the same mutex
    /// (see [`TaskSupervisor::ack_and_pending`]) makes ack-record + drain
    /// atomic with ack-check + insert, eliminating the interleaving race.
    ///
    /// When `notify_failure` observes "ack not yet recorded", it
    /// stashes the would-be `SpawnOnlyFailureSignal` here keyed by the
    /// supervisor's unique `task_id`. The value carries the
    /// associated `tool_call_id` so `mark_synth_ack_emitted(tc_id)`
    /// can scan and drain ALL pending tasks under that
    /// `tool_call_id` — important for pipeline cascade, where many
    /// child tasks share the parent's `tool_call_id`.
    ///
    /// Codex round-2 MAJOR: bounded by [`MAX_PENDING_FAILURES`] with FIFO
    /// eviction (oldest first); `pending_insertion_order` records insert
    /// order so eviction is O(1).
    pending: HashMap<String, PendingFailure>,
    /// FIFO insertion order for `pending`. When `pending.len()` exceeds
    /// [`MAX_PENDING_FAILURES`] the front entry is evicted to keep the
    /// map bounded under pathological flows (ack never arrives + task
    /// never completes/cancels).
    pending_insertion_order: VecDeque<String>,
    /// Companion to `pending` (Codex round-4 BLOCKER): tracks unique
    /// `task_id`s for which the failure callback already fired.
    /// Keyed by `task_id` (not `tool_call_id`) because pipeline
    /// cascades have many tasks under the same `tool_call_id` and
    /// each child must fire its own signal (see
    /// `mark_descendants_failed_emits_progress_and_failure_signal_per_child`).
    /// Guards the deferred-emission replay path and a sibling
    /// `mark_failed` for the same task so each task fires at most one
    /// `SpawnOnlyFailureSignal`.
    ///
    /// Codex round-2 MAJOR: bounded by
    /// [`MAX_FAILURE_SIGNAL_EMITTED_IDS`] with FIFO eviction; the
    /// `emitted_insertion_order` queue records insert order so
    /// eviction is O(1). Stale entries (>1024 fires) are safe to
    /// evict — the task has long since terminated and `task_id` is a
    /// UUID never reused.
    emitted_task_ids: HashSet<String>,
    /// FIFO insertion order for `emitted_task_ids`.
    emitted_insertion_order: VecDeque<String>,
}

impl AckAndPending {
    /// Insert a pending failure under `task_id`. If the map already
    /// holds [`MAX_PENDING_FAILURES`] entries, evict the oldest entry
    /// first and log a WARN so operators can spot stuck flows. Returns
    /// `Some(evicted)` when an eviction happened.
    fn insert_pending(&mut self, task_id: String, value: PendingFailure) -> Option<PendingFailure> {
        // If the key is already present, refresh in place — no
        // ordering change so we do not re-queue.
        if let Some(slot) = self.pending.get_mut(&task_id) {
            *slot = value;
            return None;
        }
        let evicted = if self.pending.len() >= MAX_PENDING_FAILURES {
            // Pop the oldest entry that still has a live map slot.
            // `pending_insertion_order` can contain stale ids (drained
            // out of the map directly) so skip those.
            loop {
                match self.pending_insertion_order.pop_front() {
                    Some(stale) => {
                        if let Some(victim) = self.pending.remove(&stale) {
                            tracing::warn!(
                                evicted_task_id = %stale,
                                evicted_tool_call_id = %victim.tool_call_id,
                                cap = MAX_PENDING_FAILURES,
                                "evicting oldest pending spawn_only failure stash: cap exceeded",
                            );
                            break Some(victim);
                        }
                    }
                    None => break None,
                }
            }
        } else {
            None
        };
        self.pending.insert(task_id.clone(), value);
        self.pending_insertion_order.push_back(task_id);
        evicted
    }

    /// Remove a pending entry by `task_id` AND its companion entry
    /// from `pending_insertion_order` so the FIFO queue cannot grow
    /// without bound.
    ///
    /// Codex round-3 MAJOR (PR #1324 follow-up): the round-2 cap only
    /// fires when `pending.len()` exceeds [`MAX_PENDING_FAILURES`]. In
    /// the common fail-before-ack → ack-drain cycle the HashMap
    /// returns to zero each round, so the cap is never hit and the
    /// VecDeque grows linearly forever. Popping the matching id here
    /// keeps both collections in lockstep.
    fn remove_pending(&mut self, task_id: &str) -> Option<PendingFailure> {
        let removed = self.pending.remove(task_id);
        if removed.is_some() {
            self.forget_pending_in_order(task_id);
        }
        removed
    }

    /// Drain every pending failure matching `tool_call_id`. Returns
    /// the drained entries; the insertion-order queue is updated in
    /// lockstep so the VecDeque cannot leak across drain cycles.
    ///
    /// Codex round-3 MAJOR (PR #1324 follow-up): same leak class as
    /// `remove_pending` — when no eviction is ever triggered, the
    /// `pending_insertion_order` queue would otherwise accumulate one
    /// entry per failure forever. Pop in lockstep here.
    fn drain_pending_for_tool_call(&mut self, tool_call_id: &str) -> Vec<PendingFailure> {
        let mut hits = Vec::new();
        let mut drained_ids: Vec<String> = Vec::new();
        self.pending.retain(|task_id, pf| {
            if pf.tool_call_id == tool_call_id {
                drained_ids.push(task_id.clone());
                hits.push(pf.clone());
                false // remove
            } else {
                true // keep
            }
        });
        for task_id in &drained_ids {
            self.forget_pending_in_order(task_id);
        }
        hits
    }

    /// Remove `task_id` from `pending_insertion_order` if present.
    /// `VecDeque::remove` is O(n) but the deque is bounded at
    /// [`MAX_PENDING_FAILURES`] (256), so the linear scan is cheap.
    fn forget_pending_in_order(&mut self, task_id: &str) {
        if let Some(pos) = self
            .pending_insertion_order
            .iter()
            .position(|tid| tid == task_id)
        {
            self.pending_insertion_order.remove(pos);
        }
    }

    /// Mark `task_id` as having dispatched its failure signal. Returns
    /// `true` if this is the first dispatch (caller should proceed to
    /// invoke the callback), `false` if a previous path already
    /// dispatched (caller must suppress). Bounded by
    /// [`MAX_FAILURE_SIGNAL_EMITTED_IDS`] with FIFO eviction.
    fn mark_emitted(&mut self, task_id: &str) -> bool {
        if !self.emitted_task_ids.insert(task_id.to_string()) {
            return false;
        }
        self.emitted_insertion_order.push_back(task_id.to_string());
        while self.emitted_task_ids.len() > MAX_FAILURE_SIGNAL_EMITTED_IDS {
            if let Some(stale) = self.emitted_insertion_order.pop_front() {
                if self.emitted_task_ids.remove(&stale) {
                    tracing::warn!(
                        evicted_task_id = %stale,
                        cap = MAX_FAILURE_SIGNAL_EMITTED_IDS,
                        "evicting oldest emitted-failure-signal id: cap exceeded",
                    );
                }
            } else {
                break;
            }
        }
        true
    }

}

/// Pending failure entry — see the field-level doc on
/// `AckAndPending::pending`.
#[derive(Debug, Clone)]
struct PendingFailure {
    /// The `tool_call_id` of the failed task. Used by
    /// `mark_synth_ack_emitted` to identify which pending entries to
    /// drain when an ack arrives for that id.
    tool_call_id: String,
    /// The failure signal payload that `notify_failure` would have
    /// dispatched if the synth-ack had already been recorded.
    signal: SpawnOnlyFailureSignal,
}

impl Default for TaskSupervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskSupervisor {
    /// Create an empty supervisor.
    pub fn new() -> Self {
        Self {
            tasks: Arc::new(Mutex::new(HashMap::new())),
            poisoned_parents: Arc::new(Mutex::new(HashSet::new())),
            on_change: Arc::new(Mutex::new(None)),
            on_change_listeners: Arc::new(Mutex::new(HashMap::new())),
            on_failure: Arc::new(Mutex::new(None)),
            progress_reporter: Arc::new(Mutex::new(None)),
            cancel_tokens: Arc::new(CancelTokenStore::default()),
            ack_and_pending: Arc::new(Mutex::new(AckAndPending::default())),
        }
    }

    /// Record that the spawn_only synth-ack ("Background work started for
    /// `<tool>`.") was emitted to the LLM for `tool_call_id`. Called from
    /// the synth-ack gate in `loop_runner.rs` for every spawn_only tool
    /// call in a turn whose synth-ack actually fires (gated by
    /// `any_tool_invocation_errored`).
    ///
    /// The set is the load-bearing signal for the post-spawn failure
    /// feedback loop — see the field-level doc on
    /// `AckAndPending::synth_ack_emitted_tool_call_ids`. Idempotent.
    ///
    /// Codex round-4 BLOCKER (PR #1324 follow-up): after recording the
    /// ack, drain any pending failure for this `tool_call_id` and emit
    /// the `SpawnOnlyFailureSignal` NOW.
    ///
    /// Codex round-2 BLOCKER (PR #1324 follow-up): the ack-record +
    /// pending-drain pair happens under the SAME mutex as
    /// `notify_failure`'s ack-check + pending-insert pair. The previous
    /// design used two separate mutexes for `synth_ack_emitted` and
    /// `pending_failures`, leaving a narrow interleave where ack-check
    /// observes false, then drain runs against an empty map, then
    /// notify inserts pending and the stash sits forever. Folding both
    /// collections under [`AckAndPending`] makes the ordering atomic.
    pub fn mark_synth_ack_emitted(&self, tool_call_id: &str) {
        if tool_call_id.is_empty() {
            return;
        }
        // Atomic: insert ack AND drain any pending entries that
        // arrived before the ack was recorded. No interleaving with
        // `notify_failure`'s ack-check + pending-insert pair is
        // possible because they hold the same mutex.
        let drained: Vec<PendingFailure> = {
            let mut guard = self
                .ack_and_pending
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            guard
                .synth_ack_emitted_tool_call_ids
                .insert(tool_call_id.to_string());
            guard.drain_pending_for_tool_call(tool_call_id)
        };
        // Dispatch happens AFTER releasing the mutex — the failure
        // callback is user code that may take other locks (notably
        // `on_failure`), so we must not hold `ack_and_pending` across
        // it.
        for pf in drained {
            let task_id = pf.signal.task_id.clone();
            self.dispatch_failure_signal(&task_id, pf.signal);
        }
    }


    /// Set a callback that fires whenever a task's status changes.
    pub fn set_on_change(&self, cb: impl Fn(&BackgroundTask) + Send + Sync + 'static) {
        let mut guard = self.on_change.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(Arc::new(cb));
    }

    /// Set a callback that fires only when a `spawn_only` task transitions to
    /// `Failed`. This is the M8.9 hook the session actor uses to enqueue a
    /// synthetic recovery turn. The callback is only invoked once per failed
    /// task — re-marking a task as failed (or any subsequent state change)
    /// will not re-fire the signal.
    pub fn set_on_failure_signal(
        &self,
        cb: impl Fn(&SpawnOnlyFailureSignal) + Send + Sync + 'static,
    ) {
        let mut guard = self.on_failure.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(Box::new(cb));
    }




    /// #2055 review round 2 — copy the REGISTRATION observers from `parent`
    /// onto this (freshly created) supervisor: the `on_register` callback
    /// and the NAMED `on_change_listeners` map. Called wherever a child /
    /// nested supervisor is minted (`ToolRegistry::snapshot_excluding`, the
    /// nested-spawn child registry), so goal-ledger task rows cover nested
    /// subagent registrations instead of silently stopping at the first
    /// fresh supervisor.
    ///
    /// Deliberately NOT copied: the primary `on_change`, `on_failure`,
    /// `on_terminal`, and `on_relaunch` callbacks — their wake/continuation
    /// semantics are per-instance by design (a child's terminal event must
    /// not double-drive the parent's re-entry wiring). Existing entries
    /// under the same listener key are replaced; entries only the child has
    /// are kept. Both copies are `Arc` clones — cheap, and later re-wiring
    /// on either side does not affect the other.
    pub fn inherit_registration_observers(&self, parent: &TaskSupervisor) {
        let parent_listeners: Vec<(String, OnChangeCallback)> = parent
            .on_change_listeners
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(key, callback)| (key.clone(), Arc::clone(callback)))
            .collect();
        if !parent_listeners.is_empty() {
            let mut guard = self
                .on_change_listeners
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            for (key, callback) in parent_listeners {
                guard.insert(key, callback);
            }
        }
    }

    /// Attach a [`ProgressReporter`] that receives a
    /// [`ProgressEvent::ToolProgress`] for every supervised runtime-state
    /// transition. The emitted event carries the originating `tool_call_id`
    /// (`ProgressEvent::ToolProgress::tool_id`) so chat UIs can anchor every
    /// long-running spawn_only task to a single bubble — no per-tool plumbing
    /// required.
    ///
    /// Wired by the agent's spawn_only branch in `execution.rs`. Setting a
    /// reporter is idempotent; the latest reporter wins. Pass a
    /// [`crate::progress::SilentReporter`] to detach.
    pub fn set_progress_reporter(&self, reporter: Arc<dyn ProgressReporter>) {
        let mut guard = self
            .progress_reporter
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        *guard = Some(reporter);
    }


    /// Acquire (or create) the cancel token for `task_id`. Workers should
    /// call this once at the top of their critical section and then poll
    /// `is_cancelled()` at safe points. Returns a freshly allocated token
    /// for unknown task ids — callers that want strict membership checks
    /// should use `get_task` first.
    pub fn cancel_token(&self, task_id: &str) -> Arc<TaskCancelToken> {
        self.cancel_tokens.ensure(task_id)
    }

    /// Cancel a tracked task. Sets the per-task cancellation token (so
    /// in-loop workers can short-circuit at the next safe point) and
    /// transitions the supervisor record to `Cancelled`. Returns:
    ///
    /// - `Ok(())` when the task was running/queued and has now been
    ///   marked `Cancelled`.
    /// - `Err(TaskCancelError::NotFound)` when no task with that id is
    ///   tracked. Maps to `404` at the API edge.
    /// - `Err(TaskCancelError::AlreadyTerminal)` when the task is
    ///   already in a terminal state (`Completed` / `Failed` /
    ///   `Cancelled`). Maps to `409` at the API edge.
    pub fn cancel(&self, task_id: &str) -> Result<(), TaskCancelError> {
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            let task = tasks.get_mut(task_id).ok_or(TaskCancelError::NotFound)?;
            if task.status.is_terminal() {
                return Err(TaskCancelError::AlreadyTerminal);
            }
            task.status = TaskStatus::Cancelled;
            task.runtime_state = TaskRuntimeState::Cancelled;
            task.updated_at = Utc::now();
            task.completed_at = Some(Utc::now());
            if task.error.is_none() {
                task.error = Some("cancelled by supervisor".to_string());
            }
            task.clone()
        };

        // Trigger the cancel token AFTER the task has been marked
        // cancelled so any waiter that wakes can re-read the supervisor
        // and see the terminal state.
        let token = self.cancel_tokens.ensure(task_id);
        token.cancel();

        self.notify_change(&snapshot);
        self.emit_progress_for_state(&snapshot);
        // Codex round-4 BLOCKER (PR #1324 follow-up): if a cancelled
        // task happened to have a pending failure stash (defensive —
        // cancel + late mark_failed normally would no-op via the
        // terminal guard, but the entry could exist if mark_failed
        // landed before cancel transitioned the task), drop it so a
        // later mark_synth_ack_emitted doesn't surface a recovery
        // signal for a task the user / system already cancelled.
        self.drain_pending_failure_for_task(task_id);
        Ok(())
    }

    /// Relaunch a tracked task with the supplied options. Returns the
    /// freshly allocated `new_task_id` on success.
    ///
    /// The supervisor pre-registers the new task in the `Spawned` state
    /// (mirroring the original task's tool name / call id / session
    /// metadata) and fires `set_on_relaunch` so the runtime can drive the
    /// actual re-execution. When no relaunch callback is wired the call
    /// still succeeds — the new task id is returned so callers can
    /// observe the placeholder in dashboards even when the runtime
    /// owner has not subscribed yet.
    pub fn relaunch(&self, task_id: &str, opts: RelaunchOpts) -> Result<String, TaskRelaunchError> {
        let original = {
            let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            tasks
                .get(task_id)
                .cloned()
                .ok_or(TaskRelaunchError::NotFound)?
        };
        if matches!(original.status, TaskStatus::Running | TaskStatus::Spawned) {
            return Err(TaskRelaunchError::StillActive);
        }

        // Pre-allocate a successor task id and seed it on the supervisor
        // so dashboards see the relaunch as a peer of the original task.
        // Issue #738: carry the originating cmid forward so a relaunched
        // task that itself fails again still has the right thread anchor
        // for any synthetic recovery turn.
        let new_task_id = self.register_with_input_and_cmid(
            &original.tool_name,
            &original.tool_call_id,
            original.session_key.as_deref(),
            original.tool_input.clone(),
            original.originating_client_message_id.clone(),
        );

        // Stamp the lineage on the new task: callers can use
        // `runtime_detail` to surface the relaunch-from edge.
        let detail = serde_json::json!({
            "relaunched_from": task_id,
            "from_node": opts.from_node,
        })
        .to_string();
        self.mark_runtime_state(&new_task_id, TaskRuntimeState::Spawned, Some(detail));

        Ok(new_task_id)
    }

    /// Emit a [`ProgressEvent::ToolProgress`] for `task` if a reporter has
    /// been wired via [`Self::set_progress_reporter`]. The message is
    /// `"<tool_name>: <state-label>"`, with the task's `error` text appended
    /// in parentheses on `Failed` transitions so the UI can surface the
    /// reason without re-walking the supervisor's state.
    fn emit_progress_for_state(&self, task: &BackgroundTask) {
        let guard = self
            .progress_reporter
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let Some(reporter) = guard.as_ref().cloned() else {
            return;
        };
        drop(guard);
        let label = runtime_state_label(&task.runtime_state);
        let message = match task.runtime_state {
            TaskRuntimeState::Failed | TaskRuntimeState::Cancelled => match task.error.as_deref() {
                Some(reason) if !reason.is_empty() => {
                    format!("{}: {} ({})", task.tool_name, label, reason)
                }
                _ => format!("{}: {}", task.tool_name, label),
            },
            _ => format!("{}: {}", task.tool_name, label),
        };
        reporter.report(ProgressEvent::ToolProgress {
            name: task.tool_name.clone(),
            tool_id: task.tool_call_id.clone(),
            message,
        });
    }

    /// Register a new background task. Returns the generated task ID, or
    /// an empty-string sentinel when the parent's child fan-out cap fired
    /// (see [`MAX_CHILDREN_PER_PARENT`]). Callers that need strict
    /// rejection semantics should use [`Self::try_register_with_input`].
    pub fn register(
        &self,
        tool_name: &str,
        tool_call_id: &str,
        session_key: Option<&str>,
    ) -> String {
        self.register_with_lineage(tool_name, tool_call_id, session_key)
    }

    /// Register a new background task with optional ledger-path lineage.
    /// Returns an empty-string sentinel on cap rejection — see
    /// [`Self::try_register_with_input`] for strict semantics.
    pub fn register_with_lineage(
        &self,
        tool_name: &str,
        tool_call_id: &str,
        session_key: Option<&str>,
    ) -> String {
        match self.register_full(tool_name, tool_call_id, session_key, None, None) {
            Ok(id) => id,
            Err(error) => {
                tracing::error!(
                    tool = tool_name,
                    tool_call_id = session_key.unwrap_or(""),
                    session_key = ?session_key,
                    error = %error,
                    "task supervisor register_with_lineage refused; returning empty id"
                );
                String::new()
            }
        }
    }

    /// Register a new background task with optional ledger-path lineage and
    /// the original tool input. The tool input is preserved so failure
    /// signals can include it without re-walking the message history.
    /// Returns an empty-string sentinel on cap rejection — see
    /// [`Self::register`] for details.
    pub fn register_with_input(
        &self,
        tool_name: &str,
        tool_call_id: &str,
        session_key: Option<&str>,
        tool_input: Option<Value>,
    ) -> String {
        match self.register_full(tool_name, tool_call_id, session_key, tool_input, None) {
            Ok(id) => id,
            Err(error) => {
                tracing::error!(
                    tool = tool_name,
                    tool_call_id = tool_call_id,
                    session_key = ?session_key,
                    error = %error,
                    "task supervisor register_with_input refused (legacy entry point); returning empty id"
                );
                String::new()
            }
        }
    }

    /// Issue #738 fix: register a task and capture the originating user
    /// turn's `client_message_id`. The cmid is later threaded into any
    /// `SpawnOnlyFailureSignal` emitted for this task so the M8.9
    /// recovery `InboundMessage` keeps the original thread_id rather
    /// than minting an orphan UUIDv7.
    pub fn register_with_input_and_cmid(
        &self,
        tool_name: &str,
        tool_call_id: &str,
        session_key: Option<&str>,
        tool_input: Option<Value>,
        originating_client_message_id: Option<String>,
    ) -> String {
        match self.register_full(tool_name, tool_call_id, session_key, tool_input, originating_client_message_id) {
            Ok(id) => id,
            Err(error) => {
                tracing::error!(
                    tool = tool_name,
                    tool_call_id = tool_call_id,
                    session_key = ?session_key,
                    error = %error,
                    "task supervisor register_with_input_and_cmid refused (legacy entry point); returning empty id"
                );
                String::new()
            }
        }
    }

    /// Strict variant of [`Self::register_with_input`]: returns the typed
    /// [`RegisterTaskError`] on cap rejection so callers can surface a
    /// structured tool failure instead of swallowing the empty-string
    /// sentinel that the legacy entry points return for compatibility.
    pub fn try_register_with_input(
        &self,
        tool_name: &str,
        tool_call_id: &str,
        session_key: Option<&str>,
        tool_input: Option<Value>,
    ) -> Result<String, RegisterTaskError> {
        self.register_full(tool_name, tool_call_id, session_key, tool_input, None)
    }

    #[allow(clippy::too_many_arguments)]
    fn register_full(
        &self,
        tool_name: &str,
        tool_call_id: &str,
        session_key: Option<&str>,
        tool_input: Option<Value>,
        originating_client_message_id: Option<String>,
    ) -> Result<String, RegisterTaskError> {
        // Per-parent fan-out cap. Detached registrations (`session_key ==
        // None`) skip the gate because they do not have a parent to
        // attribute the count to — those are MCP/test bookkeeping calls
        // and stay capped only by host process memory.
        if let Some(parent_session_key) = session_key {
            let cap = max_children_per_parent();

            // Fast path: a previously-poisoned parent stays poisoned for the
            // lifetime of the supervisor so the runaway loop's downstream
            // registers see the rejection without re-counting.
            let already_poisoned = self
                .poisoned_parents
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .contains(parent_session_key);
            if already_poisoned {
                // Diagnostic count for the error payload — live children
                // (status-active OR worker-still-running), matching the
                // semantics of the gating count below.
                let count = self
                    .tasks
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .values()
                    .filter(|task| {
                        task.parent_session_key.as_deref() == Some(parent_session_key)
                            && (task.status.is_active() || is_task_live(&task.id))
                    })
                    .count();
                let error = RegisterTaskError::ChildFanoutExceeded {
                    parent_session_key: parent_session_key.to_string(),
                    count,
                    cap,
                };
                tracing::warn!(
                    parent_session_key = parent_session_key,
                    count,
                    cap,
                    "task supervisor refusing register: parent already poisoned"
                );
                record_child_session_lifecycle("tracked", "refused_poisoned");
                return Err(error);
            }

            // Run the per-session cap query under a SINGLE `tasks`
            // lock acquisition so the count and any subsequent side
            // effects observe a consistent snapshot.
            let current_count = {
                let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
                // Count LIVE children only. `tasks` is never pruned, so an
                // unfiltered count is the session's lifetime total — a
                // long-lived session that merely completed `cap` tasks over
                // its life would trip the cap, poison the key forever, and
                // force-fail its active work. The cap's job is to bound
                // runaway CONCURRENT fan-out; terminal children are done and
                // hold no resources this cap protects.
                //
                // "Live" is status-active OR worker-still-running (codex P2):
                // `cancel` flips the STATUS to Cancelled immediately, but the
                // detached worker keeps executing until it observes the token
                // — status alone would let a spawn→cancel-all→spawn cycle run
                // 2× the cap concurrently. The process-global live-set
                // (armed by `TaskTerminalGuard::new`, cleared on its Drop at
                // every worker exit path) tracks actual worker liveness.

                tasks
                    .values()
                    .filter(|task| {
                        task.parent_session_key.as_deref() == Some(parent_session_key)
                            && (task.status.is_active() || is_task_live(&task.id))
                    })
                    .count()
            };
            if current_count >= cap {
                // Mark the parent session as poisoned so subsequent
                // attempts fail fast without re-counting.
                self.poisoned_parents
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .insert(parent_session_key.to_string());

                let reason = format!("child fanout exceeded ({current_count} of {cap})");

                // Force-fail every still-active child of the runaway
                // parent so the cascade collapses instead of waiting on
                // each child to finish on its own. Snapshot the active
                // ids first so the per-id `mark_failed` does not deadlock
                // on the supervisor's `tasks` mutex.
                let active_children: Vec<String> = self
                    .tasks
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .values()
                    .filter(|task| {
                        task.parent_session_key.as_deref() == Some(parent_session_key)
                            && task.status.is_active()
                    })
                    .map(|task| task.id.clone())
                    .collect();
                for child_id in active_children {
                    self.mark_failed(&child_id, reason.clone());
                }

                let error = RegisterTaskError::ChildFanoutExceeded {
                    parent_session_key: parent_session_key.to_string(),
                    count: current_count,
                    cap,
                };
                tracing::error!(
                    parent_session_key = parent_session_key,
                    count = current_count,
                    cap,
                    "task supervisor refusing register: child fanout cap exceeded"
                );
                counter!(
                    "octos_task_supervisor_fanout_rejected_total",
                    "reason" => "child_fanout_exceeded".to_string()
                )
                .increment(1);
                return Err(error);
            }
        }

        let id = TaskId::new().to_string();
        let derived_child_session_key = session_key.map(|parent| format!("{parent}#child-{id}"));
        let task = BackgroundTask {
            id: id.clone(),
            tool_name: tool_name.to_string(),
            tool_call_id: tool_call_id.to_string(),
            parent_session_key: session_key.map(|s| s.to_string()),
            child_session_key: derived_child_session_key,
            child_terminal_state: None,
            child_join_state: None,
            child_joined_at: None,
            child_failure_action: None,
            status: TaskStatus::Spawned,
            runtime_state: TaskRuntimeState::Spawned,
            runtime_detail: None,
            started_at: Utc::now(),
            updated_at: Utc::now(),
            completed_at: None,
            output_files: Vec::new(),
            error: None,
            final_output: None,
            failed_by_observer: false,
            session_key: session_key.map(|s| s.to_string()),
            tool_input,
            originating_client_message_id,
            // #966 / M13-B — set None at register time. Callers that
            // know the spawn source/role (model vs supervisor, role
            // template, runtime policy stamp) populate via the new
            // `with_m13b_projection(...)` setter immediately after
            // `register_*`. Future supervisor refactors can thread
            // these through register_* directly when convenient.
            source: None,
            role: None,
            summary: None,
            artifact_count: None,
            runtime_policy_stamp: None,
            projection_metadata: None,
            workspace_root: None,
        };
        let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        // Publish the task only after its full initialization while holding
        // the task lock, so readers never observe an uncommitted registration.
        tasks.insert(id.clone(), task);
        drop(tasks);
        record_child_session_lifecycle(
            "tracked",
            if session_key.is_some() {
                "registered"
            } else {
                "detached"
            },
        );
        // #2055 — the single success path every `register*` entry point
        // funnels through: fire the registration observer AFTER the insert
        // and snapshot persist, with no locks held (see `notify_register`).
        Ok(id)
    }

    /// #966 / M13-B — attach the projection metadata (origin, role,
    /// summary, artifact count, runtime policy stamp) to an already-
    /// registered task. Designed for callers who already know how to
    /// derive each piece at spawn time but want to avoid expanding
    /// every `register_*` signature with five new optional args.
    /// Pass `None` for any field whose value is not yet known; the
    /// underlying [`BackgroundTask`] keeps any already-populated value
    /// when the corresponding argument is `None`.
    pub fn set_m13b_projection(
        &self,
        task_id: &str,
        source: Option<String>,
        role: Option<String>,
        summary: Option<String>,
        artifact_count: Option<u32>,
        runtime_policy_stamp: Option<Value>,
    ) {
        // Codex P2 fix: also persist + notify + emit_progress so the
        // reconnect-hydration and `task/updated` subscribers actually
        // observe the new metadata. Without this the projection fields
        // sit in-memory until some unrelated state change fires the
        // callbacks. Mirror the persist/notify/emit pattern used by
        // mark_running / mark_completed / cancel.
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            let Some(task) = tasks.get_mut(task_id) else {
                return;
            };
            let mut changed = false;
            if source.is_some() {
                task.source = source;
                changed = true;
            }
            if role.is_some() {
                task.role = role;
                changed = true;
            }
            if summary.is_some() {
                task.summary = summary;
                changed = true;
            }
            if artifact_count.is_some() {
                task.artifact_count = artifact_count;
                changed = true;
            }
            if runtime_policy_stamp.is_some() {
                task.runtime_policy_stamp = runtime_policy_stamp;
                changed = true;
            }
            if !changed {
                return;
            }
            // Stamp updated_at so reconnect hydration / dashboards see
            // the projection update even when no lifecycle transition
            // fires.
            task.updated_at = Utc::now();
            task.clone()
        };
        self.notify_change(&snapshot);
        self.emit_progress_for_state(&snapshot);
    }

    /// #1707 round 5 codex round 2 (board item #13 round 2) — stamp the
    /// MASTER session's workspace root onto an already-registered task.
    /// Same post-registration shape as [`Self::set_m13b_projection`]:
    /// keeps every `register_*` signature unchanged (octos-agent stays
    /// additive) while letting the registration site record the purge-side
    /// workspace value the background-task mirror derives `cwd` from. A
    /// `None` / empty value is ignored — the task keeps any existing stamp
    /// (and the legacy `output_files` derivation stays the fallback).
    pub fn set_workspace_root(&self, task_id: &str, workspace_root: Option<&str>) {
        let Some(workspace_root) = workspace_root.filter(|value| !value.is_empty()) else {
            return;
        };
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            let Some(task) = tasks.get_mut(task_id) else {
                return;
            };
            if task.workspace_root.as_deref() == Some(workspace_root) {
                return;
            }
            task.workspace_root = Some(workspace_root.to_string());
            task.updated_at = Utc::now();
            task.clone()
        };
        self.notify_change(&snapshot);
        self.emit_progress_for_state(&snapshot);
    }

    /// Attach (or replace) the tool input for an already-registered task.
    /// Useful when the task is registered eagerly and the args become
    /// available later in the spawn pipeline.
    pub fn set_tool_input(&self, task_id: &str, tool_input: Value) {
        let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(task) = tasks.get_mut(task_id) {
            task.tool_input = Some(tool_input);
        }
    }

    /// Mark a task as running.
    ///
    /// **M8 DoD gate (Req #4)**: this is a no-op when the task is already in
    /// a terminal state. Without the guard a worker that races with `cancel()`
    /// — e.g. cancel fires before the worker observes its cancel token, and
    /// the worker still calls `mark_running` — could resurrect a `Cancelled`
    /// task back to `Running`, undoing the user's cancellation.
    pub fn mark_running(&self, task_id: &str) {
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(task) = tasks.get_mut(task_id) {
                if task.status.is_terminal() {
                    tracing::warn!(
                        task_id = %task_id,
                        current_status = task.status.as_str(),
                        current_runtime_state = ?task.runtime_state,
                        attempted_status = TaskStatus::Running.as_str(),
                        "ignoring late mark_running: task already in terminal state",
                    );
                    return;
                }
                task.status = TaskStatus::Running;
                task.runtime_state = TaskRuntimeState::ExecutingTool;
                task.runtime_detail = None;
                task.updated_at = Utc::now();
                Some(task.clone())
            } else {
                None
            }
        };
        if let Some(ref task) = snapshot {
            self.notify_change(task);
            self.emit_progress_for_state(task);
        }
    }

    /// Update the fine-grained runtime state while keeping the coarse status.
    ///
    /// **M8 DoD gate (Req #4)**: this is a no-op when the task is already in
    /// a terminal state (`Completed`/`Failed`/`Cancelled`). A late harness
    /// event from a worker that already cancelled cannot otherwise flip the
    /// stored `runtime_state` away from `Cancelled`, leaking incorrect
    /// progress emissions and ledger snapshots.
    pub fn mark_runtime_state(
        &self,
        task_id: &str,
        runtime_state: TaskRuntimeState,
        runtime_detail: Option<String>,
    ) {
        let (snapshot, previous_detail) = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(task) = tasks.get_mut(task_id) {
                if task.status.is_terminal() {
                    tracing::warn!(
                        task_id = %task_id,
                        current_status = task.status.as_str(),
                        current_runtime_state = ?task.runtime_state,
                        attempted_runtime_state = ?runtime_state,
                        "ignoring late mark_runtime_state: task already in terminal state",
                    );
                    return;
                }
                let previous_detail = task.runtime_detail.clone();
                task.runtime_state = runtime_state;
                task.runtime_detail = runtime_detail;
                task.updated_at = Utc::now();
                (Some(task.clone()), previous_detail)
            } else {
                (None, None)
            }
        };
        if let Some(ref task) = snapshot {
            self.notify_change(task);
            self.emit_progress_for_state(task);
            let (previous_kind, previous_phase) = workflow_labels(previous_detail.as_deref());
            let (current_kind, current_phase) = workflow_labels(task.runtime_detail.as_deref());
            if let (Some(workflow_kind), Some(to_phase)) =
                (current_kind.as_deref(), current_phase.as_deref())
            {
                let from_phase = if previous_kind.as_deref() == Some(workflow_kind) {
                    previous_phase.as_deref().unwrap_or("untracked")
                } else {
                    "untracked"
                };
                if from_phase != to_phase {
                    record_workflow_phase_transition(workflow_kind, from_phase, to_phase);
                }
            }
        }
    }

    /// Mark a task as completed with output files.
    ///
    /// **M8 DoD gate (Req #4)**: this is a no-op when the task is already in a
    /// terminal state (`Completed`/`Cancelled`/owner-reported `Failed`). The
    /// check + write happen under the same lock as the rest of the supervisor
    /// so the guard is a CAS-style atomic transition. A late-arriving worker
    /// that finishes after the user has cancelled the task therefore *cannot*
    /// resurrect it to `Completed`. The race is logged at `warn` so operators
    /// can observe it.
    ///
    /// One exception: an OBSERVER-derived `Failed`
    /// ([`Self::mark_failed_observed`], set by the harness-event bridge for a
    /// mid-run fail-fast classification) IS overridden — the caller here is
    /// the owner that watched the worker actually finish, so its completion
    /// corrects the premature verdict.
    pub fn mark_completed(&self, task_id: &str, output_files: Vec<String>) {
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(task) = tasks.get_mut(task_id) {
                if task.status.is_terminal() {
                    // An OBSERVER-derived `Failed` (harness-event bridge
                    // classified a mid-run error as fatal while the worker
                    // loop kept running) is provisional, not authoritative —
                    // the caller of mark_completed IS the owner watching the
                    // worker actually finish, so its verdict corrects the
                    // premature failure (mini4 `review-octos-web-v3`: chip
                    // stuck "failed: unknown tool: write_file" although the
                    // worker completed). `Cancelled`, `Completed`, and
                    // owner-reported `Failed` remain final: a late worker
                    // cannot resurrect a cancelled task.
                    let observer_failed =
                        task.status == TaskStatus::Failed && task.failed_by_observer;
                    if !observer_failed {
                        tracing::warn!(
                            task_id = %task_id,
                            current_status = task.status.as_str(),
                            current_runtime_state = ?task.runtime_state,
                            attempted_status = TaskStatus::Completed.as_str(),
                            "ignoring late mark_completed: task already in terminal state",
                        );
                        return;
                    }
                    tracing::info!(
                        task_id = %task_id,
                        observed_error = task.error.as_deref().unwrap_or_default(),
                        "owner completion overrides observer-derived failure",
                    );
                    task.failed_by_observer = false;
                    task.error = None;
                    // The failure stamped its message as the summary; drop it
                    // so the completion summary below reflects the real
                    // outcome instead of the transient error.
                    task.summary = None;
                }
                task.status = TaskStatus::Completed;
                task.runtime_state = TaskRuntimeState::Completed;
                task.updated_at = Utc::now();
                task.completed_at = Some(Utc::now());
                let artifact_count = output_files.len() as u32;
                task.output_files = output_files;
                if task.artifact_count.is_some() || artifact_count > 0 {
                    task.artifact_count = Some(artifact_count);
                }
                if task.summary.is_none() {
                    task.summary = Some(if artifact_count > 0 {
                        format!(
                            "{} completed with {} artifact(s)",
                            task.tool_name, artifact_count
                        )
                    } else {
                        format!("{} completed", task.tool_name)
                    });
                }
                Some(task.clone())
            } else {
                None
            }
        };
        if let Some(ref task) = snapshot {
            self.notify_change(task);
            self.emit_progress_for_state(task);
            // Codex round-4 BLOCKER (PR #1324 follow-up): drain any
            // pending failure stash for this task's unique task_id.
            // Normally `mark_failed` is the only path that inserts
            // (and the terminal guard in `mark_failed` prevents a
            // completion after a failure today). Defensive cleanup
            // ensures a stale entry can't fire later when a sibling
            // task's `mark_synth_ack_emitted` arrives on the same
            // tool_call_id.
            self.drain_pending_failure_for_task(&task.id);
        }
    }

    /// Record the worker's full final output text on the task, capped at
    /// [`FINAL_OUTPUT_CAP_BYTES`]. Called by the spawn completion path just
    /// before the terminal transition (success AND failure) so
    /// `read_task_output` — and any announce/recovery flow — can serve the
    /// child's actual result. Without this, a spawn child's result existed
    /// only in its in-memory context: the SubAgentOutputRouter file is never
    /// fed by a spawn child's loop, so `read_task_output` returned an empty
    /// string and models concluded the result "was lost".
    ///
    /// Deliberately NOT gated on terminal status: it records payload, not a
    /// state transition, and must land whether the record is still `Running`
    /// or an observer already flipped it. No `notify_change`: dashboards key
    /// on state transitions, and the subsequent `mark_completed`/`mark_failed`
    /// snapshot (same lock domain) carries the field to persistence again.
    pub fn record_final_output(&self, task_id: &str, output: &str) {
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            match tasks.get_mut(task_id) {
                Some(task) => {
                    task.final_output = Some(octos_core::truncated_utf8(
                        output,
                        FINAL_OUTPUT_CAP_BYTES,
                        "\n…[final output truncated]",
                    ));
                    task.updated_at = Utc::now();
                    Some(task.clone())
                }
                None => None,
            }
        };
        if let Some(ref task) = snapshot {
            // #1723: `record_final_output` runs AFTER `mark_completed` (the
            // content isn't assembled until the child's turn ends), so the
            // terminal `on_change` → `upsert_background_task_agent` mirror that
            // copies `final_output` → the roster agent's readable output already
            // fired while `final_output` was still `None` — leaving the agent
            // view empty despite a captured result. Fire `on_change` again now
            // that `final_output` is set so the mirror (idempotent
            // `set_agent_output_if_empty`) runs with it present and re-emits an
            // `agent/updated` carrying the output. Without this the TUI agent
            // view / `/ps` detail shows "no output" for a completed child.
            self.notify_change(task);
        }
    }

    /// Mark a task as failed with an error message.
    ///
    /// On the FIRST transition from a non-`Failed` status to `Failed`, also
    /// emits a `SpawnOnlyFailureSignal` so listeners (e.g. the session
    /// actor) can schedule a recovery turn. Re-marking an already-failed
    /// task is a no-op for the failure signal — this guarantees at most one
    /// recovery attempt per task even if multiple paths report the failure.
    ///
    /// **M8 DoD gate (Req #4)**: this is a no-op when the task is already
    /// `Cancelled` or `Completed`. The check + write happen under the same
    /// lock so a late worker that races with `cancel()` cannot overwrite a
    /// `Cancelled` task to `Failed` (or a `Completed` task either). Re-marking
    /// an already-`Failed` task is still allowed (idempotent) so existing
    /// `was_already_failed` semantics are preserved.
    pub fn mark_failed(&self, task_id: &str, error: String) {
        self.mark_failed_inner(task_id, error, false)
    }

    /// [`Self::mark_failed`] for OBSERVERS — reporters that classified a
    /// mid-run signal as fatal without watching the worker actually stop
    /// (today: the harness-event bridge in [`Self::apply_harness_event`]).
    /// The failure is recorded and propagated exactly like an owner failure,
    /// but stamped `failed_by_observer` so the owner's later
    /// [`Self::mark_completed`] may override it when the worker demonstrably
    /// survived and finished (mini4 `review-octos-web-v3` regression).
    pub fn mark_failed_observed(&self, task_id: &str, error: String) {
        self.mark_failed_inner(task_id, error, true)
    }

    fn mark_failed_inner(&self, task_id: &str, error: String, observed: bool) {
        let (snapshot, was_already_failed) = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(task) = tasks.get_mut(task_id) {
                if matches!(task.status, TaskStatus::Cancelled | TaskStatus::Completed) {
                    tracing::warn!(
                        task_id = %task_id,
                        current_status = task.status.as_str(),
                        current_runtime_state = ?task.runtime_state,
                        attempted_status = TaskStatus::Failed.as_str(),
                        "ignoring late mark_failed: task already in terminal state",
                    );
                    return;
                }
                let already_failed = task.status == TaskStatus::Failed;
                task.status = TaskStatus::Failed;
                task.runtime_state = TaskRuntimeState::Failed;
                // Owner verdicts are authoritative: an owner (re-)mark
                // clears the provisional stamp so a stray late completion
                // can no longer flip the task. An observer's FIRST failure
                // stamps it provisional; an observer re-mark of an
                // already-failed task keeps the existing stamp (it must not
                // soften an owner-reported failure).
                if !observed {
                    task.failed_by_observer = false;
                } else if !already_failed {
                    task.failed_by_observer = true;
                }
                task.updated_at = Utc::now();
                task.completed_at = Some(Utc::now());
                if task.summary.is_none() {
                    task.summary = Some(error.chars().take(1200).collect());
                }
                task.error = Some(error);
                (Some(task.clone()), already_failed)
            } else {
                (None, false)
            }
        };
        if let Some(ref task) = snapshot {
            self.notify_change(task);
            if !was_already_failed {
                self.emit_progress_for_state(task);
                self.notify_failure(task);
            }
        }
    }

    /// Emit a `SpawnOnlyFailureSignal` for a freshly-failed task, if a
    /// failure callback has been registered. The error_message is taken
    /// from the task's `error` field (set immediately before this call).
    ///
    /// **Synth-ack gate (two-phase)**: emits or defers based on whether
    /// the LLM previously received the "Background work started for
    /// `<tool>`." synth-ack for this task's `tool_call_id` (recorded via
    /// [`Self::mark_synth_ack_emitted`]):
    ///
    /// * **Ack already emitted** → build the signal and dispatch via
    ///   [`Self::dispatch_failure_signal`] immediately. Idempotent on
    ///   replay via `AckAndPending::emitted_task_ids`.
    /// * **Ack not yet emitted** → stash the signal in
    ///   `AckAndPending::pending` keyed by `task_id` (carrying its
    ///   `tool_call_id`) and return without dispatching. When
    ///   `mark_synth_ack_emitted` later runs for the same
    ///   `tool_call_id`, it scans the map, drains every pending
    ///   entry under that id (pipeline cascade has many tasks under
    ///   one tool_call_id), and emits each signal then.
    ///   This closes the `tokio::spawn` → `mark_synth_ack_emitted` race
    ///   in `execution.rs::handle_spawn_only_branch` where a fast
    ///   failure can hit before the foreground records the ack
    ///   (Codex round-4 BLOCKER, PR #1324 follow-up).
    /// * **Ack permanently suppressed** (sibling-error / pre-flight
    ///   short-circuit) → the pending entry sits in the map until the
    ///   bounded-cap eviction runs (Codex round-2 MAJOR), or until
    ///   `cancel` / `mark_completed` drains it. The LLM already saw
    ///   the sibling error / `[VALIDATION FAILED]` tool_result, so
    ///   the absence of an emitted signal is the correct behaviour.
    ///
    /// **Atomicity (Codex round-2 BLOCKER)**: the ack-check, idempotency
    /// check, and pending insert all happen under the SAME mutex
    /// ([`AckAndPending`]). The previous design used three separate
    /// `Mutex`es, leaving an interleave where
    /// `notify_failure` could observe ack=false → `mark_synth_ack_emitted`
    /// could record ack + drain empty pending → `notify_failure` could
    /// then insert a pending entry that nothing will ever drain. Holding
    /// the single mutex across the entire decision tree makes this race
    /// impossible.
    fn notify_failure(&self, task: &BackgroundTask) {
        if task.tool_call_id.is_empty() {
            // Defensive: an empty id can't be matched by the synth-ack
            // set, so we could never drain a deferred entry. Treat
            // this as "skip" — the LLM already saw something else for
            // this code path (tasks that bypassed id-bearing dispatch).
            tracing::debug!(
                task_id = %task.id,
                tool_name = %task.tool_name,
                "skipping SpawnOnlyFailureSignal: task has empty tool_call_id (cannot key synth-ack lookup)",
            );
            return;
        }
        let signal = SpawnOnlyFailureSignal {
            task_id: task.id.clone(),
            tool_name: task.tool_name.clone(),
            tool_input: task.tool_input.clone().unwrap_or(Value::Null),
            error_message: task.error.clone().unwrap_or_default(),
            suggested_alternatives: parse_alternatives(task.error.as_deref().unwrap_or("")),
            parent_session_key: task.parent_session_key.clone(),
            originating_client_message_id: task.originating_client_message_id.clone(),
        };
        // Atomic ack-check + idempotency-check + (dispatch | stash).
        // The decision branch holds `ack_and_pending` so no interleave
        // with `mark_synth_ack_emitted` can leave a pending entry
        // un-drained, and the idempotency guard cannot race a sibling
        // `mark_failed`.
        let mut guard = self
            .ack_and_pending
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        // Idempotency guard: if a previous `notify_failure` (or a
        // drained pending entry) already fired the signal for this
        // task_id, suppress. Protects against:
        //   * `mark_failed` called twice (live + cascade-fail).
        //   * BLOCKER race: failure landed before ack, was stashed,
        //     drained by `mark_synth_ack_emitted`, and now a sibling
        //     path calls `mark_failed` again on the same task.
        // Note: keyed by `task_id` (unique), NOT `tool_call_id`,
        // because pipeline cascade has many tasks sharing the parent's
        // `tool_call_id` and each child must fire its own signal.
        if guard.emitted_task_ids.contains(&task.id) {
            tracing::debug!(
                task_id = %task.id,
                tool_call_id = %task.tool_call_id,
                "skipping SpawnOnlyFailureSignal: already emitted for this task_id",
            );
            return;
        }
        if guard
            .synth_ack_emitted_tool_call_ids
            .contains(&task.tool_call_id)
        {
            // Ack already recorded — mark emitted atomically and
            // release the mutex before invoking the callback (which
            // may take its own locks).
            if !guard.mark_emitted(&task.id) {
                // mark_emitted returns false when another path won
                // the race; this is technically reachable only when
                // the idempotency check above and mark_emitted disagree
                // (impossible while we hold the lock), but the
                // defensive return is cheap.
                return;
            }
            drop(guard);
            self.invoke_failure_callback(&signal);
        } else {
            // Two-phase: stash and wait for the ack. The pending map
            // is keyed by `task_id` (unique) and carries the
            // `tool_call_id` so `mark_synth_ack_emitted` can scan and
            // drain all matching entries — required for cascade where
            // many tasks share one tool_call_id.
            tracing::debug!(
                task_id = %task.id,
                tool_name = %task.tool_name,
                tool_call_id = %task.tool_call_id,
                "deferring SpawnOnlyFailureSignal: synth-ack not yet recorded (will emit on ack or stay pending if ack is suppressed)",
            );
            guard.insert_pending(
                task.id.clone(),
                PendingFailure {
                    tool_call_id: task.tool_call_id.clone(),
                    signal,
                },
            );
        }
    }

    /// Internal helper: drop any pending failure stash for `task_id`
    /// (the supervisor's unique task identifier). Called from
    /// terminal paths that should invalidate a deferred failure
    /// (currently `mark_completed` and `cancel`). No-op when nothing
    /// is pending.
    fn drain_pending_failure_for_task(&self, task_id: &str) -> Option<PendingFailure> {
        if task_id.is_empty() {
            return None;
        }
        let mut guard = self
            .ack_and_pending
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        guard.remove_pending(task_id)
    }

    /// Internal helper: fire the failure callback and mark the
    /// `task_id` as emitted so future replays / cascade paths observe
    /// the idempotency guard. Called from `mark_synth_ack_emitted`
    /// (drained pending entry path); the `notify_failure` direct-
    /// dispatch path inlines the same logic under
    /// `ack_and_pending` to keep the ack-check + emitted-mark atomic.
    fn dispatch_failure_signal(&self, task_id: &str, signal: SpawnOnlyFailureSignal) {
        // Single-mutex idempotency: mark_emitted returns false when
        // another path already dispatched. Lock is released BEFORE
        // calling the user-supplied callback so the callback may
        // freely take any other lock (notably `on_failure`).
        {
            let mut guard = self
                .ack_and_pending
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            if !guard.mark_emitted(task_id) {
                tracing::debug!(
                    task_id = %task_id,
                    "dispatch_failure_signal: another path already emitted; suppressing",
                );
                return;
            }
        }
        self.invoke_failure_callback(&signal);
    }

    /// Internal helper: invoke the user-supplied `on_failure` callback
    /// with `signal`. Separated from the dispatcher so callers that
    /// already hold (or already released) `ack_and_pending` can reuse
    /// the callback-invocation path without re-checking the emitted set.
    fn invoke_failure_callback(&self, signal: &SpawnOnlyFailureSignal) {
        let guard = self.on_failure.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(cb) = guard.as_ref() {
            cb(signal);
        }
    }

    /// Record the child-session contract outcome for a task.
    pub fn mark_child_session_outcome(
        &self,
        task_id: &str,
        terminal_state: ChildSessionTerminalState,
        join_state: ChildSessionJoinState,
    ) {
        let failure_action = child_failure_action_for_terminal_state(&terminal_state);
        let kind_label = child_terminal_kind_label(&terminal_state);
        let outcome_label = child_join_outcome_label(&join_state);
        let snapshot = {
            let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(task) = tasks.get_mut(task_id) {
                task.child_terminal_state = Some(terminal_state);
                task.child_join_state = Some(join_state.clone());
                task.child_joined_at = match join_state {
                    ChildSessionJoinState::Joined => Some(Utc::now()),
                    ChildSessionJoinState::Orphaned => None,
                };
                task.child_failure_action = failure_action;
                task.updated_at = Utc::now();
                Some(task.clone())
            } else {
                None
            }
        };
        if let Some(ref task) = snapshot {
            self.notify_change(task);
            record_child_session_lifecycle(kind_label, outcome_label);
            if matches!(join_state, ChildSessionJoinState::Orphaned) {
                record_child_session_orphan("terminal_event_not_joined");
            }
        }
    }

    /// Apply a structured harness event to a tracked task.
    pub fn apply_harness_event(
        &self,
        task_id: &str,
        event: &HarnessEvent,
    ) -> Result<(), &'static str> {
        let snapshot = self.get_task(task_id).ok_or("unknown task")?;
        let (workflow_kind, current_phase) = workflow_labels(snapshot.runtime_detail.as_deref());
        let runtime_detail =
            event.runtime_detail_value(workflow_kind.as_deref(), current_phase.as_deref());

        match &event.payload {
            HarnessEventPayload::Progress { .. }
            | HarnessEventPayload::Phase { .. }
            | HarnessEventPayload::Retry { .. } => {
                self.mark_runtime_state(
                    task_id,
                    TaskRuntimeState::ExecutingTool,
                    Some(runtime_detail.to_string()),
                );
            }
            HarnessEventPayload::Failure { data } => {
                self.mark_runtime_state(
                    task_id,
                    TaskRuntimeState::Failed,
                    Some(runtime_detail.to_string()),
                );
                self.mark_failed(task_id, data.message.clone());
            }
            HarnessEventPayload::SubAgentDispatch { .. } => {
                // Dispatch events are observational — they record the fact
                // that a task was shipped off to an MCP-backed sub-agent
                // without mutating the task's terminal state. The outer
                // spawn lifecycle still decides when the task completes or
                // fails; we just attach the structured detail so operators
                // can see which backend is servicing the task.
                self.mark_runtime_state(
                    task_id,
                    TaskRuntimeState::ExecutingTool,
                    Some(runtime_detail.to_string()),
                );
            }
            HarnessEventPayload::SubagentProgress { .. } => {
                // Sub-agent progress is a periodic textual summary generated
                // by `AgentSummaryGenerator`. It does not change the
                // lifecycle state — we simply fold it into the runtime
                // detail so dashboards can render a live "what is the
                // sub-agent doing" label.
                self.mark_runtime_state(
                    task_id,
                    snapshot.runtime_state,
                    Some(runtime_detail.to_string()),
                );
            }
            HarnessEventPayload::Error { data } => {
                // Structured error events are diagnostic — record them in the
                // runtime detail but only transition to Failed when the
                // recovery hint marks the variant as non-retryable.
                self.mark_runtime_state(
                    task_id,
                    TaskRuntimeState::ExecutingTool,
                    Some(runtime_detail.to_string()),
                );
                // Tool-scoped errors (a tool CALL errored — `tool_execution`,
                // `plugin_*`) are loop-recoverable by design: the agent loop
                // feeds the error back to the model and KEEPS RUNNING, so
                // their `fail_fast` hint scopes to the call, not the task.
                // Failing the task here painted healthy workers as dead
                // (mini4 `unknown tool: write_file` — chip went red while the
                // worker went on to complete successfully). The owner join
                // path reports the true terminal state; the detail above
                // keeps the error visible to operators.
                let tool_scoped =
                    crate::harness_errors::HarnessError::variant_is_tool_scoped(&data.variant);
                if !tool_scoped && matches!(data.recovery.as_str(), "fail_fast" | "bug") {
                    // This is an OBSERVER verdict — the loop may still
                    // survive (retry lanes, non-streaming fallback), so mark
                    // it overridable by the owner's completion.
                    self.mark_failed_observed(task_id, data.message.clone());
                }
            }
        }

        Ok(())
    }

    /// Return a snapshot for a specific task id.
    pub fn get_task(&self, task_id: &str) -> Option<BackgroundTask> {
        let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        tasks.get(task_id).cloned()
    }

    /// Fire the primary callback and every named observer. Clone callbacks
    /// outside their mutexes before invocation so observers may safely update
    /// their own registration.
    fn notify_change(&self, task: &BackgroundTask) {
        let primary = self
            .on_change
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        let listeners = self
            .on_change_listeners
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .values()
            .cloned()
            .collect::<Vec<_>>();
        if let Some(cb) = primary {
            cb(task);
        }
        for cb in listeners {
            cb(task);
        }
    }



    /// Return all non-completed (active) tasks.
    pub fn get_active_tasks(&self) -> Vec<BackgroundTask> {
        let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        tasks
            .values()
            .filter(|t| t.status.is_active())
            .cloned()
            .collect()
    }

    /// Return all tracked tasks.
    pub fn get_all_tasks(&self) -> Vec<BackgroundTask> {
        let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        tasks.values().cloned().collect()
    }

    /// Return all tasks belonging to a specific session.
    pub fn get_tasks_for_session(&self, session_key: &str) -> Vec<BackgroundTask> {
        let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        tasks
            .values()
            .filter(|t| t.session_key.as_deref() == Some(session_key))
            .cloned()
            .collect()
    }

}

/// RAII guard that drives a background task to a terminal state if its
/// owning worker body is dropped (panicked, aborted, or torn down with the
/// runtime) before reaching its own terminal `mark_*` arm.
///
/// C1 step 2: a background `tokio::spawn` body that panics or is cancelled
/// after `mark_running` but before its `mark_completed`/`mark_failed` arm
/// would otherwise leave the task `Running` forever — the TUI task count
/// never decrements and the chip stays "Orchestrating".
///
/// Construct the guard in the FOREGROUND (before `tokio::spawn`) and move it
/// into the background body. No explicit disarm is needed: on normal
/// completion the body's own `mark_*` arm has already moved the task terminal,
/// so by the time `Drop` runs the task is no longer `is_active()` and the
/// guard's `mark_failed` is a no-op (the terminal guards inside `mark_failed` /
/// `mark_completed` make this idempotent).
pub struct TaskTerminalGuard {
    supervisor: Arc<TaskSupervisor>,
    task_id: String,
}

impl TaskTerminalGuard {
    /// Arm a guard for `task_id` on `supervisor`.
    ///
    /// fix/orphan-sweep-liveness-gate: arming the guard also records the task
    /// id in the process-global live-set ([`mark_task_live`]). The guard is
    /// constructed in the FOREGROUND, before each detached `spawn_only` worker
    /// is spawned (`execution.rs`, `spawn.rs`), then moved into the worker
    /// future — so the id enters the live-set synchronously within the
    /// spawning turn (closing the pre-poll window) and this is the single
    /// insert site for "a detached worker is live". The matching clear happens
    /// in `Drop`, which runs on EVERY exit path (success, failure, cancel,
    /// panic-unwind, or unpolled drop).
    pub fn new(supervisor: Arc<TaskSupervisor>, task_id: String) -> Self {
        mark_task_live(&task_id);
        Self {
            supervisor,
            task_id,
        }
    }
}

impl Drop for TaskTerminalGuard {
    fn drop(&mut self) {
        // fix/orphan-sweep-liveness-gate: clear the live-set entry FIRST so the
        // task is no longer "live" the instant its worker future terminates —
        // on every exit path, including panic-unwind. After this, a genuinely
        // stale row from a later restart is correctly reapable.
        clear_task_live(&self.task_id);
        // Only fire if the task is still active. For already-terminal tasks
        // this lookup short-circuits and we leave the recorded outcome
        // (Completed/Failed/Cancelled) untouched — and even if we raced and
        // called `mark_failed` anyway, the supervisor's terminal guard would
        // no-op it. The active-check keeps the common (success) path from
        // touching the lock twice and avoids a spurious failure-signal fire.
        let still_active = self
            .supervisor
            .get_task(&self.task_id)
            .map(|task| task.status.is_active())
            .unwrap_or(false);
        if still_active {
            self.supervisor.mark_failed(
                &self.task_id,
                "worker dropped before reaching terminal state".to_string(),
            );
        }
    }
}





#[cfg(test)]
#[path = "task_supervisor_tests.rs"]
mod tests;
