//! Turn-level test-task marking for the testgen pipeline.
//!
//! A harness (or an upstream planner) marks a conversation turn as a
//! test-generation task by prefixing the user text with
//! [`TEST_TASK_MARKER`]. The agent loop strips the marker and appends a
//! bounded host notice carrying the TestWriter role prompt plus the
//! scaffold→fill→verify loop contract — the in-session equivalent of
//! spawning a `test_writer` child (which solo/stdio sessions cannot do,
//! since `spawn` is only registered by the session actor).
//!
//! This is the "节点初始化默认 TestWriter + 注入上下文" step of the
//! pipeline: the ROLE text is direction-only (≤45 words); the actual
//! coverage rules are enforced mechanically by the `testgen` tool's
//! verdict, never by prompt prose.

use crate::role_template::ROLE_TEST_WRITER_PROMPT_PREFIX;

/// Machine marker prefixing a test-generation turn. Harness-owned; the
/// marker line is stripped from the text the model sees.
pub const TEST_TASK_MARKER: &str = "[task-kind:test]";

/// Host notice appended to a marked turn. Machine-terse: the four verdicts
/// of the `testgen verify` pipeline with the action each one demands.
pub const TEST_TASK_NOTICE: &str = "[testgen] Host notice: this turn is a TEST task.\n\
1. Call `testgen {\"action\":\"scaffold\"}` first — the host extracts the API contract + \
validation checklist and writes test skeletons with pre-named stub cases (do NOT re-read \
backend source to rediscover interfaces).\n\
2. Fill EVERY stub (keep the exact titles), then call `testgen {\"action\":\"verify\"}`.\n\
3. Act on the verdict only: `gap` → fill the listed stubs and re-verify; `test_err` → fix \
YOUR test code; `app_bug` → the application is wrong: STOP and report the evidence, do not \
edit application code; `covered` → done.\n";

/// A scanned turn input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TestTaskScan {
    /// The user text with the marker line stripped (unchanged when absent).
    pub text: String,
    /// `Some(notice)` when the turn was marked — append to the user message.
    pub notice: Option<&'static str>,
}

/// Scan turn input for [`TEST_TASK_MARKER`]. The marker must appear as the
/// first non-whitespace token so free text can never trip it; the rest of
/// the line after the marker is discarded (it is harness metadata, not
/// content).
pub fn scan_turn_input(user_content: &str) -> TestTaskScan {
    let trimmed_start = user_content.trim_start();
    if !trimmed_start.starts_with(TEST_TASK_MARKER) {
        return TestTaskScan {
            text: user_content.to_string(),
            notice: None,
        };
    }
    let rest = trimmed_start
        .strip_prefix(TEST_TASK_MARKER)
        .unwrap_or(trimmed_start);
    // Drop the remainder of the marker line, then the leading newline(s).
    let rest = rest.split_once('\n').map(|(_, r)| r).unwrap_or(rest);
    let text = rest.trim_start_matches('\n').to_string();
    TestTaskScan {
        text,
        notice: Some(TEST_TASK_NOTICE),
    }
}

/// Compose the final user message for a scanned turn: original text first
/// (stable for provider prompt caches), host notice appended after.
pub fn compose_turn_message(scan: &TestTaskScan) -> String {
    match scan.notice {
        Some(notice) => format!(
            "{}\n\n{notice}{}",
            scan.text, ROLE_TEST_WRITER_PROMPT_PREFIX
        ),
        None => scan.text.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unmarked_text_passes_through_unchanged() {
        let scan = scan_turn_input("Write unit tests for the register endpoint.");
        assert_eq!(scan.notice, None);
        assert_eq!(scan.text, "Write unit tests for the register endpoint.");
        // Compose is the identity for unmarked turns.
        assert_eq!(compose_turn_message(&scan), scan.text);
    }

    #[test]
    fn marked_turn_is_stripped_and_injected() {
        let input = "[task-kind:test]\nGive REQ-1 full test coverage.";
        let scan = scan_turn_input(input);
        assert_eq!(scan.notice, Some(TEST_TASK_NOTICE));
        assert_eq!(scan.text, "Give REQ-1 full test coverage.");

        let composed = compose_turn_message(&scan);
        assert!(composed.starts_with("Give REQ-1 full test coverage."));
        assert!(composed.contains("[testgen] Host notice"));
        // The role prompt travels verbatim — same words a spawned
        // test_writer child would receive.
        assert!(composed.contains(ROLE_TEST_WRITER_PROMPT_PREFIX));
    }

    #[test]
    fn marker_must_be_first_token_to_trip() {
        // Mid-text mention is NOT a marker.
        let text = "The marker [task-kind:test] is documented here.";
        let scan = scan_turn_input(text);
        assert_eq!(scan.notice, None);
        assert_eq!(scan.text, text);
    }

    #[test]
    fn marker_with_trailing_metadata_line_is_dropped() {
        let input = "[task-kind:test] REQ-1 priority\nCover the register endpoint.";
        let scan = scan_turn_input(input);
        assert_eq!(scan.notice, Some(TEST_TASK_NOTICE));
        assert_eq!(scan.text, "Cover the register endpoint.");
    }

    #[test]
    fn marker_alone_yields_empty_text_with_notice() {
        let scan = scan_turn_input("[task-kind:test]\n");
        assert_eq!(scan.notice, Some(TEST_TASK_NOTICE));
        assert_eq!(scan.text, "");
    }
}
