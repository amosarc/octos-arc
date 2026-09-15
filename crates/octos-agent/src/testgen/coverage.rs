//! Ledger verification — the mechanical half of coverage checking.
//!
//! "骨架即台账": the host-generated skeleton pre-declares every case the
//! TestWriter must fill, with stable `it()` titles. This module re-parses
//! the written test files and compares them against that ledger:
//!
//! - `missing`       — a ledger title with no matching `it()` at all
//! - `unfilled`      — the case exists but its `TODO(testgen)` marker stays
//! - `skipped`       — the case is `.skip`-ed / `xit`-ed / `it.todo`
//! - `no_assertion`  — the case body has zero `expect`/`assert` calls
//!
//! Every check here is a syntax-level fact — no semantics, no model
//! judgment. Cases the model ADDED beyond the ledger are welcome and never
//! penalized; the ledger is a floor, not a ceiling.

use std::collections::HashSet;
use std::path::Path;

use eyre::Result;
use serde::Serialize;
use tree_sitter::Node;

use super::contract::{node_text, parse_js};
use super::skeleton::{LedgerStub, TODO_MARKER};

/// Mechanical ledger state for one workspace.
#[derive(Debug, Default, Serialize)]
pub struct LedgerState {
    /// Ledger titles with no matching `it()` in the test files.
    pub missing: Vec<String>,
    /// Matching `it()`s that still carry the `TODO(testgen)` marker.
    pub unfilled: Vec<String>,
    /// Matching `it()`s registered as skipped (`.skip` / `xit` / `it.todo`).
    pub skipped: Vec<String>,
    /// Matching `it()`s whose body contains no `expect`/`assert` call.
    pub no_assertion: Vec<String>,
    /// `it()` titles present in the files but not required by the ledger
    /// (extras are allowed; tracked for observability).
    pub extra: Vec<String>,
}

impl LedgerState {
    /// `true` when every ledger stub is filled, present, skipped-free and
    /// carries at least one assertion.
    pub fn complete(&self) -> bool {
        self.missing.is_empty()
            && self.unfilled.is_empty()
            && self.skipped.is_empty()
            && self.no_assertion.is_empty()
    }
}

/// One parsed `it()`/`test()` case found in a test file.
struct FoundCase {
    title: String,
    skipped: bool,
    has_assertion: bool,
    has_todo: bool,
}

/// Check the written test files against the ledger. Files that don't exist
/// yet count every one of their ledger stubs as missing.
pub fn check_ledger(workspace: &Path, ledger: &[LedgerStub]) -> Result<LedgerState> {
    let mut state = LedgerState::default();

    let mut file_paths: Vec<&String> = ledger
        .iter()
        .map(|s| &s.file)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    file_paths.sort();

    let mut found_by_file: std::collections::HashMap<String, Vec<FoundCase>> =
        std::collections::HashMap::new();
    for rel in &file_paths {
        let abs = workspace.join(rel);
        let source = match std::fs::read_to_string(&abs) {
            Ok(s) => s,
            Err(_) => continue, // file absent: its stubs stay "missing"
        };
        let cases = extract_cases(&source)?;
        found_by_file.insert((*rel).clone(), cases);
    }

    let mut matched: HashSet<String> = HashSet::new();
    for stub in ledger {
        let Some(cases) = found_by_file.get(&stub.file) else {
            state.missing.push(stub.title.clone());
            continue;
        };
        let Some(c) = cases.iter().find(|c| c.title == stub.title) else {
            state.missing.push(stub.title.clone());
            continue;
        };
        matched.insert(format!("{}::{}", stub.file, stub.title));
        if c.skipped {
            state.skipped.push(stub.title.clone());
        }
        if c.has_todo {
            state.unfilled.push(stub.title.clone());
        }
        // An unfilled (TODO) or skipped case trivially lacks assertions —
        // report no_assertion only for otherwise-filled cases to keep one
        // attention target per stub.
        if !c.has_assertion && !c.has_todo && !c.skipped {
            state.no_assertion.push(stub.title.clone());
        }
    }

    // Extras: cases in the files that no ledger stub requires.
    for (file, cases) in &found_by_file {
        for c in cases {
            if !matched.contains(&format!("{file}::{}", c.title)) {
                state.extra.push(c.title.clone());
            }
        }
    }
    Ok(state)
}

/// Extract every `it(...)` / `test(...)` case from a test file.
fn extract_cases(source: &str) -> Result<Vec<FoundCase>> {
    let tree = parse_js(source)?;
    let bytes = source.as_bytes();
    let mut out = Vec::new();
    walk_cases(tree.root_node(), bytes, &mut out);
    Ok(out)
}

fn walk_cases(node: Node, source: &[u8], out: &mut Vec<FoundCase>) {
    if node.kind() == "call_expression" {
        if let Some(c) = try_case(node, source) {
            out.push(c);
            // Do not recurse into the case body — nested its would be
            // counted twice (and vitest doesn't allow them anyway).
            return;
        }
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk_cases(child, source, out);
    }
}

fn try_case(call: Node, source: &[u8]) -> Option<FoundCase> {
    let fn_node = call.child_by_field_name("function")?;
    let (is_case, skipped) = match fn_node.kind() {
        "identifier" => {
            let name = node_text(&fn_node, source)?;
            (
                matches!(name.as_str(), "it" | "test" | "xit" | "xtest"),
                matches!(name.as_str(), "xit" | "xtest"),
            )
        }
        "member_expression" => {
            let prop = node_text(&fn_node.child_by_field_name("property")?, source)?;
            let object = fn_node.child_by_field_name("object")?;
            let object_name = node_text(&object, source).unwrap_or_default();
            let base = object_name == "it" || object_name == "test";
            (
                base && matches!(prop.as_str(), "skip" | "todo" | "only"),
                base && matches!(prop.as_str(), "skip" | "todo"),
            )
        }
        _ => return None,
    };
    if !is_case {
        return None;
    }

    let args = call.child_by_field_name("arguments")?;
    let mut cursor = args.walk();
    let first = args.children(&mut cursor).find(|c| c.is_named())?;
    let title = case_title(&first, source)?;

    let body = call
        .child_by_field_name("arguments")
        .and_then(|a| {
            let mut c = a.walk();
            a.children(&mut c)
                .filter(|x| x.is_named())
                .nth(1)
        })
        .map(|callback| callback_body(callback));

    let body_text = body
        .and_then(|b| b.utf8_text(source).ok().map(String::from))
        .unwrap_or_default();
    let has_assertion = body_text.contains("expect(")
        || body_text.contains("expect.")
        || body_text.contains("assert.");

    Some(FoundCase {
        title,
        skipped,
        has_assertion,
        has_todo: body_text.contains(TODO_MARKER),
    })
}

/// Title from a string literal (or a simple template string) first argument.
fn case_title(node: &Node, source: &[u8]) -> Option<String> {
    match node.kind() {
        "string" => {
            let mut cursor = node.walk();
            node.children(&mut cursor)
                .find(|c| c.kind() == "string_fragment")
                .and_then(|c| c.utf8_text(source).ok().map(String::from))
        }
        _ => None,
    }
}

/// The case callback's body: arrow/function expression → statement block.
fn callback_body(callback: Node) -> Node {
    match callback.kind() {
        "arrow_function" | "function_expression" | "function" => callback
            .child_by_field_name("body")
            .unwrap_or(callback),
        "identifier" => callback, // it('t', namedHelper): body unknown → text = name
        _ => callback,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stub(title: &str, file: &str) -> LedgerStub {
        LedgerStub {
            title: title.to_string(),
            endpoint: "POST /api/x".to_string(),
            category: "valid".to_string(),
            file: file.to_string(),
        }
    }

    #[test]
    fn all_green_ledger_passes() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("backend/tests")).unwrap();
        std::fs::write(
            dir.path().join("backend/tests/app.test.js"),
            r#"
it('POST /api/x valid', () => {
  expect(1).toBe(1);
});
it('POST /api/x boundary', () => {
  const res = await request(app).post('/api/x');
  expect(res.status).toBe(400);
});
"#,
        )
        .unwrap();
        let ledger = vec![
            stub("POST /api/x valid", "backend/tests/app.test.js"),
            stub("POST /api/x boundary", "backend/tests/app.test.js"),
        ];
        let state = check_ledger(dir.path(), &ledger).unwrap();
        assert!(state.complete(), "state: {state:?}");
        assert!(state.extra.is_empty());
    }

    #[test]
    fn missing_unfilled_skipped_and_assertionless_are_caught() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("backend/tests")).unwrap();
        std::fs::write(
            dir.path().join("backend/tests/app.test.js"),
            r#"
it('POST /api/x valid', () => {
  // TODO(testgen): fill this case
});
it.skip('POST /api/x boundary', () => {
  expect(1).toBe(1);
});
it('POST /api/x length', () => {});
it('POST /api/x extra bonus', () => {
  expect(true).toBe(true);
});
"#,
        )
        .unwrap();
        let ledger = vec![
            stub("POST /api/x valid", "backend/tests/app.test.js"),
            stub("POST /api/x boundary", "backend/tests/app.test.js"),
            stub("POST /api/x length", "backend/tests/app.test.js"),
            stub("POST /api/x invalid", "backend/tests/app.test.js"),
        ];
        let state = check_ledger(dir.path(), &ledger).unwrap();
        assert_eq!(state.unfilled, vec!["POST /api/x valid"]);
        assert_eq!(state.skipped, vec!["POST /api/x boundary"]);
        assert_eq!(state.no_assertion, vec!["POST /api/x length"]);
        assert_eq!(state.missing, vec!["POST /api/x invalid"]);
        assert_eq!(state.extra, vec!["POST /api/x extra bonus"]);
        assert!(!state.complete());
    }

    #[test]
    fn absent_file_counts_all_stubs_missing() {
        let dir = tempfile::tempdir().unwrap();
        let ledger = vec![stub("POST /api/x valid", "backend/tests/app.test.js")];
        let state = check_ledger(dir.path(), &ledger).unwrap();
        assert_eq!(state.missing, vec!["POST /api/x valid"]);
        assert!(!state.complete());
    }

    #[test]
    fn xit_and_todo_modifiers_are_skipped_cases() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("backend/tests")).unwrap();
        std::fs::write(
            dir.path().join("backend/tests/app.test.js"),
            r#"
xit('POST /api/x valid', () => { expect(1).toBe(1); });
it.todo('POST /api/x boundary');
it.only('POST /api/x length', () => { expect(1).toBe(1); });
"#,
        )
        .unwrap();
        let ledger = vec![
            stub("POST /api/x valid", "backend/tests/app.test.js"),
            stub("POST /api/x boundary", "backend/tests/app.test.js"),
            stub("POST /api/x length", "backend/tests/app.test.js"),
        ];
        let state = check_ledger(dir.path(), &ledger).unwrap();
        assert_eq!(state.skipped.len(), 2, "xit + it.todo are skips: {state:?}");
        // `.only` runs — it is not a skip and it asserts.
        assert!(!state.skipped.contains(&"POST /api/x length".to_string()));
    }
}
