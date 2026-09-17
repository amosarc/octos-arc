//! Shared scaffolding for the integration-test binaries. Compiled once per
//! test target that declares `mod common;` — keep it dependency-light.
#![allow(dead_code)]

use std::sync::Mutex;

use async_trait::async_trait;
use octos_agent::{Agent, AgentConfig, ToolRegistry};
use octos_core::{AgentId, Message, ToolCall};
use octos_llm::{ChatConfig, ChatResponse, LlmProvider, StopReason, TokenUsage, ToolSpec};
use octos_memory::EpisodeStore;
use tempfile::TempDir;

/// FIFO-scripted provider: pops pre-built responses in order and errors
/// loudly once the script is exhausted (an under-provisioned script fails
/// the test, it never loops forever). `context_window` is a comfortable
/// 128k so compaction never fires in these tests.
pub(crate) struct ScriptedLlm {
    responses: Mutex<Vec<ChatResponse>>,
    model_id: String,
}

impl ScriptedLlm {
    pub(crate) fn new(responses: Vec<ChatResponse>) -> Self {
        Self {
            responses: Mutex::new(responses),
            model_id: "mock".to_string(),
        }
    }

    pub(crate) fn with_model_id(mut self, model_id: &str) -> Self {
        self.model_id = model_id.to_string();
        self
    }
}

#[async_trait]
impl LlmProvider for ScriptedLlm {
    async fn chat(
        &self,
        _messages: &[Message],
        _tools: &[ToolSpec],
        _config: &ChatConfig,
    ) -> eyre::Result<ChatResponse> {
        let mut r = self.responses.lock().unwrap();
        if r.is_empty() {
            eyre::bail!("ScriptedLlm: no more responses");
        }
        Ok(r.remove(0))
    }
    fn context_window(&self) -> u32 {
        128_000
    }
    fn model_id(&self) -> &str {
        &self.model_id
    }
    fn provider_name(&self) -> &str {
        "mock"
    }
}

/// A `ToolUse` response carrying `tool_calls`.
pub(crate) fn tool_use(calls: Vec<ToolCall>) -> ChatResponse {
    ChatResponse {
        content: None,
        reasoning_content: None,
        tool_calls: calls,
        stop_reason: StopReason::ToolUse,
        usage: TokenUsage {
            input_tokens: 50,
            output_tokens: 5,
            ..Default::default()
        },
        provider_index: None,
    }
}

/// An `EndTurn` response with `content`.
pub(crate) fn end_turn(text: &str) -> ChatResponse {
    ChatResponse {
        content: Some(text.into()),
        reasoning_content: None,
        tool_calls: vec![],
        stop_reason: StopReason::EndTurn,
        usage: TokenUsage {
            input_tokens: 5,
            output_tokens: 5,
            ..Default::default()
        },
        provider_index: None,
    }
}

/// A tool call with empty arguments and no metadata.
pub(crate) fn tc(id: &str, name: &str) -> ToolCall {
    ToolCall {
        id: id.into(),
        name: name.into(),
        arguments: serde_json::json!({}),
        metadata: None,
    }
}

/// Standard test agent: episode store under `<memory_dir>/.octos`, no
/// episode persistence, no auto file sends.
pub(crate) async fn boot_agent(
    id: &str,
    memory_dir: &TempDir,
    llm: std::sync::Arc<dyn LlmProvider>,
    tools: ToolRegistry,
) -> Agent {
    let memory = std::sync::Arc::new(
        EpisodeStore::open(memory_dir.path().join(".octos"))
            .await
            .unwrap(),
    );
    Agent::new(AgentId::new(id), llm, tools, memory).with_config(AgentConfig {
        save_episodes: false,
        suppress_auto_send_files: true,
        ..Default::default()
    })
}
