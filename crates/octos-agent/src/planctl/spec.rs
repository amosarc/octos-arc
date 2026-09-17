//! Acceptance-spec signal inventory + per-test action summaries (ast).
//!
//! The D-flip: the model never reads the Playwright specs to learn what
//! the UI must contain. This module walks every staged spec with the
//! tree-sitter TS grammar and compiles:
//!
//! - **Signals** — getByTestId/getByRole (incl. regex `name:` patterns) /
//!   getByLabel/getByPlaceholder/getByText/getByTitle, `page.goto`
//!   targets, `locator('a[href="…"]')` selectors, bare `/api/…` route
//!   literals, `127.0.0.1:PORT`/`localhost:PORT` URLs, `[data-testid]`
//!   string fallbacks, `test()` titles, and select-alias tables.
//! - **Action summaries** — each `test()` body's
//!   goto→fill→check/uncheck→click→select→expect chain compressed to one
//!   ≤120-char line, so the model learns the motion order without
//!   opening the file.
//!
//! Honesty rules: signals are `(kind, value)` deduped globally keeping
//! the FIRST occurrence; support-helper signals fold into globals with
//! the enclosing function name as note; parse failures skip the file
//! into `scan_errors` — never bail the whole inventory.

use std::collections::HashMap;
use std::path::Path;

use super::{AcceptanceSignal, PlanStep, SignalKind, SpecInventory, TestActionSummary};

/// Per-kind per-file cap before the global budget kicks in.
const PER_KIND_PER_FILE_CAP: usize = 12;
/// Total signal budget for the whole inventory.
const TOTAL_CAP: usize = 160;
/// Compressed action-chain budget per test.
const SUMMARY_CHARS: usize = 120;
/// Per-step summary cap at render time is applied in render.rs; here we
/// keep at most this many summaries per file.
const SUMMARIES_PER_FILE_CAP: usize = 24;
/// Single-file read cap (mirrors the walker's limit).
const MAX_FILE_BYTES: u64 = 1024 * 1024;

/// Signals + summaries extracted from ONE spec file.
#[derive(Debug, Default)]
pub struct FileSignals {
    signals: Vec<AcceptanceSignal>,
    summaries: Vec<TestActionSummary>,
    /// helper function name → action chain INSIDE the helper body. Test
    /// summaries expand these after ALL files are scanned (helpers may
    /// sit after the test, or in support/*.ts) — navigation chains like
    /// openRegister → goto → click a[href] must reach the model.
    helper_actions: std::collections::HashMap<String, Vec<String>>,
}

/// Scan a staged spec directory: top-level `*.spec.ts` plus
/// `support/**/*.ts` (node_modules skipped; 1MB/file cap; deterministic
/// order). Unassigned signals land in `inventory.globals`.
pub fn scan_spec_dir(dir: &Path) -> SpecInventory {
    let mut inventory = SpecInventory::default();
    let files = collect_spec_files(dir);
    inventory.files = files.to_vec();

    let mut all: Vec<AcceptanceSignal> = Vec::new();
    let mut helper_registry: std::collections::HashMap<String, Vec<String>> =
        std::collections::HashMap::new();
    for rel in &files {
        let path = dir.join(rel);
        let source = match std::fs::read(&path) {
            Ok(bytes) if bytes.len() as u64 <= MAX_FILE_BYTES => {
                String::from_utf8_lossy(&bytes).into_owned()
            }
            Ok(_) => {
                inventory
                    .scan_errors
                    .push(format!("{rel}: file exceeds 1MB cap"));
                continue;
            }
            Err(e) => {
                inventory.scan_errors.push(format!("{rel}: read error {e}"));
                continue;
            }
        };
        match extract_spec_file(&source, rel) {
            Ok(mut fs) => {
                if fs.summaries.len() > SUMMARIES_PER_FILE_CAP {
                    fs.summaries.truncate(SUMMARIES_PER_FILE_CAP);
                    inventory.truncated = true;
                }
                inventory.summaries.append(&mut fs.summaries);
                all.append(&mut fs.signals);
                helper_registry.extend(fs.helper_actions);
            }
            Err(e) => inventory.scan_errors.push(format!("{rel}: {e}")),
        }
    }

    // Global (kind, value) dedupe — first occurrence wins.
    let mut seen = std::collections::HashSet::new();
    all.retain(|s| seen.insert((s.kind, s.value.clone())));

    // Second pass: expand `{helper:…}` placeholders in test summaries
    // against the global registry (single-file parses cannot see helpers
    // declared later or in support/*.ts).
    for summary in &mut inventory.summaries {
        summary.summary = expand_summary(&summary.summary, &helper_registry);
    }

    // Per-kind-per-file cap.
    let mut per_kind_file: HashMap<(SignalKind, String), usize> = HashMap::new();
    all.retain(|s| {
        let key = (s.kind, s.spec_file.clone());
        let n = per_kind_file.entry(key).or_insert(0);
        *n += 1;
        *n <= PER_KIND_PER_FILE_CAP
    });

    // Global budget: keep by priority (TestId > Goto/Href > ApiRoute >
    // Port > Role > Label > Text > Placeholder > OptionLabel > Title).
    if all.len() > TOTAL_CAP {
        inventory.truncated = true;
        all.sort_by_key(|s| {
            (
                kind_priority(s.kind),
                s.spec_file.clone(),
                signal_line_rank(s),
            )
        });
        all.truncate(TOTAL_CAP);
        all.sort_by(|a, b| a.spec_file.cmp(&b.spec_file));
    }
    inventory.globals = all;

    // Distinct ports seen in url/port signals.
    let mut ports = std::collections::BTreeSet::new();
    for s in &inventory.globals {
        if let Some(p) = port_of_signal(s) {
            ports.insert(p);
        }
    }
    inventory.ports = ports.into_iter().collect();
    inventory
}

/// Move signals/summaries into `by_step` buckets by matching the owning
/// spec file's basename against `PlanStep.targets.spec_files`. Anything
/// unmapped (helper files, unknown basenames) stays in globals.
pub fn assign_to_steps(inventory: &mut SpecInventory, steps: &[PlanStep]) {
    let mut by_basename: HashMap<String, String> = HashMap::new();
    for step in steps {
        for spec in &step.targets.spec_files {
            by_basename.insert(basename(spec), step.id.clone());
        }
    }
    let globals = std::mem::take(&mut inventory.globals);
    let mut unassigned = Vec::new();
    for signal in globals {
        match by_basename.get(basename(&signal.spec_file).as_str()) {
            Some(step_id) => inventory
                .by_step
                .entry(step_id.clone())
                .or_default()
                .push(signal),
            None => unassigned.push(signal),
        }
    }
    inventory.globals = unassigned;
    for summary in &mut inventory.summaries {
        summary.step_id = by_basename
            .get(basename(&summary.spec_file).as_str())
            .cloned();
    }
}

fn basename(path: &str) -> String {
    path.rsplit('/').next().unwrap_or(path).to_string()
}

fn port_of_signal(s: &AcceptanceSignal) -> Option<u16> {
    match s.kind {
        SignalKind::Port => s.value.parse().ok(),
        SignalKind::Url => {
            let after = s.value.split("://").nth(1)?;
            let host_port = after.split('/').next()?;
            host_port.split(':').nth(1)?.parse().ok()
        }
        _ => None,
    }
}

fn kind_priority(kind: SignalKind) -> u8 {
    match kind {
        SignalKind::Testid => 0,
        SignalKind::Goto | SignalKind::Href => 1,
        SignalKind::ApiRoute => 2,
        SignalKind::Port | SignalKind::Url => 3,
        SignalKind::RoleName => 4,
        SignalKind::Label => 5,
        SignalKind::Text => 6,
        SignalKind::Placeholder => 7,
        SignalKind::OptionLabel => 8,
        SignalKind::TestTitle => 9,
    }
}

/// Deterministic rank for stable sorting: approximate by value bytes.
fn signal_line_rank(s: &AcceptanceSignal) -> String {
    s.value.clone()
}

/// Deterministic file list: sorted top-level `*.spec.ts`, then
/// `support/**/*.ts`, both relative to `dir`.
fn collect_spec_files(dir: &Path) -> Vec<String> {
    let mut specs = Vec::new();
    let mut support = Vec::new();
    let Ok(read_dir) = std::fs::read_dir(dir) else {
        return specs;
    };
    for entry in read_dir.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if path.is_file() && name.ends_with(".spec.ts") {
            specs.push(name);
        }
    }
    // One level of `support/` is the Playwright convention; go two deep
    // to be safe but stay bounded.
    for sub in ["support"] {
        let sub_dir = dir.join(sub);
        let Ok(read_dir) = std::fs::read_dir(&sub_dir) else {
            continue;
        };
        for entry in read_dir.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if path.is_file() && name.ends_with(".ts") && !name.ends_with(".d.ts") {
                support.push(format!("{sub}/{name}"));
            } else if path.is_dir() {
                if let Ok(inner) = std::fs::read_dir(&path) {
                    for e2 in inner.flatten() {
                        let p2 = e2.path();
                        let n2 = e2.file_name().to_string_lossy().into_owned();
                        if p2.is_file() && n2.ends_with(".ts") && !n2.ends_with(".d.ts") {
                            support.push(format!("{sub}/{}/{n2}", name));
                        }
                    }
                }
            }
        }
    }
    specs.sort();
    support.sort();
    specs.extend(support);
    specs
}

// ── Per-file extraction ─────────────────────────────────────────────

const BY_PREFIXES: &[(&str, SignalKind)] = &[
    ("getByTestId", SignalKind::Testid),
    ("getByRole", SignalKind::RoleName),
    ("getByLabel", SignalKind::Label),
    ("getByPlaceholder", SignalKind::Placeholder),
    ("getByText", SignalKind::Text),
    ("getByTitle", SignalKind::Text),
];

/// Walk one spec file. TS grammar: the specs carry type annotations the
/// plain JS grammar rejects.
pub fn extract_spec_file(source: &str, file: &str) -> eyre::Result<FileSignals> {
    let mut parser = tree_sitter::Parser::new();
    parser
        .set_language(&tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into())
        .map_err(|e| eyre::eyre!("failed to load TS grammar: {e}"))?;
    let tree = parser
        .parse(source, None)
        .ok_or_else(|| eyre::eyre!("failed to parse"))?;
    if tree.root_node().has_error() {
        return Err(eyre::eyre!("parse errors (tree-sitter ERROR nodes)"));
    }
    let bytes = source.as_bytes();

    let mut out = FileSignals::default();
    let mut ctx = WalkCtx {
        bytes,
        file,
        enclosing_fn: Vec::new(),
        out: &mut out,
        helper_actions: std::collections::HashMap::new(),
    };
    let mut smoke = SmokeCtx::default();
    walk_signals(tree.root_node(), &mut ctx, &mut smoke);

    // Literal fallbacks over the raw text: bare `/api/…` route strings,
    // ports/urls, `[data-testid="…"]` inside selector strings.
    literal_fallbacks(source, file, &mut ctx);
    out.helper_actions = ctx.helper_actions;
    Ok(out)
}

/// Actions directly inside a helper body (incl. `{helper:name}` call
/// placeholders expanded later against the global registry).
fn collect_body_actions(fn_node: tree_sitter::Node, bytes: &[u8]) -> Vec<String> {
    let mut actions: Vec<String> = Vec::new();
    let mut stack = vec![fn_node];
    while let Some(n) = stack.pop() {
        if n.kind() == "call_expression" {
            if let Some(fn_node) = n.child_by_field_name("function") {
                if fn_node.kind() == "identifier" {
                    if let Some(name) = fn_node.utf8_text(bytes).ok() {
                        actions.push(format!("{{helper:{name}}}"));
                    }
                }
            }
            if let Some(step) = classify_action(n, bytes) {
                if !actions.contains(&step) {
                    actions.push(step);
                }
            }
        }
        let mut cursor = n.walk();
        for child in n.children(&mut cursor) {
            stack.push(child);
        }
    }
    actions.reverse();
    actions.truncate(10);
    actions
}

struct WalkCtx<'a> {
    bytes: &'a [u8],
    file: &'a str,
    enclosing_fn: Vec<String>,
    out: &'a mut FileSignals,
    helper_actions: std::collections::HashMap<String, Vec<String>>,
}

/// Test-title bookkeeping while walking (for action summaries).
#[derive(Default)]
struct SmokeCtx {
    current_test: Option<String>,
}

impl<'a> WalkCtx<'a> {
    fn note(&self) -> Option<String> {
        self.enclosing_fn.last().cloned()
    }

    fn push_signal(&mut self, kind: SignalKind, value: String) {
        if value.is_empty() {
            return;
        }
        self.out.signals.push(AcceptanceSignal {
            kind,
            value,
            note: self.note(),
            spec_file: self.file.to_string(),
        });
    }
}

/// Document-order recursive walk collecting signals and, inside `test()`
/// bodies, the compressed action chain.
fn walk_signals(node: tree_sitter::Node, ctx: &mut WalkCtx, smoke: &mut SmokeCtx) {
    match node.kind() {
        "call_expression" => {
            if let Some(call) = classify_call(node, ctx) {
                match call {
                    ClassifiedCall::TestTitle(title) => {
                        // Record title signal + walk the body for the
                        // action chain.
                        let prev = smoke.current_test.clone();
                        smoke.current_test = Some(title.clone());
                        ctx.push_signal(SignalKind::TestTitle, title.clone());
                        let summary = action_summary(node, ctx.bytes);
                        if !summary.is_empty() {
                            ctx.out.summaries.push(TestActionSummary {
                                spec_file: ctx.file.to_string(),
                                test_title: title,
                                summary,
                                step_id: None,
                            });
                        }
                        walk_children(node, ctx, smoke);
                        smoke.current_test = prev;
                        return;
                    }
                    ClassifiedCall::Signal(kind, value) => {
                        ctx.push_signal(kind, value);
                    }
                }
            }
        }
        "function_declaration" => {
            if let Some(name) = node
                .child_by_field_name("name")
                .and_then(|n| n.utf8_text(ctx.bytes).ok().map(String::from))
            {
                ctx.helper_actions
                    .insert(name.clone(), collect_body_actions(node, ctx.bytes));
                ctx.enclosing_fn.push(name);
                walk_children(node, ctx, smoke);
                ctx.enclosing_fn.pop();
                return;
            }
        }
        "variable_declarator" => {
            // `export async function`-style: `const name = (…) => …`
            if let Some(name) = node
                .child_by_field_name("name")
                .and_then(|n| n.utf8_text(ctx.bytes).ok().map(String::from))
            {
                let is_fn = node
                    .child_by_field_name("value")
                    .map(|v| {
                        matches!(
                            v.kind(),
                            "arrow_function" | "function_expression" | "function"
                        )
                    })
                    .unwrap_or(false);
                if is_fn {
                    ctx.helper_actions
                        .insert(name.clone(), collect_body_actions(node, ctx.bytes));
                    ctx.enclosing_fn.push(name);
                    walk_children(node, ctx, smoke);
                    ctx.enclosing_fn.pop();
                    return;
                }
            }
        }
        _ => {}
    }
    walk_children(node, ctx, smoke);
}

fn walk_children(node: tree_sitter::Node, ctx: &mut WalkCtx, smoke: &mut SmokeCtx) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk_signals(child, ctx, smoke);
    }
}

enum ClassifiedCall {
    /// `test('title', …)` / `test.describe('title', …)`
    TestTitle(String),
    /// A plain locator/goto signal.
    Signal(SignalKind, String),
}

/// Recognize the locator/goto/test calls at one call_expression node.
fn classify_call(node: tree_sitter::Node, ctx: &WalkCtx) -> Option<ClassifiedCall> {
    let fn_node = node.child_by_field_name("function")?;
    let property = match fn_node.kind() {
        "member_expression" => fn_node.child_by_field_name("property")?,
        _ => {
            // Bare `test('title', …)` identifier call.
            if fn_node.kind() == "identifier" && text(fn_node, ctx.bytes).as_deref() == Some("test")
            {
                let title = first_string_arg(node, ctx.bytes)?;
                return Some(ClassifiedCall::TestTitle(title));
            }
            return None;
        }
    };
    let prop = text(property, ctx.bytes)?;
    match prop.as_str() {
        "getByTestId" | "getByRole" | "getByLabel" | "getByPlaceholder" | "getByText"
        | "getByTitle" => {
            let (_, kind) = BY_PREFIXES
                .iter()
                .find(|(p, _)| *p == prop)
                .expect("prefix from the table");
            if prop == "getByRole" {
                let role = first_string_arg(node, ctx.bytes)?;
                let mut value = role.clone();
                if let Some(name) = options_name(node, ctx.bytes) {
                    value = format!("{role} {name}");
                }
                Some(ClassifiedCall::Signal(*kind, value))
            } else {
                // First string arg, or the `name:` option, or a regex.
                if let Some(v) = first_string_arg(node, ctx.bytes) {
                    return Some(ClassifiedCall::Signal(*kind, v));
                }
                if let Some(v) = first_regex_arg(node, ctx.bytes) {
                    return Some(ClassifiedCall::Signal(*kind, v));
                }
                if prop == "getByRole" {
                    return None;
                }
                options_name(node, ctx.bytes).map(|v| ClassifiedCall::Signal(*kind, v))
            }
        }
        "goto" => {
            // `page.goto('/x')` — only literal-string args (a call like
            // baseUrl() is honestly skipped; its URL shows up as a url
            // signal from the string literal).
            let v = first_string_arg(node, ctx.bytes)?;
            Some(ClassifiedCall::Signal(SignalKind::Goto, v))
        }
        "locator" => {
            let selector = first_string_arg(node, ctx.bytes)?;
            let href = unescape_href(&selector)?;
            Some(ClassifiedCall::Signal(SignalKind::Href, href))
        }
        "describe" => {
            // test.describe('title', …)
            let object = fn_node.child_by_field_name("object")?;
            if text(object, ctx.bytes).as_deref() == Some("test") {
                let title = first_string_arg(node, ctx.bytes)?;
                Some(ClassifiedCall::TestTitle(title))
            } else {
                None
            }
        }
        _ => {
            // Select-alias tables: `{ passport: ['passport', '护照'], … }`
            // — signals come from literal_fallbacks, not the walker.
            None
        }
    }
}

/// Compress a `test()` body into one action chain line.
fn action_summary(test_call: tree_sitter::Node, bytes: &[u8]) -> String {
    let mut actions: Vec<String> = Vec::new();
    let mut stack = vec![test_call];
    while let Some(n) = stack.pop() {
        if n.kind() == "call_expression" {
            if let Some(fn_node) = n.child_by_field_name("function") {
                if fn_node.kind() == "identifier" {
                    if let Some(name) = fn_node.utf8_text(bytes).ok() {
                        // Placeholder — expanded against the GLOBAL helper
                        // registry once every file has been scanned
                        // (helpers may be declared after the test, or in
                        // support/*.ts).
                        actions.push(format!("{{helper:{name}}}"));
                    }
                }
            }
            if let Some(step) = classify_action(n, bytes) {
                if !actions.contains(&step) {
                    actions.push(step);
                }
            }
            if actions.len() >= 12 {
                break;
            }
        }
        let mut cursor = n.walk();
        for child in n.children(&mut cursor) {
            stack.push(child);
        }
    }
    actions.reverse(); // stack DFS reversed = document order
    actions.truncate(12);
    actions.join(" → ")
}

/// Expand `{helper:name}` placeholders in a raw summary against the
/// global helper registry (navigation chains like openRegister → goto →
/// click a[href="/register"] must reach the model, or the "walk the
/// action chain" instruction has no first step). Cycles cut, nesting
/// bounded, final text capped at [`SUMMARY_CHARS`].
pub(crate) fn expand_summary(
    raw: &str,
    helpers: &std::collections::HashMap<String, Vec<String>>,
) -> String {
    fn expand_chain(
        chain: &[String],
        helpers: &std::collections::HashMap<String, Vec<String>>,
        visited: &mut Vec<String>,
        out: &mut Vec<String>,
    ) {
        for step in chain {
            if let Some(name) = step
                .strip_prefix("{helper:")
                .and_then(|s| s.strip_suffix('}'))
            {
                if visited.iter().any(|v| v == name) || visited.len() >= 4 {
                    continue;
                }
                if let Some(sub) = helpers.get(name) {
                    visited.push(name.to_string());
                    expand_chain(sub, helpers, visited, out);
                }
            } else if !out.contains(step) {
                out.push(step.clone());
            }
        }
    }
    let raw_chain: Vec<String> = raw.split(" → ").map(String::from).collect();
    let mut expanded = Vec::new();
    expand_chain(&raw_chain, helpers, &mut Vec::new(), &mut expanded);
    expanded.truncate(12);
    let mut summary = expanded.join(" → ");
    if summary.chars().count() > SUMMARY_CHARS {
        summary = format!(
            "{}…",
            summary
                .chars()
                .take(SUMMARY_CHARS.saturating_sub(1))
                .collect::<String>()
        );
    }
    summary
}

/// One chained action → `verb target` node.
fn classify_action(call: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    let fn_node = call.child_by_field_name("function")?;
    if fn_node.kind() != "member_expression" {
        return None;
    }
    let property = fn_node.child_by_field_name("property")?;
    let prop = text(property, bytes)?;
    let object = fn_node.child_by_field_name("object")?;
    let locator = locator_name(object, bytes);
    match prop.as_str() {
        "goto" => {
            let arg = call.child_by_field_name("arguments").and_then(|a| {
                let mut c = a.walk();
                a.children(&mut c)
                    .find(|n| n.kind() == "string")
                    .and_then(|n| n.utf8_text(bytes).ok().map(String::from))
            });
            Some(format!(
                "goto {}",
                arg.unwrap_or_default().trim_matches(['"', '\''])
            ))
        }
        "fill" => Some(format!(
            "fill {}",
            locator.unwrap_or_else(|| "field".into())
        )),
        "check" => Some(format!(
            "check {}",
            locator.unwrap_or_else(|| "control".into())
        )),
        "uncheck" => Some(format!(
            "uncheck {}",
            locator.unwrap_or_else(|| "control".into())
        )),
        "click" => Some(format!(
            "click {}",
            locator.unwrap_or_else(|| "element".into())
        )),
        "selectOption" => Some(format!(
            "select {}",
            locator.unwrap_or_else(|| "select".into())
        )),
        p if p.starts_with("toBe") || p.starts_with("toHave") || p == "toContainText" => {
            let short = match p {
                "toBeVisible" => "visible",
                "toBeHidden" => "hidden",
                "toHaveValue" => "value",
                "toHaveText" | "toContainText" => "text",
                "toHaveCount" => "count",
                _ => "assert",
            };
            Some(format!("expect {short}"))
        }
        _ => None,
    }
}

/// Walk a call/member chain down to the nearest getBy* hop and name it.
fn locator_name(mut node: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    loop {
        match node.kind() {
            "call_expression" => {
                if let Some(fn_node) = node.child_by_field_name("function") {
                    if fn_node.kind() == "member_expression" {
                        if let Some(property) = fn_node.child_by_field_name("property") {
                            let prop = text(property, bytes)?;
                            match prop.as_str() {
                                "getByLabel" | "getByPlaceholder" | "getByText" | "getByTitle"
                                | "getByTestId" => {
                                    return first_string_arg(node, bytes)
                                        .or_else(|| first_regex_arg(node, bytes));
                                }
                                "getByRole" => {
                                    let role = first_string_arg(node, bytes)?;
                                    if let Some(name) = options_name(node, bytes) {
                                        return Some(format!("{role} {name}"));
                                    }
                                    return Some(role);
                                }
                                _ => {
                                    node = fn_node.child_by_field_name("object").unwrap_or(node);
                                    continue;
                                }
                            }
                        }
                    }
                }
                return None;
            }
            "member_expression" => {
                node = node.child_by_field_name("object").unwrap_or(node);
            }
            _ => return None,
        }
    }
}

/// `name:` option out of a `{ name: … }` options object — string or regex.
fn options_name(call: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    let args = call.child_by_field_name("arguments")?;
    let mut cursor = args.walk();
    for arg in args.children(&mut cursor).filter(|c| c.is_named()) {
        if arg.kind() != "object" {
            continue;
        }
        let mut obj_cursor = arg.walk();
        for pair in arg.children(&mut obj_cursor).filter(|c| c.is_named()) {
            if pair.kind() != "pair" {
                continue;
            }
            let key = pair.child_by_field_name("key");
            let value = pair.child_by_field_name("value");
            if let (Some(k), Some(v)) = (key, value) {
                if text(k, bytes).as_deref() == Some("name") {
                    return match v.kind() {
                        "string" => v
                            .child_by_field_name("value") // ts: string has `value` field? no — use child kind
                            .and_then(|n| text(n, bytes))
                            .or_else(|| string_value(v, bytes)),
                        "regex" => regex_pattern(v, bytes),
                        _ => None,
                    };
                }
            }
        }
    }
    None
}

fn first_string_arg(call: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    let args = call.child_by_field_name("arguments")?;
    let mut cursor = args.walk();
    args.children(&mut cursor)
        .filter(|c| c.is_named())
        .find(|c| c.kind() == "string")
        .and_then(|n| string_value(n, bytes))
}

fn first_regex_arg(call: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    let args = call.child_by_field_name("arguments")?;
    let mut cursor = args.walk();
    args.children(&mut cursor)
        .filter(|c| c.is_named())
        .find(|c| c.kind() == "regex")
        .and_then(|n| regex_pattern(n, bytes))
}

/// The inner text of a string literal node.
fn string_value(string_node: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    let mut cursor = string_node.walk();
    string_node
        .children(&mut cursor)
        .find(|c| c.kind() == "string_fragment")
        .and_then(|c| text(c, bytes))
}

/// The pattern text of a regex literal node (flags dropped; the `/…/`
/// shape is kept so anchored patterns read naturally).
fn regex_pattern(regex_node: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    let mut cursor = regex_node.walk();
    let pattern = regex_node
        .children(&mut cursor)
        .find(|c| c.kind() == "regex_pattern")
        .and_then(|c| text(c, bytes))?;
    Some(format!("/{pattern}/"))
}

/// `'a[href="/register"]'` → `"/register"` (tiny unescaper; CSS-escaped
/// sequences are copied through untouched).
fn unescape_href(selector: &str) -> Option<String> {
    let key = "a[href=\"";
    let start = selector.find(key)? + key.len();
    let rest = &selector[start..];
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

fn text(node: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    node.utf8_text(bytes).ok().map(String::from)
}

/// Text-level fallbacks over the raw source: bare `/api/…` strings,
/// `127.0.0.1:PORT`/`localhost:PORT` urls+ports, `[data-testid="…"]`,
/// and select-alias tables (`key: ['a', 'b']`).
fn literal_fallbacks(source: &str, file: &str, ctx: &mut WalkCtx) {
    // data-testid literal fallback inside any string.
    const TD: &str = "data-testid=\"";
    for line in source.lines() {
        let mut rest = line;
        while let Some(at) = rest.find(TD) {
            let after = &rest[at + TD.len()..];
            if let Some(end) = after.find('"') {
                let value = &after[..end];
                if !value.is_empty() && !value.contains(' ') {
                    ctx.push_signal(SignalKind::Testid, value.to_string());
                }
                rest = &after[end + 1..];
            } else {
                break;
            }
        }
    }

    // Alias tables: `key: ['a', 'b']` object entries → option labels.
    for line in source.lines() {
        let Some(colon) = line.find(": [") else {
            continue;
        };
        let key: String = line[..colon]
            .trim()
            .trim_matches(['"', '\'', ','])
            .to_string();
        if key.is_empty() || !key.chars().next().is_some_and(|c| c.is_ascii_alphabetic()) {
            continue;
        }
        let bracket_start = line[colon..].find('[').unwrap() + colon;
        let Some(close) = line[bracket_start..].find(']') else {
            continue;
        };
        let inner = &line[bracket_start + 1..bracket_start + close];
        for item in inner.split(',') {
            let v = item.trim().trim_matches(['\'', '"']).trim();
            if !v.is_empty() {
                ctx.out.signals.push(AcceptanceSignal {
                    kind: SignalKind::OptionLabel,
                    value: v.to_string(),
                    note: Some(key.clone()),
                    spec_file: file.to_string(),
                });
            }
        }
    }

    // URL + port literals.
    for needle in [
        "http://127.0.0.1:",
        "http://localhost:",
        "https://localhost:",
    ] {
        let mut rest = source;
        while let Some(at) = rest.find(needle) {
            let after = &rest[at..];
            let end = after
                .find(|c: char| {
                    !(c.is_ascii_digit()
                        || c == ':'
                        || c.is_ascii_alphabetic()
                        || c == '.'
                        || c == '/'
                        || c == '-'
                        || c == '_')
                })
                .unwrap_or(after.len());
            let url = after[..end].trim_end_matches(['/', '.', ':']).to_string();
            if let Some(port) = url.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()) {
                if port > 0 {
                    ctx.push_signal(SignalKind::Url, url.clone());
                    ctx.push_signal(SignalKind::Port, port.to_string());
                }
            }
            if let Some(path_at) = url.find("/api/") {
                let path = url[path_at..].trim_end_matches('/').to_string();
                if path.len() > "/api/".len() {
                    ctx.push_signal(SignalKind::ApiRoute, path);
                }
            }
            rest = &rest[at + needle.len()..];
        }
    }

    // Bare `/api/…` route strings.
    for line in source.lines() {
        let mut rest = line;
        while let Some(at) = rest.find("/api/") {
            // Must open a string literal (quote just before).
            let before = rest[..at].chars().last();
            if !matches!(before, Some('\'') | Some('"') | Some('`')) {
                rest = &rest[at + 5..];
                continue;
            }
            let after = &rest[at..];
            let end = after
                .find(|c: char| c == '\'' || c == '"' || c == '`' || c.is_whitespace())
                .unwrap_or(after.len());
            let route = after[..end].trim_end_matches(['\'', '"', '`']).to_string();
            if route.len() > "/api/".len() {
                ctx.push_signal(SignalKind::ApiRoute, route);
            }
            rest = &rest[end.max(5)..];
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixtures_dir() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("arc-plan")
    }

    fn has(inv: &SpecInventory, kind: SignalKind, value: &str) -> bool {
        inv.globals
            .iter()
            .chain(inv.by_step.values().flatten())
            .any(|s| s.kind == kind && s.value == value)
    }

    #[test]
    fn smoke_spec_yields_testid_role_and_goto() {
        let inv = scan_spec_dir(&fixtures_dir().join("smoke-only"));
        let _ = inv; // populated below by the direct-file variant

        // Direct-file variant (the smoke fixture lives next to the ticket
        // files; scan a synthetic dir to isolate it).
        let dir = tempfile::tempdir().unwrap();
        std::fs::copy(
            fixtures_dir().join("smoke-REQ-1.spec.ts"),
            dir.path().join("REQ-1.spec.ts"),
        )
        .unwrap();
        let inv = scan_spec_dir(dir.path());
        assert!(has(&inv, SignalKind::Testid, "count"), "{:?}", inv.globals);
        assert!(has(&inv, SignalKind::RoleName, "button Increment"));
        assert!(has(&inv, SignalKind::Goto, "/"));
        assert!(has(
            &inv,
            SignalKind::TestTitle,
            "REQ-1: increment and decrement the counter"
        ));
    }

    #[test]
    fn ticket_specs_yield_regex_names_meter_href_and_port() {
        let inv = scan_spec_dir(&fixtures_dir());
        // regex name pattern from getByLabel
        assert!(
            inv.globals
                .iter()
                .any(|s| s.kind == SignalKind::Label && s.value.contains("证件号码")),
            "regex label: {:?}",
            inv.globals
        );
        // meter role with bilingual regex name
        assert!(
            inv.globals
                .iter()
                .any(|s| s.kind == SignalKind::RoleName && s.value.starts_with("meter "))
        );
        // alert role (bare, no name)
        assert!(has(&inv, SignalKind::RoleName, "alert"));
        // data-testid fallback + getByTestId
        assert!(has(&inv, SignalKind::Testid, "password-strength"));
        // hrefs folded from the support helpers
        assert!(has(&inv, SignalKind::Href, "/register"));
        assert!(has(&inv, SignalKind::Href, "/login"));
        // port from the support baseUrl default
        assert!(inv.ports.contains(&3301), "ports: {:?}", inv.ports);
        assert!(has(&inv, SignalKind::Port, "3301"));
        // api routes from the synthetic api spec
        assert!(has(&inv, SignalKind::ApiRoute, "/api/health"));
        assert!(has(&inv, SignalKind::ApiRoute, "/api/trains/search"));
        // option aliases folded from the support table
        assert!(has(&inv, SignalKind::OptionLabel, "护照"));
        assert!(has(&inv, SignalKind::OptionLabel, "adult"));
        // helper note carries the enclosing function name
        let register_href = inv
            .globals
            .iter()
            .find(|s| s.kind == SignalKind::Href && s.value == "/register")
            .unwrap();
        assert_eq!(register_href.note.as_deref(), Some("openRegister"));
        assert_eq!(
            register_href.spec_file, "support/e2e.ts",
            "support signals fold into globals"
        );
    }

    #[test]
    fn action_summaries_compress_test_bodies() {
        let inv = scan_spec_dir(&fixtures_dir());
        assert!(!inv.summaries.is_empty());
        let smoke = inv
            .summaries
            .iter()
            .find(|s| s.test_title.contains("increment"))
            .expect("smoke summary");
        assert!(
            smoke.summary.starts_with("goto /") && smoke.summary.contains("click"),
            "smoke chain: {}",
            smoke.summary
        );
        for s in &inv.summaries {
            assert!(
                s.summary.chars().count() <= 120,
                "summary too long: {}",
                s.summary
            );
        }
    }

    #[test]
    fn helper_navigation_chains_expand_into_test_summaries() {
        // r1 postmortem: every goto lived in a helper (openRegister →
        // openHome → page.goto, locator('a[href]')).click), so summaries
        // shipped without ANY navigation step and the model had no first
        // step to walk. The registry must inline them across files
        // (registerAccount in the spec, fillRegistrationFields in
        // support/e2e.ts).
        let inv = scan_spec_dir(&fixtures_dir());
        let with_helper_nav = inv
            .summaries
            .iter()
            .filter(|s| s.summary.contains("goto"))
            .count();
        assert!(
            with_helper_nav > 0,
            "no summary carries a goto step: {:?}",
            inv.summaries
                .iter()
                .map(|s| s.summary.clone())
                .take(6)
                .collect::<Vec<_>>()
        );
        let reg = inv
            .summaries
            .iter()
            .find(|s| s.test_title.contains("successfully register"))
            .expect("register summary");
        assert!(
            reg.summary.contains("goto") && reg.summary.contains("fill"),
            "register chain must open with navigation then fill: {}",
            reg.summary
        );
        assert!(
            reg.summary.contains("click") && reg.summary.contains("expect"),
            "register chain must carry submit+assertion: {}",
            reg.summary
        );
        // No unexpanded placeholders may leak.
        for s in &inv.summaries {
            assert!(
                !s.summary.contains("{helper:"),
                "unexpanded placeholder leaked: {}",
                s.summary
            );
        }
    }

    #[test]
    fn helper_expansion_cuts_cycles_and_respects_budget() {
        let mut helpers = std::collections::HashMap::new();
        helpers.insert("a".to_string(), vec!["{helper:b}".into(), "click A".into()]);
        helpers.insert("b".to_string(), vec!["{helper:a}".into(), "fill B".into()]);
        let out = expand_summary("{helper:a} → expect visible", &helpers);
        assert!(out.contains("click A") && out.contains("fill B"));
        assert!(!out.contains("{helper:"), "cycle must be cut: {out}");
        assert!(out.chars().count() <= 120);
    }

    #[test]
    fn assign_to_steps_buckets_by_basename_and_keeps_globals() {
        let mut inv = scan_spec_dir(&fixtures_dir());
        let steps = vec![
            PlanStep {
                id: "REQ-1".into(),
                targets: super::super::StepTargets {
                    spec_files: vec![
                        "REQ-1.1-user-registration.spec.ts".into(),
                        "REQ-1.2-user-login.spec.ts".into(),
                    ],
                    ..Default::default()
                },
                ..Default::default()
            },
            PlanStep {
                id: "REQ-2".into(),
                targets: super::super::StepTargets {
                    spec_files: vec!["REQ-3-api-contract.spec.ts".into()],
                    ..Default::default()
                },
                ..Default::default()
            },
        ];
        assign_to_steps(&mut inv, &steps);
        // Collision group: the REQ-1 step owns BOTH REQ-1.1/REQ-1.2 spec
        // files (the adapter wrote them into its targets), so its bucket
        // fills with signals from those files even though no file is
        // basename-equal to the step id.
        assert!(inv.by_step.contains_key("REQ-1"));
        assert!(
            inv.by_step
                .get("REQ-1")
                .unwrap()
                .iter()
                .all(|s| s.spec_file.starts_with("REQ-1."))
        );
        assert!(inv.by_step.contains_key("REQ-2"));
        // support file stays global
        assert!(inv.globals.iter().any(|s| s.spec_file == "support/e2e.ts"));
        // summaries got step ids
        let api_summary = inv
            .summaries
            .iter()
            .find(|s| s.spec_file == "REQ-3-api-contract.spec.ts")
            .unwrap();
        assert_eq!(api_summary.step_id.as_deref(), Some("REQ-2"));
    }

    #[test]
    fn collision_group_maps_prefixed_specs_to_prefix_steps() {
        // The adapter's rule: `REQ-1.1-x.spec.ts` maps to the `REQ-1`
        // step when no step is named `REQ-1.1`. Kernel assignment is
        // basename-exact; the collision resolution lives adapter-side
        // (plan_envelope.py writes targets.spec_files per step). Here we
        // pin that exact-basename assignment never misroutes.
        let mut inv = scan_spec_dir(&fixtures_dir());
        let steps = vec![PlanStep {
            id: "REQ-1".into(),
            targets: super::super::StepTargets {
                spec_files: vec!["REQ-1.1-user-registration.spec.ts".into()],
                ..Default::default()
            },
            ..Default::default()
        }];
        assign_to_steps(&mut inv, &steps);
        assert!(inv.by_step.contains_key("REQ-1"));
        let sigs = &inv.by_step["REQ-1"];
        assert!(
            sigs.iter()
                .all(|s| s.spec_file == "REQ-1.1-user-registration.spec.ts")
        );
    }

    #[test]
    fn broken_spec_file_is_skipped_not_fatal() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("REQ-9-broken.spec.ts"), "test((((( {{").unwrap();
        std::fs::write(
            dir.path().join("REQ-1-ok.spec.ts"),
            "test('ok', async ({page}) => { await page.goto('/x'); });",
        )
        .unwrap();
        let inv = scan_spec_dir(dir.path());
        assert_eq!(inv.scan_errors.len(), 1, "{:?}", inv.scan_errors);
        assert!(has(&inv, SignalKind::Goto, "/x"), "good file still scanned");
        assert!(inv.files.contains(&"REQ-9-broken.spec.ts".to_string()));
    }

    #[test]
    fn missing_dir_yields_empty_inventory() {
        let inv = scan_spec_dir(std::path::Path::new("/nonexistent-arc-specs"));
        assert!(inv.globals.is_empty());
        assert!(inv.files.is_empty());
    }

    #[test]
    fn caps_and_dedupe_hold_on_repeated_signals() {
        let mut many = String::from("import { test, expect } from '@playwright/test';\n");
        for i in 0..40 {
            many.push_str(&format!(
                "test('t{i}', async ({{ page }}) => {{ await page.getByTestId('dup-id').click(); }});\n"
            ));
        }
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("REQ-1.spec.ts"), many).unwrap();
        let inv = scan_spec_dir(dir.path());
        let dupes = inv
            .globals
            .iter()
            .filter(|s| s.kind == SignalKind::Testid && s.value == "dup-id")
            .count();
        assert_eq!(dupes, 1, "(kind,value) must dedupe globally");
    }
}
