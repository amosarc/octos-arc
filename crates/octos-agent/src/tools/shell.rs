//! Shell tool for executing commands.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use eyre::Result;
use serde::Deserialize;
use tokio::time::timeout;

use super::{
    ConcurrencyClass, TOOL_APPROVAL_CTX, TOOL_CTX, Tool, ToolApprovalDecision, ToolApprovalRequest,
    ToolContext, ToolResult,
};
use crate::policy::{ApprovalPolicy, CommandPolicy, Decision, SafePolicy};
use crate::sandbox::{NoSandbox, Sandbox};
use crate::subprocess_env::{EnvAllowlist, sanitize_command_env};
use crate::task_supervisor::TaskSupervisor;
use crate::tools::policy::BashFileWrites;

/// Monotonic sequence used to synthesise a `tool_call_id` for background shell
/// tasks when the caller did not thread one through `ToolContext` (e.g. the
/// legacy `execute()` entry point used by tests). Production callers always
/// carry a real tool id, so this only fires off the hot path.
static SHELL_BG_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Artifact offload threshold: outputs above this many BYTES spill to
/// disk. 3KB keeps routine command echo (ls, git status, curl one-liners)
/// inline while catching test-runner/build spew early — ARC replay traces
/// showed multi-KB test logs riding along in history at full replay cost
/// under the old 8KB line.
const ARTIFACT_SPILL_BYTES: usize = 3 * 1024;
/// Inline head/tail sizes for a spilled output (bytes).
const ARTIFACT_HEAD_BYTES: usize = 2048;
const ARTIFACT_TAIL_BYTES: usize = 1024;

pub(crate) fn artifact_offload_enabled() -> bool {
    match std::env::var("OCTOS_TOOL_ARTIFACT") {
        Ok(v) => v != "0",
        Err(_) => true,
    }
}

/// Spill threshold with `OCTOS_TOOL_ARTIFACT_BYTES` numeric override
/// (invalid values fall back to the default — a typo must not silently
/// disable offloading).
fn artifact_spill_bytes() -> usize {
    std::env::var("OCTOS_TOOL_ARTIFACT_BYTES")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|bytes| *bytes > 0)
        .unwrap_or(ARTIFACT_SPILL_BYTES)
}

/// Offload a large shell output to `<workspace>/.octos/artifacts/` and
/// return the compact head+tail+pointer replacement. `None` = keep the
/// text inline (below threshold, feature off, or the workspace is not
/// writable — failure NEVER blocks the command result).
pub(crate) fn artifact_offload(workspace: &Path, text: &str) -> Option<String> {
    if text.len() <= artifact_spill_bytes() {
        return None;
    }
    let seq = SHELL_BG_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let dir = workspace.join(".octos").join("artifacts");
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join(format!("shell-{stamp}-{seq}.log"));
    std::fs::write(&path, text).ok()?;
    // Find a UTF-8 boundary at/after the head offset (the text is valid
    // UTF-8; slicing near the boundary then trimming a partial char is
    // enough — floor to a char boundary for both cuts).
    let head = floor_char_boundary(text, ARTIFACT_HEAD_BYTES);
    let tail_from = ceil_char_boundary(text, text.len().saturating_sub(ARTIFACT_TAIL_BYTES));
    Some(format!(
        "{}\n... [{} bytes spilled to {} — re-read with a file tool if you need the middle] ...\n{}\n(artifact: {})",
        &text[..head],
        text.len(),
        path.display(),
        &text[tail_from..],
        path.display(),
    ))
}

fn floor_char_boundary(s: &str, i: usize) -> usize {
    let mut i = i.min(s.len());
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

fn ceil_char_boundary(s: &str, i: usize) -> usize {
    let mut i = i.min(s.len());
    while i < s.len() && !s.is_char_boundary(i) {
        i += 1;
    }
    i
}

/// Tool for executing shell commands.
pub struct ShellTool {
    /// Timeout for command execution.
    timeout: Duration,
    /// Working directory for commands.
    cwd: std::path::PathBuf,
    /// Policy for command approval.
    policy: Arc<dyn CommandPolicy>,
    /// #28b — loaded ONCE at construction (never re-read per call).
    bash_file_writes: crate::tools::policy::BashFileWrites,
    /// Runtime approval behavior for commands that request approval.
    approval_policy: ApprovalPolicy,
    /// Sandbox for command isolation.
    sandbox: Arc<dyn Sandbox>,
    /// Optional shared task supervisor. When set (or supplied via
    /// [`ToolContext::task_supervisor`]), background shell commands — a
    /// trailing `&` or an explicit `background: true` arg — are registered as
    /// tracked tasks so they surface in `/ps` and the sub-agent dock. Mirrors
    /// [`crate::tools::spawn::SpawnTool`]'s supervisor wiring.
    task_supervisor: Option<Arc<TaskSupervisor>>,
    /// Session key used to tag registered background tasks (links the task to
    /// its owning session in `/ps`). Falls back to
    /// [`ToolContext::parent_session_key`] when unset.
    session_key: Option<String>,
    /// Optional hard CEILING on a single command's effective timeout. When set,
    /// the per-command timeout is `min(requested-or-default, max_timeout)` — an
    /// upper bound that overrides a LARGER LLM-provided `timeout_secs`, so a
    /// closed worker can guarantee no foreground command outlives its attempt
    /// deadline. `None` = no extra cap (only the outer `[1, 600]s` clamp
    /// applies) — unchanged behaviour for every existing caller.
    max_timeout: Option<Duration>,
}

impl ShellTool {
    /// Create a new shell tool with safe defaults.
    pub fn new(cwd: impl Into<std::path::PathBuf>) -> Self {
        Self {
            timeout: Duration::from_secs(120),
            cwd: cwd.into(),
            policy: Arc::new(SafePolicy::default()),
            bash_file_writes: BashFileWrites::default(),
            approval_policy: ApprovalPolicy::Ask,
            sandbox: Arc::new(NoSandbox),
            task_supervisor: None,
            session_key: None,
            max_timeout: None,
        }
    }

    /// Cap the effective per-command timeout at `cap_secs` (a hard CEILING).
    ///
    /// The effective timeout becomes `min(requested-or-default, cap_secs)`, so
    /// this overrides a LARGER LLM-provided `timeout_secs` (which the agent
    /// loop can otherwise raise up to the outer 600s clamp). A closed worker
    /// sets this to its attempt deadline so no single foreground command can
    /// outlive the deadline. `cap_secs` is floored to 1. Default: no extra cap.
    pub fn with_max_timeout_secs(mut self, cap_secs: u64) -> Self {
        self.max_timeout = Some(Duration::from_secs(cap_secs.max(1)));
        self
    }

    /// Set a custom command policy.
    /// #28b — set the bash file-writes knob (defaults to `Allow`).
    pub fn with_bash_file_writes(mut self, mode: crate::tools::policy::BashFileWrites) -> Self {
        self.bash_file_writes = mode;
        self
    }

    pub fn with_policy(mut self, policy: Arc<dyn CommandPolicy>) -> Self {
        self.policy = policy;
        self
    }

    /// Set the runtime approval behavior.
    pub fn with_approval_policy(mut self, approval_policy: ApprovalPolicy) -> Self {
        self.approval_policy = approval_policy;
        self
    }

    /// Set a sandbox for command isolation.
    pub fn with_sandbox(mut self, sandbox: Box<dyn Sandbox>) -> Self {
        self.sandbox = Arc::from(sandbox);
        self
    }

    /// Set a shared sandbox for command isolation.
    pub fn with_shared_sandbox(mut self, sandbox: Arc<dyn Sandbox>) -> Self {
        self.sandbox = sandbox;
        self
    }

    /// Register background shell commands (a trailing `&` or an explicit
    /// `background: true` arg) as tracked tasks in the shared
    /// [`TaskSupervisor`] so they surface in `/ps` and the sub-agent dock.
    ///
    /// Mirrors [`crate::tools::spawn::SpawnTool::with_task_supervisor`]: the
    /// same supervisor + session key pair. In
    /// production the foreground executor already threads the SSOT supervisor
    /// and session key onto every tool call via
    /// [`ToolContext::task_supervisor`] / [`ToolContext::parent_session_key`],
    /// so this builder is primarily for explicit construction and tests; at
    /// execute time the explicit handle wins and the context is the fallback.
    pub fn with_task_supervisor(
        mut self,
        supervisor: Arc<TaskSupervisor>,
        session_key: impl Into<String>,
    ) -> Self {
        self.task_supervisor = Some(supervisor);
        self.session_key = Some(session_key.into());
        self
    }

    /// Run a background command detached and return immediately.
    ///
    /// The command still runs through the same sandbox/env path as a
    /// foreground command (policy/approval were already enforced by the
    /// caller). When a supervisor is available — the explicit builder handle
    /// or [`ToolContext::task_supervisor`] — the command is registered as a
    /// tracked task (status `running`) and a lightweight watcher flips it to
    /// terminal (`completed`/`failed`) when the child exits, so `/ps` shows
    /// the running→done transition. Without a supervisor the command still
    /// runs detached and is reaped, but is untracked.
    async fn execute_background(
        &self,
        ctx: &ToolContext,
        raw_command: &str,
        effective_cwd: &Path,
    ) -> ToolResult {
        // Strip the trailing `&` so OUR child is the actual work (see
        // `strip_trailing_ampersand`).
        let command = strip_trailing_ampersand(raw_command);

        let mut cmd = self.sandbox.wrap_command(&command, effective_cwd);
        // We return immediately and never read the pipes, so piping stdout/
        // stderr risks a full-buffer deadlock in a chatty child. Discard the
        // std streams (in-command redirects like `> log 2>&1` still win).
        cmd.stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        apply_frontend_tool_env(&mut cmd, effective_cwd);
        apply_quarto_tool_env(&mut cmd, &command, effective_cwd);
        apply_git_tool_env(&mut cmd, &command);
        sanitize_command_env(&mut cmd, &EnvAllowlist::empty());
        apply_harness_event_sink_env(&mut cmd, ctx);

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                return ToolResult {
                    output: format!("Failed to start background command: {e}"),
                    success: false,
                    ..Default::default()
                };
            }
        };
        let child_pid = child.id();
        let label = background_label(&command);

        // Resolve the supervisor + session key: an explicit builder handle
        // wins, else fall back to the per-turn ToolContext (the foreground
        // executor threads the SSOT supervisor here).
        let supervisor = self
            .task_supervisor
            .clone()
            .or_else(|| ctx.task_supervisor.clone());
        let session_key = self
            .session_key
            .clone()
            .or_else(|| ctx.parent_session_key.clone());

        // Register a tracked task (mirrors spawn's `register_with_lineage`).
        let task_id = supervisor.as_ref().map(|sup| {
            let tool_call_id = if ctx.tool_id.is_empty() {
                let seq = SHELL_BG_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                format!("shell-bg-{seq}")
            } else {
                ctx.tool_id.clone()
            };
            sup.register_with_lineage(&label, &tool_call_id, session_key.as_deref())
        });

        match (supervisor, task_id) {
            // Tracked: spawn a watcher that flips the task terminal on exit.
            (Some(sup), Some(id)) if !id.is_empty() => {
                // Flip Spawned → Running (the child is already executing), so
                // `/ps` shows an active task, mirroring the spawn tool.
                sup.mark_running(&id);
                let watch_id = id.clone();
                let watch_label = label.clone();
                tokio::spawn(async move {
                    let mut child = child;
                    match child.wait().await {
                        Ok(status) if status.success() => sup.mark_completed(&watch_id, vec![]),
                        Ok(status) => sup.mark_failed(
                            &watch_id,
                            format!(
                                "{watch_label} exited with status {}",
                                status.code().unwrap_or(-1)
                            ),
                        ),
                        Err(e) => sup.mark_failed(
                            &watch_id,
                            format!("failed to wait on background command: {e}"),
                        ),
                    }
                });
                ToolResult {
                    output: format!(
                        "Started background task {id} (pid {}): {label}",
                        child_pid
                            .map(|p| p.to_string())
                            .unwrap_or_else(|| "?".to_string())
                    ),
                    success: true,
                    ..Default::default()
                }
            }
            // Untracked (no supervisor, or fan-out cap refused the register):
            // still run detached, but reap the child so it doesn't zombie.
            _ => {
                tokio::spawn(async move {
                    let mut child = child;
                    let _ = child.wait().await;
                });
                ToolResult {
                    output: format!(
                        "Started background command (untracked, pid {}): {label}",
                        child_pid
                            .map(|p| p.to_string())
                            .unwrap_or_else(|| "?".to_string())
                    ),
                    success: true,
                    ..Default::default()
                }
            }
        }
    }
}

fn frontend_tool_cache_dir(cwd: &Path) -> PathBuf {
    let user = std::env::var("USER")
        .or_else(|_| std::env::var("USERNAME"))
        .unwrap_or_else(|_| "unknown".to_string());
    let cache_key = cwd
        .to_string_lossy()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' })
        .collect::<String>();
    let preferred = std::env::temp_dir()
        .join("octos-frontend-tool-cache")
        .join(user)
        .join(cache_key);
    let _ = std::fs::create_dir_all(&preferred);
    preferred
}

fn apply_frontend_tool_env(cmd: &mut tokio::process::Command, cwd: &Path) {
    let cache_dir = frontend_tool_cache_dir(cwd);
    cmd.env("ASTRO_TELEMETRY_DISABLED", "1")
        .env("NPM_CONFIG_CACHE", &cache_dir)
        .env("npm_config_cache", &cache_dir);
}

/// True when `command` runs the `quarto` CLI as a command (not merely
/// mentions it in a path). Splitting on shell separators means
/// `cd sites/foo && quarto render` is detected via its `quarto render`
/// segment.
///
/// A segment's leading `NAME=value` env-assignment prefixes are skipped
/// before testing the command word, so `QUARTO_PROJECT_DIR=. quarto
/// render` is still recognised — without this, the inline-env form left
/// HOME unredirected and quarto's sass cache escaped the sandbox
/// (`unable to open database file: …sass.kv`).
fn command_invokes_quarto(command: &str) -> bool {
    command
        .split(['\n', ';', '&', '|'])
        .map(str::trim)
        .any(segment_runs_quarto)
}

/// True when the first command word of a single shell segment is
/// `quarto`, after skipping any leading `NAME=value` env assignments.
fn segment_runs_quarto(segment: &str) -> bool {
    segment
        .split_whitespace()
        .find(|token| !is_env_assignment(token))
        == Some("quarto")
}

/// True for a `NAME=value` shell env-assignment token (the form that may
/// legally precede a command word, e.g. `FOO=bar cmd`).
fn is_env_assignment(token: &str) -> bool {
    match token.split_once('=') {
        Some((name, _)) => {
            !name.is_empty()
                && name.chars().enumerate().all(|(i, c)| {
                    c == '_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit())
                })
        }
        None => false,
    }
}

/// Directory used as `$HOME` for quarto invocations. Quarto writes its
/// sass cache to `$HOME/Library/Caches/quarto` and ignores
/// `QUARTO_CACHE`/`XDG_CACHE_HOME` on macOS, so under the sandbox
/// (writes confined to cwd) the default home cache is denied and
/// `quarto render` fails with `unable to open database file: …sass.kv`.
/// Pointing HOME at a dir inside the (writable) workspace keeps the
/// cache contained without weakening sandbox isolation.
fn quarto_home_dir(cwd: &Path) -> PathBuf {
    cwd.join(".octos-quarto-home")
}

fn apply_quarto_tool_env(cmd: &mut tokio::process::Command, command: &str, cwd: &Path) {
    if !command_invokes_quarto(command) {
        return;
    }
    let home = quarto_home_dir(cwd);
    let _ = std::fs::create_dir_all(&home);
    cmd.env("HOME", &home);
}

#[cfg(windows)]
const NULL_DEVICE_PATH: &str = "NUL";
#[cfg(not(windows))]
const NULL_DEVICE_PATH: &str = "/dev/null";

fn contains_git_invocation(command: &str) -> bool {
    shell_command_segments(command)
        .iter()
        .any(|segment| segment_invokes_git(segment))
}

fn shell_command_segments(command: &str) -> Vec<Vec<String>> {
    let mut segments = Vec::new();
    let mut segment = Vec::new();
    let mut token = String::new();
    let mut quote = None;
    let mut chars = command.chars().peekable();

    while let Some(ch) = chars.next() {
        if let Some(quote_ch) = quote {
            if ch == quote_ch {
                quote = None;
            } else if quote_ch == '"' && ch == '\\' {
                if let Some(&next) = chars.peek() {
                    if matches!(next, '"' | '\\' | '$' | '`' | '\n') {
                        token.push(chars.next().expect("peeked char exists"));
                    } else {
                        token.push(ch);
                    }
                } else {
                    token.push(ch);
                }
            } else {
                token.push(ch);
            }
            continue;
        }

        match ch {
            '\'' | '"' => quote = Some(ch),
            '\\' => {
                if let Some(&next) = chars.peek() {
                    if is_shell_token_separator(next) || matches!(next, '\'' | '"' | '\\') {
                        token.push(chars.next().expect("peeked char exists"));
                    } else {
                        token.push(ch);
                    }
                } else {
                    token.push(ch);
                }
            }
            '\n' | ';' | '&' | '|' | '(' | ')' => {
                push_shell_token(&mut segment, &mut token);
                push_shell_segment(&mut segments, &mut segment);
            }
            ch if ch.is_whitespace() => push_shell_token(&mut segment, &mut token),
            _ => token.push(ch),
        }
    }

    push_shell_token(&mut segment, &mut token);
    push_shell_segment(&mut segments, &mut segment);
    segments
}

fn is_shell_token_separator(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, ';' | '&' | '|' | '(' | ')')
}

fn push_shell_token(segment: &mut Vec<String>, token: &mut String) {
    if !token.is_empty() {
        segment.push(std::mem::take(token));
    }
}

fn push_shell_segment(segments: &mut Vec<Vec<String>>, segment: &mut Vec<String>) {
    if !segment.is_empty() {
        segments.push(std::mem::take(segment));
    }
}

fn segment_invokes_git(segment: &[String]) -> bool {
    let mut index = 0;
    while let Some(token) = segment.get(index) {
        if token_invokes_git(token) {
            return true;
        }
        if looks_like_env_assignment(token) {
            index += 1;
            continue;
        }
        if is_git_invocation_wrapper(token) {
            index += 1;
            while segment.get(index).is_some_and(|wrapped| {
                wrapped.starts_with('-') || looks_like_env_assignment(wrapped)
            }) {
                index += 1;
            }
            continue;
        }
        return false;
    }
    false
}

fn token_invokes_git(token: &str) -> bool {
    if looks_like_env_assignment(token) {
        return false;
    }
    let basename = command_basename(token);
    basename.eq_ignore_ascii_case("git") || basename.eq_ignore_ascii_case("git.exe")
}

fn is_git_invocation_wrapper(token: &str) -> bool {
    const WRAPPERS: &[&str] = &[
        "env",
        "env.exe",
        "time",
        "time.exe",
        "sudo",
        "sudo.exe",
        "npx",
        "npx.exe",
        "command",
        "command.exe",
        "exec",
        "exec.exe",
    ];

    let basename = command_basename(token);
    WRAPPERS
        .iter()
        .any(|wrapper| basename.eq_ignore_ascii_case(wrapper))
}

fn command_basename(token: &str) -> &str {
    token.rsplit(['/', '\\']).next().unwrap_or(token)
}

fn looks_like_env_assignment(token: &str) -> bool {
    let Some((name, _value)) = token.split_once('=') else {
        return false;
    };
    !name.is_empty()
        && name
            .chars()
            .all(|ch| ch == '_' || ch.is_ascii_alphanumeric())
}

fn apply_git_tool_env(cmd: &mut tokio::process::Command, command: &str) {
    if contains_git_invocation(command) {
        cmd.env("GIT_CONFIG_GLOBAL", NULL_DEVICE_PATH)
            .env("GIT_CONFIG_NOSYSTEM", "1");
    }
}

fn apply_harness_event_sink_env(cmd: &mut tokio::process::Command, ctx: &ToolContext) {
    if let Some(sink) = ctx.harness_event_sink.as_deref() {
        cmd.env("OCTOS_EVENT_SINK", sink);
        return;
    }
    // Legacy callers that route through `execute()` pass `ToolContext::zero()` —
    // the sink isn't on the typed context but may still live on the
    // task-local `TOOL_CTX` that older executor paths populate.
    if let Ok(Some(sink)) = TOOL_CTX.try_with(|inner| inner.harness_event_sink.clone()) {
        cmd.env("OCTOS_EVENT_SINK", sink);
    }
}

#[derive(Debug, Deserialize)]
// #1770: unknown keys are usually a typo of a real parameter; rejecting
// them (with a did-you-mean via `args::parse_tool_args`) lets the model
// self-correct instead of silently dropping its intent.
#[serde(deny_unknown_fields)]
struct ShellInput {
    command: String,
    #[serde(default)]
    timeout_secs: Option<u64>,
    /// Explicit request to run the command detached (in addition to the
    /// trailing-`&` heuristic). When true, the command is registered as a
    /// tracked background task and control returns immediately.
    #[serde(default)]
    background: Option<bool>,
}

/// True when the shell command should run in the background: either an
/// explicit `background: true` arg, or a trailing `&` (the command backgrounds
/// itself). A trailing `&&` is the logical-AND operator, not a background
/// request, so it is excluded.
fn is_background_command(command: &str, explicit: Option<bool>) -> bool {
    explicit == Some(true) || has_trailing_ampersand(command)
}

/// True when `command` ends with a single background `&` (ignoring trailing
/// whitespace) that is not part of a `&&` operator.
fn has_trailing_ampersand(command: &str) -> bool {
    let trimmed = command.trim_end();
    trimmed.ends_with('&') && !trimmed.ends_with("&&")
}

/// Strip a single trailing `&` (and surrounding whitespace) so the command
/// runs as OUR detached child rather than being re-backgrounded by the wrapper
/// shell — which would `fork` the real work, exit immediately, and orphan the
/// grandchild (reparented to PID 1), defeating lifecycle tracking. With the
/// `&` removed, our `sh -c "<cmd>"` child IS the work, so the watcher's
/// `child.wait()` observes the real completion.
fn strip_trailing_ampersand(command: &str) -> String {
    let trimmed = command.trim_end();
    if let Some(stripped) = trimmed.strip_suffix('&') {
        let stripped = stripped.trim_end();
        // Guard against `&&` (invalid at end anyway): only strip a lone `&`.
        if !stripped.ends_with('&') {
            return stripped.to_string();
        }
    }
    command.to_string()
}

/// Build a short, single-line human label for a background command, used as
/// the supervisor task's display name in `/ps`. Mirrors how the spawn tool
/// passes a human label as the registered task's `tool_name`.
fn background_label(command: &str) -> String {
    let one_line: String = command.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut label = String::from("shell: ");
    const MAX: usize = 80;
    if one_line.chars().count() > MAX {
        label.extend(one_line.chars().take(MAX));
        label.push('…');
    } else {
        label.push_str(&one_line);
    }
    label
}

#[async_trait]
impl Tool for ShellTool {
    fn name(&self) -> &str {
        "shell"
    }

    fn description(&self) -> &str {
        "Execute a shell command and return the output. Use this to run tests, build code, or interact with the filesystem."
    }

    fn tags(&self) -> &[&str] {
        &["runtime", "code"]
    }

    fn concurrency_class(&self) -> ConcurrencyClass {
        // Shell commands can mutate the filesystem or spawn long-lived
        // processes. Running them in parallel with other tool calls races
        // observable state (e.g. `shell: rm foo` vs `read_file foo/x`), so
        // shell runs in the serialized Exclusive phase — after every Safe
        // sibling in the batch has completed. See M8.8 and #1766.
        ConcurrencyClass::Exclusive
    }

    fn input_schema(&self) -> serde_json::Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "timeout_secs": {
                    "type": "integer",
                    "description": "Optional timeout in seconds (default: 120)"
                },
                "background": {
                    "type": "boolean",
                    "description": "Run the command detached in the background. A trailing `&` also backgrounds the command. Background commands are tracked as tasks (visible in `/ps`) and return immediately."
                }
            },
            "required": ["command"]
        })
    }

    async fn execute(&self, args: &serde_json::Value) -> Result<ToolResult> {
        // Legacy entry point: route through the typed path with a zero-value
        // context so out-of-band callers (tests, `ToolRegistry::execute`)
        // exercise the same Phase 2-D scope resolution as migrated callers.
        self.execute_with_context(&ToolContext::zero(), args).await
    }

    async fn execute_with_context(
        &self,
        ctx: &ToolContext,
        args: &serde_json::Value,
    ) -> Result<ToolResult> {
        let input: ShellInput =
            super::args::parse_tool_args(self.name(), &self.input_schema(), args)?;

        // Phase 2-D of the SessionScope migration: when the host has
        // threaded a scope through `ToolContext`, prefer the scope's
        // workspace so every tool in the session shares the single
        // filesystem contract.
        //
        // Codex P1 (round-1, this PR): respect a hinted workspace
        // override. `SessionRuntime` builds `session_scope` from
        // `<data_dir>/users/<id>/workspace` independent of any
        // `workspace_hint` supplied by the coding-agent flow, while
        // the registry rebinds every tool's `cwd` to the *hinted*
        // workspace via `with_workspace_root`. If `self.cwd` differs
        // from `scope.workspace()`, the caller deliberately pointed
        // this tool at a different workspace — honour that and keep
        // `self.cwd`. Otherwise the migration is a no-op for the
        // hinted-coding-agent path until Phase 3 reconciles
        // SessionScope construction with the hinted workspace.
        //
        // The same effective CWD also feeds the `CommandPolicy::check`
        // call so a policy that consults the working directory sees a
        // consistent value with what the child process will observe.
        let effective_cwd: &Path = match ctx.session_scope.as_ref() {
            Some(scope) if scope.workspace() == self.cwd.as_path() => scope.workspace(),
            _ => &self.cwd,
        };
        // #28b — deny knob: heuristic pre-screen of the command TEXT for
        // write-shaped shell usage. False negatives are tolerated (the 28a
        // receipt still shows what actually changed); false positives
        // escape via a trailing `# octos:allow-write` comment on the
        // command line (documented escape hatch). The knob was loaded at
        // construction — no per-call I/O.
        if self.bash_file_writes == BashFileWrites::Deny
            && !command_allows_write_explicitly(&input.command)
            && command_looks_like_file_write(&input.command)
        {
            return Ok(ToolResult {
                output: "Command refused by tool_policy.bash_file_writes=deny (it looks like a file-writing shell command). Use the edit_file / diff_edit tools for code changes instead. If this refusal is a false positive, append the comment `# octos:allow-write` to the command line to run it explicitly. Command: ".to_owned() + &input.command,
                success: false,
                ..Default::default()
            });
        }
        // #28a — BEFORE snapshot for the file-change receipt (git-status
        // level; None on non-git/fail-open omits the receipt entirely).
        let dirty_before = snapshot_dirty_paths(effective_cwd);

        // Check policy first
        let decision = self.policy.check(&input.command, effective_cwd);
        match decision {
            Decision::Deny => {
                tracing::warn!(command = %input.command, "command denied by policy");
                return Ok(ToolResult {
                    output: format!(
                        "Command denied by security policy: {}\n\nThis command was blocked because it matches a dangerous pattern.",
                        input.command
                    ),
                    success: false,
                    ..Default::default()
                });
            }
            Decision::Ask => {
                if !self.approval_policy.allows_prompt() {
                    tracing::warn!(
                        command = %input.command,
                        "command requires approval but approval policy is never"
                    );
                    return Ok(ToolResult {
                        output: format!(
                            "Command requires approval but approval_policy is never: {}",
                            input.command
                        ),
                        success: false,
                        ..Default::default()
                    });
                }

                let requester = TOOL_APPROVAL_CTX.try_with(Clone::clone).ok();
                let Some(requester) = requester else {
                    tracing::warn!(command = %input.command, "command requires approval — denied (no interactive approval available)");
                    return Ok(ToolResult {
                        output: format!(
                            "Command requires approval and was denied: {}\n\nThis command matches a potentially dangerous pattern (e.g. sudo, rm -rf, git push --force). It cannot be executed without interactive approval.",
                            input.command
                        ),
                        success: false,
                        ..Default::default()
                    });
                };

                let tool_id = if ctx.tool_id.is_empty() {
                    TOOL_CTX
                        .try_with(|inner| inner.tool_id.clone())
                        .unwrap_or_default()
                } else {
                    ctx.tool_id.clone()
                };
                let decision = requester
                    .request_approval(ToolApprovalRequest {
                        tool_id,
                        tool_name: self.name().to_owned(),
                        title: "Approve shell command".to_owned(),
                        body: format!("Run command: {}", input.command),
                        command: Some(input.command.clone()),
                        cwd: Some(effective_cwd.to_string_lossy().into_owned()),
                    })
                    .await;
                if matches!(decision, ToolApprovalDecision::Deny) {
                    tracing::warn!(command = %input.command, "command denied by interactive approval");
                    return Ok(ToolResult {
                        output: format!(
                            "Command denied by user approval: {}{}",
                            input.command,
                            crate::tools::USER_APPROVAL_DENIED_SUFFIX
                        ),
                        success: false,
                        ..Default::default()
                    });
                }
            }
            Decision::Allow => {}
        }

        // Background execution: a trailing `&` or an explicit `background: true`
        // arg asks the command to run detached. Register it as a tracked
        // supervisor task (so it surfaces in `/ps` and the sub-agent dock),
        // spawn it detached through the same sandbox, and return immediately
        // without waiting. Foreground commands (no `&`) are unchanged.
        // Fail closed on an unhonorable sandbox config BEFORE spawning
        // anything: the typed refusal (model-facing Display — operator
        // remediation stays in the logs) IS the tool result. The wrap-level
        // refusal command would also fail, but a background command discards
        // its stderr, and the model deserves the full refusal text, not a
        // truncated stderr line.
        if let Some(refusal) = self.sandbox.refusal() {
            return Ok(ToolResult {
                output: refusal.to_string(),
                success: false,
                ..Default::default()
            });
        }

        if is_background_command(&input.command, input.background) {
            return Ok(self
                .execute_background(ctx, &input.command, effective_cwd)
                .await);
        }

        // Clamp timeout to [1, 600] seconds to prevent abuse
        const MIN_TIMEOUT: u64 = 1;
        const MAX_TIMEOUT: u64 = 600;
        let mut timeout_duration = input
            .timeout_secs
            .map(|s| Duration::from_secs(s.clamp(MIN_TIMEOUT, MAX_TIMEOUT)))
            .unwrap_or(self.timeout);
        // Apply the hard per-command ceiling (if configured): the effective
        // timeout can only be LOWERED by it, never raised. This is what keeps a
        // closed worker's foreground command bounded by its attempt deadline
        // even when the LLM requests a larger `timeout_secs`.
        if let Some(cap) = self.max_timeout {
            timeout_duration = timeout_duration.min(cap);
        }

        // Execute command (through sandbox).
        // Spawn the child, grab its PID, then timeout on wait_with_output().
        // If timeout fires, kill by PID to prevent orphaned processes.
        // (wait_with_output() takes ownership of child, so we save the PID first.)
        let mut cmd = self.sandbox.wrap_command(&input.command, effective_cwd);
        cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
        apply_frontend_tool_env(&mut cmd, effective_cwd);
        apply_quarto_tool_env(&mut cmd, &input.command, effective_cwd);
        apply_git_tool_env(&mut cmd, &input.command);
        sanitize_command_env(&mut cmd, &EnvAllowlist::empty());
        apply_harness_event_sink_env(&mut cmd, ctx);

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                return Ok(ToolResult {
                    output: format!("Failed to execute command: {e}"),
                    success: false,
                    ..Default::default()
                });
            }
        };
        let child_pid = child.id();

        // Dropping the caller's future detaches this waiter, so the child is
        // still reaped to completion.
        let waiter = tokio::spawn(async move { child.wait_with_output().await });
        let result = timeout(timeout_duration, async {
            waiter.await.map_err(std::io::Error::other)?
        })
        .await;

        match result {
            Ok(Ok(output)) => {
                let stdout = String::from_utf8_lossy(&output.stdout);
                let stderr = String::from_utf8_lossy(&output.stderr);
                let exit_code = output.status.code().unwrap_or(-1);

                let mut result_text = String::new();

                if !stdout.is_empty() {
                    result_text.push_str(&stdout);
                }

                if !stderr.is_empty() {
                    if !result_text.is_empty() {
                        result_text.push_str("\n--- stderr ---\n");
                    }
                    result_text.push_str(&stderr);
                }

                if result_text.is_empty() {
                    result_text = "(no output)".to_string();
                }

                // Sandbox-denial scan runs on the FULL text (the denial line
                // may be exactly what truncation cuts); the hint is appended
                // after truncation so it survives the cut.
                let denial_hint = crate::sandbox::sandbox_denial_hint(
                    !self.sandbox.is_noop(),
                    output.status.success(),
                    &result_text,
                );

                // Truncate if too long.
                // Artifact-offload first (OCTOS_TOOL_ARTIFACT, default ON):
                // >8KB outputs land in <workspace>/.octos/artifacts/ and the
                // history keeps head+tail+pointer — bash output was 83% of
                // history bytes on the ARC sheet run, the single largest
                // replay-cost driver.
                // Exit code lives at the HEAD of the result: downstream
                // model-visible truncation (context_manager tool_output_
                // policy, cuts from the tail) must never lose it — the
                // shell-spiral detector classifies success by this marker,
                // and a tail-placed suffix got stripped by that cut
                // (2026-10-02 sheet run: healthy requirements dump →
                // spiral misfire → turn killed at 85s).
                if artifact_offload_enabled() {
                    if let Some(spill) =
                        artifact_offload(effective_cwd, &result_text)
                    {
                        result_text = spill;
                    }
                }
                const MAX_OUTPUT: usize = 50000;
                octos_core::truncate_utf8(
                    &mut result_text,
                    MAX_OUTPUT,
                    "\n... (output truncated)",
                );

                result_text.insert_str(
                    0,
                    &format!("Exit code: {exit_code}\n\n"),
                );
                if let Some(hint) = denial_hint {
                    result_text.push_str(hint);
                }

                // #28a — file-change receipt: AFTER snapshot + diff,
                // appended ONCE to THIS result's tail (never the system
                // prompt, never a history rewrite — prompt-cache stable).
                let receipt = diff_to_receipt(dirty_before, snapshot_dirty_paths(effective_cwd));
                if let Some(receipt) = receipt.as_deref() {
                    result_text.push_str(receipt);
                    // #28b — warn knob: nudge ONLY when files actually
                    // changed (files_changed > 0), sharing the receipt's
                    // AFTER snapshot (no second git-status scan). Zero
                    // behavior change under `allow`.
                    if self.bash_file_writes == BashFileWrites::Warn
                        && !receipt.trim_end().ends_with("files_changed: 0")
                    {
                        result_text.push_str(
                            "\nnote: prefer the edit_file / diff_edit tools for code changes (tool_policy.bash_file_writes=warn)",
                        );
                    }
                }

                Ok(ToolResult {
                    output: result_text,
                    success: output.status.success(),
                    ..Default::default()
                })
            }
            Ok(Err(e)) => Ok(ToolResult {
                output: format!("Failed to execute command: {e}"),
                success: false,
                ..Default::default()
            }),
            Err(_) => {
                // Graceful shutdown: SIGTERM first, then SIGKILL after grace period.
                // wait_with_output() consumed the Child, so we kill via PID.
                // Use negative PID to target the entire process group.
                #[cfg(unix)]
                if let Some(pid) = child_pid {
                    use std::process::Command as StdCommand;

                    // 1. Send SIGTERM to process group for graceful shutdown.
                    // `--` is required before the negative PID: GNU/procps
                    // `kill` otherwise parses `-<pid>` as an option and the
                    // group signal is silently never delivered (macOS
                    // accepted the bare form, Linux did not).
                    let group = format!("-{pid}");
                    let _ = StdCommand::new("kill").args(["-15", "--", &group]).status();
                    let _ = StdCommand::new("kill")
                        .args(["-15", &pid.to_string()])
                        .status();

                    // 2. Brief grace period, then SIGKILL gated on a probe of
                    // the GROUP — a leader-only probe skipped the escalation
                    // when the shell died to SIGTERM while backgrounded
                    // grandchildren lived on (#1781 CI). `kill -0 -- -pgid`
                    // succeeds while ANY member is alive and cannot hit a
                    // recycled group while a member remains.
                    tokio::time::sleep(Duration::from_millis(500)).await;

                    let group_alive = StdCommand::new("kill")
                        .args(["-0", "--", &group])
                        .status()
                        .is_ok_and(|s| s.success());
                    if group_alive {
                        let _ = StdCommand::new("kill").args(["-9", "--", &group]).status();
                    }
                    let leader_alive = StdCommand::new("kill")
                        .args(["-0", &pid.to_string()])
                        .status()
                        .is_ok_and(|s| s.success());
                    if leader_alive {
                        let _ = StdCommand::new("kill")
                            .args(["-9", &pid.to_string()])
                            .status();
                    }
                }
                #[cfg(windows)]
                if let Some(pid) = child_pid {
                    use std::process::Command as StdCommand;
                    let _ = StdCommand::new("taskkill")
                        .args(["/F", "/T", "/PID", &pid.to_string()])
                        .status();
                }
                Ok(ToolResult {
                    output: format!(
                        "Command timed out after {} seconds",
                        timeout_duration.as_secs()
                    ),
                    success: false,
                    ..Default::default()
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trace_subset_path_matching_is_precise() {
        // Denied: exact files, subpaths, absolute spellings, ./ prefixes.
        for p in [
            ".arc/octos-events.jsonl",
            "./.arc/octos-events.jsonl",
            "/workspace/template/.arc/runner-events.jsonl",
            ".arc/traceability/node_states.json",
            "/w/.arc/traceability/requirements.json",
            ".arc/host-plan/state.v1.json",
            ".arc/debug.log",
        ] {
            assert!(path_in_trace_subset(p), "must deny: {p}");
        }
        // Allowed: the operating-contract surface of .arc stays readable.
        for p in [
            ".arc/acceptance/REQ-1-1-1-register.spec.ts",
            ".arc/acceptance/support/e2e.ts",
            ".arc/host/restart-server.sh",
            ".arc/fixture-manifest.json",
            ".arc",
            "backend/data/db.json",
            "src/arc/trace.rs",
        ] {
            assert!(!path_in_trace_subset(p), "must allow: {p}");
        }
        // Glob patterns naming the subset are caught by the same matcher.
        assert!(path_in_trace_subset("**/.arc/octos-events.jsonl"));
        assert!(path_in_trace_subset(".arc/traceability/**"));
    }

    #[test]
    fn trace_guard_command_mechanics() {
        // Pure heuristics (enabled flag is process-global, mirroring the
        // browser/full-suite guards' test convention).
        assert!(command_references_trace_subset(
            "tail -30 .arc/octos-events.jsonl"
        ));
        assert!(command_references_trace_subset(
            "grep -rn node_states .arc/traceability/"
        ));
        assert!(command_references_trace_subset(
            "python3 -c \"import json; d=json.load(open('.arc/host-plan/state.v1.json'))\""
        ));
        assert!(!command_references_trace_subset(
            "cat .arc/acceptance/REQ-1.spec.ts"
        ));
        assert!(!command_references_trace_subset("ls .arc/host"));
        // Escape hatch lives on the last line only.
        assert!(command_allows_trace_read_explicitly(
            "cat .arc/debug.log # octos:allow-trace-read"
        ));
        assert!(!command_allows_trace_read_explicitly(
            "# octos:allow-trace-read\ncat .arc/debug.log"
        ));
    }

    #[test]
    fn artifact_offload_spills_large_output_with_pointer() {
        let tmp = std::env::temp_dir().join(format!(
            "octos-artifact-test-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let big = format!("{}\n", "x".repeat(20 * 1024));
        let spilled = artifact_offload(&tmp, &big)
            .expect("output above 8KB must spill");
        assert!(spilled.contains("spilled to"));
        assert!(spilled.contains(".octos/artifacts"));
        assert!(spilled.len() < big.len() / 2);
        // Artifact file actually landed and holds the full text.
        let dir = tmp.join(".octos").join("artifacts");
        let mut files: Vec<_> = std::fs::read_dir(&dir).unwrap().collect();
        assert_eq!(files.len(), 1);
        let f = files.remove(0).unwrap().path();
        assert_eq!(std::fs::read_to_string(&f).unwrap(), big);
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn artifact_offload_keeps_small_output_inline() {
        let tmp = std::env::temp_dir().join(format!(
            "octos-artifact-test-small-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        assert!(artifact_offload(&tmp, "short output").is_none());
        assert!(!tmp.join(".octos").exists());
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn char_boundary_helpers_never_panic_on_multibyte() {
        let s = "中文内容测试abc";
        let h = floor_char_boundary(s, 4);
        let t = ceil_char_boundary(s, s.len() - 4);
        assert!(s.is_char_boundary(h));
        assert!(s.is_char_boundary(t));
        assert!(h <= 4);
        assert!(t >= s.len() - 4);
    }

    #[test]
    fn browser_script_heuristic_matches_inline_node_verifiers() {
        // The ARC-observed shape: inline node -e bootstrap of a browser.
        assert!(command_looks_like_browser_script(
            r#"node -e "const { chromium } = require('/x/node_modules/playwright'); chromium.launch();""#
        ));
        assert!(command_looks_like_browser_script(
            r#"NODE -E 'const p=require("puppeteer");'"#
        ));
    }

    #[test]
    fn browser_script_heuristic_lets_sanctioned_commands_through() {
        // The host SPEC RUN: file argument, not an inline -e payload.
        assert!(!command_looks_like_browser_script(
            "node /x/@playwright/test/cli.js test --config .arc/acceptance/playwright.config.ts --reporter=line A.spec.ts"
        ));
        // npx fallback and plain non-node commands.
        assert!(!command_looks_like_browser_script(
            "npx --yes @playwright/test test --config cfg"
        ));
        assert!(!command_looks_like_browser_script("node server.js"));
        assert!(!command_looks_like_browser_script("echo chromium"));
    }

    #[test]
    fn ui_smoke_run_command_shape_passes_every_guard() {
        // The host-authored UI contract smoke (arc/ui_smoke.py) stages its
        // suite at .arc/ui-smoke/ and sanctions an agent-side invocation of
        // the same shape as SPEC RUN. All three ARC guards must let it
        // through: not an inline node payload, not the .arc/acceptance
        // config, not a trace-subset path. Inline self-rolled browser
        // scripts stay refused (hackathon--sheet: the model had no browser
        // verification channel at all and shipped 6 defect classes).
        let cmd = "node /w/local-grader/node_modules/@playwright/test/cli.js \
                   test --config .arc/ui-smoke/playwright.config.ts";
        assert!(!command_looks_like_browser_script(cmd));
        assert!(!command_looks_like_full_spec_run(cmd));
        assert!(!command_references_trace_subset(cmd));
        let npx = "npx --yes @playwright/test test --config .arc/ui-smoke/playwright.config.ts";
        assert!(!command_looks_like_browser_script(npx));
        assert!(!command_looks_like_full_spec_run(npx));
        assert!(!command_references_trace_subset(npx));
        // And the refusal side stays intact: inline self-rolled browser
        // scripts still match, and .arc/ui-smoke is not on the trace-deny
        // list (the smoke suite + its report stay agent-readable).
        assert!(command_looks_like_browser_script(
            "node -e \"const {chromium} = require('playwright')\""
        ));
        assert!(!TRACE_READ_DENIED
            .iter()
            .any(|m| m.contains("ui-smoke")));
    }

    #[test]
    fn browser_escape_hatch_authorizes_explicitly() {
        let cmd = "node -e 'chromium.launch()' # octos:allow-browser";
        assert!(command_allows_browser_explicitly(cmd));
        assert!(!command_allows_browser_explicitly("node -e 'chromium.launch()'"));
    }

    #[test]
    fn full_spec_run_heuristic_matches_unfiltered_acceptance_runs() {
        // keep-25-run7 observed shape: cli.js against the acceptance config
        // with no spec file argument.
        assert!(command_looks_like_full_spec_run(
            "node /workspace/submission/local-grader/node_modules/@playwright/test/cli.js test --config .arc/acceptance/playwright.config.ts --reporter=line 2>&1"
        ));
        // npx fallback spelling against the acceptance config.
        assert!(command_looks_like_full_spec_run(
            "npx --yes @playwright/test test --config .arc/acceptance/playwright.config.ts --reporter=line"
        ));
    }

    #[test]
    fn full_spec_run_heuristic_lets_filtered_and_unrelated_commands_through() {
        // Filtered batch runs carry spec file arguments.
        assert!(!command_looks_like_full_spec_run(
            "node /x/@playwright/test/cli.js test --config .arc/acceptance/playwright.config.ts --reporter=line REQ-1.1.spec.ts REQ-1.2.spec.ts"
        ));
        // Playwright outside the acceptance config is not the host's suite.
        assert!(!command_looks_like_full_spec_run(
            "npx playwright test --config my-own.config.ts"
        ));
        // Inspecting the acceptance directory is not a test invocation.
        assert!(!command_looks_like_full_spec_run(
            "ls -la .arc/acceptance/ && cat .arc/acceptance/playwright.config.ts"
        ));
    }

    #[test]
    fn full_suite_escape_hatch_authorizes_explicitly() {
        let cmd = "node /x/cli.js test --config .arc/acceptance/playwright.config.ts # octos:allow-full-suite";
        assert!(command_allows_full_suite_explicitly(cmd));
        assert!(!command_allows_full_suite_explicitly(
            "node /x/cli.js test --config .arc/acceptance/playwright.config.ts"
        ));
    }

    use super::*;

    /// Runaway guard for the "poll until the background task reaches state X"
    /// loops below. NOT a latency assertion: each loop breaks the instant the
    /// supervisor reports a terminal status, so a generous ceiling costs a passing
    /// run nothing, and a genuinely broken run still fails — just later.
    /// Mirrors `spawn_tests::BACKGROUND_DEADLINE`.
    const BACKGROUND_DEADLINE: std::time::Duration = std::time::Duration::from_secs(60);

    // -----------------------------------------------------------------------
    // Fail-closed sandbox refusal: the pre-spawn guard is the tool result.
    // -----------------------------------------------------------------------

    fn refusing_sandbox() -> Box<dyn crate::sandbox::Sandbox> {
        Box::new(crate::sandbox::RefusingSandbox {
            error: crate::sandbox::SandboxUnavailable {
                requested: "landlock".to_string(),
                reason: "test refusal".to_string(),
                remediation: "install a backend".to_string(),
            },
        })
    }

    use crate::tools::test_fixture::scoped_ctx;

    #[tokio::test]
    async fn shell_refuses_with_typed_message_when_sandbox_unavailable() {
        let temp = tempfile::tempdir().expect("tempdir");
        let tool = super::ShellTool::new(temp.path()).with_sandbox(refusing_sandbox());
        let out = tool
            .execute(&serde_json::json!({"command": "touch should-not-exist"}))
            .await
            .expect("execute");
        assert!(!out.success, "refusal must fail the tool call");
        assert!(
            out.output.contains("sandbox unavailable") && out.output.contains("test refusal"),
            "refusal carries the mode/reason: {}",
            out.output
        );
        assert!(
            out.output.contains("operator") && !out.output.contains("install a backend"),
            "the tool result is the MODEL-facing text — operator remediation stays in \
             the logs: {}",
            out.output
        );
        assert!(
            !temp.path().join("should-not-exist").exists(),
            "the command must never run"
        );
    }

    // -----------------------------------------------------------------------
    // #28b — bash_file_writes knob: three positions, escape hatch, and the
    // zero-difference-under-default guarantee.
    // -----------------------------------------------------------------------

    #[test]
    fn bash_file_writes_deny_refuses_write_command() {
        let temp = tempfile::tempdir().expect("tempdir");
        let tool = super::ShellTool::new(temp.path())
            .with_bash_file_writes(crate::tools::policy::BashFileWrites::Deny);
        let out = tokio::runtime::Runtime::new()
            .expect("rt")
            .block_on(tool.execute(&serde_json::json!({
                "command": "echo hi > /tmp/never-created-28b",
            })))
            .expect("execute");
        assert!(!out.success);
        assert!(out.output.contains("tool_policy.bash_file_writes=deny"));
        assert!(out.output.contains("edit_file / diff_edit"));
        assert!(out.output.contains("# octos:allow-write"));
        assert!(!std::path::Path::new("/tmp/never-created-28b").exists());
        std::fs::remove_file("/tmp/never-created-28b").ok();
    }

    #[tokio::test]
    async fn test_denied_command() {
        let tool = ShellTool::new(std::env::temp_dir());
        let result = tool
            .execute(&serde_json::json!({"command": "rm -rf /"}))
            .await
            .unwrap();
        assert!(!result.success);
        assert!(result.output.contains("denied"));
    }

    #[test]
    fn quarto_command_redirects_home_into_workspace() {
        // Regression (2026-06-08): `quarto render` died under the sandbox
        // with `unable to open database file:
        // ~/Library/Caches/quarto/sass/sass.kv`. Quarto locates that cache
        // via $HOME and ignores QUARTO_CACHE/XDG_CACHE_HOME on macOS, so we
        // redirect HOME into the (sandbox-writable) workspace for quarto
        // commands. No sandbox isolation is weakened — the cache lands
        // inside cwd, which is already writable.
        let cwd = std::env::temp_dir().join(format!("octos-quarto-env-{}", std::process::id()));
        std::fs::create_dir_all(&cwd).unwrap();

        let mut cmd = tokio::process::Command::new("sh");
        apply_quarto_tool_env(&mut cmd, "cd sites/foo && quarto render", &cwd);

        let home = cmd
            .as_std()
            .get_envs()
            .find(|(k, _)| k.to_str() == Some("HOME"))
            .and_then(|(_, v)| v)
            .map(PathBuf::from)
            .expect("HOME must be set for quarto commands");
        assert!(
            home.starts_with(&cwd),
            "quarto HOME must live inside the workspace, got {home:?}"
        );

        std::fs::remove_dir_all(&cwd).ok();
    }

    #[test]
    fn detects_git_invocation_in_compound_shell_command() {
        assert!(contains_git_invocation(
            "cd /tmp/repo && git diff -- notes.txt"
        ));
        assert!(contains_git_invocation("GIT_DIR=.git git status --short"));
        assert!(contains_git_invocation("env GIT_DIR=.git git status"));
        assert!(contains_git_invocation("/usr/bin/git log --oneline"));
        assert!(contains_git_invocation(r#""/usr/bin/git" log --oneline"#));
        assert!(contains_git_invocation("time git status --short"));
        assert!(contains_git_invocation("sudo -E git push"));
        assert!(contains_git_invocation("npx git log --oneline"));
        assert!(contains_git_invocation(
            "printf ref | git hash-object --stdin"
        ));
        assert!(contains_git_invocation(
            r#""C:\Program Files\Git\cmd\git.exe" status"#
        ));
        assert!(!contains_git_invocation("printf 'git diff -- notes.txt'"));
        assert!(!contains_git_invocation("grep git README.md"));
        assert!(!contains_git_invocation("FOO=/usr/bin/git echo ok"));
    }

    // -----------------------------------------------------------------------
    // Phase 2-D: SessionScope integration tests for ShellTool.
    //
    // The child process CWD is the load-bearing observable here — a shell
    // command that runs `pwd` (or the `cd` cmd-builtin equivalent on
    // Windows) must see `scope.workspace()` when the host has threaded a
    // scope through `ToolContext`, and must see `self.cwd` (legacy
    // behaviour) when the host has not.
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn background_command_registers_and_flips_terminal_with_supervisor() {
        use crate::task_supervisor::{TaskStatus, TaskSupervisor};

        let supervisor = Arc::new(TaskSupervisor::new());

        let tool = ShellTool::new(std::env::temp_dir())
            .with_task_supervisor(supervisor.clone(), "api:test-session");

        // Trailing `&` → background. Returns immediately.
        let result = tool
            .execute(&serde_json::json!({"command": "sleep 1 &"}))
            .await
            .unwrap();
        assert!(result.success, "{}", result.output);
        assert!(
            result.output.contains("Started background task"),
            "unexpected output: {}",
            result.output
        );

        // The task is registered as active (running) right away — before the
        // 1s sleep exits — which is exactly what makes it visible in `/ps`.
        let tasks = supervisor.get_tasks_for_session("api:test-session");
        assert_eq!(
            tasks.len(),
            1,
            "expected one registered task, got {tasks:?}"
        );
        assert!(
            tasks[0].status.is_active(),
            "task should be active immediately, got {:?}",
            tasks[0].status
        );
        assert!(
            tasks[0].tool_name.contains("sleep 1"),
            "label should reflect the command, got {:?}",
            tasks[0].tool_name
        );

        // Poll until the watcher flips it terminal after the child exits.
        let started = std::time::Instant::now();
        loop {
            let tasks = supervisor.get_tasks_for_session("api:test-session");
            if let Some(t) = tasks.first() {
                if t.status == TaskStatus::Completed {
                    break;
                }
                if t.status == TaskStatus::Failed {
                    panic!("background task failed: {:?}", t.error);
                }
            }
            if started.elapsed() > BACKGROUND_DEADLINE {
                let statuses: Vec<_> = supervisor
                    .get_tasks_for_session("api:test-session")
                    .iter()
                    .map(|t| t.status.as_str().to_string())
                    .collect();
                panic!("background task did not complete in 15s: {statuses:?}");
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    }

    #[tokio::test]
    async fn max_timeout_secs_caps_a_larger_requested_timeout() {
        // A hard 1s ceiling must override a larger requested `timeout_secs`:
        // `sleep 30` with `timeout_secs: 30` under `with_max_timeout_secs(1)`
        // is killed at ~1s, so a closed worker's foreground command cannot
        // outlive its deadline even when the LLM asks for a big timeout.
        let tool = ShellTool::new(std::env::temp_dir()).with_max_timeout_secs(1);
        let started = std::time::Instant::now();
        let result = tool
            .execute(&serde_json::json!({"command": "sleep 30", "timeout_secs": 30}))
            .await
            .unwrap();
        assert!(!result.success, "a capped, timed-out command must fail");
        assert!(
            result.output.contains("timed out after 1 seconds"),
            "expected a 1s timeout (cap applied), got: {}",
            result.output
        );
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "the 1s cap must bound the wait well under the requested 30s",
        );
    }

    #[tokio::test]
    async fn shell_safe_policy_still_denies_with_scope_present() {
        // Codex-anticipated regression: a scope on the context must NOT
        // weaken the `SafePolicy` denylist — `rm -rf /` is refused
        // whether or not a scope is set. The CWD that the policy sees
        // changes (it now sees the scope workspace), but the denylist is
        // command-string only so the verdict is unchanged.
        let scope_dir = tempfile::tempdir().unwrap();
        let scope = octos_core::SessionScope::solo(scope_dir.path().to_path_buf(), vec![])
            .expect("scope construction");
        let tool = ShellTool::new(std::env::temp_dir());
        let ctx = scoped_ctx("shell-with-scope", scope);

        let result = tool
            .execute_with_context(&ctx, &serde_json::json!({"command": "rm -rf /"}))
            .await
            .unwrap();
        assert!(!result.success);
        assert!(
            result.output.contains("denied"),
            "expected deny, got: {}",
            result.output
        );
    }
}

// ---------------------------------------------------------------------------
// #28b — bash file-writes knob helpers.
// ---------------------------------------------------------------------------

/// ARC guard (2026-09-22): inline node scripts that bootstrap a browser
/// stack. The ARC flow's sanctioned verifier is the host-provided SPEC RUN
/// command; the model nonetheless hand-rolled `node -e` chromium scripts
/// 78-87 times per run (identical arguments, pure queue/tool waste, and
/// the prompt-level ban was ignored — the third documented case of soft
/// constraints not binding flash-tier models). Gate is an env knob the
/// embedding host sets (`OCTOS_DENY_BROWSER_SCRIPTS=1`); default off keeps
/// vanilla octos unchanged.
pub(crate) fn browser_script_denial_enabled() -> bool {
    static ENABLED: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| {
        std::env::var("OCTOS_DENY_BROWSER_SCRIPTS")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
    });
    *ENABLED
}

/// Escape hatch mirroring [`command_allows_write_explicitly`]: a trailing
/// `# octos:allow-browser` comment explicitly authorizes a browser-shaped
/// inline script under the guard.
pub(crate) fn command_allows_browser_explicitly(command: &str) -> bool {
    command
        .lines()
        .last()
        .is_some_and(|last| last.contains("# octos:allow-browser"))
}

/// Heuristic: an INLINE node script (`node -e` / `--eval` / heredoc) whose
/// payload references a browser stack (playwright / chromium / puppeteer).
/// The sanctioned SPEC RUN (`node <cli.js> test --config …`) and
/// `npx playwright …` do not match — they are not inline `-e` payloads.
/// False positives escape via `# octos:allow-browser`.
pub(crate) fn command_looks_like_browser_script(command: &str) -> bool {
    let lowered = command.to_ascii_lowercase();
    let inline_node = lowered.contains("node -e")
        || lowered.contains("node --eval")
        || lowered.contains("node <<");
    let browser_stack = lowered.contains("playwright")
        || lowered.contains("chromium")
        || lowered.contains("puppeteer");
    inline_node && browser_stack
}

/// ARC guard (2026-09-22): a full-suite SPEC RUN — a playwright test against
/// the host's acceptance config that names NO spec file. keep-25-run7: the
/// model hand-assembled the unfiltered command 7 times (~111s each) even
/// though the host prompt only sanctions filtered runs; a full suite can
/// never go green while other nodes are unimplemented, inviting churn.
/// Gate is an env knob the embedding host sets (`OCTOS_DENY_FULL_SPEC_RUN=1`);
/// default off keeps vanilla octos unchanged.
pub(crate) fn full_spec_run_denial_enabled() -> bool {
    static ENABLED: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| {
        std::env::var("OCTOS_DENY_FULL_SPEC_RUN")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
    });
    *ENABLED
}

/// Escape hatch: a trailing `# octos:allow-full-suite` comment explicitly
/// authorizes a full-suite run under the guard.
pub(crate) fn command_allows_full_suite_explicitly(command: &str) -> bool {
    command
        .lines()
        .last()
        .is_some_and(|last| last.contains("# octos:allow-full-suite"))
}

/// Guard verdict + refusal text shared by every bash-ish tool entrypoint:
/// `None` lets the command through, `Some(refusal)` blocks it before spawn.
pub(crate) fn full_spec_run_guard(command: &str) -> Option<String> {
    if !full_spec_run_denial_enabled()
        || command_allows_full_suite_explicitly(command)
        || !command_looks_like_full_spec_run(command)
    {
        return None;
    }
    Some(
        "Command refused: full-suite acceptance runs are disabled for this workspace. \
         Append the spec file arguments for the nodes you are working on (e.g. \
         `... --reporter=line REQ-1.1.spec.ts REQ-1.2.spec.ts`) so the run stays \
         scoped to this batch; full-suite verification is the host's job. If this \
         refusal is a false positive, append the comment `# octos:allow-full-suite` \
         to the command line to run it explicitly. Command: "
            .to_owned()
            + command,
    )
}

/// Heuristic: a playwright/cli.js test invocation pointed at the host's
/// `.arc/acceptance` config that names no `.spec.ts` file argument — i.e.
/// it would run the WHOLE staged suite. Filtered runs (with spec file
/// arguments) pass; non-acceptance playwright usage is untouched.
pub(crate) fn command_looks_like_full_spec_run(command: &str) -> bool {
    let lowered = command.to_ascii_lowercase();
    // `node <path>/cli.js test …` and `npx --yes @playwright/test test …`
    // are the two sanctioned spellings; both end in a bare `test` token
    // before the flags.
    let test_invocation = lowered.contains("cli.js test")
        || lowered.contains("playwright test")
        || lowered.contains("playwright/test test");
    if !(test_invocation && lowered.contains(".arc/acceptance")) {
        return false;
    }
    !command
        .split_whitespace()
        .any(|token| token.ends_with(".spec.ts"))
}

/// ARC guard (2026-09-25, hg-github-001): the host trace/debug subset under
/// `.arc/` is archaeology bait — the model mined its own
/// `octos-events.jsonl` / `node_states.json` / `debug.log` every session
/// segment (~10-15 calls × 4 segments, zero edits in the worst turn) to
/// reconstruct what previous sessions had done. The sanctioned carriers of
/// that context are the host's SEGMENT DIGEST / HOST REPORT. Run-level
/// traceability is consumed host-side, so agent-side read-blocking loses
/// nothing. Contract-relevant paths (`.arc/acceptance/`, `.arc/host/`,
/// `.arc/fixture-manifest.json`) stay readable. Gate is an env knob the
/// embedding host sets (`OCTOS_DENY_TRACE_READ=1`); default off keeps
/// vanilla octos unchanged.
///
/// ZONE semantics (not purely "read"): the bash form matches any command
/// that NAMES a guarded path, so writes/removals (`rm .arc/traceability/…`)
/// are refused too — intentional host-state protection; the escape hatch
/// covers legitimate cases. File-tool forms only see their path argument.
pub(crate) const TRACE_READ_DENIED: &[&str] = &[
    ".arc/octos-events.jsonl",
    ".arc/runner-events.jsonl",
    ".arc/debug.log",
    ".arc/stdout.log",
    ".arc/stderr.log",
    ".arc/traceability/",
    ".arc/host-plan/",
];

pub(crate) fn trace_read_denial_enabled() -> bool {
    static ENABLED: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| {
        std::env::var("OCTOS_DENY_TRACE_READ")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
    });
    *ENABLED
}

/// Escape hatch: a trailing `# octos:allow-trace-read` comment explicitly
/// authorizes a trace-referencing command under the guard.
pub(crate) fn command_allows_trace_read_explicitly(command: &str) -> bool {
    command
        .lines()
        .last()
        .is_some_and(|last| last.contains("# octos:allow-trace-read"))
}

/// Does this PATH (tool argument or bash operand) land in the trace subset?
/// Matches the exact marker file, anything under a marker directory, and
/// absolute spellings that END with or CONTAIN a marker directory
/// (`/workspace/template/.arc/traceability/node_states.json`).
pub(crate) fn path_in_trace_subset(path: &str) -> bool {
    let mut p = path.trim().replace('\\', "/");
    while let Some(stripped) = p.strip_prefix("./") {
        p = stripped.to_string();
    }
    TRACE_READ_DENIED.iter().any(|marker| {
        let m = marker.trim_end_matches('/');
        p == *m
            || p.starts_with(&format!("{m}/"))
            || p.ends_with(&format!("/{m}"))
            || p.contains(&format!("/{m}/"))
    })
}

/// Heuristic: the command text NAMES a trace-subset path (cat/grep/tail/
/// python-open on `.arc/octos-events.jsonl` etc. all match via the path
/// marker appearing anywhere in the command).
pub(crate) fn command_references_trace_subset(command: &str) -> bool {
    TRACE_READ_DENIED.iter().any(|marker| command.contains(marker))
}

/// Shared refusal text pointing the model at the sanctioned carriers.
pub(crate) fn trace_read_refusal(detail: &str) -> String {
    format!(
        "Refused: `{detail}` is a host trace artifact (agent history / host \
         state under .arc) and is not agent-consumable. Prior-session \
         context arrives via the summaries your embedding host provides \
         (segment digests / host reports, when present); rely on those plus \
         the running app and `git log` for state. If you genuinely need \
         this file, run it through bash with a trailing \
         `# octos:allow-trace-read` comment to acknowledge the escape hatch."
    )
}

/// Bash-command form of the guard: `None` lets the command through.
pub(crate) fn trace_read_guard_command(command: &str) -> Option<String> {
    if !trace_read_denial_enabled()
        || command_allows_trace_read_explicitly(command)
        || !command_references_trace_subset(command)
    {
        return None;
    }
    Some(trace_read_refusal(command))
}

/// Path-argument form for file tools (read_file / grep / glob / list_dir):
/// `None` lets the read through.
pub(crate) fn trace_read_guard_path(path: &str) -> Option<String> {
    if !trace_read_denial_enabled() || !path_in_trace_subset(path) {
        return None;
    }
    Some(trace_read_refusal(path))
}

/// #28b — escape hatch: a trailing `# octos:allow-write` comment on the
/// command line explicitly authorizes a write-shaped command under `deny`.
pub(crate) fn command_allows_write_explicitly(command: &str) -> bool {
    command
        .lines()
        .last()
        .is_some_and(|last| last.contains("# octos:allow-write"))
}

/// #28b — heuristic: does this command LOOK like it writes files? A
/// curated WHITELIST of shapes (documented; false negatives tolerated —
/// the 28a receipt still reports what actually changed):
///   * shell redirection to a file: `> file`, `>> file`, `2> file`, `&> file`
///   * `tee file` / `tee -a file`
///   * in-place editors: `sed -i`, `gawk -i`, `perl -pi`, `ruby -pi`
///   * `cp`/`mv`/`rm`/`mkdir`/`touch`/`truncate`/`ln` with a non-flag arg
///   * heredocs: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`
///   * `python -c`/`python3 -c`/`node -e` whose payload mentions
///     open(...,"w") / fs.write — the two dominant scripted-write shapes
///   * `dd of=`, `install`, `patch <`, `git apply`, `unzip -o`, `tar -x`
pub(crate) fn command_looks_like_file_write(command: &str) -> bool {
    // Redirections (single-line scan; quotes rarely wrap the target).
    for token_hint in [">>", " 2> ", "&> ", "> ", ">/"] {
        if command.contains(token_hint) {
            return true;
        }
    }
    if command.contains("<<") {
        return true; // heredoc (covers <<- / <<' / <<" variants).
    }
    let lowered = command.to_ascii_lowercase();
    for kw in [
        " tee ",
        "tee -a",
        "sed -i",
        "sed --in-place",
        "gawk -i",
        "perl -pi",
        "perl -i",
        "ruby -pi",
        "dd of=",
        " of=",
        " install ",
        "patch <",
        "git apply",
        "unzip -o",
        "tar -x",
    ] {
        if lowered.contains(kw) {
            return true;
        }
    }
    // Mutating coreutils with a non-flag argument (flags skipped so
    // `truncate -s 0 f` matches, while `grep -rn foo` style stays clean —
    // the keyword list itself disambiguates).
    for kw in ["cp ", "mv ", "rm ", "mkdir ", "touch ", "truncate ", "ln "] {
        if let Some((_, rest)) = lowered.split_once(kw) {
            let has_operand = rest.split_whitespace().any(|w| !w.starts_with('-'));
            if has_operand {
                return true;
            }
        }
    }
    // Scripted writes via `python -c` / `node -e`.
    if (lowered.contains("python -c")
        || lowered.contains("python3 -c")
        || lowered.contains("node -e"))
        && ((lowered.contains("open(") && lowered.contains("\"w"))
            || lowered.contains("fs.write")
            || lowered.contains("writefilesync"))
    {
        return true;
    }
    false
}

// ---------------------------------------------------------------------------
// #28a — file-change receipt: a `git status`-level dirty-diff taken before
// and after the command, appended ONCE to THIS tool result's tail.
// ---------------------------------------------------------------------------

/// #28a — one entry of the working-tree dirty set (`git status --porcelain`
/// line, path made repo-relative). No timestamps, no timings, no absolute
/// paths — the receipt must stay noise-free for the model and for prompt-
/// cache stability (it lives only in the tool result, never in the system
/// prompt and never rewriting history).
#[derive(Debug, Clone)]
struct ChangeReceipt {
    files_changed: usize,
    listed: Vec<String>,
    truncated: bool,
}

impl ChangeReceipt {
    fn render(&self) -> String {
        let mut out = format!("\nfiles_changed: {}", self.files_changed);
        for path in &self.listed {
            out.push_str("\n  ");
            out.push_str(path);
        }
        if self.truncated {
            out.push_str(&format!(
                "\n  (+{} more; not listed)",
                self.files_changed - self.listed.len()
            ));
        }
        out
    }
}

/// #28a — snapshot the repo-relative dirty paths (modified + untracked,
/// i.e. `git status --porcelain` lines) at `git status` cost (ms-scale, no
/// index refresh beyond what status itself does; no new dependencies).
/// `None` when the dir is not a git work tree (receipt silently omitted —
/// acceptance ④) or git is unavailable (fail-open).
pub(crate) fn snapshot_dirty_paths(cwd: &Path) -> Option<Vec<String>> {
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(cwd)
        .args(["status", "--porcelain", "--untracked-files=all"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None; // not a git work tree (or git absent) — omit silently.
    }
    let text = String::from_utf8_lossy(&out.stdout);
    // Keep the RAW porcelain line ("XY PATH") so the diff can compare status
    // flips; strip only a rename's "ORIG -> " to keep the destination side.
    Some(
        text.lines()
            .filter(|line| !line.trim().is_empty())
            .map(|line| {
                let path = line.get(3..).unwrap_or("").trim();
                match path.split_once(" -> ") {
                    Some((_, dst)) => format!("{} {}", &line[..2], dst),
                    None => line.to_owned(),
                }
            })
            .collect(),
    )
}

pub(crate) const CHANGE_RECEIPT_MAX_LISTED: usize = 20;

pub(crate) fn diff_to_receipt(
    before: Option<Vec<String>>,
    after: Option<Vec<String>>,
) -> Option<String> {
    // Either side missing the snapshot ⇒ non-git or fail-open: no receipt.
    let (before, after) = (before?, after?);
    let before_set: std::collections::HashSet<&String> = before.iter().collect();
    // CHANGED = after-line not present verbatim in before (new file OR a
    // status flip on the same path — both are real tree changes).
    let mut changed: Vec<String> = after
        .iter()
        .filter(|line| !before_set.contains(*line))
        .map(|line| line.get(3..).unwrap_or(line.as_str()).trim().to_owned())
        .collect();
    if changed.is_empty() {
        return Some("\nfiles_changed: 0".to_owned());
    }
    let total = changed.len();
    changed.sort();
    changed.dedup();
    let truncated = total > CHANGE_RECEIPT_MAX_LISTED;
    let listed: Vec<String> = changed
        .into_iter()
        .take(CHANGE_RECEIPT_MAX_LISTED)
        .collect();
    Some(
        ChangeReceipt {
            files_changed: total,
            listed,
            truncated,
        }
        .render(),
    )
}

#[cfg(test)]
mod change_receipt_tests {}
