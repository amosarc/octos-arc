//! Progress reporting for agent execution.
//!
//! This module provides a trait for reporting agent progress,
//! allowing CLI and other consumers to display real-time updates.

use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::abi_schema::PROGRESS_EVENT_SCHEMA_VERSION;

/// Canonical schema identifier for harness progress events on the wire.
///
/// Producers that emit progress envelopes into the structured event transport
/// MUST use this string; consumers can branch on it to identify supported
/// payload shapes. See `docs/OCTOS_HARNESS_ABI_VERSIONING.md`.
pub const HARNESS_PROGRESS_EVENT_SCHEMA: &str = "octos.agent.progress.event.v1";

/// Versioned wrapper for a [`ProgressEvent`] when it is serialized onto the
/// transport. This is the durable ABI consumers should branch on — the raw
/// enum variants are an implementation detail that may grow between minor
/// versions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProgressEventEnvelope {
    /// Canonical schema identifier (see [`HARNESS_PROGRESS_EVENT_SCHEMA`]).
    #[serde(default = "default_progress_event_schema_name")]
    pub schema: String,
    /// Numeric schema version companion to `schema`. Defaults to
    /// [`PROGRESS_EVENT_SCHEMA_VERSION`] when absent so older replayed
    /// streams remain parseable.
    #[serde(default = "default_progress_event_schema_version")]
    pub schema_version: u32,
    /// The event payload.
    pub event: ProgressEvent,
}

fn default_progress_event_schema_name() -> String {
    HARNESS_PROGRESS_EVENT_SCHEMA.to_string()
}

fn default_progress_event_schema_version() -> u32 {
    PROGRESS_EVENT_SCHEMA_VERSION
}

impl ProgressEventEnvelope {
    /// Wrap an event with the current harness progress schema.
    pub fn wrap(event: ProgressEvent) -> Self {
        Self {
            schema: HARNESS_PROGRESS_EVENT_SCHEMA.to_string(),
            schema_version: PROGRESS_EVENT_SCHEMA_VERSION,
            event,
        }
    }
}

/// Events emitted during agent execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ProgressEvent {
    /// Agent started working on task.
    TaskStarted { task_id: String },

    /// Agent is thinking (calling LLM).
    Thinking { iteration: u32 },

    /// Live telemetry for an unbounded interactive turn. This is deliberately
    /// usage/time visibility, not a fee budget or a terminal limit.
    AgentProgress {
        iteration: u32,
        active_tokens: u64,
        elapsed: Duration,
        checkpoints: u32,
        reflecting: bool,
    },

    /// LLM responded with text.
    Response { content: String, iteration: u32 },

    /// Agent is calling a tool.
    ToolStarted {
        name: String,
        tool_id: String,
        /// The call arguments as requested by the LLM (pre-hook). Carried so
        /// downstream projections (UI protocol tool cards, envelope
        /// `arguments_preview`) can echo what the tool was asked to do.
        arguments: Option<serde_json::Value>,
    },

    /// Mid-execution progress from a tool (e.g., stderr line from binary plugin).
    ToolProgress {
        name: String,
        tool_id: String,
        message: String,
    },

    /// Tool execution completed.
    ToolCompleted {
        name: String,
        tool_id: String,
        success: bool,
        output_preview: String,
        duration: Duration,
    },

    /// File was modified.
    FileModified { path: String },

    /// Model-authored plan/todo checklist snapshot from the `update_plan`
    /// tool. Carried as the serialized `UiPlanRecord` JSON so this low-level
    /// event stays decoupled from the octos-core wire structs; the serve
    /// mapper deserializes it into a `plan/updated` notification.
    PlanUpdated { plan: serde_json::Value },

    /// Token usage update.
    TokenUsage {
        input_tokens: u32,
        output_tokens: u32,
    },

    /// Task completed.
    TaskCompleted {
        success: bool,
        iterations: u32,
        duration: Duration,
    },

    /// Task was interrupted (Ctrl+C).
    TaskInterrupted { iterations: u32 },

    /// Hit max iterations limit.
    MaxIterationsReached { limit: u32 },

    /// Hit token budget limit.
    TokenBudgetExceeded { used: u32, limit: u32 },

    /// Hit activity timeout.
    ActivityTimeoutReached { elapsed: Duration, limit: Duration },

    /// Status update during LLM call (e.g. retry progress, provider switching).
    LlmStatus { message: String, iteration: u32 },

    /// Streaming text chunk from LLM.
    StreamChunk { text: String, iteration: u32 },

    /// Streaming reasoning chunk from LLM.
    ReasoningChunk { text: String, iteration: u32 },

    /// Streaming completed.
    StreamDone { iteration: u32 },

    /// LLM call is being retried — signals stream forwarder to clear its buffer
    /// so partial text from the failed attempt isn't concatenated with the retry.
    StreamRetry { iteration: u32 },

    /// Cost update after a response.
    CostUpdate {
        /// Session-cumulative token counts: completed runs folded into the
        /// shared session-usage base (when the embedder injected one) plus
        /// the live turn. u64 because sessions outlive single turns.
        session_input_tokens: u64,
        session_output_tokens: u64,
        /// TURN-cumulative token counts (every response recorded this
        /// turn so far, including ones that emit no cost update of their
        /// own — mid-turn tool iterations). Metrics
        /// counters meter the DELTA between successive events, so
        /// nothing is dropped when only the terminal response emits;
        /// the session_* fields are unsuitable for that (their base
        /// includes pre-turn history the per-turn reporter never saw).
        turn_input_tokens: u32,
        turn_output_tokens: u32,
        /// Cost of this response (None if pricing unknown).
        response_cost: Option<f64>,
        /// Cumulative session cost: each completed run and each response
        /// of the live turn priced at the model that produced it. `None`
        /// until any priced usage exists.
        session_cost: Option<f64>,
        /// Model identifier that produced this response. Forwarded by the
        /// API bridge into `metadata.token_cost.model` so chat clients can
        /// render `model · tokens_in / tokens_out · duration` footers.
        /// `None` when the reporter cannot resolve a model id (e.g.
        /// synthetic test fixtures).
        model: Option<String>,
        /// Model context window in tokens, when the provider exposes it.
        /// Lets clients render an honest ctx-fill gauge against the real
        /// window instead of a hardcoded default.
        context_window: Option<u32>,
    },
}

/// Stable, compact status-line text shared by terminal and UI projections.
pub fn agent_progress_message(
    iteration: u32,
    active_tokens: u64,
    elapsed: Duration,
    checkpoints: u32,
    reflecting: bool,
) -> String {
    let seconds = elapsed.as_secs();
    let elapsed = if seconds >= 60 {
        format!("{}m{:02}s", seconds / 60, seconds % 60)
    } else {
        format!("{seconds}s")
    };
    if reflecting {
        format!(
            "Reflection checkpoint {} · Step {iteration} · {active_tokens} tokens · {elapsed}",
            checkpoints.saturating_add(1),
        )
    } else {
        format!(
            "Step {iteration} · {active_tokens} tokens · {elapsed} · {checkpoints} reflection{}",
            if checkpoints == 1 { "" } else { "s" },
        )
    }
}

/// Trait for receiving progress updates.
pub trait ProgressReporter: Send + Sync {
    /// Called when a progress event occurs.
    fn report(&self, event: ProgressEvent);

    /// M8.10 follow-up (#649): the per-turn `thread_id` (typically the user
    /// message's `client_message_id`) bound to this reporter. Tools that
    /// spawn long-running background work read this so the late-arriving
    /// completion can be stamped with the originating turn's id rather than
    /// inheriting whatever the per-chat sticky map currently holds.
    /// Default is `None` — silent / console reporters don't track threads.
    fn thread_id(&self) -> Option<&str> {
        None
    }
}

/// Default reporter that does nothing (silent mode).
pub struct SilentReporter;

impl ProgressReporter for SilentReporter {
    fn report(&self, _event: ProgressEvent) {}
}

