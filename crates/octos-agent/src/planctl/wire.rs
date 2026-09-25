//! Turn marker protocol for the ARC host plan (mirrors `testgen::turn`).
//!
//! Wire format of a plan-carrying turn:
//!
//! ```text
//! [arc-plan]
//! {"v":1,"seq":3,"phase":"node",...}
//!
//! <ordinary user prompt>
//! ```
//!
//! The marker must be the FIRST non-whitespace token so free text can
//! never trip it (mid-sentence mentions are content, not protocol). The
//! next non-empty line carries the envelope as single-line compact JSON
//! (≤[`WIRE_MAX_BYTES`]); everything after it is the ordinary prompt.
//!
//! Failure mode is explicit, never blocking: the marker line AND the
//! payload line are ALWAYS stripped (bare JSON never enters the
//! model-visible history) and a short constant [`HOST_PARSE_NOTICE`] is
//! appended so the model sees the plan is absent this turn.
//!
//! Chaining: the loop runner scans `[arc-plan]` FIRST, then feeds the
//! stripped text through `testgen::turn::scan_turn_input` — both markers
//! can coexist on one turn.

use super::{
    Envelope, HOST_PARSE_NOTICE, MARKER, MAX_EXTRA_RULES_CHARS, MAX_RULESET_TEXT_CHARS,
    MAX_SHARED_CONTRACT_CHARS, MAX_STEP_TEXT_CHARS, MAX_STEPS, MAX_TASK_CHARS, MAX_VALIDATION_CHARS,
    WIRE_MAX_BYTES,
    feature_enabled,
};

/// A scanned turn input.
#[derive(Debug, Clone, PartialEq)]
pub struct WireScan {
    /// The user text with the marker + payload stripped (byte-identical to
    /// the input when the turn was not marked or the feature is off).
    pub text: String,
    /// `Some(Ok(envelope))` on a valid marked turn; `Some(Err(reason))`
    /// when the payload failed to parse or validate; `None` when the turn
    /// was not marked (or the feature is disabled).
    pub envelope: Option<Result<Envelope, String>>,
}

/// Scan turn input for [`MARKER`]. See the module docs for the wire
/// contract. A disabled feature (`OCTOS_ARC_PLAN=0`) is pass-through.
pub fn scan_turn_input(user_content: &str) -> WireScan {
    scan_turn_input_for(feature_enabled(), user_content)
}

/// Injected core of [`scan_turn_input`], unit-testable without env mutation.
fn scan_turn_input_for(enabled: bool, user_content: &str) -> WireScan {
    if !enabled {
        return WireScan {
            text: user_content.to_string(),
            envelope: None,
        };
    }
    let trimmed_start = user_content.trim_start();
    if !trimmed_start.starts_with(MARKER) {
        return WireScan {
            text: user_content.to_string(),
            envelope: None,
        };
    }

    // Marker line (any trailing content on it is harness metadata, not
    // content) → payload line (next NON-empty line) → ordinary prompt.
    let after_marker = trimmed_start.strip_prefix(MARKER).unwrap_or(trimmed_start);
    let rest = after_marker
        .split_once('\n')
        .map(|(_, r)| r)
        .unwrap_or(after_marker);

    let payload_start = rest.trim_start_matches(['\n', ' ', '\t']);
    if payload_start.is_empty() {
        // Marker with no payload at all: nothing to strip beyond the marker.
        return WireScan {
            text: String::new(),
            envelope: Some(Err("marker present but payload line missing".into())),
        };
    }

    let (payload_line, prompt) = match payload_start.split_once('\n') {
        Some((line, rest)) => (line, rest.trim_start_matches('\n')),
        None => (payload_start, ""),
    };

    let envelope = if payload_line.len() > WIRE_MAX_BYTES {
        Err(format!("payload line exceeds {WIRE_MAX_BYTES} bytes"))
    } else {
        serde_json::from_str::<Envelope>(payload_line)
            .map_err(|e| format!("payload JSON parse error: {e}"))
            .and_then(|env| validate_envelope(&env).map(|()| env))
    };

    WireScan {
        text: prompt.to_string(),
        envelope: Some(envelope),
    }
}

/// Compose the final user message for a scanned turn. Valid envelopes are
/// host-side state (they land in the system-row segment via
/// `Agent::arc_plan_apply`), so the user message stays the stripped prompt
/// verbatim. Failures keep the stripped prompt and append the short
/// constant notice — visible, bounded, never blocking.
pub fn compose_turn_message(scan: &WireScan) -> String {
    match &scan.envelope {
        None => scan.text.clone(),
        Some(Ok(_)) => scan.text.clone(),
        Some(Err(_)) => format!("{}\n\n{HOST_PARSE_NOTICE}", scan.text),
    }
}

/// Validate an envelope against the wire contract. Enforced invariants:
/// version, task/step text budgets, step count, id uniqueness, focus
/// membership, known ruleset. `host_signals` is host-authoritative and is
/// deliberately NOT validated here — a malformed report degrades segment
/// rendering, never the turn.
pub fn validate_envelope(env: &Envelope) -> Result<(), String> {
    if env.v != 1 {
        return Err(format!("unsupported envelope version {} (want 1)", env.v));
    }
    if env.task.chars().count() > MAX_TASK_CHARS {
        return Err(format!("task exceeds {MAX_TASK_CHARS} chars"));
    }
    if let Some(rules) = &env.rules {
        if rules.chars().count() > 64 {
            return Err("rules label exceeds 64 chars".into());
        }
    }
    if let Some(text) = &env.rules_text {
        if text.chars().count() > MAX_RULESET_TEXT_CHARS {
            return Err(format!("rules_text exceeds {MAX_RULESET_TEXT_CHARS} chars"));
        }
    }
    if let Some(extra) = &env.extra_rules {
        if extra.chars().count() > MAX_EXTRA_RULES_CHARS {
            return Err(format!("extra_rules exceeds {MAX_EXTRA_RULES_CHARS} chars"));
        }
    }
    if let Some(shared) = &env.shared_contract {
        if shared.chars().count() > MAX_SHARED_CONTRACT_CHARS {
            return Err(format!(
                "shared_contract exceeds {MAX_SHARED_CONTRACT_CHARS} chars"
            ));
        }
    }
    if env.steps.len() > MAX_STEPS {
        return Err(format!("{} steps exceeds {MAX_STEPS}", env.steps.len()));
    }
    let mut seen = std::collections::HashSet::new();
    for step in &env.steps {
        if step.id.is_empty() {
            return Err("step id must be non-empty".into());
        }
        if !seen.insert(step.id.as_str()) {
            return Err(format!("duplicate step id {:?}", step.id));
        }
        if step.input.chars().count() > MAX_STEP_TEXT_CHARS {
            return Err(format!(
                "step {:?} input exceeds {MAX_STEP_TEXT_CHARS} chars",
                step.id
            ));
        }
        if let Some(v) = &step.validation {
            if v.chars().count() > MAX_VALIDATION_CHARS {
                return Err(format!(
                    "step {:?} validation exceeds {MAX_VALIDATION_CHARS} chars",
                    step.id
                ));
            }
        }
        if step.expected_output.chars().count() > MAX_STEP_TEXT_CHARS {
            return Err(format!(
                "step {:?} expected_output exceeds {MAX_STEP_TEXT_CHARS} chars",
                step.id
            ));
        }
    }
    if let Some(focus) = &env.focus {
        if !env.steps.iter().any(|s| &s.id == focus) {
            return Err(format!("focus {focus:?} does not match any step"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::planctl::{PlanPhase, PlanStep, StepAction};

    fn sample_envelope() -> Envelope {
        let mut env = Envelope::default();
        env.seq = 3;
        env.phase = PlanPhase::Node;
        env.task = "火车票预订演示系统".into();
        env.steps = vec![PlanStep {
            id: "REQ-1".into(),
            action: StepAction::Modify,
            input: "注册旅客账号".into(),
            ..Default::default()
        }];
        env.focus = Some("REQ-1".into());
        env
    }

    fn marked_prompt(env: &Envelope, prompt: &str) -> String {
        format!(
            "{MARKER}\n{}\n\n{prompt}",
            serde_json::to_string(env).unwrap()
        )
    }

    #[test]
    fn unmarked_text_passes_through_byte_identical() {
        let text = "Build the register endpoint.";
        let scan = scan_turn_input(text);
        assert_eq!(scan.envelope, None);
        assert_eq!(scan.text, text);
        assert_eq!(compose_turn_message(&scan), text);
    }

    #[test]
    fn mid_text_mention_does_not_trip() {
        let text = "The marker [arc-plan] is documented in the host README.";
        let scan = scan_turn_input(text);
        assert_eq!(scan.envelope, None);
        assert_eq!(scan.text, text);
    }

    #[test]
    fn marked_turn_strips_marker_and_payload_keeps_prompt() {
        let env = sample_envelope();
        let input = marked_prompt(&env, "Implement REQ-1 now.");
        let scan = scan_turn_input(&input);
        let env_out = scan.envelope.clone().unwrap().unwrap();
        assert_eq!(env_out, env);
        assert_eq!(scan.text, "Implement REQ-1 now.");
        // Success path: no notice appended — the plan rides the system row.
        assert_eq!(compose_turn_message(&scan), "Implement REQ-1 now.");
    }

    #[test]
    fn feature_disabled_is_passthrough() {
        let env = sample_envelope();
        let input = marked_prompt(&env, "hello");
        let scan = scan_turn_input_for(false, &input);
        assert_eq!(scan.envelope, None);
        assert_eq!(scan.text, input, "disabled feature must not strip bytes");
    }

    #[test]
    fn invalid_json_is_stripped_with_notice() {
        let input = format!("{MARKER}\n{{not json}}\n\nFix the bug please.");
        let scan = scan_turn_input(&input);
        let err = scan.envelope.clone().unwrap().unwrap_err();
        assert!(err.contains("parse error"), "got: {err}");
        assert_eq!(scan.text, "Fix the bug please.");
        let composed = compose_turn_message(&scan);
        assert!(composed.starts_with("Fix the bug please."));
        assert!(composed.contains("failed validation"));
        assert!(!composed.contains("{not json}"), "bare JSON must not leak");
    }

    #[test]
    fn missing_payload_is_reported() {
        let input = format!("{MARKER}\n\nhello world");
        let scan = scan_turn_input(&input);
        assert!(scan.envelope.unwrap().is_err());
    }

    #[test]
    fn each_validation_failure_strips_and_reports() {
        // wrong version
        let mut env = sample_envelope();
        env.v = 2;
        // bad focus
        let mut env2 = sample_envelope();
        env2.focus = Some("NOPE".into());
        // duplicate ids
        let mut env3 = sample_envelope();
        env3.steps.push(env3.steps[0].clone());
        // overlong field
        let mut env4 = sample_envelope();
        env4.task = "x".repeat(MAX_TASK_CHARS + 1);
        // overlong rules label / rules_text / validation
        let mut env5 = sample_envelope();
        env5.rules = Some("x".repeat(65));
        let mut env5b = sample_envelope();
        env5b.rules = Some("any-label-passes".into());
        env5b.rules_text = Some("y".repeat(MAX_RULESET_TEXT_CHARS + 1));

        // too many steps
        let mut env6 = sample_envelope();
        env6.steps = (0..MAX_STEPS + 1)
            .map(|i| PlanStep {
                id: format!("S{i}"),
                ..Default::default()
            })
            .collect();
        // empty step id
        let mut env7 = sample_envelope();
        env7.steps[0].id = String::new();

        for env in [env, env2, env3, env4, env5, env6, env7] {
            let input = marked_prompt(&env, "prompt body");
            let scan = scan_turn_input(&input);
            let err = match &scan.envelope {
                Some(Err(e)) => e.clone(),
                other => panic!("marked turn must carry a scan result, got {other:?}"),
            };
            assert!(!err.is_empty());
            let composed = compose_turn_message(&scan);
            assert!(composed.contains("failed validation"));
            assert!(
                !composed.contains("\"seq\""),
                "envelope JSON must never leak into the prompt"
            );
        }
    }

    #[test]
    fn oversized_payload_line_is_rejected_without_parse() {
        let big = format!("\"{}\"", "x".repeat(WIRE_MAX_BYTES + 1));
        let input = format!("{MARKER}\n{big}\n\nprompt");
        let scan = scan_turn_input(&input);
        let err = scan.envelope.unwrap().unwrap_err();
        assert!(err.contains("exceeds"), "got: {err}");
        assert_eq!(scan.text, "prompt");
    }

    #[test]
    fn envelope_without_focus_is_valid() {
        let mut env = sample_envelope();
        env.focus = None;
        validate_envelope(&env).unwrap();
    }
}
