//! Model catalog with capabilities, costs, and aliases.
//!
//! Provides programmatic model discovery: check if a model supports
//! vision, tool use, streaming, and look up cost per token. Also owns the
//! serde shapes for the canonical `model_catalog.json` (`QosCatalog`), the
//! single source of truth the provider registry reads family defaults from
//! and the CLI seeds context-window / pricing tables from.

use serde::{Deserialize, Serialize};

/// Model capability type for routing decisions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ModelType {
    /// High-quality output, thorough analysis (>4000 tokens in deep search).
    Strong,
    /// Low latency, quick responses (<50s deep search or <1s tool call).
    Fast,
}

/// Unified model catalog entry — single source of truth for model metadata + live QoS.
fn is_false(value: &bool) -> bool {
    !*value
}

/// Static fields (type, cost, ds_output) are loaded from `model_catalog.json`.
/// Dynamic fields (stability, tool_avg_ms, p95_ms, score) were updated by the
/// retired QoS scanner and now persist unchanged.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelCatalogEntry {
    /// Provider/model key, e.g. "zai/glm-5.3".
    pub provider: String,
    /// Model capability type.
    #[serde(rename = "type")]
    pub model_type: ModelType,
    /// Whether this row is its provider family's default model — the model a
    /// family resolves to when a profile names no model. Exactly one row per
    /// family carries it; `registry::catalog_default_model` reads it.
    ///
    /// Skipped when false so the per-profile catalogs the CLI rewrites do not
    /// sprout `"default": false` on every row.
    #[serde(default, rename = "default", skip_serializing_if = "is_false")]
    pub is_family_default: bool,
    /// Tool call stability (0.0 to 1.0). Updated by the QoS scanner.
    pub stability: f64,
    /// Average tool call latency in ms. Updated by the QoS scanner.
    pub tool_avg_ms: u64,
    /// P95 tool call latency in ms. Updated by the QoS scanner.
    pub p95_ms: u64,
    /// Composite QoS score (lower = better). Updated by the QoS scanner.
    pub score: f64,
    /// Input cost in USD per million tokens.
    pub cost_in: f64,
    /// Output cost in USD per million tokens.
    pub cost_out: f64,
    /// Deep search output token count (quality indicator). 0 = not evaluated.
    #[serde(default)]
    pub ds_output: u64,
    /// Context window size in tokens. 0 = unknown.
    #[serde(default)]
    pub context_window: u64,
    /// Maximum output tokens. 0 = unknown.
    #[serde(default)]
    pub max_output: u64,
}

/// Full model catalog with timestamp.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QosCatalog {
    pub updated_at: String,
    pub models: Vec<ModelCatalogEntry>,
}
