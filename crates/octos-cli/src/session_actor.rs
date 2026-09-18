//! Session-scoped runtime shared state.
//!
//! This module now only carries the LIVE pieces of the former session-actor
//! machinery: the [`SessionTaskQueryStore`] the serve/stdio/api task-query
//! surfaces read, and the [`PendingMessages`] alias. The per-session actor
//! task itself (and its registry/factory) was removed with the gateway
//! subsystem; the serve/stdio transport owns its turn execution directly.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex, Weak};

use octos_agent::TaskSupervisor;
use octos_core::SessionKey;

/// Shared lookup table for session-scoped background task supervisors.
#[derive(Default, Clone)]
pub struct SessionTaskQueryStore {
    /// Per-session list of registered supervisors, oldest-first. A session
    /// accumulates more than one when a long-running `spawn_only` task spawned
    /// in an earlier turn is still live (its worker holds that turn's
    /// supervisor alive via `Arc<ToolRegistry>`) while a later turn registers a
    /// fresh supervisor — `ToolRegistry::snapshot_excluding` builds a NEW
    /// `TaskSupervisor` per turn. Keeping all live ones (rather than
    /// overwriting) lets `cancel_task` reach the supervisor whose cancel token
    /// the live worker actually polls; cancelling through a later turn's
    /// supervisor would only fire a useless fresh token.
    supervisors: Arc<StdMutex<HashMap<String, Vec<SessionTaskQueryEntry>>>>,
}

struct SessionTaskQueryEntry {
    supervisor: Weak<TaskSupervisor>,
    data_dir: PathBuf,
}

impl SessionTaskQueryStore {
    pub fn register(
        &self,
        session_key: &SessionKey,
        supervisor: &Arc<TaskSupervisor>,
        data_dir: &Path,
    ) {
        let mut guard = self.supervisors.lock().unwrap_or_else(|e| e.into_inner());
        let entries = guard.entry(session_key.to_string()).or_default();
        // Drop entries whose supervisor has been dropped (its turn ended with
        // no live task holding it), then dedup: if this exact supervisor is
        // already registered, just refresh its data_dir. Otherwise append at
        // the end so the per-session order stays oldest-first — `cancel_task`
        // scans oldest-first to prefer the supervisor the live worker polls.
        entries.retain(|entry| entry.supervisor.strong_count() > 0);
        for entry in entries.iter_mut() {
            if let Some(existing) = entry.supervisor.upgrade() {
                if Arc::ptr_eq(&existing, supervisor) {
                    entry.data_dir = data_dir.to_path_buf();
                    return;
                }
            }
        }
        entries.push(SessionTaskQueryEntry {
            supervisor: Arc::downgrade(supervisor),
            data_dir: data_dir.to_path_buf(),
        });
    }

    /// Return every live supervisor + data dir registered for `session_key`,
    /// oldest-first, pruning entries whose `Arc<TaskSupervisor>` has dropped
    /// (and the session key entirely when none remain). A session has more
    /// than one when an earlier turn's supervisor is still alive — a live
    /// `spawn_only` worker holds it — alongside a later turn's fresh one.
    fn live_entries_for_session(&self, session_key: &str) -> Vec<(Arc<TaskSupervisor>, PathBuf)> {
        let mut guard = self.supervisors.lock().unwrap_or_else(|e| e.into_inner());
        let Some(entries) = guard.get_mut(session_key) else {
            return Vec::new();
        };
        let mut live = Vec::new();
        entries.retain(|entry| match entry.supervisor.upgrade() {
            Some(supervisor) => {
                live.push((supervisor, entry.data_dir.clone()));
                true
            }
            None => false,
        });
        if entries.is_empty() {
            guard.remove(session_key);
        }
        live
    }

    /// C8 / GAP A: return the raw [`octos_agent::BackgroundTask`] snapshots for
    /// `session_key` (and every reachable descendant session), each paired with
    /// the owning supervisor's `data_dir` for path encoding. Mirrors
    /// [`Self::query_json`]'s breadth-first traversal but yields the raw task
    /// structs so the WS `session/open` handler can replay each one as a
    /// `task/updated` event through the SAME emission path live updates use
    /// (`background_task_to_progress_json`). A reconnecting / freshly-opening
    /// TUI starts with an empty `session.tasks` and only applies incremental
    /// updates, so without this replay the existing task list is invisible
    /// until the next live transition.
    pub fn raw_tasks_for_session(
        &self,
        session_key: &str,
    ) -> Vec<(octos_agent::BackgroundTask, PathBuf)> {
        let mut tasks: Vec<(octos_agent::BackgroundTask, PathBuf)> = Vec::new();
        let mut visited: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut queue: std::collections::VecDeque<String> = std::collections::VecDeque::new();
        queue.push_back(session_key.to_string());
        visited.insert(session_key.to_string());

        let mut seen_task_ids: std::collections::HashSet<String> = std::collections::HashSet::new();
        while let Some(current) = queue.pop_front() {
            // See `query_json`: walk every live supervisor for the session
            // oldest-first, dedup by task id across supervisors.
            for (supervisor, data_dir) in self.live_entries_for_session(&current) {
                for task in supervisor.get_tasks_for_session(&current) {
                    if !seen_task_ids.insert(task.id.clone()) {
                        continue;
                    }
                    if let Some(child_key) = task.child_session_key.as_deref() {
                        if visited.insert(child_key.to_string()) {
                            queue.push_back(child_key.to_string());
                        }
                    }
                    tasks.push((task, data_dir.clone()));
                }
            }
        }

        tasks
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// C8 / GAP A: `raw_tasks_for_session` returns the live `BackgroundTask`
    /// snapshots (paired with the owning supervisor's data_dir) so the WS
    /// `session/open` handler can replay them as `task/updated` events. It must
    /// surface the same tasks `query_json` does, keyed by the registered
    /// `SessionKey`, and return empty for an unknown session.
    #[test]
    fn raw_tasks_for_session_returns_live_supervisor_tasks() {
        let dir = tempfile::TempDir::new().unwrap();
        let data_dir = dir.path().join("profile-data");
        std::fs::create_dir_all(&data_dir).unwrap();

        let supervisor = Arc::new(TaskSupervisor::new());
        let task_id = supervisor.register("bg_research", "call-1", Some("api:session"));
        supervisor.mark_running(&task_id);

        let store = SessionTaskQueryStore::default();
        let session_key = SessionKey::new("api", "session");
        store.register(&session_key, &supervisor, &data_dir);

        let tasks = store.raw_tasks_for_session(&session_key.to_string());
        assert_eq!(tasks.len(), 1, "the running task must be surfaced");
        let (task, returned_data_dir) = &tasks[0];
        assert_eq!(task.id, task_id);
        assert_eq!(task.tool_name, "bg_research");
        assert_eq!(task.tool_call_id, "call-1");
        assert_eq!(task.status, octos_agent::TaskStatus::Running);
        assert_eq!(returned_data_dir, &data_dir);

        // An unknown session has no live supervisor → empty replay.
        assert!(
            store
                .raw_tasks_for_session(&SessionKey::new("api", "other").to_string())
                .is_empty()
        );
    }

    /// `raw_tasks_for_session` mirrors `query_json`'s multi-supervisor walk +
    /// task-id dedup (it feeds reconnect/session-open task replay).
    #[test]
    fn raw_tasks_for_session_walks_all_live_supervisors_and_dedups() {
        let dir = tempfile::TempDir::new().unwrap();
        let data_dir = dir.path().join("profile-data");
        std::fs::create_dir_all(&data_dir).unwrap();
        let session_key = SessionKey::new("api", "session");

        let sup1 = Arc::new(TaskSupervisor::new());
        let t1 = sup1.register("bg_research", "call-1", Some("api:session"));
        sup1.mark_running(&t1);

        let sup2 = Arc::new(TaskSupervisor::new());
        let t2 = sup2.register("deep_search", "call-2", Some("api:session"));
        sup2.mark_running(&t2);

        let store = SessionTaskQueryStore::default();
        store.register(&session_key, &sup1, &data_dir);
        store.register(&session_key, &sup2, &data_dir);

        let tasks = store.raw_tasks_for_session(&session_key.to_string());
        assert_eq!(
            tasks.iter().filter(|(task, _)| task.id == t1).count(),
            1,
            "T1 must appear exactly once"
        );
        assert!(
            tasks.iter().any(|(task, _)| task.id == t2),
            "the later supervisor's own task must surface"
        );
    }
}
