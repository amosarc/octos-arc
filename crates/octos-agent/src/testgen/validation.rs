//! Validation-rule checklist extraction for the testgen pipeline.
//!
//! Scans a parsed JS source for the shapes a validation branch takes:
//! requiredness (`if (!x)`, `x === undefined`), length bounds
//! (`x.length < n`), type checks (`typeof x !== 'string'`), format regexes
//! (`/^..$/.test(x)`), enum membership (`OPTIONS.includes(x)`), and the
//! validation error branches themselves (`res.status(400|422)`).
//!
//! The output is an evidence-backed checklist — every rule carries the file
//! and line it was seen at. It is explicitly a *minimum* coverage set, not
//! a semantic guarantee: the checklist tells the TestWriter what the code
//! already enforces so those branches get test cases; it does not claim to
//! find every business rule.

use eyre::Result;
use serde::Serialize;
use tree_sitter::Node;

use super::contract::{node_text, parse_js};

/// Kind of validation rule observed.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "kind", content = "value")]
pub enum RuleKind {
    /// Field must be present/truthy (`if (!x)`, `x === undefined`).
    Required,
    /// `x.length` lower bound.
    LengthMin(u32),
    /// `x.length` upper bound.
    LengthMax(u32),
    /// `typeof x` comparison.
    TypeCheck,
    /// Regex `.test(x)` format check.
    Format,
    /// `ENUM.includes(x)` membership check.
    EnumMembership,
    /// A validation error response branch (`res.status(400|422)`).
    ErrorBranch,
}

/// One extracted validation rule with file:line evidence.
#[derive(Debug, Clone, Serialize)]
pub struct ValidationRule {
    pub kind: RuleKind,
    /// Field or variable the rule constrains (short name when known).
    pub subject: String,
    /// Workspace-relative source file.
    pub file: String,
    /// 1-based line of the evidence.
    pub line: usize,
    /// Raw source snippet (bounded) — the "why" behind the rule.
    pub detail: String,
}

/// Scan one file for validation rules. Rules are attributed to endpoints
/// later by matching line spans (`contract::EndpointSpan`); rules outside
/// every handler body keep `detail` context but stay shared helpers.
pub fn scan_file(source: &str, file: &str) -> Result<Vec<ValidationRule>> {
    let tree = parse_js(source)?;
    let bytes = source.as_bytes();
    let mut rules = Vec::new();
    walk(tree.root_node(), bytes, file, &mut rules);
    Ok(rules)
}

fn walk(node: Node, source: &[u8], file: &str, rules: &mut Vec<ValidationRule>) {
    match node.kind() {
        "if_statement" => {
            if let Some(cond) = node.child_by_field_name("condition") {
                classify_condition(cond, source, file, rules);
            }
        }
        "call_expression" => {
            if let Some(rule) = classify_call(node, source, file) {
                rules.push(rule);
            }
        }
        _ => {}
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk(child, source, file, rules);
    }
}

fn classify_condition(cond: Node, source: &[u8], file: &str, rules: &mut Vec<ValidationRule>) {
    let line = cond.start_position().row + 1;
    // The JS grammar wraps if-conditions in a parenthesized_expression;
    // unwrap it (and any nested parens) before classifying.
    let inner = unwrap_parens(cond);
    match inner.kind() {
        "unary_expression" => {
            let op = inner
                .child_by_field_name("operator")
                .map(|o| operator_text(&o, source))
                .unwrap_or_default();
            let arg = inner.child_by_field_name("argument");
            match (op.as_str(), arg) {
                ("!", Some(a)) => rules.push(rule(
                    RuleKind::Required,
                    short_subject(&a, source),
                    file,
                    line,
                    cond,
                    source,
                )),
                ("typeof", Some(a)) => rules.push(rule(
                    RuleKind::TypeCheck,
                    short_subject(&a, source),
                    file,
                    line,
                    cond,
                    source,
                )),
                _ => {}
            }
        }
        "binary_expression" => {
            let left = inner.child_by_field_name("left");
            let right = inner.child_by_field_name("right");
            let op = inner
                .child_by_field_name("operator")
                .map(|o| operator_text(&o, source))
                .unwrap_or_default();
            if let (Some(l), Some(r)) = (left, right) {
                let l_text = node_text(&l, source).unwrap_or_default();
                let r_text = node_text(&r, source).unwrap_or_default();

                // Length bounds: `.length` on one side, a number on the other.
                if l_text.contains(".length") || r_text.contains(".length") {
                    if let Some(n) = numeric_side(&l_text, &r_text) {
                        let bound = length_bound(&op, n);
                        if let Some(kind) = bound {
                            let subject_node = if l_text.contains(".length") { l } else { r };
                            rules.push(rule(
                                kind,
                                short_subject(&subject_node, source),
                                file,
                                line,
                                inner,
                                source,
                            ));
                            return;
                        }
                    }
                }

                // Presence checks against undefined/null.
                if matches!(op.as_str(), "===" | "==" | "!==" | "!=")
                    && (is_nullish(&l_text) || is_nullish(&r_text))
                {
                    let subject_node = if is_nullish(&l_text) { r } else { l };
                    rules.push(rule(
                        RuleKind::Required,
                        short_subject(&subject_node, source),
                        file,
                        line,
                        inner,
                        source,
                    ));
                    return;
                }

                // typeof comparisons.
                if matches!(op.as_str(), "===" | "==" | "!==" | "!=")
                    && (l.kind() == "unary_expression" || r.kind() == "unary_expression")
                {
                    let subject_node = if l.kind() == "unary_expression" { l } else { r };
                    rules.push(rule(
                        RuleKind::TypeCheck,
                        short_subject(&subject_node, source),
                        file,
                        line,
                        inner,
                        source,
                    ));
                }
            }
        }
        "call_expression" => {
            if let Some(r) = classify_call(inner, source, file) {
                rules.push(r);
            }
        }
        _ => {}
    }
}

/// Peel `parenthesized_expression` layers — the JS grammar wraps every
/// if-condition in one, and guards may nest extra parens.
fn unwrap_parens(node: Node) -> Node {
    let mut cur = node;
    while cur.kind() == "parenthesized_expression" {
        let mut cursor = cur.walk();
        match cur.children(&mut cursor).find(|c| c.is_named()) {
            Some(inner) => cur = inner,
            None => break,
        }
    }
    cur
}

fn classify_call(call: Node, source: &[u8], file: &str) -> Option<ValidationRule> {
    let line = call.start_position().row + 1;
    let fn_node = call.child_by_field_name("function")?;
    if fn_node.kind() == "member_expression" {
        let prop = fn_node.child_by_field_name("property")?;
        let prop_text = node_text(&prop, source)?;
        let object = fn_node.child_by_field_name("object")?;

        // /^regex$/.test(x) — format check.
        if prop_text == "test" && object.kind() == "regex" {
            let args = call.child_by_field_name("arguments")?;
            let subject = first_arg_text(&args, source);
            return Some(rule(
                RuleKind::Format,
                subject.map(|s| short_text(&s)).unwrap_or_default(),
                file,
                line,
                call,
                source,
            ));
        }
        // OPTIONS.includes(x) — enum membership.
        if prop_text == "includes" {
            let args = call.child_by_field_name("arguments")?;
            let subject = first_arg_text(&args, source);
            return Some(rule(
                RuleKind::EnumMembership,
                subject.map(|s| short_text(&s)).unwrap_or_default(),
                file,
                line,
                call,
                source,
            ));
        }
        // res.status(400|422) — validation error branch.
        if prop_text == "status" {
            let args = call.child_by_field_name("arguments")?;
            let code = first_arg_text(&args, source).unwrap_or_default();
            if code == "400" || code == "422" {
                return Some(rule(
                    RuleKind::ErrorBranch,
                    format!("status {}", code),
                    file,
                    line,
                    call,
                    source,
                ));
            }
        }
    }
    None
}

fn rule(
    kind: RuleKind,
    subject: String,
    file: &str,
    line: usize,
    evidence: Node,
    source: &[u8],
) -> ValidationRule {
    ValidationRule {
        kind,
        subject,
        file: file.to_string(),
        line,
        detail: short_text(&node_text(&evidence, source).unwrap_or_default()),
    }
}

/// Shorten a subject expression: `req.body.password` → `password`,
/// `x.length` → `x`; anything longer than a bound stays trimmed raw text.
fn short_subject(node: &Node, source: &[u8]) -> String {
    let text = node_text(node, source).unwrap_or_default();
    for prefix in [
        "req.body.",
        "req.query.",
        "req.params.",
        "body.",
        "query.",
        "params.",
    ] {
        if let Some(rest) = text.strip_prefix(prefix) {
            return rest.split('.').next().unwrap_or(rest).to_string();
        }
    }
    if let Some(rest) = text.strip_suffix(".length") {
        return short_text(rest);
    }
    short_text(&text)
}

fn short_text(text: &str) -> String {
    let one_line = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut out: String = one_line.chars().take(60).collect();
    if one_line.chars().count() > 60 {
        out.push('…');
    }
    out
}

fn numeric_side(l: &str, r: &str) -> Option<u32> {
    // The number must sit on the OPPOSITE side of the `.length` operand.
    if let Ok(n) = l.trim().parse::<u32>() {
        if r.contains(".length") {
            return Some(n);
        }
    }
    if let Ok(n) = r.trim().parse::<u32>() {
        if l.contains(".length") {
            return Some(n);
        }
    }
    None
}

/// Guard-clause convention: the if-condition holds the REJECTION predicate.
/// `if (x.length < 3) reject` ⇒ valid length starts at 3 (LengthMin(3));
/// `>` / `>=` cap the max; `===` pins an exact length (min side recorded).
fn length_bound(op: &str, n: u32) -> Option<RuleKind> {
    match op {
        "<" => Some(RuleKind::LengthMin(n)),
        "<=" => Some(RuleKind::LengthMin(n.saturating_add(1))),
        ">" => Some(RuleKind::LengthMax(n)),
        ">=" => Some(RuleKind::LengthMax(n.saturating_sub(1))),
        "===" | "==" => Some(RuleKind::LengthMin(n)),
        _ => None,
    }
}

fn is_nullish(text: &str) -> bool {
    let t = text.trim();
    t == "undefined" || t == "null"
}

fn operator_text(node: &Node, source: &[u8]) -> String {
    let mut cursor = node.walk();
    node.children(&mut cursor)
        .find(|c| !c.is_named())
        .and_then(|c| node_text(&c, source))
        .or_else(|| node_text(node, source))
        .unwrap_or_default()
}

fn first_arg_text(args: &Node, source: &[u8]) -> Option<String> {
    let mut cursor = args.walk();
    args.children(&mut cursor)
        .filter(|c| c.is_named())
        .find(|c| c.kind() != "comment")
        .and_then(|c| node_text(&c, source))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules_of(source: &str) -> Vec<ValidationRule> {
        scan_file(source, "backend/src/app.js").unwrap()
    }

    #[test]
    fn extracts_required_length_type_format_enum_branch_rules() {
        let source = r#"
app.post('/api/register', (req, res) => {
  const { username, password, role } = req.body;
  if (!username) return res.status(400).json({});
  if (password === undefined) return res.status(400).json({});
  if (typeof role !== 'string') return res.status(400).json({});
  if (username.length < 3) return res.status(400).json({});
  if (username.length > 32) return res.status(400).json({});
  if (!/^[a-z0-9-]+$/.test(username)) return res.status(400).json({});
  if (!ROLES.includes(role)) return res.status(400).json({});
});
"#;
        let rules = rules_of(source);
        let kinds: Vec<&RuleKind> = rules.iter().map(|r| &r.kind).collect();
        assert!(kinds.contains(&&RuleKind::Required), "got {kinds:?}");
        assert!(kinds.contains(&&RuleKind::TypeCheck));
        assert!(kinds.contains(&&RuleKind::Format));
        assert!(kinds.contains(&&RuleKind::EnumMembership));
        assert!(
            kinds
                .iter()
                .filter(|k| **k == &RuleKind::ErrorBranch)
                .count()
                >= 7
        );

        // Length bounds from the two comparisons.
        assert!(kinds.contains(&&RuleKind::LengthMin(3)), "got {kinds:?}");
        assert!(kinds.contains(&&RuleKind::LengthMax(32)), "got {kinds:?}");

        // Subjects are shortened to field names and carry line evidence.
        let required = rules.iter().find(|r| r.kind == RuleKind::Required).unwrap();
        assert_eq!(required.subject, "username");
        assert!(required.line > 2);
        assert_eq!(required.file, "backend/src/app.js");
        assert!(!required.detail.is_empty(), "evidence snippet present");
    }

    #[test]
    fn length_bounds_follow_the_guard_clause_convention() {
        // The if-condition is the rejection predicate: `x.length < 3` in a
        // guard means valid lengths START at 3.
        let source = r#"
if (code.length === 6) fail();
if (name.length >= 2) fail();
if (name.length <= 100) fail();
if (nick.length < 3) fail();
if (nick.length > 32) fail();
"#;
        let rules = rules_of(source);
        let kinds: Vec<&RuleKind> = rules.iter().map(|r| &r.kind).collect();
        assert!(
            kinds.contains(&&RuleKind::LengthMin(6)),
            "exact pin: {kinds:?}"
        );
        assert!(
            kinds.contains(&&RuleKind::LengthMax(1)),
            ">= 2 rejected → max 1: {kinds:?}"
        );
        assert!(
            kinds.contains(&&RuleKind::LengthMin(101)),
            "<= 100 rejected → min 101: {kinds:?}"
        );
        assert!(kinds.contains(&&RuleKind::LengthMin(3)));
        assert!(kinds.contains(&&RuleKind::LengthMax(32)));
    }

    #[test]
    fn no_rules_from_plain_logic() {
        let source = r#"
app.get('/api/x', (req, res) => {
  const total = a + b * 3;
  res.json({ total });
});
"#;
        let rules = rules_of(source);
        assert!(
            rules.iter().all(|r| r.kind == RuleKind::ErrorBranch),
            "plain arithmetic yields no validation rules: {rules:?}"
        );
    }

    #[test]
    fn rules_carry_line_numbers_into_deep_bodies() {
        let source = "app.post('/x', (req, res) => {\n  if (!req.body.name) {\n    return res.status(400).json({});\n  }\n});";
        let rules = rules_of(source);
        let required = rules.iter().find(|r| r.kind == RuleKind::Required).unwrap();
        assert_eq!(required.line, 2);
        assert_eq!(required.subject, "name");
    }
}
