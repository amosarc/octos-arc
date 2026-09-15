//! End-to-end testgen pipeline test against a REAL vitest run.
//!
//! Builds a web-react-express-shaped workspace, runs the host scaffold,
//! fills the ledger stubs, and drives `testgen verify` through all four
//! verdicts (covered / gap / app_bug / test_err) with real node execution.
//!
//! Ignored by default: requires `npx` with a cached vitest (network on
//! first use). Run explicitly: `cargo test -p octos-agent --test testgen_e2e -- --ignored --nocapture`

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use octos_agent::tools::Tool;
use octos_agent::testgen::{self, tool::TestgenTool};

/// Install vitest + supertest + express ONCE into a shared base backend
/// (the generated workspaces symlink its node_modules). This makes the
/// closed loop real: the suite loads `express`, drives `supertest`-shaped
/// skeletons, and runs under a workspace-local vitest — the exact shape of
/// a web-react-express workspace after `npm install`.
fn shared_node_modules() -> PathBuf {
    static BASE: OnceLock<PathBuf> = OnceLock::new();
    let base = BASE.get_or_init(|| {
        let backend = std::env::temp_dir().join(format!(
            "octos-testgen-e2e-deps-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&backend);
        std::fs::create_dir_all(&backend).expect("create deps dir");
        let status = std::process::Command::new("npm")
            .args([
                "install",
                "--no-save",
                "--no-audit",
                "--no-fund",
                "--loglevel=error",
                "vitest@^4.0.0",
                "supertest@^7.1.4",
                "express@^5.2.1",
            ])
            .current_dir(&backend)
            .status()
            .expect("npm must be available for this test");
        assert!(status.success(), "npm install failed in {backend:?}");
        backend
    });
    base.join("node_modules")
}

/// Link the shared node_modules into the fixture workspace.
fn link_node_modules(workspace: &Path) {
    let target = workspace.join("backend/node_modules");
    #[cfg(unix)]
    std::os::unix::fs::symlink(shared_node_modules(), &target).expect("symlink node_modules");
    #[cfg(not(unix))]
    std::fs::create_dir_all(&target).expect("node_modules placeholder");
}

/// Minimal web-react-express-shaped workspace: two endpoints, one with a
/// validation branch rich enough for the checklist.
fn express_workspace() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("backend/src")).unwrap();
    std::fs::create_dir_all(dir.path().join("frontend/src")).unwrap();
    link_node_modules(dir.path());
    std::fs::write(
        dir.path().join("backend/package.json"),
        r#"{
  "name": "backend",
  "scripts": {
    "test": "vitest run --config vitest.config.js",
    "start": "node src/index.js"
  },
  "devDependencies": { "vitest": "^4.0.0", "supertest": "^7.1.4" }
}"#,
    )
    .unwrap();
    std::fs::write(dir.path().join("frontend/package.json"), r#"{"name":"frontend"}"#).unwrap();
    std::fs::write(
        dir.path().join("backend/vitest.config.js"),
        r#"const { defineConfig } = require('vitest/config');
module.exports = defineConfig({
  test: { environment: 'node', include: ['tests/**/*.{test,spec}.{js,jsx,ts,tsx}'] },
});
"#,
    )
    .unwrap();
    std::fs::write(
        dir.path().join("backend/src/app.js"),
        r#"
const app = require('express')();

app.get('/api/health', (req, res) => {
  res.json({ ok: true });
});

app.post('/api/register', (req, res) => {
  const { username } = req.body;
  if (!username) return res.status(400).json({});
  if (username.length < 3) return res.status(400).json({});
  if (username.length > 32) return res.status(400).json({});
  if (!/^[a-z0-9-]+$/.test(username)) return res.status(400).json({});
  res.json({ ok: true });
});

module.exports = app;
"#,
    )
    .unwrap();
    dir
}

/// Replace every `// TODO(testgen)` stub body line with a passing or
/// failing assertion (index-selectable).
fn fill_stubs(workspace: &Path, make_assertion: impl Fn(usize) -> String) {
    let test_file = workspace.join("backend/tests/app.test.js");
    let content = std::fs::read_to_string(&test_file).unwrap();
    let mut idx = 0usize;
    let filled = content
        .lines()
        .map(|line| {
            if line.trim() == "// TODO(testgen): fill this case" {
                let assertion = make_assertion(idx);
                idx += 1;
                format!("  {assertion}")
            } else {
                line.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&test_file, &filled).unwrap();
    assert!(idx >= 8, "expected at least 8 stubs, replaced {idx}");
}

async fn run_verify(workspace: &Path) -> serde_json::Value {
    let tool = TestgenTool::new(workspace);
    let result = tool
        .execute(&serde_json::json!({"action": "verify"}))
        .await
        .expect("tool executes");
    serde_json::from_str(&result.output).expect("verdict is valid JSON")
}

#[tokio::test]
#[ignore = "requires node/npx with a cached vitest"]
async fn scaffold_fill_verify_covered_end_to_end() {
    let dir = express_workspace();

    // 1. Host scaffold: contract + checklist + skeleton + ledger.
    let out = testgen::scaffold(dir.path(), &[]).expect("scaffold succeeds");
    assert_eq!(out.endpoints.len(), 2);
    assert_eq!(out.ledger.len(), 8);
    assert!(out.injected_tokens <= testgen::MAX_INJECT_TOKENS);
    assert!(dir.path().join("backend/tests/testgen-ledger.json").is_file());
    // The injected contract carries the validation rules so the model never
    // re-reads app source.
    assert!(out.injected_prompt.contains("length bound"));
    assert!(out.injected_prompt.contains("format regex"));

    // 2. TestWriter fills every stub with a green assertion.
    fill_stubs(dir.path(), |_| "expect(1 + 1).toBe(2);".to_string());

    // 3. Verify → covered.
    let verdict = run_verify(dir.path()).await;
    assert_eq!(verdict["result"]["verdict"], "covered", "got: {verdict}");
}

#[tokio::test]
#[ignore = "requires node/npx with a cached vitest"]
async fn verify_reports_gap_when_a_stub_stays_unfilled() {
    let dir = express_workspace();
    testgen::scaffold(dir.path(), &[]).unwrap();

    // Fill all but the LAST stub.
    fill_stubs(dir.path(), |i| {
        if i == 7 {
            "// TODO(testgen): fill this case".to_string()
        } else {
            "expect(1 + 1).toBe(2);".to_string()
        }
    });

    let verdict = run_verify(dir.path()).await;
    assert_eq!(verdict["result"]["verdict"], "gap", "got: {verdict}");
    let unfilled = verdict["result"]["unfilled"].as_array().unwrap();
    assert_eq!(unfilled.len(), 1, "exactly one stub left: {verdict}");
}

#[tokio::test]
#[ignore = "requires node/npx with a cached vitest"]
async fn complete_ledger_with_red_suite_is_app_bug() {
    let dir = express_workspace();
    testgen::scaffold(dir.path(), &[]).unwrap();

    // Ledger complete, but one assertion is wrong → the APPLICATION layer
    // owns this failure, not the test writer.
    fill_stubs(dir.path(), |i| {
        if i == 0 {
            "expect(1 + 1).toBe(3);".to_string()
        } else {
            "expect(1 + 1).toBe(2);".to_string()
        }
    });

    let verdict = run_verify(dir.path()).await;
    assert_eq!(verdict["result"]["verdict"], "app_bug", "got: {verdict}");
    let failures = verdict["result"]["failures"].as_array().unwrap();
    assert!(!failures.is_empty(), "failure evidence present: {verdict}");
    assert!(
        failures[0]["message"].as_str().unwrap_or("").contains("expected"),
        "failure carries the assertion evidence"
    );
}

#[tokio::test]
#[ignore = "requires node/npx with a cached vitest"]
async fn broken_test_file_is_test_err_not_app_bug() {
    let dir = express_workspace();
    testgen::scaffold(dir.path(), &[]).unwrap();
    fill_stubs(dir.path(), |_| "expect(1 + 1).toBe(2);".to_string());

    // Break the test file's syntax — a TEST-side defect that must classify
    // as test_err (test writer's job), never app_bug.
    let test_file = dir.path().join("backend/tests/app.test.js");
    let content = std::fs::read_to_string(&test_file).unwrap();
    std::fs::write(&test_file, format!("{content}\nconst broken = {{{{;")).unwrap();

    let verdict = run_verify(dir.path()).await;
    assert_eq!(verdict["result"]["verdict"], "test_err", "got: {verdict}");
}

#[tokio::test]
#[ignore = "requires node/npx with a cached vitest"]
async fn scaffold_verify_round_trip_through_the_tool_surface() {
    // The model-facing path: tool execute(scaffold) → fill → tool
    // execute(verify), asserting the JSON envelope the model sees.
    let dir = express_workspace();
    let tool = TestgenTool::new(dir.path());

    let scaffold_result = tool
        .execute(&serde_json::json!({"action": "scaffold"}))
        .await
        .unwrap();
    assert!(scaffold_result.success, "{}", scaffold_result.output);
    let scaffold_json: serde_json::Value = serde_json::from_str(&scaffold_result.output).unwrap();
    assert_eq!(scaffold_json["ledger_size"], 8);
    assert!(scaffold_json["injected_prompt"]
        .as_str()
        .unwrap()
        .contains("POST /api/register"));

    fill_stubs(dir.path(), |_| "expect(2).toBe(2);".to_string());

    let verify_result = tool
        .execute(&serde_json::json!({"action": "verify"}))
        .await
        .unwrap();
    assert!(verify_result.success, "{}", verify_result.output);
    let verify_json: serde_json::Value = serde_json::from_str(&verify_result.output).unwrap();
    assert_eq!(verify_json["result"]["verdict"], "covered");
}
