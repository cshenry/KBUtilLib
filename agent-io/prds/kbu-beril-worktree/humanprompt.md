# kbu-beril-worktree — human summary

**The pain:** I want several BERIL projects open in parallel — a Claude session
on `foo`, another on `bar` — but BERIL is branch-per-project and one working tree
holds one branch at a time. Two sessions in the same folder clobber each other.
`beril start` doesn't help, and worse, it does an unconditional release-tag
checkout that would yank a worktree off its project branch.

**The fix:** a `kbu beril worktree` command group that runs **one git worktree per
project** — each its own directory on its own `projects/<id>` branch, all backed
by the primary BERIL `.git`. Project dirs are disjoint, so parallel work is
conflict-free.

**Commands:**
- `new <id> [--open]` — create (or re-adopt) a worktree under a configurable root,
  symlink the gitignored `.env` + `.venv-berdl` into it, write a per-worktree
  Cursor workspace file. `--open` launches Cursor.
- `open <id>` — launch Cursor on the worktree → full Claude **extension** per
  window (my flow). Recreates the worktree first if it's gone.
- `start <id> [--agent …]` — launch a CLI agent in the worktree for JupyterHub
  users with no Cursor. A faithful proxy of `beril start` that skips **only** the
  release-checkout.
- `rm <id> [--force]` — remove the worktree **directory only** and **keep the
  branch**. Idempotent (no-op if already gone), refuses on uncommitted changes.
- `ls` — live worktrees + reopenable `projects/*` branches.
- `set-root <path>` / `doctor`.

**Key design facts:**
- **No merge-back.** Worktrees share one `.git`; the `projects/<id>` branch lives
  in the primary repo the whole time. `rm` keeps it. To take a project to `main`,
  recreate the worktree, open Claude, run `/submit` — BERIL's reviewed-PR path.
  kbu never touches `main`.
- **One repo (KBUtilLib only).** No BERIL PR. The launch proxy *imports* BERIL's
  own helpers (`_sync_auth_token`, `get_vertex_config`, `get_default_agent`) and
  replicates only ~15 lines of `run_start` glue **minus** the checkout — so it
  tracks upstream instead of drifting. `kbu beril worktree doctor` + a unit test
  guard the imports and fail loud on a BERIL rename.
- **Config is a kbu setting**, persisted in `~/.kbutillib/config.yaml` (a `beril`
  section: `root`, `worktree_root`), with env overrides (`BERIL_ROOT`,
  `WORKING_BERIL_DIRECTORY`). My `worktree_root` = `~/Dropbox/Projects/WorkingBERIL/`
  (consciously on Dropbox for cross-machine BERIL ops). Default is a sibling
  `WorkingBERIL/` of the BERIL root.
- **Cursor = Shape B**, one window per worktree; workspace file lives outside the
  git tree so BERIL stays pristine.
- **Hazard:** never run `beril start` directly in a worktree (release-checkout
  detaches it) — use `kbu beril worktree start`.

**Out of scope:** BERIL-repo changes, merging to main, cross-machine
orchestration, Spark contention scheduling, non-Cursor IDEs, multi-root
single-window mode.
