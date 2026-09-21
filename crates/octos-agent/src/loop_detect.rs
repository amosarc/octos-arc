//! Loop detection for the agent tool execution loop.
//!
//! Tracks tool call signatures (name + argument hash) and detects
//! repeating patterns in the last N calls. When a cycle is detected,
//! returns a warning message that should be injected as a system message.

use std::collections::HashMap;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

/// Soft "no-progress" hint appended to a tool result when the same
/// (name, args, result) triple has been seen 3 times in a row. The
/// LLM sees this on its next iteration as part of the most recent
/// tool result — it does not terminate the turn. Hard cycle detection
/// (existing logic) catches anything that survives this nudge.
///
/// This is the OpenClaw lesson — distinguish "no progress" (same args
/// AND same result) from legitimate polling (same args, different
/// result over time). The production loop on mini3 session 8w2ime had
/// kimi-k2.5 calling a contract-inspection tool 5 times with the same
/// args, all returning identical 4 KB trees. With result hashing, that
/// fires at iter 3 — early enough to nudge before the hard cycle
/// detector terminates the turn at iter 4. Legitimate polls like
/// `check_background_tasks` (which return different statuses while a
/// background job runs) are unaffected.
pub const NO_PROGRESS_HINT: &str = "\n\n[NO PROGRESS] You have now called this tool 3 times in a row with identical arguments AND received identical results. Calling it again will produce the same result. To make progress, either switch to a different tool (read_file / list_dir / view_image for file content) or finish the turn with the information you already have.";

const PEER_POLLING_HINT: &str = "\n\n[PEER POLLING] Three consecutive reads returned the same peer snapshot. Peer work is asynchronous: this does not prove failure or that a later read cannot change. Do not busy-wait. Reflect on the reported peer state, do independent work or use an available bounded wait, and gather fresh evidence before claiming completion.";

fn is_peer_polling_tool(tool_name: &str) -> bool {
    matches!(tool_name, "peer_gather" | "peer_list")
}

/// #1765: number of consecutive identical tool calls (same name +
/// identical arguments JSON) that trips the doom-loop guard. When the
/// LLM issues the SAME call this many times in a row, the conversation
/// loop aborts the turn with a clear model-and-user-facing message
/// instead of issuing the next LLM call — each further retry would only
/// burn tokens. Mirrors opencode's `DOOM_LOOP_THRESHOLD = 3`
/// (`packages/opencode/src/session/processor.ts:29`).
///
/// Scope: the guard is wired into the CONVERSATION loop
/// (`process_message_inner`) only. Asynchronous peer reads use result-aware
/// reflection instead: identical arguments cannot prove an immutable result.
/// The background task loop
/// deliberately keeps its softer treatment (the `record_result`
/// no-progress hint) because unattended tasks legitimately poll
/// status tools with identical arguments while a background job runs.
/// Verifier-configured agents are likewise exempt — the verifier lane
/// injects a `verdict: Repeating` note at this exact streak length and
/// the planner self-corrects, which is a richer recovery than an abort.
pub const DOOM_LOOP_THRESHOLD: usize = 3;

/// A semantic progress warning: the model is changing the same file over and
/// over even though the exact tool arguments differ, so signature-based loop
/// detection cannot see the churn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileChurnSignal {
    pub path: String,
    pub edits: usize,
    /// The second threshold asks the provider/router to move away from the
    /// current model where a stronger/healthier alternative is available.
    pub escalation: bool,
}

/// Green-stop signal: the same staged acceptance spec (`.arc/acceptance/…`,
/// the host's verbatim SPEC RUN command) passed IDENTICALLY twice with no
/// source-editing tool call in between. The node is verified done; the
/// conversation loop arms a short force-stop so the model wraps up instead
/// of re-verifying what is already green (keep-25: turns kept running
/// 200+s after a green spec run — the success signal never became a stop
/// signal).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GreenStopSignal {
    /// Trimmed spec-run command, for logs only.
    pub command: String,
    /// Tests passed in the (identical) green runs.
    pub passed: u32,
}

/// Per-turn green-stop tracking. A source edit (write_file / edit_file /
/// diff_edit / apply_patch via [`mutation_path`]) between two green runs
/// means the second green verifies NEW code — that is a fresh baseline, not
/// a redundant re-check, so it re-arms instead of firing. Shell commands
/// (npm run build, restart-server, curl) do NOT reset: dist/重启 are
/// derived state, idempotent without a source edit.
#[derive(Debug, Default)]
struct GreenStopState {
    last_green: Option<(u64, u32)>,
    source_edit_since_green: bool,
    pending: Option<GreenStopSignal>,
}

/// Tracks tool call patterns and detects loops.
pub struct LoopDetector {
    /// Ring buffer of recent tool call signatures (name + args).
    /// Used by `record()` for hard cycle detection.
    signatures: Vec<u64>,
    /// Ring buffer of recent (name + args + result) signatures.
    /// Used by `record_result()` for the OpenClaw-style "no progress"
    /// soft hint, which only fires when both args AND result repeat —
    /// distinguishing stuck loops from legitimate polling.
    result_signatures: Vec<u64>,
    /// Maximum window size to check for patterns.
    window: usize,
    /// #1765: signature of the most recent call recorded by
    /// [`Self::record_doom`], used to detect consecutive identical calls.
    doom_last_signature: Option<u64>,
    /// #1765: length of the current consecutive-identical-call streak.
    doom_streak: usize,
    /// Successful mutating calls per logical file path. This catches CSS/edit
    /// spirals whose replacement text changes on every call and therefore
    /// evades exact argument hashing.
    file_mutations: HashMap<String, usize>,
    file_churn_threshold: usize,
    pending_file_churn: Option<FileChurnSignal>,
    /// An unchanged asynchronous peer snapshot requests reflection, not a
    /// fabricated final or an abort before its next read can observe progress.
    pending_peer_polling: Option<String>,
    green: GreenStopState,
}

impl LoopDetector {
    /// Create a new detector with the given window size.
    pub fn new(window: usize) -> Self {
        Self {
            signatures: Vec::with_capacity(window * 2),
            result_signatures: Vec::with_capacity(window * 2),
            window,
            doom_last_signature: None,
            doom_streak: 0,
            file_mutations: HashMap::new(),
            file_churn_threshold: std::env::var("OCTOS_FILE_CHURN_THRESHOLD")
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
                .map(|value| value.clamp(2, 100))
                .unwrap_or(5),
            pending_file_churn: None,
            pending_peer_polling: None,
            green: GreenStopState::default(),
        }
    }

    /// Record a successful file-mutating tool call. Returns a model-facing
    /// hint at the first threshold and again at each threshold multiple. The
    /// second and later firings request provider/model escalation, but never
    /// terminate the user turn.
    pub fn record_file_mutation(
        &mut self,
        tool_name: &str,
        args: &serde_json::Value,
        success: bool,
    ) -> Option<String> {
        if !success {
            return None;
        }
        let path = mutation_path(tool_name, args)?;
        let edits = self.file_mutations.entry(path.clone()).or_default();
        *edits += 1;
        if *edits < self.file_churn_threshold || *edits % self.file_churn_threshold != 0 {
            return None;
        }

        let signal = FileChurnSignal {
            path: path.clone(),
            edits: *edits,
            escalation: *edits >= self.file_churn_threshold.saturating_mul(2),
        };
        let escalation_note = if signal.escalation {
            " This is a repeated threshold breach; the harness will request a provider/model escalation."
        } else {
            " The harness will run a tools-disabled synthesis checkpoint before more edits."
        };
        self.pending_file_churn = Some(signal);
        Some(format!(
            "\n\n[FILE CHURN] `{path}` has been modified {} times in this turn. Stop patching blindly: inspect the rendered/resulting state, identify the root cause, and choose one bounded next change.{escalation_note}",
            *edits
        ))
    }

    pub fn take_file_churn_signal(&mut self) -> Option<FileChurnSignal> {
        self.pending_file_churn.take()
    }

    pub fn take_peer_polling_signal(&mut self) -> Option<String> {
        self.pending_peer_polling.take()
    }

    /// Record one completed tool call for green-stop tracking. Call AFTER
    /// execution, in tool-result order (same site as [`Self::record_result`]).
    ///
    /// A shell call whose command references the host's staged acceptance
    /// specs (`.arc/acceptance`) with a green Playwright summary is a "green
    /// run". The same command going green again with NO source edit since
    /// the previous green arms a [`GreenStopSignal`]. Fires once per pair;
    /// after firing the pair state clears (a later green starts a new pair).
    pub fn record_green_stop(&mut self, tool_name: &str, args: &serde_json::Value, result: &str) {
        if mutation_path(tool_name, args).is_some() {
            self.green.source_edit_since_green = true;
            return;
        }
        let Some(command) = args
            .get("command")
            .and_then(serde_json::Value::as_str)
            .map(str::trim)
            .filter(|cmd| !cmd.is_empty())
        else {
            return;
        };
        if !command.contains(".arc/acceptance") {
            return;
        }
        let Some(passed) = parse_green_summary(result) else {
            return;
        };
        let mut hasher = DefaultHasher::new();
        command.hash(&mut hasher);
        let signature = hasher.finish();
        match self.green.last_green {
            Some((prior_signature, prior_passed))
                if prior_signature == signature
                    && prior_passed == passed
                    && !self.green.source_edit_since_green =>
            {
                self.green.pending = Some(GreenStopSignal {
                    command: command.chars().take(160).collect(),
                    passed,
                });
                self.green.last_green = None;
                self.green.source_edit_since_green = false;
            }
            _ => {
                self.green.last_green = Some((signature, passed));
                self.green.source_edit_since_green = false;
            }
        }
    }

    /// Take the pending green-stop signal, if the last completed batch
    /// armed one. Mirrors [`Self::take_file_churn_signal`] /
    /// [`Self::take_peer_polling_signal`].
    pub fn take_green_stop_signal(&mut self) -> Option<GreenStopSignal> {
        self.green.pending.take()
    }

    /// #1765: record a tool call for the doom-loop guard and return the
    /// streak length when it reaches [`DOOM_LOOP_THRESHOLD`].
    ///
    /// A call is "identical" when both the tool name and the exact
    /// arguments JSON match the previous call; any non-identical call
    /// resets the streak to 1. Returns `Some(streak)` for every call at
    /// or past the threshold (not just the first) so a caller that
    /// defers the abort once — e.g. because the shell-spiral recovery
    /// path owns the streak — still gets a signal on the next repeat.
    pub fn record_doom(&mut self, tool_name: &str, args: &serde_json::Value) -> Option<usize> {
        if is_peer_polling_tool(tool_name) {
            // An asynchronous read breaks a consecutive mutating-call streak;
            // its progress cannot be known before the tool actually executes.
            self.doom_last_signature = None;
            self.doom_streak = 0;
            return None;
        }
        let sig = Self::signature(tool_name, args);
        if self.doom_last_signature == Some(sig) {
            self.doom_streak += 1;
        } else {
            self.doom_last_signature = Some(sig);
            self.doom_streak = 1;
        }
        (self.doom_streak >= DOOM_LOOP_THRESHOLD).then_some(self.doom_streak)
    }

    /// Record a tool call and check for repeating patterns.
    /// Returns a warning message if a loop is detected.
    pub fn record(&mut self, tool_name: &str, args: &serde_json::Value) -> Option<String> {
        if is_peer_polling_tool(tool_name) {
            // Recorded AFTER execution with its result hash instead. Keep the
            // history so genuine mixed mutating-tool cycles remain protected.
            return None;
        }
        let sig = Self::signature(tool_name, args);
        self.push_call_signature(sig);

        // Only check once we have enough history
        if self.signatures.len() < 4 {
            return None;
        }

        let len = self.signatures.len();
        let check_len = len.min(self.window);
        let window = &self.signatures[len - check_len..];

        // Check for cycles of length 1, 2, and 3
        for cycle_len in 1..=3 {
            if check_len >= cycle_len * 3 && Self::is_repeating(window, cycle_len) {
                return Some(format!(
                    "[LOOP DETECTED] The last {check_len} tool calls follow a repeating pattern \
                     (cycle length {cycle_len}). Try a different approach or break the cycle."
                ));
            }
        }

        None
    }

    fn push_call_signature(&mut self, signature: u64) {
        self.signatures.push(signature);
        if self.signatures.len() > self.window * 2 {
            let drain_to = self.signatures.len() - self.window;
            self.signatures.drain(..drain_to);
        }
    }

    /// Compute a signature hash for a tool call.
    fn signature(name: &str, args: &serde_json::Value) -> u64 {
        let mut hasher = DefaultHasher::new();
        name.hash(&mut hasher);
        // Hash the JSON string representation for stability
        let args_str = args.to_string();
        args_str.hash(&mut hasher);
        hasher.finish()
    }

    /// Compute a signature hash for a tool call AND its result.
    fn signature_with_result(name: &str, args: &serde_json::Value, result: &str) -> u64 {
        let mut hasher = DefaultHasher::new();
        name.hash(&mut hasher);
        let args_str = args.to_string();
        args_str.hash(&mut hasher);
        result.hash(&mut hasher);
        hasher.finish()
    }

    /// Record a tool call's RESULT after the tool has executed.
    ///
    /// Returns `Some(NO_PROGRESS_HINT)` when the last 3 records for
    /// this `(name, args, result)` triple are identical — meaning the
    /// LLM called the same tool with the same args 3 times in a row
    /// AND got the same result each time. This is the "no progress"
    /// signal that distinguishes stuck loops from legitimate polling.
    ///
    /// Fires at most once per "burst" — after firing, the result
    /// signature ring is cleared so a 4th identical call does not
    /// re-fire (the existing `record()` cycle detector picks up
    /// anything that survives the soft nudge).
    ///
    /// Callers should append the returned hint to the tool result
    /// message's content so the LLM sees it as part of its next-turn
    /// context. Does NOT terminate the turn — that's the hard
    /// detector's job.
    pub fn record_result(
        &mut self,
        tool_name: &str,
        args: &serde_json::Value,
        result: &str,
    ) -> Option<String> {
        let sig = Self::signature_with_result(tool_name, args, result);
        if is_peer_polling_tool(tool_name) {
            self.push_call_signature(sig);
        }
        self.result_signatures.push(sig);

        // Bound the ring buffer
        if self.result_signatures.len() > self.window * 2 {
            let drain_to = self.result_signatures.len() - self.window;
            self.result_signatures.drain(..drain_to);
        }

        let len = self.result_signatures.len();
        if len < 3 {
            return None;
        }
        let last3 = &self.result_signatures[len - 3..];
        if last3[0] == last3[1] && last3[1] == last3[2] {
            // Fire once — clear so a 4th identical call won't re-fire.
            self.result_signatures.clear();
            if is_peer_polling_tool(tool_name) {
                self.pending_peer_polling = Some(tool_name.to_owned());
                return Some(PEER_POLLING_HINT.to_owned());
            }
            return Some(NO_PROGRESS_HINT.to_string());
        }
        None
    }

    /// Check if the window contains a repeating pattern of the given cycle length.
    /// Requires at least 3 full repetitions of the cycle.
    fn is_repeating(window: &[u64], cycle_len: usize) -> bool {
        if window.len() < cycle_len * 3 {
            return false;
        }
        let tail = &window[window.len() - cycle_len * 3..];
        let pattern = &tail[..cycle_len];
        tail[cycle_len..cycle_len * 2] == *pattern && tail[cycle_len * 2..] == *pattern
    }
}

fn mutation_path(tool_name: &str, args: &serde_json::Value) -> Option<String> {
    if !matches!(
        tool_name,
        "write_file" | "edit_file" | "diff_edit" | "apply_patch"
    ) {
        return None;
    }
    ["path", "file_path", "filename", "file"]
        .into_iter()
        .find_map(|key| args.get(key).and_then(serde_json::Value::as_str))
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .map(ToOwned::to_owned)
}

/// Extract the passed-test count from a Playwright summary, `Some` only for
/// an unambiguously green run: at least one `N passed` line and no
/// `failed` / `flaky` / `did not run` / `interrupted` line anywhere in the
/// output. Playwright prints the summary as
/// `  3 passed (12.4s)`; failures print `  2 failed │ 1 passed`.
fn parse_green_summary(result: &str) -> Option<u32> {
    let mut passed: Option<u32> = None;
    for line in result.lines() {
        let line = line.trim();
        let digits: String = line.chars().take_while(|c| c.is_ascii_digit()).collect();
        if digits.is_empty() {
            continue;
        }
        let after = line[digits.len()..].trim_start();
        if after.starts_with("passed") {
            passed = digits.parse().ok();
        } else if after.starts_with("failed")
            || after.starts_with("flaky")
            || after.starts_with("did not run")
            || after.starts_with("interrupted")
        {
            return None;
        }
    }
    passed.filter(|count| *count > 0)
}


#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // ---------- green-stop (loop_detect) ----------

    const SPEC_CMD: &str = "npx playwright test .arc/acceptance/REQ-1.1.spec.ts --workers=1";
    const GREEN_OUT: &str =
        "Running 1 test using 1 worker\n\n  ✓ REQ-1.1\n\n  1 passed (5.6s)";

    fn record_spec(mut detector: &mut LoopDetector, command: &str, output: &str) {
        detector.record_green_stop("shell", &json!({ "command": command }), output);
    }

    #[test]
    fn green_summary_parses_pure_pass() {
        assert_eq!(parse_green_summary("  3 passed (12.4s)"), Some(3));
        assert_eq!(parse_green_summary(GREEN_OUT), Some(1));
        assert_eq!(parse_green_summary("  0 passed (2.0s)"), None);
    }

    #[test]
    fn green_summary_rejects_failures_flaky_and_interruptions() {
        assert_eq!(parse_green_summary("  2 failed │ 1 passed (3.0s)"), None);
        assert_eq!(parse_green_summary("  1 flaky (3.0s)"), None);
        assert_eq!(parse_green_summary("  2 did not run (3.0s)"), None);
        assert_eq!(parse_green_summary("  1 interrupted (3.0s)"), None);
        assert_eq!(parse_green_summary("no summary at all"), None);
    }

    #[test]
    fn identical_double_green_with_no_edit_arms_signal() {
        let mut d = LoopDetector::new(10);
        assert!(d.take_green_stop_signal().is_none());
        record_spec(&mut d, SPEC_CMD, GREEN_OUT);
        assert!(d.take_green_stop_signal().is_none(), "first green only arms");
        record_spec(&mut d, SPEC_CMD, GREEN_OUT);
        let signal = d.take_green_stop_signal().expect("second identical green fires");
        assert_eq!(signal.passed, 1);
        assert!(signal.command.contains("REQ-1.1"));
        // fires once per pair
        assert!(d.take_green_stop_signal().is_none());
    }

    #[test]
    fn source_edit_between_greens_rearms_instead_of_firing() {
        let mut d = LoopDetector::new(10);
        record_spec(&mut d, SPEC_CMD, GREEN_OUT);
        d.record_green_stop(
            "edit_file",
            &json!({ "path": "frontend/src/main.jsx", "old": "a", "new": "b" }),
            "ok",
        );
        // A shell build between greens does NOT count as a source edit.
        d.record_green_stop(
            "shell",
            &json!({ "command": "npm run build --prefix frontend" }),
            "built",
        );
        record_spec(&mut d, SPEC_CMD, GREEN_OUT);
        assert!(
            d.take_green_stop_signal().is_none(),
            "green after an edit verifies NEW code: fresh baseline, no fire"
        );
        // A third green with no further edit now fires.
        record_spec(&mut d, SPEC_CMD, GREEN_OUT);
        assert!(d.take_green_stop_signal().is_some());
    }

    #[test]
    fn different_pass_counts_do_not_fire() {
        let mut d = LoopDetector::new(10);
        record_spec(&mut d, SPEC_CMD, "  1 passed (1.0s)");
        record_spec(&mut d, SPEC_CMD, "  2 passed (1.0s)");
        assert!(d.take_green_stop_signal().is_none());
    }

    #[test]
    fn non_spec_commands_are_ignored() {
        let mut d = LoopDetector::new(10);
        for _ in 0..3 {
            d.record_green_stop(
                "shell",
                &json!({ "command": "npm run build --prefix frontend" }),
                GREEN_OUT,
            );
        }
        assert!(d.take_green_stop_signal().is_none());
    }

    fn detector_with_churn_threshold(threshold: usize) -> LoopDetector {
        let mut detector = LoopDetector::new(10);
        detector.file_churn_threshold = threshold;
        detector
    }

    #[test]
    fn peer_polling_should_preserve_stuck_mutating_cycles_but_honor_changed_peer_results() {
        for changing in [false, true] {
            let mut detector = LoopDetector::new(12);
            let mutation = json!({"path":"a.txt", "content":"unchanged"});
            for index in 0..3 {
                assert!(detector.record_doom("write_file", &mutation).is_none());
                assert!(detector.record("write_file", &mutation).is_none());
                assert!(detector.record_doom("peer_gather", &json!({})).is_none());
                assert!(detector.record("peer_gather", &json!({})).is_none());
                let result = if changing {
                    format!("progress {index}")
                } else {
                    "unchanged".into()
                };
                let _ = detector.record_result("peer_gather", &json!({}), &result);
            }
            assert_eq!(
                detector.record("write_file", &mutation).is_some(),
                !changing,
                "unchanged peer polling must not erase protection for a real repeated mutation cycle"
            );
        }
    }

    #[test]
    fn same_file_churn_detects_varied_edit_arguments() {
        let mut detector = detector_with_churn_threshold(3);
        for index in 0..2 {
            assert!(
                detector
                    .record_file_mutation(
                        "edit_file",
                        &json!({"path": "app/globals.css", "replacement": index}),
                        true,
                    )
                    .is_none()
            );
        }
        let hint = detector
            .record_file_mutation(
                "edit_file",
                &json!({"path": "app/globals.css", "replacement": 2}),
                true,
            )
            .expect("third successful edit should warn");
        assert!(hint.contains("FILE CHURN"));
        assert_eq!(
            detector.take_file_churn_signal(),
            Some(FileChurnSignal {
                path: "app/globals.css".into(),
                edits: 3,
                escalation: false,
            })
        );
    }

    #[test]
    fn failed_or_read_only_calls_do_not_count_as_file_churn() {
        let mut detector = detector_with_churn_threshold(2);
        assert!(
            detector
                .record_file_mutation("edit_file", &json!({"path": "a.rs"}), false)
                .is_none()
        );
        assert!(
            detector
                .record_file_mutation("read_file", &json!({"path": "a.rs"}), true)
                .is_none()
        );
        assert!(detector.take_file_churn_signal().is_none());
    }

    #[test]
    fn should_not_detect_on_few_calls() {
        let mut d = LoopDetector::new(10);
        assert!(d.record("shell", &json!({"command": "ls"})).is_none());
        assert!(d.record("shell", &json!({"command": "ls"})).is_none());
        assert!(d.record("shell", &json!({"command": "ls"})).is_none());
    }

    #[test]
    fn should_detect_single_call_loop() {
        let mut d = LoopDetector::new(10);
        let args = json!({"command": "cat foo.txt"});
        // Need 4 identical calls for 3 repetitions of cycle-1 pattern
        for _ in 0..3 {
            assert!(d.record("read_file", &args).is_none());
        }
        let warning = d.record("read_file", &args);
        assert!(warning.is_some());
        assert!(warning.unwrap().contains("LOOP DETECTED"));
    }

    #[test]
    fn record_result_fires_after_three_identical_triples() {
        let mut d = LoopDetector::new(10);
        let args = json!({"project": "slides/demo"});
        let result = "{\"contracts\":[{\"ready\":true}]}";
        assert!(d.record_result("check", &args, result).is_none());
        assert!(d.record_result("check", &args, result).is_none());
        let hint = d.record_result("check", &args, result);
        assert!(
            hint.is_some(),
            "expected NO PROGRESS hint after 3 identical"
        );
        assert!(hint.unwrap().contains("NO PROGRESS"));
    }

    #[test]
    fn should_fire_doom_when_third_identical_call_arrives() {
        let mut d = LoopDetector::new(10);
        let args = json!({"path": "a.txt"});
        assert!(d.record_doom("read_file", &args).is_none());
        assert!(d.record_doom("read_file", &args).is_none());
        assert_eq!(d.record_doom("read_file", &args), Some(3));
    }

    #[test]
    fn should_not_fire_doom_for_alternating_calls() {
        let mut d = LoopDetector::new(10);
        for _ in 0..10 {
            assert!(d.record_doom("read_file", &json!({"path": "a"})).is_none());
            assert!(d.record_doom("read_file", &json!({"path": "b"})).is_none());
        }
    }
}
