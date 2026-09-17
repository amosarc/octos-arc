//! Built-in stack profiles for the testgen pipeline.
//!
//! A profile encodes the test contract of a known workspace layout: where
//! tests must live, which runner executes them, and which scaffold pieces
//! the generated tests may import. The initial profile targets the
//! `web-react-express` shape (React/Vite frontend, Express/SQLite backend,
//! Vitest + supertest unit/API tests, Playwright E2E).
//!
//! Detection is marker-based on purpose: generated workspaces never carry a
//! machine-readable manifest (template metadata is excluded from the copied
//! tree), so the profile is inferred from on-disk markers only. When no
//! profile matches, callers must refuse the task instead of guessing a
//! contract — a wrong test contract silently produces tests the runner
//! will never pick up.

use std::path::Path;

use eyre::Result;

/// Which built-in stack a workspace matched.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StackKind {
    /// React (Vite) frontend + Express (SQLite) backend; Vitest + supertest
    /// for unit/API tests; Playwright E2E under `backend/test-e2e/`.
    WebReactExpress,
}

/// Unit-test runner flavor used to build host verify commands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TestRunner {
    Vitest,
}

/// The test contract of a detected workspace. All paths are
/// workspace-relative with forward slashes.
#[derive(Debug, Clone)]
pub struct StackProfile {
    pub kind: StackKind,
    /// Express app entry whose export is the supertest target.
    pub api_root: &'static str,
    /// Backend unit/integration test root (`backend/tests`).
    pub backend_test_root: &'static str,
    /// Frontend unit test root (`frontend/tests`).
    pub frontend_test_root: &'static str,
    /// Playwright E2E root. Acceptance-owned: NOT part of the TestWriter
    /// coverage ledger (scenario mapping there is semantic, and the
    /// acceptance layer already owns it).
    pub e2e_test_root: &'static str,
    pub unit_runner: TestRunner,
}

/// One concrete command the host runs to execute tests.
#[derive(Debug, Clone)]
pub struct TestCommand {
    /// Workspace-relative directory the command runs in.
    pub cwd: &'static str,
    /// Full argv tail executed via `npx` (or the runner binary directly).
    pub args: String,
    /// Reporter flag the host's output parser understands.
    pub reporter: Reporter,
}

/// Machine-parseable reporter the host expects from the runner.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reporter {
    /// `vitest run --reporter=json` — single JSON document on stdout.
    VitestJson,
}

impl StackProfile {
    pub fn web_react_express() -> Self {
        Self {
            kind: StackKind::WebReactExpress,
            api_root: "backend/src/app.js",
            backend_test_root: "backend/tests",
            frontend_test_root: "frontend/tests",
            e2e_test_root: "backend/test-e2e",
            unit_runner: TestRunner::Vitest,
        }
    }

    /// Classify a workspace-relative test path into its test area.
    /// Returns `None` for paths outside every test root — the write
    /// confinement for test-writing sessions denies those. Prefix matches
    /// are slash-bounded so `backend/tests-extra/` does NOT classify as a
    /// test root.
    pub fn test_area(&self, ws_rel_path: &str) -> Option<TestArea> {
        let p = normalize_rel(ws_rel_path);
        let under = |root: &str| p == root || p.starts_with(&format!("{root}/"));
        if under(self.backend_test_root) {
            Some(TestArea::BackendUnit)
        } else if under(self.frontend_test_root) {
            Some(TestArea::FrontendUnit)
        } else if under(self.e2e_test_root) {
            Some(TestArea::E2E)
        } else {
            None
        }
    }

    /// Build the host verify command(s) for workspace-relative test files.
    /// Backend and frontend files run in separate vitest invocations
    /// because their configs live in different directories; the caller
    /// aggregates exit codes.
    pub fn vitest_commands(&self, ws_rel_files: &[String]) -> Vec<TestCommand> {
        let mut backend = Vec::new();
        let mut frontend = Vec::new();
        for f in ws_rel_files {
            let p = normalize_rel(f);
            if p.starts_with(self.backend_test_root) {
                backend.push(strip_prefix(&p, "backend/"));
            } else if p.starts_with(self.frontend_test_root) {
                frontend.push(strip_prefix(&p, "frontend/"));
            }
        }
        let mut out = Vec::new();
        if !backend.is_empty() {
            out.push(TestCommand {
                cwd: "backend",
                args: format!("npx vitest run {} --reporter=json", backend.join(" ")),
                reporter: Reporter::VitestJson,
            });
        }
        if !frontend.is_empty() {
            out.push(TestCommand {
                cwd: "frontend",
                args: format!("npx vitest run {} --reporter=json", frontend.join(" ")),
                reporter: Reporter::VitestJson,
            });
        }
        out
    }
}

/// Which test root a path belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TestArea {
    BackendUnit,
    FrontendUnit,
    E2E,
}

/// Detect a stack profile from on-disk markers. `None` means no built-in
/// profile matches this workspace.
pub fn detect(workspace: &Path) -> Option<StackProfile> {
    let app_js = workspace.join("backend/src/app.js");
    let vitest_cfg = workspace.join("backend/vitest.config.js");
    let backend_pkg = workspace.join("backend/package.json");
    let frontend_pkg = workspace.join("frontend/package.json");
    if !(app_js.is_file()
        && vitest_cfg.is_file()
        && backend_pkg.is_file()
        && frontend_pkg.is_file())
    {
        return None;
    }
    // The backend `test` script must be vitest-driven; otherwise the
    // profile's runner contract would be wrong.
    let pkg = std::fs::read_to_string(&backend_pkg).ok()?;
    let scripts_start = pkg.find("\"scripts\"")?;
    let scripts = pkg.get(scripts_start..)?;
    if scripts.contains("vitest") {
        Some(StackProfile::web_react_express())
    } else {
        None
    }
}

fn normalize_rel(path: &str) -> String {
    path.trim()
        .trim_start_matches("./")
        .replace('\\', "/")
        .to_string()
}

fn strip_prefix(path: &str, prefix: &str) -> String {
    path.strip_prefix(prefix).unwrap_or(path).to_string()
}

/// Parse a package.json just enough to learn whether a named script exists
/// and what it shells out to. Avoids a full JSON dependency in the hot path
/// while staying honest about malformed manifests (returns `None`).
pub fn package_script(workspace: &Path, dir: &str, script: &str) -> Result<Option<String>> {
    let raw = match std::fs::read_to_string(workspace.join(dir).join("package.json")) {
        Ok(raw) => raw,
        Err(_) => return Ok(None),
    };
    let value: serde_json::Value = serde_json::from_str(&raw)?;
    Ok(value
        .get("scripts")
        .and_then(|s| s.get(script))
        .and_then(|v| v.as_str())
        .map(String::from))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn web_react_express_profile_constants_match_the_template_contract() {
        let p = StackProfile::web_react_express();
        assert_eq!(p.api_root, "backend/src/app.js");
        assert_eq!(p.backend_test_root, "backend/tests");
        assert_eq!(p.frontend_test_root, "frontend/tests");
        assert_eq!(p.e2e_test_root, "backend/test-e2e");
        assert_eq!(p.unit_runner, TestRunner::Vitest);
    }

    #[test]
    fn test_area_classification_is_root_prefix_based() {
        let p = StackProfile::web_react_express();
        assert_eq!(
            p.test_area("backend/tests/register.test.js"),
            Some(TestArea::BackendUnit)
        );
        assert_eq!(
            p.test_area("./frontend/tests/App.test.jsx"),
            Some(TestArea::FrontendUnit)
        );
        assert_eq!(
            p.test_area("backend/test-e2e/req-1.spec.js"),
            Some(TestArea::E2E)
        );
        // Outside every test root — write confinement must deny.
        assert_eq!(p.test_area("backend/src/app.js"), None);
        assert_eq!(p.test_area("frontend/src/App.tsx"), None);
        // Lookalike prefixes must not pass.
        assert_eq!(p.test_area("backend/tests-extra/x.js"), None);
    }

    #[test]
    fn vitest_commands_split_by_area_and_strip_workspace_dirs() {
        let p = StackProfile::web_react_express();
        let cmds = p.vitest_commands(&[
            "backend/tests/a.test.js".to_string(),
            "frontend/tests/b.test.jsx".to_string(),
            "backend/src/app.js".to_string(), // not a test path: ignored
        ]);
        assert_eq!(cmds.len(), 2);
        assert_eq!(cmds[0].cwd, "backend");
        assert_eq!(
            cmds[0].args,
            "npx vitest run tests/a.test.js --reporter=json"
        );
        assert_eq!(cmds[1].cwd, "frontend");
        assert_eq!(
            cmds[1].args,
            "npx vitest run tests/b.test.jsx --reporter=json"
        );
    }

    #[test]
    fn detect_requires_all_markers_and_a_vitest_test_script() {
        let dir = tempfile::tempdir().unwrap();
        assert!(
            detect(dir.path()).is_none(),
            "empty workspace matches nothing"
        );

        for rel in [
            "backend/src/app.js",
            "backend/vitest.config.js",
            "backend/package.json",
            "frontend/package.json",
        ] {
            let path = dir.path().join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, "{}").unwrap();
        }
        // Marker present but the test script is not vitest — no match.
        std::fs::write(
            dir.path().join("backend/package.json"),
            r#"{"scripts":{"test":"jest"}}"#,
        )
        .unwrap();
        assert!(detect(dir.path()).is_none());

        std::fs::write(
            dir.path().join("backend/package.json"),
            r#"{"scripts":{"test":"vitest run --config vitest.config.js"}}"#,
        )
        .unwrap();
        let profile = detect(dir.path()).expect("markers + vitest script should match");
        assert_eq!(profile.kind, StackKind::WebReactExpress);
    }

    #[test]
    fn package_script_reads_scripts_or_none() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("backend")).unwrap();
        std::fs::write(
            dir.path().join("backend/package.json"),
            r#"{"scripts":{"db:prepare:e2e":"node src/database/prepare_e2e.js"}}"#,
        )
        .unwrap();
        let script = package_script(dir.path(), "backend", "db:prepare:e2e").unwrap();
        assert_eq!(script.as_deref(), Some("node src/database/prepare_e2e.js"));
        assert_eq!(
            package_script(dir.path(), "backend", "missing").unwrap(),
            None
        );
        assert_eq!(package_script(dir.path(), "nope", "x").unwrap(), None);
    }
}
