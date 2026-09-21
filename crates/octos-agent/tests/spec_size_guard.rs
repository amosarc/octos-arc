//! Spec-size regression guard (token plan, iteration 1).
//!
//! The provider-facing tool array is the first segment of every cached
//! prompt prefix: its byte cost is paid on every non-cached request and on
//! every prefix rebuild. This guard pins the total serialized spec size so
//! description/schema growth has to argue against the budget instead of
//! silently creeping. Baseline: 14,319 B over 26 tools before the
//! iteration-1 slim; 9,090 B over 21 tools after (alias + discovery-meta
//! exclusion, description/schema compaction).
//!
//! Budget note: the plan's aspirational target was 8,500 B. The last ~600 B
//! are held by (a) behavioral notes whose removal provably causes model
//! rework loops (edit_file match semantics, apply_patch envelope grammar),
//! (b) read_file's #1638 R6 origin/main byte-lock, (c) Codex-parity schema
//! surface (spawn_agent `items`/role enum). Cutting any of those trades
//! cheap bytes for expensive retry turns — revisit only with bench evidence.

use octos_agent::tools::ToolRegistry;

const SPEC_BUDGET_BYTES: usize = 9_200;

fn spec_rows(registry: &ToolRegistry) -> Vec<(String, usize)> {
    let mut rows: Vec<(String, usize)> = registry
        .specs()
        .iter()
        .map(|spec| {
            (
                spec.name.clone(),
                serde_json::to_string(spec).map(|v| v.len()).unwrap_or(0),
            )
        })
        .collect();
    rows.sort_by_key(|(_, size)| std::cmp::Reverse(*size));
    rows
}

#[test]
fn slimmed_specs_fit_the_budget() {
    let registry = ToolRegistry::with_builtins(std::env::temp_dir()).with_spec_slim(true);
    let rows = spec_rows(&registry);
    let total: usize = rows.iter().map(|(_, size)| size).sum();

    println!("TOOL_COUNT={}", rows.len());
    println!("TOTAL_SPEC_BYTES={total} (budget {SPEC_BUDGET_BYTES})");
    for (name, size) in &rows {
        println!("  {name}: {size}");
    }

    assert!(
        total <= SPEC_BUDGET_BYTES,
        "serialized tool specs are {total} B, budget is {SPEC_BUDGET_BYTES} B — \
         slim a description or schema note instead of raising this budget"
    );
}

#[test]
fn spec_slim_hides_alias_duplicates_but_keeps_them_registered() {
    let registry = ToolRegistry::with_builtins(std::env::temp_dir()).with_spec_slim(true);
    let names: Vec<String> = registry.specs().iter().map(|s| s.name.clone()).collect();
    for alias in octos_agent::tools::SPEC_SLIM_EXCLUDED {
        assert!(
            !names.iter().any(|n| n == alias),
            "alias '{alias}' must be absent from slimmed specs()"
        );
        assert!(
            registry.get(alias).is_some(),
            "alias '{alias}' must stay registered and executable"
        );
    }
    // The canonical names the model IS advertised stay present.
    for canonical in ["bash", "spawn_agent", "wait_agent"] {
        assert!(
            names.iter().any(|n| n == canonical),
            "canonical tool '{canonical}' missing from slimmed specs()"
        );
    }
}

#[test]
fn disabling_spec_slim_restores_the_full_array() {
    let slim = ToolRegistry::with_builtins(std::env::temp_dir()).with_spec_slim(true);
    let full = ToolRegistry::with_builtins(std::env::temp_dir()).with_spec_slim(false);
    assert!(full.specs().len() > slim.specs().len());
    let full_names: Vec<String> = full.specs().iter().map(|s| s.name.clone()).collect();
    for alias in octos_agent::tools::SPEC_SLIM_EXCLUDED {
        assert!(
            full_names.iter().any(|n| n == alias),
            "'{alias}' missing from full specs()"
        );
    }
}
