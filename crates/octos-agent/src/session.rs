//! Session state machine and configuration for agent loop control.
//!
//! Provides formal lifecycle states (A4) and configurable per-session limits (A5).
//!
//! TODO: Wire `SessionStateHandle` and `SessionUsage` into the agent loop
//! (`agent.rs`) to enforce `max_turns` and per-tool limits at runtime.

use std::sync::Arc;
use tokio::sync::watch;

/// Session lifecycle states.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionState {
    /// Waiting for user input.
    Idle,
    /// Agent loop is actively running.
    Processing,
    /// Agent is waiting for external input (e.g., human confirmation).
    AwaitingInput,
    /// Session has been closed.
    Closed,
}

impl std::fmt::Display for SessionState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Idle => write!(f, "idle"),
            Self::Processing => write!(f, "processing"),
            Self::AwaitingInput => write!(f, "awaiting_input"),
            Self::Closed => write!(f, "closed"),
        }
    }
}

/// Observable session state with watch channel for subscribers.
#[derive(Clone)]
pub struct SessionStateHandle {
    tx: Arc<watch::Sender<SessionState>>,
    rx: watch::Receiver<SessionState>,
}

impl SessionStateHandle {
    pub fn new() -> Self {
        let (tx, rx) = watch::channel(SessionState::Idle);
        Self {
            tx: Arc::new(tx),
            rx,
        }
    }

    /// Get the current state.
    pub fn get(&self) -> SessionState {
        *self.rx.borrow()
    }

    /// Transition to a new state.
    pub fn set(&self, state: SessionState) {
        let _ = self.tx.send(state);
    }

    /// Subscribe to state changes.
    pub fn subscribe(&self) -> watch::Receiver<SessionState> {
        self.tx.subscribe()
    }
}

impl Default for SessionStateHandle {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn should_start_in_idle() {
        let handle = SessionStateHandle::new();
        assert_eq!(handle.get(), SessionState::Idle);
    }

    #[test]
    fn should_transition_states() {
        let handle = SessionStateHandle::new();
        handle.set(SessionState::Processing);
        assert_eq!(handle.get(), SessionState::Processing);
        handle.set(SessionState::AwaitingInput);
        assert_eq!(handle.get(), SessionState::AwaitingInput);
        handle.set(SessionState::Closed);
        assert_eq!(handle.get(), SessionState::Closed);
    }

    #[tokio::test]
    async fn should_notify_subscribers() {
        let handle = SessionStateHandle::new();
        let mut rx = handle.subscribe();
        handle.set(SessionState::Processing);
        rx.changed().await.unwrap();
        assert_eq!(*rx.borrow(), SessionState::Processing);
    }

    #[test]
    fn should_display_state() {
        assert_eq!(SessionState::Idle.to_string(), "idle");
        assert_eq!(SessionState::Processing.to_string(), "processing");
        assert_eq!(SessionState::AwaitingInput.to_string(), "awaiting_input");
        assert_eq!(SessionState::Closed.to_string(), "closed");
    }

}
