//! Hardened controller-side `git` plumbing for worktree admin cleanup.
//!
//! Surviving slice of the retired fleet-worktree mechanism: `octos-agent`'s
//! spawn tool (`tools/spawn.rs`) calls [`clear_worktree_admin_entry`] when it
//! removes a worktree-isolated worker's checkout, and every git op here is
//! routed through the hardened [`git_command`] builder.
//!
//! # SECURITY — controller-side git hardening
//!
//! EVERY git op here goes through [`git_command`], which (1) invokes an
//! ABSOLUTE `git` ([`GIT_BIN`], never `$PATH` — so a planted `git` in a
//! controller-`$PATH` dir is never run), (2) STRIPS provider/API-key +
//! injection env vars ([`crate::env_hygiene`] — so no controller secret is
//! inherited), and (3) sets hooks (`core.hooksPath=<null device>`), fsmonitor
//! (`core.fsmonitor=`), and global/system config
//! (`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM=<null device>`) to empty, so no
//! hook/fsmonitor fires and no global/system-config `filter.*` runs on a
//! controller op. The null device is [`NULL_DEVICE`] — `/dev/null`, or `NUL`
//! on Windows.

use std::path::{Path, PathBuf};
use std::process::Command;

use eyre::{Result, WrapErr, eyre};

/// The platform's null device: a path that can never be a directory and never
/// holds config. `/dev/null` on Unix, `NUL` on Windows — a reserved device name,
/// so it is the exact analogue.
///
/// On Windows the old hardcoded `/dev/null` did still mask config and hooks, but
/// only by ACCIDENT: it is not the null device there, merely a path that happens
/// not to exist. That is a property of the filesystem, not a guarantee — create
/// `C:\dev\null` and the fence silently opens. `NUL` cannot be a directory or a
/// readable config no matter what is on disk.
#[cfg(not(windows))]
const NULL_DEVICE: &str = "/dev/null";
#[cfg(windows)]
const NULL_DEVICE: &str = "NUL";

/// `-c core.hooksPath=<null device>`: disables ALL git hooks for a
/// controller-side invocation (the null device can never be a hooks directory,
/// so no hook is ever found). One part of the code-exec fence — see the module
/// note and [`git_command`].
#[cfg(not(windows))]
const NO_HOOKS: &str = "core.hooksPath=/dev/null";
#[cfg(windows)]
const NO_HOOKS: &str = "core.hooksPath=NUL";

/// `-c core.fsmonitor=`: override any worker-set local `core.fsmonitor` to EMPTY
/// so no fsmonitor hook program can run on a controller-side git op (defensive
/// belt — these ops don't scan the index, but override unconditionally).
const NO_FSMONITOR: &str = "core.fsmonitor=";

/// Candidate absolute `git` paths, most-preferred first. Every entry MUST be a
/// location a spawned worktree worker cannot write to (see [`GIT_BIN`]).
///
/// On Windows the analogue of `/usr/bin` is `%ProgramFiles%\Git`, which requires
/// Administrator to write. `%LOCALAPPDATA%\Programs\Git` — where Git for Windows
/// installs in per-user mode — is deliberately EXCLUDED for exactly the reason
/// Homebrew is on Unix: it is user-writable, which is the plant vector. A host
/// with only a per-user git therefore fails closed (git not found) rather than
/// running a binary the worker could have replaced.
#[cfg(not(windows))]
const GIT_CANDIDATES: &[&str] = &["/usr/bin/git", "/bin/git"];
#[cfg(windows)]
const GIT_CANDIDATES: &[&str] = &[
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\bin\git.exe",
];

/// Absolute path to the `git` binary, resolved ONCE from NON-worker-writable
/// system locations — NEVER via `$PATH`. A spawned worker's NATIVE tools have
/// full-FS (`FilesystemScope::Host`) write, so a bare `git` (PATH-resolved)
/// would let it plant a fake `git` earlier in the controller's `$PATH` (or in a
/// user-writable dir like `/opt/homebrew/bin`) and have the CONTROLLER, running
/// UNSANDBOXED, execute it. Pin to [`GIT_CANDIDATES`] (root-owned /
/// SIP-protected on Unix, Administrator-only on Windows — not worker-writable);
/// the fallback stays ABSOLUTE so there is never a `$PATH` lookup. Mirrors the
/// timeout-kill's `KILL_BIN`/`PS_BIN`. (Homebrew/`/usr/local` are deliberately
/// NOT candidates — they are user/daemon-writable, which is exactly the plant
/// vector.)
static GIT_BIN: std::sync::LazyLock<PathBuf> = std::sync::LazyLock::new(|| {
    GIT_CANDIDATES
        .iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
        // Keep the fallback ABSOLUTE: a missing git must fail as "cannot spawn
        // this exact path", never degrade into a `$PATH` lookup.
        .unwrap_or_else(|| PathBuf::from(GIT_CANDIDATES[0]))
});

/// Build the base `git -C <repo> …` command with the controller-side code-exec
/// AND secret-exfiltration hardening applied, so a lower-trust worker cannot
/// hijack a CONTROLLER-side git op running OUTSIDE the sandbox:
///
/// - **binary**: an ABSOLUTE `git` ([`GIT_BIN`]), never `$PATH`-resolved, so a
///   worker-planted `git` in a controller-`$PATH` dir is never invoked.
/// - **env**: provider/API-key + injection vars are STRIPPED
///   ([`crate::env_hygiene::sanitize_git_command_env`]) so even a config-driven
///   exec could not read a controller secret (defense in depth).
/// - **hooks**: `-c core.hooksPath=/dev/null` — no hook fires on any controller
///   op (`worktree add`'s post-checkout, etc.).
/// - **fsmonitor**: `-c core.fsmonitor=` — a worker-set local fsmonitor program
///   is overridden to empty (belt).
/// - **global** and **system** config: masked to `/dev/null` via
///   `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM`, so a worker that wrote a
///   `~/.gitconfig` (or `/etc/gitconfig`) `filter.*` can't influence a controller
///   op either.
///
/// EVERY controller-side git invocation MUST go through this builder.
fn git_command(repo: &Path) -> Command {
    let mut cmd = Command::new(&*GIT_BIN);
    cmd.arg("-C")
        .arg(repo)
        .arg("-c")
        .arg(NO_HOOKS)
        .arg("-c")
        .arg(NO_FSMONITOR);
    // Strip EVERY provider secret (heuristic + runtime-registered, e.g.
    // `VERTEX_SA_JSON`) + injection var from the inherited controller env — the
    // SAME set the worker-sandboxed git ops strip. Done BEFORE re-applying the
    // git-specific env below, so those overrides can never be stripped.
    crate::env_hygiene::sanitize_git_command_env(&mut cmd);
    cmd.env("GIT_CONFIG_GLOBAL", NULL_DEVICE)
        .env("GIT_CONFIG_SYSTEM", NULL_DEVICE);
    cmd
}

/// Clear the worktree admin entry for `checkout` and NOTHING else.
///
/// The safe replacement for a repo-global `git worktree prune` on any cleanup
/// path: prune also unregisters worktrees that merely have no checkout on disk
/// yet, which is the normal state mid-`git worktree add`. Best-effort — a repo
/// whose common dir cannot be resolved is left untouched.
pub fn clear_worktree_admin_entry(repo_root: &Path, checkout: &Path) {
    if let Ok(git_dir) = git_common_dir(repo_root) {
        clear_stale_admin_entry(&git_dir, checkout);
    }
}

/// Remove a stale worktree admin entry (`<git_dir>/worktrees/<slug>`) still
/// referencing `checkout` — the leftover a LOCKED worktree's `prune` skips,
/// which keeps the branch recorded as checked-out and wedges relaunch (HIGH #5).
/// Contained-path checked: only an entry physically under `<git_dir>/worktrees`
/// is removed, and only when its recorded `gitdir` resolves to `checkout`.
fn clear_stale_admin_entry(git_dir: &Path, checkout: &Path) {
    let worktrees_dir = git_dir.join("worktrees");
    let Ok(entries) = std::fs::read_dir(&worktrees_dir) else {
        return;
    };
    let want = canonical_leaf(checkout);
    for entry in entries.flatten() {
        let admin = entry.path();
        // Containment: never touch anything outside `<git_dir>/worktrees`.
        if !admin.starts_with(&worktrees_dir) {
            continue;
        }
        let Ok(content) = std::fs::read_to_string(admin.join("gitdir")) else {
            continue;
        };
        // `gitdir` records "<checkout>/.git"; its parent is the checkout.
        let recorded = PathBuf::from(content.trim());
        let recorded_checkout = recorded.parent().map(Path::to_path_buf).unwrap_or(recorded);
        if canonical_leaf(&recorded_checkout) == want && want.is_some() {
            if let Err(error) = std::fs::remove_dir_all(&admin) {
                tracing::warn!(
                    admin = %admin.display(), %error,
                    "fleet worktree: failed to clear stale worktree admin entry",
                );
            }
        }
    }
}

/// Resolve a path's canonical LOCATION even when its leaf does not exist:
/// canonicalize the parent (resolving any symlink) and re-attach the leaf. Used
/// to match a removed checkout against a recorded (canonical) `gitdir`.
fn canonical_leaf(p: &Path) -> Option<PathBuf> {
    let parent = p.parent()?;
    let name = p.file_name()?;
    Some(std::fs::canonicalize(parent).ok()?.join(name))
}

/// The repository's common git directory (`<repo>/.git` for a normal repo),
/// absolute. This is what must be WRITABLE in the worker sandbox for a commit
/// inside a worktree checkout to succeed (objects/refs/logs and the new
/// worktree's admin files all live under it).
fn git_common_dir(repo_root: &Path) -> Result<PathBuf> {
    let raw = run_git(repo_root, &["rev-parse", "--git-common-dir"])?;
    let p = PathBuf::from(raw);
    let abs = if p.is_absolute() {
        p
    } else {
        repo_root.join(p)
    };
    Ok(std::fs::canonicalize(&abs).unwrap_or(abs))
}

/// Run a controller-side `git <args>` in `repo` through [`git_command`] (hooks
/// disabled + global/system config masked), returning trimmed stdout on success
/// or an error carrying stderr on failure. Routing EVERY op through
/// [`git_command`] is what closes the hook/filter code-exec surface across all
/// config sources (module SECURITY note).
fn run_git(repo: &Path, args: &[&str]) -> Result<String> {
    let output = git_command(repo)
        .args(args)
        .output()
        .wrap_err_with(|| format!("failed to run git {}", args.join(" ")))?;
    if !output.status.success() {
        return Err(eyre!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn git_command_neutralizes_all_config_sources() {
        // CRITICAL (codex re-review, fix 3): every controller git op must mask
        // GLOBAL + SYSTEM config to /dev/null AND disable hooks, so no
        // worker-influenced config source can define a `filter.*`/hook. Assert
        // the centralized builder wires all three neutralizers.
        let cmd = git_command(Path::new("/some/repo"));
        let args: Vec<String> = cmd
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert!(
            args.windows(2).any(|w| w[0] == "-c" && w[1] == NO_HOOKS),
            "must disable hooks, args: {args:?}",
        );
        assert!(
            args.windows(2)
                .any(|w| w[0] == "-c" && w[1] == "core.fsmonitor="),
            "must override fsmonitor to empty (belt), args: {args:?}",
        );
        let envs: std::collections::HashMap<String, Option<String>> = cmd
            .get_envs()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().into_owned(),
                    v.map(|v| v.to_string_lossy().into_owned()),
                )
            })
            .collect();
        assert_eq!(
            envs.get("GIT_CONFIG_GLOBAL"),
            Some(&Some(NULL_DEVICE.to_string())),
            "must mask GLOBAL config to the null device",
        );
        assert_eq!(
            envs.get("GIT_CONFIG_SYSTEM"),
            Some(&Some(NULL_DEVICE.to_string())),
            "must mask SYSTEM config to the null device",
        );
    }

    #[test]
    fn controller_git_command_env_is_sanitized() {
        // HIGH (controller-hijack): the controller git op must not inherit ANY
        // controller provider secret — heuristic (`OPENAI_API_KEY`) OR
        // runtime-REGISTERED but non-heuristic (`VERTEX_SA_JSON`). Re-exec THIS
        // test binary with both in its env; the child asserts `git_command` strips
        // both (the same set the worker-sandboxed ops strip) while KEEPING the git
        // config overrides.
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("git_worktree::tests::child_controller_git_env_sanitized")
            .arg("--exact")
            .arg("--ignored")
            .env("OPENAI_API_KEY", "sk-controller-secret")
            .env("VERTEX_SA_JSON", "{\"private_key\":\"sa-json-secret\"}")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "child regression failed\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
    }

    /// Child half of [`controller_git_command_env_is_sanitized`]: runs in the
    /// re-exec'd process with the two provider secrets in its env. Restored
    /// verbatim from 9a9b79e^ after slim5-batch7b dropped it and left the
    /// parent test matching zero tests.
    #[test]
    #[ignore]
    fn child_controller_git_env_sanitized() {
        // Runs in a re-exec'd process holding `OPENAI_API_KEY` + `VERTEX_SA_JSON`.
        // `VERTEX_SA_JSON` is a registered provider secret whose NAME the heuristic
        // does NOT flag, so register it (as `profile_factory` does in production)
        // before building the command.
        assert!(
            !crate::is_secret_env_name("VERTEX_SA_JSON"),
            "VERTEX_SA_JSON must be a name the heuristic does NOT flag",
        );
        crate::register_secret_env_names(["VERTEX_SA_JSON"]);

        let cmd = git_command(Path::new("/some/repo"));
        let envs: Vec<(String, Option<String>)> = cmd
            .get_envs()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().into_owned(),
                    v.map(|v| v.to_string_lossy().into_owned()),
                )
            })
            .collect();
        // The heuristic provider key is STRIPPED.
        assert!(
            envs.iter()
                .any(|(k, v)| k == "OPENAI_API_KEY" && v.is_none()),
            "OPENAI_API_KEY must be stripped from the controller git env: {envs:?}",
        );
        // The REGISTERED (non-heuristic) provider secret is ALSO stripped — the
        // gap this fix closes.
        assert!(
            envs.iter()
                .any(|(k, v)| k == "VERTEX_SA_JSON" && v.is_none()),
            "registered VERTEX_SA_JSON must be stripped from the controller git env: {envs:?}",
        );
        // An injection var is stripped unconditionally.
        assert!(
            envs.iter().any(|(k, v)| k == "LD_PRELOAD" && v.is_none()),
            "LD_PRELOAD must be stripped: {envs:?}",
        );
        // The git-specific overrides REMAIN (config masking intact).
        assert!(
            envs.iter()
                .any(|(k, v)| k == "GIT_CONFIG_GLOBAL" && v.as_deref() == Some(NULL_DEVICE)),
            "GIT_CONFIG_GLOBAL override must remain: {envs:?}",
        );
        assert!(
            envs.iter()
                .any(|(k, v)| k == "GIT_CONFIG_SYSTEM" && v.as_deref() == Some(NULL_DEVICE)),
            "GIT_CONFIG_SYSTEM override must remain: {envs:?}",
        );
    }
}
