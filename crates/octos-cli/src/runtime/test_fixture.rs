//! `#[doc(hidden)]` test scaffolding shared by runtime unit tests and
//! integration tests. Not part of the supported API — production code must
//! not reference anything here.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;

use octos_agent::{SandboxConfig, ToolRegistry, create_sandbox};
use octos_core::Message;
use octos_llm::{ChatConfig, ChatResponse, LlmProvider, ToolSpec};
use octos_memory::{EpisodeStore, MemoryStore};

use super::ProfileRuntime;

/// Inert LLM: errors loudly if ever called (unit-test default).
pub struct StubLlm;

#[async_trait]
impl LlmProvider for StubLlm {
    async fn chat(
        &self,
        _messages: &[Message],
        _tools: &[ToolSpec],
        _config: &ChatConfig,
    ) -> eyre::Result<ChatResponse> {
        Err(eyre::eyre!("stub LLM not callable in tests"))
    }
    fn model_id(&self) -> &str {
        "stub-model"
    }
    fn provider_name(&self) -> &str {
        "stub"
    }
}

/// Fully-formed [`ProfileRuntime`] for tests: the 27-field struct literal is
/// pinned here once; tests vary behaviour only through the parameters.
#[allow(clippy::too_many_arguments)]
pub async fn profile_runtime_fixture(
    profile_id: &str,
    data_dir: PathBuf,
    llm: Arc<dyn LlmProvider>,
    primary_model_id: &str,
    system_prompt: &str,
    memory_refresh_enabled: bool,
    sandbox: SandboxConfig,
) -> Arc<ProfileRuntime> {
    std::fs::create_dir_all(&data_dir).unwrap();
    let memory = Arc::new(EpisodeStore::open(&data_dir).await.unwrap());
    let memory_store = Arc::new(MemoryStore::open(&data_dir).await.unwrap());
    let base_tools = ToolRegistry::with_builtins_and_sandbox(&data_dir, create_sandbox(&sandbox));
    Arc::new(ProfileRuntime {
        profile_id: profile_id.to_string(),
        data_dir,
        session_store_root: None,
        config: crate::config::Config::default(),
        llm,
        runtime_qos_catalog: None,
        primary_model_id: primary_model_id.to_string(),
        provider_name: "stub".to_string(),
        credentials: HashMap::new(),
        skills_dir: None,
        tool_policy: None,
        default_sandbox: sandbox,
        max_iterations: None,
        session_defaults: None,
        agent_profile: None,
        format_after_edit: false,
        snapshots: None,
        tool_specs: Arc::new(base_tools),
        human_approval_rules: None,
        prompt_parts: crate::commands::gateway::prompt::GatewayPromptParts {
            pre_memory: system_prompt.to_string(),
            post_memory: String::new(),
        },
        system_prompt: system_prompt.to_string(),
        memory,
        memory_store,
        embedder: None,
        memory_inject_tokens: 2500,
        memory_refresh_enabled,
        hook_executor: None,
    })
}
