//! Test skeleton generation — the "骨架即台账" (skeleton-as-ledger) design.
//!
//! The host writes the complete test file per scanned source module: real
//! imports, a `describe` shell per endpoint, and four category-named stub
//! cases (`valid`, `boundary`, `length`, `invalid`) that map 1:1 to the
//! four mandatory coverage categories of the pipeline:
//!
//! 1. `valid`    — legal inputs, happy path
//! 2. `boundary` — boundary criticals (min/max/zero/empty)
//! 3. `length`   — length extremes (over-long / under-short / out of range)
//! 4. `invalid`  — illegal inputs and validation error branches
//!
//! Each stub body starts with a `TODO(testgen)` marker. Coverage checking
//! later degrades from a semantic problem ("does this test cover that
//! rule?") to a mechanical one: every ledger stub filled, at least one
//! assertion per case, no skip markers, tests green. The stub *names* are
//! the ledger; the checker only has to parse them — which is what keeps
//! the whole guarantee static and token-free.

use serde::{Deserialize, Serialize};

use super::contract::EndpointContract;
use super::profile::StackProfile;
use super::validation::{RuleKind, ValidationRule};

/// TODO marker the coverage checker greps for. Never localize or reword:
/// it is a machine contract, not a comment.
pub const TODO_MARKER: &str = "TODO(testgen): fill this case";

/// The four mandatory coverage categories, in ledger order.
pub const CATEGORIES: [&str; 4] = ["valid", "boundary", "length", "invalid"];

/// One generated test file (workspace-relative path + full content).
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SkeletonFile {
    pub path: String,
    pub content: String,
}

/// One ledger entry: a stub the TestWriter must fill.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LedgerStub {
    /// Exact `it()` title the checker matches on.
    pub title: String,
    /// `"{METHOD} {path}"` of the owning endpoint.
    pub endpoint: String,
    /// One of `CATEGORIES`.
    pub category: String,
    /// Workspace-relative test file holding the stub.
    pub file: String,
}

/// Render skeletons for a batch of endpoints. Endpoints are grouped by
/// source file so one module gets one test file (`src/app.js` →
/// `tests/app.test.js`).
pub fn generate(
    profile: &StackProfile,
    endpoints: &[EndpointContract],
    rules: &[ValidationRule],
) -> Vec<SkeletonFile> {
    let mut files: Vec<SkeletonFile> = Vec::new();
    for (source_file, group) in group_by_file(endpoints) {
        let path = format!("{}/{}.test.js", profile.backend_test_root, source_file);
        let content = render_module(profile, &source_file, &group, rules);
        files.push(SkeletonFile { path, content });
    }
    files
}

/// Ledger for the generated skeletons, in file → endpoint → category order.
pub fn ledger(files: &[SkeletonFile], endpoints: &[EndpointContract]) -> Vec<LedgerStub> {
    let mut out = Vec::new();
    for (source_file, group) in group_by_file(endpoints) {
        let Some(file) = files
            .iter()
            .find(|f| f.path.ends_with(&format!("{source_file}.test.js")))
            .map(|f| f.path.clone())
        else {
            continue;
        };
        for ep in group {
            for category in CATEGORIES {
                out.push(LedgerStub {
                    title: stub_title(&ep.key, category),
                    endpoint: ep.key.clone(),
                    category: category.to_string(),
                    file: file.clone(),
                });
            }
        }
    }
    out
}

/// Group endpoints by their source file *name* (stem): `backend/src/app.js`
/// and `backend/src/routes/users.js` → `app`, `routes/users`.
fn group_by_file(endpoints: &[EndpointContract]) -> Vec<(String, Vec<&EndpointContract>)> {
    let mut order: Vec<String> = Vec::new();
    let mut groups: Vec<Vec<&EndpointContract>> = Vec::new();
    for ep in endpoints {
        let stem = source_stem(&ep.file);
        match order.iter().position(|s| s == &stem) {
            Some(i) => groups[i].push(ep),
            None => {
                order.push(stem);
                groups.push(vec![ep]);
            }
        }
    }
    order.into_iter().zip(groups).collect()
}

/// `backend/src/app.js` → `app`; `backend/src/routes/users.js` →
/// `routes/users`. The result is both the test filename fragment and a
/// stable grouping key.
fn source_stem(file: &str) -> String {
    let trimmed = file.trim().replace('\\', "/");
    let no_ext = trimmed.strip_suffix(".js").unwrap_or(&trimmed);
    no_ext
        .split_once("src/")
        .map(|(_, rest)| rest.trim_end_matches('/').to_string())
        .unwrap_or_else(|| {
            no_ext
                .rsplit('/')
                .next()
                .unwrap_or(no_ext)
                .to_string()
        })
}

fn stub_title(endpoint_key: &str, category: &str) -> String {
    format!("{endpoint_key} {category}")
}

fn render_module(
    profile: &StackProfile,
    stem: &str,
    endpoints: &[&EndpointContract],
    rules: &[ValidationRule],
) -> String {
    let mut out = String::new();
    out.push_str("// Generated by octos testgen (skeleton-as-ledger).\n");
    out.push_str("// Rules for the TestWriter, enforced mechanically by the host:\n");
    out.push_str("//   1. Fill EVERY stub below; delete each TODO(testgen) marker as you go.\n");
    out.push_str("//   2. At least one assertion per case; never use .skip / xit / todo modifiers.\n");
    out.push_str("//   3. Never modify application source to make a test pass — report the bug instead.\n");
    out.push_str("// Run: cd backend && npx vitest run\n\n");
    // ESM imports, even in CJS packages: vitest transpiles test files and
    // REFUSES `require('vitest')` outright ("Vitest cannot be imported in
    // a CommonJS module"). The CJS `app` module interoperates as the ESM
    // default export.
    out.push_str("import { describe, it, expect } from 'vitest';\n");
    out.push_str("import request from 'supertest';\n");
    // backend/tests/<stem>.test.js → backend/src/<stem>.js: up ONE level.
    out.push_str(&format!("import app from '../src/{stem}.js';\n\n"));

    for ep in endpoints {
        let ep_rules: Vec<&ValidationRule> = rules
            .iter()
            .filter(|r| r.file == ep.file)
            .collect();
        out.push_str(&format!("describe('{}', () => {{\n", ep.key));
        if !ep_rules.is_empty() {
            out.push_str("  // Host-extracted validation rules (minimum coverage set):\n");
            for r in ep_rules.iter().take(12) {
                out.push_str(&format!(
                    "  //   {} ({})\n",
                    r.detail,
                    rule_label(&r.kind)
                ));
            }
        }
        for category in CATEGORIES {
            let title = stub_title(&ep.key, category);
            let focus = category_focus(category, ep);
            out.push_str(&format!("  it('{title}', () => {{\n"));
            if let Some(focus) = focus {
                out.push_str(&format!("    // Focus: {focus}\n"));
            }
            out.push_str(&format!("    // {TODO_MARKER}\n"));
            out.push_str("  });\n");
        }
        out.push_str("});\n\n");
    }
    let _ = profile; // reserved: per-profile import flavor (ESM vs CJS)
    out
}

fn category_focus(category: &str, ep: &EndpointContract) -> Option<String> {
    let fields = ep.body_fields.iter().map(|f| f.name.clone()).collect::<Vec<_>>().join(", ");
    match category {
        "valid" => Some(format!(
            "legal inputs for [{fields}] → success response"
        )),
        "boundary" => Some("min/max/zero/empty critical values".to_string()),
        "length" => Some("over-long / under-short / out-of-range values".to_string()),
        "invalid" => Some(format!(
            "illegal values → validation error branch ({})",
            if ep.path_params.is_empty() {
                "status 400/422"
            } else {
                "status 400/422 or 404"
            }
        )),
        _ => None,
    }
}

/// Compact rule label used in skeleton comments and gap reports.
pub fn rule_label(kind: &RuleKind) -> &'static str {
    match kind {
        RuleKind::Required => "required",
        RuleKind::LengthMin(_) | RuleKind::LengthMax(_) => "length bound",
        RuleKind::TypeCheck => "type check",
        RuleKind::Format => "format regex",
        RuleKind::EnumMembership => "enum membership",
        RuleKind::ErrorBranch => "error branch",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testgen::contract::ParamField;

    fn profile() -> StackProfile {
        StackProfile::web_react_express()
    }

    fn ep(method: &str, path: &str, file: &str) -> EndpointContract {
        EndpointContract {
            key: format!("{method} {path}"),
            method: method.to_string(),
            path: path.to_string(),
            file: file.to_string(),
            handler_line: 1,
            path_params: vec![],
            body_fields: vec![ParamField {
                name: "username".to_string(),
                line: 1,
                optional: false,
            }],
            query_fields: vec![],
            handler_ref: None,
        }
    }

    #[test]
    fn four_category_ledger_stubs_per_endpoint() {
        let endpoints = vec![ep("POST", "/api/register", "backend/src/app.js")];
        let files = generate(&profile(), &endpoints, &[]);
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].path, "backend/tests/app.test.js");

        let ledger = ledger(&files, &endpoints);
        assert_eq!(ledger.len(), 4);
        assert_eq!(ledger[0].title, "POST /api/register valid");
        assert_eq!(ledger[3].title, "POST /api/register invalid");
        assert!(ledger.iter().all(|s| s.file == "backend/tests/app.test.js"));
        // Ledger categories are exactly the four mandatory ones, in order.
        assert_eq!(
            ledger.iter().map(|s| s.category.as_str()).collect::<Vec<_>>(),
            vec!["valid", "boundary", "length", "invalid"]
        );
    }

    #[test]
    fn stubs_carry_todo_marker_and_rules_comment() {
        let mut ep_with_rules = ep("POST", "/api/register", "backend/src/app.js");
        ep_with_rules.handler_line = 10;
        let rules = vec![ValidationRule {
            kind: RuleKind::Required,
            subject: "username".to_string(),
            file: "backend/src/app.js".to_string(),
            line: 11,
            detail: "if (!username)".to_string(),
        }];
        let files = generate(&profile(), &[ep_with_rules], &rules);
        let content = &files[0].content;
        assert_eq!(content.matches(TODO_MARKER).count(), 4);
        // vitest API via ESM import (require('vitest') is refused by vitest).
        assert!(content.contains("import { describe, it, expect } from 'vitest';"));
        assert!(content.contains("import request from 'supertest';"));
        assert!(content.contains("import app from '../src/app.js';"));
        assert!(content.contains("//   if (!username) (required)"));
        assert!(content.contains("describe('POST /api/register'"));
        // The mechanical red lines live in the file header.
        assert!(content.contains(".skip"));
        assert!(content.contains("Never modify application source"));
    }

    #[test]
    fn endpoints_grouped_per_source_file() {
        let endpoints = vec![
            ep("POST", "/api/register", "backend/src/app.js"),
            ep("GET", "/api/health", "backend/src/app.js"),
            ep("GET", "/api/users", "backend/src/routes/users.js"),
        ];
        let files = generate(&profile(), &endpoints, &[]);
        assert_eq!(files.len(), 2);
        assert!(files.iter().any(|f| f.path == "backend/tests/app.test.js"));
        assert!(
            files
                .iter()
                .any(|f| f.path == "backend/tests/routes/users.test.js")
        );
        let ledger = ledger(&files, &endpoints);
        assert_eq!(ledger.len(), 12, "4 stubs x 3 endpoints (2 in app + 1 in routes/users)");
    }

    #[test]
    fn stub_titles_are_unique_and_matchable() {
        let endpoints = vec![
            ep("POST", "/api/register", "backend/src/app.js"),
            ep("GET", "/api/register", "backend/src/app.js"),
        ];
        let files = generate(&profile(), &endpoints, &[]);
        let ledger = ledger(&files, &endpoints);
        let mut titles: Vec<&str> = ledger.iter().map(|s| s.title.as_str()).collect();
        let total = titles.len();
        titles.sort();
        titles.dedup();
        assert_eq!(titles.len(), total, "titles must be unique per endpoint+category");
    }
}
