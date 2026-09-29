//! Configuration for chat requests.

use serde::{Deserialize, Serialize};

/// Provider-neutral prompt-cache metadata attached by the agent immediately
/// before dispatch. Providers may consume only the fields they explicitly
/// support; compatible/unknown endpoints must omit provider-specific wire
/// fields entirely.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PromptCacheContext {
    /// Stable, privacy-preserving traffic-affinity key (max 64 chars).
    pub affinity_key: String,
    /// Identity of the current cache-relevant System/tool/model epoch.
    pub epoch_id: String,
    pub stable_prefix_hash: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub semantic_boundaries: Vec<SemanticCheckpointHint>,
}

/// Optional semantic boundary offered to local/hybrid runtimes. It is a hint,
/// never proof that a checkpoint exists and never required for correctness.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SemanticCheckpointHint {
    pub boundary_id: String,
    pub boundary_kind: String,
    pub prefix_hash: String,
    pub prefix_token_estimate: usize,
    pub estimated_recompute_tokens: usize,
    pub checkpoint_priority: u8,
}

/// Configuration for a chat completion request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatConfig {
    /// Per-request model override (ARC 请求端视觉路由 2026-09-28): when set,
    /// THIS request targets the named model on the same endpoint and
    /// credentials. `ModelHints` are re-derived from the override name, so
    /// provider quirks (GLM reasoning tiers, kimi reasoning stubs, system
    /// merging) follow the effective model, not the lane default. Requesters
    /// that only sometimes carry images set it conditionally; an empty/None
    /// value keeps the lane's default model.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    /// Maximum tokens to generate.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    /// Temperature for sampling (0.0 = deterministic, 1.0 = creative).
    ///
    /// On the Anthropic protocol path, `Some(0.0)` — the built-in default —
    /// is treated as "unset" and stays off the wire (the `#2172` invariant:
    /// no-override requests must be byte-identical), and any value is
    /// suppressed for every model except GLM via z.ai (a default-deny
    /// allowlist: first-party Claude — Opus 4.7+/Sonnet 5 — and custom
    /// endpoints reject sampling; GLM accepts it). Use e.g. `0.01` for
    /// near-greedy decoding on GLM.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    /// How the model should choose tools.
    #[serde(default)]
    pub tool_choice: ToolChoice,
    /// Stop sequences.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stop_sequences: Vec<String>,
    /// Reasoning effort for thinking models (none/low/medium/high/max).
    /// Maps to provider-specific parameters (OpenAI reasoning.effort,
    /// Anthropic thinking budget, Gemini thinkingConfig).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<ReasoningEffort>,
    /// Binary thinking switch (enabled/disabled), exposed next to
    /// `reasoning_effort` the way `model` is exposed (user spec 2026-09-29).
    /// Default (`None`) is DISABLED — see [`ChatConfig::effective_thinking`]
    /// for how the two params resolve.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thinking: Option<Thinking>,
    /// Structured output format. When set, the model will return responses
    /// conforming to the given schema (JSON mode or JSON Schema).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub response_format: Option<ResponseFormat>,
    /// Anthropic-only: opaque `context_management` payload (e.g. the
    /// `clear_tool_uses_20250919` config) that decorates the request so the
    /// server performs tier-2 tool-use clearing on its side. M8.5 wires this
    /// from the `ApiMicroCompactionConfig` builder. Providers other than
    /// Anthropic ignore this field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_management: Option<serde_json::Value>,
    /// Extra sampler params for OpenAI-compatible servers, flattened verbatim
    /// into the request body — e.g. `{"repeat_penalty": 1.1, "top_p": 0.95}`.
    /// For params octos does not model (`repeat_penalty`, `top_p`, `top_k`,
    /// `min_p`, `frequency_penalty`, `presence_penalty`, …). `None` → nothing is
    /// added, so cloud requests are unchanged. Do not put `temperature` /
    /// `max_tokens` here — use their dedicated fields. See issue #2172.
    ///
    /// On the Anthropic protocol path only `top_p` / `top_k` exist in the
    /// Messages API, and they are forwarded only to GLM via z.ai (a
    /// default-deny allowlist); every other key — and everything, on any
    /// non-GLM model (first-party Claude, custom endpoints), which rejects
    /// sampling — is dropped with a warning naming the dropped keys.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sampling_params: Option<serde_json::Map<String, serde_json::Value>>,
    /// Per-request prompt-cache retention preference.
    ///
    /// [`CacheRetention::None`] asks the provider to skip prompt-cache
    /// WRITES for this one request — on Anthropic that means emitting NO
    /// `cache_control` breakpoints, since every breakpoint both reads and
    /// writes and an unread write still bills the 1.25x premium. Set it on
    /// one-shot calls (compaction summaries, sub-agent digests) whose
    /// prefix is never sent again; the agent loop, whose prefix IS replayed
    /// every iteration, must stay on [`CacheRetention::Default`]. Mirrors
    /// pi's `cacheRetention: "none"` on its summarization requests.
    /// Providers without explicit cache breakpoints ignore this field.
    #[serde(default, skip_serializing_if = "CacheRetention::is_default")]
    pub cache_retention: CacheRetention,
    /// Internal prompt-cache affinity and semantic checkpoint metadata.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompt_cache_context: Option<PromptCacheContext>,
}

/// Prompt-cache retention for a single request. See
/// [`ChatConfig::cache_retention`].
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CacheRetention {
    /// The provider's configured caching behavior (on Anthropic: the three
    /// ephemeral `cache_control` breakpoints, unless the provider was built
    /// with caching disabled).
    #[default]
    Default,
    /// Do not write this request's prefix to the provider's prompt cache.
    None,
}

impl CacheRetention {
    /// `skip_serializing_if` hook: an unset preference stays off the wire so
    /// persisted `ChatConfig` JSON keeps its pre-field shape.
    pub fn is_default(&self) -> bool {
        matches!(self, CacheRetention::Default)
    }
}

/// Structured output format for chat responses.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResponseFormat {
    /// Plain text (default behavior).
    Text,
    /// JSON mode — model returns valid JSON but without schema enforcement.
    JsonObject,
    /// JSON Schema mode — model returns JSON conforming to the provided schema.
    JsonSchema {
        /// Schema name (required by OpenAI).
        name: String,
        /// JSON Schema the response must conform to.
        schema: serde_json::Value,
        /// Whether to enforce strict schema adherence (default: true).
        #[serde(default = "default_strict")]
        strict: bool,
    },
}

fn default_strict() -> bool {
    true
}

/// Reasoning effort level for thinking/reasoning models.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningEffort {
    /// Disable reasoning where the provider supports it.
    #[serde(rename = "none")]
    Disabled,
    Low,
    Medium,
    High,
    /// Maximum reasoning. DeepSeek V4 accepts `reasoning_effort:"max"`; providers
    /// without a distinct max tier (OpenAI/Grok) clamp this to `high`, and Gemini
    /// maps it to an unbounded thinking budget.
    Max,
}

/// Binary thinking switch (user spec 2026-09-29): `thinking` parameter
/// exposed next to `reasoning_effort`, values `enabled`/`disabled`, default
/// disabled. Independent of — and master over — the graded effort.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Thinking {
    Enabled,
    Disabled,
}

/// Effective thinking state resolved from the `thinking` +
/// `reasoning_effort` parameters (see [`ChatConfig::effective_thinking`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffectiveThinking {
    /// No reasoning: emit the provider's off control where one exists.
    Off,
    /// Reasoning on at the provider's default depth: toggle-based styles
    /// emit the enabled toggle; effort-field styles emit nothing.
    DefaultDepth,
    /// Reasoning on at a graded level.
    Level(ReasoningEffort),
}

impl ChatConfig {
    /// Resolve the exposed `thinking` + `reasoning_effort` pair into the
    /// per-request thinking state. Precedence:
    ///
    /// 1. `thinking: "disabled"` — or `reasoning_effort: "none"` — means
    ///    off; an explicit off always wins.
    /// 2. A graded effort (`low`/`high`/`max`) implies thinking on at that
    ///    level, even when `thinking` is unset.
    /// 3. `thinking: "enabled"` with no grade is on at the provider's
    ///    default depth.
    /// 4. Both unset (the API default) is OFF.
    pub fn effective_thinking(&self) -> EffectiveThinking {
        match (self.thinking, self.reasoning_effort) {
            (Some(Thinking::Disabled), _)
            | (_, Some(ReasoningEffort::Disabled))
            | (None, None) => EffectiveThinking::Off,
            (_, Some(level)) => EffectiveThinking::Level(level),
            (Some(Thinking::Enabled), None) => EffectiveThinking::DefaultDepth,
        }
    }
}

impl Default for ChatConfig {
    fn default() -> Self {
        Self {
            model: None,
            max_tokens: Some(crate::context::default_max_tokens()),
            temperature: Some(0.0),
            tool_choice: ToolChoice::Auto,
            stop_sequences: Vec::new(),
            reasoning_effort: None,
            thinking: None,
            response_format: None,
            context_management: None,
            sampling_params: None,
            cache_retention: CacheRetention::Default,
            prompt_cache_context: None,
        }
    }
}

/// How the model should choose tools.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolChoice {
    /// Model decides whether to use tools.
    #[default]
    Auto,
    /// Model must use a tool.
    Required,
    /// Model must not use tools.
    None,
    /// Model must use a specific tool.
    Specific { name: String },
}

impl ToolChoice {
    /// Whether this choice needs an explicit wire field. `Auto` is every
    /// provider's default, so it is omitted and request bodies built before
    /// `tool_choice` was serialized stay byte-identical (cache-relevant on
    /// providers that key on the whole request).
    fn is_explicit(&self, has_tools: bool) -> bool {
        has_tools && !matches!(self, Self::Auto)
    }

    /// OpenAI chat-completions / OpenRouter form: `"none" | "required" |
    /// {"type":"function","function":{"name":…}}`.
    pub fn openai_chat_wire(&self, has_tools: bool) -> Option<serde_json::Value> {
        if !self.is_explicit(has_tools) {
            return Option::None;
        }
        Some(match self {
            Self::Auto => serde_json::Value::String("auto".into()),
            Self::Required => serde_json::Value::String("required".into()),
            Self::None => serde_json::Value::String("none".into()),
            Self::Specific { name } => serde_json::json!({
                "type": "function",
                "function": { "name": name },
            }),
        })
    }

    /// OpenAI Responses form: `"none" | "required" |
    /// {"type":"function","name":…}`.
    pub fn openai_responses_wire(&self, has_tools: bool) -> Option<serde_json::Value> {
        if !self.is_explicit(has_tools) {
            return Option::None;
        }
        Some(match self {
            Self::Auto => serde_json::Value::String("auto".into()),
            Self::Required => serde_json::Value::String("required".into()),
            Self::None => serde_json::Value::String("none".into()),
            Self::Specific { name } => serde_json::json!({
                "type": "function",
                "name": name,
            }),
        })
    }

    /// Anthropic Messages form: `{"type":"none"|"auto"|"any"|"tool","name":…}`.
    pub fn anthropic_wire(&self, has_tools: bool) -> Option<serde_json::Value> {
        if !self.is_explicit(has_tools) {
            return Option::None;
        }
        Some(match self {
            Self::Auto => serde_json::json!({ "type": "auto" }),
            Self::Required => serde_json::json!({ "type": "any" }),
            Self::None => serde_json::json!({ "type": "none" }),
            Self::Specific { name } => serde_json::json!({ "type": "tool", "name": name }),
        })
    }

    /// Gemini `toolConfig.functionCallingConfig` form.
    pub fn gemini_function_calling_config(&self, has_tools: bool) -> Option<serde_json::Value> {
        if !self.is_explicit(has_tools) {
            return Option::None;
        }
        Some(match self {
            Self::Auto => serde_json::json!({ "mode": "AUTO" }),
            Self::Required => serde_json::json!({ "mode": "ANY" }),
            Self::None => serde_json::json!({ "mode": "NONE" }),
            Self::Specific { name } => serde_json::json!({
                "mode": "ANY",
                "allowed_function_names": [name],
            }),
        })
    }
}

#[cfg(test)]
mod tool_choice_wire_tests {
    use super::ToolChoice;

    #[test]
    fn should_omit_tool_choice_for_the_default_and_for_tool_less_requests() {
        assert!(ToolChoice::Auto.openai_chat_wire(true).is_none());
        assert!(ToolChoice::None.openai_chat_wire(false).is_none());
        assert!(ToolChoice::None.anthropic_wire(false).is_none());
        assert!(
            ToolChoice::None
                .gemini_function_calling_config(false)
                .is_none()
        );
        assert!(ToolChoice::None.openai_responses_wire(false).is_none());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_chat_config_defaults() {
        let config = ChatConfig::default();
        assert_eq!(
            config.max_tokens,
            Some(crate::context::default_max_tokens())
        );
        assert_eq!(config.temperature, Some(0.0));
        assert!(matches!(config.tool_choice, ToolChoice::Auto));
        assert!(config.stop_sequences.is_empty());
    }

    #[test]
    fn effective_thinking_resolution_table() {
        use EffectiveThinking as ET;
        let cfg = |thinking: Option<Thinking>, effort: Option<ReasoningEffort>| ChatConfig {
            thinking,
            reasoning_effort: effort,
            ..ChatConfig::default()
        };
        // API default (both unset) is OFF.
        assert_eq!(cfg(None, None).effective_thinking(), ET::Off);
        // `thinking` is the master switch; an explicit off always wins.
        assert_eq!(
            cfg(Some(Thinking::Disabled), Some(ReasoningEffort::Max)).effective_thinking(),
            ET::Off
        );
        // `reasoning_effort: "none"` is the effort family's off value.
        assert_eq!(
            cfg(Some(Thinking::Enabled), Some(ReasoningEffort::Disabled)).effective_thinking(),
            ET::Off
        );
        // A graded level implies on at that level.
        assert_eq!(
            cfg(None, Some(ReasoningEffort::Low)).effective_thinking(),
            ET::Level(ReasoningEffort::Low)
        );
        assert_eq!(
            cfg(Some(Thinking::Enabled), Some(ReasoningEffort::Max)).effective_thinking(),
            ET::Level(ReasoningEffort::Max)
        );
        // `thinking: "enabled"` with no grade is on at the provider default.
        assert_eq!(
            cfg(Some(Thinking::Enabled), None).effective_thinking(),
            ET::DefaultDepth
        );
    }

    #[test]
    fn thinking_param_serde_values() {
        assert_eq!(
            serde_json::to_value(Thinking::Enabled).unwrap(),
            serde_json::json!("enabled")
        );
        assert_eq!(
            serde_json::to_value(Thinking::Disabled).unwrap(),
            serde_json::json!("disabled")
        );
        let cfg: ChatConfig =
            serde_json::from_str(r#"{"thinking": "enabled"}"#).unwrap();
        assert_eq!(cfg.thinking, Some(Thinking::Enabled));
    }

    #[test]
    fn test_chat_config_serde_roundtrip() {
        let config = ChatConfig {
            model: None,
            max_tokens: Some(2048),
            temperature: Some(0.7),
            tool_choice: ToolChoice::Required,
            stop_sequences: vec!["STOP".to_string()],
            reasoning_effort: None,
            thinking: None,
            response_format: None,
            context_management: None,
            sampling_params: None,
            cache_retention: CacheRetention::Default,
            prompt_cache_context: None,
        };
        let json = serde_json::to_string(&config).unwrap();
        let deserialized: ChatConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.max_tokens, Some(2048));
        assert_eq!(deserialized.temperature, Some(0.7));
        assert!(matches!(deserialized.tool_choice, ToolChoice::Required));
        assert_eq!(deserialized.stop_sequences, vec!["STOP"]);
    }
}
