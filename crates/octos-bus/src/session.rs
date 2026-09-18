//! Session management with JSONL persistence and LRU eviction.

use std::collections::HashSet;
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use chrono::{DateTime, Utc};
use eyre::Result;
use lru::LruCache;
use metrics::counter;
use octos_core::{Message, MessageRole, SessionKey};
use serde::{Deserialize, Serialize};
use tracing::{debug, warn};

/// Current schema version for session JSONL files.
const CURRENT_SESSION_SCHEMA: u32 = 1;

/// Observer callback invoked AFTER a successful durable commit by
/// [`SessionManager::add_message_with_seq`] (and the equivalent
/// `SessionHandle` path). Implements the post-commit hook UPCR-2026-012's
/// `message/persisted` notification dispatches through.
///
/// Strict-ordering invariant: `add_message_with_seq` calls observers
/// synchronously after the in-memory append. Two concurrent commits to the
/// same session key serialize on the per-key persist lock (see
/// `persist_lock_for`) — independent writers (separate `SessionHandle`s,
/// the canonical persist helper, a `SessionManager`) all contend on the
/// same lock, so observer fires preserve commit order per session.
///
/// The seq passed to the observer is the row's index in the durable
/// transcript: on the `SessionHandle` path it is read back from the
/// on-disk file under the persist lock; on the `SessionManager` path it is
/// the merged in-memory mirror's `messages.len() - 1` after the append.
///
/// Errors raised by an observer are logged and dropped: the durable commit
/// has already happened, and the observer is best-effort fan-out for
/// downstream wire-protocol notifications. A failing observer must not roll
/// back the session row.
pub type MessageCommitObserver =
    std::sync::Arc<dyn Fn(&SessionKey, &Message, usize) + Send + Sync + 'static>;

type ScopedCommitObservers = std::collections::HashMap<
    PathBuf,
    std::sync::Weak<dyn Fn(&SessionKey, &Message, usize) + Send + Sync>,
>;

fn scoped_commit_observers() -> &'static std::sync::Mutex<ScopedCommitObservers> {
    static OBSERVERS: std::sync::OnceLock<std::sync::Mutex<ScopedCommitObservers>> =
        std::sync::OnceLock::new();
    OBSERVERS.get_or_init(Default::default)
}

/// Bind an observer to a runtime's storage root. The runtime must retain the
/// supplied Arc; weak registration never keeps a closed runtime alive.
pub fn set_scoped_message_commit_observer(data_dir: &Path, observer: &MessageCommitObserver) {
    let root = std::fs::canonicalize(data_dir).unwrap_or_else(|_| data_dir.to_owned());
    let mut observers = scoped_commit_observers().lock().unwrap();
    observers.retain(|_, observer| observer.strong_count() > 0);
    observers.insert(root, std::sync::Arc::downgrade(observer));
}

/// Fire the observer if installed. Panics inside the observer are caught
/// (best-effort) so a faulty subscriber cannot poison the commit path. The
/// commit has already succeeded by the time we get here — observer failure
/// is fan-out failure, not commit failure.
fn notify_message_commit(
    data_dir: &Path,
    key: &SessionKey,
    message: &Message,
    committed_seq: usize,
) {
    let root = std::fs::canonicalize(data_dir).unwrap_or_else(|_| data_dir.to_owned());
    let Some(observer) = scoped_commit_observers()
        .lock()
        .unwrap()
        .get(&root)
        .cloned()
        .and_then(|observer| observer.upgrade())
    else {
        return;
    };
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        observer(key, message, committed_seq);
    }));
    if let Err(err) = result {
        warn!(
            session = %key.0,
            "message commit observer panicked: {:?}",
            err
        );
    }
}

/// Per-process counter for unique rewrite-temp-file names.
///
/// Two writers racing the same session file (e.g. fanout children of one
/// parent terminating in the same millisecond, both calling
/// `parent.upsert_child_contract → parent.rewrite()`) used to share a
/// single `<file>.jsonl.tmp` path. They'd both `File::create` it (the
/// second truncating the first), and only one `rename` would succeed —
/// the loser saw `ENOENT` and returned an error. In the spawn lifecycle
/// this manifested as the unlucky child being marked `Orphaned` instead
/// of `Joined` despite both terminal states being `Completed`.
static REWRITE_TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Build a unique temp-file path for atomic rewrite of a session JSONL.
///
/// PID + monotonic counter make the suffix collision-free across:
/// - Concurrent rewrites of the same parent file from different tokio tasks
///   (counter ticks)
/// - Concurrent rewrites from different processes sharing a data dir
///   (PID disambiguates)
fn rewrite_tmp_path(target: &Path) -> PathBuf {
    let pid = std::process::id();
    let seq = REWRITE_TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    target.with_extension(format!("jsonl.{pid}-{seq}.tmp"))
}

/// Seal a torn tail before appending: if the file is non-empty and does
/// not end in a newline (a crash split an earlier write), write the
/// missing terminator first so the next row can never fuse with the torn
/// bytes into one unparseable line. The torn bytes are preserved, never
/// truncated — they may be a complete row that merely lost its
/// terminator; the read path skips what it cannot parse. Mirrors
/// `supervisor_store::write_rows_sealed_locked`.
///
/// Callers serialize appends per session key (the persist lock), like the
/// supervisor store's append lock; the seal does not defend against
/// unsynchronized cross-process writers to the same file.
///
/// O_APPEND: the sealing write lands at EOF regardless of the read seek.
fn seal_torn_tail(file: &mut std::fs::File, file_len: u64) -> std::io::Result<()> {
    use std::io::{Read, Seek, SeekFrom, Write};
    if file_len > 0 {
        file.seek(SeekFrom::Start(file_len - 1))?;
        let mut last = [0_u8; 1];
        file.read_exact(&mut last)?;
        if last[0] != b'\n' {
            file.write_all(b"\n")?;
        }
    }
    Ok(())
}

/// FNV-1a 64-bit hash — deterministic across Rust versions (unlike DefaultHasher).
/// Used for session filename suffixes on truncated keys.
fn fnv1a_64(data: &[u8]) -> u64 {
    const FNV_OFFSET: u64 = 0xcbf29ce484222325;
    const FNV_PRIME: u64 = 0x00000100000001B3;
    let mut hash = FNV_OFFSET;
    for &byte in data {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(FNV_PRIME);
    }
    hash
}

/// Encode a string for safe use as a directory/file name component.
/// Alphanumerics, `-`, `_` pass through; everything else is percent-encoded.
pub fn encode_path_component(s: &str) -> String {
    let mut encoded = String::new();
    for byte in s.as_bytes() {
        if byte.is_ascii_alphanumeric() || *byte == b'-' || *byte == b'_' {
            encoded.push(*byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

/// Derive a stable child session key from a parent session key and a child id.
///
/// The child id is percent-encoded so it remains safe for filenames and
/// ledger metadata.
pub fn child_session_key(parent: &SessionKey, child_id: &str) -> SessionKey {
    let child_id = encode_path_component(child_id);
    SessionKey(format!("{}#child-{child_id}", parent.0))
}

fn default_session_schema() -> u32 {
    CURRENT_SESSION_SCHEMA
}

/// Derive a 50-char display title from a user message's text content.
///
/// Trims whitespace, strips JSON content-array wrappers if present, and
/// truncates to 50 Unicode characters at a UTF-8 boundary so the result
/// is safe to persist and round-trip through serde.
/// Unwrap `[{"type":"text","text":"…"}]`-shaped content (many UI clients send
/// this) to its inner text; plain-string content passes through unchanged.
/// Shared by title derivation and the last-prompt preview so neither surfaces
/// raw content-part JSON.
fn content_display_text(content: &str) -> String {
    let plain = content.trim();
    serde_json::from_str::<Vec<serde_json::Value>>(plain)
        .ok()
        .and_then(|parts| {
            parts
                .into_iter()
                .find_map(|p| p.get("text").and_then(|t| t.as_str()).map(String::from))
        })
        .unwrap_or_else(|| plain.to_string())
}

fn derive_title_from_message(content: &str) -> String {
    content_display_text(content)
        .trim()
        .chars()
        .take(50)
        .collect::<String>()
        .trim()
        .to_string()
}

/// Derive `thread_id` for a brand-new write about to be persisted.
///
/// PR F (M8.10 thread-binding chain `#649 → #740`): fail-closed for
/// Assistant/Tool roles. Returns:
/// - `Ok(Some(tid))` — User message: from `client_message_id`, or a
///   freshly-synthesized UUIDv7 if no cmid was supplied.
/// - `Ok(None)` — System message (system rows aren't thread-scoped).
/// - `Err(_)` — Assistant/Tool message arrived unbound. The previous
///   "walk history for the most recent user" derivation was structurally
///   wrong under concurrent web turns: sibling users could rotate the
///   in-memory history between the originating turn's user-write and its
///   assistant-write, picking the WRONG cmid. The fix forces every
///   caller on the new write path to pre-stamp `thread_id`. The metric
///   `octos_session_persist_total{outcome="rejected_unbound_assistant"}`
///   alerts on regressions.
pub(crate) fn derive_thread_id_for_new_write(
    message: &Message,
    _history: &[Message],
) -> Result<Option<String>> {
    use octos_core::MessageRole;

    match message.role {
        MessageRole::System => Ok(None),
        MessageRole::User => Ok(Some(
            message
                .client_message_id
                .clone()
                .unwrap_or_else(|| uuid::Uuid::now_v7().to_string()),
        )),
        MessageRole::Assistant | MessageRole::Tool => Err(eyre::eyre!(
            "PR F (M8.10 thread-binding): Assistant/Tool persist on new write \
             path requires caller-supplied `thread_id`. Pre-stamp the \
             originating turn's `thread_id` before calling \
             `add_message_with_seq` — see `persist_assistant_message` in \
             `octos-cli/src/session_actor.rs` for the canonical helper."
        )),
    }
}

/// Synthesize `thread_id` for legacy JSONL records that pre-date the field
/// (M8.10 PR #1). Runs on session load — the synthesized values are
/// in-memory only; nothing is persisted at load time. On the next write,
/// [`derive_thread_id_for_new_write`] produces the same logical structure
/// going forward, so transcripts converge naturally.
///
/// Algorithm walks the messages in JSONL order and threads them via the
/// `client_message_id` hints already stamped by the new write path:
///
/// - `User` with `client_message_id`: starts a new thread keyed by that id.
/// - `User` without `client_message_id`: synthesizes `synth_{seq}` so the
///   message still has a stable group key.
/// - `Assistant` / `Tool`: inherit the current thread (the one the most
///   recent user message rooted, or the previous assistant's thread for a
///   tool result).
/// - `System`: untouched (`None` — system messages aren't thread-scoped).
pub(crate) fn synthesize_thread_ids(messages: &mut [Message]) {
    use octos_core::MessageRole;

    let mut current_thread: Option<String> = None;
    for (seq, message) in messages.iter_mut().enumerate() {
        if message.thread_id.is_some() {
            // Already populated (new write path) — track it as the current
            // thread so subsequent legacy rows that lack the field can
            // inherit cleanly.
            if matches!(message.role, MessageRole::User | MessageRole::Assistant) {
                current_thread.clone_from(&message.thread_id);
            }
            continue;
        }

        match message.role {
            MessageRole::System => {}
            MessageRole::User => {
                let id = message
                    .client_message_id
                    .clone()
                    .unwrap_or_else(|| format!("synth_{seq}"));
                message.thread_id = Some(id.clone());
                current_thread = Some(id);
            }
            MessageRole::Assistant | MessageRole::Tool => {
                if let Some(thread) = current_thread.clone() {
                    message.thread_id = Some(thread);
                } else {
                    // No user message has been seen yet — orphaned assistant
                    // (e.g. system primer transcript). Synthesize a stable id
                    // so the load-time render never trips on a missing key.
                    let id = format!("synth_{seq}");
                    message.thread_id = Some(id.clone());
                    current_thread = Some(id);
                }
            }
        }
    }
}

/// Append-only control records interleaved with `Message` lines in a session
/// JSONL. Discriminated on `kind` so [`assemble_session_messages`] can tell
/// them apart from `Message` rows — a persisted [`Message`] never carries a
/// `kind` field, and an internally-tagged control record has no `role`, so the
/// two decode paths are mutually exclusive.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum SessionControlRecord {
    /// Conversation-only rewind marker: drop the last `num_turns` user turns
    /// from the transcript accumulated up to this point in the log. Written by
    /// [`SessionManager::rollback_last_n_user_turns`] and replayed on every
    /// load (append-only, never a truncation).
    Rollback { num_turns: u32, at: DateTime<Utc> },
}

/// Serialize a rollback control record to its single-line JSONL form.
fn rollback_marker_line(num_turns: u32) -> Result<String> {
    let record = SessionControlRecord::Rollback {
        num_turns,
        at: Utc::now(),
    };
    Ok(serde_json::to_string(&record)?)
}

/// One ordered item in a session JSONL's post-meta timeline: a persisted
/// [`Message`] or an append-only rollback control marker. Parsing keeps the two
/// interleaved in log order so the marker can be replayed positionally — by log
/// order within a single file ([`fold_session_timeline`]) or by timestamp after
/// a flat + per-user merge (in [`SessionManager::load_from_disk`]).
///
/// `Message` is boxed so the small `Rollback` variant does not inflate every
/// element of a session-length `Vec` (clippy `large_enum_variant`).
enum SessionTimelineItem {
    Message(Box<Message>),
    Rollback { num_turns: u32, at: DateTime<Utc> },
}

/// Parse a session JSONL's post-meta lines into an ordered timeline WITHOUT
/// applying any rollback drop.
///
/// Control lines are recognized before the `Message` decode (they carry a
/// `kind` discriminator a `Message` never has) and are NOT parsed as messages.
/// Unparseable lines are skipped, matching the prior
/// `filter_map(|line| serde_json::from_str(line).ok())` behavior. The rollback
/// drop is deferred to [`fold_session_timeline`] so the merge path in
/// [`SessionManager::load_from_disk`] can apply markers against the combined
/// flat + per-user transcript rather than one file in isolation.
fn parse_session_timeline<'a>(lines: impl Iterator<Item = &'a str>) -> Vec<SessionTimelineItem> {
    let mut timeline: Vec<SessionTimelineItem> = Vec::new();
    for line in lines.filter(|line| !line.trim().is_empty()) {
        if let Ok(control) = serde_json::from_str::<SessionControlRecord>(line) {
            match control {
                SessionControlRecord::Rollback { num_turns, at } => {
                    timeline.push(SessionTimelineItem::Rollback { num_turns, at });
                }
            }
            continue;
        }
        if let Ok(message) = serde_json::from_str::<Message>(line) {
            timeline.push(SessionTimelineItem::Message(Box::new(message)));
        }
    }
    timeline
}

/// Fold an ordered timeline into the trimmed `Message` list, replaying each
/// rollback marker at its position.
///
/// A `rollback` record drops the last N user turns accumulated *so far* —
/// applying it positionally (rather than after the whole timeline) is what makes
/// rewind-then-continue survive a reload: turns appended after the marker are
/// kept, turns before it are trimmed. `thread_id`s are synthesized for legacy
/// rows before each drop and once at the end, so a timeline without any control
/// record folds exactly as it did pre-marker (a single synthesize pass over the
/// full transcript).
fn fold_session_timeline(timeline: Vec<SessionTimelineItem>) -> Vec<Message> {
    let mut messages: Vec<Message> = Vec::new();
    for item in timeline {
        match item {
            SessionTimelineItem::Message(message) => messages.push(*message),
            SessionTimelineItem::Rollback { num_turns, .. } => {
                synthesize_thread_ids(&mut messages);
                drop_last_n_user_turns(&mut messages, num_turns);
            }
        }
    }
    synthesize_thread_ids(&mut messages);
    messages
}

/// Count the number of distinct user turns in `messages`.
///
/// A "user turn" is the message group sharing one `User` message's
/// [`Message::thread_id`]. System messages and the assistant/tool replies
/// that inherit a turn's thread_id never start a new turn, so the count
/// equals the number of distinct thread_ids rooted by a `User` message.
fn count_user_turns(messages: &[Message]) -> u32 {
    let mut seen: HashSet<&str> = HashSet::new();
    let mut count = 0_u32;
    for msg in messages {
        if matches!(msg.role, MessageRole::User) {
            if let Some(tid) = msg.thread_id.as_deref() {
                if seen.insert(tid) {
                    count = count.saturating_add(1);
                }
            }
        }
    }
    count
}

/// Drop the last `n` user turns from `messages` in place, returning the number
/// of turns actually removed (clamped to the transcript's turn count).
///
/// A user turn is the set of messages sharing one `User` message's
/// [`Message::thread_id`]. Removing the last `n` turns deletes those groups'
/// user + assistant + tool messages — i.e. everything from the `n`-from-last
/// user message onward. Leading `System` messages and any compaction-summary
/// `System` message carry `thread_id == None`, are never part of a user turn,
/// and always survive; dropping every user turn therefore returns the
/// transcript to its pre-first-user state.
///
/// Deterministic and idempotent when driven by the append-only rollback
/// marker: applied at the marker's position in the JSONL log, replaying the
/// same log always yields the same trimmed transcript. `n == 0` is a no-op.
fn drop_last_n_user_turns(messages: &mut Vec<Message>, n: u32) -> u32 {
    if n == 0 {
        return 0;
    }
    // Ordered, distinct thread_ids rooted by a `User` message.
    let mut user_threads: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for msg in messages.iter() {
        if matches!(msg.role, MessageRole::User) {
            if let Some(tid) = msg.thread_id.clone() {
                if seen.insert(tid.clone()) {
                    user_threads.push(tid);
                }
            }
        }
    }
    let drop_count = (n as usize).min(user_threads.len());
    if drop_count == 0 {
        return 0;
    }
    let to_drop: HashSet<String> = user_threads
        .split_off(user_threads.len() - drop_count)
        .into_iter()
        .collect();
    messages.retain(|msg| match msg.thread_id.as_deref() {
        Some(tid) => !to_drop.contains(tid),
        None => true,
    });
    drop_count as u32
}

/// Assemble the ordered `Message` list from a session JSONL's post-meta lines,
/// applying any interleaved [`SessionControlRecord`] at its position in the
/// log. Single-file convenience over [`parse_session_timeline`] +
/// [`fold_session_timeline`].
fn assemble_session_messages<'a>(lines: impl Iterator<Item = &'a str>) -> Vec<Message> {
    fold_session_timeline(parse_session_timeline(lines))
}

fn record_session_persist(outcome: &'static str) {
    counter!(
        "octos_session_persist_total",
        "outcome" => outcome.to_string()
    )
    .increment(1);
}

fn record_session_rewrite(outcome: &'static str) {
    counter!(
        "octos_session_rewrite_total",
        "outcome" => outcome.to_string()
    )
    .increment(1);
}

/// Structured terminal outcome for a child session.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChildSessionTerminalState {
    Completed,
    RetryableFailure,
    TerminalFailure,
}

/// Whether the child session terminal contract was joined back to a parent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChildSessionJoinState {
    Joined,
    Orphaned,
}

/// Explicit failure policy for terminal child-session outcomes.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ChildSessionFailureAction {
    Retry,
    Escalate,
}

/// Durable child-session contract persisted alongside the session history.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChildSessionContract {
    pub task_id: String,
    pub task_label: String,
    pub parent_session_key: String,
    pub child_session_key: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workflow_kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub current_phase: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terminal_state: Option<ChildSessionTerminalState>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub join_state: Option<ChildSessionJoinState>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub joined_at: Option<DateTime<Utc>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failure_action: Option<ChildSessionFailureAction>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub output_files: Vec<String>,
}

fn merge_child_contracts(
    flat: Vec<ChildSessionContract>,
    per_user: Vec<ChildSessionContract>,
) -> Vec<ChildSessionContract> {
    let mut merged = flat;
    for contract in per_user {
        if let Some(existing) = merged
            .iter_mut()
            .find(|existing| existing.task_id == contract.task_id)
        {
            Session::merge_child_contract(contract, existing);
        } else {
            merged.push(contract);
        }
    }
    merged
}

/// Metadata stored as the first line of each JSONL session file.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct SessionMeta {
    /// Schema version for forward-compatible deserialization.
    #[serde(default = "default_session_schema")]
    schema_version: u32,
    session_key: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    parent_key: Option<String>,
    /// Topic name for multi-session support (e.g. "research", "code").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    topic: Option<String>,
    /// Short summary of the session (first user message, truncated).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    summary: Option<String>,
    /// Display title for sidebar/listings. Auto-derived from first user
    /// message; preserved if set manually via [`SessionManager::update_title`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    title: Option<String>,
    /// Whether `title` was set manually (preserved across auto-derivation).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    title_manual: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    child_contracts: Vec<ChildSessionContract>,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}

/// A conversation session with message history.
#[derive(Debug, Clone)]
pub struct Session {
    pub key: SessionKey,
    /// Parent session key if this session was forked.
    pub parent_key: Option<SessionKey>,
    /// Topic name for multi-session support.
    pub topic: Option<String>,
    /// Short summary of the session content.
    pub summary: Option<String>,
    /// Display title (auto-derived from the first user message; a
    /// `title_manual` flag carried in persisted metadata preserves it across
    /// new messages).
    pub title: Option<String>,
    /// True if title was set manually and should not be overwritten by
    /// auto-derivation.
    pub title_manual: bool,
    /// Durable child-session contracts associated with this session.
    pub child_contracts: Vec<ChildSessionContract>,
    pub messages: Vec<Message>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl Session {
    fn new(key: SessionKey) -> Self {
        let now = Utc::now();
        let topic = key.topic().map(|t| t.to_string());
        Self {
            key,
            parent_key: None,
            topic,
            summary: None,
            title: None,
            title_manual: false,
            child_contracts: vec![],
            messages: vec![],
            created_at: now,
            updated_at: now,
        }
    }

    /// Get the most recent N messages from history.
    pub fn get_history(&self, max: usize) -> &[Message] {
        let len = self.messages.len();
        if len <= max {
            &self.messages
        } else {
            &self.messages[len - max..]
        }
    }

    fn merge_child_contract(update: ChildSessionContract, existing: &mut ChildSessionContract) {
        existing.task_label = update.task_label;
        existing.parent_session_key = update.parent_session_key;
        existing.child_session_key = update.child_session_key;
        if update.workflow_kind.is_some() {
            existing.workflow_kind = update.workflow_kind;
        }
        if update.current_phase.is_some() {
            existing.current_phase = update.current_phase;
        }
        if update.terminal_state.is_some() {
            existing.terminal_state = update.terminal_state;
        }
        if update.join_state.is_some() {
            existing.join_state = update.join_state;
        }
        if update.joined_at.is_some() {
            existing.joined_at = update.joined_at;
        }
        if update.failure_action.is_some() {
            existing.failure_action = update.failure_action;
        }
        if update.error.is_some() {
            existing.error = update.error;
        }
        if !update.output_files.is_empty() {
            existing.output_files = update.output_files;
        }
    }
}

/// Default maximum number of sessions kept in memory.
const DEFAULT_MAX_SESSIONS: usize = 1000;

/// Maximum session file size we'll load (10 MB). Prevents OOM on corrupted/adversarial files.
const MAX_SESSION_FILE_SIZE: u64 = 10 * 1024 * 1024;

pub struct SessionManager {
    sessions_dir: PathBuf,
    cache: LruCache<String, Session>,
}

impl SessionManager {
    pub fn open(data_dir: &Path) -> Result<Self> {
        let sessions_dir = data_dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir)?;
        Ok(Self {
            sessions_dir,
            cache: LruCache::new(NonZeroUsize::new(DEFAULT_MAX_SESSIONS).expect("default > 0")),
        })
    }

    /// True for runtime-internal topics that should never appear in the
    /// user-facing session listing (`child-*` spawn fanouts and `*.tasks`
    /// background-task ledger sidecars).
    /// Load a session from disk (read-only). Returns None if not found.
    pub async fn load(&self, key: &SessionKey) -> Option<Session> {
        self.load_from_disk(key).await
    }

    /// Get or create a session. Loads from disk on first access.
    pub async fn get_or_create(&mut self, key: &SessionKey) -> &mut Session {
        let key_str = key.0.clone();
        let disk_session = if self.cache.contains(&key_str) {
            None
        } else {
            Some(
                self.load_from_disk(key)
                    .await
                    .unwrap_or_else(|| Session::new(key.clone())),
            )
        };
        self.cache.get_or_insert_mut(key_str, || {
            disk_session.unwrap_or_else(|| Session::new(key.clone()))
        })
    }

    /// Check whether a session is known to this manager. Returns `true`
    /// when the session is either resident in the LRU cache or has a
    /// JSONL file on disk under the configured `sessions_dir`. Does NOT
    /// create the session if missing — used by handlers that need to
    /// reject `unknown_session` requests per UPCR-2026-009 / -010 / -011.
    ///
    /// Checks BOTH the legacy flat layout (`<sessions_dir>/<key>.jsonl`)
    /// AND the canonical per-user layout (`<data_dir>/users/<base>/sessions/<topic>.jsonl`)
    /// to mirror `load_from_disk`'s discovery rules. Without this dual check,
    /// after LRU eviction or daemon restart, sessions persisted via
    /// `ApiChannel::persist_to_session` (which uses the canonical per-user
    /// path) would be reported as unknown — causing UPCR-2026-009 / -010 /
    /// -011 handlers to reject valid sessions.
    pub fn session_known(&mut self, key: &SessionKey) -> bool {
        let key_str = key.0.clone();
        if self.cache.contains(&key_str) {
            return true;
        }
        let flat_path = self.session_path(key);
        if flat_path.exists() {
            return true;
        }
        // Mirror load_from_disk's per-user fallback (this fn @ ~line 1079).
        let base_key = key.base_key();
        let encoded_base = encode_path_component(base_key);
        let topic = key.topic().unwrap_or("default");
        let encoded_topic = encode_path_component(topic);
        let users_dir = self
            .sessions_dir
            .parent()
            .unwrap_or(&self.sessions_dir)
            .join("users");
        let per_user_path = users_dir
            .join(&encoded_base)
            .join("sessions")
            .join(format!("{encoded_topic}.jsonl"));
        per_user_path.exists()
    }

    /// Add a message to a session and persist it.
    pub async fn add_message(&mut self, key: &SessionKey, message: Message) -> Result<()> {
        self.add_message_with_seq(key, message).await.map(|_| ())
    }

    /// Add a message to a session, persist it, and return its committed sequence.
    ///
    /// Serialises on the per-key persist lock (see [`persist_lock_for`]) so a
    /// concurrent writer on the same key cannot interleave between the disk
    /// append and the seq derivation. The lock is NOT reentrant: a caller
    /// that already holds it must use
    /// [`Self::add_message_with_seq_unlocked`] instead.
    pub async fn add_message_with_seq(
        &mut self,
        key: &SessionKey,
        message: Message,
    ) -> Result<usize> {
        let lock = persist_lock_for(key);
        let _guard = lock.lock().await;
        self.add_message_with_seq_unlocked(key, message).await
    }

    /// Append + seq derivation without taking the persist lock — the caller
    /// must already hold it (see [`persist_lock_for`]).
    ///
    /// The committed seq is derived from this manager's in-memory mirror:
    /// the manager's transcript is the MERGED flat + per-user view assembled
    /// at load time (see [`Self::load_from_disk`]), so the merged mirror —
    /// not a single file's row count — is the closest authority the manager
    /// has. The lock still guarantees the append and the len-read are atomic
    /// against every other locked writer (canonical appends, rewrites,
    /// rollback markers) on the same key.
    async fn add_message_with_seq_unlocked(
        &mut self,
        key: &SessionKey,
        mut message: Message,
    ) -> Result<usize> {
        // Auto-derive title from first user message before persistence so the
        // first append_to_disk includes the title in the JSONL meta line.
        // Manual titles (flagged `title_manual` in persisted metadata) are preserved.
        if matches!(message.role, MessageRole::User) {
            let session = self.get_or_create(key).await;
            if !session.title_manual && session.title.is_none() {
                let derived = derive_title_from_message(&message.content);
                if !derived.is_empty() {
                    session.title = Some(derived);
                }
            }
        }

        // Stamp `thread_id` on the inbound message before the disk write so
        // the persisted JSONL line and the in-memory mirror agree (M8.10
        // PR #1). Caller-supplied `thread_id` wins — covers replay paths
        // and tests that pre-fill the field deliberately.
        //
        // PR F (M8.10): the new-write derivation is fail-closed for
        // Assistant/Tool roles. Callers MUST pre-stamp before calling this;
        // the previous "derive from history" fallback picked the wrong
        // sibling user under concurrent rapid-fire turns. Failure here
        // surfaces a structural caller bug rather than silently shipping
        // a mis-routed thread.
        if message.thread_id.is_none() {
            let session = self.get_or_create(key).await;
            match derive_thread_id_for_new_write(&message, &session.messages) {
                Ok(tid) => message.thread_id = tid,
                Err(error) => {
                    record_session_persist("rejected_unbound_assistant");
                    return Err(error);
                }
            }
        }

        let _ = self.get_or_create(key).await;
        if let Err(error) = self.append_to_disk(key, &message).await {
            record_session_persist("failed");
            return Err(error);
        }
        let observer_root = self.data_dir();
        let session = self.get_or_create(key).await;
        session.messages.push(message);
        session.updated_at = Utc::now();
        record_session_persist("committed");
        let committed_seq = session.messages.len().saturating_sub(1);
        // UPCR-2026-012: post-commit observer fan-out. Fires AFTER the
        // append_to_disk above succeeded and the in-memory mirror is
        // updated, so a `message/persisted` notification reflects a row
        // the OS has accepted (the append write returned; per-append
        // fsync is deliberately not done). A failed disk write returns
        // above before this point, so the observer never sees a row
        // that did not commit.
        if let Some(committed) = session.messages.last() {
            notify_message_commit(&observer_root, key, committed, committed_seq);
        }
        Ok(committed_seq)
    }

    /// Get the JSONL file path for a session key.
    ///
    /// Uses byte-level percent-encoding for non-safe characters to ensure
    /// different keys always produce different filenames. Operating on raw
    /// UTF-8 bytes (not Unicode codepoints) makes this immune to normalization
    /// collisions on filesystems like APFS/HFS+.
    ///
    /// Truncates encoded name to 200 chars to stay within the 255-byte
    /// filesystem filename limit (reserving space for ".jsonl" suffix).
    pub fn session_path(&self, key: &SessionKey) -> PathBuf {
        Self::session_path_static(&self.sessions_dir, key)
    }

    /// Return the data directory (parent of sessions_dir).
    pub fn data_dir(&self) -> PathBuf {
        self.sessions_dir
            .parent()
            .unwrap_or(&self.sessions_dir)
            .to_path_buf()
    }

    /// Static version of `session_path` — used by `SessionHandle` too.
    pub(crate) fn session_path_static(sessions_dir: &Path, key: &SessionKey) -> PathBuf {
        // Max encoded name length: 200 chars + ".jsonl" (6) = 206, well within 255.
        // When truncation occurs, append a hash suffix to avoid collisions between
        // keys that differ only past the truncation point.
        const HASH_SUFFIX_LEN: usize = 17; // "_{hash:016X}"
        const MAX_NAME_LEN: usize = 200 - HASH_SUFFIX_LEN;
        let mut safe_name = String::new();
        let mut truncated = false;
        for byte in key.0.as_bytes() {
            if safe_name.len() >= MAX_NAME_LEN {
                truncated = true;
                break;
            }
            if byte.is_ascii_alphanumeric() || *byte == b'-' || *byte == b'_' {
                safe_name.push(*byte as char);
            } else {
                // Percent-encode each byte: ':' -> '%3A', non-ASCII -> '%XX' per byte
                safe_name.push_str(&format!("%{byte:02X}"));
            }
        }
        if truncated {
            // Append 16-char hex hash of full key to prevent collisions.
            // Uses FNV-1a (stable across Rust versions) instead of DefaultHasher
            // (which wraps SipHash and is NOT guaranteed stable across toolchain upgrades).
            let hash = fnv1a_64(key.0.as_bytes());
            safe_name.push_str(&format!("_{hash:016X}"));
        }
        sessions_dir.join(format!("{safe_name}.jsonl"))
    }

    /// Load a session from its JSONL file.
    ///
    /// Checks the legacy flat layout first, then the per-user directory layout.
    /// Uses spawn_blocking to avoid blocking the async runtime.
    async fn load_from_disk(&self, key: &SessionKey) -> Option<Session> {
        let flat_path = self.session_path(key);
        let base_key = key.base_key();
        let encoded_base = encode_path_component(base_key);
        let topic = key.topic().unwrap_or("default");
        let encoded_topic = encode_path_component(topic);
        let users_dir = self
            .sessions_dir
            .parent()
            .unwrap_or(&self.sessions_dir)
            .join("users");
        let per_user_path = users_dir
            .join(&encoded_base)
            .join("sessions")
            .join(format!("{encoded_topic}.jsonl"));

        if !flat_path.exists() && !per_user_path.exists() {
            return None;
        }

        let key_clone = key.clone();
        tokio::task::spawn_blocking(move || {
            // Parse a session file into its meta + an un-applied timeline
            // (messages interleaved with rollback markers). The rollback drop
            // is DEFERRED — the flat + per-user merge below applies markers
            // against the COMBINED transcript, so a marker co-located with one
            // layout still trims turns that live in the other (codex P1: a
            // mixed flat/per-user layout must not resurrect rolled-back turns).
            fn parse_session_file(
                path: &Path,
                key: &SessionKey,
            ) -> Option<(SessionMeta, Vec<SessionTimelineItem>)> {
                // Guard against oversized files to prevent OOM
                if let Ok(file_meta) = std::fs::metadata(path) {
                    if file_meta.len() > MAX_SESSION_FILE_SIZE {
                        warn!(
                            key = %key,
                            path = %path.display(),
                            size = file_meta.len(),
                            limit = MAX_SESSION_FILE_SIZE,
                            "session file too large, skipping"
                        );
                        return None;
                    }
                }

                let content = std::fs::read_to_string(path).ok()?;
                let mut lines = content.lines();

                let meta_line = lines.next()?;
                let meta: SessionMeta = serde_json::from_str(meta_line).ok()?;

                if meta.schema_version > CURRENT_SESSION_SCHEMA {
                    warn!(
                        key = %key,
                        path = %path.display(),
                        file_version = meta.schema_version,
                        current_version = CURRENT_SESSION_SCHEMA,
                        "session file has newer schema version, skipping"
                    );
                    return None;
                }

                Some((meta, parse_session_timeline(lines)))
            }

            // Fold a single file's timeline into a `Session` (per-user only or
            // legacy flat only — the marker application is unambiguous).
            fn session_from(
                meta: SessionMeta,
                messages: Vec<Message>,
                key: &SessionKey,
            ) -> Session {
                Session {
                    key: key.clone(),
                    parent_key: meta.parent_key.map(SessionKey),
                    topic: meta.topic,
                    summary: meta.summary,
                    title: meta.title,
                    title_manual: meta.title_manual,
                    child_contracts: meta.child_contracts,
                    messages,
                    created_at: meta.created_at,
                    updated_at: meta.updated_at,
                }
            }

            let flat = flat_path
                .exists()
                .then(|| parse_session_file(&flat_path, &key_clone))
                .flatten();
            let per_user = per_user_path
                .exists()
                .then(|| parse_session_file(&per_user_path, &key_clone))
                .flatten();

            let merged = match (flat, per_user) {
                (Some((flat_meta, flat_timeline)), Some((per_user_meta, per_user_timeline))) => {
                    // Merge the two timelines: dedup messages by fingerprint (a
                    // message migrated into both layouts appears once), keep
                    // EVERY rollback marker, then order by timestamp so each
                    // marker lands after the messages it should trim
                    // (message-before-marker on an exact tie). Folding the
                    // combined, ordered timeline applies the drop across BOTH
                    // files — the fix for the mixed-layout resurrection.
                    let mut items: Vec<SessionTimelineItem> = Vec::with_capacity(
                        flat_timeline.len().saturating_add(per_user_timeline.len()),
                    );
                    let mut seen: std::collections::HashMap<String, usize> =
                        std::collections::HashMap::new();

                    for item in flat_timeline.into_iter().chain(per_user_timeline) {
                        match item {
                            SessionTimelineItem::Rollback { .. } => items.push(item),
                            SessionTimelineItem::Message(message) => {
                                // The dedup fingerprint must be synthesis-INDEPENDENT: a
                                // legacy flat row (no persisted `thread_id`) and its
                                // migrated per-user copy (a `thread_id` synthesized +
                                // persisted by an earlier migration, possibly a
                                // migration-added `client_message_id`) are the SAME
                                // logical message. `thread_id` is only synthesized later
                                // in `fold_session_timeline`, and neither field is content
                                // identity, so normalize both to `None` before
                                // fingerprinting — else the two copies fingerprint
                                // differently, both survive, and a partial-migration
                                // (stale flat + per-user) session doubles on reload.
                                let mut ident = (*message).clone();
                                ident.thread_id = None;
                                ident.client_message_id = None;
                                let Ok(fingerprint) = serde_json::to_string(&ident) else {
                                    continue;
                                };
                                // On a cross-layout dup, keep the RICHER copy: the
                                // migrated per-user row carries the canonical
                                // thread_id/client_message_id the legacy flat row lacks,
                                // so hydrate/thread projections resolve the real IDs on
                                // reload rather than the flat row's missing/synthesized
                                // ones (flat is processed first, so without this the
                                // stale row would win).
                                let richness = message.thread_id.is_some() as u8
                                    + message.client_message_id.is_some() as u8;
                                match seen.get(&fingerprint).copied() {
                                    None => {
                                        seen.insert(fingerprint, items.len());
                                        items.push(SessionTimelineItem::Message(message));
                                    }
                                    Some(idx) => {
                                        let kept = match &items[idx] {
                                            SessionTimelineItem::Message(existing) => {
                                                existing.thread_id.is_some() as u8
                                                    + existing.client_message_id.is_some() as u8
                                            }
                                            SessionTimelineItem::Rollback { .. } => u8::MAX,
                                        };
                                        if richness > kept {
                                            items[idx] = SessionTimelineItem::Message(message);
                                        }
                                    }
                                }
                            }
                        }
                    }
                    // Stable sort: preserves flat-before-per-user order for
                    // messages sharing a timestamp (matches the prior merge's
                    // `sort_by_key(|m| m.timestamp)` stability).
                    items.sort_by_key(|item| match item {
                        SessionTimelineItem::Message(message) => (message.timestamp, 0u8),
                        SessionTimelineItem::Rollback { at, .. } => (*at, 1u8),
                    });
                    let messages = fold_session_timeline(items);

                    Session {
                        key: key_clone.clone(),
                        parent_key: per_user_meta
                            .parent_key
                            .or(flat_meta.parent_key)
                            .map(SessionKey),
                        topic: per_user_meta.topic.or(flat_meta.topic),
                        summary: per_user_meta.summary.or(flat_meta.summary),
                        title: per_user_meta.title.or(flat_meta.title),
                        title_manual: per_user_meta.title_manual || flat_meta.title_manual,
                        child_contracts: merge_child_contracts(
                            flat_meta.child_contracts,
                            per_user_meta.child_contracts,
                        ),
                        messages,
                        created_at: flat_meta.created_at.min(per_user_meta.created_at),
                        updated_at: flat_meta.updated_at.max(per_user_meta.updated_at),
                    }
                }
                (Some((meta, timeline)), None) | (None, Some((meta, timeline))) => {
                    session_from(meta, fold_session_timeline(timeline), &key_clone)
                }
                (None, None) => return None,
            };

            debug!(
                key = %key_clone,
                messages = merged.messages.len(),
                flat_exists = flat_path.exists(),
                per_user_exists = per_user_path.exists(),
                "Loaded session from disk"
            );

            Some(merged)
        })
        .await
        .ok()
        .flatten()
    }

    /// Append a message to the JSONL file. Creates the file with metadata if new.
    /// Uses spawn_blocking to avoid blocking the async runtime.
    async fn append_to_disk(&self, key: &SessionKey, message: &Message) -> Result<()> {
        let path = self.session_path(key);

        // Prepare metadata outside spawn_blocking (needs cache access)
        let session_peek = self.cache.peek(&key.0);
        let parent_key = session_peek.and_then(|s| s.parent_key.as_ref().map(|k| k.0.clone()));
        let topic = session_peek.and_then(|s| s.topic.clone());
        let summary = session_peek.and_then(|s| s.summary.clone());
        let title = session_peek.and_then(|s| s.title.clone());
        let title_manual = session_peek.map(|s| s.title_manual).unwrap_or(false);
        let child_contracts = session_peek
            .map(|session| session.child_contracts.clone())
            .unwrap_or_default();
        let key_str = key.0.clone();
        let msg_json = serde_json::to_string(message)?;

        tokio::task::spawn_blocking(move || {
            use std::io::Write;

            let mut file = std::fs::OpenOptions::new()
                .read(true)
                .create(true)
                .append(true)
                .open(&path)?;

            // Check file size after open to avoid TOCTOU race with exists() check
            let file_len = file.metadata()?.len();
            let is_new = file_len == 0;

            // Refuse to append if the file is already at the size limit.
            // The session should be compacted before it reaches this point.
            //
            // Per UPCR-2026-012: the durable-commit observer fires only
            // when this function returns Ok; previously a silent
            // `Ok(())` here would have leaked an observer notification
            // for a row that never reached disk. Return an error so the
            // caller path (`add_message_with_seq`) propagates the
            // failure and the observer is skipped.
            if !is_new && file_len >= MAX_SESSION_FILE_SIZE {
                warn!(
                    key = key_str,
                    size = file_len,
                    limit = MAX_SESSION_FILE_SIZE,
                    "session file at size limit, skipping append"
                );
                return Err(eyre::eyre!(
                    "session file at size limit ({} >= {}), refusing append",
                    file_len,
                    MAX_SESSION_FILE_SIZE
                ));
            }

            if is_new {
                let meta = SessionMeta {
                    schema_version: CURRENT_SESSION_SCHEMA,
                    session_key: key_str,
                    parent_key,
                    topic,
                    summary,
                    title,
                    title_manual,
                    child_contracts,
                    created_at: Utc::now(),
                    updated_at: Utc::now(),
                };
                writeln!(file, "{}", serde_json::to_string(&meta)?)?;
            } else {
                seal_torn_tail(&mut file, file_len)?;
            }

            writeln!(file, "{msg_json}")?;
            Ok::<_, eyre::Report>(())
        })
        .await
        .map_err(|e| eyre::eyre!("spawn_blocking join error: {e}"))??;

        Ok(())
    }

    /// Rewrite a session's JSONL file from the in-memory state.
    /// Uses atomic write-then-rename to avoid corruption on crash; the
    /// temp file is fsynced before the rename and the parent directory
    /// after it, so the rewrite is durable, not just atomic.
    /// Uses spawn_blocking to avoid blocking the async runtime.
    ///
    /// Serialises on the per-key persist lock so the whole-file rewrite
    /// cannot interleave with (and erase) a canonical locked append or a
    /// concurrent `SessionHandle` rewrite of the same key.
    /// The directory session files persist under — the on-disk
    /// collision domain for fork child keys (serve scopes its fork
    /// reservations by it; two managers over the same dir CAN collide,
    /// two different profiles' dirs cannot).
    pub fn sessions_dir(&self) -> &std::path::Path {
        &self.sessions_dir
    }

    pub async fn rewrite(&self, key: &SessionKey) -> Result<()> {
        let lock = persist_lock_for(key);
        let _guard = lock.lock().await;
        let session = self
            .cache
            .peek(&key.0)
            .ok_or_else(|| eyre::eyre!("session not in cache: {}", key))?;

        // Build the full content string synchronously (no I/O)
        let meta = SessionMeta {
            schema_version: CURRENT_SESSION_SCHEMA,
            session_key: key.0.clone(),
            parent_key: session.parent_key.as_ref().map(|k| k.0.clone()),
            topic: session.topic.clone(),
            summary: session.summary.clone(),
            title: session.title.clone(),
            title_manual: session.title_manual,
            child_contracts: session.child_contracts.clone(),
            created_at: session.created_at,
            updated_at: session.updated_at,
        };
        let mut content = serde_json::to_string(&meta)?;
        content.push('\n');
        for msg in &session.messages {
            content.push_str(&serde_json::to_string(msg)?);
            content.push('\n');
        }

        let msg_count = session.messages.len();
        let path = self.session_path(key);
        let key_display = key.to_string();

        let rewrite_result = tokio::task::spawn_blocking(move || {
            use std::io::Write;
            let tmp_path = rewrite_tmp_path(&path);
            let write_result = (|| -> Result<(), eyre::Report> {
                let mut file = std::fs::File::create(&tmp_path)?;
                file.write_all(content.as_bytes())?;
                // `flush()` on a std File is a no-op (no userspace buffer) —
                // `sync_all` is what actually gets the bytes to stable
                // storage before the rename swaps the inode.
                file.sync_all()?;
                // Atomic rename (on same filesystem)
                std::fs::rename(&tmp_path, &path)?;
                if let Some(dir) = path.parent() {
                    fsync_dir(dir);
                }
                Ok(())
            })();
            if write_result.is_err() {
                // Best-effort tmp cleanup, same as rewrite_blocking_inner.
                let _ = std::fs::remove_file(&tmp_path);
            }
            write_result
        })
        .await
        .map_err(|e| eyre::eyre!("spawn_blocking join error: {e}"))?;
        if let Err(error) = rewrite_result {
            record_session_rewrite("failed");
            return Err(error);
        }

        debug!(key = %key_display, messages = msg_count, "Rewrote session to disk");
        record_session_rewrite("committed");
        Ok(())
    }

    /// Fork a session: create a new session that copies the last N messages from the parent.
    ///
    /// The new session's channel is taken from the parent key; `new_chat_id` becomes the chat ID.
    /// Returns the new session's key.
    pub async fn fork(
        &mut self,
        parent_key: &SessionKey,
        new_chat_id: &str,
        copy_messages: usize,
    ) -> Result<SessionKey> {
        let parent = self.get_or_create(parent_key).await;
        let messages: Vec<Message> = parent.get_history(copy_messages).to_vec();
        // Shared derivation rule (SessionKey::fork_child): profile +
        // channel preserved, raw SPA parents yield raw children.
        let new_key = parent_key.fork_child(new_chat_id);

        let now = Utc::now();
        let session = Session {
            key: new_key.clone(),
            parent_key: Some(parent_key.clone()),
            topic: None,
            summary: None,
            title: None,
            title_manual: false,
            child_contracts: vec![],
            messages,
            created_at: now,
            updated_at: now,
        };
        self.cache.put(new_key.0.clone(), session);
        if let Err(error) = self.rewrite(&new_key).await {
            // A failed write must not leave a cache-resident ghost:
            // `session_known` would keep answering true for a child
            // that was never durably created, wedging retries on
            // `child_exists` (codex #1613 P2).
            self.cache.pop(&new_key.0);
            return Err(error);
        }

        debug!(
            parent = %parent_key,
            child = %new_key,
            copied = copy_messages,
            "Forked session"
        );
        Ok(new_key)
    }

    /// Clear a session's chat history (both in-memory and on disk).
    ///
    /// Removes session data from:
    /// 1. In-memory LRU cache
    /// 2. Flat layout JSONL (`sessions/{encoded_key}.jsonl`)
    /// 3. Per-user layout JSONL (`users/{encoded_base}/sessions/{topic}.jsonl`)
    ///
    /// Does NOT remove the user workspace directory — workspace data (slides,
    /// git repos, artifacts) has a separate lifecycle from chat history.
    pub async fn clear(&mut self, key: &SessionKey) -> Result<()> {
        self.cache.pop(&key.0);

        // 1. Flat layout JSONL
        let flat_path = self.session_path(key);
        if flat_path.exists() {
            tokio::fs::remove_file(&flat_path).await?;
        }

        // 2. Per-user layout JSONL
        let base_key = key.base_key();
        let encoded_base = encode_path_component(base_key);
        let users_dir = self
            .sessions_dir
            .parent()
            .unwrap_or(&self.sessions_dir)
            .join("users");
        let user_dir = users_dir.join(&encoded_base);

        if user_dir.exists() {
            let topic = key.topic().unwrap_or("default");
            let encoded_topic = encode_path_component(topic);
            let per_user_path = user_dir
                .join("sessions")
                .join(format!("{encoded_topic}.jsonl"));
            if per_user_path.exists() {
                if let Err(e) = tokio::fs::remove_file(&per_user_path).await {
                    warn!(
                        key = %key,
                        path = %per_user_path.display(),
                        error = %e,
                        "failed to delete per-user session file"
                    );
                }
            }
        }

        Ok(())
    }

    /// Drop the cached in-memory copy of a session so the next read consults
    /// disk. Required by callers that write through alternate channels (e.g.
    /// `SessionHandle`) and must keep the manager's LRU cache from serving
    /// stale post-write reads.
    pub fn invalidate_cache(&mut self, key: &SessionKey) {
        self.cache.pop(&key.0);
    }

    /// Roll back the last `num_turns` user turns for `key` (conversation-only
    /// rewind): append an idempotent rollback marker to the session JSONL and
    /// trim the in-memory transcript to match. Returns the number of user turns
    /// actually dropped, clamped to the session's turn count.
    ///
    /// The marker is appended to the same on-disk file that holds the session's
    /// messages (per-user layout preferred, legacy flat as fallback), so both
    /// [`Self::load_from_disk`] and the per-user `SessionHandle` load path
    /// replay the drop at the marker's log position on the next load. This is
    /// what keeps the trim durable without truncating history. `num_turns == 0`
    /// is a defensive no-op (the handler rejects it as `invalid_params`).
    ///
    /// Conversation-only: no git, worktree, or workspace file state is touched.
    pub async fn rollback_last_n_user_turns(
        &mut self,
        key: &SessionKey,
        num_turns: u32,
    ) -> Result<u32> {
        // Ensure the session is resident so the trim reflects the full
        // (merged) on-disk transcript.
        let _ = self.get_or_create(key).await;
        // Serialise the count-read + marker-append + trim under the per-key
        // persist lock — the SAME lock the rewrite / canonical-append paths
        // hold (#1528). Without it this read-then-append raced a concurrent
        // `rewrite`, which snapshots the pre-marker message vec and renames
        // over the file, silently ERASING the appended rollback marker; and a
        // concurrent append could land between the turn-count read and the
        // marker write, computing the marker's `num_turns` against a stale
        // transcript. `append_rollback_marker` takes no lock itself, so
        // holding it here does not re-enter.
        let lock = persist_lock_for(key);
        let _guard = lock.lock().await;
        let dropped = {
            let session = self
                .cache
                .peek(&key.0)
                .ok_or_else(|| eyre::eyre!("session not in cache after get_or_create: {key}"))?;
            num_turns.min(count_user_turns(&session.messages))
        };
        if dropped == 0 {
            return Ok(0);
        }
        // Durable first: append the append-only marker to disk so a crash
        // between here and the in-memory trim still replays identically on the
        // next load.
        self.append_rollback_marker(key, num_turns).await?;
        // Trim the in-memory mirror to match the persisted state so the next
        // turn continues from the trimmed transcript.
        if let Some(session) = self.cache.get_mut(&key.0) {
            drop_last_n_user_turns(&mut session.messages, num_turns);
            session.updated_at = Utc::now();
        }
        Ok(dropped)
    }

    /// Append a rollback control line to the on-disk JSONL for `key`. Chooses
    /// the per-user layout file when present, else the legacy flat file; when
    /// neither exists there is nothing on disk to trim on reload, so the append
    /// is skipped (the in-memory trim is authoritative for a cache-only
    /// session).
    async fn append_rollback_marker(&self, key: &SessionKey, num_turns: u32) -> Result<()> {
        let flat_path = self.session_path(key);
        let base_key = key.base_key();
        let encoded_base = encode_path_component(base_key);
        let topic = key.topic().unwrap_or("default");
        let encoded_topic = encode_path_component(topic);
        let per_user_path = self
            .sessions_dir
            .parent()
            .unwrap_or(&self.sessions_dir)
            .join("users")
            .join(&encoded_base)
            .join("sessions")
            .join(format!("{encoded_topic}.jsonl"));

        // Write the marker to the canonical layout (per-user preferred, legacy
        // flat as fallback). `load_from_disk` folds markers AFTER merging both
        // layouts (by timestamp), so the drop trims the combined transcript
        // even when the rolled-back turns live in the OTHER file — a
        // single-file `SessionHandle::open` still applies it in log order.
        let target = if per_user_path.exists() {
            per_user_path
        } else if flat_path.exists() {
            flat_path
        } else {
            return Ok(());
        };

        let line = rollback_marker_line(num_turns)?;
        tokio::task::spawn_blocking(move || {
            use std::io::Write;
            let mut file = std::fs::OpenOptions::new()
                .read(true)
                .append(true)
                .open(&target)?;
            let file_len = file.metadata()?.len();
            // The marker is what makes `/undo` survive a reload — if it
            // fused with a torn tail the rollback would silently revert
            // on the next load.
            seal_torn_tail(&mut file, file_len)?;
            writeln!(file, "{line}")?;
            Ok::<_, eyre::Report>(())
        })
        .await
        .map_err(|e| eyre::eyre!("spawn_blocking join error: {e}"))??;
        Ok(())
    }
}

// ── SessionHandle ──────────────────────────────────────────────────────────

/// Per-session file handle — owns one session's in-memory state and I/O.
///
/// Used by `SessionActor` to eliminate the shared `SessionManager` mutex.
/// Each actor gets its own `SessionHandle`, so there is zero cross-session
/// lock contention.
///
/// File layout (per-user directory structure):
/// ```text
/// {data_dir}/users/{encoded_base_key}/sessions/{topic_or_default}.jsonl
/// ```
/// This enables future filesystem-level isolation (quotas, chroot, sandboxing).
pub struct SessionHandle {
    sessions_dir: PathBuf,
    session: Session,
    observer_root: PathBuf,
}

/// Per-key persist lock map.
///
/// Two writers (e.g. `SessionActor` and `ApiChannel::persist_to_session`) can
/// each open a fresh `SessionHandle` for the same session_key concurrently.
/// Each handle loads disk into its OWN per-instance `messages: Vec<_>`.
/// Without serialisation, both observe `len = N`, both append, both return
/// `seq = N` — duplicate seqs that break watcher correlation.
///
/// This map gives `persist_message_through_canonical_path` — and every other
/// session write path: `SessionManager::add_message_with_seq`,
/// `SessionHandle::add_message_with_seq`, the `rewrite`s,
/// `rollback_last_n_user_turns`, and the child-contract upserts — a per-key
/// Tokio mutex so all writes for the same `SessionKey.0` serialise. The mutex
/// is scoped to the session_key string (NOT the file path) so callers reaching
/// the canonical per-user JSONL via different code paths still contend on
/// the same lock.
///
/// The mutex is NOT reentrant. Methods that need to run inside an
/// already-held lock use the `_unlocked` variants
/// (`add_message_with_seq_unlocked`, `rewrite_unlocked`,
/// `upsert_child_contract_unlocked`).
///
/// Memory note: entries leak forever, one per active session_key. In a long-
/// lived bus process this grows with active distinct sessions; given
/// production keys are typically `<profile>:api:<chat>` and bounded by user
/// count, this is acceptable. We can add LRU eviction later if needed.
fn persist_lock_for(key: &SessionKey) -> std::sync::Arc<tokio::sync::Mutex<()>> {
    use std::collections::HashMap;
    use std::sync::{Mutex, OnceLock};
    static MAP: OnceLock<Mutex<HashMap<String, std::sync::Arc<tokio::sync::Mutex<()>>>>> =
        OnceLock::new();
    let map = MAP.get_or_init(|| Mutex::new(HashMap::new()));
    let mut guard = map.lock().expect("persist lock map poisoned");
    guard
        .entry(key.0.clone())
        .or_insert_with(|| std::sync::Arc::new(tokio::sync::Mutex::new(())))
        .clone()
}

/// Persist a single message to the canonical per-user `<topic>.jsonl` file
/// the `SessionActor` and `ApiChannel` both target. Returns the committed
/// per-session sequence number.
///
/// Writes for the same `key` serialise via a per-key Tokio mutex (see
/// [`persist_lock_for`]). This is the contract that closed the concurrent-
/// persist seq race: every caller — `SessionActor::persist_assistant_message`,
/// `ApiChannel::persist_to_session`, and the standalone `octos serve` `/chat`
/// handlers — funnels through this helper so the storage layer is the single
/// ordering point. Callers that also keep an in-memory `SessionHandle` mirror
/// the message via [`SessionHandle::push_message_in_memory`] AFTER the disk
/// write commits, so their local Vec stays consistent without double-writing.
///
/// Preserves the canonical migration path (legacy flat → per-user) inside
/// `SessionHandle::open`.
pub async fn persist_message_through_canonical_path(
    data_dir: &Path,
    key: &SessionKey,
    message: Message,
) -> Result<usize> {
    let lock = persist_lock_for(key);
    let _guard = lock.lock().await;
    let mut handle = SessionHandle::open(data_dir, key);
    // `_unlocked`: this function already holds the per-key persist lock and
    // the tokio mutex is not reentrant — calling the locking
    // `add_message_with_seq` here would deadlock.
    handle.add_message_with_seq_unlocked(message).await
}

impl SessionHandle {
    /// Open or create a session handle for the given key.
    ///
    /// Uses per-user directory layout: `{data_dir}/users/{base_key}/sessions/{topic}.jsonl`.
    /// Falls back to the legacy flat layout for migration.
    pub fn open(data_dir: &Path, key: &SessionKey) -> Self {
        let base_key = key.base_key();
        let encoded_base = Self::encode_path_component(base_key);
        let user_sessions_dir = data_dir.join("users").join(&encoded_base).join("sessions");
        let _ = std::fs::create_dir_all(&user_sessions_dir);

        let topic_filename = Self::topic_filename(key);
        let new_path = user_sessions_dir.join(&topic_filename);
        let marker_path = Self::migration_marker_path(&user_sessions_dir, key);
        let legacy_dir = data_dir.join("sessions");
        let legacy_path = SessionManager::session_path_static(&legacy_dir, key);

        // Migration state machine — three real cases:
        //   (A) marker present              -> migration is done; per-user is
        //                                      authoritative. Skip legacy load
        //                                      AND skip legacy delete (a stale
        //                                      legacy file is left in place so
        //                                      operator-level cleanup can find
        //                                      it; the per-user merge in
        //                                      `list_user_sessions` would still
        //                                      dedup if it surfaces).
        //   (B) per-user + legacy + no marker
        //                                   -> previous boot's `rewrite_blocking`
        //                                      succeeded but `remove_file(legacy)`
        //                                      failed. Best-effort retry the
        //                                      removal; on success write the
        //                                      marker. On failure log and keep
        //                                      going (per-user already wins).
        //   (C) per-user only               -> normal read.
        //   (D) legacy only                 -> first-time migration: load,
        //                                      rewrite into per-user, remove
        //                                      legacy, write marker.
        //   (else)                          -> empty session.
        let session = if marker_path.exists() {
            // Case (A): marker says migration is done. The per-user file is
            // authoritative even if a stale legacy file co-exists.
            Self::load_from_file(&new_path, key)
        } else if new_path.exists() {
            if legacy_path.exists() {
                // Case (B): partial-migration leftover. Retry the legacy
                // removal so subsequent boots take the cheap (A) path.
                match std::fs::remove_file(&legacy_path) {
                    Ok(()) => {
                        let _ = std::fs::write(&marker_path, b"migrated-from-flat\n");
                    }
                    Err(error) => {
                        warn!(
                            key = %key,
                            legacy_path = %legacy_path.display(),
                            error = %error,
                            "failed to retry legacy session removal during open; \
                             per-user file remains authoritative"
                        );
                    }
                }
            }
            // Case (C): per-user only — straight read.
            Self::load_from_file(&new_path, key)
        } else if legacy_path.exists() {
            // Case (D): first-time migration. Persist into the per-user JSONL
            // BEFORE removing the legacy file so a subsequent incremental
            // `add_message_with_seq` (which only appends a single line) does
            // not silently drop the pre-migration messages.
            debug!(key = %key, "migrating session from legacy flat layout");
            let session = Self::load_from_file(&legacy_path, key);
            if let Some(loaded) = session.as_ref() {
                if let Err(error) = Self::rewrite_blocking(&new_path, loaded) {
                    warn!(
                        key = %key,
                        path = %new_path.display(),
                        error = %error,
                        "failed to materialize legacy session into per-user layout; \
                         leaving legacy file in place"
                    );
                    return Self {
                        sessions_dir: user_sessions_dir,
                        session: loaded.clone(),
                        observer_root: data_dir.to_owned(),
                    };
                }
                if std::fs::remove_file(&legacy_path).is_ok() {
                    let _ = std::fs::write(&marker_path, b"migrated-from-flat\n");
                }
            }
            session
        } else {
            None
        }
        .unwrap_or_else(|| Session::new(key.clone()));

        Self {
            sessions_dir: user_sessions_dir,
            session,
            observer_root: data_dir.to_owned(),
        }
    }

    /// Path of the per-key migration marker written after a successful
    /// rewrite + legacy-remove pair. Used by [`Self::open`] to detect a
    /// completed migration on subsequent opens (so a stale legacy file —
    /// e.g. from a remove_file failure on a prior boot — does not cause
    /// double-history reads).
    fn migration_marker_path(user_sessions_dir: &Path, key: &SessionKey) -> PathBuf {
        let topic = key.topic().unwrap_or("default");
        let encoded = encode_path_component(topic);
        user_sessions_dir.join(format!(".migrated.{encoded}"))
    }

    /// Encode a path component (base key) for safe directory names.
    fn encode_path_component(s: &str) -> String {
        encode_path_component(s)
    }

    /// Get the JSONL filename for a session key's topic.
    /// Default session → `default.jsonl`, topic → `{topic}.jsonl`.
    fn topic_filename(key: &SessionKey) -> String {
        let topic = key.topic().unwrap_or("default");
        let encoded = Self::encode_path_component(topic);
        format!("{encoded}.jsonl")
    }

    /// Append + seq derivation without taking the persist lock — the caller
    /// must already hold it (see [`persist_message_through_canonical_path`]).
    async fn add_message_with_seq_unlocked(&mut self, mut message: Message) -> Result<usize> {
        // Auto-derive title from first user message before persistence so the
        // first append_to_disk includes the title in the JSONL meta line.
        // Manual titles (flagged `title_manual` in persisted metadata) are preserved.
        if matches!(message.role, MessageRole::User)
            && !self.session.title_manual
            && self.session.title.is_none()
        {
            let derived = derive_title_from_message(&message.content);
            if !derived.is_empty() {
                self.session.title = Some(derived);
            }
        }

        // Stamp `thread_id` for the new path (M8.10 PR #1). Caller-supplied
        // values are preserved; `None` triggers derivation against the
        // current in-memory log so the persisted line carries the field
        // and the in-memory copy matches without a reload.
        //
        // PR F (M8.10): fail-closed for Assistant/Tool. See the matching
        // comment in [`SessionManager::add_message_with_seq`].
        if message.thread_id.is_none() {
            match derive_thread_id_for_new_write(&message, &self.session.messages) {
                Ok(tid) => message.thread_id = tid,
                Err(error) => {
                    record_session_persist("rejected_unbound_assistant");
                    return Err(error);
                }
            }
        }

        // UPCR-2026-012: write to disk BEFORE the in-memory push so a
        // size-cap rejection (or any other I/O failure) leaves disk and
        // RAM in lockstep. Previously the push happened first, which
        // would leave a row in `Session::messages` that never reached
        // disk on failure — and the observer would have fired for it.
        if let Err(error) = self.append_to_disk(&message).await {
            record_session_persist("failed");
            return Err(error);
        }
        self.session.messages.push(message.clone());
        self.session.updated_at = Utc::now();
        record_session_persist("committed");
        // Derive the committed sequence from the ON-DISK transcript, not
        // from this handle's in-memory mirror. The caller holds the per-key
        // persist lock, so no other writer can append between our disk
        // write and this read-back — but THIS handle's mirror may be stale
        // relative to rows other writers committed after it was opened
        // (e.g. a long-lived actor handle racing the canonical persist
        // path). Two such writers each pushing onto their own stale mirror
        // both returned the same seq for different durable rows.
        // `load_from_file` runs the same assembly a reload uses (meta line
        // + rollback-marker replay), so the returned seq is exactly the
        // index the appended row has in the durable transcript; under the
        // lock our row is the last one. Falls back to the mirror length if
        // the read-back fails: the row already committed, and a post-commit
        // read failure must not turn the call into an error.
        let path = self.session_path();
        let key = self.session.key.clone();
        let disk_len = tokio::task::spawn_blocking(move || {
            Self::load_from_file(&path, &key).map(|on_disk| on_disk.messages.len())
        })
        .await
        .ok()
        .flatten();
        let committed_seq = match disk_len {
            Some(len) if len > 0 => len - 1,
            _ => self.session.messages.len().saturating_sub(1),
        };
        // Post-commit observer fan-out: fires AFTER the disk write
        // returned Ok AND after the in-memory mirror was updated. A
        // commit failure (`append_to_disk` Err) returns above without
        // firing, satisfying the "MUST NOT emit on commit failure"
        // invariant.
        notify_message_commit(
            &self.observer_root,
            &self.session.key,
            &message,
            committed_seq,
        );
        Ok(committed_seq)
    }

    /// Snapshot-and-rename without taking the persist lock — the caller
    /// must already hold it.
    fn session_path(&self) -> PathBuf {
        self.sessions_dir
            .join(Self::topic_filename(&self.session.key))
    }

    /// Synchronously rewrite a session JSONL at `path` from an in-memory
    /// `Session`. Used by the migration path in [`Self::open`] where the
    /// caller is not yet inside an async context. Atomic write-then-rename.
    ///
    /// Cleans up the tmp file if the write or rename fails so a partial
    /// migration does not leak `<path>.<pid>-<seq>.tmp` files on disk.
    /// Records the same `octos_session_rewrite_total` metric as the async
    /// `rewrite()` so operators see a unified rewrite count regardless of
    /// the originating call path.
    fn rewrite_blocking(path: &Path, session: &Session) -> Result<()> {
        let result = Self::rewrite_blocking_inner(path, session);
        match &result {
            Ok(()) => record_session_rewrite("committed"),
            Err(_) => record_session_rewrite("failed"),
        }
        result
    }

    fn rewrite_blocking_inner(path: &Path, session: &Session) -> Result<()> {
        use std::io::Write;
        let meta = SessionMeta {
            schema_version: CURRENT_SESSION_SCHEMA,
            session_key: session.key.0.clone(),
            parent_key: session.parent_key.as_ref().map(|k| k.0.clone()),
            topic: session.topic.clone(),
            summary: session.summary.clone(),
            title: session.title.clone(),
            title_manual: session.title_manual,
            child_contracts: session.child_contracts.clone(),
            created_at: session.created_at,
            updated_at: session.updated_at,
        };
        let mut content = serde_json::to_string(&meta)?;
        content.push('\n');
        for msg in &session.messages {
            content.push_str(&serde_json::to_string(msg)?);
            content.push('\n');
        }
        let tmp_path = rewrite_tmp_path(path);
        let write_result = (|| -> Result<()> {
            let mut file = std::fs::File::create(&tmp_path)?;
            file.write_all(content.as_bytes())?;
            // `flush()` on a std File is a no-op (no userspace buffer) —
            // `sync_all` is what actually gets the bytes to stable
            // storage before the rename swaps the inode.
            file.sync_all()?;
            std::fs::rename(&tmp_path, path)?;
            if let Some(dir) = path.parent() {
                fsync_dir(dir);
            }
            Ok(())
        })();
        if write_result.is_err() {
            // Best-effort tmp cleanup. If `File::create`, `write_all`, or
            // `sync_all` fail, the tmp file may exist and must not leak.
            // (After a successful rename the tmp path no longer exists and
            // the removal is a harmless no-op; the trailing `fsync_dir`
            // cannot fail the closure.)
            let _ = std::fs::remove_file(&tmp_path);
        }
        write_result
    }

    /// Append a single message to the JSONL file.
    async fn append_to_disk(&self, message: &Message) -> Result<()> {
        let path = self.session_path();
        let parent_key = self.session.parent_key.as_ref().map(|k| k.0.clone());
        let topic = self.session.topic.clone();
        let summary = self.session.summary.clone();
        let title = self.session.title.clone();
        let title_manual = self.session.title_manual;
        let child_contracts = self.session.child_contracts.clone();
        let key_str = self.session.key.0.clone();
        let msg_json = serde_json::to_string(message)?;

        tokio::task::spawn_blocking(move || {
            use std::io::Write;
            let mut file = std::fs::OpenOptions::new()
                .read(true)
                .create(true)
                .append(true)
                .open(&path)?;

            let file_len = file.metadata()?.len();
            let is_new = file_len == 0;

            if !is_new && file_len >= MAX_SESSION_FILE_SIZE {
                warn!(
                    key = key_str,
                    size = file_len,
                    limit = MAX_SESSION_FILE_SIZE,
                    "session file at size limit, refusing append"
                );
                // Issue: post-merge codex review of #747 found this path lied
                // by returning Ok(()), which let SessionHandle::add_message_with_seq
                // push to memory and fire the message/persisted observer for a
                // row that was NEVER persisted to disk. That violates UPCR-2026-012's
                // "must not emit message/persisted for a row that did not commit"
                // contract and creates phantom seq advances.
                //
                // Mirrors the SessionManager::append_to_disk fix at line 1256.
                return Err(eyre::eyre!(
                    "session file at size limit ({} >= {}), refusing append",
                    file_len,
                    MAX_SESSION_FILE_SIZE
                ));
            }

            if is_new {
                let meta = SessionMeta {
                    schema_version: CURRENT_SESSION_SCHEMA,
                    session_key: key_str,
                    parent_key,
                    topic,
                    summary,
                    title,
                    title_manual,
                    child_contracts,
                    created_at: Utc::now(),
                    updated_at: Utc::now(),
                };
                writeln!(file, "{}", serde_json::to_string(&meta)?)?;
            } else {
                seal_torn_tail(&mut file, file_len)?;
            }

            writeln!(file, "{msg_json}")?;
            Ok::<_, eyre::Report>(())
        })
        .await
        .map_err(|e| eyre::eyre!("spawn_blocking join error: {e}"))??;

        Ok(())
    }

    /// Load a session from a specific file path.
    fn load_from_file(path: &Path, key: &SessionKey) -> Option<Session> {
        if let Ok(meta) = std::fs::metadata(path) {
            if meta.len() > MAX_SESSION_FILE_SIZE {
                warn!(key = %key, size = meta.len(), "session file too large, skipping");
                return None;
            }
        }

        let content = std::fs::read_to_string(path).ok()?;
        let mut lines = content.lines();

        let meta_line = lines.next()?;
        let meta: SessionMeta = serde_json::from_str(meta_line).ok()?;

        if meta.schema_version > CURRENT_SESSION_SCHEMA {
            warn!(key = %key, file_version = meta.schema_version, "newer schema, skipping");
            return None;
        }

        // Assemble messages, replaying any append-only rollback control
        // records at their log position so a rewind survives a per-user
        // reload too (this path backs the next turn's `SessionHandle::open`).
        let messages = assemble_session_messages(lines);

        debug!(key = %key, messages = messages.len(), "Loaded session from disk");

        Some(Session {
            key: key.clone(),
            parent_key: meta.parent_key.map(SessionKey),
            topic: meta.topic,
            summary: meta.summary,
            title: meta.title,
            title_manual: meta.title_manual,
            child_contracts: meta.child_contracts,
            messages,
            created_at: meta.created_at,
            updated_at: meta.updated_at,
        })
    }
}

/// fsync a directory so a rename into it is durable. Best-effort: on non-Unix
/// std cannot open a directory for syncing, so the rename there is only as
/// durable as the filesystem makes it (it stays atomic either way).
fn fsync_dir(dir: &Path) {
    #[cfg(unix)]
    {
        if let Ok(handle) = std::fs::File::open(dir) {
            let _ = handle.sync_all();
        }
    }
    #[cfg(not(unix))]
    {
        let _ = dir;
    }
}

/// Validate a topic name. Returns Err with a message if invalid.
///
/// Rejects the literal `"default"` because the per-user storage layout
/// uses `default.jsonl` as the no-topic filename — a user-named `"default"`
/// topic would silently collide with the topic-less mapping.
pub fn validate_topic_name(topic: &str) -> std::result::Result<(), &'static str> {
    if topic.is_empty() {
        return Err("topic name cannot be empty");
    }
    if topic.len() > 50 {
        return Err("topic name too long (max 50 characters)");
    }
    if topic.contains('#') || topic.contains(':') || topic.contains('/') {
        return Err("topic name cannot contain #, :, or /");
    }
    if topic.chars().any(|c| c.is_control()) {
        return Err("topic name cannot contain control characters");
    }
    if topic.eq_ignore_ascii_case("default") {
        return Err("topic name 'default' is reserved (used as the no-topic filename in storage)");
    }
    Ok(())
}

#[cfg(test)]
#[path = "session_tests.rs"]
mod tests;
