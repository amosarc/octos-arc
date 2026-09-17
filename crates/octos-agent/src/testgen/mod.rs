//! Testgen: host-first test-case generation pipeline for coding tasks.
//!
//! The pipeline implements the "静态逻辑全下沉" principle for test writing:
//! everything that can be computed statically is computed here in Rust —
//! the interface contract (endpoints + request fields), the validation
//! checklist (evidence-backed rules), the test skeleton with its
//! category-named ledger stubs — and the model only spends tokens filling
//! stubs with case data and assertions.
//!
//! Orchestration entry points:
//! - [`scaffold`]: detect the stack profile, extract contracts for the
//!   target files, write skeleton files, and render the bounded context
//!   block the TestWriter receives.
//! - [`coverage`]: verify the ledger after the model's patch
//!   (ledger fill state + test run verdict).
//! - [`turn`]: the `[task-kind:test]` turn marker (works in every session
//!   mode, including solo/stdio where `spawn` is unavailable).
//!
//! The injected context is budgeted: [`MAX_INJECT_TOKENS`] caps the block,
//! trimmed by priority (endpoint contracts → validation rules →
//! focus hints) so a wide target set degrades instead of exploding the
//! prompt.
//!
//! Feature gating: only the tree-sitter-dependent extraction/generation
//! modules sit behind `ast`; `turn`/`profile`/`runner` are plain std code
//! and always available.

#[cfg(feature = "ast")]
pub mod contract;
#[cfg(feature = "ast")]
pub mod coverage;
pub mod profile;
pub mod runner;
#[cfg(feature = "ast")]
pub mod skeleton;
#[cfg(feature = "ast")]
pub mod tool;
pub mod turn;
#[cfg(feature = "ast")]
pub mod validation;

#[cfg(feature = "ast")]
use std::path::{Path, PathBuf};

#[cfg(feature = "ast")]
use eyre::{Result, bail};
use serde::Serialize;

#[cfg(feature = "ast")]
use crate::testgen::contract::EndpointContract;
#[cfg(feature = "ast")]
use crate::testgen::profile::StackProfile;
use crate::testgen::runner::{CaseFailure, VitestSummary};
#[cfg(feature = "ast")]
use crate::testgen::skeleton::{LedgerStub, SkeletonFile};
#[cfg(feature = "ast")]
use crate::testgen::validation::ValidationRule;

/// Upper bound for the injected context block. Sized so the contract block
/// stays an order of magnitude below what the model would burn re-reading
/// backend source to rediscover the same facts.
pub const MAX_INJECT_TOKENS: u32 = 2_000;

/// Caps on the injected block, applied in priority order when over budget.
#[cfg(feature = "ast")]
const MAX_ENDPOINTS_IN_PROMPT: usize = 24;
#[cfg(feature = "ast")]
const MAX_RULES_PER_ENDPOINT: usize = 6;

/// Machine-readable ledger artifact written by `scaffold` and read by
/// `verify`. Lives under the backend test root (vitest's include pattern
/// `tests/**/*.{test,spec}.js` never picks up a `.json` ledger).
pub const LEDGER_FILE: &str = "testgen-ledger.json";

/// Typed pipeline signal: the verdict `verify` returns. Each variant IS
/// the next instruction for the loop — the model never has to remember
/// coverage rules, the signal carries them.
#[derive(Debug, Serialize)]
#[serde(tag = "verdict", rename_all = "snake_case")]
pub enum TestGenVerdict {
    /// Ledger complete + suite green: the test node is done.
    Covered,
    /// Coverage gaps (or red cases among unfilled stubs): back to the
    /// TestWriter with the gap list as the supplement prompt.
    Gap(GapDetail),
    /// Assertions fail against green-looking expectations: evidence for the
    /// Fixer (implementer) — the APPLICATION is wrong, not the tests.
    AppBug(AppBugDetail),
    /// The suite itself fails to load (import/syntax/timeout/skipped run):
    /// test-side responsibility — the TestWriter fixes the tests.
    TestErr(TestErrDetail),
}

/// Coverage gap detail — every list is directly actionable.
#[derive(Debug, Serialize)]
pub struct GapDetail {
    pub missing: Vec<String>,
    pub unfilled: Vec<String>,
    pub skipped: Vec<String>,
    pub no_assertion: Vec<String>,
    /// Failing case titles (if any) — fix these too while filling stubs.
    pub failed: Vec<String>,
}

/// App-bug evidence: failing assertions with their first diagnostic lines.
#[derive(Debug, Serialize)]
pub struct AppBugDetail {
    pub summary: String,
    pub failures: Vec<CaseFailure>,
}

/// Test-side load failure evidence.
#[derive(Debug, Serialize)]
pub struct TestErrDetail {
    pub summary: String,
}

/// Classify a ledger state + run summary into the pipeline signal.
/// Precedence: load errors → coverage gaps → assertion failures → covered.
/// Gaps win over failures because filling a stub can flip a red suite, and
/// one attention target per round is the cheapest loop. `raw_tail` carries
/// the runner output when the report was unparseable.
#[cfg(feature = "ast")]
pub fn classify(
    state: &coverage::LedgerState,
    summary: Option<&VitestSummary>,
    raw_tail: &str,
) -> TestGenVerdict {
    let Some(s) = summary else {
        let detail = if raw_tail.trim().is_empty() {
            "runner produced no parseable report".to_string()
        } else {
            format!(
                "runner produced no parseable report: {}",
                first_line(raw_tail)
            )
        };
        return TestGenVerdict::TestErr(TestErrDetail { summary: detail });
    };
    if !s.load_errors.is_empty() {
        return TestGenVerdict::TestErr(TestErrDetail {
            summary: s.load_errors.join(" | "),
        });
    }
    if !state.complete() {
        let failed = s.failures.iter().map(|f| f.title.clone()).collect();
        return TestGenVerdict::Gap(GapDetail {
            missing: state.missing.clone(),
            unfilled: state.unfilled.clone(),
            skipped: state.skipped.clone(),
            no_assertion: state.no_assertion.clone(),
            failed,
        });
    }
    if !s.failures.is_empty() {
        return TestGenVerdict::AppBug(AppBugDetail {
            summary: format!(
                "{} of {} cases fail — tests are complete and green-side valid; fix the application",
                s.failures.len(),
                s.total
            ),
            failures: s.failures.clone(),
        });
    }
    TestGenVerdict::Covered
}

fn first_line(text: &str) -> String {
    text.lines()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("")
        .chars()
        .take(200)
        .collect()
}

/// Everything the scaffold step produced.
#[derive(Debug, Serialize)]
#[cfg(feature = "ast")]
pub struct ScaffoldOutput {
    pub profile_kind: String,
    /// Workspace-relative skeleton paths actually written to disk.
    pub written: Vec<String>,
    /// The ledger: every stub the TestWriter must fill, across all files.
    pub ledger: Vec<LedgerStub>,
    pub endpoints: Vec<EndpointContract>,
    pub rules: Vec<ValidationRule>,
    /// The bounded context block for the TestWriter turn.
    pub injected_prompt: String,
    /// Its estimated token cost (see `octos_llm::estimate_tokens`).
    pub injected_tokens: u32,
}

/// Run the scaffold step against a workspace.
///
/// `targets` are workspace-relative source files to scan; an empty list
/// defaults to the profile's API root plus direct `backend/src/*.js`
/// modules. Fails honestly when no built-in stack profile matches or when
/// no route-bearing file is found — writing tests against a guessed
/// contract is worse than refusing.
#[cfg(feature = "ast")]
pub fn scaffold(workspace: &Path, targets: &[String]) -> Result<ScaffoldOutput> {
    let profile = profile::detect(workspace)
        .ok_or_else(|| eyre::eyre!("no built-in testgen stack profile matches this workspace"))?;

    let mut scan_list: Vec<String> = Vec::new();
    for t in targets {
        let p = workspace.join(t);
        if p.is_dir() {
            collect_js_files(&p, &mut scan_list, t);
        } else {
            scan_list.push(normalize(t));
        }
    }
    if scan_list.is_empty() {
        let src = workspace.join("backend/src");
        if src.is_dir() {
            collect_js_files(&src, &mut scan_list, "backend/src");
        } else {
            scan_list.push(profile.api_root.to_string());
        }
    }
    if scan_list.is_empty() {
        bail!("no JavaScript source files found under the target set");
    }

    let mut endpoints = Vec::new();
    let mut rules = Vec::new();
    for rel in &scan_list {
        let abs = workspace.join(rel);
        let Ok(source) = std::fs::read_to_string(&abs) else {
            continue;
        };
        if source.len() > 1_048_576 {
            continue; // same 1 MB parse cap as the code_structure tool
        }
        let contracts = contract::extract_file(&source, rel)?;
        let file_rules = validation::scan_file(&source, rel)?;
        endpoints.extend(contracts.endpoints);
        rules.extend(file_rules);
    }
    if endpoints.is_empty() {
        bail!(
            "no Express route registrations found in {} scanned file(s)",
            scan_list.len()
        );
    }

    // Sort endpoints by file then line for stable output.
    endpoints.sort_by(|a, b| {
        a.file
            .cmp(&b.file)
            .then(a.handler_line.cmp(&b.handler_line))
    });

    let files = skeleton::generate(&profile, &endpoints, &rules);
    let ledger = skeleton::ledger(&files, &endpoints);

    // Write skeletons to disk. Skeletons are new files under the test root;
    // never overwrite an existing test file. The LEDGER is the one artifact
    // that IS overwritten — it tracks the current scaffold pass.
    let mut written = Vec::new();
    for f in &files {
        let abs = workspace.join(&f.path);
        if abs.exists() {
            bail!(
                "refusing to overwrite existing test file {}; remove it first",
                f.path
            );
        }
        if let Some(parent) = abs.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&abs, &f.content)?;
        written.push(f.path.clone());
    }

    let ledger_path = ledger_file(workspace, &profile);
    if let Some(parent) = ledger_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(
        &ledger_path,
        serde_json::to_string_pretty(&ledger).unwrap_or_else(|_| "[]".to_string()),
    )?;
    written.push(
        ledger_path
            .strip_prefix(workspace)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|_| LEDGER_FILE.to_string()),
    );

    let injected_prompt = render_injected(&profile, &endpoints, &rules, &files);
    let injected_tokens = octos_llm::context::estimate_tokens(&injected_prompt);

    Ok(ScaffoldOutput {
        profile_kind: format!("{:?}", profile.kind),
        written,
        ledger,
        endpoints,
        rules,
        injected_prompt,
        injected_tokens,
    })
}

/// Render the bounded context block for the TestWriter turn. Priority on
/// trim: endpoint contracts first (they define the interface), then rules,
/// then per-category focus hints which are already embedded in the
/// skeletons on disk.
#[cfg(feature = "ast")]
fn render_injected(
    profile: &StackProfile,
    endpoints: &[EndpointContract],
    rules: &[ValidationRule],
    files: &[SkeletonFile],
) -> String {
    let mut out = String::new();
    out.push_str("[testgen] Host-extracted test contract — do NOT re-read backend source to rediscover interfaces.\n");
    out.push_str(&format!(
        "Stack: {:?}. Supertest target: `{}` (default export = app).\n",
        profile.kind, profile.api_root
    ));
    out.push_str(&format!(
        "Endpoints ({} shown of {}):\n",
        endpoints.len().min(MAX_ENDPOINTS_IN_PROMPT),
        endpoints.len()
    ));
    for ep in endpoints.iter().take(MAX_ENDPOINTS_IN_PROMPT) {
        out.push_str(&format!("- {}\n", ep.key));
        let mut parts: Vec<String> = Vec::new();
        if !ep.path_params.is_empty() {
            parts.push(format!("path: {}", ep.path_params.join(", ")));
        }
        if !ep.body_fields.is_empty() {
            parts.push(format!("body: {}", render_fields(&ep.body_fields)));
        }
        if !ep.query_fields.is_empty() {
            parts.push(format!("query: {}", render_fields(&ep.query_fields)));
        }
        if !parts.is_empty() {
            out.push_str(&format!("  in: {}\n", parts.join("; ")));
        }
        let ep_rules: Vec<&ValidationRule> = rules
            .iter()
            .filter(|r| r.file == ep.file && r.kind != validation::RuleKind::ErrorBranch)
            .take(MAX_RULES_PER_ENDPOINT)
            .collect();
        for r in ep_rules {
            out.push_str(&format!(
                "  rule: {} @ {}:{} ({})\n",
                r.detail,
                file_stem(&r.file),
                r.line,
                skeleton::rule_label(&r.kind)
            ));
        }
    }
    out.push_str("Skeletons written (fill EVERY stub; one assertion min; no .skip; never touch app source):\n");
    for f in files {
        out.push_str(&format!("- {}\n", f.path));
    }
    out.push_str(&format!(
        "Verify with: cd backend && npx vitest run {} --reporter=json\n",
        files
            .iter()
            .map(|f| f.path.strip_prefix("backend/").unwrap_or(&f.path))
            .collect::<Vec<_>>()
            .join(" ")
    ));
    out
}

/// Workspace path of the ledger artifact for a detected profile.
#[cfg(feature = "ast")]
pub fn ledger_file(workspace: &Path, profile: &StackProfile) -> PathBuf {
    workspace.join(profile.backend_test_root).join(LEDGER_FILE)
}

#[cfg(feature = "ast")]
fn render_fields(fields: &[contract::ParamField]) -> String {
    fields
        .iter()
        .map(|f| {
            if f.optional {
                format!("{}(opt)", f.name)
            } else {
                f.name.clone()
            }
        })
        .collect::<Vec<_>>()
        .join(", ")
}

#[cfg(feature = "ast")]
fn file_stem(path: &str) -> String {
    Path::new(path)
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_string())
}

#[cfg(feature = "ast")]
fn normalize(path: &str) -> String {
    path.trim()
        .trim_start_matches("./")
        .replace('\\', "/")
        .to_string()
}

/// Collect `.js/.mjs/.cjs` files (depth-bounded) under a directory into
/// workspace-relative workspace-slash paths.
#[cfg(feature = "ast")]
fn collect_js_files(dir: &Path, out: &mut Vec<String>, rel_prefix: &str) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut paths: Vec<PathBuf> = entries.flatten().map(|e| e.path()).collect();
    paths.sort();
    for path in paths {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        if path.is_dir() {
            // One level of subdirectories (e.g. backend/src/routes) is
            // enough for route scanning; deeper trees are intentionally
            // out of the default scope.
            if rel_prefix.matches('/').count() < 3 {
                collect_js_files(&path, out, &format!("{rel_prefix}/{name}"));
            }
        } else if name.ends_with(".js") || name.ends_with(".mjs") || name.ends_with(".cjs") {
            out.push(format!("{rel_prefix}/{name}"));
        }
    }
}

#[cfg(all(test, feature = "ast"))]
mod tests {
    use super::*;
    use tempfile::TempDir;

    /// Minimal web-react-express-shaped workspace.
    fn express_workspace() -> TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("backend/src")).unwrap();
        std::fs::create_dir_all(dir.path().join("frontend/src")).unwrap();
        std::fs::write(
            dir.path().join("backend/package.json"),
            r#"{"name":"backend","scripts":{"test":"vitest run --config vitest.config.js","start":"node src/index.js"}}"#,
        )
        .unwrap();
        std::fs::write(dir.path().join("frontend/package.json"), "{}").unwrap();
        std::fs::write(
            dir.path().join("backend/vitest.config.js"),
            "module.exports = {};",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("backend/src/app.js"),
            r#"
const app = require('express')();
app.get('/api/health', (req, res) => res.json({ ok: true }));
app.post('/api/register', (req, res) => {
  const { username, password } = req.body;
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

    #[test]
    fn scaffold_end_to_end_on_express_workspace() {
        let dir = express_workspace();
        let out = scaffold(dir.path(), &[]).expect("scaffold succeeds on matching workspace");

        assert_eq!(out.profile_kind, "WebReactExpress");
        assert_eq!(out.endpoints.len(), 2);
        assert!(!out.rules.is_empty());
        assert_eq!(
            out.written,
            vec![
                "backend/tests/app.test.js".to_string(),
                "backend/tests/testgen-ledger.json".to_string(),
            ]
        );

        // Ledger: 4 categories × 2 endpoints.
        assert_eq!(out.ledger.len(), 8);

        // Skeleton landed on disk with ledger stubs.
        let on_disk =
            std::fs::read_to_string(dir.path().join("backend/tests/app.test.js")).unwrap();
        assert_eq!(on_disk.matches(skeleton::TODO_MARKER).count(), 8);

        // Injected block is bounded and carries the essentials.
        assert!(out.injected_tokens <= MAX_INJECT_TOKENS);
        assert!(out.injected_prompt.contains("POST /api/register"));
        assert!(out.injected_prompt.contains("username"));
        assert!(out.injected_prompt.contains("backend/tests/app.test.js"));
        assert!(out.injected_prompt.contains("--reporter=json"));
    }

    #[test]
    fn scaffold_refuses_unmatched_workspaces() {
        let dir = tempfile::tempdir().unwrap();
        let err = scaffold(dir.path(), &[]).unwrap_err();
        assert!(
            err.to_string()
                .contains("no built-in testgen stack profile"),
            "got: {err}"
        );
    }

    #[test]
    fn scaffold_refuses_to_overwrite_existing_tests() {
        let dir = express_workspace();
        std::fs::create_dir_all(dir.path().join("backend/tests")).unwrap();
        std::fs::write(dir.path().join("backend/tests/app.test.js"), "// existing").unwrap();
        let err = scaffold(dir.path(), &[]).unwrap_err();
        assert!(err.to_string().contains("refusing to overwrite"));
        // The existing file is untouched.
        assert_eq!(
            std::fs::read_to_string(dir.path().join("backend/tests/app.test.js")).unwrap(),
            "// existing"
        );
    }

    #[test]
    fn explicit_targets_restrict_the_scan() {
        let dir = express_workspace();
        // Only scan app.js explicitly (same file, but prove the plumbing).
        let out = scaffold(dir.path(), &["backend/src/app.js".to_string()]).unwrap();
        assert_eq!(out.endpoints.len(), 2);
    }
}
