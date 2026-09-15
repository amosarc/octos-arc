//! Host-side test runner for the testgen pipeline.
//!
//! Spawns the unit-test runner (Vitest) the way [`crate::tools::check`]
//! spawns static checkers — own process group, hard timeout, negative-PID
//! tree kill — and parses the JSON reporter output into the typed summary
//! the verdict classifier needs (per-case titles + first failure message).
//!
//! The runner is deliberately runner-specific: the generic `check` tool has
//! its own inline vitest parsing for diagnostics-style output; here we need
//! per-CASE fidelity, which the diagnostics shape does not carry.

use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use eyre::Result;
use serde::Serialize;
use tokio::time::timeout;

use crate::sandbox::Sandbox;
use crate::subprocess_env::{EnvAllowlist, sanitize_command_env};

/// One failing test case, evidence-shaped.
#[derive(Debug, Clone, Serialize)]
pub struct CaseFailure {
    /// Test file (workspace-relative when resolvable, absolute otherwise).
    pub file: String,
    /// Full case title as the runner reported it.
    pub title: String,
    /// First lines of the failure message (bounded — full stack traces are
    /// the model's job to fetch via read_file, not the report's to carry).
    pub message: String,
}

/// Parsed `vitest run --reporter=json` summary.
#[derive(Debug, Clone, Serialize)]
pub struct VitestSummary {
    pub total: usize,
    pub passed: usize,
    pub failed: usize,
    pub failures: Vec<CaseFailure>,
    /// Test files whose suite failed to LOAD (import/syntax errors) — these
    /// are test-side bugs, distinct from assertion failures.
    pub load_errors: Vec<String>,
}

impl VitestSummary {
    pub fn all_passed(&self) -> bool {
        self.failed == 0 && self.load_errors.is_empty()
    }
}

/// Result of one runner invocation.
#[derive(Debug)]
pub struct RunOutcome {
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    /// Parsed summary — `None` when stdout carried no parseable JSON
    /// report (crash, wrong cwd, runner missing).
    pub summary: Option<VitestSummary>,
    /// Tail of combined output for the raw fallback path.
    pub raw_tail: String,
}

/// Hard timeout for one vitest invocation.
pub const DEFAULT_RUN_TIMEOUT: Duration = Duration::from_secs(120);

/// Run `npx vitest run <files> --reporter=json` in `cwd_abs`.
/// `files` are paths relative to `cwd_abs` (already stripped of the
/// backend/frontend prefix by the profile).
///
/// When a real session sandbox is provided the runner is confined through
/// it exactly like `check`/`shell` — vitest executes project-controlled
/// code (config files, plugins) and must not escape a sandbox that would
/// confine the equivalent shell call. Windows/Docker cannot wrap the
/// POSIX-quoted argv (see check.rs), so the run is SKIPPED as a valid
/// answer rather than escaping to the host.
pub async fn run_vitest(
    cwd_abs: &Path,
    files: &[String],
    sandbox: Option<&Arc<dyn Sandbox>>,
) -> Result<RunOutcome> {
    let mut argv = vec![
        "npx".to_string(),
        // Auto-confirm the one-shot install when the workspace has no
        // local vitest: with stdin nulled, npx's "Ok to proceed?" prompt
        // would print to stdout and the run would die unparseable.
        "--yes".to_string(),
        "vitest".to_string(),
        "run".to_string(),
        "--reporter=json".to_string(),
    ];
    // vitest ≥3 routes the JSON report to a FILE (stdout only carries a
    // "report written" notice). Pin the destination so the runner can read
    // it back regardless of the reporter's stdout/file default per version.
    let report_path = std::env::temp_dir().join(format!(
        "octos-vitest-report-{}-{}.json",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    argv.push(format!("--outputFile.json={}", report_path.display()));
    argv.extend(files.iter().cloned());

    let real_sandbox = sandbox.filter(|s| !s.is_noop());
    let mut cmd = match real_sandbox {
        Some(sandbox) => {
            if cfg!(windows) || sandbox.is_docker() {
                return Ok(RunOutcome {
                    exit_code: None,
                    timed_out: false,
                    summary: None,
                    raw_tail: "testgen verify skipped: vitest cannot be sandboxed under the \
                               active backend (Windows/Docker); run it via the shell tool \
                               instead"
                        .to_string(),
                });
            }
            let quoted = shlex::try_join(argv.iter().map(String::as_str))
                .map_err(|_| eyre::eyre!("vitest argv contains a NUL byte"))?;
            sandbox.wrap_command(&quoted, cwd_abs)
        }
        None => {
            let mut cmd = tokio::process::Command::new(&argv[0]);
            cmd.args(&argv[1..]).current_dir(cwd_abs);
            cmd
        }
    };
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Own process group so the timeout path can signal the whole tree
    // (vitest workers included) — same convention as check.rs/shell.rs.
    #[cfg(unix)]
    cmd.process_group(0);
    sanitize_command_env(&mut cmd, &EnvAllowlist::empty());

    let child = cmd
        .spawn()
        .map_err(|e| eyre::eyre!("failed to spawn vitest in {}: {e}", cwd_abs.display()))?;
    let pid = child.id();

    let output = match timeout(DEFAULT_RUN_TIMEOUT, child.wait_with_output()).await {
        Ok(Ok(output)) => output,
        Ok(Err(e)) => return Err(eyre::eyre!("vitest run failed: {e}")),
        Err(_) => {
            // Never kill on a missing PID: group "-0" would signal OUR
            // own process group.
            if let Some(pid) = pid {
                kill_tree(pid).await;
            }
            return Ok(RunOutcome {
                exit_code: None,
                timed_out: true,
                summary: None,
                raw_tail: format!(
                    "vitest timed out after {}s",
                    DEFAULT_RUN_TIMEOUT.as_secs()
                ),
            });
        }
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let combined = if stderr.trim().is_empty() {
        stdout.to_string()
    } else {
        format!("{stdout}\n--- stderr ---\n{stderr}")
    };
    let raw_tail = tail_chars(&combined, 4000);

    // Parse order: stdout (older vitest printed the JSON document) first,
    // then the pinned report file (newer vitest routes the document there
    // and stdout only carries a notice line).
    let mut summary = parse_vitest_json(&stdout);
    if summary.is_none() {
        if let Ok(file_json) = std::fs::read_to_string(&report_path) {
            summary = parse_vitest_json(&file_json);
        }
    }
    let _ = std::fs::remove_file(&report_path);

    Ok(RunOutcome {
        exit_code: output.status.code(),
        timed_out: false,
        summary,
        raw_tail,
    })
}

/// Parse the vitest JSON reporter document. Tolerates leading non-JSON
/// noise by scanning to the first `{`.
pub fn parse_vitest_json(stdout: &str) -> Option<VitestSummary> {
    let start = stdout.find('{')?;
    let value: serde_json::Value = serde_json::from_str(stdout[start..].trim_start()).ok()?;
    let num = |key: &str| value.get(key).and_then(|v| v.as_u64()).unwrap_or(0) as usize;

    let mut failures = Vec::new();
    let mut load_errors = Vec::new();
    if let Some(results) = value.get("testResults").and_then(|v| v.as_array()) {
        for suite in results {
            let suite_file = suite
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("<unknown suite>")
                .to_string();
            let suite_status = suite.get("status").and_then(|v| v.as_str()).unwrap_or("");
            let assertion_error = suite
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            let assertions = suite
                .get("assertionResults")
                .and_then(|v| v.as_array())
                .map(|a| !a.is_empty())
                .unwrap_or(false);

            // A failed suite with zero assertion results failed to LOAD
            // (import/syntax error) — test-side, not an app assertion.
            if suite_status == "failed" && !assertions && !assertion_error.is_empty() {
                load_errors.push(format!(
                    "{}: {}",
                    basename(&suite_file),
                    first_lines(assertion_error, 3)
                ));
                continue;
            }

            if let Some(cases) = suite.get("assertionResults").and_then(|v| v.as_array()) {
                for case in cases {
                    let status = case.get("status").and_then(|v| v.as_str()).unwrap_or("");
                    if status != "failed" {
                        continue;
                    }
                    let title = case
                        .get("fullName")
                        .and_then(|v| v.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| {
                            let ancestors: Vec<&str> = case
                                .get("ancestorTitles")
                                .and_then(|v| v.as_array())
                                .map(|a| {
                                    a.iter()
                                        .filter_map(|t| t.as_str())
                                        .collect()
                                })
                                .unwrap_or_default();
                            let t = case
                                .get("title")
                                .and_then(|v| v.as_str())
                                .unwrap_or("<unnamed>");
                            if ancestors.is_empty() {
                                t.to_string()
                            } else {
                                format!("{} > {t}", ancestors.join(" > "))
                            }
                        });
                    let message = case
                        .get("failureMessages")
                        .and_then(|v| v.as_array())
                        .and_then(|m| m.first())
                        .and_then(|m| m.as_str())
                        .map(first_lines3)
                        .unwrap_or_else(|| "no failure message".to_string());
                    failures.push(CaseFailure {
                        file: basename(&suite_file),
                        title,
                        message,
                    });
                }
            }
        }
    }

    Some(VitestSummary {
        total: num("numTotalTests"),
        passed: num("numPassedTests"),
        failed: num("numFailedTests"),
        failures,
        load_errors,
    })
}

fn first_lines3(text: &str) -> String {
    first_lines(text, 3)
}

/// First `n` lines of `text`, joined, bounded to ~500 chars.
fn first_lines(text: &str, n: usize) -> String {
    let joined: String = text
        .lines()
        .filter(|l| !l.trim().is_empty())
        .take(n)
        .collect::<Vec<_>>()
        .join(" ⏎ ");
    let mut out: String = joined.chars().take(500).collect();
    if joined.chars().count() > 500 {
        out.push('…');
    }
    out
}

fn basename(path: &str) -> String {
    path.trim_end_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or(path)
        .to_string()
}

fn tail_chars(text: &str, max_chars: usize) -> String {
    let count = text.chars().count();
    if count <= max_chars {
        return text.to_string();
    }
    let (cut, _) = text
        .char_indices()
        .nth(count - max_chars)
        .unwrap_or((0, ' '));
    text[cut..].to_string()
}

/// Kill a timed-out runner by PID. The child owns a process group
/// (`process_group(0)`), so negative-PID signals reach the whole tree
/// (vitest workers). Uses the external `kill` binary after `--` — GNU
/// procps otherwise parses `-<pid>` as an option and silently never
/// delivers the group signal — mirroring check.rs `kill_by_pid`.
async fn kill_tree(pid: u32) {
    #[cfg(unix)]
    {
        use std::process::Command as StdCommand;
        let group = format!("-{pid}");
        let _ = StdCommand::new("kill").args(["-15", "--", &group]).status();
        let _ = StdCommand::new("kill")
            .args(["-15", &pid.to_string()])
            .status();

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
    #[cfg(not(unix))]
    {
        let _ = pid;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const REPORT: &str = r#"
{
  "numTotalTests": 3,
  "numPassedTests": 2,
  "numFailedTests": 1,
  "success": false,
  "testResults": [
    {
      "name": "/ws/backend/tests/app.test.js",
      "status": "failed",
      "message": "",
      "assertionResults": [
        { "ancestorTitles": ["POST /api/register"], "fullName": "POST /api/register valid", "title": "valid", "status": "passed", "failureMessages": [] },
        { "ancestorTitles": ["POST /api/register"], "fullName": "POST /api/register invalid", "title": "invalid", "status": "failed",
          "failureMessages": ["AssertionError: expected 200 to be 400 // Object.is equality\n\n- Expected\n+ Received\n"] }
      ]
    },
    {
      "name": "/ws/backend/tests/broken.test.js",
      "status": "failed",
      "message": "SyntaxError: Unexpected token (4:3)\n at parser.error",
      "assertionResults": []
    }
  ]
}"#;

    #[test]
    fn parses_summary_failures_and_load_errors() {
        let s = parse_vitest_json(REPORT).expect("report parses");
        assert_eq!(s.total, 3);
        assert_eq!(s.passed, 2);
        assert_eq!(s.failed, 1);
        assert_eq!(s.failures.len(), 1);
        let f = &s.failures[0];
        assert_eq!(f.file, "app.test.js");
        assert_eq!(f.title, "POST /api/register invalid");
        assert!(f.message.starts_with("AssertionError"), "got: {}", f.message);
        assert!(f.message.contains("⏎"), "multi-line message collapsed");

        assert_eq!(s.load_errors.len(), 1);
        assert!(s.load_errors[0].starts_with("broken.test.js:"));
        assert!(!s.all_passed());
    }

    #[test]
    fn passes_report_is_green() {
        let report = r#"{"numTotalTests":4,"numPassedTests":4,"numFailedTests":0,"success":true,"testResults":[{"name":"/ws/tests/x.test.js","status":"passed","message":"","assertionResults":[{"fullName":"x valid","status":"passed","failureMessages":[]}]}]}"#;
        let s = parse_vitest_json(report).unwrap();
        assert!(s.all_passed());
        assert_eq!(s.total, 4);
    }

    #[test]
    fn non_json_output_yields_none() {
        assert!(parse_vitest_json("npm ERR! missing script").is_none());
        assert!(parse_vitest_json("").is_none());
    }

    #[test]
    fn handles_json_with_noise_prefix() {
        let s = parse_vitest_json("some noise before {\"numTotalTests\":1,\"numPassedTests\":1,\"numFailedTests\":0,\"success\":true}");
        assert!(s.is_some());
    }
}
