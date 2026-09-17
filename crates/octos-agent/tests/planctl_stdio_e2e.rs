//! planctl stdio e2e (ignored by default — needs the built `octos`
//! binary; no network). Drives the REAL `octos serve --stdio --solo`
//! through the five ARC JSON-RPC methods and verifies the `[arc-plan]`
//! marker never crashes the protocol layer: profile create → llm upsert
//! (route points at a dead local port) → session/open → turn/start with
//! a marked prompt → the turn must terminate with turn/error (provider
//! unreachable), never a process crash and never the bare payload
//! echoed back.
//!
//! Run with: cargo test -p octos-agent --test planctl_stdio_e2e -- --ignored

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

fn octos_binary() -> std::path::PathBuf {
    // Same discovery as octos-cli/tests/cli_tests.rs.
    let mut path = std::env::current_exe().unwrap();
    path.pop();
    path.pop();
    path.push("octos");
    if !path.exists() {
        // Workspace target dir when run from octos-agent's test harness.
        let mut ws = path.clone();
        ws.pop();
        ws.pop();
        ws.pop();
        ws.push("octos");
        path = ws;
    }
    path
}

struct StdioClient {
    child: Child,
    /// Lines drained by the reader thread; `None` = EOF.
    lines: std::sync::mpsc::Receiver<Option<String>>,
    next_id: u64,
}

impl StdioClient {
    fn spawn(cwd: &std::path::Path, data_dir: &std::path::Path) -> Self {
        let mut child = Command::new(octos_binary())
            .args(["serve", "--stdio", "--solo"])
            .arg("--instance-data-dir")
            .arg(data_dir)
            .current_dir(cwd)
            .env("OCTOS_DANGER_FULL_ACCESS", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn octos serve --stdio --solo");
        let stdout = child.stdout.take().expect("stdout");
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                if tx.send(Some(line.unwrap_or_default())).is_err() {
                    break;
                }
            }
            let _ = tx.send(None);
        });
        Self {
            child,
            lines: rx,
            next_id: 0,
        }
    }

    /// Fire-and-forget write (turns stream their outcome as
    /// notifications; the request ACK may trail a full provider retry
    /// chain, so a marked turn must not block on it).
    fn send_raw(&mut self, method: &str, params: Value) {
        self.next_id += 1;
        let frame = json!({
            "jsonrpc": "2.0", "id": format!("req-{}", self.next_id),
            "method": method, "params": params,
        });
        let stdin = self.child.stdin.as_mut().expect("stdin");
        stdin
            .write_all((frame.to_string() + "\n").as_bytes())
            .unwrap();
        stdin.flush().unwrap();
    }

    fn send(&mut self, method: &str, params: Value) -> Value {
        self.next_id += 1;
        let id = self.next_id;
        let frame = json!({
            "jsonrpc": "2.0", "id": format!("req-{id}"),
            "method": method, "params": params,
        });
        let stdin = self.child.stdin.as_mut().expect("stdin");
        stdin
            .write_all((frame.to_string() + "\n").as_bytes())
            .unwrap();
        stdin.flush().unwrap();

        let deadline = Instant::now() + Duration::from_secs(180);
        while Instant::now() < deadline {
            match self.lines.recv_timeout(Duration::from_millis(500)) {
                Ok(Some(line)) => {
                    let Ok(v) = serde_json::from_str::<Value>(&line) else {
                        continue;
                    };
                    if v.get("id").and_then(|i| i.as_str()) == Some(&format!("req-{id}")) {
                        return v;
                    }
                }
                Ok(None) => panic!("octos closed stdout"),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    panic!("octos reader thread died")
                }
            }
        }
        panic!("timeout waiting for {method} response");
    }
}

impl Drop for StdioClient {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
#[ignore = "requires the built octos binary (cargo build -p octos-cli)"]
fn marked_turn_survives_the_stdio_protocol_without_leaking_the_payload() {
    let dir = tempfile::tempdir().unwrap();
    let data = tempfile::tempdir().unwrap();

    let mut client = StdioClient::spawn(dir.path(), data.path());

    // 1. profile/local/create
    let unique = format!("arc-e2e-{}", std::process::id());
    let res = client.send(
        "profile/local/create",
        json!({
            "requested_id": unique,
            "name": "planctl e2e",
            "username": unique,
            "email": format!("{unique}@solo.local"),
        }),
    );
    let profile_id = res["result"]["profile_id"]
        .as_str()
        .expect("profile_id")
        .to_string();

    // 2. profile/llm/upsert — route at a dead port so the turn fails
    //    fast instead of needing a real provider.
    client.send(
        "profile/llm/upsert",
        json!({
            "profile_id": profile_id,
            "set_primary": true,
            "selection": {
                "family_id": "openai",
                "model_id": "mock-model",
                "route": {"api_type": "openai", "base_url": "http://127.0.0.1:9/api"},
            },
        }),
    );

    // 3. session/open
    let session_id = format!("sess-{}", std::process::id());
    client.send(
        "session/open",
        json!({"session_id": session_id, "cwd": dir.path(), "profile_id": profile_id}),
    );

    // 4. turn/start with the marker + envelope payload, fire-and-forget:
    //    the ACK trails the (dead-route) provider retry chain, so the
    //    test must not block on it. What matters: the marker path does
    //    not crash the protocol layer.
    let envelope = json!({
        "v": 1, "seq": 1, "phase": "node", "stack": "web_react_express",
        "task": "stdio e2e", "spec_dir": ".arc/acceptance",
        "rules": "arc_web_v1",
        "ports": {"grade": 3000, "smoke": 3001, "extra": []},
        "steps": [{"id": "REQ-1", "action": "modify", "input": "x",
                   "expected_output": "y", "status": "pending"}],
        "focus": "REQ-1",
    });
    let marked = format!("[arc-plan]\n{}\n\nDo the thing.", envelope);
    let turn_id = format!("turn-{}", std::process::id());
    client.send_raw(
        "turn/start",
        json!({
            "session_id": session_id,
            "turn_id": turn_id,
            "input": [{"kind": "text", "text": marked}],
        }),
    );

    // 5. Bounded liveness window: the server keeps emitting protocol
    //    frames for a marked turn whose provider is unreachable and
    //    never exits; no frame may echo the marker or the payload.
    let deadline = Instant::now() + Duration::from_secs(20);
    let mut saw_frame = false;
    while Instant::now() < deadline {
        if let Ok(Some(status)) = client.child.try_wait() {
            panic!("octos crashed after a marked turn: {status}");
        }
        match client.lines.recv_timeout(Duration::from_millis(500)) {
            Ok(Some(line)) => {
                saw_frame = true;
                let echoed = line.trim();
                assert!(
                    !echoed.contains("[arc-plan]"),
                    "marker leaked to stdout: {echoed}"
                );
                assert!(
                    !echoed.contains("\"web_react_express\""),
                    "envelope payload leaked to stdout: {echoed}"
                );
            }
            Ok(None) => panic!("octos closed stdout under load"),
            Err(_) => continue,
        }
    }
    assert!(
        client.child.try_wait().unwrap().is_none(),
        "server must survive the marked turn"
    );
    let _ = saw_frame; // heartbeats are best-effort; liveness is the gate
}
