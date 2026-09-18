//! Crate-internal `#[cfg(test)]` scaffolding shared by the `tests` child
//! modules (`loop_runner_tests`, `spawn_tests`). Not compiled outside tests.

use std::sync::{Arc, Mutex as StdMutex};

use async_trait::async_trait;
use octos_core::{Message, ToolCall};
use octos_llm::{ChatConfig, ChatResponse, LlmProvider, TokenUsage, ToolSpec};

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
    ChatResponse::tool_use(tool_calls).with_usage(TokenUsage {
        input_tokens,
        output_tokens,
        ..Default::default()
    })
}

/// An `EndTurn` response with `content`.
pub(crate) fn end_turn(content: &str, input_tokens: u32, output_tokens: u32) -> ChatResponse {
    ChatResponse::end_turn(content).with_usage(TokenUsage {
        input_tokens,
        output_tokens,
        ..Default::default()
    })
}

/// A plain single-text `EndTurn` response with zero usage.
pub(crate) fn plain_text_response(content: &str) -> ChatResponse {
    end_turn(content, 0, 0)
}

/// A provider for tests where `chat` must never be reached — panics loudly
/// (naming the leak) instead of answering, so a mis-wired loop fails the
/// test rather than silently "passing" on a synthetic response.
pub(crate) struct InertProvider;

#[async_trait]
impl LlmProvider for InertProvider {
    async fn chat(
        &self,
        _messages: &[Message],
        _tools: &[ToolSpec],
        _config: &ChatConfig,
    ) -> Result<ChatResponse, eyre::Report> {
        unreachable!("InertProvider::chat must not be called")
    }

    fn model_id(&self) -> &str {
        "inert"
    }

    fn provider_name(&self) -> &str {
        "inert"
    }
}

/// An `Assistant` message carrying `tool_calls`, every other field default
/// (test scaffolding for loop-history fixtures).
pub(crate) fn assistant_with_calls(tool_calls: Vec<ToolCall>) -> Message {
    Message {
        role: octos_core::MessageRole::Assistant,
        content: String::new(),
        media: vec![],
        tool_calls: Some(tool_calls),
        tool_call_id: None,
        reasoning_content: None,
        client_message_id: None,
        thread_id: None,
        timestamp: chrono::Utc::now(),
    }
}

/// A single tool call with JSON arguments and no metadata (test scaffolding).
pub(crate) fn tool_call(id: &str, name: &str, arguments: serde_json::Value) -> ToolCall {
    ToolCall {
        id: id.into(),
        name: name.into(),
        arguments,
        metadata: None,
    }
}

/// Test reporter capturing every [`crate::progress::ProgressEvent`] so
/// assertions can branch on event kind without parsing JSON.
pub(crate) struct CapturingReporter {
    pub(crate) events: Arc<StdMutex<Vec<crate::progress::ProgressEvent>>>,
}

impl crate::progress::ProgressReporter for CapturingReporter {
    fn report(&self, event: crate::progress::ProgressEvent) {
        self.events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(event);
    }
}

/// Boot a test agent the way the loop tests do: a tempdir-scoped
/// `EpisodeStore` and `save_episodes: false` layered under `config`.
pub(crate) async fn boot_agent(
    id: &str,
    data_dir: &std::path::Path,
    provider: Arc<dyn LlmProvider>,
    tools: crate::tools::ToolRegistry,
    config: crate::AgentConfig,
) -> crate::Agent {
    let memory = Arc::new(
        octos_memory::EpisodeStore::open(data_dir.join("memory"))
            .await
            .unwrap(),
    );
    crate::Agent::new(octos_core::AgentId::new(id), provider, tools, memory).with_config(
        crate::AgentConfig {
            save_episodes: false,
            ..config
        },
    )
}
