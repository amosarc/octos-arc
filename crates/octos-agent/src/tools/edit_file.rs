//! Edit file tool for making precise text replacements.

use std::path::PathBuf;

use async_trait::async_trait;
use eyre::Result;
use serde::Deserialize;

use super::{ConcurrencyClass, Tool, ToolContext, ToolResult};
use crate::file_state_cache::{CacheEntry, FileStateCache};
use crate::policy::{FileAccessMode, FilesystemScope};

/// Tool for editing files via string replacement.
pub struct EditFileTool {
    /// Base directory for resolving relative paths.
    base_dir: PathBuf,
    /// Effective filesystem scope.
    filesystem_scope: FilesystemScope,
    /// Whether writes are permitted.
    file_access: FileAccessMode,
}

impl EditFileTool {
    /// Create a new edit file tool.
    pub fn new(base_dir: impl Into<PathBuf>) -> Self {
        Self {
            base_dir: base_dir.into(),
            filesystem_scope: FilesystemScope::Workspace,
            file_access: FileAccessMode::ReadWrite,
        }
    }

    /// Set the effective filesystem scope.
    pub fn with_filesystem_scope(mut self, filesystem_scope: FilesystemScope) -> Self {
        self.filesystem_scope = filesystem_scope;
        self
    }

    /// Set the effective file access mode.
    pub fn with_file_access(mut self, file_access: FileAccessMode) -> Self {
        self.file_access = file_access;
        self
    }
}

#[derive(Debug, Deserialize)]
// #1770: unknown keys are usually a typo of a real parameter; rejecting
// them (with a did-you-mean via `args::parse_tool_args`) lets the model
// self-correct instead of silently dropping its intent.
#[serde(deny_unknown_fields)]
struct EditFileInput {
    /// #1767: `filePath` is the industry-convention alias.
    #[serde(alias = "filePath")]
    path: String,
    #[serde(alias = "oldString")]
    old_string: String,
    #[serde(alias = "newString")]
    new_string: String,
}

#[async_trait]
impl Tool for EditFileTool {
    fn name(&self) -> &str {
        "edit_file"
    }

    fn description(&self) -> &str {
        "Replace `old_string` with `new_string` in a file; exact match preferred, tolerant fallback tried. `old_string` must identify one unique location."
    }

    fn tags(&self) -> &[&str] {
        &["fs", "code"]
    }

    fn concurrency_class(&self) -> ConcurrencyClass {
        // edit_file rewrites a file in place — same race hazard as
        // write_file. Serialize the whole batch. See M8.8.
        ConcurrencyClass::Exclusive
    }

    fn input_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit"
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to find; must be unique"
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text"
                }
            },
            "required": ["path", "old_string", "new_string"]
        })
    }

    async fn execute(&self, args: &serde_json::Value) -> Result<ToolResult> {
        // M8.4: legacy entry point routes through the typed path with a
        // zero-value context so out-of-band callers still exercise the same
        // file-state-cache invalidation logic.
        self.execute_with_context(&ToolContext::zero(), args).await
    }

    async fn execute_with_context(
        &self,
        ctx: &ToolContext,
        args: &serde_json::Value,
    ) -> Result<ToolResult> {
        let input: EditFileInput =
            super::args::parse_tool_args(self.name(), &self.input_schema(), args)?;

        if !self.file_access.allows_write() {
            return Ok(ToolResult {
                output: "edit_file is not permitted by read-only filesystem access".to_string(),
                success: false,
                ..Default::default()
            });
        }

        // Phase 2-C of the SessionScope migration: when the host has
        // threaded a scope through `ToolContext`, use it as the single
        // source of truth for base_dir + path classification. Same
        // write policy as `write_file` — `InWorkspace` and
        // `InGrantedDir` allowed; `InSharedZone` and `OutOfScope`
        // refused. The shared helper canonicalizes the candidate before
        // classification so ancestor symlinks can't smuggle an edit
        // out of the workspace.
        let path = match ctx.session_scope.as_ref() {
            Some(scope) => match super::resolve_path_for_session_scope_write(scope, &input.path) {
                Ok(p) => p,
                Err(reason) => {
                    return Ok(ToolResult {
                        output: format!("{reason}: {}", input.path),
                        success: false,
                        ..Default::default()
                    });
                }
            },
            None => match super::resolve_path_with_scope(
                &self.base_dir,
                &input.path,
                self.filesystem_scope,
            ) {
                Ok(p) => p,
                Err(_) => {
                    return Ok(ToolResult {
                        output: format!("Path outside working directory: {}", input.path),
                        success: false,
                        ..Default::default()
                    });
                }
            },
        };

        // Read current content (O_NOFOLLOW atomically rejects symlinks)
        let content = match super::read_no_follow(&path).await {
            Ok(c) => c,
            Err(e) => return Ok(super::file_io_error(e, &input.path)),
        };

        if input.old_string.is_empty() {
            return Ok(ToolResult {
                output: "old_string must not be empty".to_string(),
                success: false,
                ..Default::default()
            });
        }

        // #1771: cascading replacer chain — exact match first, then
        // increasingly whitespace/indentation/escape-tolerant fallbacks.
        let (range, replacer_name) = match super::replacer::find_replacement(
            &content,
            &input.old_string,
        ) {
            super::replacer::ChainOutcome::Match { range, replacer } => (range, replacer),
            super::replacer::ChainOutcome::Ambiguous { count, replacer } => {
                return Ok(ToolResult {
                    output: format!(
                        "Found {count} occurrences of the string (via {replacer} replacer). Please provide more context to make the match unique.",
                    ),
                    success: false,
                    ..Default::default()
                });
            }
            super::replacer::ChainOutcome::NoMatch => {
                return Ok(ToolResult {
                    output: format!(
                        "String not found in file. No exact match, and no fuzzy match via the line-trimmed, whitespace-normalized, indentation-flexible, escape-normalized or block-anchor replacers.\n\nSearched for:\n```\n{}\n```",
                        input.old_string
                    ),
                    success: false,
                    ..Default::default()
                });
            }
        };

        // When the match came from the escape_normalized replacer, BOTH call
        // strings carry the same double-escaping pathology — interpret
        // new_string (and the guard's needle) with the same unescape rules
        // that made old_string match. Splicing new_string verbatim would
        // write literal `\n` text into the file as code, and guarding
        // against the still-escaped old_string (1 physical line) would
        // falsely reject every legitimate multi-line match (#1771 review).
        let (guard_needle, splice_new) = if replacer_name == "escape_normalized" {
            (
                super::replacer::unescape_find(&input.old_string),
                super::replacer::unescape_find(&input.new_string),
            )
        } else {
            (input.old_string.clone(), input.new_string.clone())
        };

        // Safety guard: a fuzzy matcher must never silently swallow far more
        // of the file than the old_string described.
        let matched_text = &content[range.clone()];
        if super::replacer::is_disproportionate_match(matched_text, &guard_needle) {
            return Ok(ToolResult {
                output: format!(
                    "Fuzzy match rejected as disproportionate: the {replacer_name} replacer matched {} lines / {} bytes for an old_string of {} lines / {} bytes. Provide more context so the match is precise.",
                    matched_text.lines().count(),
                    matched_text.len(),
                    guard_needle.lines().count(),
                    guard_needle.len(),
                ),
                success: false,
                ..Default::default()
            });
        }

        if replacer_name != "exact" {
            tracing::info!(
                replacer = replacer_name,
                path = %input.path,
                "edit_file fuzzy match succeeded"
            );
        }

        // Perform replacement by byte range — the fuzzy-matched span may
        // occur elsewhere as a plain substring, so a string replacen could
        // hit the wrong occurrence.
        let mut new_content = String::with_capacity(content.len() - range.len() + splice_new.len());
        new_content.push_str(&content[..range.start]);
        new_content.push_str(&splice_new);
        new_content.push_str(&content[range.end..]);

        if let Err(e) = super::write_no_follow(&path, new_content.as_bytes()).await {
            return Ok(super::file_io_error(e, &input.path));
        }

        // #1774: opt-in post-edit formatting. Runs BEFORE cache invalidation
        // so both observe the final on-disk content. Best-effort by contract
        // — a formatter failure never fails the edit.
        let format_note = if ctx.format_after_edit {
            crate::format::post_edit_format_note(&path, &new_content).await
        } else {
            None
        };

        // M8.4: refresh the file-state cache entry. The stale pre-edit entry
        // is dropped and replaced by a PARTIAL view covering exactly the hunk
        // the edit result just shows — a follow-up read of that same range
        // can be answered with the [FILE_UNCHANGED] stub (the edit result is
        // the previous tool result it points at), while any other range still
        // misses conservatively. A full-file entry would be a trap: a model
        // that never read the whole file could get stubbed out of its own
        // content.
        let (hunk, hunk_view_range) =
            edited_hunk_preview(&new_content, range.start, splice_new.len());
        if let Some(cache) = ctx.file_state_cache.as_ref() {
            cache.invalidate(&path);
            if let Some(view_range) = hunk_view_range {
                if let Ok(meta) = tokio::fs::metadata(&path).await {
                    if let Ok(mtime) = meta.modified() {
                        if !FileStateCache::has_binary_extension(&path)
                            && FileStateCache::is_text_cacheable(new_content.as_bytes())
                        {
                            cache.put(CacheEntry::new(
                                path.clone(),
                                mtime,
                                FileStateCache::content_hash(new_content.as_bytes()),
                                new_content.len(),
                                true,
                                Some(view_range),
                            ));
                        }
                    }
                }
            }
        }

        // Report which replacer produced the match. The exact-match wording
        // is kept identical to the historical output for compatibility.
        let output = if replacer_name == "exact" {
            format!("Successfully edited {}", input.path)
        } else {
            format!(
                "Successfully edited {} (fuzzy match via {replacer_name} replacer)",
                input.path
            )
        };

        // Post-edit sight: show the edited region plus bounded context so the
        // model does not need a full re-read to see what the file looks like
        // now (tok-ticket-1: 13 edits drove 30 full-file re-reads).
        let mut output = format!("{output}{}", format_note.unwrap_or_default());
        if !hunk.is_empty() {
            output.push_str("\n\n");
            output.push_str(&hunk);
        }

        Ok(ToolResult {
            output,
            success: true,
            file_modified: Some(path),
            ..Default::default()
        })
    }
}

/// Maximum rendered size of the post-edit hunk preview (comfortably under
/// the 4KB model-visible tool-output cap, leaving room for the framing).
const HUNK_PREVIEW_MAX_CHARS: usize = 2048;
/// Context lines shown on each side of the edited region.
const HUNK_CONTEXT_LINES: usize = 10;

/// Render the edited region of `new_content` (1-indexed inclusive line span)
/// with line numbers (same `{n}│ {line}` style as `read_file`),
/// [`HUNK_CONTEXT_LINES`] of context on each side, capped at
/// [`HUNK_PREVIEW_MAX_CHARS`]. Returns the preview and the 1-indexed inclusive
/// line range it rendered (for the file-state cache). Shared with the
/// `diff_edit` tool, whose applier reports changed line spans directly.
pub(crate) fn edited_hunk_preview_lines(
    new_content: &str,
    region_start_line: u64,
    region_end_line: u64,
) -> (String, Option<(u64, u64)>) {
    let total_lines = new_content.lines().count() as u64;
    if total_lines == 0 {
        return (String::new(), None);
    }
    let region_start_line = region_start_line.clamp(1, total_lines);
    let region_end_line = region_end_line.clamp(region_start_line, total_lines);

    let window_start = region_start_line
        .saturating_sub(HUNK_CONTEXT_LINES as u64)
        .max(1);
    let window_end = (region_end_line + HUNK_CONTEXT_LINES as u64).min(total_lines);
    let width = total_lines.to_string().len();
    let render = |start: u64, end: u64| {
        let mut out = String::new();
        for (offset, line) in new_content
            .lines()
            .skip((start - 1) as usize)
            .take((end - start + 1) as usize)
            .enumerate()
        {
            let n = start + offset as u64;
            out.push_str(&format!("{n:>width$}│ {line}\n"));
        }
        out
    };
    let mut preview = render(window_start, window_end);
    if preview.len() > HUNK_PREVIEW_MAX_CHARS {
        preview = render(region_start_line, region_end_line);
    }
    if preview.len() > HUNK_PREVIEW_MAX_CHARS {
        preview.truncate(HUNK_PREVIEW_MAX_CHARS);
        preview.push_str("\n…[hunk truncated]");
    }
    preview.push_str(&format!(
        "[edit applied at lines {region_start_line}-{region_end_line}; \
         showing {}-{} of {} lines]",
        window_start, window_end, total_lines
    ));
    (preview, Some((window_start, window_end)))
}

/// Byte-range wrapper for `edit_file`: translate the replaced byte region of
/// `new_content` into its line span, then render via
/// [`edited_hunk_preview_lines`]. A splice ending in `\n` terminates its own
/// last line; one without it opens an extra (unfinished) line.
fn edited_hunk_preview(
    new_content: &str,
    region_byte_start: usize,
    region_byte_len: usize,
) -> (String, Option<(u64, u64)>) {
    let total_lines = new_content.lines().count() as u64;
    if total_lines == 0 {
        return (String::new(), None);
    }
    let clamp = |mut at: usize| {
        at = at.min(new_content.len());
        while at > 0 && !new_content.is_char_boundary(at) {
            at -= 1;
        }
        at
    };
    let region_start = clamp(region_byte_start);
    let region_end = clamp(region_byte_start + region_byte_len).max(region_start);
    let region_start_line = (new_content[..region_start].matches('\n').count() as u64) + 1;
    let region_text = &new_content[region_start..region_end];
    let replaced_line_span = if region_text.is_empty() {
        0
    } else if region_text.ends_with('\n') {
        region_text.matches('\n').count() as u64
    } else {
        region_text.matches('\n').count() as u64 + 1
    };
    let region_end_line = if replaced_line_span == 0 {
        region_start_line
    } else {
        (region_start_line + replaced_line_span - 1).min(total_lines)
    };
    edited_hunk_preview_lines(new_content, region_start_line, region_end_line)
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::tools::test_fixture::escape_ctx;

    #[tokio::test]
    async fn test_edit_file_basic_replacement() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("code.rs"),
            "fn main() {\n    println!(\"hello\");\n}\n",
        )
        .unwrap();

        let tool = EditFileTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({
                "path": "code.rs",
                "old_string": "println!(\"hello\")",
                "new_string": "println!(\"world\")"
            }))
            .await
            .unwrap();

        assert!(result.success);
        let content = std::fs::read_to_string(dir.path().join("code.rs")).unwrap();
        assert!(content.contains("println!(\"world\")"));
        assert!(!content.contains("println!(\"hello\")"));
    }

    #[tokio::test]
    async fn test_edit_file_string_not_found() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("file.txt"), "some content").unwrap();

        let tool = EditFileTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({
                "path": "file.txt",
                "old_string": "nonexistent string",
                "new_string": "replacement"
            }))
            .await
            .unwrap();

        assert!(!result.success);
        assert!(result.output.contains("String not found"));
    }

    #[tokio::test]
    async fn test_edit_file_ambiguous_match() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("dup.txt"), "foo bar foo baz foo").unwrap();

        let tool = EditFileTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({
                "path": "dup.txt",
                "old_string": "foo",
                "new_string": "qux"
            }))
            .await
            .unwrap();

        assert!(!result.success);
        assert!(result.output.contains("3 occurrences"));
    }

    #[tokio::test]
    async fn test_edit_file_traversal_blocked() {
        let dir = tempfile::tempdir().unwrap();
        let tool = EditFileTool::new(dir.path());

        let result = tool
            .execute(&serde_json::json!({
                "path": "../../etc/passwd",
                "old_string": "root",
                "new_string": "hacked"
            }))
            .await
            .unwrap();

        assert!(!result.success);
        assert!(result.output.contains("outside working directory"));
    }

    // -----------------------------------------------------------------------
    // Phase 2-C: SessionScope integration tests for EditFileTool.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn edit_file_refuses_out_of_scope_path() {
        let (scope_dir, outside_dir, ctx) = escape_ctx("edit-with-scope");
        let outside_file = outside_dir.path().join("target.txt");
        std::fs::write(&outside_file, "untouched\n").unwrap();

        let tool = EditFileTool::new(scope_dir.path());

        let result = tool
            .execute_with_context(
                &ctx,
                &serde_json::json!({
                    "path": outside_file.to_string_lossy(),
                    "old_string": "untouched",
                    "new_string": "owned",
                }),
            )
            .await
            .unwrap();
        assert!(!result.success);
        assert!(
            result.output.contains("outside session scope"),
            "expected scope rejection, got: {}",
            result.output
        );
        // File MUST remain unchanged.
        assert_eq!(
            std::fs::read_to_string(&outside_file).unwrap(),
            "untouched\n"
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn edit_file_refuses_ancestor_symlink_escape() {
        // Symmetric with the write_file ancestor-symlink test. Even
        // with a real target file pre-staged at the symlink target,
        // the scoped resolver must refuse before O_NOFOLLOW would
        // (correctly) bail on the symlink itself.
        use std::os::unix::fs::symlink;

        let (scope_dir, outside_dir, ctx) = escape_ctx("edit-with-scope");
        std::fs::write(outside_dir.path().join("target.txt"), "secret\n").unwrap();
        let link_path = scope_dir.path().join("link");
        symlink(outside_dir.path(), &link_path).unwrap();

        let tool = EditFileTool::new(scope_dir.path());

        let result = tool
            .execute_with_context(
                &ctx,
                &serde_json::json!({
                    "path": "link/target.txt",
                    "old_string": "secret",
                    "new_string": "owned",
                }),
            )
            .await
            .unwrap();
        assert!(
            !result.success,
            "ancestor-symlink escape MUST be refused, got: {}",
            result.output
        );
        assert!(
            result.output.contains("outside session scope"),
            "expected scope rejection, got: {}",
            result.output
        );
        // Real file at the symlink target must remain unchanged.
        assert_eq!(
            std::fs::read_to_string(outside_dir.path().join("target.txt")).unwrap(),
            "secret\n",
        );
    }

    // -----------------------------------------------------------------------
    // #1771: cascading fuzzy replacer chain.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn should_match_via_line_trimmed_when_indentation_differs() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("code.rs"),
            "fn main() {\n    if ready {\n        launch();\n    }\n}\n",
        )
        .unwrap();

        let tool = EditFileTool::new(dir.path());
        // LLM lost the indentation (flush left) but every line's trimmed
        // content is right — the line_trimmed replacer must recover it.
        let result = tool
            .execute(&serde_json::json!({
                "path": "code.rs",
                "old_string": "if ready {\nlaunch();\n}",
                "new_string": "if ready {\n        abort();\n    }"
            }))
            .await
            .unwrap();

        assert!(
            result.success,
            "line-trimmed fuzzy match should succeed: {}",
            result.output
        );
        assert!(
            result.output.contains("line_trimmed"),
            "success output must report which replacer matched: {}",
            result.output
        );
        let content = std::fs::read_to_string(dir.path().join("code.rs")).unwrap();
        assert!(content.contains("abort();"));
        assert!(!content.contains("launch();"));
    }

    #[tokio::test]
    async fn should_refuse_block_anchor_span_far_exceeding_old_string() {
        let dir = tempfile::tempdir().unwrap();
        let original = "fn f() {\n    a();\n    b();\n    c();\n    d();\n    e();\n    g();\n}\n";
        std::fs::write(dir.path().join("g.rs"), original).unwrap();

        let tool = EditFileTool::new(dir.path());
        // Anchors would bracket 8 lines for a 3-line old_string. The block
        // similarity score counts the six undescribed middle lines against
        // the match, so block_anchor refuses outright (NoMatch) before the
        // disproportionate guard is even consulted.
        let result = tool
            .execute(&serde_json::json!({
                "path": "g.rs",
                "old_string": "fn f() {\n    a();\n}",
                "new_string": "fn f() {}"
            }))
            .await
            .unwrap();

        assert!(
            !result.success,
            "runaway span must be refused: {}",
            result.output
        );
        assert!(
            result.output.contains("String not found"),
            "expected NoMatch refusal, got: {}",
            result.output
        );
        // File untouched.
        assert_eq!(
            std::fs::read_to_string(dir.path().join("g.rs")).unwrap(),
            original
        );
    }

    // -----------------------------------------------------------------------
    // #1767: industry-convention parameter aliases.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn should_refresh_file_cache_with_hunk_view_after_edit() {
        use crate::file_state_cache::{CacheEntry, FileStateCache};
        use std::sync::Arc;

        let dir = tempfile::tempdir().unwrap();
        let file_path = dir.path().join("code.rs");
        std::fs::write(&file_path, "fn foo() {}\n").unwrap();

        let cache = Arc::new(FileStateCache::new());
        cache.put(CacheEntry::new(
            file_path.clone(),
            std::time::SystemTime::now(),
            0xCAFE,
            12,
            false,
            None,
        ));
        assert_eq!(cache.len(), 1);

        let mut ctx = ToolContext::zero();
        ctx.file_state_cache = Some(cache.clone());

        let tool = EditFileTool::new(dir.path());
        let result = tool
            .execute_with_context(
                &ctx,
                &serde_json::json!({
                    "path": "code.rs",
                    "old_string": "fn foo() {}",
                    "new_string": "fn bar() {}"
                }),
            )
            .await
            .unwrap();

        assert!(result.success);
        // The stale entry is replaced by a partial entry covering exactly the
        // rendered hunk, so a same-range re-read can stub (the edit result is
        // the previous tool result) while any other range misses conservatively.
        let entry = cache.peek(&file_path).expect("fresh hunk entry after edit");
        assert_eq!(entry.view_range, Some((1, 1)));
        assert!(entry.is_partial_view);
        // The hunk itself is visible in the result.
        assert!(
            result.output.contains("1│ fn bar() {}"),
            "edit result must show the edited hunk: {}",
            result.output
        );
        assert!(
            result.output.contains("showing 1-1 of 1 lines"),
            "edit result must bound the preview: {}",
            result.output
        );
    }

    #[tokio::test]
    async fn should_return_edited_hunk_with_line_numbers_when_edit_succeeds() {
        let dir = tempfile::tempdir().unwrap();
        let mut file = String::new();
        for i in 1..=30 {
            file.push_str(&format!("line {i:02}\n"));
        }
        std::fs::write(dir.path().join("big.txt"), &file).unwrap();

        let tool = EditFileTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({
                "path": "big.txt",
                "old_string": "line 15\nline 16\n",
                "new_string": "line 15\nEDITED\nline 16\n"
            }))
            .await
            .unwrap();

        assert!(result.success, "edit should succeed: {}", result.output);
        assert!(
            result.output.contains("Successfully edited big.txt"),
            "historical wording must be preserved: {}",
            result.output
        );
        // The splice inserts EDITED at line 16 and shifts later content.
        assert!(
            result.output.contains("16│ EDITED"),
            "hunk must show the new content, line-numbered: {}",
            result.output
        );
        assert!(
            result.output.contains("15│ line 15") && result.output.contains("17│ line 16"),
            "hunk must bracket the edit (later lines shift by one): {}",
            result.output
        );
        // ±10-line context around the edited region (lines 5-27).
        assert!(
            result.output.contains("5│ line 05") && result.output.contains("27│ line 26"),
            "hunk must include bounded context: {}",
            result.output
        );
        assert!(
            !result.output.contains("1│ line 01") && !result.output.contains("31│"),
            "hunk must not render the whole file: {}",
            result.output
        );
    }

    #[tokio::test]
    async fn should_refresh_file_cache_entry_after_edit() {
        // Legacy-name regression guard renamed semantics: after an edit the
        // cache must never retain the STALE pre-edit entry.
        use crate::file_state_cache::{CacheEntry, FileStateCache};
        use std::sync::Arc;

        let dir = tempfile::tempdir().unwrap();
        let file_path = dir.path().join("code.rs");
        std::fs::write(&file_path, "fn foo() {}\n").unwrap();

        let cache = Arc::new(FileStateCache::new());
        cache.put(CacheEntry::new(
            file_path.clone(),
            std::time::SystemTime::now(),
            0xDEAD,
            12,
            false,
            None,
        ));

        let mut ctx = ToolContext::zero();
        ctx.file_state_cache = Some(cache.clone());

        let tool = EditFileTool::new(dir.path());
        let result = tool
            .execute_with_context(
                &ctx,
                &serde_json::json!({
                    "path": "code.rs",
                    "old_string": "fn foo() {}",
                    "new_string": "fn bar() {} // replaced"
                }),
            )
            .await
            .unwrap();

        assert!(result.success);
        let entry = cache.peek(&file_path).expect("entry refreshed after edit");
        assert_ne!(
            entry.content_hash,
            crate::file_state_cache::FileStateCache::content_hash(b"fn foo() {}\n"),
            "cached hash must reflect the new content, not the stale one"
        );
    }

    // -----------------------------------------------------------------------
    // #1774: post-edit formatting integration.
    // -----------------------------------------------------------------------
}
