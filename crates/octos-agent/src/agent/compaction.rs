//! Context window trimming and fallback truncation.

use octos_core::{Message, MessageRole};
use tracing::{info, warn};

use super::Agent;
use crate::compaction::CompactionPhase;
use crate::prompt_context::{PromptContextPhase, PromptContextRequest};

/// Outcome of the overflow-retry compaction pass
/// ([`Agent::maybe_run_retry_compaction`]).
pub(super) enum RetryCompactionVerdict {
    /// A declarative compaction pass ran; its token delta is reported so
    /// the caller can refuse to retry an unshrunk payload.
    Compacted {
        tokens_before: u32,
        tokens_after: u32,
    },
    /// No compaction ran. When `context_manager_owned` is true the
    /// caller-owned prompt bridge compacts the retry prompt itself
    /// (retrying is fine); any other no-op means the retry would resend
    /// the SAME oversized payload.
    NoOp {
        context_manager_owned: bool,
        reason: &'static str,
    },
}

impl Agent {
    pub(super) fn trim_to_context_window(&self, messages: &mut Vec<Message>) -> bool {
        use crate::compaction::{MIN_RECENT_MESSAGES, compact_messages, find_recent_boundary};
        use octos_llm::context::{estimate_message_tokens, estimate_tokens};

        if self.prompt_context_manager.is_some() {
            return false;
        }

        if messages.len() <= 1 + MIN_RECENT_MESSAGES {
            return false;
        }

        let window = self.llm.context_window();
        let raw_budget = (window as f64 * 0.8 / crate::compaction::SAFETY_MARGIN) as u32;
        // Calibrated budget: the static estimator under-counts CJK-heavy
        // conversations, so divide by the observed actual/estimated ratio to
        // trigger compaction before the provider rejects the request. See
        // [`TokenCalibration`]. Declarative policy budgets (CompactionRunner)
        // are deliberately NOT calibrated — declared semantics, not guesses.
        let budget = self.token_calibration.adjusted_budget(raw_budget);

        let total: u32 = messages.iter().map(estimate_message_tokens).sum();
        if total <= budget {
            return false;
        }

        let system_tokens = estimate_message_tokens(&messages[0]);
        if system_tokens >= budget {
            warn!(
                system_tokens,
                budget, "system prompt exceeds context window budget, cannot trim"
            );
            return false;
        }

        let split = find_recent_boundary(messages, budget, system_tokens);
        let recent_tokens: u32 = messages[split..].iter().map(estimate_message_tokens).sum();

        // If recent messages alone exceed budget, fall back to simple truncation
        if system_tokens + recent_tokens >= budget {
            return self.fallback_truncate(messages, budget);
        }

        let old_messages = &messages[1..split];
        if old_messages.is_empty() {
            return false;
        }

        let summary_budget = budget - system_tokens - recent_tokens;
        let summary_text = compact_messages(old_messages, summary_budget);
        let summary_tokens = estimate_tokens(&summary_text) + 4;

        let original_count = messages.len();
        let dropped = split - 1;
        messages.drain(1..split);
        messages.insert(
            1,
            Message {
                role: MessageRole::System,
                content: summary_text,
                media: vec![],
                tool_calls: None,
                tool_call_id: None,
                reasoning_content: None,
                client_message_id: None,
                thread_id: None,
                timestamp: chrono::Utc::now(),
            },
        );

        info!(
            original_tokens = total,
            summary_tokens,
            messages_compacted = dropped,
            messages_remaining = messages.len(),
            original_messages = original_count,
            "compacted conversation history ({} token budget)",
            budget
        );
        true
    }

    /// Run preflight compaction before the first LLM call if the wired
    /// policy declares a threshold and the conversation already exceeds it.
    ///
    /// No-op when a caller-owned prompt context manager is attached. In that
    /// mode ContextManager owns the production prompt compaction path and the
    /// legacy declarative runner must not mutate the same prompt vector first.
    /// Also no-op when no [`crate::compaction::CompactionRunner`] is attached,
    /// preserving legacy extractive behaviour for every existing caller.
    pub(super) fn maybe_run_preflight_compaction(
        &self,
        messages: &mut Vec<Message>,
    ) -> eyre::Result<Option<String>> {
        if self.prompt_context_manager.is_some() {
            return Ok(None);
        }
        let Some(runner) = self.compaction_runner.as_ref() else {
            return Ok(None);
        };
        if runner.needs_preflight(messages).is_none() {
            return Ok(None);
        }
        let outcome = runner.run(messages);
        info!(
            phase = "preflight",
            performed = outcome.performed,
            messages_dropped = outcome.messages_dropped,
            tool_results_replaced = outcome.tool_results_replaced,
            tokens_before = outcome.tokens_before,
            tokens_after = outcome.tokens_after,
            summarizer = outcome.summarizer_kind,
            "harness M6.3 compaction preflight fired"
        );
        self.enforce_preservation(messages, CompactionPhase::Preflight)?;
        // A large/resumed conversation compacts on ENTRY (iteration 1),
        // where maybe_run_turn_compaction returns None — surface the
        // preflight summary too so that conversation is still saved
        // (codex #1618 P2).
        Ok(outcome.summary)
    }

    /// Run declarative compaction per-iteration (after M0 message prep). Only
    /// active when a [`crate::compaction::CompactionRunner`] is wired; a
    /// no-op otherwise so every caller that does not wire the contract keeps
    /// the existing behaviour byte-for-byte.
    /// Run the per-turn declarative compaction pass. Returns the summary
    /// text a pass folded in (when any), so the conversational loop can
    /// persist it as a searchable episode (#1587 write side). `None` when
    /// no pass ran (context-manager-owned prompt, no runner, iteration 1,
    /// or nothing to compact).
    pub(super) fn maybe_run_turn_compaction(
        &self,
        messages: &mut Vec<Message>,
        iteration: u32,
    ) -> eyre::Result<Option<String>> {
        if self.prompt_context_manager.is_some() {
            return Ok(None);
        }
        let Some(runner) = self.compaction_runner.as_ref() else {
            return Ok(None);
        };
        // Skip the very first iteration when the preflight path already ran
        // — preflight emits its own events and enforces preservation.
        if iteration == 1 {
            return Ok(None);
        }
        let outcome = runner.run(messages);
        if outcome.performed {
            info!(
                phase = "turn_end",
                iteration,
                messages_dropped = outcome.messages_dropped,
                tool_results_replaced = outcome.tool_results_replaced,
                tokens_before = outcome.tokens_before,
                tokens_after = outcome.tokens_after,
                summarizer = outcome.summarizer_kind,
                "harness M6.3 compaction per-turn pass"
            );
            self.enforce_preservation(messages, CompactionPhase::TurnEnd)?;
            return Ok(outcome.summary);
        }
        Ok(None)
    }

    /// Compaction pass for the `LoopDecision::CompactAndRetry` arm
    /// (context-overflow retries).
    ///
    /// Unlike [`Self::maybe_run_turn_compaction`] — best-effort, silently
    /// no-ops — this variant REPORTS whether a pass actually ran and how
    /// much it shrank the conversation. An overflow retry that resends an
    /// unshrunk payload is a guaranteed second overflow: it burns a full
    /// provider round-trip and one `context_overflow` bucket unit for
    /// nothing, so the caller bails when nothing shrank. Note the turn-pass
    /// `iteration == 1` skip is deliberately absent here: an overflow ON
    /// the first call has no preflight to lean on, so compact now or the
    /// retry resends the same payload.
    pub(super) fn maybe_run_retry_compaction(
        &self,
        messages: &mut Vec<Message>,
    ) -> eyre::Result<RetryCompactionVerdict> {
        if self.prompt_context_manager.is_some() {
            return Ok(RetryCompactionVerdict::NoOp {
                context_manager_owned: true,
                reason: "caller-owned context manager bridges the retry prompt",
            });
        }
        let Some(runner) = self.compaction_runner.as_ref() else {
            return Ok(RetryCompactionVerdict::NoOp {
                context_manager_owned: false,
                reason: "no compaction runner wired",
            });
        };
        let outcome = runner.run(messages);
        if outcome.performed {
            info!(
                phase = "context_overflow_retry",
                tokens_before = outcome.tokens_before,
                tokens_after = outcome.tokens_after,
                summarizer = outcome.summarizer_kind,
                "harness compaction pass before overflow retry"
            );
            self.enforce_preservation(messages, CompactionPhase::TurnEnd)?;
            Ok(RetryCompactionVerdict::Compacted {
                tokens_before: outcome.tokens_before,
                tokens_after: outcome.tokens_after,
            })
        } else {
            Ok(RetryCompactionVerdict::NoOp {
                context_manager_owned: false,
                reason: "compaction pass made no change",
            })
        }
    }

    /// Ask the caller-owned context bridge to prepare the final model prompt.
    ///
    /// AppUI/session runtimes that own a durable context ledger replace the
    /// prompt with their canonical `ContextManager::for_prompt` generation.
    /// When this bridge is present, the compaction paths above
    /// are disabled so only one component owns prompt compaction.
    pub(super) fn prepare_prompt_with_context_manager(
        &self,
        messages: &mut Vec<Message>,
        phase: PromptContextPhase,
        iteration: u32,
    ) {
        let Some(manager) = self.prompt_context_manager.as_ref() else {
            return;
        };
        let request = PromptContextRequest {
            phase,
            iteration,
            provider_name: self.llm.provider_name().to_owned(),
            model_id: self.llm.model_id().to_owned(),
            context_window: self.llm.context_window(),
        };
        match manager.prepare_prompt(request, messages) {
            Ok(report) => {
                if report.prompt_replaced || report.compaction_performed {
                    info!(
                        phase = phase.as_str(),
                        iteration,
                        prompt_replaced = report.prompt_replaced,
                        compaction_performed = report.compaction_performed,
                        messages_before = report.messages_before,
                        messages_after = report.messages_after,
                        token_estimate = ?report.token_estimate,
                        generation = ?report.generation,
                        "caller-owned prompt context manager prepared model prompt"
                    );
                }
            }
            Err(error) => {
                warn!(
                    phase = phase.as_str(),
                    iteration,
                    error = %error,
                    "caller-owned prompt context manager failed; using existing prompt vector"
                );
            }
        }
    }

    /// Run the post-compaction validator rail against the declared
    /// `preserved_artifacts` + `preserved_invariants`.
    ///
    /// Missing required preservation entries are fail-closed: the caller
    /// aborts the turn instead of sending a prompt that already lost a declared
    /// invariant.
    fn enforce_preservation(
        &self,
        messages: &[Message],
        phase: CompactionPhase,
    ) -> eyre::Result<()> {
        let Some(runner) = self.compaction_runner.as_ref() else {
            return Ok(());
        };
        let Some(workspace) = self.compaction_workspace.as_ref() else {
            return Ok(());
        };
        match runner.check_preserved(messages, workspace) {
            Ok(ledger) => {
                if !ledger.all_preserved() {
                    let missing: Vec<&str> = ledger.missing.iter().map(|art| art.name()).collect();
                    warn!(
                        phase = phase.as_str(),
                        missing_count = missing.len(),
                        missing = %missing.join(","),
                        "harness M6.3 compaction validator: declared artifacts/invariants were dropped"
                    );
                    metrics::counter!(
                        "octos_compaction_preservation_violations_total",
                        "phase" => phase.as_str().to_string(),
                    )
                    .increment(missing.len() as u64);
                    eyre::bail!(
                        "compaction preservation validator failed during {}: missing declared artifacts/invariants: {}",
                        phase.as_str(),
                        missing.join(",")
                    );
                }
                Ok(())
            }
            Err(err) => {
                warn!(error = %err, "harness M6.3 compaction validator failed");
                Err(err.wrap_err("compaction preservation validator failed"))
            }
        }
    }

    /// Simple truncation fallback when even recent messages exceed budget.
    pub(super) fn fallback_truncate(&self, messages: &mut Vec<Message>, limit: u32) -> bool {
        let system_tokens = octos_llm::context::estimate_message_tokens(&messages[0]);
        let mut kept_tokens = system_tokens;
        let mut keep_from = messages.len();

        for i in (1..messages.len()).rev() {
            let msg_tokens = octos_llm::context::estimate_message_tokens(&messages[i]);
            if kept_tokens + msg_tokens > limit {
                break;
            }
            kept_tokens += msg_tokens;
            keep_from = i;
        }

        // Keep at least 2 non-system messages
        let max_keep_from = messages.len().saturating_sub(2);
        if keep_from > max_keep_from {
            keep_from = max_keep_from;
        }

        // Don't split inside a tool-call group
        while keep_from > 1 && messages[keep_from].role == MessageRole::Tool {
            keep_from -= 1;
        }

        if keep_from > 1 {
            let dropped = keep_from - 1;
            messages.drain(1..keep_from);
            warn!(
                messages_dropped = dropped,
                messages_kept = messages.len(),
                "fallback truncation ({} token limit)",
                limit
            );
            return dropped > 0;
        }
        false
    }
}
