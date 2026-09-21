//! The `testgen` tool: the model-facing surface of the pipeline.
//!
//! Two actions, two signals:
//! - `scaffold` — host extracts the contract/validation checklist, writes
//!   the skeleton + ledger, and returns the bounded context block.
//! - `verify`   — host re-checks the ledger mechanically, runs the suite,
//!   and returns a typed [`TestGenVerdict`]: `covered`, `gap`, `app_bug`,
//!   or `test_err`. Each verdict is the next loop instruction — no prompt
//!   re-engineering, no model-side coverage rules to remember.
//!
//! The tool shares the session sandbox with check/shell (vitest executes
//! project-controlled config and test code).

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use eyre::Result;
use serde::Deserialize;

use crate::sandbox::{NoSandbox, Sandbox};
use crate::testgen::{self, coverage};
use crate::tools::{Tool, ToolResult};
/// Hard cap on scaffold target files accepted per call.
const MAX_TARGETS: usize = 32;

pub struct TestgenTool {
    working_dir: PathBuf,
    sandbox: Arc<dyn Sandbox>,
}

impl TestgenTool {
    pub fn new(cwd: impl Into<PathBuf>) -> Self {
        Self {
            working_dir: cwd.into(),
            sandbox: Arc::new(NoSandbox),
        }
    }

    /// Confine the vitest child to the session sandbox (lockstep with
    /// check/shell — see `ToolRegistry::with_builtins_and_permissions`).
    pub fn with_shared_sandbox(mut self, sandbox: Arc<dyn Sandbox>) -> Self {
        self.sandbox = sandbox;
        self
    }
}

#[derive(Deserialize)]
struct TestgenArgs {
    action: String,
    /// scaffold: workspace-relative source files/dirs to scan. Empty =
    /// profile default (backend/src, one level deep).
    #[serde(default)]
    targets: Vec<String>,
}

#[async_trait]
impl Tool for TestgenTool {
    fn name(&self) -> &str {
        "testgen"
    }

    fn description(&self) -> &str {
        "Host-driven test pipeline. `scaffold`: extract contract + write skeletons with pre-named \
         stubs — fill EVERY stub, keep titles, never edit app source. `verify`: verdict — \
         covered / gap (fill stubs) / app_bug (report) / test_err (fix your test)."
    }

    fn tags(&self) -> &[&str] {
        &["code", "test"]
    }

    fn input_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["scaffold", "verify"],
                    "description": "scaffold: extract contract + write skeletons. verify: run the check + suite, get the verdict."
                },
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "scaffold only: source files/dirs to scan (workspace-relative). Defaults to backend/src."
                }
            },
            "required": ["action"]
        })
    }

    async fn execute(&self, args: &serde_json::Value) -> Result<ToolResult> {
        let args: TestgenArgs = serde_json::from_value(args.clone())
            .map_err(|e| eyre::eyre!("invalid arguments: {e}"))?;
        match args.action.as_str() {
            "scaffold" => self.scaffold(&args.targets).await,
            "verify" => self.verify().await,
            other => Ok(ToolResult {
                output: format!("unknown action '{other}' (expected scaffold|verify)"),
                success: false,
                ..Default::default()
            }),
        }
    }
}

impl TestgenTool {
    async fn scaffold(&self, targets: &[String]) -> Result<ToolResult> {
        if targets.len() > MAX_TARGETS {
            return Ok(ToolResult {
                output: format!(
                    "too many targets ({}) — cap is {MAX_TARGETS}",
                    targets.len()
                ),
                success: false,
                ..Default::default()
            });
        }
        match testgen::scaffold(&self.working_dir, targets) {
            Ok(out) => {
                let endpoint_keys: Vec<&str> =
                    out.endpoints.iter().map(|e| e.key.as_str()).collect();
                let payload = serde_json::json!({
                    "action": "scaffold",
                    "profile": out.profile_kind,
                    "written": out.written,
                    "ledger_size": out.ledger.len(),
                    "endpoints": endpoint_keys,
                    "injected_tokens": out.injected_tokens,
                    "injected_prompt": out.injected_prompt,
                });
                Ok(ToolResult {
                    output: serde_json::to_string_pretty(&payload)?,
                    success: true,
                    ..Default::default()
                })
            }
            Err(e) => Ok(ToolResult {
                output: format!("scaffold failed: {e}"),
                success: false,
                ..Default::default()
            }),
        }
    }

    async fn verify(&self) -> Result<ToolResult> {
        let workspace = &self.working_dir;
        let Some(profile) = testgen::profile::detect(workspace) else {
            return Ok(ToolResult {
                output: "verify failed: no testgen stack profile matches this workspace".into(),
                success: false,
                ..Default::default()
            });
        };
        let ledger_path = testgen::ledger_file(workspace, &profile);
        let Ok(raw) = std::fs::read_to_string(&ledger_path) else {
            return Ok(ToolResult {
                output: "verify failed: no ledger found — run `testgen scaffold` first".into(),
                success: false,
                ..Default::default()
            });
        };
        let ledger: Vec<testgen::skeleton::LedgerStub> = match serde_json::from_str(&raw) {
            Ok(l) => l,
            Err(e) => {
                return Ok(ToolResult {
                    output: format!("verify failed: unreadable ledger ({e}) — re-run scaffold"),
                    success: false,
                    ..Default::default()
                });
            }
        };

        // Mechanical ledger check (syntax-level, zero model judgment).
        let state = match coverage::check_ledger(workspace, &ledger) {
            Ok(s) => s,
            Err(e) => {
                return Ok(ToolResult {
                    output: format!("verify failed: ledger check error: {e}"),
                    success: false,
                    ..Default::default()
                });
            }
        };

        // Run the suite — only over ledger files that exist on disk, split
        // by area (backend / frontend vitest configs live in different
        // directories).
        let (backend, frontend): (Vec<String>, Vec<String>) = ledger
            .iter()
            .map(|s| s.file.clone())
            .collect::<std::collections::HashSet<_>>()
            .into_iter()
            .filter(|f| workspace.join(f).is_file())
            .partition(|f| f.starts_with("backend/"));
        let mut summary: Option<testgen::runner::VitestSummary> = None;
        let mut raw_tail = String::new();
        for (dir, files) in [("backend", backend), ("frontend", frontend)] {
            if files.is_empty() {
                continue;
            }
            let cwd_abs = workspace.join(dir);
            let stripped: Vec<String> = files
                .iter()
                .map(|f| f.strip_prefix(&format!("{dir}/")).unwrap_or(f).to_string())
                .collect();
            match testgen::runner::run_vitest(&cwd_abs, &stripped, Some(&self.sandbox)).await {
                Ok(outcome) => {
                    if summary.is_none() {
                        summary = outcome.summary;
                    } else if let Some(s) = outcome.summary {
                        if let Some(acc) = summary.as_mut() {
                            acc.total += s.total;
                            acc.passed += s.passed;
                            acc.failed += s.failed;
                            acc.failures.extend(s.failures);
                            acc.load_errors.extend(s.load_errors);
                        }
                    }
                    if summary.is_none() && raw_tail.is_empty() {
                        raw_tail = outcome.raw_tail;
                    }
                }
                Err(e) => {
                    return Ok(ToolResult {
                        output: format!("verify failed: {e}"),
                        success: false,
                        ..Default::default()
                    });
                }
            }
        }

        let verdict = testgen::classify(&state, summary.as_ref(), &raw_tail);
        let success = matches!(verdict, testgen::TestGenVerdict::Covered);
        let payload = serde_json::json!({
            "action": "verify",
            // The typed signal: {"verdict": "covered"|"gap"|"app_bug"|"test_err", ...detail}.
            "result": verdict,
            "ledger_size": ledger.len(),
        });
        Ok(ToolResult {
            output: serde_json::to_string_pretty(&payload)?,
            success,
            ..Default::default()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testgen::skeleton::LedgerStub;
    use crate::testgen::{GapDetail, VitestSummary};

    fn ledger_state(
        missing: Vec<&str>,
        unfilled: Vec<&str>,
        skipped: Vec<&str>,
        no_assertion: Vec<&str>,
    ) -> coverage::LedgerState {
        coverage::LedgerState {
            missing: missing.into_iter().map(String::from).collect(),
            unfilled: unfilled.into_iter().map(String::from).collect(),
            skipped: skipped.into_iter().map(String::from).collect(),
            no_assertion: no_assertion.into_iter().map(String::from).collect(),
            extra: vec![],
        }
    }

    fn summary(failed: usize, load_errors: Vec<&str>) -> VitestSummary {
        VitestSummary {
            total: 4,
            passed: 4 - failed,
            failed,
            failures: if failed > 0 {
                vec![testgen::runner::CaseFailure {
                    file: "app.test.js".into(),
                    title: "POST /api/x invalid".into(),
                    message: "AssertionError: expected 200 to be 400".into(),
                }]
            } else {
                vec![]
            },
            load_errors: load_errors.into_iter().map(String::from).collect(),
        }
    }

    #[test]
    fn classification_precedence_matches_loop_needs() {
        // Load errors first: test-side responsibility.
        let v = testgen::classify(
            &ledger_state(vec![], vec![], vec![], vec![]),
            Some(&summary(2, vec!["broken.test.js: SyntaxError"])),
            "",
        );
        assert!(matches!(v, testgen::TestGenVerdict::TestErr(_)));

        // Gaps beat failures: fill stubs before debugging assertions.
        let v = testgen::classify(
            &ledger_state(vec!["t"], vec![], vec![], vec![]),
            Some(&summary(2, vec![])),
            "",
        );
        assert!(matches!(v, testgen::TestGenVerdict::Gap(_)));

        // Green + complete ledger = covered.
        let v = testgen::classify(
            &ledger_state(vec![], vec![], vec![], vec![]),
            Some(&summary(0, vec![])),
            "",
        );
        assert!(matches!(v, testgen::TestGenVerdict::Covered));

        // Complete ledger + red suite = app bug.
        let v = testgen::classify(
            &ledger_state(vec![], vec![], vec![], vec![]),
            Some(&summary(2, vec![])),
            "",
        );
        assert!(matches!(v, testgen::TestGenVerdict::AppBug(_)));

        // No parseable report = test-side error, with the raw tail surfaced.
        let v = testgen::classify(
            &ledger_state(vec![], vec![], vec![], vec![]),
            None,
            "npm ERR! could not determine executable",
        );
        match v {
            testgen::TestGenVerdict::TestErr(d) => {
                assert!(d.summary.contains("npm ERR"), "got: {}", d.summary);
            }
            other => panic!("expected TestErr, got {other:?}"),
        }
    }

    #[test]
    fn verdict_serializes_with_snake_case_tag() {
        let v = testgen::TestGenVerdict::Gap(GapDetail {
            missing: vec!["a".into()],
            unfilled: vec![],
            skipped: vec![],
            no_assertion: vec![],
            failed: vec![],
        });
        let json = serde_json::to_value(&v).unwrap();
        assert_eq!(json["verdict"], "gap");
        assert_eq!(json["missing"][0], "a");
    }

    #[test]
    fn ledger_stub_json_round_trip() {
        let stub = LedgerStub {
            title: "POST /api/x valid".into(),
            endpoint: "POST /api/x".into(),
            category: "valid".into(),
            file: "backend/tests/app.test.js".into(),
        };
        let json = serde_json::to_string(&stub).unwrap();
        let back: LedgerStub = serde_json::from_str(&json).unwrap();
        assert_eq!(back, stub);
    }

    #[tokio::test]
    async fn verify_without_ledger_fails_cleanly() {
        let dir = tempfile::tempdir().unwrap();
        // Profile-matching workspace but NO scaffold run → no ledger yet.
        std::fs::create_dir_all(dir.path().join("backend/src")).unwrap();
        std::fs::create_dir_all(dir.path().join("frontend/src")).unwrap();
        std::fs::write(
            dir.path().join("backend/package.json"),
            r#"{"scripts":{"test":"vitest run"}}"#,
        )
        .unwrap();
        std::fs::write(dir.path().join("frontend/package.json"), "{}").unwrap();
        std::fs::write(dir.path().join("backend/vitest.config.js"), "{}").unwrap();
        std::fs::write(
            dir.path().join("backend/src/app.js"),
            "module.exports = {};",
        )
        .unwrap();

        let tool = TestgenTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({"action": "verify"}))
            .await
            .unwrap();
        assert!(!result.success);
        assert!(result.output.contains("run `testgen scaffold` first"));
    }
}
