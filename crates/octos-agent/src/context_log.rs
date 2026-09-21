//! Opt-in full-context logging for octos runs (`OCTOS_CONTEXT_LOG=1`).
//!
//! When enabled, EVERY piece of context that flowed through the agent is
//! appended, one JSON document per line, under the workspace's own
//! `.octos/` store:
//!
//! - `llm.jsonl`   — per LLM attempt: the exact `messages` array sent to
//!   the model (system prompt included) plus the parsed response (content,
//!   tool calls, stop reason, token usage) and duration.
//! - `tools.jsonl` — per tool execution: name, arguments, success, output
//!   and duration, captured at the registry dispatch boundary so every
//!   caller (agent loop, approval path, API) is covered exactly once.
//!
//! The output is a replayable record of everything the model saw and did —
//! the debugging substrate for questions like "why did the harness self-check
//! pass while the acceptance spec failed". It is deliberately NOT on by
//! default: a long turn re-serializes the whole (growing) message vector on
//! every iteration, which is exactly the fidelity wanted here and exactly
//! the disk cost nobody wants in normal runs.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU64, Ordering};

use octos_core::Message;
use serde_json::json;

/// Hard cap on a single logged tool output. Tool outputs are already
/// model-bounded (`tool_output_limit`), but shell dumps can still be tens of
/// KB; this keeps one bad command from writing megabytes of log.
const MAX_TOOL_OUTPUT_CHARS: usize = 65_536;

/// Append-only context recorder. Cheap to construct for every agent (a
/// timestamp + pid string); all writes no-op unless the run was enabled via
/// [`ContextLogger::from_env`].
#[derive(Debug, Clone)]
pub struct ContextLogger {
    inner: Option<std::sync::Arc<Inner>>,
}

#[derive(Debug)]
struct Inner {
    dir: OnceLock<PathBuf>,
    run_id: String,
    seq: AtomicU64,
}

impl ContextLogger {
    /// Build a logger enabled iff `OCTOS_CONTEXT_LOG` is one of
    /// `1|true|yes`. Always returns a value so agents can hold it without
    /// Option plumbing; disabled loggers are no-ops.
    pub fn from_env() -> Self {
        let enabled = std::env::var("OCTOS_CONTEXT_LOG")
            .map(|v| matches!(v.trim(), "1" | "true" | "yes"))
            .unwrap_or(false);
        Self::new(enabled)
    }

    /// Test/programmatic constructor.
    pub fn new(enabled: bool) -> Self {
        let inner = enabled.then(|| {
            let run_id = format!(
                "{}-{}",
                chrono::Local::now().format("%Y%m%d-%H%M%S%3f"),
                std::process::id()
            );
            std::sync::Arc::new(Inner {
                dir: OnceLock::new(),
                run_id,
                seq: AtomicU64::new(0),
            })
        });
        Self { inner }
    }

    pub fn is_enabled(&self) -> bool {
        self.inner.is_some()
    }

    /// Directory the records land in — `None` while disabled or before the
    /// first write resolved the workspace root.
    pub fn dir(&self) -> Option<&Path> {
        self.inner
            .as_ref()
            .and_then(|i| i.dir.get())
            .map(|p| p.as_path())
    }

    fn resolve_dir(&self, workspace_root: Option<&Path>) -> Option<&Path> {
        let inner = self.inner.as_ref()?;
        Some(inner.dir.get_or_init(|| {
            let root = workspace_root.unwrap_or_else(|| Path::new("."));
            let dir = root.join(".octos").join("context-logs").join(&inner.run_id);
            if let Err(error) = std::fs::create_dir_all(&dir) {
                tracing::warn!(
                    dir = %dir.display(),
                    error = %error,
                    "context log directory creation failed; context logging disabled for this run"
                );
            }
            dir
        }))
    }

    fn append(&self, workspace_root: Option<&Path>, file: &str, mut record: serde_json::Value) {
        let Some(dir) = self.resolve_dir(workspace_root) else {
            return;
        };
        let inner = self.inner.as_ref().expect("resolve_dir implies inner");
        let seq = inner.seq.fetch_add(1, Ordering::Relaxed);
        record["seq"] = json!(seq);
        record["ts"] = json!(chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string());
        record["run_id"] = json!(inner.run_id);
        let line = record.to_string();
        if let Err(error) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join(file))
            .and_then(|mut f| {
                use std::io::Write;
                f.write_all(line.as_bytes())
                    .and_then(|_| f.write_all(b"\n"))
            })
        {
            tracing::debug!(error = %error, "context log append failed");
        }
    }

    /// Record one LLM attempt: the full request context the model saw.
    ///
    /// `prefix_fingerprint` carries the redacted prompt-cache manifest
    /// (per-segment hashes + estimated tokens) so offline token reports can
    /// compute exact common-prefix reuse between consecutive requests.
    /// `compaction_event` attributes any history rewrite this iteration to a
    /// concrete compaction pass (`agent::compaction::CompactionEvent`);
    /// `None` when no pass fired.
    pub fn log_llm_request(
        &self,
        workspace_root: Option<&Path>,
        iteration: u32,
        attempt: usize,
        model: &str,
        messages: &[Message],
        tool_names: &[String],
        prefix_fingerprint: Option<serde_json::Value>,
        compaction_event: Option<serde_json::Value>,
    ) {
        if !self.is_enabled() {
            return;
        }
        let mut record = json!({
            "kind": "llm_request",
            "iteration": iteration,
            "attempt": attempt,
            "model": model,
            "message_count": messages.len(),
            "messages": messages,
            "tools": tool_names,
        });
        if let Some(fingerprint) = prefix_fingerprint {
            record["prefix_fingerprint"] = fingerprint;
        }
        if let Some(event) = compaction_event.filter(|event| !event.is_null()) {
            record["compaction_event"] = event;
        }
        self.append(workspace_root, "llm.jsonl", record);
    }

    /// Record one LLM attempt's parsed response.
    pub fn log_llm_response(
        &self,
        workspace_root: Option<&Path>,
        iteration: u32,
        attempt: usize,
        response: &octos_llm::ChatResponse,
        streamed: bool,
        duration_ms: u128,
    ) {
        if !self.is_enabled() {
            return;
        }
        self.append(
            workspace_root,
            "llm.jsonl",
            json!({
                "kind": "llm_response",
                "iteration": iteration,
                "attempt": attempt,
                "content": response.content,
                "reasoning_content": response.reasoning_content,
                "tool_calls": response.tool_calls,
                "stop_reason": response.stop_reason,
                "usage": response.usage,
                "provider_index": response.provider_index,
                "streamed": streamed,
                "duration_ms": duration_ms as u64,
            }),
        );
    }

    /// Record one tool execution (registry dispatch boundary).
    pub fn log_tool(
        &self,
        workspace_root: Option<&Path>,
        name: &str,
        args: &serde_json::Value,
        result: &crate::tools::ToolResult,
        duration_ms: u128,
    ) {
        if !self.is_enabled() {
            return;
        }
        let mut output = result.output.clone();
        let mut truncated = false;
        if output.chars().count() > MAX_TOOL_OUTPUT_CHARS {
            output = output.chars().take(MAX_TOOL_OUTPUT_CHARS).collect();
            truncated = true;
        }
        self.append(
            workspace_root,
            "tools.jsonl",
            json!({
                "kind": "tool_call",
                "tool": name,
                "args": args,
                "ok": result.success,
                "output": output,
                "output_truncated": truncated,
                "duration_ms": duration_ms as u64,
            }),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn workspace() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    #[test]
    fn disabled_logger_is_a_full_noop() {
        let ws = workspace();
        let logger = ContextLogger::new(false);
        logger.log_llm_request(Some(ws.path()), 1, 0, "test-model", &[], &[], None, None);
        assert!(!logger.is_enabled());
        assert!(logger.dir().is_none());
        assert!(
            !ws.path().join(".octos/context-logs").exists(),
            "disabled logger must not create directories"
        );
    }

    #[test]
    fn enabled_logger_writes_jsonl_records_under_workspace_octos() {
        let ws = workspace();
        let logger = ContextLogger::new(true);
        assert!(logger.is_enabled());

        // Serde-built so the record shows exactly what a caller would send.
        let messages = vec![
            serde_json::from_str::<Message>(
                r#"{"role":"user","content":"hello","timestamp":"2026-09-16T00:00:00Z"}"#,
            )
            .unwrap(),
        ];
        logger.log_llm_request(
            Some(ws.path()),
            1,
            0,
            "test-model",
            &messages,
            &["shell".to_string()],
            Some(serde_json::json!({
                "schema": "octos.prompt-cache-fingerprint.v1",
                "stable_prefix_hash": "abc",
                "conversation_segments": [
                    {"kind": "user", "hash": "def", "estimated_tokens": 2}
                ]
            })),
            Some(serde_json::json!({
                "path": "preflight_trim",
                "tokens_before": 100,
                "tokens_after": 40,
                "budget_tokens": 60,
                "window_tokens": 128000
            })),
        );
        let response = octos_llm::ChatResponse {
            content: Some("hi".to_string()),
            reasoning_content: None,
            tool_calls: vec![],
            stop_reason: octos_llm::StopReason::EndTurn,
            usage: Default::default(),
            provider_index: None,
        };
        logger.log_llm_response(Some(ws.path()), 1, 0, &response, false, 12);
        logger.log_tool(
            Some(ws.path()),
            "read_file",
            &serde_json::json!({"path": "a.txt"}),
            &crate::tools::ToolResult {
                output: "content".to_string(),
                success: true,
                ..Default::default()
            },
            3,
        );

        let dir = logger.dir().expect("dir resolved on first write");
        assert!(dir.starts_with(ws.path().join(".octos").join("context-logs")));

        let llm = std::fs::read_to_string(dir.join("llm.jsonl")).unwrap();
        let mut lines = llm.lines();
        let req: serde_json::Value = serde_json::from_str(lines.next().unwrap()).unwrap();
        assert_eq!(req["kind"], "llm_request");
        assert_eq!(req["messages"][0]["content"], "hello");
        assert_eq!(req["tools"][0], "shell");
        assert_eq!(req["prefix_fingerprint"]["stable_prefix_hash"], "abc");
        assert_eq!(req["compaction_event"]["path"], "preflight_trim");
        assert_eq!(req["compaction_event"]["window_tokens"], 128000);
        let resp: serde_json::Value = serde_json::from_str(lines.next().unwrap()).unwrap();
        assert_eq!(resp["kind"], "llm_response");
        assert_eq!(resp["stop_reason"], "end_turn");
        assert_eq!(resp["duration_ms"], 12);

        let tools = std::fs::read_to_string(dir.join("tools.jsonl")).unwrap();
        let tool: serde_json::Value = serde_json::from_str(tools.lines().next().unwrap()).unwrap();
        assert_eq!(tool["tool"], "read_file");
        assert_eq!(tool["ok"], true);
        assert_eq!(tool["output"], "content");
        // Every record carries ordering + provenance.
        assert!(tool["seq"].is_u64());
        assert_eq!(tool["run_id"], req["run_id"]);
    }

    #[test]
    fn tool_output_is_capped() {
        let ws = workspace();
        let logger = ContextLogger::new(true);
        let big = "x".repeat(MAX_TOOL_OUTPUT_CHARS + 10);
        logger.log_tool(
            Some(ws.path()),
            "shell",
            &serde_json::json!({}),
            &crate::tools::ToolResult {
                output: big,
                success: true,
                ..Default::default()
            },
            1,
        );
        let dir = logger.dir().unwrap();
        let tools = std::fs::read_to_string(dir.join("tools.jsonl")).unwrap();
        let record: serde_json::Value =
            serde_json::from_str(tools.lines().next().unwrap()).unwrap();
        assert_eq!(record["output_truncated"], true);
        let logged = record["output"].as_str().unwrap().chars().count();
        assert_eq!(logged, MAX_TOOL_OUTPUT_CHARS);
    }

    #[test]
    fn from_env_defaults_to_disabled_without_the_variable() {
        // Env mutation (set_var/remove_var) is process-global AND unsafe in
        // this edition, so from_env's enabled path is covered by
        // ContextLogger::new(true) instead. This only pins the default.
        if std::env::var_os("OCTOS_CONTEXT_LOG").is_none() {
            assert!(!ContextLogger::from_env().is_enabled());
        }
    }
}
