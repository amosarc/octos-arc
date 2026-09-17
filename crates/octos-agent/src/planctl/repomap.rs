//! aider-style repository map (ast).
//!
//! A compact, rank-ordered map of the workspace the model reads INSTEAD
//! of walking files itself: domain overlays first (routes/testids/pages —
//! the scoring-critical signals), then definition lines of the files that
//! matter most, highest PageRank first.
//!
//! Pipeline (aider `repomap.py` transplanted to a 10–40 file workspace,
//! no SQLite and no networkx):
//! 1. **Tags** — [`extract_file_tags`] walks the JS/TS/TSX grammars for
//!    definitions (function/method/class declarations, arrow functions
//!    bound by const/object-pair/assignment) and references (call/new
//!    identifiers) — the same node sets as aider's javascript-tags.scm,
//!    collected with plain walks (the crate idiom; tree-sitter 0.24's
//!    query API adds no value at this file count).
//! 2. **Reference graph** — files are nodes, cross-file identifier
//!    references are edges; `weight = mul × sqrt(ref_count)` where `mul`
//!    discounts generic idents (short / `_`-prefixed / defined in >5
//!    files) and boosts spec-signal-hit targets ×10.
//! 3. **Personalized PageRank** — teleport mass on spec-hit files and
//!    recently modified files (aider's chat_files, transplanted).
//! 4. **Greedy budget** — overlays first, then (file, ident) rows in
//!    rank order until the token budget; render is byte-stable.

use std::collections::HashMap;
use std::collections::HashSet;

use super::{DomainOverlays, Tag, TagKind, render::ChangeNote};
use octos_llm::context::estimate_tokens;

/// Map budget defaults; render_map stops early anyway.
const MAX_MAP_LINES: usize = 40;
const MAX_SOURCE_LINE_CHARS: usize = 100;
/// `mul` discount: idents defined in more than this many files are generic.
const GENERIC_DEFINERS_CAP: usize = 5;
const SPEC_TARGET_MUL: f64 = 10.0;
const GENERIC_IDENT_MUL: f64 = 0.1;

/// Extract definition and reference tags from one JS-family source file.
/// `file` is only used for extension-based grammar selection. `None` for
/// unsupported extensions or files that fail to parse (honest skip —
/// never guess).
pub fn extract_file_tags(source: &str, file: &str) -> Option<Vec<Tag>> {
    let ext = file.rsplit('.').next()?;
    let language = match ext {
        "ts" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT,
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX,
        "js" | "jsx" | "mjs" | "cjs" => tree_sitter_javascript::LANGUAGE,
        _ => return None,
    };
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&language.into()).ok()?;
    let tree = parser.parse(source, None)?;
    if tree.root_node().has_error() {
        return None;
    }
    let bytes = source.as_bytes();
    let mut tags: Vec<Tag> = Vec::new();
    collect_tags(tree.root_node(), bytes, &mut tags);
    // Deterministic: document order already, but dedupe exact repeats.
    let mut seen = HashSet::new();
    tags.retain(|t| seen.insert((t.name.clone(), t.line, t.kind)));
    Some(tags)
}

fn collect_tags(node: tree_sitter::Node, bytes: &[u8], out: &mut Vec<Tag>) {
    let line = node.start_position().row + 1;
    match node.kind() {
        "function_declaration"
        | "generator_function_declaration"
        | "class_declaration"
        | "class" => {
            if let Some(name) = child_field_text(node, "name", bytes) {
                out.push(Tag {
                    name,
                    line,
                    kind: TagKind::Def,
                });
            }
        }
        "method_definition" => {
            if let Some(name) = child_field_text(node, "name", bytes) {
                out.push(Tag {
                    name,
                    line,
                    kind: TagKind::Def,
                });
            }
        }
        "variable_declarator" => {
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
                if let Some(name) = child_field_text(node, "name", bytes) {
                    out.push(Tag {
                        name,
                        line,
                        kind: TagKind::Def,
                    });
                }
            }
        }
        "pair" => {
            // `key: () => …` object method shorthand value
            let value = node.child_by_field_name("value");
            let is_fn = value
                .map(|v| {
                    matches!(
                        v.kind(),
                        "arrow_function" | "function_expression" | "function"
                    )
                })
                .unwrap_or(false);
            if is_fn {
                if let Some(name) = child_field_text(node, "key", bytes) {
                    out.push(Tag {
                        name,
                        line,
                        kind: TagKind::Def,
                    });
                }
            }
        }
        "assignment_expression" => {
            let right = node.child_by_field_name("right");
            let is_fn = right
                .map(|v| {
                    matches!(
                        v.kind(),
                        "arrow_function" | "function_expression" | "function"
                    )
                })
                .unwrap_or(false);
            if is_fn {
                if let Some(name) = child_field_text(node, "left", bytes) {
                    out.push(Tag {
                        name,
                        line,
                        kind: TagKind::Def,
                    });
                }
            }
        }
        "call_expression" => {
            if let Some(fn_node) = node.child_by_field_name("function") {
                if fn_node.kind() == "identifier" {
                    if let Some(name) = node_text(fn_node, bytes) {
                        out.push(Tag {
                            name,
                            line,
                            kind: TagKind::Ref,
                        });
                    }
                }
            }
        }
        "new_expression" => {
            if let Some(ctor) = node.child_by_field_name("constructor") {
                if ctor.kind() == "identifier" {
                    if let Some(name) = node_text(ctor, bytes) {
                        out.push(Tag {
                            name,
                            line,
                            kind: TagKind::Ref,
                        });
                    }
                }
            }
        }
        _ => {}
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_tags(child, bytes, out);
    }
}

/// Directed file-graph edge: `from` references idents defined in `to`.
#[derive(Debug, Clone, PartialEq)]
pub struct Edge {
    pub from: String,
    pub to: String,
    pub weight: f64,
}

/// Personalized PageRank over the file graph. `teleport` maps file →
/// teleport weight (need not sum to 1; normalized). Dangling mass (files
/// without out-edges) is redistributed proportionally to teleport. Pure
/// power iteration: `iterations` steps, damping 0.85, deterministic.
pub fn pagerank(
    nodes: &[String],
    edges: &[Edge],
    teleport: &HashMap<String, f64>,
    damping: f64,
    iterations: usize,
) -> Vec<f64> {
    let n = nodes.len();
    if n == 0 {
        return Vec::new();
    }
    let index: HashMap<&str, usize> = nodes
        .iter()
        .enumerate()
        .map(|(i, f)| (f.as_str(), i))
        .collect();
    let tele: Vec<f64> = {
        let raw: Vec<f64> = nodes
            .iter()
            .map(|f| teleport.get(f).copied().unwrap_or(0.0))
            .collect();
        let sum: f64 = raw.iter().sum();
        if sum > 0.0 {
            raw.iter().map(|w| w / sum).collect()
        } else {
            vec![1.0 / n as f64; n]
        }
    };

    // Aggregate out-weights per source.
    let mut out_total = vec![0.0f64; n];
    let mut out_edges: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n];
    for e in edges {
        let (Some(a), Some(b)) = (index.get(e.from.as_str()), index.get(e.to.as_str())) else {
            continue;
        };
        let (a, b) = (*a, *b);
        if a == b {
            continue;
        }
        out_total[a] += e.weight;
        out_edges[a].push((b, e.weight));
    }

    let mut rank = vec![1.0 / n as f64; n];
    for _ in 0..iterations {
        // Dangling mass redistributed ∝ teleport.
        let dangling: f64 = (0..n)
            .filter(|&i| out_total[i] <= 0.0)
            .map(|i| rank[i])
            .sum();
        let mut next = vec![0.0f64; n];
        for i in 0..n {
            next[i] = (1.0 - damping) * tele[i];
            // Dangling mass redistributes proportionally to teleport.
            next[i] += damping * dangling * tele[i];
        }
        for (a, outs) in out_edges.iter().enumerate() {
            if out_total[a] <= 0.0 {
                continue;
            }
            let share = rank[a] / out_total[a];
            for &(b, w) in outs {
                next[b] += damping * share * w;
            }
        }
        rank = next;
    }
    rank
}

/// Assemble edge weights from per-file tags. `spec_hit_files` are files
/// whose literal signals matched spec requirements (their incoming edges
/// are boosted); `recent_files` mirrors aider's chat-file boost on the
/// teleport side, not edges.
pub fn build_edges(
    tags_by_file: &HashMap<String, Vec<Tag>>,
    spec_hit_files: &HashSet<String>,
) -> Vec<Edge> {
    // def index: ident → def files (for generic-name detection).
    let mut def_counts: HashMap<&str, HashSet<&str>> = HashMap::new();
    for (file, tags) in tags_by_file {
        for t in tags {
            if t.kind == TagKind::Def {
                def_counts.entry(t.name.as_str()).or_default().insert(file);
            }
        }
    }

    // Raw ref counts per (from, to, ident).
    let mut refs: HashMap<(String, String, String), usize> = HashMap::new();
    for (from, tags) in tags_by_file {
        for t in tags {
            if t.kind != TagKind::Ref {
                continue;
            }
            for (to, defs) in tags_by_file.iter() {
                if to == from {
                    continue;
                }
                if defs
                    .iter()
                    .any(|d| d.kind == TagKind::Def && d.name == t.name)
                {
                    *refs
                        .entry((from.clone(), to.clone(), t.name.clone()))
                        .or_insert(0) += 1;
                }
            }
        }
    }

    // Aggregate per (from, to) with multipliers.
    let mut acc: HashMap<(String, String), f64> = HashMap::new();
    for ((from, to, ident), count) in refs {
        let mut mul = 1.0f64;
        if def_counts
            .get(ident.as_str())
            .is_some_and(|fs| fs.len() > GENERIC_DEFINERS_CAP)
        {
            mul *= GENERIC_IDENT_MUL;
        }
        if ident.len() < 8 || ident.starts_with('_') {
            mul *= GENERIC_IDENT_MUL;
        }
        if spec_hit_files.contains(&to) {
            mul *= SPEC_TARGET_MUL;
        }
        *acc.entry((from, to)).or_insert(0.0) += mul * (count as f64).sqrt();
    }
    let mut edges: Vec<Edge> = acc
        .into_iter()
        .map(|((from, to), weight)| Edge { from, to, weight })
        .collect();
    edges.sort_by(|a, b| (&a.from, &a.to).cmp(&(&b.from, &b.to)));
    edges
}

/// One ranked (file, ident) row candidate.
#[derive(Debug, Clone, PartialEq)]
pub struct RankRow {
    pub file: String,
    pub ident: String,
    pub line: usize,
    pub score: f64,
}

/// Rank (file, ident) definition rows. Ranking granularity is
/// file-level (aider's per-ident share is negligible at 10-40 files);
/// rows within a file follow line order.
pub fn rank_rows(
    tags_by_file: &HashMap<String, Vec<Tag>>,
    edges: &[Edge],
    rank_by_file: &HashMap<String, f64>,
) -> Vec<RankRow> {
    let mut in_total: HashMap<String, f64> = HashMap::new();
    for e in edges {
        *in_total.entry(e.to.clone()).or_insert(0.0) += e.weight;
    }
    for e in edges {
        let _ = rank_by_file.get(&e.to);
        let _ = in_total.get(&e.to);
    }
    // File-level ranking, rows = defs per file, ordered by (file rank
    // desc, line asc). The per-ident share above is folded into file
    // rank — at 10-40 files the granularity win of per-ident shares is
    // negligible next to the overlay layer.
    let mut rows: Vec<RankRow> = Vec::new();
    for (file, tags) in tags_by_file {
        let rank = rank_by_file.get(file).copied().unwrap_or(0.0);
        for t in tags {
            if t.kind == TagKind::Def {
                rows.push(RankRow {
                    file: file.clone(),
                    ident: t.name.clone(),
                    line: t.line,
                    score: rank,
                });
            }
        }
    }
    rows.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.file.cmp(&b.file))
            .then_with(|| a.line.cmp(&b.line))
    });
    rows
}

/// Map render input.
pub struct MapInput<'a> {
    pub tags_by_file: &'a HashMap<String, Vec<Tag>>,
    pub rank_by_file: &'a HashMap<String, f64>,
    pub overlays: &'a DomainOverlays,
    /// Workspace file rel paths → first source lines for rendering.
    pub source_lines: &'a HashMap<String, Vec<String>>,
    /// Recently edited files (most recent first). Each gets reserved leading
    /// rows before the rank fill — the map must describe what the agent is
    /// actively changing even when spec-hit files outrank it (tok-ticket-1:
    /// the map never mentioned the frontend the model was editing).
    pub recent_files: &'a [String],
    pub budget_tokens: u32,
}

/// Reserved leading rows for recently edited files.
const RECENT_FILE_CAP: usize = 4;
const RECENT_ROWS_PER_FILE: usize = 2;

/// Render the map: overlay lines first, then `path:` groups with
/// `line: source` rows in rank order. Byte-stable for identical input.
/// Empty tags → empty string (render.rs shows "not scaffolded yet").
pub fn render_map(input: &MapInput) -> String {
    let mut out = String::new();
    let mut lines_used = 0usize;
    let mut rendered_files: HashSet<String> = HashSet::new();

    let mut overlay_rows: Vec<String> = Vec::new();
    for r in input.overlays.routes.iter().take(8) {
        overlay_rows.push(format!("route {r}"));
    }
    for t in input.overlays.testids.iter().take(8) {
        overlay_rows.push(format!("testid {t}"));
    }
    for p in input.overlays.pages.iter().take(8) {
        overlay_rows.push(format!("page {p}"));
    }
    if !overlay_rows.is_empty() {
        out.push_str(overlay_rows.join("\n").as_str());
        out.push('\n');
        lines_used += overlay_rows.len();
    }

    // Reserved rows for recently edited files: newest first, a couple of
    // definition lines each, ahead of rank competition.
    for file in input.recent_files.iter().take(RECENT_FILE_CAP) {
        if lines_used >= MAX_MAP_LINES || estimate_tokens(&out) > input.budget_tokens {
            break;
        }
        let tags = match input.tags_by_file.get(file) {
            Some(tags) if !tags.is_empty() => tags,
            _ => continue,
        };
        let source_line = |line: usize| {
            input
                .source_lines
                .get(file)
                .and_then(|lines| lines.get(line.saturating_sub(1)))
                .map(|l| truncate(l, MAX_SOURCE_LINE_CHARS))
                .unwrap_or_default()
        };
        out.push_str(&format!("{file}:\n"));
        lines_used += 1;
        rendered_files.insert(file.clone());
        for tag in tags.iter().take(RECENT_ROWS_PER_FILE) {
            if lines_used >= MAX_MAP_LINES {
                break;
            }
            out.push_str(&format!("  {}: {}\n", tag.line, source_line(tag.line)));
            lines_used += 1;
        }
    }

    let rows = rank_rows(input.tags_by_file, &[], input.rank_by_file);
    let mut current_file: Option<String> = None;
    for row in rows {
        if lines_used >= MAX_MAP_LINES || estimate_tokens(&out) > input.budget_tokens {
            break;
        }
        if rendered_files.contains(&row.file) {
            continue;
        }
        if current_file.as_deref() != Some(row.file.as_str()) {
            out.push_str(&format!("{}:\n", row.file));
            current_file = Some(row.file.clone());
            lines_used += 1;
        }
        let source_line = input
            .source_lines
            .get(&row.file)
            .and_then(|lines| lines.get(row.line.saturating_sub(1)))
            .map(|l| truncate(l, MAX_SOURCE_LINE_CHARS))
            .unwrap_or_default();
        out.push_str(&format!("  {}: {}\n", row.line, source_line));
        lines_used += 1;
    }
    out
}

/// Convenience: build a change note (shared by scan's localizer).
pub fn note(file: &str, symbol: &str, line: usize) -> ChangeNote {
    ChangeNote {
        file: file.to_string(),
        symbol: Some(symbol.to_string()),
        line: Some(line),
    }
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.trim_end().to_string();
    }
    let cut: String = s.chars().take(max).collect();
    format!("{cut}…").trim_end().to_string()
}

fn child_field_text(node: tree_sitter::Node, field: &str, bytes: &[u8]) -> Option<String> {
    node.child_by_field_name(field)
        .and_then(|n| node_text(n, bytes))
}

fn node_text(node: tree_sitter::Node, bytes: &[u8]) -> Option<String> {
    node.utf8_text(bytes).ok().map(String::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tags_extract_defs_and_refs_from_js() {
        let source = r#"
const app = () => {};
function register() {}
class Router {
  handle() {}
}
const obj = { submit: () => {} };
let assigned;
assigned = () => {};
const other = 42;
register();
new Router();
"#;
        let tags = extract_file_tags(source, "src/app.js").expect("js tags");
        let defs: Vec<(&str, usize)> = tags
            .iter()
            .filter(|t| t.kind == TagKind::Def)
            .map(|t| (t.name.as_str(), t.line))
            .collect();
        for name in ["app", "register", "Router", "handle", "submit", "assigned"] {
            assert!(
                defs.iter().any(|(n, _)| *n == name),
                "def {name} in {defs:?}"
            );
        }
        assert!(
            !defs.iter().any(|(n, _)| *n == "other"),
            "non-fn const is not a def"
        );
        let refs: Vec<&str> = tags
            .iter()
            .filter(|t| t.kind == TagKind::Ref)
            .map(|t| t.name.as_str())
            .collect();
        assert!(refs.contains(&"register"), "call ref: {refs:?}");
        assert!(refs.contains(&"Router"), "new ref: {refs:?}");
    }

    #[test]
    fn tsx_and_ts_files_use_their_grammars() {
        let tsx = "export const X = () => <div data-testid=\"a\" />;\n";
        assert!(extract_file_tags(tsx, "src/X.tsx").is_some());
        let ts = "export const y: number = 1;\nfunction f(): void {}\n";
        let tags = extract_file_tags(ts, "src/y.ts").expect("ts tags");
        assert!(tags.iter().any(|t| t.name == "f" && t.kind == TagKind::Def));
        // Unsupported extension → None.
        assert!(extract_file_tags("whatever", "x.py").is_none());
        // Broken source → None (honest skip).
        assert!(extract_file_tags("function (((", "x.js").is_none());
    }

    #[test]
    fn edges_weight_rules_apply() {
        let mut tags_by_file: HashMap<String, Vec<Tag>> = HashMap::new();
        tags_by_file.insert(
            "spec/a.spec.ts".into(),
            vec![
                Tag {
                    name: "makePayment".into(),
                    line: 1,
                    kind: TagKind::Ref,
                },
                Tag {
                    name: "makePayment".into(),
                    line: 2,
                    kind: TagKind::Ref,
                },
                Tag {
                    name: "_tmp".into(),
                    line: 3,
                    kind: TagKind::Ref,
                },
            ],
        );
        tags_by_file.insert(
            "backend/pay.js".into(),
            vec![
                Tag {
                    name: "makePayment".into(),
                    line: 1,
                    kind: TagKind::Def,
                },
                Tag {
                    name: "_tmp".into(),
                    line: 2,
                    kind: TagKind::Def,
                },
            ],
        );
        tags_by_file.insert(
            "backend/other.js".into(),
            vec![Tag {
                name: "makePayment".into(),
                line: 1,
                kind: TagKind::Def,
            }],
        );

        let mut spec_hits = HashSet::new();
        spec_hits.insert("backend/pay.js".to_string());
        let edges = build_edges(&tags_by_file, &spec_hits);

        let pay = edges
            .iter()
            .find(|e| e.from == "spec/a.spec.ts" && e.to == "backend/pay.js")
            .unwrap();
        let other = edges
            .iter()
            .find(|e| e.from == "spec/a.spec.ts" && e.to == "backend/other.js")
            .unwrap();
        // pay: refs(makePayment ×2, _tmp ×1) → sqrt(2)*1 + sqrt(1)*0.1,
        // boosted ×10 (spec target). other: sqrt(2)*1, no boost.
        let expected_pay = 10.0 * (2.0f64.sqrt() + GENERIC_IDENT_MUL * 1.0f64.sqrt());
        let expected_other = 2.0f64.sqrt();
        assert!(
            (pay.weight - expected_pay).abs() < 1e-9,
            "pay {}",
            pay.weight
        );
        assert!(
            (other.weight - expected_other).abs() < 1e-9,
            "other {}",
            other.weight
        );
        assert!(pay.weight > other.weight);
    }

    #[test]
    fn pagerank_two_node_first_iteration_is_pinned() {
        // 2 nodes, one edge A→B, uniform teleport, d=0.85, uniform start.
        // Hand-computed first iteration: A=0.2875, B=0.7125 (dangling B
        // mass redistributed ∝ teleport).
        let nodes = vec!["A".to_string(), "B".to_string()];
        let edges = vec![Edge {
            from: "A".into(),
            to: "B".into(),
            weight: 1.0,
        }];
        let tele: HashMap<String, f64> = HashMap::new();
        let rank = pagerank(&nodes, &edges, &tele, 0.85, 1);
        assert!((rank[0] - 0.2875).abs() < 1e-12, "A={}", rank[0]);
        assert!((rank[1] - 0.7125).abs() < 1e-12, "B={}", rank[1]);

        // Many iterations converge to sum 1 with B > A.
        let rank = pagerank(&nodes, &edges, &tele, 0.85, 40);
        let sum: f64 = rank.iter().sum();
        assert!((sum - 1.0).abs() < 1e-9, "sum={sum}");
        assert!(rank[1] > rank[0]);
    }

    #[test]
    fn pagerank_personalization_boosts_teleport_files() {
        // Triangle A→B→C→A plus a weak edge into D; teleport on D.
        let nodes: Vec<String> = ["A", "B", "C", "D"].into_iter().map(String::from).collect();
        let mk = |a: &str, b: &str| Edge {
            from: a.into(),
            to: b.into(),
            weight: 1.0,
        };
        let edges = vec![mk("A", "B"), mk("B", "C"), mk("C", "A"), mk("C", "D")];
        let mut tele: HashMap<String, f64> = HashMap::new();
        tele.insert("D".to_string(), 10.0);
        let rank = pagerank(&nodes, &edges, &tele, 0.85, 40);
        let d = rank[3];
        let avg = 0.25;
        assert!(d > avg, "teleport target D above average: {rank:?}");
    }

    #[test]
    fn rank_rows_order_files_by_rank_then_lines() {
        let mut tags_by_file: HashMap<String, Vec<Tag>> = HashMap::new();
        tags_by_file.insert(
            "a.js".into(),
            vec![
                Tag {
                    name: "one".into(),
                    line: 1,
                    kind: TagKind::Def,
                },
                Tag {
                    name: "two".into(),
                    line: 5,
                    kind: TagKind::Def,
                },
            ],
        );
        tags_by_file.insert(
            "b.js".into(),
            vec![Tag {
                name: "three".into(),
                line: 2,
                kind: TagKind::Def,
            }],
        );
        let mut rank_by_file: HashMap<String, f64> = HashMap::new();
        rank_by_file.insert("a.js".into(), 0.3);
        rank_by_file.insert("b.js".into(), 0.7);
        let rows = rank_rows(&tags_by_file, &[], &rank_by_file);
        assert_eq!(rows[0].file, "b.js", "higher rank first: {rows:?}");
        assert_eq!(rows[1].file, "a.js");
        assert_eq!(rows[1].line, 1, "within a file, line order");
    }

    #[test]
    fn render_map_is_byte_stable_overlays_first_and_budgeted() {
        let mut tags_by_file: HashMap<String, Vec<Tag>> = HashMap::new();
        let source_a: Vec<String> = (1..=6).map(|i| format!("function f{i}() {{}}")).collect();
        tags_by_file.insert(
            "backend/src/app.js".into(),
            vec![
                Tag {
                    name: "f1".into(),
                    line: 1,
                    kind: TagKind::Def,
                },
                Tag {
                    name: "f2".into(),
                    line: 2,
                    kind: TagKind::Def,
                },
                Tag {
                    name: "f3".into(),
                    line: 3,
                    kind: TagKind::Def,
                },
            ],
        );
        let mut rank_by_file: HashMap<String, f64> = HashMap::new();
        rank_by_file.insert("backend/src/app.js".into(), 0.9);
        let overlays = DomainOverlays {
            routes: vec!["/api/register".into()],
            testids: vec!["password-strength".into()],
            pages: vec![],
        };
        let mut source_lines: HashMap<String, Vec<String>> = HashMap::new();
        source_lines.insert("backend/src/app.js".into(), source_a);

        let input = MapInput {
            tags_by_file: &tags_by_file,
            rank_by_file: &rank_by_file,
            overlays: &overlays,
            source_lines: &source_lines,
            recent_files: &[],
            budget_tokens: 450,
        };
        let map1 = render_map(&input);
        let map2 = render_map(&input);
        assert_eq!(map1, map2, "byte-stable");
        assert!(map1.starts_with("route /api/register\ntestid password-strength\n"));
        assert!(map1.contains("backend/src/app.js:"));
        assert!(map1.contains("  1: function f1() {}"));
        assert!(
            estimate_tokens(&map1) <= 450,
            "map within budget: {} tokens",
            estimate_tokens(&map1)
        );

        // Budget squeeze drops trailing symbol rows but keeps overlays.
        let squeezed = MapInput {
            budget_tokens: 30,
            ..input
        };
        let small = render_map(&squeezed);
        assert!(small.contains("route /api/register"));
        assert!(
            estimate_tokens(&small) <= 30 + 20,
            "{}",
            estimate_tokens(&small)
        );
    }

    #[test]
    fn should_reserve_leading_rows_for_recently_edited_files() {
        // A spec-hit host file outranks everything, but the file the agent is
        // actively editing must still appear — right after the overlays,
        // before rank competition eats the budget.
        let mut tags_by_file: HashMap<String, Vec<Tag>> = HashMap::new();
        tags_by_file.insert(
            "backend/src/lib/auth.js".into(),
            (1..=6)
                .map(|i| Tag {
                    name: format!("host_sym_{i}"),
                    line: i,
                    kind: TagKind::Def,
                })
                .collect(),
        );
        tags_by_file.insert(
            "frontend/src/main.jsx".into(),
            (1..=6)
                .map(|i| Tag {
                    name: format!("app_sym_{i}"),
                    line: i,
                    kind: TagKind::Def,
                })
                .collect(),
        );
        let mut rank_by_file: HashMap<String, f64> = HashMap::new();
        rank_by_file.insert("backend/src/lib/auth.js".into(), 10.0);
        rank_by_file.insert("frontend/src/main.jsx".into(), 0.1);
        let source_lines: HashMap<String, Vec<String>> = tags_by_file
            .keys()
            .map(|k| {
                (
                    k.clone(),
                    (1..=6)
                        .map(|i| format!("function sym_{i}() {{}}"))
                        .collect(),
                )
            })
            .collect();
        let input = MapInput {
            tags_by_file: &tags_by_file,
            rank_by_file: &rank_by_file,
            overlays: &DomainOverlays {
                routes: vec!["/api/auth/login".into()],
                testids: vec![],
                pages: vec![],
            },
            source_lines: &source_lines,
            recent_files: &["frontend/src/main.jsx".into()],
            budget_tokens: 800,
        };
        let map = render_map(&input);
        let frontend_pos = map
            .find("frontend/src/main.jsx:")
            .expect("recent file present");
        let backend_pos = map
            .find("backend/src/lib/auth.js:")
            .expect("ranked file present");
        assert!(
            frontend_pos < backend_pos,
            "recently edited file leads over higher-ranked files:\n{map}"
        );
        assert!(
            map.starts_with("route /api/auth/login\n"),
            "overlays still come first:\n{map}"
        );
        // The reserved group is not repeated by the rank fill.
        assert_eq!(
            map.matches("frontend/src/main.jsx:").count(),
            1,
            "recent group must not be duplicated:\n{map}"
        );
    }

    #[test]
    fn wide_symbol_space_degrades_gracefully() {
        let mut tags_by_file: HashMap<String, Vec<Tag>> = HashMap::new();
        for f in 0..30 {
            let name = format!("file{f}.js");
            tags_by_file.insert(
                name.clone(),
                (0..40)
                    .map(|i| Tag {
                        name: format!("sym{f}_{i}"),
                        line: i + 1,
                        kind: TagKind::Def,
                    })
                    .collect(),
            );
        }
        let rank_by_file: HashMap<String, f64> = tags_by_file
            .keys()
            .enumerate()
            .map(|(i, k)| (k.clone(), 1.0 / (i + 1) as f64))
            .collect();
        let overlays = DomainOverlays::default();
        let source_lines: HashMap<String, Vec<String>> = tags_by_file
            .keys()
            .map(|k| {
                (
                    k.clone(),
                    (1..=40)
                        .map(|i| format!("function sym_x_{i}() {{}}"))
                        .collect(),
                )
            })
            .collect();
        let input = MapInput {
            tags_by_file: &tags_by_file,
            rank_by_file: &rank_by_file,
            overlays: &overlays,
            source_lines: &source_lines,
            recent_files: &[],
            budget_tokens: 800,
        };
        let map = render_map(&input);
        assert!(map.lines().count() <= MAX_MAP_LINES + 1, "line cap");
        assert!(estimate_tokens(&map) <= 800, "budget cap");
    }
}
