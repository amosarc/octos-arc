//! Host-side API contract extraction for the testgen pipeline.
//!
//! Parses Express source files (tree-sitter JS grammar) and extracts the
//! structured interface the TestWriter tests against: route registrations
//! (`app.get('/api/x', handler)`), their request fields (`req.body.*`,
//! `req.params.*`, `req.query.*`, destructured equivalents), path
//! parameters, and plain service-function signatures.
//!
//! This is the "入参签名" step of the pipeline: the model receives the
//! finished contract and must not re-read backend source to rediscover
//! interfaces. Extraction is static and best-effort — handlers registered
//! through non-literal paths or imported from unscanned files are skipped
//! honestly (absent from the contract) rather than guessed.

use eyre::Result;
use serde::Serialize;
use tree_sitter::Tree;

/// HTTP verbs we recognize as route registrations.
const ROUTE_VERBS: &[&str] = &[
    "get", "post", "put", "patch", "delete", "all", "head", "options",
];

/// Named-handler registry entry: `(name, decl_line, body_rows, params_row)`.
type NamedFn = (String, Option<usize>, Option<(usize, usize)>, Option<usize>);

/// One request field of an endpoint (body/query member or destructured name).
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ParamField {
    pub name: String,
    /// 1-based line of first use.
    pub line: usize,
    /// `true` when the binding carries a default value — the endpoint
    /// tolerates its absence.
    pub optional: bool,
}

/// A registered route with its statically visible request surface.
#[derive(Debug, Clone, Serialize)]
pub struct EndpointContract {
    /// `"{METHOD} {path}"`, e.g. `"POST /api/register"`.
    pub key: String,
    pub method: String,
    pub path: String,
    /// Workspace-relative source file.
    pub file: String,
    /// 1-based line of the registration call.
    pub handler_line: usize,
    pub path_params: Vec<String>,
    /// `req.body.*` fields and destructured `req.body` names.
    pub body_fields: Vec<ParamField>,
    pub query_fields: Vec<ParamField>,
    /// Handler was a named reference resolved to an in-file function.
    pub handler_ref: Option<String>,
}

/// A plain (non-route) function signature in a scanned file.
#[derive(Debug, Clone, Serialize)]
pub struct FnSignature {
    pub name: String,
    pub file: String,
    pub line: usize,
    pub params: Vec<ParamField>,
}

/// 0-based row span of a handler body, used to attribute validation rules
/// found in the same file to the endpoint that owns them.
#[derive(Debug, Clone, Copy)]
pub struct EndpointSpan {
    pub endpoint_index: usize,
    pub start_row: usize,
    pub end_row: usize,
}

/// Extraction result for one source file.
#[derive(Debug, Default)]
pub struct FileContracts {
    pub endpoints: Vec<EndpointContract>,
    pub functions: Vec<FnSignature>,
    pub spans: Vec<EndpointSpan>,
}

/// Parse JS/JSX source into a tree. The testgen pipeline only ever feeds it
/// JavaScript-family files; other languages are rejected upstream by the
/// profile.
pub fn parse_js(source: &str) -> Result<Tree> {
    let mut parser = tree_sitter::Parser::new();
    parser
        .set_language(&tree_sitter_javascript::LANGUAGE.into())
        .map_err(|e| eyre::eyre!("failed to load JS grammar: {e}"))?;
    parser
        .parse(source, None)
        .ok_or_else(|| eyre::eyre!("failed to parse source"))
}

/// Extract endpoint contracts and function signatures from one file.
pub fn extract_file(source: &str, file: &str) -> Result<FileContracts> {
    let tree = parse_js(source)?;
    let bytes = source.as_bytes();
    let mut out = FileContracts::default();

    // Named handler registry for `app.get('/x', handlerName)` resolution.
    let named_fns = collect_named_functions(&tree, bytes);

    walk_nodes(
        tree.root_node(),
        tree.root_node(),
        bytes,
        file,
        &named_fns,
        &mut out,
    );
    Ok(out)
}

/// Document-order recursive walk. Route calls stop their subtree descent so
/// nested registrations inside a handler body are not double-counted.
#[allow(clippy::too_many_arguments)]
fn walk_nodes(
    node: tree_sitter::Node,
    root: tree_sitter::Node,
    source: &[u8],
    file: &str,
    named_fns: &[NamedFn],
    out: &mut FileContracts,
) {
    if node.kind() == "call_expression" {
        if let Some((span, ep)) = try_route_call(node, root, source, file, named_fns) {
            let idx = out.endpoints.len();
            if let Some((start_row, end_row)) = span {
                out.spans.push(EndpointSpan {
                    endpoint_index: idx,
                    start_row,
                    end_row,
                });
            }
            out.endpoints.push(ep);
            return;
        }
    } else if node.kind() == "function_declaration" {
        if let Some(f) = function_signature(node, source, file) {
            out.functions.push(f);
        }
    } else if matches!(node.kind(), "lexical_declaration" | "variable_declaration") {
        for f in arrow_signatures(node, source, file) {
            out.functions.push(f);
        }
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk_nodes(child, root, source, file, named_fns, out);
    }
}

type ExtractedEndpoint = (Option<(usize, usize)>, EndpointContract);

/// Recognize `VERB_OBJECT.METHOD('path', ..., handler)` call expressions.
fn try_route_call(
    call: tree_sitter::Node,
    root: tree_sitter::Node,
    source: &[u8],
    file: &str,
    named_fns: &[NamedFn],
) -> Option<ExtractedEndpoint> {
    let fn_node = call.child_by_field_name("function")?;
    let (verb, _) = match fn_node.kind() {
        "member_expression" => {
            let prop = fn_node.child_by_field_name("property")?;
            (
                node_text(&prop, source)?,
                fn_node.child_by_field_name("object"),
            )
        }
        "computed_member_expression" => {
            let prop = fn_node.child_by_field_name("property")?;
            let raw = node_text(&prop, source)?;
            let prop_text = raw.trim_matches(|c| c == '\'' || c == '"').to_string();
            (prop_text, fn_node.child_by_field_name("object"))
        }
        _ => return None,
    };
    if !ROUTE_VERBS.contains(&verb.as_str()) {
        return None;
    }

    let args = call.child_by_field_name("arguments")?;
    let positional: Vec<tree_sitter::Node> = {
        let mut cursor = args.walk();
        args.children(&mut cursor)
            .filter(|c| c.is_named())
            .collect()
    };
    if positional.is_empty() {
        return None;
    }
    // First argument must be a literal route path we can resolve.
    let path_node = positional[0];
    let path = match path_node.kind() {
        "string" => child_text(&path_node, "value", source)
            .or_else(|| first_string_fragment(&path_node, source))?,
        _ => return None,
    };
    let handler = *positional.last()?;

    let mut span = None;
    let mut body_fields = Vec::new();
    let mut query_fields = Vec::new();
    let mut handler_ref = None;
    let mut handler_line = path_node.start_position().row + 1;

    match handler.kind() {
        "arrow_function" | "function_expression" | "function" => {
            let params = handler.child_by_field_name("parameters");
            let alias = params
                .and_then(|p| first_param_identifier(&p, source))
                .unwrap_or_else(|| "req".to_string());
            if let Some(body) = handler.child_by_field_name("body") {
                span = Some((body.start_position().row, body.end_position().row));
                let (b, q) = collect_request_fields(body, source, &alias);
                body_fields = b;
                query_fields = q;
            }
            handler_line = handler.start_position().row + 1;
        }
        "identifier" => {
            let name = node_text(&handler, source)?;
            handler_ref = Some(name.clone());
            if let Some((_, line, body_rows, param_rows)) =
                named_fns.iter().find(|(n, _, _, _)| n == &name).cloned()
            {
                let alias = param_rows
                    .and_then(|row| source_rows_identifier(source, &row))
                    .unwrap_or_else(|| "req".to_string());
                if let Some((start, end)) = body_rows {
                    span = Some((start, end));
                    // The named handler body lives outside the call
                    // subtree — resolve it against the tree root.
                    if let Some(body_node) = node_at_rows(root, start, end) {
                        let (b, q) = collect_request_fields(body_node, source, &alias);
                        body_fields = b;
                        query_fields = q;
                    }
                }
                if let Some(l) = line {
                    handler_line = l;
                }
            }
        }
        _ => return None,
    }

    let key = format!("{} {}", verb.to_uppercase(), path);
    let contract = EndpointContract {
        key: key.clone(),
        method: verb.to_uppercase(),
        path,
        file: file.to_string(),
        handler_line,
        path_params: extract_path_params(&key),
        body_fields,
        query_fields,
        handler_ref,
    };
    Some((span, contract))
}

/// `'/api/users/:id'` → `["id"]`.
fn extract_path_params(key: &str) -> Vec<String> {
    let path = key.split_once(' ').map(|(_, p)| p).unwrap_or("");
    path.split('/')
        .filter_map(|seg| seg.strip_prefix(':').map(String::from))
        .collect()
}

/// Collect `alias.body.X` / `alias.query.X` member chains and destructured
/// `const { a, b } = alias.body` names within a handler body.
fn collect_request_fields(
    body: tree_sitter::Node,
    source: &[u8],
    alias: &str,
) -> (Vec<ParamField>, Vec<ParamField>) {
    let mut body_fields: Vec<ParamField> = Vec::new();
    let mut query_fields: Vec<ParamField> = Vec::new();
    let mut stack = vec![body];
    while let Some(node) = stack.pop() {
        match node.kind() {
            "member_expression" => {
                if let Some((kind, field)) = request_member_chain(node, source, alias) {
                    let line = node.start_position().row + 1;
                    push_field(
                        if kind == "body" {
                            &mut body_fields
                        } else {
                            &mut query_fields
                        },
                        field,
                        line,
                        false,
                    );
                }
            }
            "variable_declarator" => {
                // const { a, b = 1 } = req.body  /  const { q } = req.query
                let value = node.child_by_field_name("value");
                let name = node.child_by_field_name("name");
                if let (Some(v), Some(n)) = (value, name) {
                    if n.kind() == "object_pattern" {
                        if let Some(kind) = request_container_chain(v, source, alias) {
                            let fields = if kind == "body" {
                                &mut body_fields
                            } else {
                                &mut query_fields
                            };
                            let mut cursor = n.walk();
                            for child in n.children(&mut cursor).filter(|c| c.is_named()) {
                                let text = node_text(&child, source).unwrap_or_default();
                                let base = text.split('=').next().unwrap_or(&text);
                                let field =
                                    base.split(':').next().unwrap_or(base).trim().to_string();
                                if field.is_empty()
                                    || !field
                                        .chars()
                                        .next()
                                        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
                                {
                                    continue;
                                }
                                push_field(
                                    fields,
                                    field,
                                    child.start_position().row + 1,
                                    text.contains('='),
                                );
                            }
                        }
                    }
                }
            }
            _ => {}
        }
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            stack.push(child);
        }
    }
    // Document order for deterministic output (stack DFS visits siblings
    // in reverse).
    body_fields.sort_by_key(|f| f.line);
    query_fields.sort_by_key(|f| f.line);
    (body_fields, query_fields)
}

/// Match the two-level container `alias.body` / `alias.query` — the value
/// side of `const { a } = req.body` destructuring.
fn request_container_chain(
    node: tree_sitter::Node,
    source: &[u8],
    alias: &str,
) -> Option<&'static str> {
    let prop = node.child_by_field_name("property")?;
    let object = node.child_by_field_name("object")?;
    let inner = node_text(&prop, source)?;
    let root_name = node_text(&object, source)?;
    if root_name != alias {
        return None;
    }
    match inner.as_str() {
        "body" => Some("body"),
        "query" => Some("query"),
        _ => None,
    }
}

/// Match `alias.{body|query}.FIELD` chains. Returns `("body"|"query", field)`.
fn request_member_chain(
    node: tree_sitter::Node,
    source: &[u8],
    alias: &str,
) -> Option<(&'static str, String)> {
    let prop = node.child_by_field_name("property")?;
    let object = node.child_by_field_name("object")?;
    let field = node_text(&prop, source)?;
    if object.kind() == "member_expression" {
        let inner_prop = object.child_by_field_name("property")?;
        let inner = node_text(&inner_prop, source)?;
        let root = object.child_by_field_name("object")?;
        let root_name = node_text(&root, source)?;
        if root_name == alias {
            match inner.as_str() {
                "body" => return Some(("body", field)),
                "query" => return Some(("query", field)),
                _ => return None,
            }
        }
    }
    None
}

fn push_field(fields: &mut Vec<ParamField>, name: String, line: usize, optional: bool) {
    if let Some(existing) = fields.iter_mut().find(|f| f.name == name) {
        existing.optional = existing.optional || optional;
        return;
    }
    fields.push(ParamField {
        name,
        line,
        optional,
    });
}

/// Top-level (and nested) named functions: `(name, decl_line, body_rows, params_rows)`.
#[allow(clippy::type_complexity)]
fn collect_named_functions(
    tree: &Tree,
    source: &[u8],
) -> Vec<(String, Option<usize>, Option<(usize, usize)>, Option<usize>)> {
    let mut out = Vec::new();
    let mut stack = vec![tree.root_node()];
    while let Some(node) = stack.pop() {
        match node.kind() {
            "function_declaration" => {
                let name = child_text(&node, "name", source);
                let body = node
                    .child_by_field_name("body")
                    .map(|b| (b.start_position().row, b.end_position().row));
                let params = node
                    .child_by_field_name("parameters")
                    .map(|p| p.start_position().row);
                if let Some(name) = name {
                    out.push((name, Some(node.start_position().row + 1), body, params));
                }
            }
            "variable_declarator" => {
                let name = child_text(&node, "name", source);
                let value = node.child_by_field_name("value");
                if let (Some(name), Some(value)) = (name, value) {
                    if matches!(
                        value.kind(),
                        "arrow_function" | "function_expression" | "function"
                    ) {
                        let body = value
                            .child_by_field_name("body")
                            .map(|b| (b.start_position().row, b.end_position().row));
                        let params = value
                            .child_by_field_name("parameters")
                            .map(|p| p.start_position().row);
                        out.push((name, None, body, params));
                    }
                }
            }
            _ => {}
        }
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            stack.push(child);
        }
    }
    out
}

fn function_signature(node: tree_sitter::Node, source: &[u8], file: &str) -> Option<FnSignature> {
    let name = child_text(&node, "name", source)?;
    let params = node
        .child_by_field_name("parameters")
        .map(|p| parse_formal_params(&p, source))
        .unwrap_or_default();
    Some(FnSignature {
        name,
        file: file.to_string(),
        line: node.start_position().row + 1,
        params,
    })
}

/// `const f = (a, b = 1) => ...` signatures declared via var/let/const.
fn arrow_signatures(node: tree_sitter::Node, source: &[u8], file: &str) -> Vec<FnSignature> {
    let mut out = Vec::new();
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() != "variable_declarator" {
            continue;
        }
        let value = child.child_by_field_name("value");
        let Some(value) = value else { continue };
        if !matches!(
            value.kind(),
            "arrow_function" | "function_expression" | "function"
        ) {
            continue;
        }
        if let Some(name) = child_text(&child, "name", source) {
            let params = value
                .child_by_field_name("parameters")
                .map(|p| parse_formal_params(&p, source))
                .unwrap_or_default();
            out.push(FnSignature {
                name,
                file: file.to_string(),
                line: child.start_position().row + 1,
                params,
            });
        }
    }
    out
}

/// Parameter list with optionality from defaults (`b = 1`) — TS `?` markers
/// are handled the same way by text inspection.
fn parse_formal_params(params: &tree_sitter::Node, source: &[u8]) -> Vec<ParamField> {
    let mut out = Vec::new();
    let mut cursor = params.walk();
    for child in params.children(&mut cursor).filter(|c| c.is_named()) {
        let text = match node_text(&child, source) {
            Some(t) => t,
            None => continue,
        };
        let base = text.split('=').next().unwrap_or(&text).trim();
        let name = base.trim_end_matches('?').trim().to_string();
        if name.is_empty()
            || !name
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        {
            continue;
        }
        out.push(ParamField {
            name,
            line: child.start_position().row + 1,
            optional: text.contains('=') || text.contains('?'),
        });
    }
    out
}

fn first_param_identifier(params: &tree_sitter::Node, source: &[u8]) -> Option<String> {
    let mut cursor = params.walk();
    params
        .children(&mut cursor)
        .filter(|c| c.is_named())
        .find(|c| c.kind() == "identifier")
        .and_then(|c| node_text(&c, source))
}

/// Recover the parameter list text of a function declared at `row` by
/// re-reading the source line (named-handler alias resolution).
fn source_rows_identifier(source: &[u8], row: &usize) -> Option<String> {
    let text = std::str::from_utf8(source).ok()?;
    let line = text.lines().nth(*row)?;
    let open = line.find('(')?;
    let close = line.find(')')?;
    let inner = line.get(open + 1..close)?;
    inner
        .split(',')
        .next()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Locate a node whose row span matches — used once per resolved handler to
/// re-walk its body for request fields.
fn node_at_rows(root: tree_sitter::Node, start: usize, end: usize) -> Option<tree_sitter::Node> {
    let mut stack = vec![root];
    while let Some(node) = stack.pop() {
        if node.start_position().row == start && node.end_position().row == end {
            return Some(node);
        }
        if node.start_position().row > start || node.end_position().row < end {
            continue;
        }
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            stack.push(child);
        }
    }
    None
}

fn first_string_fragment(node: &tree_sitter::Node, source: &[u8]) -> Option<String> {
    let mut cursor = node.walk();
    node.children(&mut cursor)
        .find(|c| c.kind() == "string_fragment")
        .and_then(|c| node_text(&c, source))
}

fn child_text(node: &tree_sitter::Node, field: &str, source: &[u8]) -> Option<String> {
    node.child_by_field_name(field)
        .and_then(|n| n.utf8_text(source).ok().map(String::from))
}

pub(crate) fn node_text(node: &tree_sitter::Node, source: &[u8]) -> Option<String> {
    node.utf8_text(source).ok().map(String::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = r#"
const express = require('express');
const app = express();

function lookupUser(id) {
  if (!id) return null;
  return db.users[id];
}

app.get('/api/health', (req, res) => {
  res.json({ ok: true });
});

app.post('/api/register', async (req, res) => {
  const { username, email = '' } = req.body;
  const captcha = req.body.captcha;
  const q = req.query.invite;
  if (!username) return res.status(400).json({});
  res.json({});
});

const handler = (req, res) => {
  const id = req.params.id;
  const name = req.body.name;
  res.json({});
};

app.put('/api/users/:id', handler);

function greet(name = 'world') {
  return `hi ${name}`;
}

const add = (a, b = 2) => a + b;
"#;

    #[test]
    fn extracts_routes_with_literal_handlers() {
        let out = extract_file(FIXTURE, "backend/src/app.js").unwrap();
        assert_eq!(
            out.endpoints.len(),
            3,
            "GET health, POST register, PUT users/:id"
        );

        let health = &out.endpoints[0];
        assert_eq!(health.key, "GET /api/health");
        assert_eq!(health.method, "GET");
        assert!(health.body_fields.is_empty());

        let register = &out.endpoints[1];
        assert_eq!(register.key, "POST /api/register");
        let names: Vec<&str> = register
            .body_fields
            .iter()
            .map(|f| f.name.as_str())
            .collect();
        assert_eq!(names, vec!["username", "email", "captcha"]);
        let email = register
            .body_fields
            .iter()
            .find(|f| f.name == "email")
            .unwrap();
        assert!(email.optional, "destructuring default marks optionality");
        let username = register
            .body_fields
            .iter()
            .find(|f| f.name == "username")
            .unwrap();
        assert!(!username.optional);
        let q = &register.query_fields[0];
        assert_eq!(q.name, "invite");
    }

    #[test]
    fn resolves_named_handlers_and_path_params() {
        let out = extract_file(FIXTURE, "backend/src/app.js").unwrap();
        let put = out
            .endpoints
            .iter()
            .find(|e| e.method == "PUT")
            .expect("PUT route extracted");
        assert_eq!(put.path, "/api/users/:id");
        assert_eq!(put.path_params, vec!["id"]);
        assert_eq!(put.handler_ref.as_deref(), Some("handler"));
        // Body fields come from the resolved handler body (`req.params.id`
        // is a path-param read, not a body field).
        assert_eq!(put.body_fields.len(), 1);
        assert_eq!(put.body_fields[0].name, "name");
        // The span lets validation rules attach to this endpoint.
        assert_eq!(out.spans.len(), 3);
    }

    #[test]
    fn extracts_function_signatures_with_defaults() {
        let out = extract_file(FIXTURE, "backend/src/app.js").unwrap();
        let greet = out.functions.iter().find(|f| f.name == "greet").unwrap();
        assert_eq!(greet.params.len(), 1);
        assert!(greet.params[0].optional, "default value marks optional");
        assert_eq!(greet.file, "backend/src/app.js");

        let add = out.functions.iter().find(|f| f.name == "add").unwrap();
        assert_eq!(add.params.len(), 2);
        assert!(add.params[1].optional);
        assert!(!add.params[0].optional);
    }

    #[test]
    fn skips_non_literal_route_paths_instead_of_guessing() {
        let source = r#"
const path = process.env.ROUTE;
app.post(path, (req, res) => { res.json({}); });
app.post(`/api/${name}`, (req, res) => { res.json({}); });
"#;
        let out = extract_file(source, "backend/src/app.js").unwrap();
        assert!(out.endpoints.is_empty());
    }

    #[test]
    fn parses_broken_source_without_panic() {
        let out = extract_file("app.post('/x', (((", "backend/src/app.js");
        assert!(out.is_ok() || out.is_err(), "must not panic either way");
    }
}
