use super::*;

use octos_core::MessageRole;
use tempfile::TempDir;

fn make_message(role: MessageRole, content: &str) -> Message {
    // PR F (M8.10): pre-stamp Assistant/Tool messages with a synthetic
    // thread_id so they pass the new-write fail-closed check. Tests
    // that exercise the legacy untagged path use bare struct literals
    // or the dedicated helpers directly. Production code uses the
    // typed `Message::assistant_with_thread`/`tool_with_thread`
    // constructors and the canonical `persist_assistant_message`
    // helper, both of which already supply thread_id.
    let thread_id = match role {
        MessageRole::Assistant | MessageRole::Tool => Some("test-thread-default".to_string()),
        _ => None,
    };
    Message {
        role,
        content: content.into(),
        media: vec![],
        tool_calls: None,
        tool_call_id: None,
        reasoning_content: None,
        client_message_id: None,
        thread_id,
        timestamp: Utc::now(),
    }
}

#[test]
fn test_session_get_history() {
    let mut session = Session::new(SessionKey::new("cli", "test"));
    for i in 0..10 {
        session
            .messages
            .push(make_message(MessageRole::User, &format!("msg{i}")));
    }
    let history = session.get_history(3);
    assert_eq!(history.len(), 3);
    assert_eq!(history[0].content, "msg7");
    assert_eq!(history[2].content, "msg9");
}

#[tokio::test]
async fn test_session_manager_create_and_retrieve() {
    let tmp = TempDir::new().unwrap();
    let mut mgr = SessionManager::open(tmp.path()).unwrap();
    let key = SessionKey::new("cli", "default");

    let session = mgr.get_or_create(&key).await;
    assert_eq!(session.messages.len(), 0);

    mgr.add_message(&key, make_message(MessageRole::User, "hello"))
        .await
        .unwrap();
    mgr.add_message(&key, make_message(MessageRole::Assistant, "hi"))
        .await
        .unwrap();

    let session = mgr.get_or_create(&key).await;
    assert_eq!(session.messages.len(), 2);
}

#[tokio::test]
async fn test_session_manager_persistence() {
    let tmp = TempDir::new().unwrap();
    let key = SessionKey::new("cli", "persist");

    // Write session
    {
        let mut mgr = SessionManager::open(tmp.path()).unwrap();
        mgr.add_message(&key, make_message(MessageRole::User, "saved"))
            .await
            .unwrap();
        mgr.add_message(&key, make_message(MessageRole::Assistant, "reply"))
            .await
            .unwrap();
    }

    // New manager should load from disk
    {
        let mut mgr = SessionManager::open(tmp.path()).unwrap();
        let session = mgr.get_or_create(&key).await;
        assert_eq!(session.messages.len(), 2);
        assert_eq!(session.messages[0].content, "saved");
        assert_eq!(session.messages[1].content, "reply");
    }
}

#[tokio::test]
async fn rollback_last_n_user_turns_trims_and_survives_reload() {
    let tmp = TempDir::new().unwrap();
    let key = SessionKey::new("cli", "rollback-rt");

    {
        let mut mgr = SessionManager::open(tmp.path()).unwrap();
        // Seed 3 user turns (user + assistant each) with persisted
        // thread_ids so the drop can group them.
        for n in 1..=3 {
            let tid = format!("t{n}");
            let mut user = make_message(MessageRole::User, &format!("turn {n}"));
            user.client_message_id = Some(tid.clone());
            user.thread_id = Some(tid.clone());
            mgr.add_message(&key, user).await.unwrap();
            let mut asst = make_message(MessageRole::Assistant, &format!("reply {n}"));
            asst.thread_id = Some(tid.clone());
            mgr.add_message(&key, asst).await.unwrap();
        }

        // Roll back the last user turn: appends the marker + trims memory.
        let dropped = mgr.rollback_last_n_user_turns(&key, 1).await.unwrap();
        assert_eq!(dropped, 1);

        let session = mgr.get_or_create(&key).await;
        assert_eq!(session.messages.len(), 4, "turns 1 & 2 remain in memory");
        assert!(session.messages.iter().all(|m| m.content != "turn 3"));
        assert!(session.messages.iter().all(|m| m.content != "reply 3"));
    }

    // A fresh manager reloads from disk; the append-only marker replays the
    // trim (NOT a truncation — the dropped rows are still on disk).
    {
        let mut reload = SessionManager::open(tmp.path()).unwrap();
        let session = reload.get_or_create(&key).await;
        let contents: Vec<&String> = session.messages.iter().map(|m| &m.content).collect();
        assert_eq!(
            session.messages.len(),
            4,
            "rollback marker must survive reload; got {contents:?}"
        );
        assert!(session.messages.iter().all(|m| m.content != "turn 3"));
    }
}

#[tokio::test]
async fn test_session_manager_clear() {
    let tmp = TempDir::new().unwrap();
    let key = SessionKey::new("cli", "clear-me");
    let mut mgr = SessionManager::open(tmp.path()).unwrap();

    mgr.add_message(&key, make_message(MessageRole::User, "temp"))
        .await
        .unwrap();
    assert_eq!(mgr.get_or_create(&key).await.messages.len(), 1);

    mgr.clear(&key).await.unwrap();

    // After clear, should be empty
    let session = mgr.get_or_create(&key).await;
    assert_eq!(session.messages.len(), 0);
}

#[tokio::test]
async fn test_session_rewrite() {
    let tmp = TempDir::new().unwrap();
    let key = SessionKey::new("cli", "rewrite");
    let mut mgr = SessionManager::open(tmp.path()).unwrap();

    // Add 5 messages
    for i in 0..5 {
        mgr.add_message(&key, make_message(MessageRole::User, &format!("msg{i}")))
            .await
            .unwrap();
    }

    // Mutate in-memory: keep only last 2
    let session = mgr.get_or_create(&key).await;
    session.messages.drain(0..3);
    assert_eq!(session.messages.len(), 2);

    // Rewrite to disk
    mgr.rewrite(&key).await.unwrap();

    // Load fresh from disk — should have only 2 messages
    let mut mgr2 = SessionManager::open(tmp.path()).unwrap();
    let session2 = mgr2.get_or_create(&key).await;
    assert_eq!(session2.messages.len(), 2);
    assert_eq!(session2.messages[0].content, "msg3");
    assert_eq!(session2.messages[1].content, "msg4");
}

#[tokio::test]
async fn test_fork_creates_child() {
    let tmp = TempDir::new().unwrap();
    let mut mgr = SessionManager::open(tmp.path()).unwrap();
    let parent = SessionKey::new("telegram", "chat1");

    for i in 0..5 {
        mgr.add_message(&parent, make_message(MessageRole::User, &format!("msg{i}")))
            .await
            .unwrap();
    }

    let child_key = mgr.fork(&parent, "chat1_fork", 3).await.unwrap();
    assert_eq!(child_key, SessionKey::new("telegram", "chat1_fork"));

    let child = mgr.get_or_create(&child_key).await;
    assert_eq!(child.parent_key, Some(parent.clone()));
    assert_eq!(child.messages.len(), 3);
    assert_eq!(child.messages[0].content, "msg2");
    assert_eq!(child.messages[2].content, "msg4");
}

#[tokio::test]
async fn torn_tail_does_not_eat_next_appended_message() {
    use std::io::Write;
    let tmp = TempDir::new().unwrap();
    let key = SessionKey::new("cli", "torn-tail-append");
    let mut mgr = SessionManager::open(tmp.path()).unwrap();
    mgr.add_message(&key, make_message(MessageRole::User, "before torn"))
        .await
        .unwrap();

    // Simulate a crash mid-write: partial JSON row, no trailing newline.
    std::fs::OpenOptions::new()
        .append(true)
        .open(mgr.session_path(&key))
        .unwrap()
        .write_all(b"{\"role\":\"user\",\"content\":\"torn")
        .unwrap();

    mgr.add_message(&key, make_message(MessageRole::User, "after torn"))
        .await
        .unwrap();

    // A fresh manager reloads from disk: the torn row is skipped, but the
    // complete row appended after it must survive.
    let mut reload = SessionManager::open(tmp.path()).unwrap();
    let session = reload.get_or_create(&key).await;
    let contents: Vec<&str> = session
        .messages
        .iter()
        .map(|m| m.content.as_str())
        .collect();
    assert_eq!(
        contents,
        ["before torn", "after torn"],
        "torn tail must not eat the next appended message"
    );
}

/// Issue #2006: the same seal must protect the rollback control line —
/// otherwise the marker fuses with a torn tail, is dropped on reload, and
/// `/undo` un-does itself (the rolled-back turn resurrects).
#[tokio::test]
async fn torn_tail_does_not_eat_rollback_marker() {
    use std::io::Write;
    let tmp = TempDir::new().unwrap();
    let key = SessionKey::new("cli", "torn-tail-rollback");
    let mut mgr = SessionManager::open(tmp.path()).unwrap();
    for n in 1..=2 {
        let tid = format!("t{n}");
        let mut user = make_message(MessageRole::User, &format!("turn {n}"));
        user.client_message_id = Some(tid.clone());
        user.thread_id = Some(tid.clone());
        mgr.add_message(&key, user).await.unwrap();
        let mut asst = make_message(MessageRole::Assistant, &format!("reply {n}"));
        asst.thread_id = Some(tid.clone());
        mgr.add_message(&key, asst).await.unwrap();
    }

    // Crash mid-write leaves a torn tail right before the rollback marker.
    std::fs::OpenOptions::new()
        .append(true)
        .open(mgr.session_path(&key))
        .unwrap()
        .write_all(b"{\"role\":\"user\",\"content\":\"torn")
        .unwrap();

    let dropped = mgr.rollback_last_n_user_turns(&key, 1).await.unwrap();
    assert_eq!(dropped, 1);

    // Fresh reload replays the marker: turn 2 must stay rolled back.
    let mut reload = SessionManager::open(tmp.path()).unwrap();
    let session = reload.get_or_create(&key).await;
    let contents: Vec<&str> = session
        .messages
        .iter()
        .map(|m| m.content.as_str())
        .collect();
    assert_eq!(
        contents,
        ["turn 1", "reply 1"],
        "rollback marker fused with a torn tail resurrects the rolled-back turn"
    );
}
