//! Three-layer workspace scan engine (ast).
//!
//! Layered so the cheap layer runs on EVERY mutating batch and the
//! expensive layer only when the cheap one sees signal movement:
//!
//! - **L0 walker** — `ignore::WalkBuilder`, gitignore-aware
//!   (node_modules/dist/.arc/*.db are excluded by the harness gitignore
//!   and again by an explicit belt-and-braces filter), deterministic
//!   order, hard caps: ≤[`MAX_FILES`] files, ≤1MB/file, depth ≤6.
//! - **L1 rg literal layer** — `grep-searcher` + `grep-regex` (the
//!   ripgrep engine as library crates) harvests literal signals:
//!   `data-testid="x"`, `'/api/…'` route strings, `127.0.0.1:PORT` /
//!   `localhost:PORT`, `a[href="…"]`. [`drift`] compares the specs'
//!   required literal signals against these hits — pure string
//!   comparison, no parse, millisecond-scale. This is the drift engine
//!   AND the escalation filter: only files whose lines hit a signal
//!   climb to L2.
//! - **L2 ts precise layer** — tree-sitter structural signals for files
//!   that changed: [`change_localize`] diffs the cached tag set against
//!   the new source and localizes edits to `file:symbol:line` rows for
//!   the `<arc-workspace>` tail block. Structural confirmation always
//!   goes through this layer; the rg layer only nominates candidates.

use std::collections::HashMap;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use super::{AcceptanceSignal, DriftReport, SignalKind, Tag, TagKind, render::ChangeNote};

/// Walker hard caps — ARC workspaces are 10–40 files; anything past this
/// is degraded (skipped), never scanned at unbounded cost.
pub const MAX_FILES: usize = 64;
const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_DEPTH: usize = 6;
const SCANNED_EXTENSIONS: &[&str] = &["js", "mjs", "cjs", "jsx", "tsx", "ts", "html"];
/// Directories never scanned even without a .gitignore (platform
/// workspaces may lack one).
const SKIPPED_DIRS: &[&str] = &[
    "node_modules",
    "dist",
    ".arc",
    ".git",
    ".octos",
    "build",
    "coverage",
];

/// One workspace file the L0 walker admitted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceFile {
    /// Workspace-relative path (forward slashes, sorted).
    pub rel: String,
    pub path: PathBuf,
}

/// L0: deterministic, gitignore-aware file collection.
pub fn walk_workspace(root: &Path) -> Vec<WorkspaceFile> {
    let mut out: Vec<WorkspaceFile> = Vec::new();
    let walker = ignore::WalkBuilder::new(root)
        .hidden(true)
        .git_ignore(true)
        .git_global(false)
        .parents(true)
        .build();
    for entry in walker.flatten() {
        let path = entry.path();
        let Ok(rel) = path.strip_prefix(root) else {
            continue;
        };
        if rel.as_os_str().is_empty() {
            continue;
        }
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        if rel_str.split('/').any(|seg| SKIPPED_DIRS.contains(&seg)) {
            continue;
        }
        if rel.iter().count() > MAX_DEPTH {
            continue;
        }
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        let ext_ok = path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| SCANNED_EXTENSIONS.contains(&e));
        if !ext_ok {
            continue;
        }
        if meta.len() > MAX_FILE_BYTES {
            continue;
        }
        out.push(WorkspaceFile {
            rel: rel_str,
            path: path.to_path_buf(),
        });
        if out.len() >= MAX_FILES {
            break;
        }
    }
    out.sort_by(|a, b| a.rel.cmp(&b.rel));
    out
}

/// One literal workspace hit (L1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiteralHit {
    pub file: String,
    pub line: usize,
    pub kind: SignalKind,
    pub value: String,
}

/// Drift is a PURE literal comparison — only these required-signal kinds
/// participate (role/label/text signals have no workspace-literal shape).
pub fn is_literal_kind(kind: SignalKind) -> bool {
    matches!(
        kind,
        SignalKind::Testid | SignalKind::ApiRoute | SignalKind::Port | SignalKind::Href
    )
}

/// L1: harvest literal signals from the given files via the ripgrep
/// engine (grep-regex matcher + grep-searcher line loop), values
/// extracted per line with capture groups.
pub fn harvest_literals(files: &[WorkspaceFile]) -> Vec<LiteralHit> {
    // (kind, line regex with ONE capture group)
    const PATTERNS: &[(&str, &str)] = &[
        (
            "data-testid",
            // Covers JSX/string attrs, object keys, and
            // setAttribute('data-testid', 'x') two-arg form.
            r#"data-[Tt]est[Ii]d["']?\s*[:,=]\s*["']([^"']{1,80})["']"#,
        ),
        (
            "api route",
            r#"["'`](/api/[A-Za-z0-9/_:.\-{}$]{1,80})["'`]"#,
        ),
        ("host port", r#"(?:127\.0\.0\.1|localhost):([0-9]{2,5})"#),
        ("href", r#"a\[href=["']([^"']{1,80})["']\]"#),
    ];

    let mut hits = Vec::new();
    for (label, pattern) in PATTERNS {
        let kind = match *label {
            "data-testid" => SignalKind::Testid,
            "api route" => SignalKind::ApiRoute,
            "host port" => SignalKind::Port,
            _ => SignalKind::Href,
        };
        let Ok(matcher) = grep_regex::RegexMatcher::new(pattern) else {
            continue;
        };
        let Ok(captures) = regex::Regex::new(pattern) else {
            continue;
        };
        for file in files {
            let mut found: Vec<(usize, String)> = Vec::new();
            let mut searcher = grep_searcher::SearcherBuilder::new()
                .line_number(true)
                .build();
            let result = searcher.search_path(
                &matcher,
                &file.path,
                grep_searcher::sinks::UTF8(|line_no: u64, line: &str| {
                    found.push((line_no as usize, line.to_string()));
                    Ok(true)
                }),
            );
            if result.is_err() {
                continue;
            }
            for (line_no, line) in found {
                let Some(caps) = captures.captures(&line) else {
                    continue;
                };
                let mut value = caps
                    .get(1)
                    .map(|g| g.as_str().to_string())
                    .unwrap_or_default();
                if kind == SignalKind::ApiRoute {
                    // Strip the quoting — the required side stores bare
                    // paths like `/api/register`.
                    value = value
                        .trim_matches(['"', '\'', '`'])
                        .trim_end_matches('/')
                        .to_string();
                }
                if value.is_empty() {
                    continue;
                }
                hits.push(LiteralHit {
                    file: file.rel.clone(),
                    line: line_no,
                    kind,
                    value,
                });
            }
        }
    }
    hits.sort_by(|a, b| {
        (&a.file, a.line, a.kind, &a.value).cmp(&(&b.file, b.line, b.kind, &b.value))
    });
    hits
}

/// L1 drift engine: required literal signals vs workspace hits → the
/// missing set. `newly_satisfied` is filled by [`reconcile_drift`] (it
/// needs the previous round's missing set, which the caller owns).
pub fn drift(required: &[AcceptanceSignal], hits: &[LiteralHit]) -> DriftReport {
    let hit_set: HashSet<(SignalKind, &str)> =
        hits.iter().map(|h| (h.kind, h.value.as_str())).collect();
    let missing = required
        .iter()
        .filter(|s| is_literal_kind(s.kind) && !hit_set.contains(&(s.kind, s.value.as_str())))
        .cloned()
        .collect();
    DriftReport {
        missing,
        newly_satisfied: Vec::new(),
    }
}

/// Merge the previous round's missing set into a new report: signals that
/// were missing and are no longer become `newly_satisfied`.
pub fn reconcile_drift(prev_missing: &[AcceptanceSignal], current: &mut DriftReport) {
    let still: HashSet<(SignalKind, &str)> = current
        .missing
        .iter()
        .map(|s| (s.kind, s.value.as_str()))
        .collect();
    current.newly_satisfied = prev_missing
        .iter()
        .filter(|s| !still.contains(&(s.kind, s.value.as_str())))
        .cloned()
        .collect();
}

/// L2: localize what changed in a file whose tags were cached. New or
/// moved definitions surface as `file:symbol:line` notes; pure-ref churn
/// is ignored (the drift layer speaks for refs).
pub fn change_localize(file: &str, old_tags: &[Tag], new_source: &str) -> Vec<ChangeNote> {
    let Some(new_tags) = super::repomap::extract_file_tags(new_source, file) else {
        return Vec::new();
    };
    let old_defs: HashMap<&str, usize> = old_tags
        .iter()
        .filter(|t| t.kind == TagKind::Def)
        .map(|t| (t.name.as_str(), t.line))
        .collect();
    let new_defs: HashMap<&str, usize> = new_tags
        .iter()
        .filter(|t| t.kind == TagKind::Def)
        .map(|t| (t.name.as_str(), t.line))
        .collect();

    let mut notes: Vec<ChangeNote> = Vec::new();
    for (name, line) in &new_defs {
        match old_defs.get(name) {
            None => notes.push(change_note(file, name, *line)),
            Some(old_line) if old_line != line => {
                notes.push(change_note(file, name, *line));
            }
            _ => {}
        }
    }
    for (name, line) in &old_defs {
        if !new_defs.contains_key(name) {
            notes.push(change_note(file, name, *line));
        }
    }
    notes.sort_by(|a, b| {
        (
            a.file.as_str(),
            a.line.unwrap_or(0),
            a.symbol.as_deref().unwrap_or(""),
        )
            .cmp(&(
                b.file.as_str(),
                b.line.unwrap_or(0),
                b.symbol.as_deref().unwrap_or(""),
            ))
    });
    notes.truncate(8);
    notes
}

fn change_note(file: &str, symbol: &str, line: usize) -> ChangeNote {
    ChangeNote {
        file: file.to_string(),
        symbol: Some(symbol.to_string()),
        line: Some(line),
    }
}

/// path+mtime tag cache (mirrors `file_state_cache::FileStateCache`
/// semantics): a stale mtime invalidates the entry.
#[derive(Debug, Default, Clone)]
pub struct TagCache {
    entries: HashMap<String, (SystemTime, Vec<Tag>)>,
}

impl TagCache {
    pub fn get(&self, rel: &str, current_mtime: SystemTime) -> Option<&[Tag]> {
        let (mtime, tags) = self.entries.get(rel)?;
        if *mtime != current_mtime {
            return None;
        }
        Some(tags)
    }

    pub fn put(&mut self, rel: String, mtime: SystemTime, tags: Vec<Tag>) {
        self.entries.insert(rel, (mtime, tags));
    }

    pub fn into_map(self) -> HashMap<String, (SystemTime, Vec<Tag>)> {
        self.entries
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(root: &Path, rel: &str, contents: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, contents).unwrap();
    }

    #[test]
    fn walker_respects_gitignore_and_skips_dirs() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, ".gitignore", "node_modules/\ndist/\n.arc/\n*.db\n");
        write(root, "backend/src/app.js", "const app = 1;\n");
        write(root, "frontend/src/main.jsx", "export default 1;\n");
        write(root, "node_modules/pkg/index.js", "module.exports = 2;\n");
        write(root, "dist/bundle.js", "// built\n");
        write(root, ".arc/host-plan/state.v1.json", "{}\n");
        write(root, "backend/data/cache.db", "binary\n");
        write(root, "README.md", "# readme\n");

        let files = walk_workspace(root);
        let rels: Vec<&str> = files.iter().map(|f| f.rel.as_str()).collect();
        assert!(rels.contains(&"backend/src/app.js"), "{rels:?}");
        assert!(rels.contains(&"frontend/src/main.jsx"));
        assert!(!rels.iter().any(|r| r.contains("node_modules")), "{rels:?}");
        assert!(!rels.iter().any(|r| r.starts_with("dist/")), "{rels:?}");
        assert!(!rels.iter().any(|r| r.starts_with(".arc/")), "{rels:?}");
        assert!(!rels.iter().any(|r| r.ends_with(".db")));
        assert!(!rels.contains(&"README.md"), "extension filter");
    }

    #[test]
    fn harvest_literals_finds_testid_api_port_href() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "src/app.js",
            concat!(
                "app.get('/api/health', h);\n",
                "app.post('/api/register', h);\n",
                "el.setAttribute('data-testid', 'submit-btn');\n",
                "const url = 'http://127.0.0.1:3301';\n",
                "const link = 'a[href=\"/login\"]';\n",
                "const host = 'localhost:3000';\n",
            ),
        );
        let files = walk_workspace(dir.path());
        assert_eq!(files.len(), 1);
        let hits = harvest_literals(&files);

        assert!(
            hits.iter()
                .any(|h| h.kind == SignalKind::ApiRoute && h.value == "/api/health" && h.line == 1),
            "{hits:?}"
        );
        assert!(
            hits.iter()
                .any(|h| h.kind == SignalKind::ApiRoute && h.value == "/api/register")
        );
        assert!(
            hits.iter()
                .any(|h| h.kind == SignalKind::Testid && h.value == "submit-btn")
        );
        assert!(
            hits.iter()
                .any(|h| h.kind == SignalKind::Port && h.value == "3301")
        );
        assert!(
            hits.iter()
                .any(|h| h.kind == SignalKind::Port && h.value == "3000")
        );
        assert!(
            hits.iter()
                .any(|h| h.kind == SignalKind::Href && h.value == "/login")
        );
    }

    #[test]
    fn drift_reports_missing_literal_signals_only() {
        let required = vec![
            sig(SignalKind::Testid, "password-strength"),
            sig(SignalKind::ApiRoute, "/api/register"),
            sig(SignalKind::RoleName, "button Next"),
            sig(SignalKind::Href, "/login"),
            sig(SignalKind::Port, "3301"),
        ];
        let hits = vec![
            hit(SignalKind::Testid, "password-strength"),
            hit(SignalKind::ApiRoute, "/api/register"),
            hit(SignalKind::Port, "3301"),
        ];
        let report = drift(&required, &hits);
        // role_name is not a literal kind — never drifts.
        assert_eq!(report.missing.len(), 1, "{:?}", report.missing);
        assert_eq!(report.missing[0].value, "/login");
        assert!(report.newly_satisfied.is_empty());
    }

    #[test]
    fn reconcile_marks_previously_missing_as_newly_satisfied() {
        let required = vec![sig(SignalKind::Testid, "count")];
        // Round 1: not in workspace.
        let first = drift(&required, &[]);
        assert_eq!(first.missing.len(), 1);
        // Round 2: implemented.
        let mut second = drift(&required, &[hit(SignalKind::Testid, "count")]);
        reconcile_drift(&first.missing, &mut second);
        assert!(second.missing.is_empty());
        assert_eq!(second.newly_satisfied.len(), 1);
        assert_eq!(second.newly_satisfied[0].value, "count");
        // Fingerprint changed → tail block worthy.
        assert_ne!(first.fingerprint(), second.fingerprint());
    }

    #[test]
    fn change_localize_reports_added_moved_and_removed_defs() {
        let old_source = "function alpha() {}\nfunction beta() {}\n";
        let old_tags = super::super::repomap::extract_file_tags(old_source, "x.js").unwrap();
        let new_source = "function alpha() {}\nconst gamma = () => {};\n";
        let notes = change_localize("frontend/src/app.js", &old_tags, new_source);
        let names: Vec<&str> = notes
            .iter()
            .map(|n| n.symbol.as_deref().unwrap_or(""))
            .collect();
        assert!(names.contains(&"gamma"), "added def reported: {names:?}");
        assert!(names.contains(&"beta"), "removed def reported: {names:?}");
        assert!(!names.contains(&"alpha"), "untouched def silent: {names:?}");
        for note in &notes {
            assert_eq!(note.file, "frontend/src/app.js");
            assert!(note.symbol.is_some() && note.line.is_some());
        }
    }

    #[test]
    fn change_localize_reports_moved_def_at_new_line() {
        let old_source = "function a() {}\nfunction b() {}\n";
        let old_tags = super::super::repomap::extract_file_tags(old_source, "x.js").unwrap();
        let new_source = "// lead\nfunction a() {}\nfunction b() {}\n";
        let notes = change_localize("f.js", &old_tags, new_source);
        let a_note = notes
            .iter()
            .find(|n| n.symbol.as_deref() == Some("a"))
            .unwrap();
        assert_eq!(a_note.line, Some(2), "moved def reports its NEW line");
    }

    #[test]
    fn tag_cache_invalidates_on_mtime_change() {
        let mut cache = TagCache::default();
        let t0 = SystemTime::UNIX_EPOCH;
        let t1 = t0 + std::time::Duration::from_secs(1);
        cache.put(
            "src/app.js".to_string(),
            t0,
            vec![Tag {
                name: "a".into(),
                line: 1,
                kind: TagKind::Def,
            }],
        );
        assert!(cache.get("src/app.js", t0).is_some());
        assert!(
            cache.get("src/app.js", t1).is_none(),
            "mtime change invalidates"
        );
        assert!(cache.get("other.js", t0).is_none());
    }

    fn sig(kind: SignalKind, value: &str) -> AcceptanceSignal {
        AcceptanceSignal {
            kind,
            value: value.into(),
            note: None,
            spec_file: "REQ-1.spec.ts".into(),
        }
    }

    fn hit(kind: SignalKind, value: &str) -> LiteralHit {
        LiteralHit {
            file: "src/app.js".into(),
            line: 1,
            kind,
            value: value.into(),
        }
    }
}
