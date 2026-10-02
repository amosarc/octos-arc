//! List directory tool.

use std::path::PathBuf;

use async_trait::async_trait;
use eyre::Result;
use octos_core::PathClassification;
use serde::Deserialize;

use super::{Tool, ToolContext, ToolResult};
use crate::policy::FilesystemScope;

/// List contents of a directory.
pub struct ListDirTool {
    base_dir: PathBuf,
    filesystem_scope: FilesystemScope,
}

impl ListDirTool {
    pub fn new(base_dir: impl Into<PathBuf>) -> Self {
        Self {
            base_dir: base_dir.into(),
            filesystem_scope: FilesystemScope::Workspace,
        }
    }

    pub fn with_filesystem_scope(mut self, filesystem_scope: FilesystemScope) -> Self {
        self.filesystem_scope = filesystem_scope;
        self
    }
}

#[derive(Deserialize)]
// #1770: unknown keys are usually a typo of a real parameter; rejecting
// them (with a did-you-mean via `args::parse_tool_args`) lets the model
// self-correct instead of silently dropping its intent.
#[serde(deny_unknown_fields)]
struct Input {
    path: String,
    /// Maximum entries to return (default 500).
    #[serde(default)]
    limit: Option<usize>,
}

/// Default cap on returned entries. Without it a huge directory rode into
/// the loop-layer 50K blind cut with no recovery advice (pi caps at 500 and
/// says so in the result).
const DEFAULT_ENTRY_LIMIT: usize = 500;

#[async_trait]
impl Tool for ListDirTool {
    fn name(&self) -> &str {
        "list_dir"
    }

    fn description(&self) -> &str {
        "List the contents of a directory. Returns at most `limit` entries (default 500)."
    }

    fn tags(&self) -> &[&str] {
        &["search", "code"]
    }

    fn input_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list (relative to working directory)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 500)"
                }
            },
            "required": ["path"]
        })
    }

    fn truncation_recovery(
        &self,
        args: &serde_json::Value,
        omitted_bytes: usize,
    ) -> Option<String> {
        let _ = args;
        Some(format!(
            "[{omitted_bytes} bytes omitted] Directory listing exceeded the tool-result cap. \
             Narrow the path to a subdirectory, or page through with shell \
             `ls <dir> | sed -n 'N,Mp'`."
        ))
    }

    async fn execute(&self, args: &serde_json::Value) -> Result<ToolResult> {
        // PR-B: legacy entry point routes through the typed path with a
        // zero-value context so out-of-band callers exercise the same
        // SessionScope-aware logic when no scope is wired.
        self.execute_with_context(&ToolContext::zero(), args).await
    }

    async fn execute_with_context(
        &self,
        ctx: &ToolContext,
        args: &serde_json::Value,
    ) -> Result<ToolResult> {
        let input: Input = super::args::parse_tool_args(self.name(), &self.input_schema(), args)?;

        // ARC guard: host trace/debug subset under .arc is not
        // agent-consumable (OCTOS_DENY_TRACE_READ; see shell.rs).
        if let Some(refusal) = super::shell::trace_read_guard_path(&input.path) {
            return Ok(ToolResult {
                output: refusal,
                success: false,
                ..Default::default()
            });
        }

        // Interim mitigation (#1378, superseded by #1377): `up/<base64>/<name>`
        // is an opaque upload handle, not a browsable directory. When the input
        // targets that namespace and doesn't resolve to a real listable entry,
        // return guidance pointing the model at `read_file` instead of a
        // misleading "Directory not found" (observed to make models — incl.
        // gemini-2.5-pro on replay — give up on a file they can read). The gate
        // (shared with `glob`) redirects only for a genuine upload handle OR
        // when there's no real `up/` directory, so a repo that uses `up/` as a
        // normal folder keeps its normal behaviour.
        //
        // CRITICAL (codex round-6 P3): this substitution is applied ONLY on the
        // not-found / not-a-dir exits — NEVER on a resolver error. A traversal /
        // out-of-scope violation (`up/../secret`) must surface as the real
        // "Path outside session scope" error, not be masked as upload confusion.
        let ws_root = ctx
            .session_scope
            .as_ref()
            .map(|s| s.workspace().to_path_buf())
            .unwrap_or_else(|| self.base_dir.clone());
        let up_guidance = super::upload_namespace_redirect(&input.path, &ws_root);
        let fail = |orig: String| -> ToolResult {
            ToolResult {
                output: up_guidance.map(str::to_string).unwrap_or(orig),
                success: false,
                ..Default::default()
            }
        };

        // PR-B: when the host has threaded a `SessionScope` through
        // `ToolContext`, validate the directory against it (same
        // classification semantics as `read_file` — InWorkspace,
        // InGrantedDir, InSharedZone, and InSkillDir all permit reads).
        // No scope wired => keep the legacy `base_dir + FilesystemScope`
        // path for backward compatibility with `octos chat`.
        // Resolver errors are returned VERBATIM (no upload guidance) so policy
        // violations are reported as such.
        let target = match ctx.session_scope.as_ref() {
            Some(scope) => match super::resolve_path_for_session_scope_read(scope, &input.path) {
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

        if let Some(r) = super::reject_symlink(&target).await {
            return Ok(r);
        }

        if !target.exists() {
            return Ok(fail(format!("Error: Directory not found: {}", input.path)));
        }

        if !target.is_dir() {
            return Ok(fail(format!("Error: Not a directory: {}", input.path)));
        }

        let mut entries = match tokio::fs::read_dir(&target).await {
            Ok(entries) => entries,
            Err(e) => {
                return Ok(ToolResult {
                    output: format!("Error: {e}"),
                    success: false,
                    ..Default::default()
                });
            }
        };

        // PR-B: when a SessionScope is present, drop entries whose
        // canonicalised path classifies as `OutOfScope`. This catches
        // the case where the listed directory contains a symlink that
        // points out of every declared zone — without the filter, the
        // bare entry name would leak the symlink's existence even if
        // following it would be refused at open time.
        let scope_filter = ctx.session_scope.as_ref();

        let mut dirs = Vec::new();
        let mut files = Vec::new();
        let entry_limit = input.limit.unwrap_or(DEFAULT_ENTRY_LIMIT).max(1);
        let mut entry_limit_reached = false;

        while let Ok(Some(entry)) = entries.next_entry().await {
            if dirs.len() + files.len() >= entry_limit {
                entry_limit_reached = true;
                break;
            }
            let entry_path = entry.path();
            if let Some(scope) = scope_filter {
                if matches!(
                    scope.classify_canonical_path(&entry_path),
                    PathClassification::OutOfScope
                ) {
                    continue;
                }
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if entry_path.is_dir() {
                dirs.push(name);
            } else {
                files.push(name);
            }
        }

        dirs.sort();
        files.sort();

        if dirs.is_empty() && files.is_empty() {
            return Ok(ToolResult {
                output: format!("Directory {} is empty.", input.path),
                success: true,
                ..Default::default()
            });
        }

        let mut out = String::new();
        for d in &dirs {
            out.push_str(&format!("[dir]  {d}\n"));
        }
        for f in &files {
            out.push_str(&format!("[file] {f}\n"));
        }

        let note = if entry_limit_reached {
            format!(
                "\n\n[{entry_limit} entries limit reached — raise limit to {} or narrow the path]",
                entry_limit * 2
            )
        } else {
            String::new()
        };

        Ok(ToolResult {
            output: format!(
                "{} entries in {}:\n{}{}",
                dirs.len() + files.len(),
                input.path,
                out.trim_end(),
                note
            ),
            success: true,
            ..Default::default()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use tempfile::TempDir;

    use crate::tools::test_fixture::{escape_ctx_with_skill_zones, scoped_ctx};

    #[tokio::test]
    async fn test_list_dir() {
        let dir = TempDir::new().unwrap();
        std::fs::create_dir(dir.path().join("subdir")).unwrap();
        std::fs::write(dir.path().join("file.txt"), "hello").unwrap();

        let tool = ListDirTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({"path": "."}))
            .await
            .unwrap();
        assert!(result.success);
        assert!(result.output.contains("[dir]  subdir"));
        assert!(result.output.contains("[file] file.txt"));
    }

    #[tokio::test]
    async fn list_dir_entry_limit_truncates_with_advice() {
        // An entry-capped listing must say so and offer the next step —
        // otherwise the model cannot tell a capped listing from a complete
        // one.
        let dir = TempDir::new().unwrap();
        for i in 0..10 {
            std::fs::write(dir.path().join(format!("f{i:02}.txt")), "x").unwrap();
        }

        let tool = ListDirTool::new(dir.path());
        let result = tool
            .execute(&serde_json::json!({"path": ".", "limit": 5}))
            .await
            .unwrap();

        assert!(result.success);
        assert!(
            result.output.contains("5 entries in"),
            "expected the capped header, got: {}",
            result.output
        );
        assert!(
            result.output.contains("entries limit reached"),
            "expected the limit note, got: {}",
            result.output
        );
        assert!(
            result.output.contains("raise limit to 10"),
            "expected the raise-limit advice, got: {}",
            result.output
        );
    }

    // ------------------------------------------------------------------
    // PR-B: SessionScope integration tests for ListDirTool.
    // ------------------------------------------------------------------

    use octos_core::SessionScope;

    #[tokio::test]
    async fn list_dir_on_upload_handle_namespace_returns_guidance() {
        // #1378: list_dir('up') / list_dir('up/<handle>') must not return the
        // misleading "Directory not found"; it returns guidance pointing the
        // model at read_file so it stops looping into "please re-upload".
        let workspace = TempDir::new().unwrap();
        let scope = SessionScope::solo(workspace.path().to_path_buf(), vec![]).unwrap();
        let tool = ListDirTool::new(workspace.path());
        let ctx = scoped_ctx("list-dir-with-scope", scope);

        for p in ["up", "up/MDE5_handle/report.md"] {
            let result = tool
                .execute_with_context(&ctx, &serde_json::json!({ "path": p }))
                .await
                .unwrap();
            assert!(!result.success, "expected non-success for {p:?}");
            assert!(
                result.output.contains("opaque upload handle")
                    && result.output.contains("read_file"),
                "expected upload guidance for {p:?}, got: {}",
                result.output
            );
            assert!(
                !result.output.contains("Directory not found"),
                "must NOT return the misleading not-found for {p:?}"
            );
        }
    }

    #[tokio::test]
    async fn list_dir_outside_workspace_and_skill_zones_refused() {
        // A dir entirely outside every declared zone classifies as
        // `OutOfScope` and must be refused.
        let skill = TempDir::new().unwrap();
        let (workspace, outside, ctx) =
            escape_ctx_with_skill_zones("list-dir-with-scope", &[skill.path()]);

        let tool = ListDirTool::new(workspace.path());

        let result = tool
            .execute_with_context(
                &ctx,
                &serde_json::json!({"path": outside.path().to_string_lossy()}),
            )
            .await
            .unwrap();
        assert!(!result.success);
        assert!(
            result.output.contains("outside session scope"),
            "expected scope rejection, got: {}",
            result.output
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn list_dir_drops_symlinked_entries_pointing_out_of_scope() {
        // A symlink inside the skill_dir pointing to /tmp must not
        // leak the target's contents through `list_dir`. The
        // canonical-classify guard returns `OutOfScope` for the
        // symlink target so the entry is dropped.
        use std::os::unix::fs::symlink;

        let skill = TempDir::new().unwrap();
        let (workspace, outside, ctx) =
            escape_ctx_with_skill_zones("list-dir-with-scope", &[skill.path()]);
        std::fs::write(outside.path().join("secret"), "exfil").unwrap();

        // Real entry inside the skill_dir (should remain).
        std::fs::write(skill.path().join("real.txt"), "ok").unwrap();
        // Symlink pointing outside every declared zone (should be dropped).
        symlink(outside.path(), skill.path().join("escape")).unwrap();

        let tool = ListDirTool::new(workspace.path());

        let result = tool
            .execute_with_context(
                &ctx,
                &serde_json::json!({"path": skill.path().to_string_lossy()}),
            )
            .await
            .unwrap();
        assert!(result.success, "expected success, got: {}", result.output);
        assert!(
            result.output.contains("real.txt"),
            "expected real entry retained, got: {}",
            result.output
        );
        assert!(
            !result.output.contains("escape"),
            "symlinked entry pointing out-of-scope must be dropped, got: {}",
            result.output
        );
    }
}
