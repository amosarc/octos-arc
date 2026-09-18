//! Compaction summarizer seam (harness M6.3).
//!
//! Compaction turns a block of old conversation messages into a bounded
//! summary. The strategy lives behind a shared trait so the runtime can swap
//! implementations without reshuffling the agent loop:
//!
//! - [`ExtractiveSummarizer`] — deterministic, dependency-free. Preserves the
//!   existing M0 behaviour (header + per-message first-line extraction +
//!   tool-argument stripping). Default for every caller.
//!
//! (The M6.4 LLM-iterative summarizer was retired: it was a config-gated
//! opt-in that no in-tree workspace policy ever declared, and the
//! extractive path is the compaction behaviour the coding runtime relies
//! on. The blocking LLM bridge below survives — the live LLM compaction
//! path in `compaction.rs` drives it.)
//!
//! The trait is deliberately minimal: take the old messages plus a budget,
//! hand back a `Result<String>`. Failures fall back to the extractive path
//! so no single implementation can break the loop.

use std::sync::Arc;

use eyre::Result;
use octos_core::Message;
use tokio::runtime::Handle;

use crate::compaction::compact_messages;
use crate::workspace_policy::CompactionSummarizerKind;

/// Seam for compaction summarization strategies.
///
/// Implementors take a slice of messages to compact and a `budget_tokens`
/// ceiling, and return a text summary whose token estimate stays at or below
/// the budget. Deterministic implementations should be pure — the signature
/// stays synchronous so the agent loop can run compaction without awaiting
/// inside the message-prep pipeline.
///
/// The trait must be `Send + Sync` so the runtime can keep summarizers in
/// `Arc<dyn Summarizer>` and share them across spawned worker tasks.
pub trait Summarizer: Send + Sync {
    /// Stable, human-readable identifier for this summarizer strategy.
    ///
    /// Reported in compaction phase events so operators can tell which
    /// strategy compacted the turn. Keep this lowercase snake_case so it
    /// also serializes cleanly through `CompactionSummarizerKind`.
    fn kind(&self) -> &'static str;

    /// Return a bounded summary of `messages`.
    ///
    /// Implementors MUST respect `budget_tokens`. The extractive fallback
    /// measures token count via `octos_llm::context::estimate_tokens`, so
    /// approximate adherence is acceptable — but wildly overshooting the
    /// budget is a contract violation and will be rejected by the runtime.
    fn summarize(&self, messages: &[Message], budget_tokens: u32) -> Result<String>;
}

/// Deterministic, dependency-free summarizer that preserves the existing
/// extractive behaviour. Used by default so the absence of a policy leaves
/// the loop indistinguishable from the pre-M6.3 runtime.
#[derive(Debug, Default, Clone, Copy)]
pub struct ExtractiveSummarizer;

impl ExtractiveSummarizer {
    /// Construct a new extractive summarizer.
    pub fn new() -> Self {
        Self
    }
}

impl Summarizer for ExtractiveSummarizer {
    fn kind(&self) -> &'static str {
        "extractive"
    }

    fn summarize(&self, messages: &[Message], budget_tokens: u32) -> Result<String> {
        Ok(compact_messages(messages, budget_tokens))
    }
}

/// Drive a future to completion from a synchronous caller without crashing
/// the Tokio runtime.
///
/// - On a multi-threaded runtime we use `block_in_place + block_on` so the
///   worker thread can park cleanly.
/// - On a current-thread runtime (e.g. `#[tokio::test]` default) we off-host
///   the work onto a dedicated OS thread whose runtime handle is the same
///   current-thread scheduler — `Handle::block_on` is safe there because the
///   thread is not itself inside the runtime. This keeps the trait
///   signature synchronous without forcing callers into multi-threaded
///   scheduling.
pub(crate) fn run_llm_call_blocking<F, T>(future: F) -> T
where
    F: std::future::Future<Output = T> + Send + 'static,
    T: Send + 'static,
{
    let handle = Handle::current();
    match handle.runtime_flavor() {
        tokio::runtime::RuntimeFlavor::MultiThread => {
            tokio::task::block_in_place(|| handle.block_on(future))
        }
        _ => {
            // Current-thread runtime: a plain `block_on` from inside would
            // deadlock because we are the single worker. Spawn an OS thread
            // so it can drive the future via the same runtime handle.
            let (tx, rx) = std::sync::mpsc::channel();
            let thread_handle = handle.clone();
            std::thread::spawn(move || {
                let out = thread_handle.block_on(future);
                let _ = tx.send(out);
            });
            rx.recv().expect("llm summarizer worker thread dropped")
        }
    }
}

/// Map a declared [`CompactionSummarizerKind`] to a concrete `Summarizer`
/// instance. The enum currently declares only the extractive strategy.
pub fn default_summarizer_for(kind: CompactionSummarizerKind) -> Arc<dyn Summarizer> {
    match kind {
        CompactionSummarizerKind::Extractive => Arc::new(ExtractiveSummarizer::new()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use octos_core::MessageRole;

    fn user(content: &str) -> Message {
        Message {
            role: MessageRole::User,
            content: content.to_string(),
            media: vec![],
            tool_calls: None,
            tool_call_id: None,
            reasoning_content: None,
            client_message_id: None,
            thread_id: None,
            timestamp: chrono::Local::now(),
        }
    }

    #[test]
    fn extractive_summarizer_reports_stable_kind() {
        assert_eq!(ExtractiveSummarizer::new().kind(), "extractive");
    }

    #[test]
    fn extractive_summarizer_produces_nonempty_summary_within_budget() {
        let messages = vec![user("hello"), user("world")];
        let summary = ExtractiveSummarizer::new()
            .summarize(&messages, 2_000)
            .expect("summarize should succeed");
        assert!(summary.contains("Conversation Summary"));
        assert!(summary.contains("> User: hello"));
    }
}
