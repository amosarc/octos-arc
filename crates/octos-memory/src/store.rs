//! Episode store: persistent storage for episodes using redb (pure Rust).

use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use eyre::{Result, WrapErr};
use redb::{Database, ReadableTable, TableDefinition};
use tracing::{debug, warn};

use crate::episode::{Episode, EpisodeSource};
use crate::hybrid_search::{HybridIndex, HybridScore};

/// Table for episodes: key = episode_id, value = JSON
const EPISODES_TABLE: TableDefinition<&str, &str> = TableDefinition::new("episodes");

/// Index table for episodes by working directory: key = cwd, value = list of episode IDs (JSON)
const CWD_INDEX_TABLE: TableDefinition<&str, &str> = TableDefinition::new("cwd_index");

/// Table for episode embeddings: key = episode_id, value = bincode-serialized Vec<f32>
const EMBEDDINGS_TABLE: TableDefinition<&str, &[u8]> = TableDefinition::new("embeddings");

/// Default embedding dimension (OpenAI text-embedding-3-small), used when the
/// caller opens the store without declaring a width.
///
/// Public because remote OpenAI-compatible providers whose native size differs
/// (e.g. DashScope `text-embedding-v4` at 1024) can be pinned to this value via
/// the `dimensions` request field. Providers that CANNOT reach it — notably
/// in-process EmbeddingGemma at 768, since Matryoshka only truncates downward —
/// must instead size the index itself via [`EpisodeStore::open_with_dimension`].
pub const DEFAULT_DIMENSION: usize = 1536;

/// Typed cause for "the redb episode store at `path` is already owned by
/// another process".
///
/// redb is single-writer-single-process, so a second `octos serve` against the
/// same data dir can never open it. That is a deployment/config mistake with a
/// concrete fix, not an internal fault — but the strict opener used to report
/// it as an untyped string, so every caller upstack could only re-wrap prose.
/// Carrying a typed cause lets a caller *recognise* the condition
/// ([`is_episode_store_locked`]) and render its own actionable message, the
/// same way the API layer already downcasts to [`std::io::Error`] for
/// permission-denied workspaces.
#[derive(Debug, Clone)]
pub struct EpisodeStoreLocked {
    /// The `episodes.redb` path whose lock is held elsewhere.
    pub path: PathBuf,
}

impl std::fmt::Display for EpisodeStoreLocked {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Keep the redb wording ("Database already open. Cannot acquire
        // lock.") — operators grep for it and it is what redb itself prints.
        write!(
            f,
            "failed to open redb database at {}: Database already open. \
             Cannot acquire lock. Another octos process (typically an `octos \
             serve` daemon) already owns this data directory. Stop it, or \
             start this instance against its own storage with \
             `--instance-data-dir <dir>`.",
            self.path.display(),
        )
    }
}

impl std::error::Error for EpisodeStoreLocked {}

/// True when `error` carries an [`EpisodeStoreLocked`] anywhere in its eyre
/// chain — i.e. the failure is redb lock contention rather than corruption,
/// I/O, or a permission problem.
///
/// Structural (downcast), not string matching, so wrapping the error with
/// extra context upstack cannot break the check.
pub fn is_episode_store_locked(error: &eyre::Report) -> bool {
    error
        .chain()
        .any(|cause| cause.downcast_ref::<EpisodeStoreLocked>().is_some())
}

/// Parse a cwd-index JSON array of episode IDs. On corrupt JSON, salvage any
/// quoted strings that look like episode IDs instead of silently replacing
/// the list with an empty one (which would orphan every other episode
/// indexed under that cwd). Shared by the store, scan, and delete paths so
/// corruption handling stays consistent.
fn parse_episode_ids_with_salvage(raw: &str) -> Vec<String> {
    match serde_json::from_str(raw) {
        Ok(ids) => ids,
        Err(e) => {
            warn!(
                "failed to parse cwd index JSON ({} bytes): {e}; attempting salvage",
                raw.len()
            );
            let salvaged: Vec<String> = raw
                .split('"')
                .enumerate()
                .filter_map(|(i, s)| {
                    // Odd indices are inside quotes in valid JSON arrays
                    if i % 2 == 1 && !s.is_empty() && !s.contains(['[', ']', ',']) {
                        Some(s.to_string())
                    } else {
                        None
                    }
                })
                .collect();
            warn!(
                "salvaged {} episode IDs from corrupted index",
                salvaged.len()
            );
            salvaged
        }
    }
}

/// Store for episodes using redb (pure Rust embedded database).
///
/// # Degraded mode
///
/// `redb` is a single-writer-single-process embedded database — only
/// one process at a time may hold the OS file lock on
/// `episodes.redb`. In the production fleet `octos serve` and
/// `octos gateway` run as separate processes that bootstrap
/// `ProfileRuntime` independently per profile, and both call
/// `EpisodeStore::open` against the same path. The first opener (the
/// long-lived `octos serve` daemon) wins; the second opener (`octos
/// gateway` subprocesses) previously crashed with
/// `redb::DatabaseError::DatabaseAlreadyOpen` ("Database already
/// open. Cannot acquire lock."), launchd restarted it, and the cycle
/// repeated every ~2 seconds.
///
/// To prevent that crashloop without sacrificing serve-mode
/// persistence, [`EpisodeStore::open_or_degraded`] falls back to a
/// **degraded** in-memory store when the redb file is already locked:
/// `db` is `None`, all mutating operations silently no-op, and all
/// public read operations return empty. Callers that need to observe
/// the degradation (logging, metrics) can read [`Self::is_degraded`].
///
/// Two opener entry points formalize the role split:
/// - [`Self::open`] (strict) — fails when the lock is already held.
///   The right choice for the process that *must* own the canonical
///   store (`octos serve`, `octos chat`, the test suite). Surfaces
///   deployment misconfigurations as errors instead of silently
///   degrading the canonical writer.
/// - [`Self::open_or_degraded`] — falls back to the degraded handle
///   on lock contention. The right choice for `octos gateway`
///   subprocesses (and other companion processes) that should keep
///   going when the canonical store is owned elsewhere.
///
/// This split prevents the gateway-starts-first dev workflow from
/// flipping canonical ownership to gateway and degrading serve.
///
/// Episode reads on the gateway path are best-effort already
/// (memory-bank recall happens through the in-process `MemoryStore`,
/// not `EpisodeStore`), and episode writes from a sub-agent are
/// completion-summary persistence that serve will redo on
/// `/api/chat` if it cares. The fleet still benefits from gateway
/// channel polling staying alive.
pub struct EpisodeStore {
    /// `Some(db)` when the redb file was successfully opened (the
    /// owning process). `None` when this is a degraded in-memory
    /// fallback because the redb file lock was already held by
    /// another process (see the type-level docs).
    db: Option<Arc<Database>>,
    index: RwLock<HybridIndex>,
}

impl EpisodeStore {
    /// Open or create an episode store at the given path.
    ///
    /// **Strict mode** — fails if the redb file lock is already held
    /// by another process. This is the right entry point for the
    /// process that *must* own the canonical store (`octos serve`,
    /// `octos chat`, the agent test suite). If the lock is held, the
    /// caller likely has a deployment misconfiguration and should
    /// surface the error rather than silently degrading.
    ///
    /// For the `octos gateway` subprocess (and anywhere else that
    /// runs alongside an existing serve daemon and should keep going
    /// when the canonical store is owned elsewhere), use
    /// [`Self::open_or_degraded`].
    pub async fn open(data_dir: impl AsRef<Path>) -> Result<Self> {
        Self::open_inner(data_dir.as_ref(), false, DEFAULT_DIMENSION).await
    }

    /// Like [`Self::open`] but sizes the vector index to `dimension` instead of
    /// [`DEFAULT_DIMENSION`].
    ///
    /// The HNSW index is built at ONE fixed width and
    /// [`HybridIndex::insert`] drops any vector that does not match it,
    /// degrading that episode to BM25-only. So the index must be sized from the
    /// embedding provider actually in use — 1536 for `text-embedding-3-small`,
    /// but 768 for in-process EmbeddingGemma, which can never reach 1536
    /// (Matryoshka only truncates downward). Callers that have an embedder
    /// should pass `embedder.dimension()`.
    ///
    /// Changing the dimension invalidates previously persisted embeddings:
    /// they stay in redb but are skipped on rebuild (with a warning) until
    /// the episodes are re-embedded.
    pub async fn open_with_dimension(data_dir: impl AsRef<Path>, dimension: usize) -> Result<Self> {
        Self::open_inner(data_dir.as_ref(), false, dimension).await
    }

    /// [`Self::open_or_degraded`] with an explicit vector-index width.
    /// See [`Self::open_with_dimension`] for why this must track the embedder.
    pub async fn open_or_degraded_with_dimension(
        data_dir: impl AsRef<Path>,
        dimension: usize,
    ) -> Result<Self> {
        Self::open_inner(data_dir.as_ref(), true, dimension).await
    }

    async fn open_inner(data_dir: &Path, allow_degraded: bool, dimension: usize) -> Result<Self> {
        let data_dir = data_dir.to_path_buf();
        tokio::fs::create_dir_all(&data_dir)
            .await
            .wrap_err("failed to create data directory")?;

        let db_path = data_dir.join("episodes.redb");
        let db_path_for_log = db_path.clone();

        // redb is sync, so we spawn_blocking for the initial open + table init + index rebuild.
        //
        // We surface the `DatabaseAlreadyOpen` case as a typed sentinel
        // (`Ok(None)`) so the outer task can decide whether to install
        // the degraded fallback (gateway) or propagate the error
        // (serve). Every other error bubbles up verbatim.
        let result: Result<Option<(Database, HybridIndex)>> =
            tokio::task::spawn_blocking(move || {
                let db = match Database::create(&db_path) {
                    Ok(db) => db,
                    Err(redb::DatabaseError::DatabaseAlreadyOpen) => return Ok(None),
                    Err(e) => {
                        return Err(eyre::Report::new(e).wrap_err("failed to open redb database"));
                    }
                };

                // Initialize tables
                let write_txn = db.begin_write()?;
                {
                    let _ = write_txn.open_table(EPISODES_TABLE)?;
                    let _ = write_txn.open_table(CWD_INDEX_TABLE)?;
                    let _ = write_txn.open_table(EMBEDDINGS_TABLE)?;
                }
                write_txn.commit()?;

                // Rebuild in-memory hybrid index from stored data
                let mut index = HybridIndex::new(dimension);
                {
                    let read_txn = db.begin_read()?;
                    let episodes_table = read_txn.open_table(EPISODES_TABLE)?;
                    let embeddings_table = read_txn.open_table(EMBEDDINGS_TABLE)?;

                    for entry in episodes_table.iter()? {
                        let (key, value) = entry?;
                        let ep_id = key.value().to_string();
                        if let Ok(episode) = serde_json::from_str::<Episode>(value.value()) {
                            let embedding: Option<Vec<f32>> = embeddings_table
                                .get(ep_id.as_str())
                                .ok()
                                .flatten()
                                .and_then(|v| bincode::deserialize(v.value()).ok());
                            index.insert(&ep_id, &episode.summary, embedding.as_deref());
                        }
                    }
                }

                // One summary instead of one line per episode. `insert` warns on
                // the first mismatch and counts the rest, so without this a
                // model change was either a single warning buried in the boot
                // log or (before) thousands of identical ones.
                let coverage = index.vector_coverage();
                if coverage.has_dimension_mismatch() {
                    warn!(
                        path = %data_dir.display(),
                        expected_dimension = coverage.dimension,
                        mismatched = coverage.dimension_mismatches,
                        vectorized = coverage.vectorized,
                        total = coverage.total,
                        "episode store rebuilt with dimension-mismatched embeddings dropped — \
                         those episodes are BM25-only until re-embedded. This happens when the \
                         configured embedding model changed; stored vectors cannot be converted."
                    );
                }
                debug!(
                    path = %data_dir.display(),
                    vectorized = coverage.vectorized,
                    total = coverage.total,
                    "opened episode store"
                );
                Ok(Some((db, index)))
            })
            .await?;

        match result? {
            Some((db, index)) => Ok(Self {
                db: Some(Arc::new(db)),
                index: RwLock::new(index),
            }),
            None if allow_degraded => {
                warn!(
                    path = %db_path_for_log.display(),
                    "redb episode store already held by another process; \
                     installing degraded in-memory fallback. Writes will \
                     no-op and reads will return empty for this handle. \
                     This is expected for `octos gateway` subprocesses \
                     when `octos serve` already owns the lock."
                );
                Ok(Self {
                    db: None,
                    index: RwLock::new(HybridIndex::new(dimension)),
                })
            }
            // Typed and UNWRAPPED, so callers upstack can both recognise the
            // condition (`is_episode_store_locked`) and forward a message
            // that already names the path and the remedy. The
            // developer-facing "wrong opener?" hint is a log line rather than
            // error context: it is noise for the operator who sees this on a
            // session/open, and burying the actionable sentence one level
            // deeper is exactly what made this error unreadable in clients.
            None => {
                debug!(
                    path = %db_path_for_log.display(),
                    "strict `EpisodeStore::open` failed on a held lock — if this is the \
                     `octos gateway` subprocess, call `open_or_degraded` instead"
                );
                Err(eyre::Report::new(EpisodeStoreLocked {
                    path: db_path_for_log.clone(),
                }))
            }
        }
    }

    /// `true` when this store is operating in the degraded in-memory
    /// fallback mode described on the type-level docs (the redb file
    /// lock was already held when [`Self::open_or_degraded`] ran).
    /// Callers can use this for diagnostics, metrics, or to skip
    /// persistence-dependent codepaths.
    pub fn is_degraded(&self) -> bool {
        self.db.is_none()
    }

    /// Store an episode.
    ///
    /// In degraded mode ([`Self::is_degraded`]) the disk write is
    /// skipped and the call returns `Ok(())`. The in-memory hybrid
    /// index is updated with the summary so [`Self::find_relevant_hybrid`]
    /// (which is index-only when populated) can match it for ranking,
    /// but full episode bodies come from disk — so the public read
    /// recall methods
    /// still return empty on a degraded handle because there is no
    /// DB to fetch bodies from. The intent is "writes accepted, reads
    /// empty," not in-memory persistence.
    pub async fn store(&self, episode: Episode) -> Result<()> {
        let episode_id = episode.id.clone();
        let episode_id_for_index = episode_id.clone();
        let summary = episode.summary.clone();

        // Degraded fallback: skip the disk write, update the in-memory
        // index, return success. See type-level docs on `EpisodeStore`.
        let Some(db) = self.db.clone() else {
            match self.index.write() {
                Ok(mut idx) => idx.insert(&episode_id_for_index, &summary, None),
                Err(e) => warn!("index write lock poisoned, skipping update: {e}"),
            }
            return Ok(());
        };

        let cwd = episode.working_dir.to_string_lossy().to_string();
        let episode_json =
            serde_json::to_string(&episode).wrap_err("failed to serialize episode")?;

        tokio::task::spawn_blocking(move || {
            let write_txn = db.begin_write()?;
            {
                // Store episode
                let mut table = write_txn.open_table(EPISODES_TABLE)?;
                table.insert(episode_id.as_str(), episode_json.as_str())?;

                // Update cwd index
                let mut index = write_txn.open_table(CWD_INDEX_TABLE)?;
                let existing: Vec<String> = index
                    .get(cwd.as_str())?
                    .map(|v| parse_episode_ids_with_salvage(v.value()))
                    .unwrap_or_default();

                let mut ids = existing;
                if !ids.contains(&episode_id) {
                    ids.push(episode_id);
                }
                let ids_json = serde_json::to_string(&ids)?;
                index.insert(cwd.as_str(), ids_json.as_str())?;
            }
            write_txn.commit()?;
            Ok::<_, eyre::Report>(())
        })
        .await??;

        // Update in-memory hybrid index (text only, no embedding yet)
        match self.index.write() {
            Ok(mut idx) => idx.insert(&episode_id_for_index, &summary, None),
            Err(e) => warn!("index write lock poisoned, skipping update: {e}"),
        }

        Ok(())
    }

    /// Store an embedding for an episode.
    ///
    /// In degraded mode the disk write is skipped and the call
    /// returns `Ok(())`. The in-memory hybrid index is updated with
    /// the embedding, but the public read methods still return
    /// empty for the same reason as [`Self::store`] — they need
    /// disk-backed bodies. Follows the same "writes accepted, reads
    /// empty" contract.
    pub async fn store_embedding(&self, episode_id: &str, embedding: Vec<f32>) -> Result<()> {
        // Degraded fallback: skip the disk write, update the in-memory
        // embedding entry only, return success.
        let Some(db) = self.db.clone() else {
            match self.index.write() {
                Ok(mut idx) => {
                    let _ = idx.add_embedding(episode_id, &embedding);
                }
                Err(e) => warn!("index write lock poisoned, skipping embedding update: {e}"),
            }
            return Ok(());
        };
        let ep_id = episode_id.to_string();
        let emb_bytes = bincode::serialize(&embedding).wrap_err("failed to serialize embedding")?;

        tokio::task::spawn_blocking(move || {
            let write_txn = db.begin_write()?;
            {
                let mut table = write_txn.open_table(EMBEDDINGS_TABLE)?;
                table.insert(ep_id.as_str(), emb_bytes.as_slice())?;
            }
            write_txn.commit()?;
            Ok::<_, eyre::Report>(())
        })
        .await??;

        // Attach embedding to the existing in-memory index entry.
        match self.index.write() {
            Ok(mut idx) => {
                idx.add_embedding(episode_id, &embedding);
            }
            Err(e) => warn!("index write lock poisoned, skipping embedding update: {e}"),
        }

        Ok(())
    }

    /// Like [`Self::find_relevant_hybrid_scored`] but applies an
    /// optional `min_best_modality` floor on
    /// [`HybridScore::best_modality`] BEFORE the in-index
    /// combined-rank truncation to `limit`.
    ///
    /// This is the contamination-safe entry point for callers (such as
    /// the agent loop's "Relevant Past Experiences" injection) that
    /// would otherwise face the dead band where `limit` or more
    /// sub-threshold vector-only candidates crowd out a high-
    /// `best_modality` low-`combined` candidate (codex P2 round 2 on
    /// PR #1195). Pushing the floor down into the index ensures the
    /// guarantee holds regardless of memory-store size: if ANY
    /// candidate clears the floor, it reaches the returned set
    /// (subject to `limit`).
    ///
    /// `min_best_modality == None` matches
    /// [`Self::find_relevant_hybrid_scored`] semantics exactly.
    pub async fn find_relevant_hybrid_scored_filtered(
        &self,
        query: &str,
        query_embedding: Option<Vec<f32>>,
        limit: usize,
        min_best_modality: Option<f32>,
        exclude_conversation: bool,
    ) -> Result<Vec<(Episode, HybridScore)>> {
        // When excluding conversation episodes (task recall, #1587), the
        // index has no source field, so over-fetch and filter AFTER
        // resolving episodes, then truncate to `limit`. Excluding is a
        // SAFETY guarantee (a conversation episode can never enter task
        // recall regardless of over-fetch size); the over-fetch only
        // improves COMPLETENESS — under extreme conversation-episode
        // density task recall may still return fewer than `limit`, which is
        // safe (less recall, never contamination).
        const FILTER_OVERFETCH: usize = 8;
        let search_limit = if exclude_conversation {
            limit.saturating_mul(FILTER_OVERFETCH).max(limit)
        } else {
            limit
        };
        // Search the in-memory index
        let matches = {
            let idx = self
                .index
                .read()
                .map_err(|e| eyre::eyre!("index lock poisoned: {e}"))?;
            idx.search_scored_filtered(
                query,
                query_embedding.as_deref(),
                search_limit,
                min_best_modality,
            )
        };

        // Fetch full episodes from DB. Preserve (id, score) pairing so
        // callers can gate on a similarity threshold.
        let id_scores: Vec<(String, HybridScore)> = matches;
        // Degraded fallback: there is no on-disk store to read from.
        // The hybrid index only knows about episodes inserted in this
        // process's lifetime (which is empty at open for a degraded
        // handle) so returning an empty Vec is correct.
        let Some(db) = self.db.clone() else {
            return Ok(Vec::new());
        };

        tokio::task::spawn_blocking(move || {
            let read_txn = db.begin_read()?;
            let table = read_txn.open_table(EPISODES_TABLE)?;

            // Build id -> score map and an id-order index so we can
            // attach the matching score to each fetched episode while
            // preserving the hybrid ranking order.
            let score_by_id: std::collections::HashMap<&str, HybridScore> =
                id_scores.iter().map(|(id, s)| (id.as_str(), *s)).collect();
            let id_order: std::collections::HashMap<&str, usize> = id_scores
                .iter()
                .enumerate()
                .map(|(i, (id, _))| (id.as_str(), i))
                .collect();

            let mut scored: Vec<(Episode, HybridScore)> = Vec::new();
            for (id, _) in &id_scores {
                if let Some(json) = table.get(id.as_str())? {
                    if let Ok(episode) = serde_json::from_str::<Episode>(json.value()) {
                        let score =
                            score_by_id
                                .get(episode.id.as_str())
                                .copied()
                                .unwrap_or(HybridScore {
                                    combined: 0.0,
                                    bm25: 0.0,
                                    vector: 0.0,
                                });
                        scored.push((episode, score));
                    }
                }
            }

            // Preserve the ranking order from hybrid search
            scored.sort_by_key(|(e, _)| id_order.get(e.id.as_str()).copied().unwrap_or(usize::MAX));

            if exclude_conversation {
                scored.retain(|(e, _)| e.source != EpisodeSource::Conversation);
            }
            scored.truncate(limit);
            Ok(scored)
        })
        .await?
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::episode::{Episode, EpisodeOutcome};
    use octos_core::{AgentId, TaskId};
    use std::path::PathBuf;

    fn make_episode(summary: &str, cwd: &str) -> Episode {
        Episode::new(
            TaskId::new(),
            AgentId::new("test-agent"),
            PathBuf::from(cwd),
            summary.into(),
            EpisodeOutcome::Success,
        )
    }

    /// A 768-d embedder (in-process EmbeddingGemma) must actually reach the
    /// vector lane. Before the index width was made configurable this store
    /// was hardcoded to 1536, so every 768-d vector was dropped on insert and
    /// hybrid search silently degraded to BM25-only.
    #[tokio::test]
    async fn open_with_dimension_indexes_vectors_at_a_non_default_width() {
        const DIM: usize = 768;
        let dir = tempfile::tempdir().unwrap();
        let store = EpisodeStore::open_with_dimension(dir.path(), DIM)
            .await
            .unwrap();

        let ep = make_episode("deployed the metal shader cache", "/proj");
        let ep_id = ep.id.clone();
        store.store(ep).await.unwrap();

        // Unit vector pointing at dim 0; the query points the same way.
        let mut embedding = vec![0.0f32; DIM];
        embedding[0] = 1.0;
        store
            .store_embedding(&ep_id, embedding.clone())
            .await
            .unwrap();

        // Query text deliberately shares NO tokens with the summary, so any
        // non-zero score has to come from the vector lane.
        let scored = store
            .find_relevant_hybrid_scored_filtered("zzzz", Some(embedding), 10, None, false)
            .await
            .unwrap();

        let hit = scored
            .iter()
            .find(|(ep, _)| ep.id == ep_id)
            .expect("768-d vector was dropped — index width did not follow the embedder");
        assert!(
            hit.1.vector > 0.9,
            "expected near-1.0 cosine on an identical unit vector, got {}",
            hit.1.vector
        );
    }

    #[tokio::test]
    async fn test_reopen_persists_data() {
        let dir = tempfile::tempdir().unwrap();
        {
            let store = EpisodeStore::open(dir.path()).await.unwrap();
            let ep = make_episode("persistent data", "/proj");
            store.store(ep).await.unwrap();
        }
        // Reopen and verify data persists via the hybrid recall path.
        let store = EpisodeStore::open(dir.path()).await.unwrap();
        let results = store
            .find_relevant_hybrid_scored_filtered("persistent data", None, 10, None, false)
            .await
            .unwrap();
        assert!(
            results
                .iter()
                .any(|(ep, _)| ep.summary == "persistent data"),
            "reopened store must still recall the persisted episode"
        );
    }
}
