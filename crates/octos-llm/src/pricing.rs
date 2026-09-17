//! Model pricing for cost estimation.
//!
//! Prices are approximate and may become stale. Last updated: 2025-02.
//! Source: provider pricing pages. Update when models or prices change.

/// Pricing per 1M tokens (input, output) in USD.
#[derive(Debug, Clone, Copy)]
pub struct ModelPricing {
    pub input_per_million: f64,
    pub output_per_million: f64,
}

use std::collections::HashMap;
use std::sync::RwLock;

/// Cached pricing from runtime catalog.
static PRICING_CATALOG: RwLock<Option<HashMap<String, ModelPricing>>> = RwLock::new(None);

/// Seed pricing from model_catalog.json entries.
/// Called at startup alongside context::seed_from_catalog().
pub fn seed_pricing_catalog(entries: &[(String, f64, f64)]) {
    let mut map = HashMap::new();
    // A bare model id can be shared by several catalog rows — the native
    // provider (`moonshot/kimi-k2.5`) and its re-hosts
    // (`openrouter/moonshotai/kimi-k2.5`, `nvidia/…`). A plain last-writer-wins
    // insert would let a re-host clobber the native rate under the shared bare
    // alias `kimi-k2.5`, mispricing direct requests (which look up the bare id).
    // Award the bare alias to the row with the FEWEST path segments (the most
    // canonical/native), so the result is deterministic and order-independent.
    // On an EQUAL segment count the more-native HOST wins: compare the provider
    // prefix (segment before the first `/`) FIRST, then the full lowercased key —
    // `minimax/MiniMax-M3` ($0.15/$1.5) and `r9s/minimax-m3` ($0.5/$2) both have
    // one slash, so without a tie-breaker the bare `minimax-m3` rate would depend
    // on which row the router exported first. Comparing the provider prefix
    // (rather than the raw key) keeps a native `zai/…` row ahead of a
    // `zai-coding/…` re-host, whose `-` would otherwise sort before the native
    // row's `/` (mirrors `context::build_catalog_map`). (Tracking the owner's key
    // rather than only its depth is what lets the tie-break compare keys.)
    //
    // The bare alias is LOWERCASED (`catalog_pricing` lowercases the requested id
    // before lookup) so a case-variant native key like `minimax/MiniMax-M2.5` is
    // still reachable via `minimax-m2.5`. The full provider-qualified key,
    // however, is stored in its ORIGINAL case: lowercasing it would drop it into
    // the bare model-ID namespace, where a re-host's `model_id()` can exact-hit
    // it — `minimax/MiniMax-M2.5` (native, $0.50) lowercased to
    // `minimax/minimax-m2.5` is exactly what `OpenRouterProvider::model_id()`
    // returns for the re-hosted `openrouter/minimax/minimax-m2.5` ($0.29), so an
    // OpenRouter cost lookup would exact-select the native rate. NOTE: a bare
    // shared model name still resolves to the native (fewest-segments) rate for
    // COST ESTIMATION; distinguishing a re-host lane's own rate needs the
    // caller's provider family, which the `model_pricing(model_id)` API does not
    // carry (7 catalog model-ids are re-hosted by 2+ providers at different
    // rates, so a key-only heuristic cannot disambiguate them). That is a
    // deliberate, documented estimation limitation, not something this seeding
    // can fix on its own.
    let mut bare_owner: HashMap<String, (usize, String)> = HashMap::new();
    for (key, cost_in, cost_out) in entries {
        if *cost_in > 0.0 || *cost_out > 0.0 {
            let pricing = ModelPricing {
                input_per_million: *cost_in,
                output_per_million: *cost_out,
            };
            map.insert(key.clone(), pricing);
            if let Some(model) = key.split('/').next_back() {
                let bare = model.to_lowercase();
                let segments = key.matches('/').count();
                // Lowercased only for the deterministic tie-break comparison; the
                // full key itself is inserted above in its original case.
                let key_lower = key.to_lowercase();
                let take = match bare_owner.get(&bare) {
                    None => true,
                    Some((owned_seg, owner_key)) => {
                        segments < *owned_seg
                            || (segments == *owned_seg
                                && (
                                    crate::context::provider_prefix(&key_lower),
                                    key_lower.as_str(),
                                ) < (
                                    crate::context::provider_prefix(owner_key),
                                    owner_key.as_str(),
                                ))
                    }
                };
                if take {
                    map.insert(bare.clone(), pricing);
                    bare_owner.insert(bare, (segments, key_lower));
                }
            }
        }
    }
    *PRICING_CATALOG.write().unwrap_or_else(|e| e.into_inner()) = Some(map);
}

fn catalog_pricing(model_id: &str) -> Option<ModelPricing> {
    let guard = PRICING_CATALOG.read().ok()?;
    let map = guard.as_ref()?;
    let m = model_id.to_lowercase();
    if let Some(p) = map.get(&m) {
        return Some(*p);
    }
    // The substring fallback used to return the FIRST HashMap hit, which
    // made pricing nondeterministic whenever a model id matched several
    // keys ("gpt-5.2-codex" matches both "gpt-5.2" and "gpt-5" — which
    // one won differed per process). Deterministic rule instead:
    //   1. Keys the model id CONTAINS name a family the id extends; the
    //      LONGEST such key is the most specific family, so it wins.
    //   2. Otherwise, among keys that contain the model id, the SHORTEST
    //      is the closest match.
    // Ties break lexicographically so equal-length keys are stable too.
    let family = map
        .iter()
        .filter(|(key, _)| m.contains(key.as_str()))
        .max_by(|(a, _), (b, _)| a.len().cmp(&b.len()).then_with(|| b.cmp(a)));
    if let Some((_, p)) = family {
        return Some(*p);
    }
    map.iter()
        .filter(|(key, _)| key.contains(&m))
        .min_by(|(a, _), (b, _)| a.len().cmp(&b.len()).then_with(|| a.cmp(b)))
        .map(|(_, p)| *p)
}

/// Prompt-cache read multiplier on the input rate (Anthropic bills cache
/// hits at ~10% of the base input price).
const CACHE_READ_INPUT_MULTIPLIER: f64 = 0.1;
/// Prompt-cache write multiplier on the input rate (Anthropic bills 5-minute
/// ephemeral cache writes at 1.25x the base input price).
const CACHE_WRITE_INPUT_MULTIPLIER: f64 = 1.25;

/// Provider-specific prompt-cache billing multipliers on the base input rate.
///
/// #2194 review: cache economics are a PROTOCOL/provider property, not a
/// universal constant — pricing OpenAI or Gemini cache traffic at Anthropic's
/// 0.1x/1.25x misprices both. The per-provider rate cards live in
/// sources, and the protocol keying.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CacheRates {
    /// Cache-read (cache-hit) tokens bill at this multiple of the input rate.
    pub read_multiplier: f64,
    /// Cache-write tokens bill at this multiple of the input rate. They are
    /// DISJOINT from `input_tokens`, so a 0.0 here makes a reported write
    /// VANISH from cost — only correct for a protocol that never reports
    /// writes (Google), never for one that does.
    pub write_multiplier: f64,
}

/// Rate card for a [`CacheLane`] taken from the answering slot's
/// [`ProviderMetadata`]. This is the AUTHORITATIVE path — the lane is set from
/// the provider TYPE at construction, so it needs no label guessing and prices
/// a relabeled Anthropic proxy (zai/r9s/custom+anthropic) correctly. The
/// label-guessing helpers remain only for the legacy string-only
/// reprice fallbacks that carry no metadata.
pub fn cache_rates_for_lane(lane: crate::types::CacheLane) -> CacheRates {
    use crate::types::CacheLane;
    match lane {
        CacheLane::Anthropic => CacheRates {
            read_multiplier: CACHE_READ_INPUT_MULTIPLIER,
            write_multiplier: CACHE_WRITE_INPUT_MULTIPLIER,
        },
        CacheLane::Gemini => CacheRates {
            read_multiplier: 0.25,
            write_multiplier: 0.0,
        },
        CacheLane::Residual => CacheRates {
            read_multiplier: 1.0,
            write_multiplier: CACHE_WRITE_INPUT_MULTIPLIER,
        },
    }
}

impl ModelPricing {
    /// Calculate cost for given token counts.
    pub fn cost(&self, input_tokens: u32, output_tokens: u32) -> f64 {
        (input_tokens as f64 / 1_000_000.0) * self.input_per_million
            + (output_tokens as f64 / 1_000_000.0) * self.output_per_million
    }

    /// Cache-aware cost at ANTHROPIC's multipliers (0.1x read / 1.25x write)
    /// under the crate-wide DISJOINT accounting contract: `input_tokens`
    /// excludes cached tokens. Degenerates to [`Self::cost`] when both cache
    /// counts are zero.
    ///
    /// Cache-aware cost keyed on the answering slot's [`ProviderMetadata`]
    /// cache lane (the AUTHORITATIVE, provider-type-sourced rate). Use this
    /// wherever the metadata is in hand; it prices relabeled Anthropic proxies
    /// (and `custom` + `api_type=anthropic`) correctly without guessing from
    /// the label, whose value must stay the slot's logical identity for
    /// adaptive-lane / QoS matching.
    pub fn cost_with_cache_for_metadata(
        &self,
        metadata: &crate::types::ProviderMetadata,
        input_tokens: u32,
        output_tokens: u32,
        cache_read_tokens: u32,
        cache_write_tokens: u32,
    ) -> f64 {
        self.cost_with_cache_rates(
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            cache_rates_for_lane(metadata.cache_lane),
        )
    }

    fn cost_with_cache_rates(
        &self,
        input_tokens: u32,
        output_tokens: u32,
        cache_read_tokens: u32,
        cache_write_tokens: u32,
        rates: CacheRates,
    ) -> f64 {
        self.cost(input_tokens, output_tokens)
            + (cache_read_tokens as f64 / 1_000_000.0)
                * self.input_per_million
                * rates.read_multiplier
            + (cache_write_tokens as f64 / 1_000_000.0)
                * self.input_per_million
                * rates.write_multiplier
    }
}

/// Look up pricing for a model. Checks the runtime catalog first,
/// falls back to hardcoded defaults for models not in the catalog.
pub fn model_pricing(model_id: &str) -> Option<ModelPricing> {
    // Check runtime catalog first (populated from model_catalog.json)
    if let Some(pricing) = catalog_pricing(model_id) {
        return Some(pricing);
    }
    // Fallback to hardcoded defaults for models not in catalog
    let m = model_id.to_lowercase();

    // Anthropic
    if m.contains("claude-opus-4") || m.contains("claude-4-opus") {
        return Some(ModelPricing {
            input_per_million: 15.0,
            output_per_million: 75.0,
        });
    }
    if m.contains("claude-sonnet-4") || m.contains("claude-4-sonnet") {
        return Some(ModelPricing {
            input_per_million: 3.0,
            output_per_million: 15.0,
        });
    }
    if m.contains("claude-3-5-sonnet") {
        return Some(ModelPricing {
            input_per_million: 3.0,
            output_per_million: 15.0,
        });
    }
    if m.contains("claude-3-5-haiku") || m.contains("claude-haiku") {
        return Some(ModelPricing {
            input_per_million: 0.80,
            output_per_million: 4.0,
        });
    }

    // OpenAI — NOTE: gpt-4o-mini MUST be checked before gpt-4o (substring match)
    if m.contains("gpt-4o-mini") {
        return Some(ModelPricing {
            input_per_million: 0.15,
            output_per_million: 0.60,
        });
    }
    if m.contains("gpt-4o") {
        return Some(ModelPricing {
            input_per_million: 2.50,
            output_per_million: 10.0,
        });
    }
    if m.starts_with("o3") || m.starts_with("o4") {
        return Some(ModelPricing {
            input_per_million: 10.0,
            output_per_million: 40.0,
        });
    }

    // Gemini
    if m.contains("gemini-2") || m.contains("gemini-1.5") {
        return Some(ModelPricing {
            input_per_million: 0.075,
            output_per_million: 0.30,
        });
    }

    // DeepSeek
    if m.contains("deepseek-r1") {
        return Some(ModelPricing {
            input_per_million: 0.55,
            output_per_million: 2.19,
        });
    }
    if m.contains("deepseek") {
        return Some(ModelPricing {
            input_per_million: 0.27,
            output_per_million: 1.10,
        });
    }

    // Qwen
    if m.contains("qwen3-coder") || m.contains("qwen3-235b") || m.contains("qwen3.5") {
        return Some(ModelPricing {
            input_per_million: 0.30,
            output_per_million: 1.20,
        });
    }
    if m.contains("qwen") {
        return Some(ModelPricing {
            input_per_million: 0.15,
            output_per_million: 0.60,
        });
    }

    // Llama (via NVIDIA NIM / Groq — pricing varies by host, using NVIDIA NIM rates)
    if m.contains("llama-3.1-405b") || m.contains("llama-3.1-nemotron-ultra") {
        return Some(ModelPricing {
            input_per_million: 5.00,
            output_per_million: 15.0,
        });
    }
    if m.contains("llama-3.3-70b") || m.contains("llama-3.1-70b") || m.contains("llama-4-maverick")
    {
        return Some(ModelPricing {
            input_per_million: 0.40,
            output_per_million: 1.60,
        });
    }
    if m.contains("llama-4-scout") || m.contains("llama3-70b") {
        return Some(ModelPricing {
            input_per_million: 0.30,
            output_per_million: 1.20,
        });
    }
    // Match "llama" but not "ollama" (local runner, no pricing)
    if (m.contains("llama") && !m.contains("ollama")) || m.contains("meta/llama") {
        return Some(ModelPricing {
            input_per_million: 0.10,
            output_per_million: 0.40,
        });
    }

    // Mistral
    if m.contains("mistral-large") {
        return Some(ModelPricing {
            input_per_million: 2.00,
            output_per_million: 6.00,
        });
    }
    if m.contains("mistral") || m.contains("mixtral") {
        return Some(ModelPricing {
            input_per_million: 0.20,
            output_per_million: 0.60,
        });
    }

    // Kimi / Moonshot — NOTE: kimi-k3 MUST be checked before the generic
    // kimi-k2/moonshot branch: the full provider key ("moonshot/kimi-k3")
    // contains both substrings. K3 official rates: $3.00/M input (cache
    // miss) / $15.00/M output.
    if m.contains("kimi-k3") {
        return Some(ModelPricing {
            input_per_million: 3.00,
            output_per_million: 15.0,
        });
    }
    if m.contains("kimi-k2") || m.contains("moonshot") {
        return Some(ModelPricing {
            input_per_million: 0.60,
            output_per_million: 2.40,
        });
    }
    if m.contains("kimi") {
        return Some(ModelPricing {
            input_per_million: 0.30,
            output_per_million: 1.20,
        });
    }

    // MiniMax
    if m.contains("minimax-m1") || m.contains("minimax-m2") {
        return Some(ModelPricing {
            input_per_million: 0.50,
            output_per_million: 2.00,
        });
    }
    if m.contains("minimax") {
        return Some(ModelPricing {
            input_per_million: 0.20,
            output_per_million: 1.10,
        });
    }

    // Zhipu GLM
    if m.contains("glm-5") || m.contains("glm5") {
        return Some(ModelPricing {
            input_per_million: 0.50,
            output_per_million: 2.00,
        });
    }
    if m.contains("glm-4") || m.contains("glm4") {
        return Some(ModelPricing {
            input_per_million: 0.30,
            output_per_million: 1.20,
        });
    }

    // NVIDIA Nemotron
    if m.contains("nemotron-super") || m.contains("nemotron-ultra") {
        return Some(ModelPricing {
            input_per_million: 1.50,
            output_per_million: 5.00,
        });
    }
    if m.contains("nemotron") {
        return Some(ModelPricing {
            input_per_million: 0.20,
            output_per_million: 0.80,
        });
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_known_model_pricing() {
        let p = model_pricing("claude-sonnet-4-20250514").unwrap();
        assert!((p.input_per_million - 3.0).abs() < f64::EPSILON);
        assert!((p.output_per_million - 15.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_cost_calculation() {
        let p = ModelPricing {
            input_per_million: 3.0,
            output_per_million: 15.0,
        };
        let cost = p.cost(1_000_000, 100_000);
        // $3.00 input + $1.50 output = $4.50
        assert!((cost - 4.5).abs() < 0.001);
    }

    #[test]
    fn test_unknown_model_returns_none() {
        assert!(model_pricing("my-local-model").is_none());
        assert!(model_pricing("ollama/phi-custom").is_none());
    }
}
