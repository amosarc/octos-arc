//! Integration tests for the [`SessionSummary`] schema-version contract.
//!
//! The LLM-iterative summarizer they once exercised was retired (round-9);
//! these cases pin the persistence compatibility behaviours per
//! `docs/OCTOS_HARNESS_ABI_VERSIONING.md`.
//!
//! Run with `cargo test -p octos-agent --test session_summary`.

use std::path::{Path, PathBuf};

use octos_core::{SESSION_SUMMARY_SCHEMA_VERSION, SessionSummary};

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
}

fn load_fixture(name: &str) -> String {
    let path = fixtures_dir().join(name);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("read fixture {}: {err}", path.display()))
}

#[test]
fn should_default_missing_schema_version_to_v1() {
    // Fixture is a pre-M6.4 SessionSummary JSON with no `schema_version`
    // line. It must deserialize cleanly and pin to v1 so older persisted
    // summaries replay across harness upgrades.
    let raw = load_fixture("session_summary_legacy.json");
    let parsed: SessionSummary = serde_json::from_str(&raw).expect("legacy fixture parses");
    assert_eq!(
        parsed.schema_version, SESSION_SUMMARY_SCHEMA_VERSION,
        "missing schema_version must default to v1",
    );
    parsed
        .validate_schema_version()
        .expect("legacy version is supported");
    assert_eq!(parsed.decisions.len(), 1);
    assert_eq!(parsed.decisions[0].at_turn, 0);
}

#[test]
fn should_reject_future_schema_version_with_actionable_error() {
    // Craft a payload advertising a future version. The typed error must
    // name the type and advise upgrading — no panic.
    let raw = serde_json::json!({
        "schema_version": SESSION_SUMMARY_SCHEMA_VERSION + 12,
        "goal": "future",
        "constraints": [],
        "progress_done": [],
        "progress_in_progress": [],
        "decisions": [],
        "files": [],
        "next_steps": []
    })
    .to_string();
    let parsed: SessionSummary = serde_json::from_str(&raw).expect("JSON parses");
    let err = parsed
        .validate_schema_version()
        .expect_err("future schema version must be rejected with a typed error, not panic");
    assert_eq!(err.found, SESSION_SUMMARY_SCHEMA_VERSION + 12);
    assert_eq!(err.supported, SESSION_SUMMARY_SCHEMA_VERSION);
    let rendered = err.to_string();
    assert!(rendered.contains("SessionSummary"));
    assert!(rendered.contains("upgrade octos"));
}
