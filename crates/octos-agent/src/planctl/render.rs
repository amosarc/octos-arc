//! L1 segment rendering: budget, pressure degrade, prefix stability.
//!
//! The rendered segment is the LAST content in the system row (appended
//! after the tool-use discipline block), wrapped in the
//! [`SENTINEL_BEGIN`]…[`SENTINEL_END`] pair. The kernel is a MECHANISM
//! layer and ships zero domain prose: the CONTRACT block renders
//! `Envelope.rules_text` (host-supplied grading-stack facts) verbatim,
//! and the FOCUS block renders `PlanStep.validation` (the task's
//! Validation contract, extracted from requirements.yaml in the plan
//! phase) verbatim. Rules travel with the task; the kernel only
//! structures, budgets, and keeps the prefix stable.
//!
//! Cache law invariants:
//!
//! 1. The CONTRACT block bytes are stable within a turn — they depend
//!    only on envelope data that does not change mid-turn.
//! 2. Section order is fixed: header → CONTRACT → PLAN → FOCUS →
//!    REPO MAP → HOST REPORT → footer. Stable sections first, volatile
//!    last, so a changed focus/report never disturbs the prefix above.
//! 3. Pressure degrade ([`PressureMode`]) only shrinks BELOW the FOCUS
//!    boundary content that would change anyway; it never rewords the
//!    CONTRACT.
//!
//! Eviction order under budget pressure (§6 fit_budget): repo-map tail
//! lines → HOST REPORT detail rows → non-focus step signals → shared
//! signals → step descriptions → non-focus steps folded to a count →
//! test-title signals → minimal (focus id + port line + pointer).

use super::{
    AcceptanceSignal, Envelope, PlanPhase, PlanState, PlanStep, PressureMode, SENTINEL_BEGIN,
    SENTINEL_END, SignalKind, StepStatus,
};
use octos_llm::context::estimate_tokens;

/// Byte-frozen ruleset for `rules: "arc_web_v1"` — the union of the
/// legacy `UI_CONTRACT_PROMPT` semantics and the React(Vite)+Express
/// grading shape. Port discipline / stop-server / smoke steps are
/// deliberately ABSENT: the host rehearses builds, boots, and route
/// probes itself, so those instructions became host behavior, not prose.
/// One `file:symbol` localization row for the tail block.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChangeNote {
    pub file: String,
    pub symbol: Option<String>,
    pub line: Option<usize>,
}

impl ChangeNote {
    pub fn new(file: impl Into<String>) -> Self {
        Self {
            file: file.into(),
            symbol: None,
            line: None,
        }
    }
}

/// STABLE system-row segment: CONTRACT only (host rules_text + extra
/// rules + port line + staged-spec pointer), sentinels included.
/// Byte-stable across turns — the provider prefix cache covers this and
/// everything after it permanently, and it rides `messages[0]` through
/// compaction. The kernel adds nothing to the host's text.
pub fn render_stable_segment(state: &PlanState) -> String {
    let env = &state.envelope;
    let mut body = String::from("== CONTRACT ==\n");
    if let Some(text) = &env.rules_text {
        body.push_str(text);
        body.push('\n');
    }
    if let Some(extra) = &env.extra_rules {
        body.push_str(extra);
        body.push('\n');
    }
    body.push_str(&ports_line(&env.ports));
    if let Some(shared) = &env.shared_contract {
        body.push_str(
            "== SHARED REQUIREMENTS == (folder-level contracts; apply to \
every node)\n",
        );
        body.push_str(shared);
        body.push('\n');
    }
    // The pointer only ships when the host actually staged specs —
    // requirements-only tasks must not see a phantom directory.
    if env.specs_staged {
        // F2: spec_dir is ALREADY the full relative path (default
        // ".arc/acceptance") — concatenating the default again produced the
        // bogus ".arc/acceptance/.arc/acceptance/" pointer.
        body.push_str(&format!(
            "Acceptance specs are staged read-only at {}/ — never edit, \
copy, or delete them.\n",
            env.spec_dir
        ));
    }
    wrap(&body)
}

/// VOLATILE turn-tail: header + PLAN + FOCUS + REPO MAP + HOST REPORT,
/// wrapped in an `<arc-plan-turn>` block. Composed ONCE at turn start
/// and prepended to that turn's user message — new content on every
/// turn, so rendering it never disturbs the prefix cache. Pressure
/// degrade shrinks THIS block only; the system row is never rewritten.
pub fn render_turn_tail(state: &PlanState, mode: PressureMode) -> String {
    let budget = mode.tail_budget();
    if mode == PressureMode::Minimal {
        return wrap_tail(&render_tail_minimal(state));
    }
    for level in [
        Degrade::Full,
        Degrade::TrimRepoMap,
        Degrade::CompactReport,
        Degrade::TrimSignals,
        Degrade::CompactSteps,
        Degrade::FoldSteps,
        Degrade::Minimal,
    ] {
        let body = render_tail_at(state, level);
        if estimate_tokens(&body) <= budget || level == Degrade::Minimal {
            return wrap_tail(&body);
        }
    }
    unreachable!("degrade ladder always terminates at Degrade::Minimal")
}

fn wrap_tail(body: &str) -> String {
    format!("<arc-plan-turn>\n{body}</arc-plan-turn>")
}

fn wrap(body: &str) -> String {
    format!("{SENTINEL_BEGIN}\n{body}\n{SENTINEL_END}")
}

/// How much of each volatile section survives the budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Degrade {
    /// Everything.
    Full,
    /// Repo map trimmed to its head lines.
    TrimRepoMap,
    /// Host report: verdict lines only (drop the route table).
    CompactReport,
    /// Signals: focus ≤4, no shared, no test titles.
    TrimSignals,
    /// Steps: id + status + 40-char input; validation head 200 chars.
    CompactSteps,
    /// Non-focus steps folded to a count.
    FoldSteps,
    /// Minimal: focus id + validation head + pointer.
    Minimal,
}

fn render_tail_at(state: &PlanState, level: Degrade) -> String {
    if level == Degrade::Minimal {
        return render_tail_minimal(state);
    }
    let env = &state.envelope;
    let mut out = String::new();

    // ① header (volatile: seq advances every envelope)
    out.push_str(&format!(
        "PLAN seq={} phase={} task={}\n",
        env.seq,
        phase_name(env),
        env.task
    ));

    // ② PLAN checklist
    out.push_str("== PLAN ==\n");
    match level {
        Degrade::FoldSteps => {
            if let Some(focus) = env.focus.as_deref() {
                if let Some(step) = env.steps.iter().find(|s| s.id == focus) {
                    out.push_str(&checklist_line(step));
                }
            }
            let others = env
                .steps
                .iter()
                .filter(|s| env.focus.as_deref() != Some(s.id.as_str()))
                .count();
            if others > 0 {
                out.push_str(&format!("(+{others} other steps)\n"));
            }
        }
        _ => {
            for step in &env.steps {
                out.push_str(&checklist_line(step));
            }
        }
    }

    // ③ FOCUS — the task's Validation contract leads, then signals and
    // action chains. Kept on every tier (compacted under pressure);
    // losing it entirely would leave the model without the task's rules.
    if let Some(focus_id) = env.focus.as_deref() {
        if let Some(step) = env.steps.iter().find(|s| s.id == focus_id) {
            out.push_str(&format!("== FOCUS {focus_id} ==\n"));
            if let Some(validation) = &step.validation {
                let budget = match level {
                    Degrade::CompactSteps => 200,
                    l if l >= Degrade::FoldSteps => 600,
                    _ => crate::planctl::MAX_VALIDATION_CHARS,
                };
                out.push_str("validation: ");
                out.push_str(&truncate_chars(validation, budget));
                out.push('\n');
            }
            match level {
                Degrade::CompactSteps => {
                    out.push_str(&format!(
                        "{} {}\n",
                        step.action_name(),
                        truncate_chars(&step.input, 40)
                    ));
                }
                _ => {
                    out.push_str(&format!(
                        "expected: {}\n",
                        truncate_chars(&step.expected_output, 600)
                    ));
                }
            }
            let focus_signals = state.inventory.by_step.get(focus_id);
            let signal_cap = match level {
                Degrade::TrimSignals => 4,
                _ => 12,
            };
            if let Some(signals) = focus_signals {
                let shown = render_signals(signals, signal_cap, level);
                if !shown.is_empty() {
                    out.push_str(&format!("signals:\n{shown}"));
                }
            }
            if level < Degrade::TrimSignals {
                let summaries: Vec<&str> = state
                    .inventory
                    .summaries
                    .iter()
                    .filter(|s| s.step_id.as_deref() == Some(focus_id))
                    .map(|s| s.summary.as_str())
                    .take(12)
                    .collect();
                if !summaries.is_empty() {
                    out.push_str("actions:\n");
                    for s in summaries {
                        out.push_str(&format!("- {s}\n"));
                    }
                }
                if level == Degrade::Full {
                    let shared = state.inventory.globals.clone();
                    if !shared.is_empty() {
                        out.push_str(&format!(
                            "shared signals ({}):\n{}",
                            shared.len(),
                            render_signals(&shared, 16, level)
                        ));
                    }
                }
            }
        }
    }

    // ④ REPO MAP
    out.push_str("== REPO MAP ==\n");
    out.push_str("Live workspace signals arrive as the most recent <arc-workspace> block.\n");
    match &state.index.repo_map_text {
        Some(map) if !map.is_empty() => {
            let map = match level {
                Degrade::Full => map.clone(),
                _ => truncate_lines(map, 12),
            };
            out.push_str(&map);
            if !map.ends_with('\n') {
                out.push('\n');
            }
        }
        _ => out.push_str("(workspace not scaffolded yet)\n"),
    }

    // ⑤ HOST REPORT
    out.push_str("== HOST REPORT ==\n");
    if let Some(signals) = &env.host_signals {
        if let Some(r) = &signals.rehearsal {
            out.push_str(&format!(
                "rehearsal: build {} boot {}\n",
                mark(r.build_ok),
                mark(r.boot_ok)
            ));
            if level <= Degrade::TrimRepoMap && !r.routes.is_empty() {
                let probes: Vec<String> = r
                    .routes
                    .iter()
                    .take(8)
                    .map(|p| format!("{} {} {}", p.method, p.path, p.status))
                    .collect();
                out.push_str(&format!("routes: {}\n", probes.join(" | ")));
            }
            if level <= Degrade::TrimRepoMap && !signals.validation_probes.is_empty() {
                let probes: Vec<String> = signals
                    .validation_probes
                    .iter()
                    .take(6)
                    .map(|p| format!("{} {} -> {}", p.method, p.path, p.status))
                    .collect();
                out.push_str(&format!(
                    "validation probes (5xx is a host-verified FAILURE; a 2xx on \
garbage input means the backend did not re-validate — check the contract):\n{}\n",
                    probes.join("\n")
                ));
            }
            if !r.digest.is_empty() {
                out.push_str(&format!("digest: {}\n", truncate_chars(&r.digest, 300)));
            }
        }
        if let Some(run) = &signals.spec_run {
            out.push_str(&format!(
                "spec run: {} passed / {} failed {}\n",
                run.passed,
                run.failed,
                truncate_chars(&run.digest, 200)
            ));
        }
        if !signals.scaffolded_by_host.is_empty() {
            out.push_str(&format!(
                "scaffolded by host (already exist, do NOT rewrite): {}\n",
                signals.scaffolded_by_host.join(", ")
            ));
        }
    } else {
        out.push_str("(no host signals yet)\n");
    }
    out
}

/// Minimal tail: focus id + validation head — the task's rules are the
/// last thing to drop.
fn render_tail_minimal(state: &PlanState) -> String {
    let env = &state.envelope;
    let mut out = format!(
        "PLAN seq={} phase={} task={}\n",
        env.seq,
        phase_name(env),
        env.task
    );
    if let Some(focus) = env.focus.as_deref() {
        out.push_str(&format!("FOCUS {focus}\n"));
        if let Some(step) = env.steps.iter().find(|s| s.id == focus) {
            if let Some(validation) = &step.validation {
                out.push_str(&format!(
                    "validation: {}\n",
                    truncate_chars(validation, 120)
                ));
            }
        }
    }
    out.push_str("Full contract in the system row; specs under .arc/acceptance/.\n");
    out
}

fn phase_name(env: &Envelope) -> &'static str {
    match env.phase {
        PlanPhase::Skeleton => "skeleton",
        PlanPhase::Node => "node",
        PlanPhase::Final => "final",
        PlanPhase::Repair => "repair",
    }
}

fn ports_line(ports: &super::PortContract) -> String {
    if ports.extra.is_empty() {
        format!("Ports: grade={} smoke={}\n", ports.grade, ports.smoke)
    } else {
        let extra: Vec<String> = ports.extra.iter().map(|p| p.to_string()).collect();
        format!(
            "Ports: grade={} smoke={} extra={}\n",
            ports.grade,
            ports.smoke,
            extra.join(",")
        )
    }
}

fn checklist_line(step: &PlanStep) -> String {
    let box_ = match step.status {
        StepStatus::Done => "[x]",
        StepStatus::Active => "[~]",
        _ => "[ ]",
    };
    format!(
        "{box_} {} {} {}\n",
        step.id,
        step.action_name(),
        truncate_chars(&step.input, 80)
    )
}

fn mark(ok: bool) -> &'static str {
    if ok { "OK" } else { "FAIL" }
}

fn render_signals(signals: &[AcceptanceSignal], cap: usize, level: Degrade) -> String {
    let mut out = String::new();
    let mut shown = 0usize;
    let mut dropped = 0usize;
    for s in signals {
        // Test-title signals are the first thing evicted.
        if level >= Degrade::TrimSignals && s.kind == SignalKind::TestTitle {
            dropped += 1;
            continue;
        }
        if shown >= cap {
            dropped += 1;
            continue;
        }
        out.push_str(&format!("- {} {}\n", kind_name(s.kind), s.value));
        shown += 1;
    }
    if dropped > 0 {
        out.push_str(&format!("(-{dropped} more omitted)\n"));
    }
    out
}

fn kind_name(kind: SignalKind) -> &'static str {
    match kind {
        SignalKind::Testid => "testid",
        SignalKind::RoleName => "role",
        SignalKind::Label => "label",
        SignalKind::Placeholder => "placeholder",
        SignalKind::Text => "text",
        SignalKind::Goto => "goto",
        SignalKind::Href => "href",
        SignalKind::ApiRoute => "api",
        SignalKind::Port => "port",
        SignalKind::Url => "url",
        SignalKind::OptionLabel => "option",
        SignalKind::TestTitle => "title",
    }
}

/// Truncate to `max` CHARS (not bytes) so CJK task text stays bounded.
fn truncate_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let cut: String = s.chars().take(max).collect();
    format!("{cut}…")
}

fn truncate_lines(s: &str, max_lines: usize) -> String {
    let mut lines: Vec<&str> = s.lines().take(max_lines).collect();
    if s.lines().count() > max_lines {
        lines.push("(repo map truncated)");
    }
    lines.join("\n")
}

/// Render the append-only `<arc-workspace>` tail block. `Some` only when
/// there is something to say (drift movement or localized changes); the
/// CALLER decides whether the drift set actually changed since last time
/// (fingerprint compare) — this function never re-renders stale drift.
/// Hard cap: 6 lines.
pub fn render_tail_block(
    seq: u64,
    drift: &super::DriftReport,
    changes: &[ChangeNote],
) -> Option<String> {
    let drift_line = {
        let mut parts: Vec<String> = Vec::new();
        if !drift.newly_satisfied.is_empty() {
            let names: Vec<String> = drift
                .newly_satisfied
                .iter()
                .take(3)
                .map(|s| format!("{} {}", kind_name(s.kind), s.value))
                .collect();
            parts.push(format!("satisfied {}", names.join(", ")));
        }
        if !drift.missing.is_empty() {
            let names: Vec<String> = drift
                .missing
                .iter()
                .take(3)
                .map(|s| format!("{} {}", kind_name(s.kind), s.value))
                .collect();
            let more = drift.missing.len().saturating_sub(3);
            parts.push(format!(
                "still missing {}{}",
                names.join(", "),
                if more > 0 {
                    format!(" (+{more})")
                } else {
                    String::new()
                }
            ));
        }
        if parts.is_empty() {
            None
        } else {
            Some(parts.join("; "))
        }
    };

    if drift_line.is_none() && changes.is_empty() {
        return None;
    }

    let mut lines = vec![format!("<arc-workspace seq={seq}>")];
    if let Some(d) = drift_line {
        lines.push(format!("drift: {d}"));
    }
    for c in changes.iter().take(6 - lines.len() - 1) {
        match (&c.symbol, c.line) {
            (Some(sym), Some(line)) => lines.push(format!("changed: {}:{sym}:{line}", c.file)),
            (Some(sym), None) => lines.push(format!("changed: {}:{sym}", c.file)),
            (None, Some(line)) => lines.push(format!("changed: {}:{line}", c.file)),
            (None, None) => lines.push(format!("changed: {}", c.file)),
        }
    }
    lines.push("</arc-workspace>".to_string());
    debug_assert!(lines.len() <= 6, "tail block must stay within 6 lines");
    Some(lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::planctl::{
        PortContract, RehearsalReport, RouteProbe, StepAction, TestActionSummary,
    };

    fn base_state() -> PlanState {
        let mut env = Envelope::default();
        env.rules_text = Some("HOST STACK RULES verbatim marker".to_string());
        env.seq = 7;
        env.phase = PlanPhase::Node;
        env.task = "火车票预订演示系统".into();
        env.specs_staged = true;
        env.ports = PortContract {
            grade: 3000,
            smoke: 3001,
            extra: vec![3301],
        };
        env.steps = vec![
            PlanStep {
                id: "REQ-1".into(),
                action: StepAction::Modify,
                input: "注册旅客账号".into(),
                validation: Some(
                    "契约:导航回 `/`,首页显示用户名与 Sign out 退出链接,刷新后仍保持登录".into(),
                ),
                expected_output: "POST /api/register answers 201".into(),
                ..Default::default()
            },
            PlanStep {
                id: "REQ-2".into(),
                action: StepAction::Modify,
                input: "登录和退出账号".into(),
                ..Default::default()
            },
        ];
        env.focus = Some("REQ-1".into());
        PlanState {
            envelope: env,
            inventory: Default::default(),
            index: Default::default(),
            tail_seq: 0,
            installed_at_ms: 0,
            last_missing: Vec::new(),
        }
    }

    fn sig(kind: SignalKind, value: &str) -> AcceptanceSignal {
        AcceptanceSignal {
            kind,
            value: value.into(),
            note: None,
            spec_file: "REQ-1.1-user-registration.spec.ts".into(),
        }
    }

    /// STABLE segment pin: CONTRACT renders rules_text VERBATIM and ONLY
    /// the contract — no header seq, no focus, no report. The system row
    /// is therefore byte-identical across turns that only change focus.
    #[test]
    fn stable_segment_is_contract_only_and_byte_stable_across_focus_switches() {
        let a = base_state();
        let mut b = base_state();
        b.envelope.seq = 99;
        b.envelope.focus = Some("REQ-2".into());
        b.envelope.steps[0].status = crate::planctl::StepStatus::Done;
        let sa = render_stable_segment(&a);
        let sb = render_stable_segment(&b);
        assert_eq!(sa, sb, "stable segment must ignore ALL volatile fields");
        assert!(sa.starts_with(SENTINEL_BEGIN));
        assert!(sa.contains("HOST STACK RULES verbatim marker"));
        assert!(sa.contains("Ports: grade=3000 smoke=3001 extra=3301"));
        assert!(!sa.contains("seq="));
        // F2 pin: the staged-spec path appears EXACTLY ONCE (the pointer
        // must not double-concat spec_dir with the default).
        assert_eq!(
            sa.matches(".arc/acceptance").count(),
            1,
            "staged path doubled: {sa}"
        );
        assert!(!sa.contains("== FOCUS"));
        assert!(!sa.contains("== HOST REPORT"));
    }

    /// Requirements-only tasks: no staged specs, no pointer; the
    /// folder-level contract rides the stable segment verbatim.
    #[test]
    fn stable_segment_gates_spec_pointer_and_renders_shared_contract() {
        let mut state = base_state();
        state.envelope.specs_staged = false;
        state.envelope.shared_contract =
            Some("[-REQ-1] grid accessible name is Worksheet grid".into());
        let body = render_stable_segment(&state);
        assert!(!body.contains(".arc/acceptance"), "phantom pointer: {body}");
        assert!(body.contains("== SHARED REQUIREMENTS =="));
        assert!(body.contains("Worksheet grid"));
        // Byte-stability is unaffected by which optional sections shipped.
        let again = render_stable_segment(&state);
        assert_eq!(body, again);
    }

    #[test]
    fn turn_tail_renders_volatile_sections_in_order() {
        let mut state = base_state();
        state.inventory.by_step.insert(
            "REQ-1".into(),
            vec![sig(SignalKind::ApiRoute, "/api/register")],
        );
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(body.starts_with("<arc-plan-turn>"));
        assert!(body.trim_end().ends_with("</arc-plan-turn>"));
        let order = [
            "== PLAN ==",
            "== FOCUS REQ-1 ==",
            "== REPO MAP ==",
            "== HOST REPORT ==",
        ];
        let mut last = 0;
        for marker in order {
            let at = body
                .find(marker)
                .unwrap_or_else(|| panic!("missing {marker}"));
            assert!(at > last, "{marker} out of order");
            last = at;
        }
        assert!(body.contains("PLAN seq=7 phase=node"));
        assert!(body.contains("validation: 契约:导航回"));
    }

    #[test]
    fn pressure_tiers_shrink_the_tail_only() {
        let mut state = base_state();
        state.inventory.by_step.insert(
            "REQ-1".into(),
            vec![sig(SignalKind::Testid, "password-strength")],
        );
        let full = render_turn_tail(&state, PressureMode::Full);
        let skel = render_turn_tail(&state, PressureMode::Skeleton);
        let min = render_turn_tail(&state, PressureMode::Minimal);
        assert!(estimate_tokens(&full) <= 2600);
        assert!(estimate_tokens(&skel) <= 1200);
        assert!(estimate_tokens(&min) <= 500);
        // Minimal keeps focus + validation head, drops signals/map/report.
        assert!(min.contains("FOCUS REQ-1"));
        assert!(min.contains("validation: 契约:导航回"));
        assert!(!min.contains("password-strength"));
        assert!(!min.contains("== REPO MAP =="));
        // Stable segment unaffected by pressure.
        assert_eq!(
            render_stable_segment(&state),
            render_stable_segment(&state),
            "stable never changes"
        );
    }

    #[test]
    fn wide_signal_inventory_degrades_to_tail_budget_with_truncation_note() {
        let mut state = base_state();
        let signals: Vec<AcceptanceSignal> = (0..300)
            .map(|i| {
                sig(
                    SignalKind::Testid,
                    &format!("very-long-descriptive-test-id-number-{i:04}"),
                )
            })
            .collect();
        state.inventory.by_step.insert("REQ-1".into(), signals);
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(estimate_tokens(&body) <= 2600);
        assert!(body.contains("more omitted"));
    }

    #[test]
    fn focus_validation_contract_is_carried_verbatim_at_full() {
        let mut state = base_state();
        let long_contract = "导航回 `/`,显示用户名与退出链接,刷新保持登录。".repeat(20);
        state.envelope.steps[0].validation = Some(long_contract.clone());
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(
            body.contains(&format!("validation: {long_contract}")),
            "full tier carries the contract verbatim"
        );
        let mut squeezed = state.clone();
        squeezed.envelope.steps[0].validation = Some("很长的契约 ".repeat(200));
        let compact = render_turn_tail(&squeezed, PressureMode::Skeleton);
        assert!(compact.contains("validation: 很长的契约"));
    }

    #[test]
    fn empty_workspace_renders_not_scaffolded_marker() {
        let state = base_state();
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(body.contains("not scaffolded yet"));
    }

    #[test]
    fn host_report_renders_verdicts_probes_and_scaffold_list() {
        let mut state = base_state();
        state.envelope.host_signals = Some(crate::planctl::HostSignals {
            rehearsal: Some(RehearsalReport {
                build_ok: false,
                boot_ok: false,
                routes: vec![],
                digest: "npm ERR! missing script: build".into(),
            }),
            scaffolded_by_host: vec!["backend/package.json".into(), ".npmrc".into()],
            spec_run: None,
            validation_probes: vec![],
        });
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(body.contains("rehearsal: build FAIL boot FAIL"));
        assert!(body.contains("npm ERR! missing script"));
        assert!(body.contains("scaffolded by host"));
    }

    #[test]
    fn validation_probes_render_with_interpretation_hint() {
        let mut state = base_state();
        state.envelope.host_signals = Some(crate::planctl::HostSignals {
            rehearsal: Some(RehearsalReport {
                build_ok: true,
                boot_ok: true,
                routes: vec![],
                digest: String::new(),
            }),
            ..Default::default()
        });
        state
            .envelope
            .host_signals
            .as_mut()
            .unwrap()
            .validation_probes = vec![
            RouteProbe {
                method: "POST".into(),
                path: "/api/register {}".into(),
                status: 422,
            },
            RouteProbe {
                method: "POST".into(),
                path: "/api/register +10k".into(),
                status: 500,
            },
        ];
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(body.contains("validation probes"));
        assert!(body.contains("POST /api/register {} -> 422"));
        assert!(body.contains("POST /api/register +10k -> 500"));
        assert!(body.contains("5xx is a host-verified FAILURE"));
    }

    #[test]
    fn summaries_render_as_action_chains_for_focus_step() {
        let mut state = base_state();
        state.inventory.summaries.push(TestActionSummary {
            spec_file: "REQ-1.1-user-registration.spec.ts".into(),
            test_title: "registers a traveller".into(),
            summary: "goto /register → fill Name=qA → click Register → expect /login".into(),
            step_id: Some("REQ-1".into()),
        });
        let body = render_turn_tail(&state, PressureMode::Full);
        assert!(body.contains("actions:"));
        assert!(body.contains("goto /register"));
    }

    #[test]
    fn tail_block_is_none_without_news_and_capped_at_six_lines() {
        let empty = super::super::DriftReport::default();
        assert_eq!(render_tail_block(1, &empty, &[]), None);

        let mut drift = super::super::DriftReport::default();
        drift.newly_satisfied = vec![sig(SignalKind::Testid, "password-strength")];
        drift.missing = (0..10)
            .map(|i| sig(SignalKind::ApiRoute, &format!("/api/route-{i}")))
            .collect();
        let changes: Vec<ChangeNote> = (0..10)
            .map(|i| {
                let mut c = ChangeNote::new(format!("src/file{i}.jsx"));
                c.symbol = Some(format!("Comp{i}"));
                c
            })
            .collect();
        let block = render_tail_block(9, &drift, &changes).unwrap();
        assert!(block.starts_with("<arc-workspace seq=9>"));
        assert!(block.ends_with("</arc-workspace>"));
        assert!(block.lines().count() <= 6);
        assert!(block.contains("satisfied testid password-strength"));
        assert!(block.contains("still missing"));
        assert!(block.contains("changed: src/file0.jsx:Comp0"));
    }
}
