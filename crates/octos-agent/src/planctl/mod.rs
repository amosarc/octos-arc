//! ARC host planning-signal node ("宿主 Planning 信号节点").
//!
//! The ARC harness drives a long stdio session (requirement tree → code →
//! Playwright acceptance). Today the model plans ad hoc and re-reads the
//! acceptance specs every turn. This module flips that: the HOST compiles
//! the requirement tree, the acceptance contract, the workspace repo map,
//! and its own rehearsal reports into a structured envelope; the kernel
//! renders it as a resident L1 segment at the tail of the system row, and
//! pushes incremental workspace signals as an append-only tail block after
//! mutating tool batches.
//!
//! Two design laws govern every decision:
//!
//! 1. **宿主尽算** — whatever the host can compute or execute itself
//!    (signal extraction, workspace indexing, acceptance rehearsal, state
//!    bookkeeping, infrastructure scaffolding) is never delegated to the
//!    model.
//! 2. **缓存律** (ADR "Stable versus volatile prompt content") — the
//!    system row is byte-stable within a turn; volatile signals ride the
//!    append-only User-role tail channel. The plan segment lives at the
//!    END of the system row (stable rules first, volatile sections last)
//!    so the base+discipline prefix stays cacheable.
//!
//! Module map: [`wire`] (turn marker protocol + validation), [`render`]
//! (L1 segment rendering + budget + pressure degrade), [`state`] (disk
//! persistence), and — behind `ast` — [`scan`] (walker + ripgrep literal
//! layer + tree-sitter precise layer), [`spec`] (acceptance signal
//! inventory + action summaries), [`repomap`] (aider-style repo map).
//!
//! Segment budget tiers (tokens, [`PressureMode`]): Full 1800 / Skeleton
//! 850 / Minimal 320. Pressure thresholds sit below the 0.667-window
//! compaction line so the segment rides `messages[0]` through compaction
//! instead of triggering it.

pub mod render;
pub mod state;
pub mod wire;

#[cfg(feature = "ast")]
pub mod repomap;
#[cfg(feature = "ast")]
pub mod scan;
#[cfg(feature = "ast")]
pub mod spec;

use std::collections::{HashMap, HashSet};
use std::time::SystemTime;

use serde::{Deserialize, Serialize};

/// Named prompt-segment slot the rendered plan occupies. Kept out of the
/// composed system prompt by `compose_system_prompt` and appended back at
/// the very end (after the tool-use discipline block).
pub const SEGMENT_NAME: &str = "arc_host_plan";

/// Turn marker prefixing a plan-carrying turn. Harness-owned; the marker
/// line and the envelope JSON line are stripped from the text the model
/// sees (mirrors `testgen::turn`).
pub const MARKER: &str = "[arc-plan]";

/// Stored plan state (envelope + seq), relative to the workspace root.
pub const STATE_REL_PATH: &str = ".arc/host-plan/state.v1.json";

/// Default staged-acceptance directory, relative to the workspace root.
/// The adapter copies the task's Playwright specs here (read-only by
/// convention; the ruleset forbids the model from touching it).
pub const SPEC_DIR_REL_DEFAULT: &str = ".arc/acceptance";

/// Sentinel pair wrapping the rendered plan inside the system row. The
/// pressure refresh replaces ONLY the text between these markers and
/// leaves the rest of `messages[0]` byte-identical.
pub const SENTINEL_BEGIN: &str = "<<<ARC_HOST_PLAN:v1>>>";
pub const SENTINEL_END: &str = "<<<END ARC_HOST_PLAN>>>";

/// Rules are HOST- and TASK-supplied, never kernel-authored: the adapter
/// sends grading-stack facts via `Envelope.rules_text` and the task's
/// Validation contract per step via `PlanStep.validation` (extracted from
/// requirements.yaml in the plan phase). The kernel renders whatever it
/// receives and ships zero domain prose of its own.

/// Stable system-row segment budget (CONTRACT only — host rules_text +
/// extra_rules + port line). Byte-stable across turns so the provider
/// prefix cache covers it (and everything AFTER it) permanently.
pub const BUDGET_CONTRACT_TOKENS: u32 = 900;
/// Volatile turn-tail budgets (`<arc-plan-turn>` user-row block), by
/// pressure tier. The tail is new content on every turn — rendering it
/// costs nothing in cache terms, so FULL can be generous.
pub const TAIL_FULL_TOKENS: u32 = 2600;
pub const TAIL_SKELETON_TOKENS: u32 = 1200;
pub const TAIL_MINIMAL_TOKENS: u32 = 500;

/// Hard cap on the marker JSON line on the wire.
pub const WIRE_MAX_BYTES: usize = 256 * 1024;

/// Envelope field limits enforced by [`wire::validate_envelope`].
pub const MAX_TASK_CHARS: usize = 500;
pub const MAX_STEP_TEXT_CHARS: usize = 600;
/// Host-supplied ruleset text (grading-stack facts), ≤6000 chars.
pub const MAX_RULESET_TEXT_CHARS: usize = 6000;
/// Per-step Validation contract text (from requirements.yaml), ≤3000 chars.
pub const MAX_VALIDATION_CHARS: usize = 3000;
pub const MAX_EXTRA_RULES_CHARS: usize = 2400;
pub const MAX_STEPS: usize = 40;
pub const MAX_HOST_SIGNAL_TEXT_CHARS: usize = 600;

/// Short constant appended to a marked turn whose envelope failed to
/// parse/validate. The marker+JSON are ALWAYS stripped (bare JSON never
/// enters the model-visible history); this makes the failure visible
/// without blocking the turn.
pub const HOST_PARSE_NOTICE: &str = "[arc-plan] Host notice: the host plan payload on this \
turn failed validation and was dropped; continue on your own plan.\n";

/// Whether the plan marker protocol is enabled. `OCTOS_ARC_PLAN=0` opts
/// out (pass-through); absent or any other value means enabled.
pub fn feature_enabled() -> bool {
    feature_enabled_for(std::env::var("OCTOS_ARC_PLAN").ok().as_deref())
}

/// Pure core of [`feature_enabled`], unit-testable without env mutation.
fn feature_enabled_for(env_value: Option<&str>) -> bool {
    match env_value {
        Some(v) => v != "0",
        None => true,
    }
}

/// Full-tier pressure threshold (used/window). Env-overridable.
pub fn full_ratio() -> f64 {
    std::env::var("OCTOS_ARC_PLAN_FULL_RATIO")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0.50)
}

/// Skeleton-tier pressure threshold. Env-overridable.
pub fn skeleton_ratio() -> f64 {
    std::env::var("OCTOS_ARC_PLAN_SKELETON_RATIO")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0.62)
}

/// How much of the segment budget the current turn pressure allows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PressureMode {
    /// Normal: render everything within the full budget.
    Full,
    /// Elevated pressure: stable rules + focus skeleton only.
    Skeleton,
    /// Heavy pressure: minimal (focus id + port line + "read
    /// .arc/acceptance").
    Minimal,
}

impl PressureMode {
    /// Classify turn pressure. `full_ratio` < `skeleton_ratio` < the
    /// 0.667-window compaction trigger, so classification always happens
    /// before compaction can fire.
    pub fn classify(used: u32, window: u32, full_ratio: f64, skeleton_ratio: f64) -> Self {
        let ratio = if window == 0 {
            0.0
        } else {
            used as f64 / window as f64
        };
        if ratio >= skeleton_ratio {
            PressureMode::Minimal
        } else if ratio >= full_ratio {
            PressureMode::Skeleton
        } else {
            PressureMode::Full
        }
    }

    /// Token budget for the volatile turn-tail at this pressure tier.
    pub fn tail_budget(self) -> u32 {
        match self {
            PressureMode::Full => TAIL_FULL_TOKENS,
            PressureMode::Skeleton => TAIL_SKELETON_TOKENS,
            PressureMode::Minimal => TAIL_MINIMAL_TOKENS,
        }
    }
}

// ── Wire data model ─────────────────────────────────────────────────
//
// snake_case serde, `#[serde(default)]` everywhere, unknown fields
// ignored — the adapter may move ahead of the kernel.

/// Lifecycle phase the envelope was built for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PlanPhase {
    Skeleton,
    Node,
    Final,
    Repair,
}

/// Supported stacks. Only one this phase; the enum keeps the wire honest.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlanStack {
    WebReactExpress,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
#[derive(Default)]
pub struct PortContract {
    /// Port the grader drives (Playwright baseUrl).
    pub grade: u16,
    /// Port the host rehearses / smoke-boots on.
    pub smoke: u16,
    /// Extra ports the server must also listen on (ARC_EXTRA_PORTS
    /// contract, e.g. ticket-booking's 3301).
    pub extra: Vec<u16>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StepAction {
    Locate,
    Analyze,
    Modify,
    Test,
    Verify,
    Deliver,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
#[derive(Default)]
pub enum StepStatus {
    #[default]
    Pending,
    Active,
    Done,
    Blocked,
}

/// Files/routes/specs a step touches. The spec→step mapping keys on
/// `spec_files` basenames.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct StepTargets {
    #[serde(default)]
    pub files: Vec<String>,
    #[serde(default)]
    pub routes: Vec<String>,
    #[serde(default)]
    pub spec_files: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct PlanStep {
    /// Non-empty, unique across the envelope (validated at the wire).
    pub id: String,
    pub action: StepAction,
    /// Short summary line (plan checklist rendering).
    pub input: String,
    /// FULL Validation contract for this step (first line of the yaml
    /// node description) — the task-authored rules, rendered verbatim
    /// into the FOCUS section. Task rules live HERE, not in the kernel.
    #[serde(default)]
    pub validation: Option<String>,
    #[serde(default)]
    pub targets: StepTargets,
    pub expected_output: String,
    #[serde(default)]
    pub status: StepStatus,
}

impl Default for PlanStep {
    fn default() -> Self {
        Self {
            id: String::new(),
            action: StepAction::Modify,
            input: String::new(),
            validation: None,
            targets: StepTargets::default(),
            expected_output: String::new(),
            status: StepStatus::Pending,
        }
    }
}

impl PlanStep {
    /// Wire name of the step action (checklist rendering).
    pub fn action_name(&self) -> &'static str {
        match self.action {
            StepAction::Locate => "locate",
            StepAction::Analyze => "analyze",
            StepAction::Modify => "modify",
            StepAction::Test => "test",
            StepAction::Verify => "verify",
            StepAction::Deliver => "deliver",
        }
    }
}

/// Host-computed verification results fed back into the next envelope.
/// Never hard-validated at the wire (the host is the authority; a
/// malformed report degrades rendering, not the turn).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct HostSignals {
    #[serde(default)]
    pub rehearsal: Option<RehearsalReport>,
    /// Infrastructure files the host scaffolded directly (E-flip).
    #[serde(default)]
    pub scaffolded_by_host: Vec<String>,
    #[serde(default)]
    pub spec_run: Option<SpecRun>,
    /// Generic input-validation probes (empty / oversized payloads on
    /// POST routes): factual status rows; a 5xx is a host-verified
    /// failure, lower codes are facts for the model to check against the
    /// contract (a 2xx on garbage means no server-side re-validation).
    #[serde(default)]
    pub validation_probes: Vec<RouteProbe>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct RehearsalReport {
    pub build_ok: bool,
    pub boot_ok: bool,
    #[serde(default)]
    pub routes: Vec<RouteProbe>,
    /// Truncated human-readable error digest (red rehearsals only).
    #[serde(default)]
    pub digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct RouteProbe {
    pub method: String,
    pub path: String,
    pub status: u16,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct SpecRun {
    pub passed: u32,
    pub failed: u32,
    #[serde(default)]
    pub digest: String,
}

/// The plan envelope the adapter sends on the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Envelope {
    /// Wire version, must be 1.
    pub v: u8,
    /// Monotonic within a run; lets the model order stale vs fresh tails.
    pub seq: u64,
    pub phase: PlanPhase,
    pub stack: PlanStack,
    /// Task title (≤[`MAX_TASK_CHARS`]).
    pub task: String,
    /// Staged spec dir relative to the workspace root.
    pub spec_dir: String,
    /// Free-form ruleset label (observability only; not validated
    /// against a kernel-side list — the kernel ships no rulesets).
    pub rules: Option<String>,
    /// Host-supplied ruleset text (grading-stack facts). Rendered as the
    /// CONTRACT block verbatim; the kernel adds nothing to it.
    #[serde(default)]
    pub rules_text: Option<String>,
    /// Task-specific extra rules (≤[`MAX_EXTRA_RULES_CHARS`]).
    pub extra_rules: Option<String>,
    pub ports: PortContract,
    pub steps: Vec<PlanStep>,
    /// Current focus step id (must match a step; `None` on final).
    pub focus: Option<String>,
    #[serde(default)]
    pub host_signals: Option<HostSignals>,
}

impl Default for Envelope {
    fn default() -> Self {
        Self {
            v: 1,
            seq: 0,
            phase: PlanPhase::Node,
            stack: PlanStack::WebReactExpress,
            task: String::new(),
            spec_dir: SPEC_DIR_REL_DEFAULT.to_string(),
            rules: None,
            rules_text: None,
            extra_rules: None,
            ports: PortContract::default(),
            steps: Vec::new(),
            focus: None,
            host_signals: None,
        }
    }
}

// ── Scan data model (plain holders; ast-gated modules fill them) ────

/// Signal kinds the spec extractor recognizes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SignalKind {
    Testid,
    RoleName,
    Label,
    Placeholder,
    Text,
    Goto,
    Href,
    ApiRoute,
    Port,
    Url,
    OptionLabel,
    TestTitle,
}

/// One acceptance signal harvested from a spec file (D-flip: the model
/// never needs to read the spec to learn these).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcceptanceSignal {
    pub kind: SignalKind,
    /// Literal value (for regex `name:` patterns, the pattern source).
    pub value: String,
    /// Extra context (e.g. enclosing helper function name).
    pub note: Option<String>,
    /// Spec file the signal first appeared in (workspace-relative).
    pub spec_file: String,
}

/// Per-test compressed action chain (goto→fill→check→click→expect…).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TestActionSummary {
    pub spec_file: String,
    pub test_title: String,
    /// ≤120 chars single-line summary.
    pub summary: String,
    /// Step the owning spec file maps to, when known.
    pub step_id: Option<String>,
}

/// Spec-dir inventory (plain holder; `spec::scan_spec_dir` fills it).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct SpecInventory {
    pub files: Vec<String>,
    /// Distinct localhost ports seen in the specs (beyond envelope ports).
    pub ports: Vec<u16>,
    /// Signals not attributable to a step: unassigned files + support
    /// helpers (note = function name).
    pub globals: Vec<AcceptanceSignal>,
    /// step_id → signals harvested from that step's mapped spec files.
    pub by_step: HashMap<String, Vec<AcceptanceSignal>>,
    pub summaries: Vec<TestActionSummary>,
    /// Files that failed to parse — skipped, never bail.
    pub scan_errors: Vec<String>,
    /// True when the per-class/global caps dropped signals.
    pub truncated: bool,
}

/// aider-style repo-map tag: a definition or reference site.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tag {
    pub name: String,
    /// 1-based line of the definition/usage.
    pub line: usize,
    pub kind: TagKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TagKind {
    Def,
    Ref,
}

/// Workspace index (plain holder; `scan` + `repomap` fill it).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct WorkspaceIndex {
    /// rel path → (mtime at scan, tags). mtime drives invalidation.
    pub tags_by_file: HashMap<String, (SystemTime, Vec<Tag>)>,
    /// File → PageRank, descending.
    pub rank: Vec<(String, f64)>,
    /// Domain overlays (routes/testids/pages) rendered ahead of symbols.
    pub overlays: DomainOverlays,
    /// Rendered repo-map text within budget (`repomap::render_map`).
    pub repo_map_text: Option<String>,
}

/// Domain overlay rows lifted above the symbol lines in the repo map.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct DomainOverlays {
    pub routes: Vec<String>,
    pub testids: Vec<String>,
    pub pages: Vec<String>,
}

/// Literal-level drift between required spec signals and workspace hits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct DriftReport {
    /// Required by specs but not yet found in the workspace.
    pub missing: Vec<AcceptanceSignal>,
    /// Missing at the previous check, satisfied by the latest batch.
    pub newly_satisfied: Vec<AcceptanceSignal>,
}

impl DriftReport {
    /// The drift SET identity — a tail block is emitted only when this
    /// changes between checks.
    pub fn fingerprint(&self) -> String {
        let mut parts: Vec<String> = self
            .missing
            .iter()
            .map(|s| format!("{}:{}", s.kind as u8, s.value))
            .collect();
        parts.sort();
        parts.join("|")
    }
}

/// Kernel-side plan state. The disk store persists only
/// `{envelope, tail_seq}`; inventory/index are rebuilt by rescanning.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanState {
    pub envelope: Envelope,
    pub inventory: SpecInventory,
    pub index: WorkspaceIndex,
    /// Last seq used for a `<arc-workspace>` tail block.
    pub tail_seq: u64,
    pub installed_at_ms: u64,
    /// Signals that were missing at the last drift check (drift
    /// memory for `newly_satisfied`; in-memory only, never persisted).
    pub last_missing: Vec<AcceptanceSignal>,
}

/// All acceptance signals the specs require (globals + per-step).
pub fn required_signals(inventory: &SpecInventory) -> Vec<AcceptanceSignal> {
    let mut all = inventory.globals.clone();
    for signals in inventory.by_step.values() {
        all.extend(signals.iter().cloned());
    }
    all
}

/// Build a full plan state from an envelope + workspace: spec-dir scan →
/// step assignment → L0 walk → L1 harvest + drift → tags/edges/pagerank →
/// rendered repo map + domain overlays. Deterministic. (ast builds; the
/// no-ast fallback below skips the extraction layers.)
#[cfg(feature = "ast")]
pub fn build_plan_state(
    envelope: Envelope,
    workspace: &std::path::Path,
    prev_tail_seq: u64,
) -> PlanState {
    // Specs: envelope.spec_dir resolved inside the workspace, falling back
    // to the default staged dir.
    let spec_dir = workspace.join(&envelope.spec_dir);
    let spec_dir = if spec_dir.is_dir() {
        spec_dir
    } else {
        workspace.join(SPEC_DIR_REL_DEFAULT)
    };
    let mut inventory = spec::scan_spec_dir(&spec_dir);
    spec::assign_to_steps(&mut inventory, &envelope.steps);

    // Workspace: walk + literal harvest.
    let files = scan::walk_workspace(workspace);
    let hits = scan::harvest_literals(&files);

    // spec-hit files: workspace files whose literals satisfy a required
    // signal (repomap personalization + edge boost).
    let required = required_signals(&inventory);
    let mut spec_hit_files: HashSet<String> = HashSet::new();
    for hit in &hits {
        if required
            .iter()
            .any(|s| s.kind == hit.kind && s.value == hit.value)
        {
            spec_hit_files.insert(hit.file.clone());
        }
    }

    // Tags per file (failed parses are honestly absent).
    let mut tags_by_file: HashMap<String, (SystemTime, Vec<Tag>)> = HashMap::new();
    let mut tags_map: HashMap<String, Vec<Tag>> = HashMap::new();
    for f in &files {
        let Ok(source) = std::fs::read_to_string(&f.path) else {
            continue;
        };
        if let Some(tags) = repomap::extract_file_tags(&source, &f.rel) {
            let mtime = std::fs::metadata(&f.path)
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            tags_by_file.insert(f.rel.clone(), (mtime, tags.clone()));
            tags_map.insert(f.rel.clone(), tags);
        }
    }

    let edges = repomap::build_edges(&tags_map, &spec_hit_files);
    let nodes: Vec<String> = {
        let mut v: Vec<String> = tags_map.keys().cloned().collect();
        v.sort();
        v
    };
    // Teleport: spec-hit files + recently modified files carry the mass.
    let mut teleport: HashMap<String, f64> = HashMap::new();
    for f in &spec_hit_files {
        teleport.insert(f.clone(), 1.0);
    }
    for f in &files {
        if let Ok(meta) = std::fs::metadata(&f.path) {
            if let Ok(modified) = meta.modified() {
                if let Ok(age) = SystemTime::now().duration_since(modified) {
                    // Modified within the last hour → teleport weight.
                    if age < std::time::Duration::from_secs(3600) {
                        *teleport.entry(f.rel.clone()).or_insert(0.0) += 0.5;
                    }
                }
            }
        }
    }
    let ranks = repomap::pagerank(&nodes, &edges, &teleport, 0.85, 40);
    let rank_by_file: HashMap<String, f64> = nodes.iter().cloned().zip(ranks).collect();

    // Domain overlays from literal hits (scoring signals ride first).
    let mut routes = std::collections::BTreeSet::new();
    let mut testids = std::collections::BTreeSet::new();
    let mut pages = std::collections::BTreeSet::new();
    for h in &hits {
        match h.kind {
            SignalKind::ApiRoute => {
                routes.insert(h.value.clone());
            }
            SignalKind::Testid => {
                testids.insert(h.value.clone());
            }
            SignalKind::Href => {
                pages.insert(h.value.clone());
            }
            _ => {}
        }
    }
    let overlays = DomainOverlays {
        routes: routes.into_iter().collect(),
        testids: testids.into_iter().collect(),
        pages: pages.into_iter().collect(),
    };

    let source_lines: HashMap<String, Vec<String>> = files
        .iter()
        .filter_map(|f| {
            std::fs::read_to_string(&f.path)
                .ok()
                .map(|s| (f.rel.clone(), s.lines().map(String::from).collect()))
        })
        .collect();
    // Recently edited files lead the map: the model must see what it is
    // actively changing even when spec-hit host files outrank everything
    // (tok-ticket-1: the map never showed the frontend being edited).
    let mut recent_files: Vec<(std::time::Duration, String)> = tags_by_file
        .iter()
        .filter_map(|(rel, (mtime, tags))| {
            if tags.is_empty() {
                return None;
            }
            SystemTime::now()
                .duration_since(*mtime)
                .ok()
                .filter(|age| age.as_secs() < 30 * 60)
                .map(|age| (age, rel.clone()))
        })
        .collect::<Vec<_>>();
    recent_files.sort_by(|a, b| a.0.cmp(&b.0));
    let recent_files: Vec<String> = recent_files.into_iter().map(|(_, rel)| rel).collect();

    let repo_map_text = repomap::render_map(&repomap::MapInput {
        tags_by_file: &tags_map,
        rank_by_file: &rank_by_file,
        overlays: &overlays,
        source_lines: &source_lines,
        recent_files: &recent_files,
        // Doubled from 450: the orientation win of showing the actively
        // edited files outweighs the fixed cost, and rows stay under
        // MAX_MAP_LINES.
        budget_tokens: 800,
    });

    let drift_now = scan::drift(&required, &hits);
    PlanState {
        envelope,
        inventory,
        index: WorkspaceIndex {
            tags_by_file,
            rank: nodes
                .into_iter()
                .zip(rank_by_file.values().copied())
                .collect(),
            overlays,
            repo_map_text: Some(repo_map_text),
        },
        tail_seq: prev_tail_seq,
        installed_at_ms: SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0),
        last_missing: drift_now.missing,
    }
}

/// No-ast fallback: envelope-only state (wire/render/state still work).
#[cfg(not(feature = "ast"))]
pub fn build_plan_state(
    envelope: Envelope,
    _workspace: &std::path::Path,
    prev_tail_seq: u64,
) -> PlanState {
    PlanState {
        envelope,
        inventory: SpecInventory::default(),
        index: WorkspaceIndex::default(),
        tail_seq: prev_tail_seq,
        installed_at_ms: SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0),
        last_missing: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_orders_tiers_below_compaction_line() {
        // 0.667 window is the compaction trigger; both thresholds sit
        // below it at the defaults.
        assert!(skeleton_ratio() < 2.0 / 3.0);
        assert!(full_ratio() < skeleton_ratio());
        assert_eq!(
            PressureMode::classify(49, 100, 0.50, 0.62),
            PressureMode::Full
        );
        assert_eq!(
            PressureMode::classify(50, 100, 0.50, 0.62),
            PressureMode::Skeleton
        );
        assert_eq!(
            PressureMode::classify(62, 100, 0.50, 0.62),
            PressureMode::Minimal
        );
        assert_eq!(
            PressureMode::classify(0, 0, 0.50, 0.62),
            PressureMode::Full,
            "zero window must not divide by zero"
        );
    }

    #[test]
    fn budgets_match_plan_tiers() {
        assert_eq!(PressureMode::Full.tail_budget(), 2600);
        assert_eq!(PressureMode::Skeleton.tail_budget(), 1200);
        assert_eq!(PressureMode::Minimal.tail_budget(), 500);
    }

    #[test]
    fn feature_gate_defaults_on_and_zero_opts_out() {
        assert!(feature_enabled_for(None));
        assert!(feature_enabled_for(Some("1")));
        assert!(!feature_enabled_for(Some("0")));
    }

    #[test]
    fn envelope_serde_round_trip_is_snake_case_and_forward_compatible() {
        let mut env = Envelope::default();
        env.seq = 7;
        env.phase = PlanPhase::Node;
        env.focus = Some("REQ-1".into());
        env.steps = vec![PlanStep {
            id: "REQ-1".into(),
            action: StepAction::Modify,
            input: "register endpoint".into(),
            validation: Some("register contract".into()),
            targets: StepTargets {
                files: vec!["backend/src/app.js".into()],
                routes: vec!["/api/register".into()],
                spec_files: vec!["REQ-1.1-user-registration.spec.ts".into()],
            },
            expected_output: "route answers 200".into(),
            status: StepStatus::Pending,
        }];
        env.host_signals = Some(HostSignals {
            rehearsal: Some(RehearsalReport {
                build_ok: true,
                boot_ok: true,
                routes: vec![RouteProbe {
                    method: "GET".into(),
                    path: "/api/health".into(),
                    status: 200,
                }],
                digest: String::new(),
            }),
            scaffolded_by_host: vec!["backend/package.json".into()],
            spec_run: None,
            validation_probes: Vec::new(),
        });

        let json = serde_json::to_string(&env).unwrap();
        assert!(json.contains("\"web_react_express\""));
        assert!(json.contains("\"spec_files\""));
        // Round trip.
        let back: Envelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back, env);

        // Unknown fields ignored; missing optionals default.
        let forward: Envelope =
            serde_json::from_str(r#"{"v":1,"seq":3,"future_field":{"x":1}}"#).unwrap();
        assert_eq!(forward.v, 1);
        assert_eq!(forward.seq, 3);
        assert_eq!(forward.host_signals, None);
        assert_eq!(forward.rules_text, None);
    }

    #[test]
    fn drift_fingerprint_is_order_insensitive() {
        let a = DriftReport {
            missing: vec![
                signal(SignalKind::Testid, "password-strength"),
                signal(SignalKind::ApiRoute, "/api/register"),
            ],
            newly_satisfied: vec![],
        };
        let b = DriftReport {
            missing: vec![
                signal(SignalKind::ApiRoute, "/api/register"),
                signal(SignalKind::Testid, "password-strength"),
            ],
            newly_satisfied: vec![],
        };
        assert_eq!(a.fingerprint(), b.fingerprint());
    }

    fn signal(kind: SignalKind, value: &str) -> AcceptanceSignal {
        AcceptanceSignal {
            kind,
            value: value.into(),
            note: None,
            spec_file: "REQ-1.spec.ts".into(),
        }
    }
}
