//! Plan-state persistence (simplified): the disk store holds ONLY
//! `{envelope, tail_seq}` — inventory and index are rebuildable from the
//! workspace at any time (agents rebuild per turn, so a rescan is the
//! rehydrate path anyway; no reconcile bookkeeping is persisted).
//!
//! Write protocol: atomic tmp + rename into
//! `.arc/host-plan/state.v1.json` under the workspace root. Readers
//! either see the old file or the new one, never a partial write.

use std::path::{Path, PathBuf};

use super::Envelope;

const STORE_VERSION: u8 = 1;

#[derive(Debug, serde::Serialize, serde::Deserialize)]
struct StoredPlan {
    v: u8,
    envelope: Envelope,
    tail_seq: u64,
}

/// Persist the envelope + tail seq atomically. Creates parent dirs.
pub fn store(workspace: &Path, envelope: &Envelope, tail_seq: u64) -> std::io::Result<PathBuf> {
    let path = workspace.join(super::STATE_REL_PATH);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let stored = StoredPlan {
        v: STORE_VERSION,
        envelope: envelope.clone(),
        tail_seq,
    };
    let json = serde_json::to_string(&stored)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, json.as_bytes())?;
    std::fs::rename(&tmp, &path)?;
    Ok(path)
}

/// Load a previously stored plan, if any. Corrupt/absent files yield
/// `None` (rehydration is best-effort; a fresh envelope rebuilds state).
pub fn load(workspace: &Path) -> Option<(Envelope, u64)> {
    let path = workspace.join(super::STATE_REL_PATH);
    let bytes = std::fs::read(path).ok()?;
    let stored: StoredPlan = serde_json::from_slice(&bytes).ok()?;
    if stored.v != STORE_VERSION {
        return None;
    }
    Some((stored.envelope, stored.tail_seq))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn store_and_load_round_trip_envelope_and_seq() {
        let dir = tempfile::tempdir().unwrap();
        let mut env = Envelope::default();
        env.seq = 11;
        env.task = "task".into();

        let path = store(dir.path(), &env, 3).unwrap();
        assert!(path.ends_with(".arc/host-plan/state.v1.json"));

        let (back_env, back_seq) = load(dir.path()).unwrap();
        assert_eq!(back_env, env);
        assert_eq!(back_seq, 3);
    }

    #[test]
    fn load_on_missing_dir_is_none() {
        let dir = tempfile::tempdir().unwrap();
        assert!(load(dir.path()).is_none());
    }

    #[test]
    fn corrupt_store_is_none_not_panic() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".arc/host-plan/state.v1.json");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, b"{not json").unwrap();
        assert!(load(dir.path()).is_none());
    }

    #[test]
    fn store_is_atomic_no_tmp_left_behind() {
        let dir = tempfile::tempdir().unwrap();
        store(dir.path(), &Envelope::default(), 0).unwrap();
        let tmp = dir.path().join(".arc/host-plan/state.v1.json.tmp");
        assert!(!tmp.exists(), "tmp file must be renamed away");
    }
}
