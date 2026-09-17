//! Crate-internal `#[cfg(test)]` scaffolding shared by the `tests` child
//! modules (`loop_runner_tests`, `spawn_tests`). Not compiled outside tests.

use std::sync::Mutex as StdMutex;

use async_trait::async_trait;
use octos_core::{Message, ToolCall};
use octos_llm::{ChatConfig, ChatResponse, LlmProvider, StopReason, ToolSpec, TokenUsage};

/// `(messages, tools, config)` of one provider request, in call order.
pub(crate) type RecordedCall = (Vec<Message>, Vec<ToolSpec>, ChatConfig);

/// Pops pre-built `ChatResponse`s in order and records every request, so
/// tests can assert on the prompts / tool sets / config the loop actually
/// sent (see [`ScriptedProvider::recorded_calls`]). A call past the end of
/// the script errors loudly — an under-provisioned script fails the test,
/// it never loops forever.
pub(crate) struct ScriptedProvider {
    responses: StdMutex<Vec<ChatResponse>>,
    calls: StdMutex<Vec<RecordedCall>>,
}

impl ScriptedProvider {
    pub(crate) fn new(responses: Vec<ChatResponse>) -> Self {
        Self {
            responses: StdMutex::new(responses.into_iter().rev().collect()),
            calls: StdMutex::new(Vec::new()),
        }
    }

    pub(crate) fn recorded_calls(&self) -> Vec<RecordedCall> {
        self.calls
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    /// Number of not-yet-consumed scripted responses.
    pub(crate) fn remaining(&self) -> usize {
        self.responses
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .len()
    }
}

#[async_trait]
impl LlmProvider for ScriptedProvider {
    async fn chat(
        &self,
        messages: &[Message],
        tools: &[ToolSpec],
        config: &ChatConfig,
    ) -> Result<ChatResponse, eyre::Report> {
        self.calls
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push((messages.to_vec(), tools.to_vec(), config.clone()));
        self.responses
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .pop()
            .ok_or_else(|| eyre::eyre!("scripted provider exhausted"))
    }

    fn model_id(&self) -> &str {
        "mock"
    }

    fn provider_name(&self) -> &str {
        "mock"
    }
}

/// A `ToolUse` response carrying `tool_calls`.
pub(crate) fn tool_use(
    tool_calls: Vec<ToolCall>,
    input_tokens: u32,
    output_tokens: u32,
) -> ChatResponse {
    ChatResponse {
        content: None,
        reasoning_content: None,
        tool_calls,
        stop_reason: StopReason::ToolUse,
        usage: TokenUsage {
            input_tokens,
            output_tokens,
            ..Default::default()
        },
        provider_index: None,
    }
}

/// An `EndTurn` response with `content`.
pub(crate) fn end_turn(content: &str, input_tokens: u32, output_tokens: u32) -> ChatResponse {
    ChatResponse {
        content: Some(content.to_string()),
        reasoning_content: None,
        tool_calls: vec![],
        stop_reason: StopReason::EndTurn,
        usage: TokenUsage {
            input_tokens,
            output_tokens,
            ..Default::default()
        },
        provider_index: None,
    }
}

/// A plain single-text `EndTurn` response with zero usage.
pub(crate) fn plain_text_response(content: &str) -> ChatResponse {
    end_turn(content, 0, 0)
}
