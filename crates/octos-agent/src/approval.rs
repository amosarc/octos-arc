//! Human-approval model for suspend-and-resume approval flows.
//!
//! Backs the gateway-channel approval flow decided in
//! `docs/ROBRIX-PHASE4-APPROVAL-FLOW-ADR.md`: when a tool call matches a
//! configured [`HumanApprovalRules`] rule, the agent loop returns early with
//! a [`PendingApprovalDraft`] instead of executing the tool. The host
//! (session actor) projects the request to the channel and tracks the
//! human decision in its own approval store.
//!
//! Named `HumanApprovalRules` (not `ApprovalPolicy`) to avoid colliding with
//! the interactive-prompt gate [`crate::policy::ApprovalPolicy`]. The two are
//! complementary: `policy::ApprovalPolicy` controls whether a live-socket
//! client may be prompted mid-turn (`ToolApprovalRequester`); this module
//! drives the asynchronous flow for store-and-forward channels.
//!
//! Security invariants (do not weaken):
//! - `tool_args_digest` (SHA-256) binds an approval to the exact arguments
//!   drafted — a response with a different digest is rejected.
//! - the consumed-set rejects replayed responses after the first decision.
//! - responses are bound to the originating room and validated against the
//!   rule's `authorized_approvers` snapshot.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ApprovalRiskLevel {
    Normal,
    Critical,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ApprovalTimeoutBehavior {
    Notify,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalRequestSpec {
    pub title: String,
    pub summary: String,
    pub risk_level: ApprovalRiskLevel,
    pub authorized_approvers: Vec<String>,
    pub expires_at: DateTime<Utc>,
    pub on_timeout: ApprovalTimeoutBehavior,
}

impl ApprovalRequestSpec {
    pub fn validate(&self) -> Result<(), String> {
        if self.authorized_approvers.is_empty() {
            return Err("authorized_approvers must not be empty".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalRequestEnvelope {
    pub request_id: String,
    pub tool_name: String,
    pub tool_args_digest: String,
    pub title: String,
    pub summary: String,
    pub risk_level: ApprovalRiskLevel,
    pub authorized_approvers: Vec<String>,
    pub expires_at: DateTime<Utc>,
    pub on_timeout: ApprovalTimeoutBehavior,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApprovalRule {
    pub tools: Vec<String>,
    pub risk_level: ApprovalRiskLevel,
    pub authorized_approvers: Vec<String>,
    pub expires_in_secs: u64,
    pub on_timeout: ApprovalTimeoutBehavior,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct HumanApprovalRules {
    rules: Vec<ApprovalRule>,
}

impl HumanApprovalRules {
    pub fn new(rules: Vec<ApprovalRule>) -> Self {
        Self { rules }
    }

    pub fn rules(&self) -> &[ApprovalRule] {
        &self.rules
    }

    pub fn matching_rule(&self, tool_name: &str) -> Option<&ApprovalRule> {
        self.rules
            .iter()
            .find(|rule| rule.tools.iter().any(|tool| tool == tool_name))
    }

    pub fn draft_for_tool_call(
        &self,
        tool_name: &str,
        tool_id: &str,
        tool_args: serde_json::Value,
        now: DateTime<Utc>,
    ) -> Result<Option<PendingApprovalDraft>, String> {
        let Some(rule) = self.matching_rule(tool_name) else {
            return Ok(None);
        };

        let spec = ApprovalRequestSpec {
            title: approval_title_for_tool(tool_name),
            summary: approval_summary_for_tool(tool_name, &tool_args),
            risk_level: rule.risk_level,
            authorized_approvers: rule.authorized_approvers.clone(),
            expires_at: now + chrono::Duration::seconds(rule.expires_in_secs as i64),
            on_timeout: rule.on_timeout,
        };

        PendingApprovalDraft::from_spec(tool_name, tool_id, tool_args, spec).map(Some)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingApprovalDraft {
    pub request: ApprovalRequestEnvelope,
    pub tool_id: String,
    pub tool_args: serde_json::Value,
}

impl PendingApprovalDraft {
    pub fn from_spec(
        tool_name: &str,
        tool_id: &str,
        tool_args: serde_json::Value,
        spec: ApprovalRequestSpec,
    ) -> Result<Self, String> {
        spec.validate()?;
        let tool_args_digest = digest_tool_args(&tool_args);
        Ok(Self {
            request: ApprovalRequestEnvelope {
                request_id: next_request_id(),
                tool_name: tool_name.to_string(),
                tool_args_digest,
                title: spec.title,
                summary: spec.summary,
                risk_level: spec.risk_level,
                authorized_approvers: spec.authorized_approvers,
                expires_at: spec.expires_at,
                on_timeout: spec.on_timeout,
            },
            tool_id: tool_id.to_string(),
            tool_args,
        })
    }
}

fn approval_title_for_tool(tool_name: &str) -> String {
    match tool_name {
        "shell" => "Approve shell command".to_string(),
        "write_file" => "Approve file write".to_string(),
        "read_file" => "Approve file read".to_string(),
        _ => format!("Approve {tool_name}"),
    }
}

fn approval_summary_for_tool(tool_name: &str, tool_args: &serde_json::Value) -> String {
    match tool_name {
        "shell" => string_arg(tool_args, "command").unwrap_or_else(|| compact_tool_args(tool_args)),
        "write_file" | "read_file" => {
            string_arg(tool_args, "path").unwrap_or_else(|| compact_tool_args(tool_args))
        }
        _ => compact_tool_args(tool_args),
    }
}

fn string_arg(args: &serde_json::Value, key: &str) -> Option<String> {
    args.get(key)
        .and_then(serde_json::Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn compact_tool_args(args: &serde_json::Value) -> String {
    let serialized = serde_json::to_string(args).unwrap_or_else(|_| "{}".to_string());
    octos_core::truncated_utf8(&serialized, 180, "...")
}

fn next_request_id() -> String {
    static SEQ: AtomicU64 = AtomicU64::new(1);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    format!("req_{}_{}", Utc::now().timestamp_millis(), seq)
}

pub fn digest_tool_args(args: &serde_json::Value) -> String {
    let encoded = serde_json::to_vec(args).unwrap_or_default();
    let digest = Sha256::digest(encoded);
    format!("sha256:{digest:x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_approval_policy_first_match_wins() {
        let policy = HumanApprovalRules::new(vec![
            ApprovalRule {
                tools: vec!["shell".to_string()],
                risk_level: ApprovalRiskLevel::Critical,
                authorized_approvers: vec!["@alice:example.org".to_string()],
                expires_in_secs: 300,
                on_timeout: ApprovalTimeoutBehavior::Notify,
            },
            ApprovalRule {
                tools: vec!["shell".to_string()],
                risk_level: ApprovalRiskLevel::Normal,
                authorized_approvers: vec!["@bob:example.org".to_string()],
                expires_in_secs: 60,
                on_timeout: ApprovalTimeoutBehavior::Notify,
            },
        ]);

        let draft = policy
            .draft_for_tool_call(
                "shell",
                "tc-shell",
                serde_json::json!({"command": "echo hi"}),
                Utc::now(),
            )
            .unwrap()
            .unwrap();

        assert_eq!(
            draft.request.authorized_approvers,
            vec!["@alice:example.org".to_string()]
        );
        assert_eq!(draft.request.risk_level, ApprovalRiskLevel::Critical);
    }
}
