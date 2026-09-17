//! planctl end-to-end: the full turn pipeline against a scripted LLM —
//! marker apply → system-row segment → mutating-batch tail block →
//! pressure refresh → disk rehydration. Mirrors the session_summary
//! ScriptedLlm pattern; assertions follow the plan §10-P5 matrix.

use std::sync::{Arc, Mutex};

use octos_core::ToolCall;
use octos_core::{AgentId, Message};
use octos_llm::{ChatResponse, LlmProvider, StopReason, ToolSpec};
use octos_memory::EpisodeStore;

use octos_agent::Agent;
use octos_agent::planctl::{
    Envelope, HostSignals, MARKER, PlanPhase, PlanStack, PlanStep, PortContract, RehearsalReport,
    RouteProbe, SENTINEL_BEGIN, StepAction, StepStatus, StepTargets,
};

/// Records every prompt it receives; returns queued responses.
struct CaptureLlm {
    seen: Mutex<Vec<Vec<Message>>>,
    queue: Mutex<Vec<ChatResponse>>,
}

impl CaptureLlm {
    fn new(queue: Vec<ChatResponse>) -> Arc<Self> {
        Arc::new(Self {
            seen: Mutex::new(Vec::new()),
            queue: Mutex::new(queue),
        })
    }

    fn prompts(&self) -> Vec<Vec<Message>> {
        self.seen.lock().unwrap().clone()
    }
}

#[async_trait::async_trait]
impl LlmProvider for CaptureLlm {
    async fn chat(
        &self,
        messages: &[Message],
        _tools: &[ToolSpec],
        _config: &octos_llm::ChatConfig,
    ) -> eyre::Result<ChatResponse> {
        self.seen.lock().unwrap().push(messages.to_vec());
        let mut q = self.queue.lock().unwrap();
        if q.is_empty() {
            return Err(eyre::eyre!("queue exhausted"));
        }
        Ok(q.remove(0))
    }
    fn model_id(&self) -> &str {
        "mock"
    }
    fn provider_name(&self) -> &str {
        "mock"
    }
}

fn end_turn(content: &str) -> ChatResponse {
    ChatResponse {
        content: Some(content.to_string()),
        reasoning_content: None,
        tool_calls: vec![],
        stop_reason: StopReason::EndTurn,
        usage: Default::default(),
        provider_index: None,
    }
}

fn write_file_call(id: &str, path: &str, contents: &str) -> ChatResponse {
    ChatResponse {
        content: None,
        reasoning_content: None,
        tool_calls: vec![ToolCall {
            id: id.to_string(),
            name: "write_file".to_string(),
            arguments: serde_json::json!({ "path": path, "content": contents }),
            metadata: None,
        }],
        stop_reason: StopReason::ToolUse,
        usage: Default::default(),
        provider_index: None,
    }
}

fn envelope(focus: Option<&str>) -> Envelope {
    let mut env = Envelope::default();
    env.v = 1;
    env.seq = 1;
    env.phase = PlanPhase::Node;
    env.stack = PlanStack::WebReactExpress;
    env.task = "demo app".into();
    env.spec_dir = ".arc/acceptance".into();
    env.rules = Some("arc_web_v1".into());
    env.rules_text = Some(
        "HOST STACK RULES (test): backend/src/app.js exports the app; \
         serve frontend/dist; scrypt; npmmirror; .arc/acceptance READ-ONLY."
            .to_string(),
    );
    env.ports = PortContract {
        grade: 3000,
        smoke: 3001,
        extra: vec![3301],
    };
    env.steps = vec![
        PlanStep {
            id: "REQ-1".into(),
            action: StepAction::Modify,
            input: "counter page".into(),
            expected_output: "counter works".into(),
            validation: Some(
                "Validation 契约(test):导航回 `/`,首页显示用户名与 Sign out 退出链接,刷新后仍保持登录。"
                    .to_string(),
            ),
            status: StepStatus::Active,
            targets: StepTargets::default(),
        },
        PlanStep {
            id: "REQ-2".into(),
            action: StepAction::Modify,
            input: "reset button".into(),
            expected_output: "reset works".into(),
            validation: None,
            status: StepStatus::Pending,
            targets: StepTargets::default(),
        },
    ];
    env.focus = focus.map(String::from);
    env
}

fn marked(env: &Envelope, prompt: &str) -> String {
    format!(
        "{MARKER}\n{}\n\n{prompt}",
        serde_json::to_string(env).unwrap()
    )
}

async fn agent_in(dir: &std::path::Path, llm: Arc<dyn LlmProvider>, mem: &str) -> Agent {
    let memory = Arc::new(EpisodeStore::open(dir.join(mem)).await.unwrap());
    let mut tools = octos_agent::tools::ToolRegistry::with_builtins(dir);
    tools.set_workspace_root(dir.to_path_buf());
    Agent::new(AgentId::new("planctl-e2e"), llm, tools, memory).with_config(
        octos_agent::AgentConfig {
            save_episodes: false,
            ..Default::default()
        },
    )
}

/// Staged spec dir so the inventory carries at least one literal signal
/// (`data-testid="count"`), exercising drift on the write_file batch.
fn stage_specs(dir: &std::path::Path) {
    let spec_dir = dir.join(".arc/acceptance");
    std::fs::create_dir_all(&spec_dir).unwrap();
    std::fs::write(
        spec_dir.join("REQ-1.spec.ts"),
        "import { test, expect } from '@playwright/test';\n\
         test('REQ-1: counter', async ({ page }) => {\n\
           await page.goto('/');\n\
           await expect(page.getByTestId('count')).toBeVisible();\n\
         });\n",
    )
    .unwrap();
}

// (a) marker/JSON never reach the user row; the ruleset block rides the
// system row between the sentinels.
#[tokio::test]
async fn marker_payload_stays_off_the_user_row_and_ruleset_rides_the_system_row() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    let llm = CaptureLlm::new(vec![end_turn("done")]);
    let agent = agent_in(dir.path(), llm.clone(), "memory-a").await;
    let env = envelope(Some("REQ-1"));
    let result = agent
        .process_message(&marked(&env, "Implement REQ-1."), &[], vec![])
        .await
        .unwrap();
    assert_eq!(result.content, "done");

    let prompts = llm.prompts();
    assert_eq!(prompts.len(), 1);
    let messages = &prompts[0];
    let system = &messages[0];
    assert_eq!(system.role, octos_core::MessageRole::System);
    assert!(system.content.contains(SENTINEL_BEGIN), "sentinel missing");
    // System row = STABLE CONTRACT only (host rules_text); NO volatile
    // sections (no seq, no focus, no report).
    assert!(system.content.contains("HOST STACK RULES (test)"));
    assert!(system.content.contains("scrypt"));
    assert!(!system.content.contains("PLAN seq="));
    assert!(!system.content.contains("== FOCUS"));
    assert!(!system.content.contains("== HOST REPORT"));
    // User row = volatile turn-tail (focus + validation contract) above
    // the stripped prompt; no marker, no envelope JSON anywhere.
    let user = messages
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .expect("user row");
    assert!(
        user.content.contains("<arc-plan-turn>"),
        "turn tail must ride the user row: {}",
        &user.content[..200.min(user.content.len())]
    );
    assert!(user.content.contains("PLAN seq=1 phase=node"));
    assert!(user.content.contains("== FOCUS REQ-1 =="));
    assert!(
        user.content
            .contains("validation: Validation 契约(test):导航回 `/`"),
        "FOCUS must carry the validation contract"
    );
    assert!(user.content.ends_with("Implement REQ-1."));
    assert!(!user.content.contains(MARKER));
    assert!(!user.content.contains("\"seq\":"));
}

// (b) focus switch: the prefix above FOCUS is byte-identical across turns.
#[tokio::test]
async fn focus_switch_leaves_the_prefix_above_focus_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    let llm = CaptureLlm::new(vec![end_turn("one"), end_turn("two")]);
    let agent = agent_in(dir.path(), llm.clone(), "memory-a").await;
    let a = envelope(Some("REQ-1"));
    let mut b = envelope(Some("REQ-2"));
    b.seq = 2;
    agent
        .process_message(&marked(&a, "Do REQ-1."), &[], vec![])
        .await
        .unwrap();
    agent
        .process_message(&marked(&b, "Do REQ-2."), &[], vec![])
        .await
        .unwrap();

    let prompts = llm.prompts();
    let sys1 = &prompts[0][0].content;
    let sys2 = &prompts[1][0].content;
    // Cache-law headline: the ENTIRE system row is byte-identical across
    // focus switches — the volatile tail moved to the user row.
    assert_eq!(
        sys1, sys2,
        "system row must be byte-identical across focus switches"
    );
    // The seq/focus volatility lives in the user-row tail instead.
    let users1 = &prompts[0]
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .unwrap()
        .content;
    let users2 = &prompts[1]
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .unwrap()
        .content;
    assert!(users1.contains("PLAN seq=1"));
    assert!(users2.contains("PLAN seq=2"));
    assert!(users1.contains("== FOCUS REQ-1 =="));
    assert!(users2.contains("== FOCUS REQ-2 =="));
}

// (c) a successful write_file batch appends an <arc-workspace> tail block
// to the batch's Tool message while messages[0] stays byte-identical.
#[tokio::test]
async fn mutating_batch_appends_tail_block_without_touching_the_system_row() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    let llm = CaptureLlm::new(vec![
        write_file_call(
            "call_w1",
            "src/app.jsx",
            "export function App() { return <div data-testid=\"count\">0</div>; }\nfunction helper() {}\n",
        ),
        end_turn("written"),
    ]);
    let agent = agent_in(dir.path(), llm.clone(), "memory-a").await;
    let env = envelope(Some("REQ-1"));
    agent
        .process_message(&marked(&env, "Write the counter."), &[], vec![])
        .await
        .unwrap();

    let prompts = llm.prompts();
    assert_eq!(prompts.len(), 2, "one call before + one after the batch");
    let system_before = prompts[0][0].content.clone();
    let system_after = prompts[1][0].content.clone();
    assert_eq!(
        system_before, system_after,
        "cache law: the system row must not change mid-turn"
    );

    let tool_msgs: Vec<&Message> = prompts[1]
        .iter()
        .filter(|m| m.role == octos_core::MessageRole::Tool)
        .collect();
    assert_eq!(tool_msgs.len(), 1);
    assert!(
        tool_msgs[0].content.contains("<arc-workspace seq="),
        "tail block missing from the tool message: {}",
        tool_msgs[0].content
    );
    assert!(tool_msgs[0].content.contains("</arc-workspace>"));
    // The written file landed.
    assert!(dir.path().join("src/app.jsx").exists());
}

// (d) pressure degrade happens ONCE at tail composition: a heavy
// history shrinks the user-row tail (Minimal) while the system row —
// which is what the prefix cache keys on — is never rewritten.
#[tokio::test]
async fn pressure_degrades_the_turn_tail_without_touching_the_system_row() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    let llm = CaptureLlm::new(vec![end_turn("done")]);
    let agent = agent_in(dir.path(), llm, "memory-a").await;
    let env = envelope(Some("REQ-1"));
    agent
        .process_message(&marked(&env, "Implement."), &[], vec![])
        .await
        .unwrap();

    // Light history → Full tail with FOCUS; heavy history → Minimal
    // tail; the stable system row is identical in both cases.
    let light = agent.arc_plan_turn_tail(&[], "Implement.");
    let heavy_history: Vec<octos_core::Message> = (0..400)
        .map(|_| octos_core::Message::user("x".repeat(800)))
        .collect();
    let heavy = agent.arc_plan_turn_tail(&heavy_history, "Implement.");

    assert!(light.contains("== FOCUS REQ-1 =="));
    assert!(light.contains("<arc-plan-turn>"));
    assert!(
        octos_llm::context::estimate_tokens(&light)
            <= octos_agent::planctl::PressureMode::Full.tail_budget()
    );
    assert_eq!(
        octos_agent::planctl::PressureMode::Minimal,
        octos_agent::planctl::PressureMode::classify(
            999_999,
            10_000,
            octos_agent::planctl::full_ratio(),
            octos_agent::planctl::skeleton_ratio()
        )
    );
    assert!(
        !heavy.contains("== REPO MAP =="),
        "minimal tail drops the map"
    );
    assert!(heavy.contains("FOCUS REQ-1"), "minimal keeps the focus id");
    assert!(
        octos_llm::context::estimate_tokens(&heavy)
            <= octos_agent::planctl::PressureMode::Minimal.tail_budget() + 8,
        "minimal tail within budget: {}",
        octos_llm::context::estimate_tokens(&heavy)
    );
    let sys = agent.arc_plan_segment_body().unwrap();
    assert!(sys.contains(SENTINEL_BEGIN));
    assert!(!sys.contains("PLAN seq="));
}

// (e) a marker-less turn on a fresh agent rehydrates the plan from disk.
#[tokio::test]
async fn marker_less_turn_rehydrates_the_plan_from_disk() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    // Turn 1: apply via marker (writes .arc/host-plan/state.v1.json).
    let llm1 = CaptureLlm::new(vec![end_turn("one")]);
    let agent1 = agent_in(dir.path(), llm1, "memory-1").await;
    let env = envelope(Some("REQ-1"));
    agent1
        .process_message(&marked(&env, "Start."), &[], vec![])
        .await
        .unwrap();
    assert!(dir.path().join(".arc/host-plan/state.v1.json").exists());

    // Turn 2 on a BRAND-NEW agent (per-turn rebuild), no marker.
    let llm2 = CaptureLlm::new(vec![end_turn("two")]);
    let agent2 = agent_in(dir.path(), llm2.clone(), "memory-2").await;
    agent2
        .process_message("Continue the work.", &[], vec![])
        .await
        .unwrap();
    let prompts = llm2.prompts();
    let system = &prompts[0][0].content;
    assert!(
        system.contains(SENTINEL_BEGIN),
        "rehydrated segment present"
    );
    assert!(system.contains("HOST STACK RULES (test)"));
    assert!(!system.contains("== FOCUS"));
    // The tail rides the user row after rehydration.
    let user = prompts[0]
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .unwrap();
    assert!(user.content.contains("== FOCUS REQ-1 =="));
}

// (f) no marker + no stored state → the system row is byte-identical to
// snapshot + discipline (planctl invisible to plan-less sessions).
#[tokio::test]
async fn plan_less_session_is_byte_identical_to_legacy() {
    let dir = tempfile::tempdir().unwrap();
    let llm = CaptureLlm::new(vec![end_turn("ok")]);
    let agent = agent_in(dir.path(), llm.clone(), "memory-a").await;
    agent
        .process_message("Just chat.", &[], vec![])
        .await
        .unwrap();
    let prompts = llm.prompts();
    let system = &prompts[0][0].content;
    assert!(!system.contains(SENTINEL_BEGIN));
    assert_eq!(
        system,
        &format!(
            "{}{}",
            agent.system_prompt_snapshot(),
            "\n\n## Tool use discipline\n\nYou have generic file-inspection tools — `read_file`, `list_dir`, `view_image`, `grep`, `glob` — that can answer most questions about the workspace state by reading the files directly. Use them aggressively to investigate what's actually there.\n\nWhen a tool result does not answer the user's question, do NOT re-call the same tool with the same arguments — the result will be identical. Pick a different tool that can answer the specific question, usually a file-reading tool. If no tool can answer it, respond with text explaining what's missing."
        )
    );
}

// (g) before any workspace file exists the REPO MAP section says so; after
// a file lands (via apply on a rescanned turn) the map renders.
#[tokio::test]
async fn repo_map_reports_not_scaffolded_then_real_files() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    let llm = CaptureLlm::new(vec![end_turn("one")]);
    let agent = agent_in(dir.path(), llm.clone(), "memory-a").await;
    let env = envelope(Some("REQ-1"));
    agent
        .process_message(&marked(&env, "Start."), &[], vec![])
        .await
        .unwrap();
    let prompts = llm.prompts();
    let first_user = &prompts[0]
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .unwrap()
        .content;
    assert!(first_user.contains("not scaffolded yet"));

    // Model writes a file; the NEXT turn's re-scan sees it.
    std::fs::create_dir_all(dir.path().join("src")).unwrap();
    std::fs::write(
        dir.path().join("src/app.jsx"),
        "export function App() { return <div data-testid=\"count\">0</div>; }\n",
    )
    .unwrap();
    let llm2 = CaptureLlm::new(vec![end_turn("two")]);
    let agent2 = agent_in(dir.path(), llm2.clone(), "memory-2").await;
    agent2
        .process_message("Continue.", &[], vec![])
        .await
        .unwrap();
    let prompts2 = llm2.prompts();
    let user2 = &prompts2[0]
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .unwrap()
        .content;
    assert!(user2.contains("src/app.jsx"), "map lists the new file");
}

// (h) host_signals render into the HOST REPORT section.
#[tokio::test]
async fn host_signals_render_into_the_host_report() {
    let dir = tempfile::tempdir().unwrap();
    stage_specs(dir.path());
    let llm = CaptureLlm::new(vec![end_turn("done")]);
    let agent = agent_in(dir.path(), llm.clone(), "memory-a").await;
    let mut env = envelope(Some("REQ-1"));
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
        scaffolded_by_host: vec!["backend/package.json".into(), ".npmrc".into()],
        spec_run: None,
        validation_probes: vec![RouteProbe {
            method: "POST".into(),
            path: "/api/register {}".into(),
            status: 422,
        }],
    });
    agent
        .process_message(&marked(&env, "Proceed."), &[], vec![])
        .await
        .unwrap();
    let prompts = llm.prompts();
    let user = &prompts[0]
        .iter()
        .find(|m| m.role == octos_core::MessageRole::User)
        .unwrap()
        .content;
    assert!(user.contains("== HOST REPORT =="));
    assert!(user.contains("rehearsal: build OK boot OK"));
    assert!(user.contains("GET /api/health 200"));
    assert!(user.contains("validation probes"));
    assert!(user.contains("POST /api/register {} -> 422"));
    assert!(user.contains("scaffolded by host"));
    assert!(user.contains("backend/package.json"));
    // System row stays free of volatile sections.
    assert!(!prompts[0][0].content.contains("== HOST REPORT"));
}
