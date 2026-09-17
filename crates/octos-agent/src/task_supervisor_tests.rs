use super::*;

/// #1723: `record_final_output` fires `on_change` so the roster mirror
/// (`upsert_background_task_agent` → `set_agent_output_if_empty`) re-runs
/// with `final_output` present. It is called AFTER `mark_completed`, so the
/// terminal on_change already fired while `final_output` was `None`; without
/// this second notification the agent view / `/ps` detail stays empty.
#[test]
fn record_final_output_fires_on_change_with_the_output() {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    let supervisor = TaskSupervisor::new();
    let id = supervisor.register_with_input("spawn", "call-final-output", None, None);
    supervisor.mark_completed(&id, vec![]);

    // Only observe changes AFTER completion, so we isolate the
    // record_final_output notification.
    let seen_output = Arc::new(std::sync::Mutex::new(Option::<String>::None));
    let calls = Arc::new(AtomicUsize::new(0));
    let seen_c = seen_output.clone();
    let calls_c = calls.clone();
    supervisor.set_on_change(move |task| {
        calls_c.fetch_add(1, Ordering::SeqCst);
        if let Some(output) = task.final_output.clone() {
            *seen_c.lock().unwrap() = Some(output);
        }
    });

    supervisor.record_final_output(&id, "the child's full result");

    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "record_final_output must fire on_change exactly once"
    );
    assert_eq!(
        seen_output.lock().unwrap().as_deref(),
        Some("the child's full result"),
        "the on_change snapshot must carry the recorded final_output"
    );
}

#[test]
fn should_register_task_with_spawned_status() {
    let supervisor = TaskSupervisor::new();
    let id = supervisor.register_with_input("tts", "call-123", None, None);

    let tasks = supervisor.get_all_tasks();
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].id, id);
    assert_eq!(tasks[0].tool_name, "tts");
    assert_eq!(tasks[0].tool_call_id, "call-123");
    assert_eq!(tasks[0].status, TaskStatus::Spawned);
    assert_eq!(tasks[0].runtime_state, TaskRuntimeState::Spawned);
    assert!(tasks[0].child_terminal_state.is_none());
    assert!(tasks[0].child_join_state.is_none());
    assert!(tasks[0].child_failure_action.is_none());
    assert!(tasks[0].completed_at.is_none());
    assert!(tasks[0].updated_at >= tasks[0].started_at);
}

#[test]
fn should_transition_through_lifecycle_states() {
    let supervisor = TaskSupervisor::new();
    let id = supervisor.register_with_input("tts", "call-1", None, None);
    let task = &supervisor.get_all_tasks()[0];
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Queued);

    supervisor.mark_running(&id);
    let task = &supervisor.get_all_tasks()[0];
    assert_eq!(task.status, TaskStatus::Running);
    assert_eq!(task.runtime_state, TaskRuntimeState::ExecutingTool);
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Running);

    supervisor.mark_runtime_state(
        &id,
        TaskRuntimeState::DeliveringOutputs,
        Some("send_file".to_string()),
    );
    let task = &supervisor.get_all_tasks()[0];
    assert_eq!(task.status, TaskStatus::Running);
    assert_eq!(task.runtime_state, TaskRuntimeState::DeliveringOutputs);
    assert_eq!(task.runtime_detail.as_deref(), Some("send_file"));
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Verifying);

    supervisor.mark_completed(&id, vec!["output.mp3".to_string()]);
    let task = &supervisor.get_all_tasks()[0];
    assert_eq!(task.status, TaskStatus::Completed);
    assert_eq!(task.runtime_state, TaskRuntimeState::Completed);
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Ready);
    assert!(task.completed_at.is_some());
    assert_eq!(task.output_files, vec!["output.mp3"]);
}

#[test]
fn should_track_failed_tasks_with_error() {
    let supervisor = TaskSupervisor::new();
    let id = supervisor.register_with_input("tts", "call-2", None, None);

    supervisor.mark_running(&id);
    supervisor.mark_failed(&id, "connection refused".to_string());

    let task = &supervisor.get_all_tasks()[0];
    assert_eq!(task.status, TaskStatus::Failed);
    assert_eq!(task.runtime_state, TaskRuntimeState::Failed);
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Failed);
    assert_eq!(task.error.as_deref(), Some("connection refused"));
    assert!(task.completed_at.is_some());
}

#[test]
fn should_return_only_active_tasks_in_get_active() {
    let supervisor = TaskSupervisor::new();
    let id1 = supervisor.register_with_input("tts", "call-1", None, None);
    let _id2 = supervisor.register_with_input("tts", "call-2", None, None);

    supervisor.mark_completed(&id1, vec![]);

    let active = supervisor.get_active_tasks();
    assert_eq!(active.len(), 1);
    assert_eq!(active[0].tool_call_id, "call-2");
}

// ── M8.9: spawn_only failure recovery signals ───────────────────────────

use std::sync::Mutex as StdMutex;

fn collect_failure_signals(
    supervisor: &TaskSupervisor,
) -> Arc<StdMutex<Vec<SpawnOnlyFailureSignal>>> {
    let collected = Arc::new(StdMutex::new(Vec::new()));
    let captured = Arc::clone(&collected);
    supervisor.set_on_failure_signal(move |signal| {
        captured
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(signal.clone());
    });
    collected
}

#[test]
fn should_emit_failure_signal_when_spawn_only_task_status_becomes_failed() {
    let supervisor = TaskSupervisor::new();
    let collected = collect_failure_signals(&supervisor);
    let task_id = supervisor.register_with_input(
        "fm_tts",
        "call-1",
        Some("api:session"),
        Some(serde_json::json!({"voice": "yangmi", "text": "hi"})),
    );
    // Synth-ack gate: simulate the LLM having seen the
    // "Background work started for `fm_tts`." ack — production wires
    // this from `loop_runner.rs` when the synth-ack fires.
    supervisor.mark_synth_ack_emitted("call-1");
    supervisor.mark_running(&task_id);
    supervisor.mark_failed(
        &task_id,
        "voice 'yangmi' not registered. available: vivian, serena, longxiang".to_string(),
    );

    let signals = collected.lock().unwrap().clone();
    assert_eq!(signals.len(), 1, "expected exactly one failure signal");
    let signal = &signals[0];
    assert_eq!(signal.task_id, task_id);
    assert_eq!(signal.tool_name, "fm_tts");
    assert_eq!(signal.parent_session_key.as_deref(), Some("api:session"));
    assert!(
        signal
            .error_message
            .contains("voice 'yangmi' not registered")
    );
    assert_eq!(
        signal.suggested_alternatives,
        vec![
            "vivian".to_string(),
            "serena".to_string(),
            "longxiang".to_string()
        ]
    );
    assert_eq!(signal.tool_input["voice"], "yangmi");
}

#[test]
fn should_emit_failure_signal_only_after_synth_ack_recorded_for_tool_call_id() {
    let supervisor = TaskSupervisor::new();
    let collected = collect_failure_signals(&supervisor);

    // First task — synth-ack was suppressed, failure must NOT signal.
    let suppressed_task = supervisor.register_with_input("fm_tts", "call-suppressed", None, None);
    supervisor.mark_failed(&suppressed_task, "boom A".to_string());

    // Second task — synth-ack fired, failure MUST signal.
    let acked_task = supervisor.register_with_input("fm_tts", "call-acked", None, None);
    supervisor.mark_synth_ack_emitted("call-acked");
    supervisor.mark_failed(&acked_task, "boom B".to_string());

    let signals = collected.lock().unwrap().clone();
    assert_eq!(
        signals.len(),
        1,
        "exactly one failure signal — the synth-acked task — must reach the callback",
    );
    assert_eq!(signals[0].task_id, acked_task);
}

// ── F004 B2: TaskSupervisor → ToolProgress bridge ─────────────────────

/// Test reporter that captures every reported event so the bridge
/// assertions can branch on event kind without parsing JSON.
struct CapturingReporter {
    events: Arc<StdMutex<Vec<crate::progress::ProgressEvent>>>,
}

impl crate::progress::ProgressReporter for CapturingReporter {
    fn report(&self, event: crate::progress::ProgressEvent) {
        self.events
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(event);
    }
}

fn collect_progress_events(
    supervisor: &TaskSupervisor,
) -> Arc<StdMutex<Vec<crate::progress::ProgressEvent>>> {
    let events = Arc::new(StdMutex::new(Vec::new()));
    let reporter = Arc::new(CapturingReporter {
        events: Arc::clone(&events),
    });
    supervisor.set_progress_reporter(reporter);
    events
}

fn extract_tool_progress(
    events: &[crate::progress::ProgressEvent],
) -> Vec<(String, String, String)> {
    events
        .iter()
        .filter_map(|event| match event {
            crate::progress::ProgressEvent::ToolProgress {
                name,
                tool_id,
                message,
            } => Some((name.clone(), tool_id.clone(), message.clone())),
            _ => None,
        })
        .collect()
}

#[test]
fn should_emit_tool_progress_on_completion_with_tool_call_id() {
    let supervisor = TaskSupervisor::new();
    let events = collect_progress_events(&supervisor);
    let task_id = supervisor.register_with_input("podcast_generate", "call-complete-1", None, None);
    supervisor.mark_completed(&task_id, vec!["/tmp/out.mp3".to_string()]);

    let captured = events.lock().unwrap().clone();
    let tool_progress = extract_tool_progress(&captured);
    let completion = tool_progress
        .iter()
        .find(|(_, _, message)| message.ends_with(": completed"))
        .expect("completion progress event missing");
    assert_eq!(completion.0, "podcast_generate");
    assert_eq!(completion.1, "call-complete-1");
    assert_eq!(completion.2, "podcast_generate: completed");
}

// ────────── M7.9 cancel / relaunch primitives (W2) ──────────

#[test]
fn cancel_running_task_transitions_to_cancelled_and_fires_token() {
    let supervisor = TaskSupervisor::new();
    let task_id = supervisor.register_with_input("bg_research", "call-cancel-1", Some("session-A"), None);
    supervisor.mark_running(&task_id);
    let token = supervisor.cancel_token(&task_id);
    assert!(!token.is_cancelled());

    supervisor.cancel(&task_id).expect("cancel should succeed");

    let task = supervisor.get_task(&task_id).expect("task still tracked");
    assert_eq!(task.status, TaskStatus::Cancelled);
    assert_eq!(task.runtime_state, TaskRuntimeState::Cancelled);
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Cancelled);
    assert!(token.is_cancelled());
    assert!(task.completed_at.is_some());
}

#[test]
fn cancel_unknown_task_returns_not_found() {
    let supervisor = TaskSupervisor::new();
    let result = supervisor.cancel("does-not-exist");
    assert_eq!(result, Err(TaskCancelError::NotFound));
}

// ────────── M8 Req #4 DoD: cancel cannot be overwritten by late workers ──────────

/// Race regression: a worker that finishes AFTER the user has cancelled
/// the task must NOT resurrect it to `Completed`. The supervisor's
/// `mark_completed` guard short-circuits when the task is already in a
/// terminal state. Asserts state stays `Cancelled`, the on_change callback
/// fires exactly twice (once for `mark_running`, once for `cancel`), and
/// the ProgressReporter does NOT emit a spurious "completed" event after
/// cancellation.
#[test]
fn mark_completed_after_cancel_does_not_overwrite_cancelled_state() {
    use std::sync::Mutex;
    let supervisor = TaskSupervisor::new();
    let progress_events = collect_progress_events(&supervisor);
    let on_change_count: Arc<Mutex<usize>> = Arc::new(Mutex::new(0));
    {
        let on_change_count = on_change_count.clone();
        supervisor.set_on_change(move |_task| {
            *on_change_count.lock().unwrap() += 1;
        });
    }

    let task_id = supervisor.register_with_input("bg_research", "call-race-1", Some("session-X"), None);
    supervisor.mark_running(&task_id); // notify #1
    supervisor.cancel(&task_id).expect("cancel should succeed"); // notify #2

    // Late-arriving worker tries to mark completed — this is the race.
    supervisor.mark_completed(&task_id, vec!["late/output.bin".into()]); // must noop

    let task = supervisor.get_task(&task_id).expect("task still tracked");
    assert_eq!(
        task.status,
        TaskStatus::Cancelled,
        "late mark_completed must NOT overwrite Cancelled state"
    );
    assert_eq!(task.runtime_state, TaskRuntimeState::Cancelled);
    assert_eq!(task.lifecycle_state(), TaskLifecycleState::Cancelled);
    assert!(
        task.output_files.is_empty(),
        "late completion's output_files must not leak onto a Cancelled task, got: {:?}",
        task.output_files
    );

    // on_change must have fired exactly twice — guard noop must not
    // double-fire the change callback.
    assert_eq!(
        *on_change_count.lock().unwrap(),
        2,
        "on_change should fire exactly twice (mark_running + cancel), not for the noop mark_completed"
    );

    // ProgressReporter must not have emitted any "completed" message
    // after cancellation. We saw running + cancelled, but never completed.
    let captured = progress_events.lock().unwrap().clone();
    let tool_progress = extract_tool_progress(&captured);
    let post_cancel_completed: Vec<_> = tool_progress
        .iter()
        .filter(|(_, _, message)| message.contains("completed"))
        .collect();
    assert!(
        post_cancel_completed.is_empty(),
        "guard must not emit 'completed' progress for a cancelled task, got: {tool_progress:?}"
    );
}

#[test]
fn relaunch_failed_task_creates_lineage_stamped_successor() {
    let supervisor = TaskSupervisor::new();

    let task_id = supervisor.register_with_input("bg_research", "call-relaunch-1", Some("session-D"), None);
    supervisor.mark_running(&task_id);
    supervisor.mark_failed(&task_id, "node 'design' failed".to_string());

    let new_id = supervisor
        .relaunch(
            &task_id,
            RelaunchOpts {
                from_node: Some("design".into()),
            },
        )
        .expect("relaunch should succeed");
    assert_ne!(new_id, task_id, "relaunch must allocate a fresh id");

    let new_task = supervisor.get_task(&new_id).expect("successor registered");
    assert_eq!(new_task.tool_name, "bg_research");
    assert_eq!(new_task.tool_call_id, "call-relaunch-1");
    assert_eq!(new_task.session_key.as_deref(), Some("session-D"));
    // Lineage edge is stamped in runtime_detail for callers to surface.
    let detail = new_task.runtime_detail.as_deref().unwrap_or_default();
    assert!(detail.contains(&format!("\"relaunched_from\":\"{task_id}\"")), "{detail}");
}


/// STEP 2: a guard armed after `mark_running` and dropped WITHOUT a
/// terminal call drives the task to `Failed` with the dropped-worker
/// reason.
#[test]
fn terminal_guard_marks_failed_when_dropped_while_active() {
    use std::sync::Arc;

    let supervisor = Arc::new(TaskSupervisor::new());
    let id = supervisor.register_with_input("mofa_slides", "call-guard", Some("api:session"), None);
    supervisor.mark_running(&id);

    {
        let _guard = TaskTerminalGuard::new(Arc::clone(&supervisor), id.clone());
        // No terminal call inside the scope — simulate an aborted body.
    }

    let task = supervisor.get_task(&id).expect("task");
    assert_eq!(task.status, TaskStatus::Failed);
    assert_eq!(
        task.error.as_deref(),
        Some("worker dropped before reaching terminal state"),
    );
}

// ── Orphan-sweep liveness gate (fix/orphan-sweep-liveness-gate) ──
//
// The WS turn path rebuilds a BRAND-NEW per-turn `TaskSupervisor`
// every turn and calls `enable_persistence(...)` over the SHARED
// per-session ledger. `enable_persistence`'s orphan-sweep ASSUMES
// "non-terminal ⇒ no live worker", so it FALSELY marks a still-Running
// DETACHED spawn_only task (a detached bg_research run, up to ~3600s)
// as "orphaned across restart" — even though the worker is alive on the
// PREVIOUS turn's supervisor and will mark_completed shortly. The fix
// gates the sweep on a process-global live-set that survives the
// per-turn supervisor rebuild and is empty after a true cross-process
// restart.

